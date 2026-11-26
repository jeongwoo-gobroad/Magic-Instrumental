# SPDX-License-Identifier: GPL-3.0-only
"""Combined removals operate on each preceding residual and remain portable/cacheable."""

import io
import json
import shutil
import zipfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from practice_player.audio import is_managed_audio
from practice_player.cache import discover_cached_audio, hash_file
from practice_player.cli import parser, prepare_audio_options
from practice_player.combinations import (
    combination_cache_key,
    combination_signature,
    process_combination,
)
from practice_player.config import MODELS, Paths
from practice_player.magic_bundle import export_magic
from practice_player.magic_import import import_magic, load_imported
from practice_player.variants import (
    ALL_VARIANTS,
    SUPPORTED_VARIANTS,
    canonical_combination,
    removal_targets,
    validate_variant_identity,
    variant_label,
)
from tests.test_magic_bundle import portable_study


class SequentialTestEngine:
    def __init__(self):
        self.calls = []

    def __call__(self, source, output, settings, *_args, **_kwargs):
        samples, rate = sf.read(source, dtype="float32", always_2d=True)
        self.calls.append((settings.model, samples.copy()))
        gains = {"vocals": .1, "guitar": .2, "drums": .3, "bass": .15,
                 "other": .15, "piano": .1, "keys": .6, "synth": .8, "instrumental": .9}
        stems = {}
        for target in dict(MODELS[settings.model].stems):
            path = output / f"{target}.wav"
            sf.write(path, samples * gains[target], rate, subtype="FLOAT")
            stems[target] = path
        return "test", stems


@pytest.fixture
def sequential_engine(monkeypatch):
    engine = SequentialTestEngine()
    monkeypatch.setattr("practice_player.engine.separate_stems", engine)
    return engine


def forbidden(*_args, **_kwargs):
    raise AssertionError("Verified combinations must not infer again")


def test_canonical_identity_is_ordered_and_does_not_change_base_choices():
    assert len(SUPPORTED_VARIANTS) == 15
    assert len(ALL_VARIANTS) == 135
    assert canonical_combination(["no_guitar", "vocals", "no_vocals", "no_guitar"]) == (
        "no_vocals_guitar"
    )
    assert canonical_combination(["vocals", "guitar", "no_guitar", "original"]) is None
    assert removal_targets("no_vocals_guitar") == ("vocals", "guitar")
    assert variant_label("no_vocals_guitar") == "(보컬 + 기타) 없는 반주"
    with pytest.raises(ValueError):
        removal_targets("no_guitar_vocals")
    args = parser().parse_args([
        "separate", "--input", "source.wav", "--variants", "no_guitar", "no_vocals_guitar",
        "--variant", "no_vocals_guitar",
    ])
    assert args.variant == "no_vocals_guitar"


@pytest.mark.parametrize("identifier,owners,factor", [
    ("no_vocals_guitar", ["leap", "sw6"], .9 * .8),
    ("no_keys_synth", ["mega", "mega"], .4 * .2),
    ("no_vocals_guitar_drums_bass_keys_piano_synth",
     ["leap", "sw6", "sw6", "sw6", "mega", "sw6", "mega"], .9 * .8 * .7 * .85 * .4 * .9 * .2),
])
def test_actual_pipeline_removes_from_previous_residual(
    tone, tmp_path, sequential_engine, identifier, owners, factor,
):
    original, _ = sf.read(tone, dtype="float32", always_2d=True)
    before = hash_file(tone)
    paths = Paths(tmp_path / "project")
    result = process_combination(tone, identifier, "auto", "cpu", False, paths)
    actual, rate = sf.read(result.instrumental, dtype="float32", always_2d=True)
    assert [item[0] for item in sequential_engine.calls] == owners
    assert len(sequential_engine.calls) >= 2
    assert not np.array_equal(sequential_engine.calls[0][1], sequential_engine.calls[1][1])
    np.testing.assert_allclose(actual, original * factor, atol=2e-8)
    assert rate == 44100 and len(actual) == len(original)
    assert result.source_hash == before == hash_file(tone)
    assert result.selected_variant == identifier and not result.cached
    variant = result.variants[identifier]
    descriptor = variant.to_dict()
    assert descriptor["mode"] == "without" and descriptor["target"] is None
    assert descriptor["targets"] == list(removal_targets(identifier))
    assert descriptor["model_key"] == "sequential" and descriptor["source_hash"] == before
    assert is_managed_audio(result.instrumental)


def test_combination_reuse_and_discovery_are_verified_and_do_not_infer(
    tone, tmp_path, sequential_engine, monkeypatch,
):
    paths = Paths(tmp_path / "project")
    first = process_combination(tone, "no_vocals_guitar", "auto", "cpu", True, paths)
    renamed = tmp_path / "same-content.wav"
    shutil.copyfile(tone, renamed)
    monkeypatch.setattr("practice_player.engine.separate_stems", forbidden)
    hit = process_combination(renamed, "no_vocals_guitar", "auto", "cuda", True, paths)
    assert hit == replace(first, cached=True)
    before = {path.relative_to(paths.root): path.read_bytes()
              for path in paths.root.rglob("*") if path.is_file()}
    discovered = discover_cached_audio(renamed, paths, variant_id="no_vocals_guitar")
    assert discovered["selected_variant"] == "no_vocals_guitar"
    assert discovered["variants"]["no_vocals_guitar"]["targets"] == ["vocals", "guitar"]
    assert before == {path.relative_to(paths.root): path.read_bytes()
                      for path in paths.root.rglob("*") if path.is_file()}
    assert len(sequential_engine.calls) == 2


@pytest.mark.parametrize("corruption", ["audio", "chain", "signature", "frames"])
def test_corrupt_combination_is_not_advertised_or_trusted(
    tone, tmp_path, sequential_engine, corruption,
):
    paths = Paths(tmp_path / "project")
    result = process_combination(tone, "no_vocals_guitar", "auto", "cpu", False, paths)
    manifest_path = result.instrumental.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if corruption == "audio":
        result.instrumental.write_bytes(b"corrupted")
    elif corruption == "chain":
        manifest["steps"][1]["input_sha256"] = "f" * 64
    elif corruption == "signature":
        manifest["signature"]["steps"][0]["settings"]["model"]["revision"] = "bad-revision"
    else:
        manifest["source_audio"]["frames"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert "no_vocals_guitar" not in discover_cached_audio(tone, paths)["variants"]
    repaired = process_combination(tone, "no_vocals_guitar", "auto", "cpu", False, paths)
    assert hash_file(repaired.instrumental) == result.variants["no_vocals_guitar"].sha256
    assert len(sequential_engine.calls) == 2


@pytest.mark.parametrize("identifier,model", [
    ("no_vocals_guitar", "leap"), ("no_keys_piano", "mega"),
    ("no_guitar_vocals", "auto"), ("no_vocals_vocals", "auto"),
])
def test_invalid_combinations_fail_before_model_work(tone, tmp_path, identifier, model):
    paths = Paths(tmp_path / "not-created")
    with pytest.raises(ValueError):
        process_combination(tone, identifier, model, "cpu", False, paths, processor=forbidden)
    assert not paths.root.exists()


def test_explicit_combination_cli_retains_singles_and_original_provenance(
    tone, tmp_path, sequential_engine,
):
    paths = Paths(tmp_path / "project")
    result = prepare_audio_options(
        tone, "auto", "cpu", False, paths, lambda *_: None,
        variant_ids=("no_vocals", "no_guitar", "no_vocals_guitar"),
        variant_id="no_vocals_guitar",
    )
    assert {"no_vocals", "no_guitar", "no_vocals_guitar"} <= set(result.variants)
    assert result.selected_variant == "no_vocals_guitar"
    assert [item[0] for item in sequential_engine.calls] == ["leap", "sw6", "sw6"]
    assert all(item.source_hash == hash_file(tone) for item in result.variants.values())
    combined, _ = sf.read(result.instrumental)
    original, _ = sf.read(tone)
    np.testing.assert_allclose(combined, original * .72, atol=2e-8)


def test_model_revision_changes_invalidate_only_combination_recipe(monkeypatch):
    before = combination_signature("no_vocals_guitar")
    monkeypatch.setitem(MODELS, "leap", replace(MODELS["leap"], revision="updated"))
    after = combination_signature("no_vocals_guitar")
    assert before != after
    assert before["steps"][1] == after["steps"][1]


@pytest.mark.parametrize("field,value", [
    ("targets", ["guitar", "vocals"]), ("targets", ["vocals", "vocals"]),
    ("targets", ["vocals"]), ("targets", ["vocals", "unknown"]),
    ("target", "vocals"), ("mode", "stem"), ("id", "no_guitar_vocals"),
])
def test_invalid_portable_combination_identity_is_rejected(field, value):
    identity = {"id": "no_vocals_guitar", "label": "combined", "mode": "without",
                "target": None, "targets": ["vocals", "guitar"]}
    identity[field] = value
    with pytest.raises(ValueError):
        validate_variant_identity(identity)


@pytest.mark.parametrize("kind,codec", [("pdf", "wav"), ("gp", "mp3")])
def test_real_preparation_export_import_preserves_combination(
    tmp_path, sequential_engine, kind, codec,
):
    request, _, paths = portable_study(tmp_path / "project", kind, count=4)
    source = Path(request["audio"])
    result = prepare_audio_options(
        source, "auto", "cpu", False, paths, lambda *_: None,
        variant_ids=("no_vocals", "no_guitar", "no_vocals_guitar"),
        variant_id="no_vocals_guitar",
    )
    options = []
    for identifier in ("no_vocals", "no_guitar", "no_vocals_guitar"):
        prepared = result.variants[identifier].to_dict()
        option = {key: prepared[key] for key in ("id", "label", "mode", "target", "path")}
        if "targets" in prepared:
            option["targets"] = prepared["targets"]
        option.update(expected_sha256=prepared["sha256"],
                      expected_source_sha256=prepared["source_hash"],
                      expected_frames=prepared["frames"])
        options.append(option)
    request.update(audio_variants=options, default_audio_variant="no_vocals_guitar",
                   audio_format=codec)
    archive_path = export_magic(request)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == 5
        assert manifest["audio"] == manifest["audio_variants"][2]["audio"]
        assert manifest["audio_variants"][2]["targets"] == ["vocals", "guitar"]
        assert manifest["audio"]["source_sha256"] == hash_file(source)
        samples, rate = sf.read(io.BytesIO(archive.read(manifest["audio"]["path"])))
        assert rate == 44100 and len(samples) == result.source_frames
        if codec == "wav":
            prepared, _ = sf.read(result.instrumental)
            np.testing.assert_allclose(samples, prepared, atol=1 / 32768)
    reader = Paths(tmp_path / "reader")
    reader.create()
    imported = import_magic(archive_path, reader)
    bundle = load_imported(imported["bundle_path"])
    assert bundle["archive_version"] == 5
    assert bundle["default_audio_variant"] == "no_vocals_guitar"
    assert bundle["audio_variants"][2]["targets"] == ["vocals", "guitar"]
    assert len(bundle["parts"]) == (2 if kind == "gp" else 1)
    assert import_magic(archive_path, reader)["cached"]


def test_combination_cannot_be_disguised_as_legacy_format_four(tmp_path):
    from practice_player.magic_import import _validate_manifest

    # Identity validation is performed before decoding or touching any referenced member.
    identity = {"id": "no_vocals_guitar", "label": "combined", "mode": "without",
                "target": None, "targets": ["vocals", "guitar"]}
    with pytest.raises(ValueError):
        validate_variant_identity(identity, allow_combinations=False)
    from tests.test_magic_import import make_package

    with zipfile.ZipFile(make_package(tmp_path / "legacy")) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    altered = deepcopy(manifest)
    altered["version"] = 5
    with pytest.raises(ValueError, match="조합"):
        _validate_manifest(altered, {})


@pytest.mark.parametrize("location", ["namespace", "lock", "output", "manifest"])
def test_linked_combination_paths_preserve_external_files(
    tone, tmp_path, sequential_engine, location,
):
    paths = Paths(tmp_path / "project")
    paths.create()
    namespace = paths.processed / hash_file(tone) / "combinations-v1"
    key = combination_cache_key(combination_signature("no_vocals_guitar"))
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_bytes(b"user file")
    if location == "namespace":
        link, target = namespace, external
    elif location == "lock":
        link, target = namespace / f".{key}.lock", sentinel
    else:
        link = namespace / key / ("combined.wav" if location == "output" else "manifest.json")
        target = sentinel
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=location == "namespace")
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable for this account")
    with pytest.raises(ValueError, match="링크"):
        process_combination(tone, "no_vocals_guitar", "auto", "cpu", False, paths)
    assert sentinel.read_bytes() == b"user file"
    assert link.is_symlink()


def test_failed_stage_never_publishes_combination(tone, tmp_path, sequential_engine, monkeypatch):
    paths = Paths(tmp_path / "project")
    engine = sequential_engine

    def interrupted(source, output, settings, *args, **kwargs):
        if settings.model == "sw6":
            raise RuntimeError("cancelled")
        return engine(source, output, settings, *args, **kwargs)

    monkeypatch.setattr("practice_player.engine.separate_stems", interrupted)
    with pytest.raises(RuntimeError, match="cancelled"):
        process_combination(tone, "no_vocals_guitar", "auto", "cpu", False, paths)
    assert "no_vocals_guitar" not in discover_cached_audio(tone, paths)["variants"]
    assert len(engine.calls) == 1
    monkeypatch.setattr("practice_player.engine.separate_stems", engine)
    result = process_combination(tone, "no_vocals_guitar", "auto", "cpu", False, paths)
    assert result.selected_variant == "no_vocals_guitar"
    assert len(engine.calls) == 2


def test_retry_cleans_only_owned_incomplete_combination_staging(
    tone, tmp_path, sequential_engine,
):
    paths = Paths(tmp_path / "project")
    key = combination_cache_key(combination_signature("no_vocals_guitar"))
    namespace = paths.processed / hash_file(tone) / "combinations-v1"
    partial = namespace / f".{key}-abcdefgh"
    partial.mkdir(parents=True)
    (partial / "combined.wav").write_bytes(b"incomplete worker output")
    unrelated = namespace / f".{key}-ijklmnop"
    unrelated.mkdir()
    protected = unrelated / "user-export.wav"
    protected.write_bytes(b"keep this export")
    process_combination(tone, "no_vocals_guitar", "auto", "cpu", False, paths)
    assert not partial.exists()
    assert protected.read_bytes() == b"keep this export"
