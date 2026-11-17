# SPDX-License-Identifier: GPL-3.0-only
import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_RATE = 44100
PIPELINE_VERSION = 2


@dataclass(frozen=True)
class Model:
    key: str
    label: str
    repo: str
    revision: str
    weights: str
    weights_sha256: str
    config: str
    config_sha256: str
    instrumental_stem: str


MODELS = {
    "hyperace": Model(
        "hyperace",
        "BS-RoFormer HyperACE v2 · 기본",
        "pcunwa/BS-Roformer-HyperACE",
        "5b1f8283125d5e4a3614d0e3635a636e09c84059",
        "v2_voc/bs_roformer_voc_hyperacev2.ckpt",
        "54cf516f621f2f460bf660ed137e244b8931bf7a2ce85ddceecff816dbc4d668",
        "v2_voc/config.yaml",
        "033e57abee226a480ef410e0631b9fe00e8b7bc9a3bb5fe55659d0b43a238701",
        "instrument",
    ),
    "deux": Model(
        "deux",
        "Mel-Band RoFormer deux · 악기 보존 비교",
        "becruily/mel-band-roformer-deux",
        "402dd9a19baaed8c6f1b54458ce6ae2ae19f354e",
        "becruily_deux.ckpt",
        "10255c02295bf3e3865d4ee50ff752d7b19b124ed5fd93b147babc4333eda3aa",
        "config.yaml",
        "638cc7c67495a8c19bc6b3347ec350e203b3bfa869dc2e4d369a1ed1a3a7304d",
        "Instrumental",
    ),
}


@dataclass(frozen=True)
class Settings:
    model: str = "hyperace"
    device: str = "auto"
    low_memory: bool = False

    def signature(self) -> dict:
        # Device is deliberately excluded: results can be reused across machines.
        return {
            "pipeline": PIPELINE_VERSION,
            "model": asdict(MODELS[self.model]),
            "engine": "pymss==2.4.0;pymss-core==0.1.16",
            "sample_rate": SAMPLE_RATE,
            "batch_size": 1,
            "chunk_size": 352800 if self.low_memory else None,
            "tta": False,
            "normalize": False,
        }

    @property
    def cache_key(self) -> str:
        raw = json.dumps(self.signature(), sort_keys=True).encode()
        return f"{self.model}-{sha256(raw).hexdigest()[:16]}"


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT

    @property
    def targets(self) -> Path:
        return self.root / "targets"

    @property
    def score_targets(self) -> Path:
        return self.root / "score_targets"

    @property
    def processed(self) -> Path:
        return self.root / "processed"

    @property
    def models(self) -> Path:
        return self.root / "models"

    def create(self) -> None:
        for folder in (self.targets, self.score_targets, self.processed, self.models):
            folder.mkdir(parents=True, exist_ok=True)
