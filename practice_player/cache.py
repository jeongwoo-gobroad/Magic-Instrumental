# SPDX-License-Identifier: GPL-3.0-only
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .audio import inspect_audio


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

    def to_dict(self) -> dict:
        return {
            "instrumental": str(self.instrumental),
            "source_hash": self.source_hash,
            "cached": self.cached,
            "device": self.device,
        }


def cached_result(directory: Path, source_hash: str, signature: dict) -> Result | None:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest["source_sha256"] != source_hash or manifest["signature"] != signature:
            return None
        audio = directory / "instrumental.wav"
        if audio.stat().st_size != manifest["output"]["bytes"]:
            return None
        if hash_file(audio) != manifest["output"]["sha256"]:
            return None
        info = inspect_audio(audio)
        if info != manifest["audio"]:
            return None
        return Result(audio, source_hash, True, manifest["device"])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None
