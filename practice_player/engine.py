# SPDX-License-Identifier: GPL-3.0-only
"""Expensive imports/inference stay in the worker process, never the Qt UI."""

import os
from pathlib import Path

from .compat import mps_mel_band_fix
from .config import MODELS, Paths, Settings
from .models import ensure_model


def select_device(requested: str) -> str:
    import torch

    available = {
        "cpu": True,
        "cuda": torch.cuda.is_available(),
        "mps": torch.backends.mps.is_available(),
    }
    if requested == "auto":
        return next(device for device in ("cuda", "mps", "cpu") if available[device])
    if requested not in available or not available[requested]:
        raise RuntimeError(
            f"{requested} 장치를 사용할 수 없습니다. doctor 명령으로 설치를 확인하세요."
        )
    return requested


def separate_audio(
    source: Path, output: Path, settings: Settings, paths: Paths, report, *, offline: bool = False
) -> str:
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    device = select_device(settings.device)
    model = MODELS[settings.model]
    weights, config = ensure_model(model, paths.models, report, offline=offline)
    report(f"{device.upper()}에서 모델을 불러오는 중…", None)
    from pymss import MSSeparator

    params = {"batch_size": 1, "normalize": False, "cuda_attention_backend": "auto"}
    if settings.low_memory:
        params["chunk_size"] = 352800

    def progress(done, total, message):
        report(f"보컬 분리 · {device.upper()} · {message}", done / total if total else None)

    with (
        mps_mel_band_fix(device == "mps"),
        MSSeparator(
            model_type="auto",
            model_path=str(weights),
            config_path=str(config),
            device=device,
            output_format="wav",
            use_tta=False,
            store_dirs={model.instrumental_stem: str(output.parent)},
            save_as_folder=False,
            audio_params={"wav_bit_depth": "FLOAT"},
            inference_params=params,
            progress_callback=progress,
        ) as separator,
    ):
        successes = separator.process_folder(str(source))
        if not successes:
            raise RuntimeError("분리 엔진이 결과를 생성하지 못했습니다. 로그를 확인하세요.")
    generated = output.parent / f"{source.stem}_{model.instrumental_stem}.wav"
    if not generated.is_file():
        raise RuntimeError(f"인스트루먼탈 결과를 찾을 수 없습니다: {generated.name}")
    generated.replace(output)
    return device
