# SPDX-License-Identifier: GPL-3.0-only
"""Multiple encoded listening versions share one original-media score clock."""

import io
import json
import zipfile
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from practice_player.cache import hash_file
from practice_player.magic_bundle import AUDIO_VARIANT_SPECS, export_magic
from practice_player.variants import variant_label
from tests.test_magic_bundle import portable_study


def variant_study(root, kind="pdf", audio_format="mp3", pitch=0):
    request, prepared, paths = portable_study(root, kind)
    source = Path(request["audio"])
    frames, rate = sf.info(source).frames, 44100
    times = np.arange(frames, dtype=np.float64) / rate
    guitar = root / "guitar-stem.wav"
    sound = (0.12 * np.sin(2 * np.pi * 440 * times)).astype(np.float32)
    sf.write(guitar, np.column_stack([sound, sound]), rate, subtype="PCM_16")
    variants = []
    for identifier, label, path in (
        ("no_vocals", "보컬 제거", source), ("guitar", "기타만 듣기", guitar),
    ):
        mode, target = AUDIO_VARIANT_SPECS[identifier]
        variants.append({"id": identifier, "label": label, "mode": mode,
                         "target": target, "path": str(path)})
    request.update(audio_variants=variants, default_audio_variant="no_vocals",
                   audio_format=audio_format, pitch_semitones=pitch)
    return request, prepared, paths


def all_variant_study(root, kind="gp", audio_format="mp3", pitch=0):
    request, prepared, paths = portable_study(root, kind)
    frames = sf.info(request["audio"]).frames
    times = np.arange(frames, dtype=np.float64) / 44100
    options, frequencies = [], {}
    for index, (identifier, (mode, target)) in enumerate(AUDIO_VARIANT_SPECS.items()):
        frequency = 220 + 80 * index
        sound = (0.12 * np.sin(2 * np.pi * frequency * times)).astype(np.float32)
        path = root / f"choice-{identifier}.wav"
        sf.write(path, np.column_stack([sound, sound]), 44100, subtype="PCM_16")
        options.append({"id": identifier, "label": variant_label(identifier), "mode": mode,
                        "target": target, "path": str(path)})
        frequencies[identifier] = frequency
    request.update(audio_variants=options, default_audio_variant="keys",
                   audio_format=audio_format, pitch_semitones=pitch)
    return request, prepared, paths, frequencies


@pytest.mark.parametrize("kind", ["pdf", "gp"])
@pytest.mark.parametrize("audio_format", ["mp3", "wav"])
def test_multiple_versions_keep_one_score_and_exact_sample_grid(tmp_path, kind, audio_format):
    request, _, _ = variant_study(tmp_path, kind, audio_format)
    sources = {Path(request[key]) for key in ("audio", "pdf", "timeline")}
    sources.update(Path(item["path"]) for item in request["audio_variants"])
    before = {path: hash_file(path) for path in sources}
    path = export_magic(request)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert all(item.compress_type == zipfile.ZIP_STORED for item in archive.infolist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == 4
        assert manifest["audio_gain"] == 1
        assert manifest["default_audio_variant"] == "no_vocals"
        assert [item["id"] for item in manifest["audio_variants"]] == ["no_vocals", "guitar"]
        assert manifest["audio"] == manifest["audio_variants"][0]["audio"]
        assert set(manifest["entries"]) == set(archive.namelist()) - {"manifest.json"}
        for variant in manifest["audio_variants"]:
            audio = variant["audio"]
            assert audio["path"] == f"audio/variants/{variant['id']}.{audio_format}"
            assert audio["codec"] == audio_format
            assert audio["frames"] == 16 * 44100
            assert audio["duration"] == 16
            assert audio["source_sha256"] == before[Path(request["audio"])]
            assert audio["pitch_semitones"] == 0
            samples, rate = sf.read(io.BytesIO(archive.read(audio["path"])))
            assert samples.shape == (16 * 44100, 2) and rate == 44100
        timing = json.loads(archive.read("timeline.json"))
        assert len(timing["cues"]) == 8
        if kind == "gp":
            assert len(manifest["parts"]) == 2
            for part in manifest["parts"]:
                other = json.loads(archive.read(part["timeline"]))
                assert [(cue["start"], cue["source_index"], cue["visit"])
                        for cue in other["cues"]] == [
                    (cue["start"], cue["source_index"], cue["visit"]) for cue in timing["cues"]
                ]
        else:
            assert "parts" not in manifest
        assert str(tmp_path) not in archive.read("manifest.json").decode()
    assert all(hash_file(path) == digest for path, digest in before.items())


def test_all_selected_versions_bake_key_once_and_choose_default(tmp_path):
    request, _, _ = variant_study(tmp_path, pitch=2)
    request["default_audio_variant"] = "guitar"
    originals = {Path(item["path"]): hash_file(Path(item["path"]))
                 for item in request["audio_variants"]}
    with zipfile.ZipFile(export_magic(request)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["audio"] == manifest["audio_variants"][1]["audio"]
        for item, base in zip(manifest["audio_variants"], (220, 440)):
            descriptor = item["audio"]
            samples, rate = sf.read(io.BytesIO(archive.read(descriptor["path"])))
            clip = samples[rate:3 * rate, 0]
            spectrum = np.abs(np.fft.rfft(clip))
            frequency = np.argmax(spectrum) * rate / len(clip)
            assert frequency == pytest.approx(base * 2 ** (2 / 12), abs=1)
            assert descriptor["pitch_semitones"] == 2
            assert descriptor["frames"] == 16 * 44100
    assert all(hash_file(path) == digest for path, digest in originals.items())


@pytest.mark.parametrize("identifiers", [["guitar"], list(AUDIO_VARIANT_SPECS)])
def test_explicit_single_or_all_options_use_format_four(tmp_path, identifiers):
    request, _, _ = variant_study(tmp_path, audio_format="wav")
    request["audio_variants"] = [
        {"id": identifier, "label": identifier, "mode": AUDIO_VARIANT_SPECS[identifier][0],
         "target": AUDIO_VARIANT_SPECS[identifier][1], "path": request["audio"]}
        for identifier in identifiers
    ]
    request["default_audio_variant"] = identifiers[-1]
    with zipfile.ZipFile(export_magic(request)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == 4
        assert [item["id"] for item in manifest["audio_variants"]] == identifiers
        assert manifest["audio"] == manifest["audio_variants"][-1]["audio"]
        assert len([name for name in archive.namelist() if name.startswith("audio/")]) == (
            len(identifiers)
        )


@pytest.mark.parametrize("audio_format", ["mp3", "wav"])
def test_all_fifteen_versions_roundtrip_with_distinct_keyboard_outputs(tmp_path, audio_format):
    from practice_player.config import Paths
    from practice_player.magic_import import import_magic, load_imported

    request, _, _, frequencies = all_variant_study(tmp_path / "export", audio_format=audio_format)
    archive = export_magic(request)
    paths = Paths(tmp_path / "import")
    paths.create()
    result = import_magic(archive, paths)
    bundle = load_imported(result["bundle_path"])
    assert len(bundle["audio_variants"]) == 15
    assert bundle["default_audio_variant"] == "keys"
    assert len(bundle["parts"]) == 2
    for variant in bundle["audio_variants"]:
        assert (variant["mode"], variant["target"]) == AUDIO_VARIANT_SPECS[variant["id"]]
        samples, rate = sf.read(variant["path"])
        clip = samples[rate:3 * rate, 0]
        observed = np.argmax(np.abs(np.fft.rfft(clip))) * rate / len(clip)
        assert observed == pytest.approx(frequencies[variant["id"]], abs=1)
        assert samples.shape == (16 * 44100, 2)
    assert import_magic(archive, paths) == {**result, "cached": True}


@pytest.mark.parametrize("pitch", [0, 2])
def test_float_peaks_use_one_gain_after_pitch_without_clipping(tmp_path, pitch):
    from practice_player.magic_bundle import PCM16_CEILING
    from practice_player.transpose import prepare_pitch

    request, _, paths = variant_study(tmp_path, audio_format="wav", pitch=pitch)
    times = np.arange(16 * 44100, dtype=np.float64) / 44100
    before = {}
    for item, amplitude, frequency in zip(request["audio_variants"], (2.0, .5), (220, 440)):
        path = tmp_path / f"float-{item['id']}.wav"
        wave = (amplitude * np.sin(2 * np.pi * frequency * times)).astype(np.float32)
        sf.write(path, np.column_stack([wave, wave]), 44100, subtype="FLOAT")
        item["path"] = str(path)
        before[path] = hash_file(path)
    messages = []
    exported = export_magic(request, lambda message, _: messages.append(message))
    reference = [sf.read(prepare_pitch(Path(item["path"]), paths, pitch))[0]
                 for item in request["audio_variants"]]
    peak = max(np.max(np.abs(samples)) for samples in reference)
    with zipfile.ZipFile(exported) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        gain = manifest["audio_gain"]
        assert 0 < gain < 1
        assert gain == pytest.approx(PCM16_CEILING / peak, abs=1e-10)
        for variant, samples in zip(manifest["audio_variants"], reference):
            actual, rate = sf.read(io.BytesIO(archive.read(variant["audio"]["path"])))
            assert actual.shape == samples.shape and rate == 44100
            assert np.max(np.abs(actual)) <= PCM16_CEILING
            # A shared scalar must preserve the quiet stem, not normalize it to full scale.
            assert np.max(np.abs(actual - samples * gain)) <= 1 / 32768
    assert any("전체 음량" in message for message in messages)
    assert all(hash_file(path) == digest for path, digest in before.items())


@pytest.mark.parametrize("case", [
    "empty", "object", "unknown", "duplicate", "missing-default", "wrong-mode",
    "wrong-target", "blank-label", "long-label", "missing-target", "too-many",
])
def test_invalid_variant_selection_fails_before_score_work(tmp_path, case):
    source = tmp_path / "sentinel.wav"
    source.write_bytes(b"not opened")
    item = {"id": "guitar", "label": "기타만 듣기", "mode": "stem",
            "target": "guitar", "path": str(source)}
    request = {"audio_variants": [item], "default_audio_variant": "guitar"}
    if case == "empty":
        request["audio_variants"] = []
    elif case == "object":
        request["audio_variants"] = {}
    elif case == "unknown":
        item["id"] = "../../escape"
    elif case == "duplicate":
        request["audio_variants"].append(deepcopy(item))
    elif case == "missing-default":
        request["default_audio_variant"] = "no_vocals"
    elif case == "wrong-mode":
        item["mode"] = "without"
    elif case == "wrong-target":
        item["target"] = "vocals"
    elif case == "blank-label":
        item["label"] = " "
    elif case == "long-label":
        item["label"] = "x" * 101
    elif case == "missing-target":
        item["id"], item["mode"] = "original", "original"
        del item["target"]
    else:
        request["audio_variants"] = [deepcopy(item) for _ in range(len(AUDIO_VARIANT_SPECS) + 1)]
    with pytest.raises(ValueError):
        export_magic(request)
    assert source.read_bytes() == b"not opened"


def test_variant_length_mismatch_preserves_existing_export(tmp_path):
    request, _, _ = variant_study(tmp_path)
    path = Path(request["audio_variants"][1]["path"])
    samples, rate = sf.read(path)
    sf.write(path, samples[:-1], rate, subtype="PCM_16")
    output = Path(request["output"])
    output.write_bytes(b"existing export")
    with pytest.raises(ValueError, match="샘플 수"):
        export_magic({**request, "overwrite": True})
    assert output.read_bytes() == b"existing export"
    assert not list(tmp_path.glob(".magic-*"))


def test_variant_source_change_during_export_never_publishes(tmp_path, monkeypatch):
    import practice_player.magic_bundle as bundle

    request, _, _ = variant_study(tmp_path, audio_format="wav")
    output = Path(request["output"])
    output.write_bytes(b"existing export")
    source = Path(request["audio_variants"][1]["path"])
    encode = bundle._encode_audio

    def changing_source(path, *args):
        result = encode(path, *args)
        if path == source:
            with source.open("ab") as stream:
                stream.write(b"changed")
        return result

    monkeypatch.setattr(bundle, "_encode_audio", changing_source)
    with pytest.raises(ValueError, match="원본이 변경"):
        export_magic({**request, "overwrite": True})
    assert output.read_bytes() == b"existing export"


@pytest.mark.parametrize("limit", ["MAX_AUDIO_BYTES", "MAX_BUNDLE_BYTES"])
def test_audio_and_archive_limits_apply_to_multiple_versions(tmp_path, monkeypatch, limit):
    import practice_player.magic_bundle as bundle

    request, _, _ = variant_study(tmp_path)
    monkeypatch.setattr(bundle, limit, 1024)
    with pytest.raises(ValueError, match="최대"):
        export_magic(request)
    assert not Path(request["output"]).exists()
    assert not list(tmp_path.glob(".magic-*"))
