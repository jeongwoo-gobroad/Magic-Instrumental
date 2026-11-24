# SPDX-License-Identifier: GPL-3.0-only
"""Actual encoded audio plus adversarial portable archives and cache transactions."""

import hashlib
import io
import json
import stat
import struct
import zipfile
import zlib
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from practice_player.audio import run_ffmpeg
from practice_player.config import Paths
from practice_player.magic_import import import_magic, load_imported
from practice_player.score import ScoreTimeline

SPECS = {
    "no_vocals": ("without", "vocals"),
    "vocals": ("stem", "vocals"),
    "no_guitar": ("without", "guitar"),
    "guitar": ("stem", "guitar"),
    "no_drums": ("without", "drums"),
    "drums": ("stem", "drums"),
    "no_bass": ("without", "bass"),
    "bass": ("stem", "bass"),
    "no_piano": ("without", "piano"),
    "piano": ("stem", "piano"),
    "no_keys": ("without", "keys"),
    "keys": ("stem", "keys"),
    "no_synth": ("without", "synth"),
    "synth": ("stem", "synth"),
    "original": ("original", None),
}


def encoded_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def picture(color):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 80), color).save(buffer, format="PNG")
    return buffer.getvalue()


def make_package(root, version=4, *, gp=False, variants=None, floating=False):
    root.mkdir(parents=True, exist_ok=True)
    rate, frames = 44100, 4410 + 137
    times = np.arange(frames) / rate
    selected = variants or ([("no_vocals", "mp3")] if version == 3 else [("no_vocals", "wav")])
    files, options = {}, []
    for index, (identifier, codec) in enumerate(selected):
        tone = (0.2 * np.sin(2 * np.pi * (220 + index * 100) * times)).astype(np.float32)
        wav = root / f"{identifier}.wav"
        sf.write(
            wav,
            np.column_stack((tone, tone * 0.7)),
            rate,
            subtype="FLOAT" if floating else "PCM_16",
        )
        output = wav
        if codec == "mp3":
            output = root / f"{identifier}.mp3"
            run_ffmpeg(
                [
                    "-i",
                    str(wav),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "2",
                    "-ar",
                    "44100",
                    "-map_metadata",
                    "-1",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "256k",
                    "-write_xing",
                    "1",
                    "-id3v2_version",
                    "0",
                    "-write_id3v1",
                    "0",
                    str(output),
                ]
            )
        name = (
            f"audio/variants/{identifier}.{codec}"
            if version == 4
            else f"audio/instrumental.{codec}"
        )
        files[name] = output.read_bytes()
        audio = {
            "path": name,
            "frames": frames,
            "sample_rate": rate,
            "channels": 2,
            "duration": frames / rate,
            "pitch_semitones": 2,
            "source_sha256": "a" * 64,
        }
        if version == 4 or codec == "mp3":
            audio["codec"] = codec
        if codec == "mp3":
            audio["bitrate_kbps"] = 256
        mode, target = SPECS[identifier]
        options.append(
            {
                "id": identifier,
                "label": f"선택 {index + 1}",
                "mode": mode,
                "target": target,
                "audio": audio,
            }
        )
    timing = {
        "offset": 0.01,
        "end": 0.1,
        "reviewed": True,
        "warnings": ["사용자가 검토한 반복 구간"],
        "cues": [
            {
                "start": 0,
                "measure": "1",
                "visit": 1,
                "source_index": 0,
                "page": 0,
                "box": {"left": 0.1, "right": 0.6, "top": 0.2, "bottom": 0.3, "next_top": 0.6},
            },
            {
                "start": 0.05,
                "measure": "1",
                "visit": 2,
                "source_index": 0,
                "page": 0,
                "box": {"left": 0.3, "right": 0.8, "top": 0.6, "bottom": 0.7, "next_top": 0.6},
            },
        ],
    }
    files["timeline.json"] = encoded_json(timing)
    files["pages/0001.png"] = picture("white")
    # Deliberately opaque originals: import must use included PNGs, not parse these.
    source = "score/source.gp5" if gp else "score/source.pdf"
    files[source] = b"opaque original score; do not reanalyze during portable import"
    pages = [{"path": "pages/0001.png", "width": 64, "height": 80}]
    manifest = {
        "format": "Magic-Instrumental",
        "version": version,
        "title": root.name,
        "audio": options[0]["audio"],
        "score": {
            "kind": "gp" if gp else "pdf",
            "path": source,
            "part": "Guitar" if gp else "",
            "track": 2 if gp else None,
        },
        "timeline": "timeline.json",
        "pages": pages,
        "practice": {"bpm": 90, "beats": 3, "unit": 8, "origin": -0.2},
    }
    if gp and version >= 2:
        other_timing = deepcopy(timing)
        other_timing["cues"][0]["box"]["left"] = 0.05
        files["parts/01/timeline.json"] = encoded_json(other_timing)
        files["parts/01/pages/0001.png"] = picture("beige")
        manifest["parts"] = [
            {
                "track": 1,
                "name": "Bass",
                "percussion": False,
                "timeline": "parts/01/timeline.json",
                "pages": [{"path": "parts/01/pages/0001.png", "width": 64, "height": 80}],
            },
            {
                "track": 2,
                "name": "Guitar",
                "percussion": False,
                "timeline": "timeline.json",
                "pages": pages,
            },
        ]
        manifest["timing_basis"] = {"track": 2, "shared": True}
    if version == 4:
        manifest["audio_gain"] = 1
        manifest["audio_variants"] = options
        manifest["default_audio_variant"] = options[0]["id"]
    manifest["entries"] = {
        name: {"bytes": len(data), "sha256": digest(data)} for name, data in files.items()
    }
    files["manifest.json"] = encoded_json(manifest)
    archive = root / "study.magic"
    write_package(archive, files)
    return archive


def write_package(path, files, *, compression=zipfile.ZIP_STORED, info_change=None, zip64=False):
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, data in files.items():
            if info_change is not None:
                info = zipfile.ZipInfo(name)
                info_change(info)
                archive.writestr(info, data)
            elif zip64:
                with archive.open(name, "w", force_zip64=True) as output:
                    output.write(data)
            else:
                archive.writestr(name, data)


def change_package(
    path, manifest_change=None, payload_change=None, *, update_hashes=True, **write_options
):
    with zipfile.ZipFile(path) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files.pop("manifest.json"))
    if payload_change:
        payload_change(files)
    if update_hashes:
        manifest["entries"] = {
            name: {"bytes": len(data), "sha256": digest(data)} for name, data in files.items()
        }
    if manifest_change:
        manifest_change(manifest)
    files["manifest.json"] = encoded_json(manifest)
    write_package(path, files, **write_options)


def workspace(tmp_path):
    paths = Paths(tmp_path / "desktop")
    paths.create()
    return paths


@pytest.mark.parametrize(
    "version,gp", [(1, False), (1, True), (2, True), (3, False), (3, True), (4, False), (4, True)]
)
def test_import_preserves_encoded_audio_pages_route_and_originals(tmp_path, version, gp):
    archive = make_package(tmp_path / "portable", version, gp=gp)
    source_hash = digest(archive.read_bytes())
    paths = workspace(tmp_path)
    unrelated = paths.processed / ("a" * 64) / "existing.wav"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"existing separation cache")
    result = import_magic(archive, paths)
    assert result["archive_hash"] == source_hash and result["cached"] is False
    assert (
        Path(result["bundle_path"]).parent
        == paths.processed / "imported-magic" / source_hash / "v1"
    )
    bundle = load_imported(result["bundle_path"])
    assert bundle["original_source_sha256"] == "a" * 64
    assert bundle["default_audio_variant"] == "no_vocals"
    assert bundle["practice"] == {"bpm": 90, "beats": 3, "unit": 8, "origin": -0.2}
    assert len(bundle["parts"]) == (2 if gp and version >= 2 else 1)
    variant = bundle["audio_variants"][0]
    assert variant["frames"] == 4547 and variant["duration"] == 4547 / 44100
    assert variant["pitch_semitones"] == 2
    with zipfile.ZipFile(archive) as source:
        metadata = json.loads(source.read("manifest.json"))
        assert Path(variant["path"]).read_bytes() == source.read(metadata["audio"]["path"])
    assert variant["sha256"] == digest(Path(variant["path"]).read_bytes())
    assert variant["bytes"] == Path(variant["path"]).stat().st_size
    timing_hashes, page_hashes = set(), set()
    for part in bundle["parts"]:
        plan = ScoreTimeline.load(Path(part["timeline"]))
        assert plan.data["audio_sha256"] == source_hash
        assert plan.data["pdf_sha256"] == part["page_inventory_sha256"]
        assert plan.data["offset"] == 0.01 and plan.end == 0.1
        assert [cue["visit"] for cue in plan.cues] == [1, 2]
        assert plan.data["reviewed"] is True
        assert plan.data["scroll"]["page_sizes"] == [[64, 80]]
        assert plan.data["scroll"]["frames"][0]["box"] == plan.cues[0]["box"]
        assert plan.data["scroll"]["frames"][0]["x0"] == 0
        timing_hashes.add(plan.data["portable_timing_sha256"])
        page_hashes.add(plan.data["pdf_sha256"])
    assert timing_hashes == {bundle["portable_timing_sha256"]}
    assert len(page_hashes) == len(bundle["parts"])
    assert digest(archive.read_bytes()) == source_hash
    assert unrelated.read_bytes() == b"existing separation cache"


def test_all_variants_share_grid_and_cache_hit_skips_parsing(tmp_path, monkeypatch):
    import practice_player.magic_import as module

    variants = [(name, "mp3" if index == 0 else "wav") for index, name in enumerate(SPECS)]
    archive = make_package(tmp_path / "all-options", gp=True, variants=variants)
    paths = workspace(tmp_path)
    first = import_magic(archive, paths)
    bundle = load_imported(first["bundle_path"])
    assert [item["id"] for item in bundle["audio_variants"]] == list(SPECS)
    assert len({item["sha256"] for item in bundle["audio_variants"]}) == len(SPECS)

    def forbidden(*_, **__):
        raise AssertionError("cache hit must not decode audio, render or parse archive/notation")

    for name in ("_read_zip", "_inspect_audio", "_verify_png", "_native_plan"):
        monkeypatch.setattr(module, name, forbidden)
    second = import_magic(archive, paths)
    assert second == {**first, "cached": True}


@pytest.mark.parametrize("kind", ["pdf", "gp"])
def test_current_exporter_roundtrips_into_native_portable_plans(tmp_path, kind):
    from practice_player.magic_bundle import export_magic
    from tests.test_magic_variants import variant_study

    request, _, _ = variant_study(tmp_path / "export", kind=kind, pitch=2)
    archive = export_magic(request)
    result = import_magic(archive, workspace(tmp_path))
    bundle = load_imported(result["bundle_path"])
    assert bundle["archive_version"] == 4 and bundle["score"]["kind"] == kind
    assert len(bundle["audio_variants"]) == len(request["audio_variants"])
    assert all(item["pitch_semitones"] == 2 for item in bundle["audio_variants"])
    assert all(ScoreTimeline.load(Path(part["timeline"])).cues for part in bundle["parts"])


def test_legacy_wav_without_optional_hash_or_frame_metadata(tmp_path):
    archive = make_package(tmp_path / "old", version=1, floating=True)

    def old(manifest):
        manifest["audio"].pop("frames")
        manifest["audio"].pop("source_sha256")

    change_package(archive, old)
    result = import_magic(archive, workspace(tmp_path))
    bundle = load_imported(result["bundle_path"])
    assert bundle["original_source_sha256"] is None
    assert bundle["audio_variants"][0]["frames"] == 4547


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.update(default_audio_variant="missing"),
        lambda m: m.pop("audio_gain"),
        lambda m: m.update(audio_gain=0),
        lambda m: m.update(audio_gain=1.1),
        lambda m: m.update(audio_gain=True),
        lambda m: m["audio_variants"][1].update(id="no_vocals"),
        lambda m: m["audio_variants"][1].update(mode="original"),
        lambda m: m["audio_variants"][1]["audio"].update(source_sha256="b" * 64),
        lambda m: m["audio_variants"][1]["audio"].update(pitch_semitones=-1),
        lambda m: m["audio_variants"][1]["audio"].update(frames=5000, duration=5000 / 44100),
        lambda m: m["audio"].update(extra="ignored metadata must not alter the profile"),
        lambda m: m["audio_variants"][1].update(label=" "),
        lambda m: m["entries"].pop("pages/0001.png"),
        lambda m: m["pages"][0].update(width=2202),
    ],
)
def test_variant_inventory_and_canonical_metadata_reject_mismatches(tmp_path, change):
    archive = make_package(tmp_path / "invalid", variants=[("no_vocals", "wav"), ("guitar", "wav")])
    change_package(archive, change)
    paths = workspace(tmp_path)
    with pytest.raises(ValueError):
        import_magic(archive, paths)
    assert not list(paths.processed.glob("imported-magic/*/v1"))


def test_baked_shared_audio_gain_is_retained_without_a_second_application(tmp_path):
    archive = make_package(tmp_path / "gain", variants=[("no_vocals", "wav"), ("guitar", "wav")])
    change_package(archive, lambda manifest: manifest.update(audio_gain=0.4))
    result = import_magic(archive, workspace(tmp_path))
    bundle = load_imported(result["bundle_path"])
    assert bundle["audio_gain"] == 0.4
    with zipfile.ZipFile(archive) as source:
        for variant in bundle["audio_variants"]:
            assert Path(variant["path"]).read_bytes() == source.read(
                f"audio/variants/{variant['id']}.wav"
            )


@pytest.mark.parametrize(
    "fault", ["traversal", "compressed", "extra", "symlink", "zip64", "duplicate"]
)
def test_unsafe_zip_members_never_escape_the_import_namespace(tmp_path, fault):
    archive = make_package(tmp_path / "unsafe")
    if fault == "traversal":
        with zipfile.ZipFile(archive, "a") as target:
            target.writestr("../outside.txt", b"must not be written")
    elif fault == "compressed":
        change_package(archive, compression=zipfile.ZIP_DEFLATED)
    elif fault == "extra":
        change_package(
            archive, info_change=lambda info: setattr(info, "extra", struct.pack("<HH", 0xCAFE, 0))
        )
    elif fault == "symlink":
        change_package(
            archive,
            info_change=lambda info: setattr(info, "external_attr", (stat.S_IFLNK | 0o777) << 16),
        )
    elif fault == "zip64":
        change_package(archive, zip64=True)
    else:
        with pytest.warns(UserWarning), zipfile.ZipFile(archive, "a") as target:
            target.writestr("pages/0001.png", picture("red"))
    paths = workspace(tmp_path)
    with pytest.raises(ValueError):
        import_magic(archive, paths)
    assert not (tmp_path / "outside.txt").exists()
    assert not list(paths.processed.glob("imported-magic/*/v1"))


def test_valid_local_headers_cannot_overlap_another_members_payload(tmp_path):
    archive = make_package(tmp_path / "overlap")
    with zipfile.ZipFile(archive) as source:
        files = {name: source.read(name) for name in source.namelist()}
    child, parent = "pages/0001.png", "score/source.pdf"

    def local(name, data):
        raw_name = name.encode()
        return (
            struct.pack(
                "<IHHHHHIIIHH",
                0x04034B50,
                20,
                0,
                0,
                0,
                0,
                zlib.crc32(data),
                len(data),
                len(data),
                len(raw_name),
                0,
            )
            + raw_name
            + data
        )

    files[parent] = local(child, files[child])
    manifest = json.loads(files["manifest.json"])
    manifest["entries"][parent] = {"bytes": len(files[parent]), "sha256": digest(files[parent])}
    files["manifest.json"] = encoded_json(manifest)
    body, offsets = bytearray(), {}
    for name, data in files.items():
        if name == child:
            continue
        offsets[name] = len(body)
        if name == parent:
            offsets[child] = len(body) + 30 + len(name)
        body.extend(local(name, data))
    central = bytearray()
    for name, data in files.items():
        raw_name = name.encode()
        central.extend(
            struct.pack(
                "<IHHHHHHIIIHHHHHII",
                0x02014B50,
                20,
                20,
                0,
                0,
                0,
                0,
                zlib.crc32(data),
                len(data),
                len(data),
                len(raw_name),
                0,
                0,
                0,
                0,
                0,
                offsets[name],
            )
        )
        central.extend(raw_name)
    end = struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, len(files), len(files), len(central), len(body), 0
    )
    archive.write_bytes(body + central + end)
    with pytest.raises(ValueError, match="겹치"):
        import_magic(archive, workspace(tmp_path))


@pytest.mark.parametrize(
    "fault", ["crc", "sha", "png_dimensions", "png_bytes", "route", "timing_sha"]
)
def test_corrupt_assets_and_part_routes_cannot_be_published(tmp_path, fault):
    archive = make_package(tmp_path / "corrupt", gp=True)
    if fault == "crc":
        with zipfile.ZipFile(archive) as source:
            entry = source.getinfo("score/source.gp5")
        raw = bytearray(archive.read_bytes())
        begin = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
        raw[begin] ^= 1
        archive.write_bytes(raw)
    elif fault == "sha":
        change_package(
            archive,
            payload_change=lambda files: files.update(
                {"score/source.gp5": b"changed opaque source"}
            ),
            update_hashes=False,
        )
    elif fault == "png_dimensions":

        def wrong_size(manifest):
            manifest["pages"][0]["width"] = 65
            manifest["parts"][1]["pages"][0]["width"] = 65

        change_package(archive, wrong_size)
    elif fault == "png_bytes":
        change_package(
            archive, payload_change=lambda files: files.update({"pages/0001.png": b"not a PNG"})
        )
    else:

        def wrong_route(files):
            timing = json.loads(files["parts/01/timeline.json"])
            timing["cues"][1]["start"] = 0.06
            files["parts/01/timeline.json"] = encoded_json(timing)

        change_package(archive, payload_change=wrong_route, update_hashes=fault == "route")
    paths = workspace(tmp_path)
    with pytest.raises(ValueError):
        import_magic(archive, paths)
    assert not list(paths.processed.glob("imported-magic/*/v1"))


def test_mp3_profile_checked_before_decoder(tmp_path, monkeypatch):
    import practice_player.audio as audio

    archive = make_package(tmp_path / "mp3", version=3)

    def corrupt(files):
        data = bytearray(files["audio/instrumental.mp3"])
        data[0:3] = b"ID3"
        files["audio/instrumental.mp3"] = bytes(data)

    change_package(archive, payload_change=corrupt)
    monkeypatch.setattr(
        audio,
        "inspect_audio",
        lambda *_args, **_kwargs: pytest.fail("decode ran before MP3 validation"),
    )
    with pytest.raises(ValueError, match="MP3"):
        import_magic(archive, workspace(tmp_path))


def test_legacy_nonfinite_audio_rejected_without_poisoning_an_existing_import(tmp_path):
    paths = workspace(tmp_path)
    original = make_package(tmp_path / "good")
    good = import_magic(original, paths)
    corrupt = make_package(tmp_path / "nonfinite", version=1, floating=True)

    def nonfinite(files):
        buffer = io.BytesIO()
        samples = np.zeros((4547, 2), dtype=np.float32)
        samples[100, 0] = np.nan
        sf.write(buffer, samples, 44100, format="WAV", subtype="FLOAT")
        files["audio/instrumental.wav"] = buffer.getvalue()

    change_package(corrupt, payload_change=nonfinite)
    with pytest.raises(ValueError, match="NaN"):
        import_magic(corrupt, paths)
    assert load_imported(good["bundle_path"])["title"] == "good"


def test_tampered_cache_reimports_original_bytes_and_rejects_external_descriptor(tmp_path):
    archive = make_package(tmp_path / "recover")
    paths = workspace(tmp_path)
    first = import_magic(archive, paths)
    bundle = load_imported(first["bundle_path"])
    audio = Path(bundle["audio_variants"][0]["path"])
    expected = audio.read_bytes()
    audio.write_bytes(b"damaged cached output")
    with pytest.raises(ValueError):
        load_imported(first["bundle_path"])
    recovered = import_magic(archive, paths)
    assert recovered["cached"] is False and audio.read_bytes() == expected
    descriptor = Path(first["bundle_path"])
    changed = json.loads(descriptor.read_bytes())
    changed["audio_variants"][0]["path"] = str(tmp_path / "outside.wav")
    descriptor.write_bytes(encoded_json(changed))
    inventory_path = descriptor.with_name("cache-manifest.json")
    inventory = json.loads(inventory_path.read_bytes())
    inventory["files"]["bundle.json"] = {
        "bytes": descriptor.stat().st_size,
        "sha256": digest(descriptor.read_bytes()),
    }
    inventory_path.write_bytes(encoded_json(inventory))
    with pytest.raises(ValueError, match="벗어납니다"):
        load_imported(descriptor)


def test_import_source_change_leaves_existing_import_intact(tmp_path, monkeypatch):
    import practice_player.magic_import as module

    paths = workspace(tmp_path)
    old = import_magic(make_package(tmp_path / "previous"), paths)
    source = make_package(tmp_path / "changing")
    source_hash = digest(source.read_bytes())
    copy = module._copy_snapshot

    def changed_after_copy(original, snapshot):
        value = copy(original, snapshot)
        original.write_bytes(original.read_bytes() + b"changed during import")
        return value

    monkeypatch.setattr(module, "_copy_snapshot", changed_after_copy)
    with pytest.raises(ValueError, match="변경"):
        import_magic(source, paths)
    assert not (paths.processed / "imported-magic" / source_hash / "v1").exists()
    assert load_imported(old["bundle_path"])["title"] == "previous"


def test_import_cache_symlink_is_refused_without_writing_target(tmp_path):
    archive = make_package(tmp_path / "links")
    paths = workspace(tmp_path)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (paths.processed / "imported-magic").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="링크"):
        import_magic(archive, paths)
    assert not list(outside.iterdir())
