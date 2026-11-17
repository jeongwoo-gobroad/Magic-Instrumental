# SPDX-License-Identifier: GPL-3.0-only
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from .audio import decode, inspect_audio
from .cache import Result, cached_result, hash_file
from .config import Paths, Settings


def process(
    source: Path,
    settings: Settings,
    paths: Paths,
    report=lambda *_: None,
    *,
    offline: bool = False,
    runner=None,
) -> Result:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"파일이 아닙니다: {source}")
    paths.create()
    report("파일 내용 SHA-256 확인 중…", None)
    source_hash = hash_file(source)
    track = paths.processed / source_hash
    track.mkdir(exist_ok=True)
    destination = track / settings.cache_key
    report("캐시 확인 중…", None)
    with FileLock(str(track / f".{settings.cache_key}.lock"), timeout=3600):
        hit = cached_result(destination, source_hash, settings.signature())
        if hit:
            report("캐시 사용 · 재분리 없이 준비 완료", 1.0)
            return hit
        # A cancelled worker may leave scratch data. Only clean the same locked profile.
        for stale in track.glob(f".{settings.cache_key}-tmp-*"):
            if stale.is_dir():
                shutil.rmtree(stale)
        with tempfile.TemporaryDirectory(prefix=f".{settings.cache_key}-tmp-", dir=track) as tmp:
            working = Path(tmp)
            snapshot = working / f"source{source.suffix}"
            shutil.copyfile(source, snapshot)
            if hash_file(snapshot) != source_hash:
                raise RuntimeError(
                    "처리 중 원본 파일이 변경되었습니다. 파일 저장을 마친 뒤 다시 실행하세요."
                )
            report("음원을 44.1 kHz 스테레오로 변환 중…", None)
            decoded = working / "input.wav"
            decode(snapshot, decoded)
            snapshot.unlink()
            staged = working / "result"
            staged.mkdir()
            instrumental = staged / "instrumental.wav"
            if runner is None:
                from .engine import separate_audio

                runner = separate_audio
            device = runner(decoded, instrumental, settings, paths, report, offline=offline)
            report("분리 결과 검증 중…", None)
            info = inspect_audio(instrumental, scan=True)
            expected = inspect_audio(decoded)
            if (
                info["sample_rate"] != expected["sample_rate"]
                or abs(info["frames"] - expected["frames"]) > 4410
            ):
                raise RuntimeError("분리 결과 길이 또는 샘플레이트가 원본과 일치하지 않습니다.")
            manifest = {
                "source_sha256": source_hash,
                "source_name": source.name,
                "source_bytes": source.stat().st_size,
                "signature": settings.signature(),
                "device": device,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "audio": info,
                "output": {"bytes": instrumental.stat().st_size, "sha256": hash_file(instrumental)},
            }
            (staged / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # Existing invalid cache is replaced only after successful separation/validation.
            if destination.exists():
                # User exports may live here. Replace only our two cache artifacts.
                instrumental.replace(destination / "instrumental.wav")
                (staged / "manifest.json").replace(destination / "manifest.json")
            else:
                staged.replace(destination)
        report("인스트루먼탈 준비 완료", 1.0)
        return Result(destination / "instrumental.wav", source_hash, False, device)
