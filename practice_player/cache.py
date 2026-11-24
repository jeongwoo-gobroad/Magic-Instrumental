# SPDX-License-Identifier: GPL-3.0-only
import json
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from .audio import inspect_audio
from .variants import SUPPORTED_VARIANTS, AudioVariant, variant_filename, variant_target

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
