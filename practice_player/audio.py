# SPDX-License-Identifier: GPL-3.0-only
"""FFmpeg decoding/export; no shell interpolation and no system FFmpeg required."""

import math
import os
import shutil
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import SAMPLE_RATE

AUDIO_EXTENSIONS = frozenset(
    ".wav .wave .flac .mp3 .m4a .m4b .aac .aif .aiff .aifc .ogg .oga .opus "
    ".wma .alac .ape .wv .mp2 .mpa .ac3 .eac3 .dts .caf .au .snd .amr .3gp "
    ".mp4 .mkv .webm .mov .avi .mka .dsf .dff .tta .tak .m4v .aax".split()
)


def discover(targets: Path) -> list[Path]:
    return sorted(
        (p for p in targets.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS),
        key=lambda p: str(p.relative_to(targets)).casefold(),
    )


@lru_cache(maxsize=1)
def ffmpeg() -> str:
    configured = os.environ.get("FFMPEG_BINARY")
    if configured:
        binary = shutil.which(configured)
        if not binary:
            raise RuntimeError(f"FFMPEG_BINARY 실행 파일을 찾을 수 없습니다: {configured}")
        return binary
    system = shutil.which("ffmpeg")
    if system:
        return system
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def run_ffmpeg(args: list[str]) -> None:
    process = subprocess.Popen(
        [ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        _, stderr = process.communicate()
    except BaseException:
        process.kill()
        process.wait()
        raise
    if process.returncode:
        detail = stderr.decode("utf-8", errors="replace")[-3000:]
        raise RuntimeError(f"음원 변환 실패. 파일 손상·코덱·DRM 여부를 확인하세요.\n{detail}")


def decode(source: Path, destination: Path) -> None:
    run_ffmpeg(
        [
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "2",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_f32le",
            "-rf64",
            "auto",
            str(destination),
        ]
    )
    inspect_audio(destination)


def inspect_audio(path: Path, *, scan: bool = False) -> dict:
    with sf.SoundFile(path) as audio:
        if audio.frames <= 0 or audio.samplerate <= 0 or audio.channels != 2:
            raise ValueError(f"유효한 스테레오 오디오가 아닙니다: {path.name}")
        info = {
            "frames": audio.frames,
            "sample_rate": audio.samplerate,
            "channels": audio.channels,
            "duration": audio.frames / audio.samplerate,
        }
        if scan:
            for block in audio.blocks(blocksize=262144, dtype="float32", always_2d=True):
                if not np.isfinite(block).all():
                    raise ValueError("분리 결과에 NaN/Inf가 있습니다. CPU 모드로 재시도하세요.")
    return info


def tempo_filter(speed: float) -> str:
    if not math.isfinite(speed) or not 0.25 <= speed <= 2.0:
        raise ValueError("속도는 0.25~2.00 사이여야 합니다.")
    factors = []
    while speed < 0.5:
        factors.append(0.5)
        speed /= 0.5
    factors.append(speed)
    return ",".join(f"atempo={factor:.8g}" for factor in factors)


def publish_export(staged: Path, destination: Path, *, overwrite: bool) -> None:
    if overwrite:
        staged.replace(destination)
    elif os.name == "nt":
        # Windows rename fails if the destination exists, including on USB drives.
        staged.rename(destination)
    else:
        os.link(staged, destination)


def export_tempo(source: Path, destination: Path, speed: float, *, overwrite: bool = False) -> Path:
    effect = tempo_filter(speed)
    if destination.suffix.lower() not in {".wav", ".flac"}:
        raise ValueError("내보내기는 WAV 또는 FLAC을 선택하세요.")
    if source.resolve() == destination.resolve() or (
        destination == destination.with_name("instrumental.wav")
        and (destination.parent / "manifest.json").exists()
    ):
        raise ValueError("원본 캐시를 덮어쓸 수 없습니다. 다른 경로를 선택하세요.")
    if destination.exists() and not overwrite:
        raise FileExistsError("출력 파일이 이미 있습니다. 다른 이름 또는 --overwrite를 사용하세요.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{uuid.uuid4().hex}{destination.suffix}")
    codec = (
        ["-c:a", "pcm_s24le", "-rf64", "auto"]
        if destination.suffix.lower() == ".wav"
        else ["-c:a", "flac"]
    )
    try:
        run_ffmpeg(["-i", str(source), "-af", effect, *codec, str(temporary)])
        inspect_audio(temporary)
        publish_export(temporary, destination, overwrite=overwrite)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
