# SPDX-License-Identifier: GPL-3.0-only
"""Stable playback choices, independent of model-specific output names."""

from dataclasses import asdict, dataclass
from itertools import combinations
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
REMOVAL_TARGETS = tuple(TARGET_LABELS)
COMBINATION_VARIANTS = tuple(
    "no_" + "_".join(targets)
    for count in range(2, len(REMOVAL_TARGETS) + 1)
    for targets in combinations(REMOVAL_TARGETS, count)
)
ALL_VARIANTS = (*SUPPORTED_VARIANTS, *COMBINATION_VARIANTS)
MAX_PORTABLE_VARIANTS = 128


def is_combination(identifier: str) -> bool:
    return identifier in COMBINATION_VARIANTS


def removal_targets(identifier: str) -> tuple[str, ...]:
    if identifier not in ALL_VARIANTS:
        raise ValueError("지원하지 않는 재생 음원입니다.")
    return tuple(identifier[3:].split("_")) if identifier.startswith("no_") else ()


def canonical_combination(identifiers) -> str | None:
    targets = set()
    for identifier in identifiers:
        targets.update(removal_targets(identifier))
    if len(targets) < 2:
        return None
    return "no_" + "_".join(target for target in REMOVAL_TARGETS if target in targets)


def variant_target(variant_id: str) -> str | None:
    if variant_id not in ALL_VARIANTS:
        raise ValueError("지원하지 않는 재생 음원입니다.")
    if is_combination(variant_id):
        return None
    return None if variant_id == "original" else variant_id.removeprefix("no_")


def variant_mode(variant_id: str) -> str:
    variant_target(variant_id)
    if variant_id == "original":
        return "original"
    return "without" if variant_id.startswith("no_") else "stem"


def variant_label(variant_id: str) -> str:
    if variant_id == "original":
        return "원곡 그대로"
    if is_combination(variant_id):
        targets = " + ".join(TARGET_LABELS[target] for target in removal_targets(variant_id))
        return f"{targets} 없는 반주"
    target = TARGET_LABELS[variant_target(variant_id)]
    return f"{target} 없는 반주" if variant_mode(variant_id) == "without" else f"{target}만 듣기"


def validate_variant_identity(item, *, allow_combinations=True):
    if not isinstance(item, dict):
        raise ValueError("재생 음원 정보가 올바르지 않습니다.")
    identifier, label = item.get("id"), item.get("label")
    if (not isinstance(identifier, str) or identifier not in ALL_VARIANTS
            or not isinstance(label, str) or not label.strip() or len(label) > 100
            or "target" not in item or item["target"] != variant_target(identifier)
            or item.get("mode") != variant_mode(identifier)):
        raise ValueError("재생 음원 이름·악기·분리 방식이 올바르지 않습니다.")
    result = {key: item[key] for key in ("id", "label", "mode", "target")}
    if is_combination(identifier):
        if not allow_combinations or item.get("targets") != list(removal_targets(identifier)):
            raise ValueError("조합할 악기 목록이 올바르지 않습니다.")
        result["targets"] = list(item["targets"])
    elif "targets" in item:
        raise ValueError("단일 음원에는 조합할 악기 목록을 지정할 수 없습니다.")
    return result


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
    if is_combination(variant_id):
        return "sequential"
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
        result = asdict(self) | {
            "path": str(self.path), "label": self.label,
            "target": self.target, "mode": self.mode,
        }
        if is_combination(self.id):
            result["targets"] = list(removal_targets(self.id))
        return result
