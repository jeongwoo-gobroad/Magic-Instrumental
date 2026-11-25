# SPDX-License-Identifier: GPL-3.0-only
"""Prepare combined removals from successive residuals, with their own cache recipe."""

import json
import re
import shutil
import tempfile
from hashlib import sha256
from itertools import islice
from pathlib import Path

from filelock import FileLock

from .audio import inspect_audio
from .cache import Result, hash_file
from .cache_locations import ensure_cache_directory
from .config import MODELS, Settings
from .variants import AudioVariant, is_combination, recommended_model, removal_targets

COMBINATION_VERSION = 1
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_FOLDER = re.compile(r"combination-[a-f0-9]{24}\Z")


def _linked(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def combination_signature(identifier, model="auto", low_memory=False):
    if not is_combination(identifier) or model not in ("auto", *MODELS):
        raise ValueError("지원하지 않는 음원 조합 또는 분리 모델입니다.")
    targets = removal_targets(identifier)
    steps = []
    for target in targets:
        owner = recommended_model(f"no_{target}") if model == "auto" else model
        if target not in dict(MODELS[owner].stems):
            raise ValueError(
                "선택한 모델이 이 음원 조합을 지원하지 않습니다. 자동 추천을 선택하세요."
            )
        steps.append({
            "target": target, "settings": Settings(owner, low_memory=low_memory).signature(),
        })
    return {
        "version": COMBINATION_VERSION, "recipe": "sequential-residual-v1",
        "id": identifier, "model": model, "low_memory": bool(low_memory), "steps": steps,
    }


def combination_cache_key(signature):
    digest = sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    return f"combination-{digest[:24]}"


def _read_combination(directory, source_hash, signature):
    try:
        manifest_path = directory / "manifest.json"
        if (_linked(directory) or _linked(manifest_path)
                or manifest_path.stat().st_size > 1024 * 1024):
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest["source_sha256"] != source_hash or manifest["signature"] != signature
                or manifest["version"] != COMBINATION_VERSION
                or directory.name != combination_cache_key(signature)
                or not isinstance(manifest["device"], str) or not manifest["device"]
                or set(manifest["files"]) != {"combined.wav"}):
            return None
        record = manifest["files"]["combined.wav"]
        path = directory / "combined.wav"
        info = manifest["source_audio"]
        if (_linked(path) or path.stat().st_size != record["bytes"]
                or hash_file(path) != record["sha256"]
                or inspect_audio(path, scan=True) != info
                or info["sample_rate"] != 44100 or info["channels"] != 2
                or type(info["frames"]) is not int or info["frames"] <= 0):
            return None
        steps = manifest["steps"]
        if len(steps) != len(signature["steps"]):
            return None
        previous = source_hash
        for step, expected in zip(steps, signature["steps"], strict=True):
            if (step["input_sha256"] != previous
                    or step["target"] != expected["target"]
                    or step["model"] != expected["settings"]["model"]["key"]
                    or not isinstance(step["output_sha256"], str)
                    or not _SHA.fullmatch(step["output_sha256"])):
                return None
            previous = step["output_sha256"]
        if previous != record["sha256"]:
            return None
        return manifest
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError):
        return None


def _result(directory, manifest, *, cached=True):
    info, signature = manifest["source_audio"], manifest["signature"]
    record = manifest["files"]["combined.wav"]
    variant = AudioVariant(
        signature["id"], directory / "combined.wav", manifest["source_sha256"],
        info["frames"], "sequential", directory.name,
        sha256=record["sha256"], bytes=record["bytes"],
    )
    return Result(
        variant.path, variant.source_hash, cached, manifest["device"], variant.id,
        {variant.id: variant}, info["frames"], info["sample_rate"],
    )


def process_combination(source, identifier, model, device, low_memory, paths,
                        report=lambda *_: None, *, offline=False, processor=None):
    """Each model hears the previous residual; overlapping original estimates are never summed."""
    paths.require_cache_locations()
    signature = combination_signature(identifier, model, low_memory)
    source = Path(source).resolve(strict=True)
    if not source.is_file():
        raise ValueError("조합을 준비할 원곡 파일이 없습니다.")
    if processor is None:
        from .pipeline import process

        processor = process
    source_hash = hash_file(source)
    paths.create()
    ensure_cache_directory(paths.audio_cache)
    track = paths.audio_cache / source_hash
    namespace = track / "combinations-v1"
    if any(_linked(path) for path in (paths.audio_cache, track, namespace)):
        raise ValueError("캐시 경로의 바로가기·심볼릭 링크는 사용할 수 없습니다.")
    namespace.mkdir(parents=True, exist_ok=True)
    key = combination_cache_key(signature)
    directory = namespace / key
    lock = namespace / f".{key}.lock"
    if any(_linked(path) for path in (
        directory, lock, directory / "manifest.json", directory / "combined.wav",
    )):
        raise ValueError("캐시 경로의 바로가기·심볼릭 링크는 사용할 수 없습니다.")
    with FileLock(str(lock), timeout=3600):
        manifest = _read_combination(directory, source_hash, signature)
        if manifest:
            if hash_file(source) != source_hash:
                raise RuntimeError("확인 중 원곡이 변경되었습니다. 다시 준비하세요.")
            report("저장된 악기 조합을 불러왔습니다.", 1)
            return _result(directory, manifest)
        for stale in islice(namespace.iterdir(), 1024):
            if (re.fullmatch(re.escape(f".{key}-") + r"[a-z0-9_]{8}", stale.name)
                    and stale.is_dir() and not _linked(stale)):
                members = list(islice(stale.iterdir(), 3))
                if (len(members) <= 2 and all(
                    child.name in {"combined.wav", "manifest.json"}
                    and child.is_file() and not _linked(child) for child in members
                )):
                    shutil.rmtree(stale)
        current, results, steps, expected = source, [], [], None
        for index, step in enumerate(signature["steps"], 1):
            target, owner = step["target"], step["settings"]["model"]["key"]
            report(f"음원 조합 준비 · {index}/{len(signature['steps'])}", None)
            incoming_hash = hash_file(current)
            result = processor(
                current, Settings(owner, device, low_memory), paths, report,
                offline=offline, variant_id=f"no_{target}",
            )
            selected = result.variants.get(f"no_{target}")
            if (selected is None or result.source_hash != incoming_hash
                    or selected.source_hash != incoming_hash
                    or result.selected_variant != f"no_{target}"
                    or selected.model_key != owner
                    or selected.path != result.instrumental
                    or selected.pitch_semitones != 0):
                raise ValueError("조합 단계의 음원 식별 정보가 일치하지 않습니다.")
            info = inspect_audio(selected.path, scan=True)
            if expected is None:
                expected = info
            if (info != expected or selected.frames != info["frames"]
                    or result.source_frames != info["frames"]
                    or selected.sample_rate != 44100 or result.sample_rate != 44100
                    or selected.channels != 2 or info["sample_rate"] != 44100
                    or selected.path.stat().st_size != selected.bytes
                    or hash_file(selected.path) != selected.sha256):
                raise ValueError("조합 단계의 원곡 시간·검증 정보가 일치하지 않습니다.")
            if hash_file(current) != incoming_hash or hash_file(source) != source_hash:
                raise RuntimeError("처리 중 원본 또는 조합 음원이 변경되었습니다. 다시 준비하세요.")
            steps.append({"target": target, "model": owner,
                          "input_sha256": incoming_hash, "output_sha256": selected.sha256})
            results.append(result)
            current = selected.path
        with tempfile.TemporaryDirectory(prefix=f".{key}-", dir=namespace) as temporary:
            staged = Path(temporary)
            output = staged / "combined.wav"
            shutil.copyfile(current, output)
            checksum = hash_file(output)
            if checksum != steps[-1]["output_sha256"] or hash_file(source) != source_hash:
                raise RuntimeError("저장 중 원곡 또는 조합 음원이 변경되었습니다. 다시 준비하세요.")
            manifest = {
                "version": COMBINATION_VERSION, "source_sha256": source_hash,
                "source_audio": expected, "signature": signature, "steps": steps,
                "device": next((item.device for item in results if not item.cached),
                               results[-1].device),
                "files": {"combined.wav": {"bytes": output.stat().st_size, "sha256": checksum}},
            }
            (staged / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2),
                encoding="utf-8",
            )
            if any(_linked(path) for path in (
                paths.audio_cache, track, namespace, directory,
                directory / "manifest.json", directory / "combined.wav",
            )):
                raise ValueError("캐시 경로의 바로가기·심볼릭 링크는 사용할 수 없습니다.")
            directory.mkdir(exist_ok=True)
            output.replace(directory / "combined.wav")
            (staged / "manifest.json").replace(directory / "manifest.json")
        report("악기 조합 준비 완료", 1)
        return _result(directory, manifest, cached=all(item.cached for item in results))


def find_cached_combinations(source_hash, paths, *, source_frames=None, low_memory=False):
    """Discover only current, verified recipes; never infer or create a missing combination."""
    paths.require_cache_locations()
    if not isinstance(source_hash, str) or not _SHA.fullmatch(source_hash):
        raise ValueError("원곡의 캐시 식별자가 올바르지 않습니다.")
    namespace = paths.audio_cache / source_hash / "combinations-v1"
    if any(_linked(path) for path in (paths.audio_cache, namespace.parent, namespace)):
        return {}
    if not namespace.is_dir():
        return {}
    choices, priorities = {}, {}
    candidates = list(islice(namespace.iterdir(), 1025))
    if len(candidates) > 1024:
        return {}
    for directory in sorted(candidates):
        if not _FOLDER.fullmatch(directory.name) or _linked(directory):
            continue
        try:
            manifest_path = directory / "manifest.json"
            if _linked(manifest_path) or manifest_path.stat().st_size > 1024 * 1024:
                continue
            metadata = json.loads(manifest_path.read_text(encoding="utf-8"))["signature"]
            if type(metadata["low_memory"]) is not bool:
                continue
            signature = combination_signature(
                metadata["id"], metadata["model"], metadata["low_memory"],
            )
            manifest = _read_combination(directory, source_hash, signature)
            if not manifest:
                continue
            result = _result(directory, manifest)
            if source_frames is not None and result.source_frames != source_frames:
                continue
            priority = (metadata["model"] != "auto", metadata["low_memory"] != low_memory,
                        directory.name)
            identifier = result.selected_variant
            if identifier not in choices or priority < priorities[identifier]:
                choices[identifier] = result.variants[identifier]
                priorities[identifier] = priority
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError):
            continue
    return choices
