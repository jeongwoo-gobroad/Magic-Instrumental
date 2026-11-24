# SPDX-License-Identifier: GPL-3.0-only
"""Stable playback choices, independent of model-specific output names."""

from dataclasses import asdict, dataclass
from pathlib import Path

SUPPORTED_VARIANTS = (
    "no_vocals", "vocals", "no_guitar", "guitar",
    "no_drums", "drums", "no_bass", "bass", "no_keys", "keys",
    "no_piano", "piano", "no_synth", "synth",
    "original",
)
TARGET_LABELS = {
    "vocals": "보컬", "guitar": "기타", "drums": "드럼", "bass": "베이스",
    "keys": "키보드", "piano": "피아노", "synth": "신시사이저",
}


def variant_target(variant_id: str) -> str | None:
    if variant_id not in SUPPORTED_VARIANTS:
        raise ValueError("지원하지 않는 재생 음원입니다.")
    return None if variant_id == "original" else variant_id.removeprefix("no_")


def variant_mode(variant_id: str) -> str:
    variant_target(variant_id)
    if variant_id == "original":
        return "original"
    return "without" if variant_id.startswith("no_") else "stem"


def variant_label(variant_id: str) -> str:
    if variant_id == "original":
        return "원곡 그대로"
    target = TARGET_LABELS[variant_target(variant_id)]
    return f"{target} 없는 반주" if variant_mode(variant_id) == "without" else f"{target}만 듣기"


def variant_filename(variant_id: str) -> str:
    variant_target(variant_id)
    if variant_id == "original":
        return "_mixture.wav"
    return "instrumental.wav" if variant_id == "no_vocals" else f"{variant_id}.wav"


def supported_variants(model_key: str) -> tuple[str, ...]:
    from .config import MODELS

    if model_key == "original":
        return ("original",)
    if model_key not in MODELS:
        raise ValueError("지원하지 않는 분리 모델입니다.")
    stems = dict(MODELS[model_key].stems)
    return tuple(value for value in SUPPORTED_VARIANTS
                 if value == "original" or variant_target(value) in stems)


def recommended_model(variant_id: str) -> str:
    if variant_id == "original":
        return "original"
    target = variant_target(variant_id)
    if target == "vocals":
        return "leap"
    return "mega" if target in {"keys", "synth"} else "sw6"


@dataclass(frozen=True)
class AudioVariant:
    id: str
    path: Path
    source_hash: str
    frames: int
    model_key: str
    recipe_key: str
    sample_rate: int = 44100
    channels: int = 2
    pitch_semitones: int = 0
    sha256: str = ""
    bytes: int = 0

    @property
    def label(self) -> str:
        return variant_label(self.id)

    @property
    def target(self) -> str | None:
        return variant_target(self.id)

    @property
    def mode(self) -> str:
        return variant_mode(self.id)

    def to_dict(self) -> dict:
        return asdict(self) | {
            "path": str(self.path), "label": self.label,
            "target": self.target, "mode": self.mode,
        }
