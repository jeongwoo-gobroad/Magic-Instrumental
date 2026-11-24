# SPDX-License-Identifier: GPL-3.0-only
"""Independent key changes keep original media time and separation caches intact."""

import json
import math
import re
import uuid
from pathlib import Path

from filelock import FileLock

from .audio import inspect_audio, run_ffmpeg, tempo_filter
from .cache import hash_file
from .score import atomic_json

PITCH_VERSION = 1
_MAX_MANIFEST_BYTES = 64 * 1024


def _pitch_folder(paths, digest, semitones):
    root = Path(paths.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    current = root
    for name in ("processed", "pitch", digest, f"v{PITCH_VERSION}-{semitones:+d}"):
        current = current / name
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise ValueError("키 변경 캐시 폴더의 바로가기·심볼릭 링크는 사용할 수 없습니다.")
        current.mkdir(exist_ok=True)
    for name in ("instrumental.wav", "pitch.json", ".lock"):
        member = current / name
        if member.is_symlink() or (member.exists() and not member.is_file()):
            raise ValueError("키 변경 캐시 파일의 바로가기·심볼릭 링크는 사용할 수 없습니다.")
    return current


def _cached_pitch(target, manifest, digest, semitones, original):
    if not target.is_file() or not manifest.is_file():
        return False
    try:
        if manifest.stat().st_size > _MAX_MANIFEST_BYTES:
            return False
        with manifest.open("rb") as stream:
            content = stream.read(_MAX_MANIFEST_BYTES + 1)
        if len(content) > _MAX_MANIFEST_BYTES:
            return False
        info = json.loads(content.decode("utf-8"))
        if (
            not isinstance(info, dict)
            or type(info.get("version")) is not int
            or info["version"] != PITCH_VERSION
            or info.get("source_sha256") != digest
            or type(info.get("semitones")) is not int
            or info["semitones"] != semitones
            or not isinstance(info.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", info["sha256"])
        ):
            return False
        # Check the clock before hashing/scanning a possibly misplaced large file.
        if inspect_audio(target) != original or hash_file(target) != info["sha256"]:
            return False
        return inspect_audio(target, scan=True) == original
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def prepare_pitch(source: Path, paths, semitones: int, report=lambda *_: None):
    if isinstance(semitones, bool) or int(semitones) != semitones or not -12 <= semitones <= 12:
        raise ValueError("키는 원곡에서 -12~+12반음까지 바꿀 수 있습니다.")
    source = source.resolve(strict=True)
    if semitones == 0:
        return source
    semitones = int(semitones)
    digest = hash_file(source)
    original = inspect_audio(source)
    folder = _pitch_folder(paths, digest, semitones)
    target, manifest = folder / "instrumental.wav", folder / "pitch.json"
    with FileLock(str(folder / ".lock"), timeout=1800):
        _pitch_folder(paths, digest, semitones)
        if _cached_pitch(target, manifest, digest, semitones, original):
            if hash_file(source) != digest:
                raise ValueError("키 변경 준비 중 원본 음원이 변경되었습니다. 다시 시도해 주세요.")
            return target
        report(f"반주 키를 {semitones:+d}반음으로 준비합니다…", None)
        rate = original["sample_rate"]
        shifted_rate = round(rate * math.pow(2, semitones / 12))
        ratio = shifted_rate / rate
        effect = (
            f"asetrate={shifted_rate},aresample={rate},{tempo_filter(1 / ratio)},"
            f"apad,atrim=end_sample={original['frames']}"
        )
        temporary = folder / f".{uuid.uuid4().hex}.wav"
        try:
            run_ffmpeg(
                [
                    "-i",
                    str(source),
                    "-af",
                    effect,
                    "-c:a",
                    "pcm_f32le",
                    "-rf64",
                    "auto",
                    str(temporary),
                ]
            )
            result = inspect_audio(temporary, scan=True)
            if result != original:
                raise ValueError(
                    "키 변경 후 음원 길이·샘플레이트가 달라졌습니다. 다시 시도해 주세요."
                )
            if hash_file(source) != digest:
                raise ValueError("키 변경 준비 중 원본 음원이 변경되었습니다. 다시 시도해 주세요.")
            _pitch_folder(paths, digest, semitones)
            temporary.replace(target)
            atomic_json(
                manifest,
                {
                    "version": PITCH_VERSION,
                    "source_sha256": digest,
                    "semitones": semitones,
                    "sha256": hash_file(target),
                },
            )
        finally:
            temporary.unlink(missing_ok=True)
    return target
