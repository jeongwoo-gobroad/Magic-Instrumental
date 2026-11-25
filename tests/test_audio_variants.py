# SPDX-License-Identifier: GPL-3.0-only
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from practice_player.audio import decode, export_tempo, inspect_audio, is_managed_audio
from practice_player.cache import hash_file, read_cache
from practice_player.config import Paths, Settings
from practice_player.pipeline import process
from practice_player.variants import (
    SUPPORTED_VARIANTS,
    recommended_model,
    supported_variants,
    variant_label,
)


class SixStemEngine:
    def __init__(self):
        self.calls = 0

    def __call__(self, source, output, *_args, **_kwargs):
        self.calls += 1
        mixture, rate = sf.read(source, dtype="float32", always_2d=True)
        portions = {"vocals": .1, "guitar": .2, "drums": .3, "bass": .15,
                    "other": .15, "piano": .1}
        results = {}
        for stem, gain in portions.items():
            path = output / f"{stem}.wav"
            sf.write(path, mixture * gain, rate, subtype="FLOAT")
            results[stem] = path
        return "test", results


class KeysSynthEngine:
    def __init__(self):
        self.calls = 0

    def __call__(self, source, output, *_args, **_kwargs):
        self.calls += 1
        mixture, rate = sf.read(source, dtype="float32", always_2d=True)
        results = {}
        # Overlapping categories must each be subtracted from the original, never summed.
        for stem, gain in (("keys", .6), ("synth", .8)):
            path = output / f"{stem}.wav"
            sf.write(path, mixture * gain, rate, subtype="FLOAT")
            results[stem] = path
        return "test", results


def forbidden(*_args, **_kwargs):
    raise AssertionError("Cached/original audio must not run inference")


def test_model_catalog_distinguishes_guitar_from_other():
    assert "no_guitar" not in supported_variants("leap")
    assert set(supported_variants("sw6")) == set(SUPPORTED_VARIANTS) - {
        "keys", "no_keys", "synth", "no_synth",
    }
    assert set(supported_variants("mega")) == {"keys", "no_keys", "synth", "no_synth", "original"}
    assert recommended_model("vocals") == recommended_model("no_vocals") == "leap"
    assert recommended_model("guitar") == recommended_model("no_bass") == "sw6"
    assert recommended_model("piano") == recommended_model("no_piano") == "sw6"
    assert recommended_model("keys") == recommended_model("no_synth") == "mega"
    assert "피아노" in variant_label("piano")
    assert variant_label("keys") == "키보드만 듣기"
    assert recommended_model("original") == "original"
    with pytest.raises(ValueError):
        supported_variants("unknown")


def test_one_six_stem_inference_reused_across_options(tone, tmp_path):
    engine = SixStemEngine()
    paths, settings = Paths(tmp_path / "project"), Settings(model="sw6")
    first = process(tone, settings, paths, stem_runner=engine, variant_id="no_guitar")
    assert engine.calls == 1 and not first.cached
    assert first.selected_variant == "no_guitar"
    assert "no_bass" not in first.variants
    assert {"guitar", "drums", "bass", "vocals", "piano", "original"} <= first.variants.keys()
    second = process(tone, Settings(model="sw6", device="cuda"), paths,
                     offline=True, stem_runner=forbidden, variant_id="no_bass")
    assert second.cached and engine.calls == 1
    assert first.variants["guitar"].path == second.variants["guitar"].path
    all_options = process(tone, settings, paths, offline=True, stem_runner=forbidden,
                          variant_id="no_drums", prepare_all=True)
    assert set(all_options.variants) == set(supported_variants("sw6"))
    assert all_options.source_frames == 3 * 44100
    mixture = sf.read(all_options.variants["original"].path, dtype="float32")[0]
    for target in ("vocals", "guitar", "drums", "bass", "piano"):
        isolated = sf.read(all_options.variants[target].path, dtype="float32")[0]
        backing = sf.read(all_options.variants[f"no_{target}"].path, dtype="float32")[0]
        np.testing.assert_allclose(isolated + backing, mixture, atol=1e-8)
    event = all_options.to_dict()
    for value, info in event["variants"].items():
        assert info["id"] == value and info["frames"] == all_options.source_frames
        assert info["bytes"] == Path(info["path"]).stat().st_size
        assert info["sha256"] == hash_file(Path(info["path"]))
    assert event["variants"]["original"]["target"] is None
    assert event["variants"]["original"]["mode"] == "original"


def test_checked_residuals_are_prepared_in_one_pass_without_unchecked_mixes(tone, tmp_path):
    engine = SixStemEngine()
    paths, settings = Paths(tmp_path / "project"), Settings(model="sw6")
    batch = process(
        tone, settings, paths, stem_runner=engine, variant_id="no_guitar",
        variant_ids=("no_guitar", "no_bass", "no_guitar"),
    )
    assert engine.calls == 1 and not batch.cached
    assert {"no_guitar", "no_bass"} <= batch.variants.keys()
    assert "no_drums" not in batch.variants and "no_piano" not in batch.variants
    assert not (batch.instrumental.parent / "no_drums.wav").exists()
    mixture = sf.read(batch.variants["original"].path, dtype="float32")[0]
    for target in ("guitar", "bass"):
        solo = sf.read(batch.variants[target].path, dtype="float32")[0]
        residual = sf.read(batch.variants[f"no_{target}"].path, dtype="float32")[0]
        np.testing.assert_allclose(solo + residual, mixture, atol=1e-8)
    again = process(
        tone, settings, paths, stem_runner=forbidden, offline=True, variant_id="no_drums",
        variant_ids=("no_drums", "no_guitar"),
    )
    assert again.cached and engine.calls == 1
    assert "no_drums" in again.variants and "no_piano" not in again.variants
    assert again.variants["no_guitar"] == batch.variants["no_guitar"]


def test_existing_six_stem_cache_adds_piano_choices_without_inference(tone, tmp_path):
    paths, settings, engine = Paths(tmp_path / "project"), Settings(model="sw6"), SixStemEngine()
    guitar = process(tone, settings, paths, stem_runner=engine, variant_id="no_guitar")
    directory = guitar.instrumental.parent
    marker = directory / "manifest.json"
    previous = json.loads(marker.read_text())
    previous["variants"].pop("piano")
    marker.write_text(json.dumps(previous))
    original_files = {name: hash_file(directory / name) for name in previous["files"]}

    piano = process(tone, Settings(model="sw6", device="cuda"), paths, offline=True,
                    stem_runner=forbidden, variant_id="piano")
    backing = process(tone, settings, paths, offline=True, stem_runner=forbidden,
                      variant_id="no_piano")
    assert piano.cached and backing.cached and engine.calls == 1
    assert piano.instrumental == directory / "piano.wav"
    assert backing.instrumental == directory / "no_piano.wav"
    assert {"piano", "no_piano", "guitar", "no_guitar"} <= backing.variants.keys()
    assert all(hash_file(directory / name) == digest for name, digest in original_files.items())
    assert backing.variants["piano"].target == backing.variants["no_piano"].target == "piano"
    mixture = sf.read(backing.variants["original"].path, dtype="float32")[0]
    solo = sf.read(piano.instrumental, dtype="float32")[0]
    residual = sf.read(backing.instrumental, dtype="float32")[0]
    assert len(solo) == len(residual) == backing.source_frames
    np.testing.assert_allclose(solo + residual, mixture, atol=1e-8)


def test_keyboard_and_synth_share_one_inference_but_not_a_combined_mix(tone, tmp_path):
    paths, settings, engine = Paths(tmp_path / "project"), Settings(model="mega"), KeysSynthEngine()
    keyboard = process(tone, settings, paths, stem_runner=engine, variant_id="no_keys")
    assert engine.calls == 1 and not keyboard.cached
    assert {"keys", "no_keys", "synth", "original"} == set(keyboard.variants)
    synthesizer = process(tone, Settings(model="mega", device="cuda"), paths,
                          offline=True, stem_runner=forbidden, variant_id="no_synth")
    assert synthesizer.cached and engine.calls == 1
    assert set(synthesizer.variants) == set(supported_variants("mega"))
    assert synthesizer.variants["keys"].path == keyboard.variants["keys"].path
    mixture = sf.read(synthesizer.variants["original"].path, dtype="float32")[0]
    for target, fraction in (("keys", .6), ("synth", .8)):
        solo = sf.read(synthesizer.variants[target].path, dtype="float32")[0]
        residual = sf.read(synthesizer.variants[f"no_{target}"].path, dtype="float32")[0]
        assert len(solo) == len(residual) == synthesizer.source_frames
        np.testing.assert_allclose(solo, mixture * fraction, atol=1e-8)
        np.testing.assert_allclose(residual, mixture * (1 - fraction), atol=1e-8)
        np.testing.assert_allclose(solo + residual, mixture, atol=1e-8)
    assert "piano" not in synthesizer.variants


@pytest.mark.parametrize("target", ["keys", "synth"])
def test_six_stem_model_does_not_claim_general_keyboard_targets(tone, tmp_path, target):
    with pytest.raises(ValueError, match="지원하지"):
        process(tone, Settings(model="sw6"), Paths(tmp_path / "project"),
                variant_id=target, stem_runner=forbidden)


def test_original_does_not_load_model_even_with_explicit_unavailable_gpu(tone, tmp_path):
    paths = Paths(tmp_path / "project")
    settings = Settings(model="original", device="cuda")
    result = process(tone, settings, paths, variant_id="original", offline=True,
                     stem_runner=forbidden)
    assert set(result.variants) == {"original"}
    assert sf.info(result.instrumental).frames == 3 * 44100
    again = process(tone, settings, paths, variant_id="original", offline=True,
                    stem_runner=forbidden)
    assert again.cached and again.instrumental == result.instrumental


def test_concurrent_different_targets_share_one_inference(tone, tmp_path):
    paths, settings, engine = Paths(tmp_path / "project"), Settings(model="sw6"), SixStemEngine()
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(
            lambda value: process(tone, settings, paths, variant_id=value, stem_runner=engine),
            ("no_guitar", "no_bass"),
        ))
    assert engine.calls == 1
    assert {result.selected_variant for result in results} == {"no_guitar", "no_bass"}
    assert sorted(result.cached for result in results) == [False, True]


@pytest.mark.parametrize("model,low_memory,key", [
    ("hyperace", False, "hyperace-07c78ae0ade5b22a"),
    ("hyperace", True, "hyperace-18bf039fba2b07ee"),
    ("deux", False, "deux-938651baf4321e98"),
    ("deux", True, "deux-6d1b74083cd2e3ac"),
])
def test_legacy_keys_match_released_249(model, low_memory, key):
    settings = Settings(model=model, low_memory=low_memory)
    assert settings.legacy_cache_key == key
    assert settings.cache_key != key


@pytest.mark.parametrize("model,normal,low_memory", [
    ("hyperace", "4d16f1f8b3f05c84", "70aae1815fecd871"),
    ("deux", "f4a8d2d0944aa779", "da78954cf9b5d754"),
    ("leap", "0730936f5c71a55a", "37f401d18d7af6a5"),
    ("sw6", "50e6f1392b7648ca", "9efba125818a7851"),
    ("original", "e4178ab683721805", "77cb7196ce12237a"),
])
def test_keyboard_model_source_does_not_invalidate_existing_v3_cache(model, normal, low_memory):
    assert Settings(model=model).cache_key == f"{model}-{normal}"
    assert Settings(model=model, low_memory=True).cache_key == f"{model}-{low_memory}"


@pytest.mark.parametrize("tail", [-1000, 0, 1000])
def test_legacy_cache_migration_preserves_bytes_without_inference(tone, tmp_path, tail):
    paths, settings = Paths(tmp_path / "project"), Settings(device="cuda")
    source_hash = hash_file(tone)
    directory = paths.processed / source_hash / settings.legacy_cache_key
    directory.mkdir(parents=True)
    instrumental = directory / "instrumental.wav"
    samples, rate = sf.read(tone, dtype="float32", always_2d=True)
    legacy_samples = samples * .25
    if tail < 0:
        legacy_samples = legacy_samples[:tail]
    elif tail > 0:
        legacy_samples = np.pad(legacy_samples, ((0, tail), (0, 0)))
    sf.write(instrumental, legacy_samples, rate, subtype="FLOAT")
    manifest = {
        "source_sha256": source_hash, "signature": settings.legacy_signature(), "device": "mps",
        "output": {"bytes": instrumental.stat().st_size, "sha256": hash_file(instrumental)},
        "audio": inspect_audio(instrumental),
    }
    marker = directory / "manifest.json"
    marker.write_text(json.dumps(manifest), encoding="utf-8")
    originals = {path: path.read_bytes() for path in (instrumental, marker)}
    result = process(tone, settings, paths, offline=True, stem_runner=forbidden, prepare_all=True)
    assert result.cached and result.device == "mps"
    assert result.instrumental != instrumental
    assert result.source_frames == len(samples)
    assert all(path.read_bytes() == data for path, data in originals.items())
    for option in result.variants.values():
        assert sf.info(option.path).frames == len(samples)
    current = json.loads((result.instrumental.parent / "manifest.json").read_text())
    assert current["origin"]["pipeline"] == 2
    assert current["tail_adjustments"]["instrumental"] == tail
    backing = sf.read(result.instrumental, dtype="float32")[0]
    np.testing.assert_array_equal(backing[:100], legacy_samples[:100])


@pytest.mark.parametrize("fault", ["missing", "long", "nan"])
def test_invalid_multistem_output_never_publishes(tone, tmp_path, fault):
    engine = SixStemEngine()
    paths = Paths(tmp_path / "project")

    def invalid(source, output, *args, **kwargs):
        device, results = engine(source, output, *args, **kwargs)
        if fault == "missing":
            del results["guitar"]
        else:
            samples, rate = sf.read(results["guitar"], dtype="float32", always_2d=True)
            if fault == "long":
                samples = np.pad(samples, ((0, 4411), (0, 0)))
            else:
                samples[0, 0] = np.nan
            sf.write(results["guitar"], samples, rate, subtype="FLOAT")
        return device, results

    with pytest.raises((ValueError, RuntimeError)):
        process(tone, Settings(model="sw6"), paths, variant_id="no_guitar", stem_runner=invalid)
    assert not list(paths.processed.rglob("manifest.json"))


def test_corrupt_multistem_repair_preserves_user_exports(tone, tmp_path):
    paths, settings, engine = Paths(tmp_path / "project"), Settings(model="sw6"), SixStemEngine()
    result = process(tone, settings, paths, variant_id="no_guitar", stem_runner=engine)
    saved = result.instrumental.parent / "my-practice.wav"
    saved.write_bytes(b"keep-user-export")
    result.variants["bass"].path.write_bytes(b"bad")
    repaired = process(tone, settings, paths, variant_id="no_bass", stem_runner=engine)
    assert not repaired.cached and engine.calls == 2
    assert saved.read_bytes() == b"keep-user-export"
    assert read_cache(repaired.instrumental.parent, repaired.source_hash, settings.signature())


def test_source_changed_after_inference_does_not_publish(tone, tmp_path):
    paths, engine = Paths(tmp_path / "project"), SixStemEngine()

    def changing(*args, **kwargs):
        result = engine(*args, **kwargs)
        with tone.open("ab") as output:
            output.write(b"source-changed")
        return result

    with pytest.raises(RuntimeError, match="변경"):
        process(tone, Settings(model="sw6"), paths, variant_id="guitar", stem_runner=changing)
    assert not list(paths.processed.rglob("manifest.json"))


def test_unsupported_target_does_not_start_inference(tone, tmp_path):
    with pytest.raises(ValueError, match="지원하지"):
        process(tone, Settings(model="leap"), Paths(tmp_path / "project"),
                variant_id="guitar", stem_runner=forbidden)


def test_cache_rejects_traversing_payload_path(tone, tmp_path):
    paths, settings = Paths(tmp_path / "project"), Settings(model="sw6")
    result = process(tone, settings, paths, variant_id="guitar", stem_runner=SixStemEngine())
    marker = result.instrumental.parent / "manifest.json"
    manifest = json.loads(marker.read_text())
    manifest["variants"]["guitar"] = "../outside.wav"
    marker.write_text(json.dumps(manifest))
    assert read_cache(result.instrumental.parent, result.source_hash, settings.signature()) is None


def test_prepared_options_and_imported_assets_cannot_be_overwritten(tone, tmp_path):
    paths = Paths(tmp_path / "project")
    result = process(tone, Settings(model="sw6"), paths,
                     variant_id="no_guitar", stem_runner=SixStemEngine(), prepare_all=True)
    for option in result.variants.values():
        assert is_managed_audio(option.path)
        with pytest.raises(ValueError, match="캐시"):
            export_tempo(tone, option.path, 1, overwrite=True)
    imported = paths.processed / "imported-magic" / ("a" * 64) / "v1"
    asset = imported / "assets" / "audio" / "variants" / "guitar.wav"
    asset.parent.mkdir(parents=True)
    decode(tone, asset)
    (imported / "bundle.json").write_text("{}")
    (imported / "cache-manifest.json").write_text("{}")
    assert is_managed_audio(asset)
    with pytest.raises(ValueError, match="캐시"):
        export_tempo(tone, asset, 1, overwrite=True)
    assert not is_managed_audio(tone)
