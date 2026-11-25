# SPDX-License-Identifier: GPL-3.0-only
"""Cache model outputs once; derive synchronized solo and backing-track choices."""

import copy
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf
from filelock import FileLock

from .audio import decode, inspect_audio
from .cache import Result, cached_result, hash_file, read_cache, result_from_cache
from .config import MODELS, Paths, Settings
from .variants import (
    SUPPORTED_VARIANTS,
    supported_variants,
    variant_filename,
    variant_mode,
    variant_target,
)

_TAIL_ALLOWANCE = 4410


def _unchanged(source, expected):
    if hash_file(source) != expected:
        raise RuntimeError("처리 중 원본 파일이 변경되었습니다. 저장을 마친 뒤 다시 실행하세요.")


def _fit_frames(source: Path, destination: Path, expected: dict) -> int:
    info = inspect_audio(source, scan=True)
    difference = info["frames"] - expected["frames"]
    if (info["sample_rate"] != expected["sample_rate"]
            or abs(difference) > _TAIL_ALLOWANCE):
        raise RuntimeError("분리 결과 길이 또는 샘플레이트가 원본과 일치하지 않습니다.")
    if difference == 0:
        shutil.copyfile(source, destination)
        return 0
    # Keep sample zero fixed; only a bounded encoder/model tail can be padded or trimmed.
    with sf.SoundFile(source) as audio, sf.SoundFile(
        destination, "w", samplerate=44100, channels=2, subtype="FLOAT", format="WAV",
    ) as output:
        remaining = expected["frames"]
        while remaining:
            count = min(262144, remaining)
            block = audio.read(count, dtype="float32", always_2d=True)
            if len(block) < count:
                block = np.pad(block, ((0, count - len(block)), (0, 0)))
            output.write(block)
            remaining -= count
    return difference


def _subtract(mixture: Path, target: Path, destination: Path) -> None:
    with sf.SoundFile(mixture) as music, sf.SoundFile(target) as stem, sf.SoundFile(
        destination, "w", samplerate=44100, channels=2, subtype="FLOAT", format="WAV",
    ) as output:
        if (music.frames != stem.frames or music.samplerate != stem.samplerate
                or music.channels != stem.channels):
            raise ValueError("같은 원곡 시간에 맞춘 분리 결과가 필요합니다.")
        for block in music.blocks(blocksize=262144, dtype="float32", always_2d=True):
            other = stem.read(len(block), dtype="float32", always_2d=True)
            result = block - other
            if not np.isfinite(result).all():
                raise ValueError("분리 결과에 NaN/Inf가 있습니다. 결과를 게시하지 않았습니다.")
            output.write(result)


def _file_record(path: Path) -> dict:
    return {
        "bytes": path.stat().st_size, "sha256": hash_file(path), "audio": inspect_audio(path),
    }


def _derive(
    manifest: dict, existing: Path, staged: Path, requested: tuple[str, ...],
) -> dict:
    result = copy.deepcopy(manifest)
    for value in requested:
        if value in result["variants"]:
            continue
        if value == "original":
            result["variants"][value] = result["mixture"]
            continue
        target = variant_target(value)
        if target not in result["stems"]:
            raise ValueError("선택한 모델은 이 악기의 분리를 지원하지 않습니다.")
        name = variant_filename(value)
        if variant_mode(value) == "stem":
            result["variants"][value] = result["stems"][target]
        elif value == "no_vocals" and "instrumental" in result["stems"]:
            result["variants"][value] = result["stems"]["instrumental"]
        else:
            _subtract(existing / result["mixture"], existing / result["stems"][target],
                      staged / name)
            result["variants"][value] = name
            result["files"][name] = _file_record(staged / name)
    return result


def _publish(staged: Path, destination: Path, manifest: dict) -> None:
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError("캐시 경로의 바로가기·심볼릭 링크는 사용할 수 없습니다.")
    (staged / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8",
    )
    if not destination.exists():
        staged.replace(destination)
        return
    # Do not remove arbitrary files: users can have exports beside cached audio.
    for name in manifest["files"]:
        candidate = staged / name
        if candidate.is_file():
            candidate.replace(destination / name)
    (staged / "manifest.json").replace(destination / "manifest.json")


def _legacy_snapshot(track, settings, working):
    signature, key = settings.legacy_signature(), settings.legacy_cache_key
    if signature is None or key is None:
        return None
    with FileLock(str(track / f".{key}.lock"), timeout=3600):
        hit = cached_result(track / key, track.name, signature)
        if not hit:
            return None
        checksum = hash_file(hit.instrumental)
        snapshot = working / "legacy-instrumental.wav"
        shutil.copyfile(hit.instrumental, snapshot)
        if hash_file(snapshot) != checksum:
            raise RuntimeError("기존 분리 결과가 복사 중 변경되었습니다. 다시 시도하세요.")
        return hit.device, snapshot, {
            "pipeline": 2, "cache_key": key, "sha256": checksum,
            "vocals_recipe": "mixture-minus-legacy-instrumental-v1",
        }


def process(
    source: Path,
    settings: Settings,
    paths: Paths,
    report=lambda *_: None,
    *,
    offline: bool = False,
    runner=None,
    stem_runner=None,
    variant_id: str = "no_vocals",
    prepare_all: bool = False,
    variant_ids: tuple[str, ...] | None = None,
) -> Result:
    supported = supported_variants(settings.model)
    if prepare_all and variant_ids is not None:
        raise ValueError("전체 준비와 선택한 음원 목록을 함께 지정할 수 없습니다.")
    if prepare_all:
        requested = supported
    elif variant_ids is not None:
        requested = tuple(dict.fromkeys(variant_ids))
    else:
        requested = (variant_id,)
    if not requested or variant_id not in requested:
        raise ValueError("처음 재생할 음원을 준비할 목록에 포함하세요.")
    if not set(requested) <= set(supported):
        raise ValueError("선택한 모델은 이 악기의 분리를 지원하지 않습니다.")
    if runner is not None and stem_runner is not None:
        raise ValueError("분리 작업 함수는 하나만 지정하세요.")
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"파일이 아닙니다: {source}")
    paths.create()
    report("파일 내용 SHA-256 확인 중…", None)
    source_hash = hash_file(source)
    track = paths.processed / source_hash
    if track.is_symlink():
        raise ValueError("캐시 경로의 바로가기·심볼릭 링크는 사용할 수 없습니다.")
    track.mkdir(exist_ok=True)
    destination = track / settings.cache_key
    if destination.is_symlink():
        raise ValueError("캐시 경로의 바로가기·심볼릭 링크는 사용할 수 없습니다.")
    signature = settings.signature()
    report("캐시 확인 중…", None)
    with FileLock(str(track / f".{settings.cache_key}.lock"), timeout=3600):
        manifest = read_cache(destination, source_hash, signature)
        if manifest:
            if not set(requested) <= manifest["variants"].keys():
                report("저장된 분리 결과로 재생 음원을 준비합니다…", None)
                with tempfile.TemporaryDirectory(
                    prefix=f".{settings.cache_key}-tmp-", dir=track,
                ) as tmp:
                    staged = Path(tmp) / "derived"
                    staged.mkdir()
                    manifest = _derive(manifest, destination, staged, requested)
                    _unchanged(source, source_hash)
                    _publish(staged, destination, manifest)
            else:
                _unchanged(source, source_hash)
            report("캐시 사용 · 재분리 없이 준비 완료", 1.0)
            return result_from_cache(destination, manifest, variant_id)

        for stale in track.glob(f".{settings.cache_key}-tmp-*"):
            if stale.is_dir() and not stale.is_symlink():
                shutil.rmtree(stale)
        with tempfile.TemporaryDirectory(prefix=f".{settings.cache_key}-tmp-", dir=track) as tmp:
            working = Path(tmp)
            snapshot = working / f"source{source.suffix}"
            shutil.copyfile(source, snapshot)
            if hash_file(snapshot) != source_hash:
                raise RuntimeError(
                    "처리 중 원본 파일이 변경되었습니다. 파일 저장을 마친 뒤 다시 실행하세요."
                )
            source_bytes = snapshot.stat().st_size
            report("음원을 44.1 kHz 스테레오로 변환 중…", None)
            decoded = working / "input.wav"
            decode(snapshot, decoded)
            snapshot.unlink()
            expected = inspect_audio(decoded, scan=True)
            staged, raw = working / "result", working / "raw"
            staged.mkdir()
            raw.mkdir()
            legacy = _legacy_snapshot(track, settings, working)
            if settings.model == "original":
                device, outputs, origin = "cpu", {}, {"runner": "canonical-original"}
                expected_stems = set()
            else:
                expected_stems = set(dict(MODELS[settings.model].stems))
                if legacy:
                    device, instrumental, origin = legacy
                    outputs = {"instrumental": instrumental}
                    report("기존 보컬 분리 캐시를 재사용합니다…", None)
                elif runner is not None:
                    if not MODELS[settings.model].instrumental_stem:
                        raise ValueError("다중 파트 모델은 stem_runner를 사용하세요.")
                    instrumental = raw / "instrumental.wav"
                    device = runner(decoded, instrumental, settings, paths, report, offline=offline)
                    outputs = {"instrumental": instrumental}
                    origin = {"runner": "legacy-instrumental"}
                else:
                    if stem_runner is None:
                        from .engine import separate_stems

                        stem_runner = separate_stems
                    device, outputs = stem_runner(
                        decoded, raw, settings, paths, report, offline=offline,
                    )
                    origin = {"runner": "model-stems"}
            if not isinstance(outputs, dict) or not set(outputs) <= expected_stems:
                raise RuntimeError("분리 엔진의 파트 목록이 모델 설정과 다릅니다.")
            report("분리 결과와 원곡 시간 길이를 검증합니다…", None)
            adjustments = {}
            for name, path in outputs.items():
                adjustments[name] = _fit_frames(Path(path), staged / f"{name}.wav", expected)
            decoded.replace(staged / "_mixture.wav")
            if set(outputs) == {"instrumental"} and expected_stems == {"vocals", "instrumental"}:
                _subtract(staged / "_mixture.wav", staged / "instrumental.wav",
                          staged / "vocals.wav")
                adjustments["vocals"] = 0
            elif set(outputs) != expected_stems:
                raise RuntimeError("분리 엔진이 필요한 파트를 모두 생성하지 못했습니다.")
            stems = {name: f"{name}.wav" for name in sorted(expected_stems)}
            direct = {
                value: variant_filename(value) for value in SUPPORTED_VARIANTS
                if value in supported and (
                    value == "original" or variant_mode(value) == "stem"
                    or (value == "no_vocals" and "instrumental" in stems)
                )
            }
            manifest = {
                "version": 3, "source_sha256": source_hash, "source_name": source.name,
                "source_bytes": source_bytes, "signature": signature, "device": device,
                "created_at": datetime.now(timezone.utc).isoformat(), "source_audio": expected,
                "mixture": "_mixture.wav", "stems": stems, "variants": direct,
                "origin": origin, "tail_adjustments": adjustments,
                "files": {
                    name: _file_record(staged / name)
                    for name in sorted({"_mixture.wav", *stems.values()})
                },
            }
            manifest = _derive(manifest, staged, staged, requested)
            _unchanged(source, source_hash)
            _publish(staged, destination, manifest)
        report("재생 음원 준비 완료", 1.0)
        return result_from_cache(destination, manifest, variant_id, cached=legacy is not None)
