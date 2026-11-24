# SPDX-License-Identifier: GPL-3.0-only
"""Pitch cache identities and media clocks remain valid across every playback option."""

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.transpose import PITCH_VERSION, prepare_pitch


@pytest.fixture
def source_audio(tmp_path):
    paths = Paths(tmp_path / "desktop")
    paths.create()
    source = tmp_path / "source.wav"
    times = np.arange(44100 + 137) / 44100
    tone = 0.2 * np.sin(2 * np.pi * 440 * times)
    sf.write(source, np.column_stack((tone, tone * 0.7)), 44100, subtype="FLOAT")
    return source, paths


def metadata(target):
    path = target.with_name("pitch.json")
    return path, json.loads(path.read_text())


def test_existing_valid_v1_pitch_cache_is_reused_without_reencoding(source_audio, monkeypatch):
    import practice_player.transpose as module

    source, paths = source_audio
    target = prepare_pitch(source, paths, 2)
    manifest_path, manifest = metadata(target)
    assert manifest == {
        "version": 1,
        "source_sha256": hash_file(source),
        "semitones": 2,
        "sha256": hash_file(target),
    }
    previous = (target.read_bytes(), manifest_path.read_bytes())
    monkeypatch.setattr(module, "run_ffmpeg", lambda *_: pytest.fail("valid cache reencoded"))
    assert prepare_pitch(source, paths, 2) == target
    assert (target.read_bytes(), manifest_path.read_bytes()) == previous
    assert PITCH_VERSION == 1


@pytest.mark.parametrize(
    "change",
    [
        {"version": 999},
        {"version": True},
        {"source_sha256": "b" * 64},
        {"semitones": -2},
        {"semitones": 2.0},
        {"sha256": "c" * 64},
    ],
)
def test_wrong_recipe_identity_rebuilds_a_matching_hash_cache(source_audio, change, monkeypatch):
    import practice_player.transpose as module

    source, paths = source_audio
    target = prepare_pitch(source, paths, 2)
    manifest_path, manifest = metadata(target)
    manifest.update(change)
    manifest_path.write_text(json.dumps(manifest))
    calls = []
    run = module.run_ffmpeg

    def encode(args):
        calls.append(args)
        return run(args)

    monkeypatch.setattr(module, "run_ffmpeg", encode)
    assert prepare_pitch(source, paths, 2) == target
    assert len(calls) == 1
    restored = json.loads(manifest_path.read_text())
    assert restored["version"] == 1 and restored["source_sha256"] == hash_file(source)
    assert type(restored["semitones"]) is int and restored["semitones"] == 2
    assert restored["sha256"] == hash_file(target)


@pytest.mark.parametrize("broken", [b"{broken", b"[]", b"\xff", b"x" * 65537])
def test_malformed_or_oversized_manifest_recovers_without_manual_cache_deletion(
    source_audio, broken
):
    source, paths = source_audio
    target = prepare_pitch(source, paths, 2)
    manifest_path = target.with_name("pitch.json")
    manifest_path.write_bytes(broken)
    assert prepare_pitch(source, paths, 2) == target
    assert json.loads(manifest_path.read_text())["source_sha256"] == hash_file(source)


@pytest.mark.parametrize("fault", ["frames", "rate", "channels", "nonfinite"])
def test_hash_consistent_wrong_clock_or_nonfinite_output_is_rebuilt(source_audio, fault):
    source, paths = source_audio
    target = prepare_pitch(source, paths, 2)
    original = sf.info(source)
    samples = np.zeros(
        (
            original.frames - 200 if fault == "frames" else original.frames,
            1 if fault == "channels" else 2,
        ),
        dtype=np.float32,
    )
    if fault == "nonfinite":
        samples[100, 0] = np.nan
    sf.write(target, samples, 48000 if fault == "rate" else 44100, subtype="FLOAT")
    manifest_path, manifest = metadata(target)
    manifest["sha256"] = hash_file(target)
    manifest_path.write_text(json.dumps(manifest))
    assert prepare_pitch(source, paths, 2) == target
    result, rate = sf.read(target, always_2d=True)
    assert rate == 44100 and result.shape == (original.frames, 2)
    assert np.isfinite(result).all() and np.max(np.abs(result)) > 0.01


def test_source_change_during_encoding_preserves_previous_cache_and_user_exports(
    source_audio, monkeypatch
):
    import practice_player.transpose as module

    source, paths = source_audio
    target = prepare_pitch(source, paths, 2)
    manifest_path, manifest = metadata(target)
    manifest["version"] = 999
    manifest_path.write_text(json.dumps(manifest))
    saved_audio, saved_manifest = target.read_bytes(), manifest_path.read_bytes()
    user_export = target.with_name("my-practice-export.wav")
    user_export.write_bytes(b"keep this user export")
    run = module.run_ffmpeg

    def change_source(args):
        run(args)
        samples, rate = sf.read(source)
        sf.write(source, samples * 0.5, rate, subtype="FLOAT")

    monkeypatch.setattr(module, "run_ffmpeg", change_source)
    with pytest.raises(ValueError, match="원본 음원이 변경"):
        prepare_pitch(source, paths, 2)
    assert target.read_bytes() == saved_audio
    assert manifest_path.read_bytes() == saved_manifest
    assert user_export.read_bytes() == b"keep this user export"
    assert not list(target.parent.glob(".*.wav"))


def test_source_change_while_validating_a_hit_is_detected(source_audio, monkeypatch):
    import practice_player.transpose as module

    source, paths = source_audio
    target = prepare_pitch(source, paths, 2)
    inspect = module.inspect_audio

    def change_source(path, **options):
        result = inspect(path, **options)
        if Path(path) == target and options.get("scan"):
            samples, rate = sf.read(source)
            sf.write(source, samples * 0.5, rate, subtype="FLOAT")
        return result

    monkeypatch.setattr(module, "inspect_audio", change_source)
    with pytest.raises(ValueError, match="원본 음원이 변경"):
        prepare_pitch(source, paths, 2)


@pytest.mark.parametrize(
    "member",
    [
        "processed",
        "pitch",
        "digest",
        "version",
        "instrumental.wav",
        "pitch.json",
        ".lock",
    ],
)
def test_managed_pitch_symlinks_do_not_write_outside_the_cache(source_audio, tmp_path, member):
    source, paths = source_audio
    digest = hash_file(source)
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected.wav"
    protected.write_bytes(b"external user data")
    directories = {
        "processed": paths.processed,
        "pitch": paths.processed / "pitch",
        "digest": paths.processed / "pitch" / digest,
        "version": paths.processed / "pitch" / digest / "v1-+2",
    }
    if member in directories:
        link = directories[member]
        if link.exists():
            link.rmdir()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside, target_is_directory=True)
    else:
        folder = directories["version"]
        folder.mkdir(parents=True)
        (folder / member).symlink_to(protected)
    with pytest.raises(ValueError, match="심볼릭 링크"):
        prepare_pitch(source, paths, 2)
    assert protected.read_bytes() == b"external user data"
    assert list(outside.iterdir()) == [protected]


def test_symlink_installed_during_conversion_is_rejected_before_publication(
    source_audio, tmp_path, monkeypatch
):
    import practice_player.transpose as module

    source, paths = source_audio
    outside = tmp_path / "external.wav"
    outside.write_bytes(b"external data")
    run = module.run_ffmpeg

    def replace_target(args):
        run(args)
        target = Path(args[-1]).with_name("instrumental.wav")
        target.symlink_to(outside)

    monkeypatch.setattr(module, "run_ffmpeg", replace_target)
    with pytest.raises(ValueError, match="심볼릭 링크"):
        prepare_pitch(source, paths, 2)
    assert outside.read_bytes() == b"external data"
