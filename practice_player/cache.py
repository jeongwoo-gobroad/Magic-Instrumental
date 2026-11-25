# SPDX-License-Identifier: GPL-3.0-only
import json
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from .audio import inspect_audio
from .variants import (
    ALL_VARIANTS,
    SUPPORTED_VARIANTS,
    AudioVariant,
    recommended_model,
    variant_filename,
    variant_target,
)

_STEMS = {"vocals", "instrumental", "guitar", "drums", "bass", "other", "piano", "keys", "synth"}
_OWNED_NAMES = {"_mixture.wav"} | {f"{stem}.wav" for stem in _STEMS} | {
    variant_filename(value) for value in SUPPORTED_VARIANTS
}


def hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class Result:
    instrumental: Path
    source_hash: str
    cached: bool
    device: str
    selected_variant: str = "no_vocals"
    variants: dict[str, AudioVariant] = field(default_factory=dict)
    source_frames: int = 0
    sample_rate: int = 44100

    def to_dict(self) -> dict:
        return {
            "instrumental": str(self.instrumental),
            "source_hash": self.source_hash,
            "cached": self.cached,
            "device": self.device,
            "selected_variant": self.selected_variant,
            "variants": {key: value.to_dict() for key, value in self.variants.items()},
            "source_frames": self.source_frames,
            "sample_rate": self.sample_rate,
        }


def read_cache(directory: Path, source_hash: str, signature: dict) -> dict | None:
    """Verify a complete model set before advertising any of its ready options."""
    try:
        manifest_path = directory / "manifest.json"
        if directory.is_symlink() or manifest_path.is_symlink():
            return None
        if manifest_path.stat().st_size > 1024 * 1024:
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["source_sha256"] != source_hash or manifest["signature"] != signature:
            return None
        if manifest.get("version") != 3:
            return None
        files, stems, variants = (manifest[key] for key in ("files", "stems", "variants"))
        expected_stems = {pair[0] for pair in signature["model"]["stems"]}
        if not all(isinstance(value, dict) for value in (files, stems, variants)):
            return None
        if set(stems) != expected_stems or not set(stems) <= _STEMS:
            return None
        if not variants or not set(variants) <= set(SUPPORTED_VARIANTS):
            return None
        if any(value != "original" and variant_target(value) not in stems for value in variants):
            return None
        if manifest["mixture"] != "_mixture.wav":
            return None
        if any(path != f"{stem}.wav" for stem, path in stems.items()):
            return None
        if any(path != variant_filename(value) for value, path in variants.items()):
            return None
        inventory = {manifest["mixture"], *stems.values(), *variants.values()}
        if set(files) != inventory or not inventory <= _OWNED_NAMES:
            return None
        source_info = manifest["source_audio"]
        if (source_info["sample_rate"] != 44100 or source_info["channels"] != 2
                or type(source_info["frames"]) is not int or source_info["frames"] <= 0):
            return None
        for name, expected in files.items():
            audio = directory / name
            if audio.is_symlink() or audio.stat().st_size != expected["bytes"]:
                return None
            if hash_file(audio) != expected["sha256"]:
                return None
            info = inspect_audio(audio)
            if info != expected["audio"] or info != source_info:
                return None
        return manifest
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError):
        return None


def result_from_cache(
    directory: Path, manifest: dict, variant_id: str, *, cached: bool = True,
) -> Result:
    info = manifest["source_audio"]
    variants = {
        value: AudioVariant(
            value, directory / name, manifest["source_sha256"], info["frames"],
            manifest["signature"]["model"]["key"], directory.name,
            sha256=manifest["files"][name]["sha256"], bytes=manifest["files"][name]["bytes"],
        )
        for value, name in manifest["variants"].items()
    }
    return Result(
        variants[variant_id].path, manifest["source_sha256"], cached, manifest["device"],
        variant_id, variants, info["frames"], info["sample_rate"],
    )


def cached_result(
    directory: Path, source_hash: str, signature: dict, variant_id: str = "no_vocals",
) -> Result | None:
    if signature.get("pipeline") != 2:
        manifest = read_cache(directory, source_hash, signature)
        if manifest and variant_id in manifest["variants"]:
            return result_from_cache(directory, manifest, variant_id)
        return None
    try:
        manifest_path = directory / "manifest.json"
        if (directory.is_symlink() or manifest_path.is_symlink()
                or manifest_path.stat().st_size > 1024 * 1024):
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["source_sha256"] != source_hash or manifest["signature"] != signature:
            return None
        audio = directory / "instrumental.wav"
        if audio.is_symlink():
            return None
        if audio.stat().st_size != manifest["output"]["bytes"]:
            return None
        if hash_file(audio) != manifest["output"]["sha256"]:
            return None
        info = inspect_audio(audio)
        if info != manifest["audio"]:
            return None
        variant = AudioVariant(
            "no_vocals", audio, source_hash, info["frames"], signature["model"]["key"],
            directory.name, sha256=manifest["output"]["sha256"],
            bytes=manifest["output"]["bytes"],
        )
        return Result(
            audio, source_hash, True, manifest["device"], "no_vocals",
            {"no_vocals": variant}, info["frames"], info["sample_rate"],
        )
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def find_cached_variants(source_hash, paths, *, source_frames=None, low_memory=False):
    """Read known v3 recipes without creating paths, files or missing audio choices."""
    from .config import MODELS, Settings

    paths.require_cache_locations()
    if not re.fullmatch(r"[a-f0-9]{64}", source_hash):
        raise ValueError("원곡의 캐시 식별자가 올바르지 않습니다.")
    track = paths.audio_cache / source_hash
    if paths.audio_cache.is_symlink() or track.is_symlink():
        return {}
    models = tuple(dict.fromkeys(("original", "leap", "sw6", "mega", *MODELS)))
    choices, priorities = {}, {}
    frames = source_frames
    for model_index, model in enumerate(models):
        for small in (low_memory, not low_memory):
            settings = Settings(model=model, low_memory=small)
            directory = track / settings.cache_key
            manifest = read_cache(directory, source_hash, settings.signature())
            if manifest is None:
                continue
            try:
                first = next(iter(manifest["variants"]))
                result = result_from_cache(directory, manifest, first)
            except (KeyError, TypeError, ValueError):
                continue
            candidate_frames = manifest["source_audio"]["frames"]
            if frames is None:
                frames = candidate_frames
            elif candidate_frames != frames:
                if source_frames is not None:
                    continue
                raise ValueError("저장된 음원 길이가 서로 다릅니다. 원곡에서 다시 준비하세요.")
            for identifier, variant in result.variants.items():
                priority = (model != recommended_model(identifier), small != low_memory,
                            model_index)
                if identifier not in choices or priority < priorities[identifier]:
                    choices[identifier], priorities[identifier] = variant, priority
    from .combinations import find_cached_combinations

    combined = find_cached_combinations(
        source_hash, paths, source_frames=frames, low_memory=low_memory,
    )
    for identifier, variant in combined.items():
        if frames is None:
            frames = variant.frames
        elif variant.frames != frames:
            raise ValueError("저장된 음원 길이가 서로 다릅니다. 원곡에서 다시 준비하세요.")
        choices[identifier] = variant
    return {identifier: choices[identifier] for identifier in ALL_VARIANTS
            if identifier in choices}


def discover_cached_audio(source, paths, report=lambda *_: None, *, variant_id=None):
    """Build a worker payload for verified choices without preparing or playing audio."""
    if variant_id is not None and variant_id not in ALL_VARIANTS:
        raise ValueError("지원하지 않는 재생 음원입니다.")
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"파일이 아닙니다: {source}")
    report("원곡과 저장된 음원을 확인하는 중…", None)
    source_hash = hash_file(source)
    variants = find_cached_variants(source_hash, paths)
    if hash_file(source) != source_hash:
        raise RuntimeError("확인 중 원본 파일이 변경되었습니다. 저장을 마친 뒤 다시 실행하세요.")
    selected = variant_id if variant_id in variants else next(iter(variants), None)
    current = variants.get(selected)
    return {
        "source_hash": source_hash, "source_frames": current.frames if current else 0,
        "sample_rate": current.sample_rate if current else 44100,
        "variants": {identifier: item.to_dict() for identifier, item in variants.items()},
        "selected_variant": selected, "instrumental": str(current.path) if current else None,
        "cached": True, "device": "cache",
    }
