# SPDX-License-Identifier: GPL-3.0-only
"""One recording keeps its original and selected cached mixes in one portable file."""

import io
import json
import shutil
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from practice_player.audio import run_ffmpeg
from practice_player.cache import hash_file
from practice_player.cli import prepare_audio_options
from practice_player.config import Paths, Settings
from practice_player.magic_bundle import _requested_variants, export_magic
from practice_player.magic_import import import_magic, load_imported
from practice_player.pipeline import process
from tests.test_audio_combinations import SequentialTestEngine
from tests.test_magic_bundle import portable_study


def export_choice(variant):
    item = variant.to_dict()
    result = {key: item[key] for key in ("id", "label", "mode", "target", "path")}
    if "targets" in item:
        result["targets"] = item["targets"]
    result.update(expected_sha256=item["sha256"], expected_source_sha256=item["source_hash"],
                  expected_frames=item["frames"])
    return result


@pytest.fixture
def separated_engine(monkeypatch):
    engine = SequentialTestEngine()
    monkeypatch.setattr("practice_player.engine.separate_stems", engine)
    return engine


@pytest.mark.parametrize("kind,codec", [("pdf", "wav"), ("gp", "mp3")])
def test_separate_runs_and_renamed_source_export_one_song_bundle(
    tmp_path, separated_engine, kind, codec, monkeypatch,
):
    request, _, paths = portable_study(tmp_path / "project", kind, count=4)
    source = Path(request["audio"])
    first = prepare_audio_options(
        source, "auto", "cpu", False, paths, lambda *_: None, variant_ids=("no_vocals",),
    )
    renamed = paths.targets / "Renamed Practice.wav"
    shutil.copyfile(source, renamed)
    second = prepare_audio_options(
        renamed, "auto", "cpu", False, paths, lambda *_: None, variant_ids=("no_guitar",),
    )
    assert len(separated_engine.calls) == 2
    assert first.source_hash == second.source_hash == hash_file(renamed)
    assert second.variants["no_vocals"].path == first.instrumental
    options = [export_choice(second.variants[key]) for key in ("no_vocals", "no_guitar")]
    request.update(audio=str(renamed), audio_variants=options, include_original=True,
                   default_audio_variant="no_guitar", audio_format=codec)
    protected = {path: hash_file(path) for path in (
        source, renamed, Path(request["pdf"]), Path(request["timeline"]),
        *(item.path for item in second.variants.values()),
    )}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Saving and opening a bundle must not run a model")

    monkeypatch.setattr("practice_player.engine.separate_stems", forbidden)
    archive_path = export_magic(request)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == 4
        assert [item["id"] for item in manifest["audio_variants"]] == [
            "original", "no_vocals", "no_guitar",
        ]
        assert manifest["default_audio_variant"] == "no_guitar"
        assert manifest["audio"] == manifest["audio_variants"][-1]["audio"]
        assert all(item["audio"]["source_sha256"] == hash_file(source)
                   for item in manifest["audio_variants"])
        assert len([name for name in archive.namelist() if name.startswith("score/source")]) == 1
        assert archive.namelist().count("timeline.json") == 1
    reader = Paths(tmp_path / "reader")
    reader.create()
    imported = load_imported(import_magic(archive_path, reader)["bundle_path"])
    assert [item["id"] for item in imported["audio_variants"]] == [
        "original", "no_vocals", "no_guitar",
    ]
    assert imported["default_audio_variant"] == "no_guitar"
    assert all(hash_file(path) == digest for path, digest in protected.items())


def test_same_filename_with_changed_bytes_never_collects_the_other_song(
    tone, tmp_path, separated_engine,
):
    paths = Paths(tmp_path / "project")
    first = prepare_audio_options(
        tone, "auto", "cpu", False, paths, lambda *_: None, variant_ids=("no_vocals",),
    )
    other = tmp_path / "other" / tone.name
    other.parent.mkdir()
    samples, rate = sf.read(tone)
    sf.write(other, samples * .5, rate, subtype="FLOAT")
    second = prepare_audio_options(
        other, "auto", "cpu", False, paths, lambda *_: None, variant_ids=("no_guitar",),
    )
    assert first.source_hash != second.source_hash
    assert "no_vocals" not in second.variants
    assert all(item.source_hash == second.source_hash for item in second.variants.values())
    assert first.instrumental.is_file()


@pytest.mark.parametrize("input_kind,pitch", [("mono-48000", 2), ("mp3-48000", 0)])
def test_original_is_canonicalized_before_the_shared_key_and_frame_grid(
    tmp_path, input_kind, pitch, separated_engine,
):
    request, _, paths = portable_study(tmp_path / "project", "pdf", count=4)
    initial = Path(request["audio"])
    source = initial.with_suffix(".flac" if input_kind == "mono-48000" else ".mp3")
    codec = ["-c:a", "flac"] if input_kind == "mono-48000" else [
        "-c:a", "libmp3lame", "-b:a", "192k",
    ]
    run_ffmpeg(["-i", str(initial), "-ar", "48000", "-ac", "1", *codec, str(source)])
    plan = Path(request["timeline"])
    data = json.loads(plan.read_text(encoding="utf-8"))
    data["audio_sha256"] = hash_file(source)
    plan.write_text(json.dumps(data), encoding="utf-8")
    result = process(source, Settings("sw6"), paths, variant_id="guitar",
                     stem_runner=separated_engine)
    request.update(audio=str(source), audio_variants=[export_choice(result.variants["guitar"])],
                   include_original=True, default_audio_variant="guitar", audio_format="wav",
                   pitch_semitones=pitch)
    with zipfile.ZipFile(export_magic(request)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert [item["id"] for item in manifest["audio_variants"]] == ["original", "guitar"]
        for item in manifest["audio_variants"]:
            audio = item["audio"]
            assert audio["sample_rate"] == 44100 and audio["channels"] == 2
            assert audio["frames"] == result.source_frames
            assert audio["pitch_semitones"] == pitch
            assert audio["source_sha256"] == hash_file(source)
        original = manifest["audio_variants"][0]["audio"]
        samples, rate = sf.read(io.BytesIO(archive.read(original["path"])))
        from practice_player.transpose import prepare_pitch

        expected, _ = sf.read(prepare_pitch(result.variants["original"].path, paths, pitch))
        np.testing.assert_allclose(samples, expected * manifest["audio_gain"], atol=1 / 32768)
        assert rate == 44100


def test_original_only_selection_needs_no_prepared_descriptor(tmp_path, monkeypatch):
    request, _, _ = portable_study(tmp_path / "project", "pdf", count=4)
    request.update(audio_variants=[], include_original=True, default_audio_variant="original",
                   audio_format="wav")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Original-only export needs no separation or cache preparation")

    monkeypatch.setattr("practice_player.pipeline.process", forbidden)
    monkeypatch.setattr("practice_player.engine.separate_stems", forbidden)
    with zipfile.ZipFile(export_magic(request)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == 4
        assert [item["id"] for item in manifest["audio_variants"]] == ["original"]
        assert manifest["audio"] == manifest["audio_variants"][0]["audio"]


@pytest.mark.parametrize("damage", [None, "payload", "source", "frames"])
def test_supplied_original_is_once_and_its_prepared_identity_is_still_checked(
    tmp_path, damage,
):
    request, _, paths = portable_study(tmp_path / "project", "pdf", count=4)
    source = Path(request["audio"])
    result = process(source, Settings("original"), paths, variant_id="original")
    item = export_choice(result.variants["original"])
    request.update(audio_variants=[item], include_original=True,
                   default_audio_variant="original", audio_format="wav", overwrite=True)
    output = Path(request["output"])
    output.write_bytes(b"previous user export")
    protected = {path: hash_file(path) for path in (
        source, Path(request["pdf"]), Path(request["timeline"]), result.instrumental,
    )}
    if damage:
        field = {"payload": "expected_sha256", "source": "expected_source_sha256",
                 "frames": "expected_frames"}[damage]
        item[field] = item[field] + 1 if damage == "frames" else "f" * 64
        with pytest.raises(ValueError):
            export_magic(request)
        assert output.read_bytes() == b"previous user export"
    else:
        with zipfile.ZipFile(export_magic(request)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert [item["id"] for item in manifest["audio_variants"]] == ["original"]
            assert len([name for name in archive.namelist() if name.startswith("audio/")]) == 1
    assert not list(output.parent.glob(".magic-*"))
    assert all(hash_file(path) == digest for path, digest in protected.items())


def test_changed_source_during_original_decode_preserves_previous_export(tmp_path, monkeypatch):
    from practice_player import magic_bundle

    request, _, _ = portable_study(tmp_path / "project", "pdf", count=4)
    request.update(audio_variants=[], include_original=True, default_audio_variant="original",
                   audio_format="wav", overwrite=True)
    output = Path(request["output"])
    output.write_bytes(b"previous user export")
    decode = magic_bundle.decode

    def changing(source, destination):
        decode(source, destination)
        with source.open("ab") as stream:
            stream.write(b"changed while saving")

    monkeypatch.setattr(magic_bundle, "decode", changing)
    with pytest.raises(ValueError, match="변경"):
        export_magic(request)
    assert output.read_bytes() == b"previous user export"
    assert not list(output.parent.glob(".magic-*"))


@pytest.mark.parametrize("changed_field", ["audio", "pdf", "timeline"])
def test_changed_input_before_export_snapshot_cannot_relabel_the_timeline(
    tmp_path, monkeypatch, changed_field,
):
    from practice_player import magic_bundle

    request, _, _ = portable_study(tmp_path / "project", "pdf", count=4)
    request.update(audio_variants=[], include_original=True, default_audio_variant="original",
                   audio_format="wav", overwrite=True)
    output = Path(request["output"])
    output.write_bytes(b"previous user export")
    changed = Path(request[changed_field])
    original_hash = magic_bundle.hash_file
    mutated = False

    def change_after_first_hash(path):
        nonlocal mutated
        digest = original_hash(path)
        if path == changed and not mutated:
            mutated = True
            with path.open("ab") as stream:
                stream.write(b"\n " if changed_field == "timeline" else b"changed before snapshot")
        return digest

    monkeypatch.setattr(magic_bundle, "hash_file", change_after_first_hash)
    with pytest.raises(ValueError, match="변경"):
        export_magic(request)
    assert mutated
    assert output.read_bytes() == b"previous user export"
    assert not list(output.parent.glob(".magic-*"))


def test_required_original_with_combination_retains_format_and_parenthesized_label(
    tmp_path, separated_engine,
):
    request, _, paths = portable_study(tmp_path / "project", "pdf", count=4)
    result = prepare_audio_options(
        Path(request["audio"]), "auto", "cpu", False, paths, lambda *_: None,
        variant_ids=("no_vocals_guitar",),
    )
    item = export_choice(result.variants["no_vocals_guitar"])
    item["label"] = "보컬 + 기타 없는 반주"
    request.update(audio_variants=[item], include_original=True,
                   default_audio_variant="no_vocals_guitar", audio_format="wav")
    with zipfile.ZipFile(export_magic(request)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == 5
        assert [item["id"] for item in manifest["audio_variants"]] == [
            "original", "no_vocals_guitar",
        ]
        assert manifest["audio_variants"][1]["label"] == "(보컬 + 기타) 없는 반주"
        assert manifest["audio_variants"][1]["targets"] == ["vocals", "guitar"]


@pytest.mark.parametrize("value", [1, "true", None])
def test_include_original_requires_a_boolean(value):
    with pytest.raises(ValueError, match="원곡 포함"):
        _requested_variants({"audio_variants": [], "include_original": value,
                             "default_audio_variant": "original"})


def test_empty_legacy_explicit_selection_still_fails():
    with pytest.raises(ValueError, match="1개 이상"):
        _requested_variants({"audio_variants": [], "default_audio_variant": "original"})
