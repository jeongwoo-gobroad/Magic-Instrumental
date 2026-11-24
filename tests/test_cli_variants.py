# SPDX-License-Identifier: GPL-3.0-only
"""Model orchestration keeps recommended recipes and one original sample clock."""

from dataclasses import replace

import pytest
import soundfile as sf

from practice_player.cli import parser, prepare_audio_options
from practice_player.config import Paths
from practice_player.pipeline import Result
from practice_player.variants import SUPPORTED_VARIANTS, AudioVariant, supported_variants


@pytest.fixture
def recipes(tmp_path, monkeypatch):
    calls = []
    results = {}
    for model in ("leap", "sw6", "mega", "original"):
        variants = {
            identifier: AudioVariant(
                identifier, tmp_path / model / f"{identifier}.wav", "a" * 64,
                441000, model, f"{model}-recipe",
            )
            for identifier in supported_variants(model)
        }
        selected = next(iter(variants))
        results[model] = Result(
            variants[selected].path, "a" * 64, model != "sw6", "cuda" if model == "sw6" else "cpu",
            selected, variants, 441000, 44100,
        )

    def run(source, settings, paths, report, **options):
        calls.append((source, settings, paths, report, options))
        return results[settings.model]

    monkeypatch.setattr("practice_player.pipeline.process", run)
    return calls, results


def test_auto_prepares_each_recipe_once_and_preserves_assigned_priority(tmp_path, recipes):
    calls, results = recipes
    source, paths, report = tmp_path / "source.wav", Paths(tmp_path / "project"), object()
    result = prepare_audio_options(
        source, "auto", "cuda", True, paths, report,
        variant_id="no_drums", prepare_all=True, offline=True,
    )
    assert [call[1].model for call in calls] == ["leap", "sw6", "mega", "original"]
    assert all(call[0] == source and call[2] == paths and call[3] is report for call in calls)
    assert all(call[1].device == "cuda" and call[1].low_memory for call in calls)
    assert [call[4] for call in calls] == [
        {"offline": True, "variant_id": "no_vocals", "prepare_all": True},
        {"offline": True, "variant_id": "no_drums", "prepare_all": True},
        {"offline": True, "variant_id": "no_keys", "prepare_all": True},
        {"offline": True, "variant_id": "original", "prepare_all": True},
    ]
    assert tuple(result.variants) == SUPPORTED_VARIANTS
    assert all(result.variants[value] is results["leap"].variants[value]
               for value in ("vocals", "no_vocals"))
    assert all(result.variants[value] is results["sw6"].variants[value]
               for value in ("guitar", "no_guitar", "drums", "no_drums", "bass", "no_bass",
                             "piano", "no_piano"))
    assert all(result.variants[value] is results["mega"].variants[value]
               for value in ("keys", "no_keys", "synth", "no_synth"))
    assert result.variants["original"] is results["original"].variants["original"]
    assert result.instrumental == results["sw6"].variants["no_drums"].path
    assert result.selected_variant == "no_drums"
    assert not result.cached and result.device == "cuda"
    assert (result.source_hash, result.source_frames, result.sample_rate) == (
        "a" * 64, 441000, 44100,
    )


def test_all_cache_hits_remain_a_cache_hit_across_models(tmp_path, recipes):
    calls, results = recipes
    results["sw6"] = replace(results["sw6"], cached=True)
    result = prepare_audio_options(
        tmp_path / "source.wav", "auto", "cpu", False, Paths(tmp_path), None,
        prepare_all=True,
    )
    assert len(calls) == 4 and result.cached


@pytest.mark.parametrize("model,selected,expected", [
    ("auto", "guitar", {"guitar", "no_guitar", "drums", "no_drums", "bass", "no_bass",
                        "piano", "no_piano"}),
    ("auto", "piano", {"guitar", "no_guitar", "drums", "no_drums", "bass", "no_bass",
                       "piano", "no_piano"}),
    ("auto", "keys", {"keys", "no_keys", "synth", "no_synth"}),
    ("auto", "vocals", {"vocals", "no_vocals"}),
    ("auto", "original", {"original"}),
    ("sw6", "guitar", set(supported_variants("sw6"))),
    ("mega", "keys", set(supported_variants("mega"))),
])
def test_one_option_keeps_only_ready_versions_from_the_assigned_recipe(
    tmp_path, recipes, model, selected, expected,
):
    calls, _ = recipes
    result = prepare_audio_options(
        tmp_path / "source.wav", model, "cpu", False, Paths(tmp_path), None,
        variant_id=selected,
    )
    assert len(calls) == 1 and calls[0][4]["prepare_all"] is False
    assert result.selected_variant == selected and set(result.variants) == expected


@pytest.mark.parametrize("model", ["hyperace", "deux", "sw6", "leap", "mega"])
def test_original_only_ignores_manual_separation_model_and_explicit_cuda(
    tone, tmp_path, monkeypatch, model,
):
    from practice_player import pipeline

    calls, process = [], pipeline.process

    def record(source, settings, *args, **kwargs):
        calls.append(settings)
        return process(source, settings, *args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("An original-only request must not invoke a separation model")

    monkeypatch.setattr(pipeline, "process", record)
    monkeypatch.setattr("practice_player.engine.separate_stems", forbidden)
    result = prepare_audio_options(
        tone, model, "cuda", False, Paths(tmp_path / "project"), lambda *_: None,
        variant_id="original", offline=True,
    )
    assert len(calls) == 1 and calls[0].model == "original" and calls[0].device == "cuda"
    assert result.selected_variant == "original" and set(result.variants) == {"original"}
    original = result.variants["original"]
    assert original.model_key == "original" and original.path == result.instrumental
    assert original.mode == "original" and original.target is None
    assert original.frames == result.source_frames == sf.info(tone).frames
    assert sf.info(result.instrumental).frames == original.frames
    assert original.sample_rate == result.sample_rate == 44100


@pytest.mark.parametrize("field,value", [
    ("source_hash", "b" * 64), ("source_frames", 441001), ("sample_rate", 48000),
])
def test_cross_recipe_source_or_sample_grid_mismatch_is_rejected(tmp_path, recipes, field, value):
    calls, results = recipes
    results["sw6"] = replace(results["sw6"], **{field: value})
    with pytest.raises(ValueError, match="원곡 또는 음원 길이"):
        prepare_audio_options(
            tmp_path / "source.wav", "auto", "cpu", False, Paths(tmp_path), None,
            prepare_all=True,
        )
    assert [call[1].model for call in calls] == ["leap", "sw6"]


def test_missing_assigned_output_is_not_replaced_by_another_model(tmp_path, recipes):
    _, results = recipes
    results["leap"].variants.pop("vocals")
    with pytest.raises(ValueError, match="선택한 음원 결과"):
        prepare_audio_options(
            tmp_path / "source.wav", "auto", "cpu", False, Paths(tmp_path), None,
            prepare_all=True,
        )


def test_explicit_vocal_model_cannot_claim_a_guitar_output(tmp_path):
    with pytest.raises(ValueError, match="지원하지"):
        prepare_audio_options(
            tmp_path / "nonexistent.wav", "leap", "cpu", False, Paths(tmp_path), None,
            variant_id="guitar",
        )


@pytest.mark.parametrize("identifier", [
    "piano", "no_piano", "keys", "no_keys", "synth", "no_synth",
])
def test_keyboard_piano_synth_cli_options_are_valid(identifier):
    args = parser().parse_args(["separate", "--input", "source.wav", "--variant", identifier])
    assert args.variant == identifier and args.model == "auto"
