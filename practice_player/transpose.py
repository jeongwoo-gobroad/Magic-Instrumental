# SPDX-License-Identifier: GPL-3.0-only
"""Independent key changes keep original media time and separation caches intact."""

import math
import uuid
from pathlib import Path

from filelock import FileLock

from .audio import inspect_audio, run_ffmpeg, tempo_filter
from .cache import hash_file
from .score import atomic_json

PITCH_VERSION = 1


def prepare_pitch(source: Path, paths, semitones: int, report=lambda *_: None):
    if isinstance(semitones, bool) or int(semitones) != semitones or not -12 <= semitones <= 12:
        raise ValueError("키는 원곡에서 -12~+12반음까지 바꿀 수 있습니다.")
    source = source.resolve(strict=True)
    if semitones == 0:
        return source
    digest = hash_file(source)
    folder = paths.processed / "pitch" / digest / f"v{PITCH_VERSION}-{semitones:+d}"
    folder.mkdir(parents=True, exist_ok=True)
    target, manifest = folder / "instrumental.wav", folder / "pitch.json"
    with FileLock(str(folder / ".lock"), timeout=1800):
        import json

        if target.is_file() and manifest.is_file():
            info = json.loads(manifest.read_text(encoding="utf-8"))
            if info.get("sha256") == hash_file(target):
                return target
        report(f"반주 키를 {semitones:+d}반음으로 준비합니다…", None)
        original = inspect_audio(source)
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
            if result["frames"] != original["frames"] or hash_file(source) != digest:
                raise ValueError("키 변경 후 음원 길이가 달라졌습니다. 다시 시도해 주세요.")
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
