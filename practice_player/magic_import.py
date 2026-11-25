# SPDX-License-Identifier: GPL-3.0-only
"""Worker-only portable practice import; included pages never enter OMR or GP parsing."""

import hashlib
import json
import math
import mmap
import os
import re
import shutil
import stat
import struct
import tempfile
import uuid
import zlib
from pathlib import Path

from filelock import FileLock

from .cache_locations import ensure_cache_directory
from .config import Paths
from .variants import (
    ALL_VARIANTS,
    MAX_PORTABLE_VARIANTS,
    is_combination,
    validate_variant_identity,
)

IMPORT_VERSION = 1
MAX_ARCHIVE = 1024 * 1024 * 1024
MAX_AUDIO = 512 * 1024 * 1024
MAX_MANIFEST = 1_000_000
MAX_TIMING = 8_000_000
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_MEMBER = re.compile(
    r"(?:manifest\.json|timeline\.json|audio/instrumental\.(?:wav|mp3)|"
    r"audio/variants/(?:" + "|".join(ALL_VARIANTS) + r")\.(?:wav|mp3)|"
    r"score/source\.(?:pdf|gp3|gp4|gp5)|pages/[0-9]{4}\.png|"
    r"parts/[0-9]{2}/(?:timeline\.json|pages/[0-9]{4}\.png))\Z"
)
_CACHE_MEMBER = re.compile(r"(?:bundle\.json|native/track-[0-9]{2}\.json|assets/.+)\Z")
_VARIANTS = {
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


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _digest(path, limit=None):
    value, count = hashlib.sha256(), 0
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            count += len(block)
            _require(
                limit is None or count <= limit,
                "연습 파일이 읽는 도중 커졌거나 크기 한도를 넘었습니다.",
            )
            value.update(block)
    return value.hexdigest()


def _json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "연습 파일의 JSON 항목이 중복되었습니다.")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("연습 파일에 유한하지 않은 수가 있습니다.")

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as error:
        raise ValueError("연습 파일의 JSON 정보가 손상되었습니다.") from error
    _require(isinstance(value, dict), "연습 파일의 JSON 정보가 올바르지 않습니다.")
    return value


def _read_exact(stream, count):
    result = stream.read(count)
    _require(len(result) == count, "연습 파일을 끝까지 읽지 못했습니다.")
    return result


def _read_zip(path):
    """Read stored ZIP headers without extractall, decompression or trusted paths."""
    length = path.stat().st_size
    _require(22 <= length <= MAX_ARCHIVE, "연습 파일은 최대 1 GB까지 열 수 있습니다.")
    entries, spans = {}, []
    with path.open("rb") as stream:
        tail_start = max(0, length - 65557)
        stream.seek(tail_start)
        tail = stream.read()
        end = -1
        for at in range(len(tail) - 22, -1, -1):
            if tail[at : at + 4] == b"PK\x05\x06" and at + 22 + struct.unpack_from(
                "<H", tail, at + 20
            )[0] == len(tail):
                end = at
                break
        _require(end >= 0, "올바른 .magic 파일이 아닙니다.")
        disk, central_disk, disk_count, count, size, start = struct.unpack_from(
            "<HHHHII", tail, end + 4
        )
        _require(
            not disk
            and not central_disk
            and disk_count == count
            and 5 <= count <= 1200
            and size <= 300000
            and start + size == tail_start + end,
            "분할·ZIP64 또는 손상된 연습 파일은 지원하지 않습니다.",
        )
        stream.seek(start)
        central = _read_exact(stream, size)
        cursor = 0
        for _ in range(count):
            _require(
                cursor + 46 <= size and central[cursor : cursor + 4] == b"PK\x01\x02",
                "연습 파일 목록이 손상되었습니다.",
            )
            needed, flags, method = struct.unpack_from("<HHH", central, cursor + 6)
            crc, packed, byte_count = struct.unpack_from("<III", central, cursor + 16)
            names, extra, comment, entry_disk = struct.unpack_from("<HHHH", central, cursor + 28)
            attrs, offset = struct.unpack_from("<II", central, cursor + 38)
            kind = stat.S_IFMT(attrs >> 16)
            _require(
                needed <= 20
                and flags & ~0x800 == 0
                and method == 0
                and packed == byte_count
                and byte_count <= MAX_AUDIO
                and not extra
                and not entry_disk
                and kind in (0, stat.S_IFREG)
                and not attrs & 0x10
                and offset + 30 < start
                and cursor + 46 + names + comment <= size,
                "압축·암호화·링크·확장 ZIP 파일은 지원하지 않습니다.",
            )
            raw_name = central[cursor + 46 : cursor + 46 + names]
            try:
                name = raw_name.decode("utf-8")
            except UnicodeError as error:
                raise ValueError("연습 파일 이름이 올바르지 않습니다.") from error
            _require(
                _MEMBER.fullmatch(name) and name not in entries,
                "중복되거나 허용되지 않은 연습 파일 이름입니다.",
            )
            stream.seek(offset)
            local = _read_exact(stream, 30)
            local_needed, local_flags, local_method = struct.unpack_from("<HHH", local, 4)
            local_crc, local_packed, local_size = struct.unpack_from("<III", local, 14)
            local_names, local_extra = struct.unpack_from("<HH", local, 26)
            _require(
                local[:4] == b"PK\x03\x04"
                and local_needed == needed
                and local_flags == flags
                and local_method == method
                and local_crc == crc
                and local_packed == byte_count
                and local_size == byte_count
                and local_names == names
                and not local_extra,
                "연습 파일의 로컬 정보가 일치하지 않습니다.",
            )
            _require(_read_exact(stream, names) == raw_name, "연습 파일 이름이 일치하지 않습니다.")
            begin = offset + 30 + names
            _require(begin + byte_count <= start, "연습 파일 위치가 올바르지 않습니다.")
            entries[name] = {"from": begin, "bytes": byte_count, "crc": crc}
            spans.append((offset, begin + byte_count))
            cursor += 46 + names + comment
        spans.sort()
        _require(
            cursor == size and all(right[0] >= left[1] for left, right in zip(spans, spans[1:])),
            "연습 파일 영역이 겹치거나 손상되었습니다.",
        )
    return entries


def _copy_entry(stream, entry, output=None, *, limit=MAX_AUDIO, expected=None):
    _require(entry["bytes"] <= limit, "연습 파일의 구성 요소가 너무 큽니다.")
    stream.seek(entry["from"])
    remaining, crc, digest = entry["bytes"], 0, hashlib.sha256()
    collected = bytearray() if output is None else None
    while remaining:
        block = _read_exact(stream, min(1024 * 1024, remaining))
        remaining -= len(block)
        crc = zlib.crc32(block, crc)
        digest.update(block)
        if output is None:
            collected.extend(block)
        else:
            output.write(block)
    _require(crc & 0xFFFFFFFF == entry["crc"], "연습 파일의 CRC 검증에 실패했습니다.")
    actual = digest.hexdigest()
    _require(expected is None or actual == expected, "연습 파일의 SHA-256 검증에 실패했습니다.")
    return bytes(collected) if collected is not None else actual


def _audio_descriptor(audio, version, identifier=None):
    _require(
        isinstance(audio, dict)
        and _number(audio.get("duration"), 0, 3600)
        and audio["duration"] > 0
        and audio.get("channels") == 2
        and type(audio.get("channels")) is int
        and audio.get("sample_rate") == 44100
        and type(audio.get("sample_rate")) is int
        and _integer(audio.get("pitch_semitones"), -12, 12),
        "반주 정보가 올바르지 않습니다.",
    )
    if version >= 3:
        _require(
            _integer(audio.get("frames"), 1, MAX_AUDIO // 4)
            and audio["duration"] == audio["frames"] / 44100
            and isinstance(audio.get("source_sha256"), str)
            and _SHA.fullmatch(audio["source_sha256"]),
            "반주의 원곡 식별 정보·샘플 수가 올바르지 않습니다.",
        )
    else:
        if "frames" in audio:
            _require(
                _integer(audio["frames"], 1, MAX_AUDIO // 4), "반주 샘플 수가 올바르지 않습니다."
            )
        if "source_sha256" in audio:
            _require(
                isinstance(audio["source_sha256"], str) and _SHA.fullmatch(audio["source_sha256"]),
                "원곡 식별 정보가 올바르지 않습니다.",
            )
    if version >= 4:
        codec = audio.get("codec")
        _require(
            codec in ("wav", "mp3") and audio.get("path") == f"audio/variants/{identifier}.{codec}",
            "재생 음원 경로·형식이 올바르지 않습니다.",
        )
        fields = {
            "path",
            "codec",
            "frames",
            "sample_rate",
            "channels",
            "duration",
            "pitch_semitones",
            "source_sha256",
        }
        if codec == "mp3":
            fields.add("bitrate_kbps")
        _require(set(audio) == fields, "재생 음원 정보가 올바르지 않습니다.")
    elif version == 3:
        _require(
            audio.get("path") == "audio/instrumental.mp3" and audio.get("codec") == "mp3",
            "MP3 반주 형식이 올바르지 않습니다.",
        )
    else:
        _require(
            audio.get("path") == "audio/instrumental.wav" and "codec" not in audio,
            "기존 연습 파일에는 WAV 반주가 필요합니다.",
        )
    if audio.get("codec") == "mp3":
        _require(
            type(audio.get("bitrate_kbps")) is int
            and audio["bitrate_kbps"] in (128, 192, 256, 320),
            "MP3 음질 정보가 올바르지 않습니다.",
        )


def _validate_manifest(manifest, entries):
    version = manifest.get("version")
    _require(
        manifest.get("format") == "Magic-Instrumental"
        and type(version) is int
        and version in (1, 2, 3, 4, 5),
        "이 .magic 버전은 지원하지 않습니다. 앱을 업데이트해 주세요.",
    )
    _require(
        isinstance(manifest.get("title"), str)
        and len(manifest["title"]) <= 200
        and manifest.get("timeline") == "timeline.json",
        "곡·악보 정보가 올바르지 않습니다.",
    )
    audio, score = manifest.get("audio"), manifest.get("score")
    _audio_descriptor(audio, version, manifest.get("default_audio_variant"))
    if version >= 4:
        _require(
            _number(manifest.get("audio_gain"), 0, 1) and manifest["audio_gain"] > 0,
            "저장된 음원들의 공통 볼륨 정보가 올바르지 않습니다.",
        )
        variants = manifest.get("audio_variants")
        _require(
            isinstance(variants, list)
            and 1 <= len(variants) <= (MAX_PORTABLE_VARIANTS if version == 5 else len(_VARIANTS)),
            "재생 음원 목록이 올바르지 않습니다.",
        )
        seen = set()
        for variant in variants:
            _require(isinstance(variant, dict), "재생 음원 정보가 올바르지 않습니다.")
            identifier = variant.get("id")
            validate_variant_identity(variant, allow_combinations=version == 5)
            _require(
                identifier not in seen, "재생 음원 선택이 중복되었습니다.",
            )
            seen.add(identifier)
            _audio_descriptor(variant.get("audio"), version, identifier)
            _require(
                all(
                    variant["audio"][key] == audio[key]
                    for key in (
                        "frames",
                        "sample_rate",
                        "channels",
                        "duration",
                        "pitch_semitones",
                        "source_sha256",
                    )
                ),
                "재생 음원들의 원곡·길이·키가 일치하지 않습니다.",
            )
        default = manifest.get("default_audio_variant")
        _require(version != 5 or any(is_combination(item) for item in seen),
                 "조합 연습 파일에 조합된 음원이 없습니다.")
        selected = next((item for item in variants if item["id"] == default), None)
        _require(
            selected is not None and selected["audio"] == audio,
            "처음 재생할 음원이 목록과 일치하지 않습니다.",
        )
    else:
        _require(
            "audio_variants" not in manifest and "default_audio_variant" not in manifest,
            "여러 음원에는 새 .magic 형식이 필요합니다.",
        )
        variants = [
            {
                "id": "no_vocals",
                "label": "보컬 제거",
                "mode": "without",
                "target": "vocals",
                "audio": audio,
            }
        ]
        default = "no_vocals"
    _require(
        isinstance(score, dict)
        and score.get("kind") in ("pdf", "gp")
        and isinstance(score.get("part"), str)
        and len(score["part"]) <= 500,
        "악보 종류가 올바르지 않습니다.",
    )
    _require(
        score.get("path") == "score/source.pdf"
        if score["kind"] == "pdf"
        else isinstance(score.get("path"), str)
        and re.fullmatch(r"score/source\.gp[345]", score["path"]),
        "악보 원본 경로가 올바르지 않습니다.",
    )
    track = score.get("track") if score.get("track") is not None else 1
    _require(_integer(track, 1, 64), "기준 파트 번호가 올바르지 않습니다.")
    expected = {
        "manifest.json",
        "timeline.json",
        score["path"],
        *(item["audio"]["path"] for item in variants),
    }

    def pages(values, prefix):
        _require(
            isinstance(values, list) and 1 <= len(values) <= 200,
            "파트 페이지 수가 올바르지 않습니다.",
        )
        for index, page in enumerate(values, 1):
            _require(
                isinstance(page, dict)
                and page.get("path") == f"{prefix}/{index:04d}.png"
                and _integer(page.get("width"), 1, 2201)
                and _integer(page.get("height"), 1, 2201),
                "악보 화면 정보가 올바르지 않습니다.",
            )
            expected.add(page["path"])

    pages(manifest.get("pages"), "pages")
    parts = [
        {
            "track": track,
            "name": score["part"] or "악보",
            "percussion": False,
            "pages": manifest["pages"],
            "timeline": "timeline.json",
        }
    ]
    if version == 2 or (version in (3, 4, 5) and score["kind"] == "gp"):
        parts = manifest.get("parts")
        basis = manifest.get("timing_basis")
        _require(
            score["kind"] == "gp"
            and isinstance(parts, list)
            and 1 <= len(parts) <= 64
            and isinstance(basis, dict)
            and basis.get("shared") is True
            and basis.get("track") == track,
            "전체 파트 정보가 올바르지 않습니다.",
        )
        seen, total = set(), 0
        for part in parts:
            _require(
                isinstance(part, dict)
                and _integer(part.get("track"), 1, 64)
                and part["track"] not in seen
                and isinstance(part.get("name"), str)
                and len(part["name"]) <= 500
                and type(part.get("percussion")) is bool,
                "파트 이름이나 번호가 올바르지 않습니다.",
            )
            seen.add(part["track"])
            selected = part["track"] == track
            prefix = f"parts/{part['track']:02d}"
            _require(
                part.get("timeline")
                == ("timeline.json" if selected else f"{prefix}/timeline.json"),
                "파트 시간표 경로가 올바르지 않습니다.",
            )
            if selected:
                _require(part.get("pages") == manifest["pages"], "기준 파트의 페이지가 다릅니다.")
            pages(part.get("pages"), "pages" if selected else f"{prefix}/pages")
            expected.add(part["timeline"])
            total += len(part["pages"])
        _require(track in seen and total <= 1000, "기준 파트가 없거나 전체 페이지가 너무 많습니다.")
    elif version in (3, 4, 5):
        _require(
            "parts" not in manifest and "timing_basis" not in manifest,
            "PDF 파트 정보가 올바르지 않습니다.",
        )
    _require(
        set(entries) == expected
        and isinstance(manifest.get("entries"), dict)
        and set(manifest["entries"]) == expected - {"manifest.json"},
        "연습 파일 구성 요소·검증 목록이 일치하지 않습니다.",
    )
    for name, record in manifest["entries"].items():
        _require(
            isinstance(record, dict)
            and type(record.get("bytes")) is int
            and record["bytes"] == entries[name]["bytes"]
            and isinstance(record.get("sha256"), str)
            and _SHA.fullmatch(record["sha256"]),
            "연습 파일 검증 정보가 올바르지 않습니다.",
        )
    return variants, default, parts, track


def _validate_timing(timing, page_count, reference=None):
    _require(
        timing.get("camera_mode") in (None, "system")
        and isinstance(timing.get("cues"), list)
        and 1 <= len(timing["cues"]) <= 20000
        and _number(timing.get("offset"), -3600, 3600),
        "마디 시간표가 올바르지 않습니다.",
    )
    previous = -1
    for cue in timing["cues"]:
        _require(
            isinstance(cue, dict)
            and _number(cue.get("start"), 0, 86400)
            and cue["start"] < 86400
            and cue["start"] > previous
            and _integer(cue.get("page"), 0, page_count - 1)
            and type(cue.get("measure")) in (str, int)
            and len(str(cue["measure"])) <= 32
            and _integer(cue.get("visit"), 1, 2**53 - 1)
            and _integer(cue.get("source_index"), 0, 2**53 - 1),
            "마디 순서나 페이지가 올바르지 않습니다.",
        )
        box = cue.get("box")
        _require(
            isinstance(box, dict)
            and all(
                _number(box.get(key), 0, 1)
                for key in ("left", "right", "top", "bottom", "next_top")
            )
            and box["left"] < box["right"]
            and box["top"] < box["bottom"],
            "마디 위치가 올바르지 않습니다.",
        )
        previous = cue["start"]
    _require(
        _number(timing.get("end"), 0, 86400) and timing["end"] > previous,
        "악보 끝 시간이 올바르지 않습니다.",
    )
    warnings = timing.get("warnings", [])
    _require(
        isinstance(warnings, list)
        and len(warnings) <= 20000
        and all(isinstance(value, str) and len(value) <= 10000 for value in warnings),
        "악보 안내 정보가 올바르지 않습니다.",
    )
    _require(
        "reviewed" not in timing or type(timing["reviewed"]) is bool,
        "악보 검토 정보가 올바르지 않습니다.",
    )
    if reference is not None:
        _require(
            timing["offset"] == reference["offset"]
            and timing["end"] == reference["end"]
            and len(timing["cues"]) == len(reference["cues"])
            and all(
                all(
                    cue[key] == original[key]
                    for key in ("start", "source_index", "measure", "visit")
                )
                for cue, original in zip(timing["cues"], reference["cues"])
            ),
            "파트 간 연주 시간·마디 순서가 다릅니다.",
        )


def _validate_mp3(data, audio):
    error = (
        "MP3 반주 정보가 손상되었거나 지원하지 않는 형식입니다. 데스크톱에서 다시 저장해 주세요."
    )
    rates = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
    at, count, declared = 0, 0, 0
    while at < len(data):
        _require(at + 4 <= len(data), error)
        header = struct.unpack_from(">I", data, at)[0]
        bitrate = rates[(header >> 12) & 15]
        _require(
            header >> 21 == 0x7FF
            and (header >> 19) & 3 == 3
            and (header >> 17) & 3 == 1
            and (header >> 10) & 3 == 0
            and (header >> 6) & 3 != 3
            and bitrate == audio["bitrate_kbps"],
            error,
        )
        size = 144000 * bitrate // 44100 + ((header >> 9) & 1)
        _require(at + size <= len(data), error)
        if not count:
            tag = 4 + (0 if header & 0x10000 else 2) + 32
            _require(tag + 16 <= size and data[tag : tag + 4] in (b"Info", b"Xing"), error)
            flags, declared, byte_count = struct.unpack_from(">III", data, tag + 4)
            _require(flags & 3 == 3 and flags & ~15 == 0 and byte_count == len(data), error)
            lame = tag + 16 + (100 if flags & 4 else 0) + (4 if flags & 8 else 0)
            _require(
                lame + 36 <= size and data[lame : lame + 4] in (b"LAME", b"Lavc", b"Lavf"), error
            )
            padding = int.from_bytes(data[lame + 21 : lame + 24], "big")
            samples = declared * 1152 - (padding >> 12) - (padding & 4095)
            _require(
                declared > 0 and samples == audio["frames"] and samples * 4 <= MAX_AUDIO, error
            )
        at += size
        count += 1
        _require(count <= declared + 1 and (count - 1) * 1152 <= MAX_AUDIO // 4 + 8190, error)
    _require(count == declared + 1, error)


def _validate_wav(data, audio, *, strict):
    error = "WAV 음원 길이·형식이 올바르지 않습니다. 데스크톱에서 다시 저장해 주세요."
    _require(
        len(data) >= 44
        and data[:4] == b"RIFF"
        and data[8:12] == b"WAVE"
        and struct.unpack_from("<I", data, 4)[0] == len(data) - 8,
        error,
    )
    at, chunks, fmt, payload = 12, 0, None, None
    while at < len(data):
        chunks += 1
        _require(at + 8 <= len(data) and chunks <= 1024, error)
        kind, size = data[at : at + 4], struct.unpack_from("<I", data, at + 4)[0]
        begin = at + 8
        _require(begin + size <= len(data), error)
        if kind == b"fmt ":
            _require(fmt is None and size >= 16, error)
            fmt = struct.unpack_from("<HHIIHH", data, begin)
            encoding, channels, rate, byte_rate, align, depth = fmt
            _require(
                channels == 2
                and rate == 44100
                and depth in (8, 16, 24, 32, 64)
                and align == channels * depth // 8
                and byte_rate == rate * align
                and encoding in (1, 3)
                and (encoding != 3 or depth in (32, 64)),
                error,
            )
            if strict:
                _require(encoding == 1 and depth == 16, error)
        elif kind == b"data":
            _require(payload is None and size > 0, error)
            payload = size
        at = begin + size + size % 2
    _require(
        at == len(data) and fmt is not None and payload is not None and payload % fmt[4] == 0, error
    )
    frames = payload // fmt[4]
    _require(
        frames * 4 <= MAX_AUDIO and ("frames" not in audio or frames == audio["frames"]), error
    )


def _inspect_audio(path, audio, version):
    _require(path.stat().st_size > 0, "재생 음원이 비어 있습니다.")
    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if audio.get("codec") == "mp3":
            _validate_mp3(data, audio)
        else:
            _validate_wav(data, audio, strict=version >= 4)
    from .audio import inspect_audio

    info = inspect_audio(path, scan=True)
    _require(
        info["sample_rate"] == 44100 and info["channels"] == 2 and info["frames"] * 4 <= MAX_AUDIO,
        "재생 음원의 샘플 수·채널 정보가 올바르지 않습니다.",
    )
    if "frames" in audio:
        _require(info["frames"] == audio["frames"], "재생 음원의 원본 샘플 수가 일치하지 않습니다.")
    _require(
        info["duration"] == audio["duration"]
        if version >= 3
        else abs(info["duration"] - audio["duration"]) < 0.05,
        "재생 음원 길이가 일치하지 않습니다.",
    )
    return info


def _verify_png(path, page):
    from PIL import Image

    try:
        with path.open("rb") as stream:
            header = stream.read(24)
        _require(
            len(header) == 24
            and header[:8] == b"\x89PNG\r\n\x1a\n"
            and header[12:16] == b"IHDR"
            and struct.unpack_from(">II", header, 16) == (page["width"], page["height"]),
            "악보 PNG 화면 정보가 일치하지 않습니다.",
        )
        with Image.open(path) as picture:
            _require(
                picture.format == "PNG"
                and picture.size == (page["width"], page["height"])
                and getattr(picture, "n_frames", 1) == 1,
                "악보 PNG 화면 정보가 일치하지 않습니다.",
            )
            picture.verify()
        with Image.open(path) as picture:
            picture.load()
    except (OSError, SyntaxError, struct.error, zlib.error, Image.DecompressionBombError) as error:
        raise ValueError("악보 PNG 화면이 손상되었습니다.") from error


def _practice(value):
    _require(value is None or isinstance(value, dict), "메트로놈 설정이 올바르지 않습니다.")
    defaults = {"bpm": 120, "beats": 4, "unit": 4, "origin": 0}
    result = {key: (value or {}).get(key, default) for key, default in defaults.items()}
    _require(
        _number(result["bpm"], 20, 400)
        and _integer(result["beats"], 1, 16)
        and type(result["unit"]) is int
        and result["unit"] in (2, 4, 8, 16)
        and _number(result["origin"], -30, 1800),
        "메트로놈 설정이 올바르지 않습니다.",
    )
    return result


def _native_plan(timing, part, archive_hash, inventory_hash, timing_hash):
    from .score import ScoreTimeline

    frames = []
    for cue in timing["cues"]:
        frames.append(
            {
                "page": cue["page"],
                "source_index": cue["source_index"],
                "box": dict(cue["box"]),
                "x0": 0,
                "x1": 0,
                "y0": 0,
                "y1": 0,
                "intro_y": 0,
                "settle": 0,
            }
        )
    plan = {
        "version": 1,
        "audio_sha256": archive_hash,
        "pdf_sha256": inventory_hash,
        "portable_timing_sha256": timing_hash,
        "page_count": len(part["pages"]),
        "cues": timing["cues"],
        "end": timing["end"],
        "offset": timing["offset"],
        "reviewed": timing.get("reviewed", False),
        "warnings": timing.get("warnings", []),
        "notation_source": {"method": "portable-magic", "track_name": part["name"]},
        "signature": {
            "import_version": IMPORT_VERSION,
            "archive_sha256": archive_hash,
            "track": part["track"],
        },
        "scroll": {
            "version": 1,
            "profile": {"zoom": "page", "width": 800, "height": 650},
            "page_sizes": [[page["width"], page["height"]] for page in part["pages"]],
            "frames": frames,
        },
    }
    if timing.get("camera_mode"):
        plan["camera_mode"] = timing["camera_mode"]
    ScoreTimeline(plan)
    return plan


def _safe_directory(base, relative):
    current = base.resolve(strict=True)
    for name in Path(relative).parts:
        _require(name not in ("", ".", ".."), "연습 파일 캐시 경로가 올바르지 않습니다.")
        current = current / name
        if current.exists() or current.is_symlink():
            _require(
                not current.is_symlink() and current.is_dir(),
                "연습 파일 캐시 폴더에 링크나 다른 파일이 있습니다.",
            )
        else:
            current.mkdir()
    return current


def _regular_file(base, name):
    path = base
    for index, piece in enumerate(Path(name).parts):
        _require(piece not in ("", ".", ".."), "연습 파일 캐시 경로가 올바르지 않습니다.")
        path = path / piece
        _require(not path.is_symlink(), "연습 파일 캐시에 링크가 있습니다.")
        _require(
            path.is_file() if index == len(Path(name).parts) - 1 else path.is_dir(),
            "연습 파일 캐시 구성 요소가 없습니다.",
        )
    return path


def load_imported(bundle_path: Path) -> dict:
    """Validate an imported cache in a worker; never call this hash scan in the GUI."""
    bundle_path = Path(bundle_path).absolute()
    folder = bundle_path.parent
    _require(
        bundle_path.name == "bundle.json"
        and folder.name == f"v{IMPORT_VERSION}"
        and _SHA.fullmatch(folder.parent.name)
        and folder.parent.parent.name == "imported-magic",
        "가져온 연습 파일 캐시 경로가 올바르지 않습니다.",
    )
    for parent in (folder, folder.parent, folder.parent.parent, folder.parent.parent.parent):
        _require(
            not parent.is_symlink() and parent.is_dir(),
            "가져온 연습 파일 캐시 폴더가 올바르지 않습니다.",
        )
    inventory_path = _regular_file(folder, "cache-manifest.json")
    _require(
        inventory_path.stat().st_size <= MAX_MANIFEST, "가져온 연습 파일 검증 목록이 너무 큽니다."
    )
    inventory = _json(inventory_path.read_bytes())
    records = inventory.get("files")
    _require(
        inventory.get("version") == IMPORT_VERSION
        and inventory.get("archive_hash") == folder.parent.name
        and isinstance(records, dict)
        and 6 <= len(records) <= 1266
        and "bundle.json" in records,
        "가져온 연습 파일 검증 목록이 올바르지 않습니다.",
    )
    total = 0
    for name, record in records.items():
        _require(
            isinstance(name, str)
            and _CACHE_MEMBER.fullmatch(name)
            and (not name.startswith("assets/") or _MEMBER.fullmatch(name[7:]))
            and isinstance(record, dict)
            and _integer(record.get("bytes"), 0, MAX_AUDIO)
            and isinstance(record.get("sha256"), str)
            and _SHA.fullmatch(record["sha256"]),
            "가져온 연습 파일 검증 정보가 올바르지 않습니다.",
        )
        path = _regular_file(folder, name)
        _require(
            path.stat().st_size == record["bytes"]
            and _digest(path, record["bytes"]) == record["sha256"],
            "가져온 연습 파일 캐시가 변경되었거나 손상되었습니다.",
        )
        total += record["bytes"]
    _require(total <= MAX_ARCHIVE + 129 * MAX_TIMING, "가져온 연습 파일 캐시가 너무 큽니다.")
    actual, directory_count = set(), 0
    for current, directories, files in os.walk(folder, followlinks=False):
        directory_count += len(directories)
        _require(directory_count <= 1200, "가져온 연습 파일 캐시 폴더가 너무 많습니다.")
        for name in (*directories, *files):
            _require(
                not (Path(current) / name).is_symlink(), "가져온 연습 파일 캐시에 링크가 있습니다."
            )
        actual.update((Path(current) / name).relative_to(folder).as_posix() for name in files)
        _require(len(actual) <= 1267, "가져온 연습 파일 캐시 구성 요소가 너무 많습니다.")
    _require(
        actual == set(records) | {"cache-manifest.json"},
        "가져온 연습 파일 캐시 구성이 일치하지 않습니다.",
    )
    _require(bundle_path.stat().st_size <= MAX_TIMING, "가져온 연습 파일 정보가 너무 큽니다.")
    bundle = _json(bundle_path.read_bytes())
    _require(
        bundle.get("format") == "Magic-Instrumental-PC"
        and bundle.get("import_version") == IMPORT_VERSION
        and bundle.get("archive_hash") == folder.parent.name,
        "가져온 연습 파일 정보가 올바르지 않습니다.",
    )
    variants, parts = bundle.get("audio_variants"), bundle.get("parts")
    _require(
        isinstance(variants, list)
        and 1 <= len(variants) <= MAX_PORTABLE_VARIANTS
        and isinstance(parts, list)
        and 1 <= len(parts) <= 64,
        "가져온 연습 파일의 파트·음원 목록이 올바르지 않습니다.",
    )

    def checked_path(value):
        _require(isinstance(value, str), "가져온 연습 파일 경로가 올바르지 않습니다.")
        path = Path(value)
        _require(path.is_absolute(), "가져온 연습 파일 경로가 올바르지 않습니다.")
        try:
            name = path.relative_to(folder).as_posix()
        except ValueError as error:
            raise ValueError("가져온 연습 파일이 캐시 폴더를 벗어납니다.") from error
        _require(name in records, "가져온 연습 파일의 검증 기록이 없습니다.")
        return records[name]

    for variant in variants:
        _require(isinstance(variant, dict), "가져온 재생 음원 정보가 올바르지 않습니다.")
        validate_variant_identity(variant, allow_combinations=bundle.get("archive_version") == 5)
        record = checked_path(variant.get("path"))
        _require(
            variant.get("sha256") == record["sha256"] and variant.get("bytes") == record["bytes"],
            "가져온 재생 음원의 검증 정보가 일치하지 않습니다.",
        )
    for part in parts:
        _require(
            isinstance(part, dict)
            and isinstance(part.get("pages"), list)
            and 1 <= len(part["pages"]) <= 200,
            "가져온 악보 정보가 올바르지 않습니다.",
        )
        checked_path(part.get("timeline"))
        for page in part["pages"]:
            _require(isinstance(page, dict), "가져온 악보 화면 정보가 올바르지 않습니다.")
            checked_path(page.get("path"))
    _require(isinstance(bundle.get("score"), dict), "가져온 악보 정보가 올바르지 않습니다.")
    checked_path(bundle["score"].get("path"))
    return bundle


def _copy_snapshot(source, destination):
    digest, size = hashlib.sha256(), 0
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        for block in iter(lambda: incoming.read(1024 * 1024), b""):
            size += len(block)
            _require(size <= MAX_ARCHIVE, "연습 파일은 최대 1 GB까지 열 수 있습니다.")
            digest.update(block)
            outgoing.write(block)
    return digest.hexdigest()


def _publish(stage, final):
    backup = None
    if final.exists():
        _require(
            not final.is_symlink() and final.is_dir(),
            "가져온 연습 파일 캐시 경로가 올바르지 않습니다.",
        )
        backup = final.with_name(f".previous-{uuid.uuid4().hex}")
        final.rename(backup)
    try:
        stage.rename(final)
    except BaseException:
        if backup is not None:
            backup.rename(final)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def import_magic(source: Path, paths: Paths, report=lambda *_: None) -> dict:
    """Import v1–v5 into an archive-hash namespace, preserving existing imports on failure."""
    paths.require_cache_locations()
    source = Path(source).resolve(strict=True)
    _require(
        source.is_file() and 22 <= source.stat().st_size <= MAX_ARCHIVE,
        "연습 파일은 최대 1 GB까지 열 수 있습니다.",
    )
    report("연습 파일 원본 확인 중…", None)
    archive_hash = _digest(source, MAX_ARCHIVE)
    root = ensure_cache_directory(paths.audio_cache).resolve(strict=True)
    base = _safe_directory(root, f"imported-magic/{archive_hash}")
    final = base / f"v{IMPORT_VERSION}"
    lock = base / ".import.lock"
    _require(not lock.is_symlink(), "연습 파일 잠금 경로가 올바르지 않습니다.")
    with FileLock(str(lock), timeout=3600):
        if final.exists() or final.is_symlink():
            _require(
                not final.is_symlink() and final.is_dir(),
                "연습 파일 캐시 경로가 올바르지 않습니다.",
            )
            try:
                load_imported(final / "bundle.json")
                _require(
                    _digest(source, MAX_ARCHIVE) == archive_hash,
                    "가져오는 중 원본 연습 파일이 변경되었습니다.",
                )
                report("저장된 연습 파일을 불러왔습니다.", 1)
                return {
                    "bundle_path": str(final / "bundle.json"),
                    "archive_hash": archive_hash,
                    "cached": True,
                }
            except (OSError, ValueError, KeyError, TypeError):
                pass
        with tempfile.TemporaryDirectory(prefix=".import-", dir=base) as temporary:
            work = Path(temporary)
            snapshot = work / "source.magic"
            _require(
                _copy_snapshot(source, snapshot) == archive_hash,
                "가져오는 중 원본 연습 파일이 변경되었습니다. 다시 시도해 주세요.",
            )
            entries = _read_zip(snapshot)
            _require("manifest.json" in entries, "연습 파일의 설명 정보가 없습니다.")
            with snapshot.open("rb") as stream:
                manifest = _json(_copy_entry(stream, entries["manifest.json"], limit=MAX_MANIFEST))
            variants, default, parts, track = _validate_manifest(manifest, entries)
            practice = _practice(manifest.get("practice"))
            stage = work / "payload"
            stage.mkdir()
            records = {}
            with snapshot.open("rb") as stream:
                for index, (name, entry) in enumerate(entries.items(), 1):
                    report(
                        f"연습 파일 검증 · {index}/{len(entries)}",
                        index / (len(entries) + len(variants) + len(parts)),
                    )
                    target = stage / "assets" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    expected = manifest["entries"].get(name, {}).get("sha256")
                    with target.open("xb") as output:
                        digest = _copy_entry(stream, entry, output, expected=expected)
                    records[f"assets/{name}"] = {"bytes": entry["bytes"], "sha256": digest}
            portable_audio = []
            for variant in variants:
                audio = variant["audio"]
                report(f"재생 음원 검증 · {variant['label']}", None)
                info = _inspect_audio(stage / "assets" / audio["path"], audio, manifest["version"])
                descriptor = {key: variant[key] for key in ("id", "label", "mode", "target")}
                if "targets" in variant:
                    descriptor["targets"] = list(variant["targets"])
                descriptor.update(info)
                descriptor.update(
                    path=str(final / "assets" / audio["path"]),
                    codec=audio.get("codec", "wav"),
                    pitch_semitones=audio["pitch_semitones"],
                    source_sha256=audio.get("source_sha256"),
                    **manifest["entries"][audio["path"]],
                )
                if audio.get("codec") == "mp3":
                    descriptor["bitrate_kbps"] = audio["bitrate_kbps"]
                portable_audio.append(descriptor)
            reference_part = next(part for part in parts if part["track"] == track)
            reference_path = stage / "assets" / reference_part["timeline"]
            _require(reference_path.stat().st_size <= MAX_TIMING, "마디 시간표가 너무 큽니다.")
            reference = _json(reference_path.read_bytes())
            _validate_timing(reference, len(reference_part["pages"]))
            common = {
                "offset": reference["offset"],
                "end": reference["end"],
                "cues": [
                    {key: cue[key] for key in ("start", "source_index", "measure", "visit")}
                    for cue in reference["cues"]
                ],
            }
            timing_hash = hashlib.sha256(_json_bytes(common)).hexdigest()
            portable_parts = []
            for part in parts:
                report(f"악보·마디 검증 · {part['name']}", None)
                timing_path = stage / "assets" / part["timeline"]
                _require(timing_path.stat().st_size <= MAX_TIMING, "마디 시간표가 너무 큽니다.")
                timing = _json(timing_path.read_bytes())
                _validate_timing(timing, len(part["pages"]), reference)
                inventory, page_records = [], []
                for page in part["pages"]:
                    _verify_png(stage / "assets" / page["path"], page)
                    inventory.append(
                        {
                            "path": page["path"],
                            "width": page["width"],
                            "height": page["height"],
                            "sha256": manifest["entries"][page["path"]]["sha256"],
                        }
                    )
                    page_records.append(
                        {
                            "path": str(final / "assets" / page["path"]),
                            "width": page["width"],
                            "height": page["height"],
                        }
                    )
                inventory_hash = hashlib.sha256(_json_bytes(inventory)).hexdigest()
                name = f"native/track-{part['track']:02d}.json"
                plan = _native_plan(timing, part, archive_hash, inventory_hash, timing_hash)
                (stage / "native").mkdir(exist_ok=True)
                (stage / name).write_bytes(_json_bytes(plan))
                records[name] = {
                    "bytes": (stage / name).stat().st_size,
                    "sha256": _digest(stage / name),
                }
                portable_parts.append(
                    {
                        "track": part["track"],
                        "name": part["name"],
                        "percussion": part["percussion"],
                        "pages": page_records,
                        "timeline": str(final / name),
                        "page_inventory_sha256": inventory_hash,
                    }
                )
            bundle = {
                "format": "Magic-Instrumental-PC",
                "import_version": IMPORT_VERSION,
                "archive_version": manifest["version"],
                "archive_hash": archive_hash,
                "title": manifest["title"],
                "original_source_sha256": manifest["audio"].get("source_sha256"),
                "audio_gain": manifest["audio_gain"] if manifest["version"] >= 4 else 1,
                "default_audio_variant": default,
                "default_part": track,
                "portable_timing_sha256": timing_hash,
                "practice": practice,
                "audio_variants": portable_audio,
                "parts": portable_parts,
                "score": {
                    **manifest["score"],
                    "track": track,
                    "path": str(final / "assets" / manifest["score"]["path"]),
                },
            }
            (stage / "bundle.json").write_bytes(_json_bytes(bundle))
            records["bundle.json"] = {
                "bytes": (stage / "bundle.json").stat().st_size,
                "sha256": _digest(stage / "bundle.json"),
            }
            (stage / "cache-manifest.json").write_bytes(
                _json_bytes(
                    {"version": IMPORT_VERSION, "archive_hash": archive_hash, "files": records}
                )
            )
            _require(
                _digest(source, MAX_ARCHIVE) == archive_hash,
                "가져오는 중 원본 연습 파일이 변경되었습니다.",
            )
            _publish(stage, final)
    report("연습 파일을 불러왔습니다.", 1)
    return {
        "bundle_path": str(final / "bundle.json"),
        "archive_hash": archive_hash,
        "cached": False,
    }
