# SPDX-License-Identifier: GPL-3.0-only
import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_RATE = 44100
PIPELINE_VERSION = 3


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
    stems: tuple[tuple[str, str], ...] = ()
    model_type: str = "auto"
    download_base_url: str | None = None
    mps_model_backend: str | None = None


MODELS = {
    "hyperace": Model(
        "hyperace",
        "BS-RoFormer HyperACE v2 · 기존 보컬",
        "pcunwa/BS-Roformer-HyperACE",
        "5b1f8283125d5e4a3614d0e3635a636e09c84059",
        "v2_voc/bs_roformer_voc_hyperacev2.ckpt",
        "54cf516f621f2f460bf660ed137e244b8931bf7a2ce85ddceecff816dbc4d668",
        "v2_voc/config.yaml",
        "033e57abee226a480ef410e0631b9fe00e8b7bc9a3bb5fe55659d0b43a238701",
        "instrument",
        (("vocals", "vocals"), ("instrumental", "instrument")),
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
        (("vocals", "Vocals"), ("instrumental", "Instrumental")),
    ),
    "leap": Model(
        "leap",
        "BS-RoFormer Leap Xe · 보컬 권장",
        "pcunwa/BS-Roformer-Leap",
        "4e47d6662ae82eaa8b4ac4329fe66099a843b48e",
        "Xe/bs_leap_xe_voc.ckpt",
        "b739c1d2d87a81cd3dd3844ed9ad0bd678708c7a0a761a03a1aaff9af79a096d",
        "Xe/leap_xe_config_voc.yaml",
        "d3cb8c84be2e9bcbc64c1086e2256dc097ab5addebc5ce507818e90ff8cbdc25",
        "other",
        (("vocals", "vocals"), ("instrumental", "other")),
    ),
    "sw6": Model(
        "sw6",
        "BS-RoFormer SW Fixed · 기타·드럼·베이스·피아노",
        "enerjazzer/BS-ROFO-SW-Fixed",
        "a443a2985534b3bc815ef54a5d446c6a0390f974",
        "BS-Rofo-SW-Fixed.ckpt",
        "24e7d35ee9c64415673d3fd33e06a67cac2c103c5df6267ba1576459c775916e",
        "BS-Rofo-SW-Fixed.yaml",
        "f9fada9f94e5ba2d2e4600196299459294bc5f532b314c209cc156ac63e4329b",
        "",
        tuple((name, name) for name in ("bass", "drums", "other", "vocals", "guitar", "piano")),
    ),
    "mega": Model(
        "mega",
        "MVSep Mega 53 · 키보드·신시사이저",
        "ZFTurbo/Music-Source-Separation-Training",
        "v1.0.21",
        "mvsep_mega_model_bs_roformer_53_stems_v1.ckpt",
        "c62820893bbf86d4e734f966bd142d9157cfc8bb8e79e9d8f9ea553f3ff3519f",
        "mvsep_mega_model_bs_roformer_53_stems.yaml",
        "7e198062a251587088adb91215a4f44ab59e67bd62fcc805cf54d6e7dfc51103",
        "",
        (("keys", "keys"), ("synth", "synth")),
        "bs_roformer",
        "https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/download/v1.0.21",
        "torch",
    ),
}

_LEGACY_MODEL_FIELDS = (
    "key", "label", "repo", "revision", "weights", "weights_sha256", "config",
    "config_sha256", "instrumental_stem",
)
_LEGACY_LABELS = {
    "hyperace": "BS-RoFormer HyperACE v2 · 기본",
    "deux": "Mel-Band RoFormer deux · 악기 보존 비교",
}


@dataclass(frozen=True)
class Settings:
    model: str = "hyperace"
    device: str = "auto"
    low_memory: bool = False

    def signature(self) -> dict:
        # Device is deliberately excluded: results can be reused across machines.
        if self.model == "original":
            model = {"key": "original", "stems": []}
        else:
            model = asdict(MODELS[self.model])
            model.pop("label")
            for key in ("download_base_url", "mps_model_backend"):
                if model[key] is None:
                    model.pop(key)
            model["stems"] = [list(pair) for pair in MODELS[self.model].stems]
        return {
            "pipeline": PIPELINE_VERSION,
            "model": model,
            "engine": "pymss==2.4.0;pymss-core==0.1.16",
            "sample_rate": SAMPLE_RATE,
            "batch_size": 1,
            "chunk_size": 352800 if self.low_memory else None,
            "tta": False,
            "normalize": False,
            "frame_policy": "original-pcm-tail-pad-trim-4410-v1",
            "variant_recipe": "mixture-minus-target-v1",
        }

    def legacy_signature(self) -> dict | None:
        if self.model not in _LEGACY_LABELS:
            return None
        # Keep the exact v2 model shape, including its old UI label, for cache reuse.
        model = {key: getattr(MODELS[self.model], key) for key in _LEGACY_MODEL_FIELDS}
        model["label"] = _LEGACY_LABELS[self.model]
        return {
            "pipeline": 2, "model": model,
            "engine": "pymss==2.4.0;pymss-core==0.1.16", "sample_rate": SAMPLE_RATE,
            "batch_size": 1, "chunk_size": 352800 if self.low_memory else None,
            "tta": False, "normalize": False,
        }

    @property
    def legacy_cache_key(self) -> str | None:
        signature = self.legacy_signature()
        if signature is None:
            return None
        raw = json.dumps(signature, sort_keys=True).encode()
        return f"{self.model}-{sha256(raw).hexdigest()[:16]}"

    @property
    def cache_key(self) -> str:
        raw = json.dumps(self.signature(), sort_keys=True).encode()
        return f"{self.model}-{sha256(raw).hexdigest()[:16]}"


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT
    audio_cache: Path = field(init=False)
    score_cache: Path = field(init=False)
    cache_location_error: str = field(init=False, repr=False, compare=False)
    cache_location_revision: tuple | None = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        from .cache_locations import read_cache_locations

        object.__setattr__(self, "root", Path(self.root))
        audio, score, error, revision = read_cache_locations(self.root)
        object.__setattr__(self, "audio_cache", audio)
        object.__setattr__(self, "score_cache", score)
        object.__setattr__(self, "cache_location_error", error)
        object.__setattr__(self, "cache_location_revision", revision)

    def require_cache_locations(self) -> None:
        from .cache_locations import require_current_locations

        require_current_locations(self)

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
