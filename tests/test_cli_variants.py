# SPDX-License-Identifier: GPL-3.0-only
"""Model orchestration keeps recommended recipes and one original sample clock."""

import json
import shutil
import subprocess
import sys
from dataclasses import replace

import pytest
import soundfile as sf

from practice_player.cache import discover_cached_audio, hash_file
from practice_player.cli import parser, prepare_audio_options
from practice_player.config import ROOT, Paths, Settings
from practice_player.pipeline import Result, process
from practice_player.variants import SUPPORTED_VARIANTS, AudioVariant, supported_variants
from tests.test_audio_variants import KeysSynthEngine, SixStemEngine


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
    monkeypatch.setattr("practice_player.cache.hash_file", lambda _: "a" * 64)
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


def test_checked_batch_groups_recipes_and_uses_first_choice_by_default(tmp_path, recipes):
    calls, _ = recipes
    result = prepare_audio_options(
        tmp_path / "source.wav", "auto", "cpu", False, Paths(tmp_path), None,
        variant_ids=("no_drums", "no_guitar", "no_vocals", "no_drums"),
    )
    assert [call[1].model for call in calls] == ["sw6", "leap"]
    assert [call[4] for call in calls] == [
        {"offline": False, "variant_id": "no_drums", "prepare_all": False,
         "variant_ids": ("no_drums", "no_guitar")},
        {"offline": False, "variant_id": "no_vocals", "prepare_all": False,
         "variant_ids": ("no_vocals",)},
    ]
    assert result.selected_variant == "no_drums"
    assert "no_synth" not in result.variants


@pytest.mark.parametrize("model,selected,batch,all_options", [
    ("auto", None, (), False),
    ("auto", None, ("no_guitar", "unknown"), False),
    ("auto", "no_bass", ("no_guitar",), False),
    ("leap", None, ("no_vocals", "no_guitar"), False),
    ("auto", None, ("no_guitar",), True),
])
def test_invalid_batch_fails_before_any_model(
    tmp_path, recipes, model, selected, batch, all_options,
):
    calls, _ = recipes
    with pytest.raises(ValueError):
        prepare_audio_options(
            tmp_path / "source.wav", model, "cpu", False, Paths(tmp_path), None,
            variant_id=selected, variant_ids=batch, prepare_all=all_options,
        )
    assert not calls


def test_batch_cli_parsing_rejects_ambiguous_all_request():
    args = parser().parse_args([
        "separate", "--input", "source.wav", "--variants", "guitar", "no_drums",
    ])
    assert args.variants == ["guitar", "no_drums"] and args.variant is None
    with pytest.raises(SystemExit):
        parser().parse_args([
            "separate", "--input", "source.wav", "--variants", "guitar", "--all-variants",
        ])


def test_missing_cache_scan_is_read_only_and_has_no_selected_audio(tone, tmp_path):
    paths = Paths(tmp_path / "not-created")
    result = discover_cached_audio(tone, paths, variant_id="guitar")
    assert result == {
        "source_hash": hash_file(tone), "source_frames": 0, "sample_rate": 44100,
        "variants": {}, "selected_variant": None, "instrumental": None,
        "cached": True, "device": "cache",
    }
    assert not paths.root.exists()


def test_discovery_recovers_other_models_and_memory_settings_without_writes(
    tone, tmp_path, fake_engine,
):
    paths = Paths(tmp_path / "project")
    vocals = process(tone, Settings("leap", low_memory=True), paths, runner=fake_engine)
    guitar = process(tone, Settings("sw6"), paths, stem_runner=SixStemEngine(),
                     variant_id="no_guitar")
    synth = process(tone, Settings("mega", low_memory=True), paths,
                    stem_runner=KeysSynthEngine(), variant_id="no_synth")
    before = {path.relative_to(paths.root): path.read_bytes()
              for path in paths.root.rglob("*") if path.is_file()}
    renamed = tmp_path / "renamed.wav"
    shutil.copyfile(tone, renamed)
    result = discover_cached_audio(renamed, paths, variant_id="no_synth")
    assert result["source_hash"] == guitar.source_hash == vocals.source_hash == synth.source_hash
    assert result["selected_variant"] == "no_synth"
    assert result["instrumental"] == str(synth.instrumental)
    assert set(result["variants"]) == {
        "vocals", "no_vocals", "guitar", "no_guitar", "drums", "bass", "piano",
        "keys", "synth", "no_synth", "original",
    }
    assert result["variants"]["vocals"]["model_key"] == "leap"
    assert result["variants"]["guitar"]["model_key"] == "sw6"
    assert result["variants"]["keys"]["model_key"] == "mega"
    assert result["source_frames"] == sf.info(tone).frames
    assert all(path.relative_to(paths.root) in before for path in paths.root.rglob("*")
               if path.is_file())
    assert all((paths.root / path).read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize("damage", ["stem", "manifest", "signature", "missing_device"])
def test_discovery_does_not_advertise_partial_or_stale_model_caches(
    tone, tmp_path, fake_engine, damage,
):
    paths = Paths(tmp_path / "project")
    valid = process(tone, Settings("leap"), paths, runner=fake_engine)
    broken = process(tone, Settings("sw6"), paths, stem_runner=SixStemEngine(),
                     variant_id="no_guitar")
    marker = broken.instrumental.parent / "manifest.json"
    if damage == "stem":
        broken.variants["bass"].path.write_bytes(b"broken cached stem")
    elif damage == "manifest":
        marker.write_text("{}")
    else:
        manifest = json.loads(marker.read_text())
        if damage == "signature":
            manifest["signature"]["model"]["revision"] = "another-revision"
        else:
            manifest.pop("device")
        marker.write_text(json.dumps(manifest))
    result = discover_cached_audio(tone, paths, variant_id="no_guitar")
    assert set(result["variants"]) == set(valid.variants)
    assert result["selected_variant"] == "no_vocals"
    assert result["instrumental"] == str(valid.instrumental)


def test_discovery_prioritizes_recommended_model_then_normal_memory(
    tone, tmp_path, fake_engine,
):
    paths = Paths(tmp_path / "project")
    process(tone, Settings("hyperace"), paths, runner=fake_engine)
    process(tone, Settings("leap", low_memory=True), paths, runner=fake_engine)
    leap = process(tone, Settings("leap"), paths, runner=fake_engine)
    original = process(tone, Settings("original"), paths, variant_id="original")
    result = discover_cached_audio(tone, paths)
    assert result["variants"]["no_vocals"]["path"] == str(leap.instrumental)
    assert result["variants"]["original"]["path"] == str(original.instrumental)


def test_preparation_includes_other_caches_but_keeps_explicit_new_model(
    tone, tmp_path, fake_engine,
):
    paths = Paths(tmp_path / "project")
    leap = process(tone, Settings("leap"), paths, runner=fake_engine)
    hyperace = process(tone, Settings("hyperace"), paths, runner=fake_engine)
    guitar = process(tone, Settings("sw6"), paths, stem_runner=SixStemEngine(),
                     variant_id="no_guitar")
    result = prepare_audio_options(
        tone, "hyperace", "cuda", False, paths, lambda *_: None,
        variant_ids=("no_vocals",), offline=True,
    )
    assert result.cached and result.instrumental == hyperace.instrumental != leap.instrumental
    assert result.variants["no_guitar"].path == guitar.instrumental
    assert result.variants["vocals"].model_key == "hyperace"


def test_cached_audio_cli_and_batch_run_without_ml_imports(tone, tmp_path, fake_engine):
    paths = Paths(tmp_path / "project")
    process(tone, Settings("leap"), paths, runner=fake_engine)
    process(tone, Settings("sw6"), paths, stem_runner=SixStemEngine(),
            variant_id="no_guitar")
    script = """
import sys
from practice_player.cli import main
sys.modules['practice_player.engine'] = None
sys.modules['torch'] = None
sys.modules['pymss'] = None
raise SystemExit(main(sys.argv[1:]))
"""
    for command in (
        ["cached-audio", "--variant", "no_guitar"],
        ["separate", "--device", "cuda", "--offline", "--variants", "no_guitar", "no_vocals"],
    ):
        completed = subprocess.run(
            [sys.executable, "-c", script, *command, "--input", str(tone),
             "--root", str(paths.root), "--events"],
            cwd=ROOT, capture_output=True, encoding="utf-8", timeout=20,
        )
        assert completed.returncode == 0, completed.stderr
        event = json.loads(completed.stdout.split("@@PRACTICE@@")[-1])
        assert event["type"] == ("cached_audio" if command[0] == "cached-audio" else "result")
        assert event["selected_variant"] == "no_guitar" and event["cached"]
        assert {"no_vocals", "no_guitar", "original"} <= event["variants"].keys()


def test_discovery_rechecks_input_bytes(tone, tmp_path, monkeypatch):
    from practice_player import cache

    def changed(*_args, **_kwargs):
        with tone.open("ab") as output:
            output.write(b"changed source")
        return {}

    monkeypatch.setattr(cache, "find_cached_variants", changed)
    with pytest.raises(RuntimeError, match="변경"):
        discover_cached_audio(tone, Paths(tmp_path / "project"))


def test_preparation_rechecks_source_after_collecting_other_cached_variants(
    tone, tmp_path, fake_engine, monkeypatch,
):
    from practice_player import cache

    paths = Paths(tmp_path / "project")
    cached = process(tone, Settings("leap"), paths, runner=fake_engine)
    scan = cache.find_cached_variants

    def changed(*args, **kwargs):
        result = scan(*args, **kwargs)
        with tone.open("ab") as output:
            output.write(b"source changed during cache scan")
        return result

    monkeypatch.setattr(cache, "find_cached_variants", changed)
    with pytest.raises(RuntimeError, match="변경"):
        prepare_audio_options(
            tone, "auto", "cuda", False, paths, lambda *_: None,
            variant_ids=("no_vocals",), offline=True,
        )
    assert cached.instrumental.is_file()
    assert not (paths.processed / hash_file(tone)).exists()


@pytest.mark.parametrize("linked", ["processed", "track", "recipe", "audio"])
def test_discovery_ignores_symlinked_caches(tone, tmp_path, fake_engine, linked):
    paths = Paths(tmp_path / "project")
    prepared = process(tone, Settings("leap"), paths, runner=fake_engine)
    source = {
        "processed": paths.processed,
        "track": prepared.instrumental.parent.parent,
        "recipe": prepared.instrumental.parent,
        "audio": prepared.instrumental,
    }[linked]
    outside = tmp_path / ("outside.wav" if linked == "audio" else "outside")
    source.rename(outside)
    source.symlink_to(outside, target_is_directory=linked != "audio")
    result = discover_cached_audio(tone, paths)
    assert result["variants"] == {} and result["selected_variant"] is None
    assert outside.exists()
