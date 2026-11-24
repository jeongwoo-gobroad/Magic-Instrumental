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


def separate_stems(
    source: Path, output: Path, settings: Settings, paths: Paths, report, *, offline: bool = False
) -> tuple[str, dict[str, Path]]:
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    device = select_device(settings.device)
    model = MODELS[settings.model]
    output.mkdir(parents=True, exist_ok=True)
    weights, config = ensure_model(model, paths.models, report, offline=offline)
    report(f"{device.upper()}에서 모델을 불러오는 중…", None)
    from pymss import MSSeparator

    params = {"batch_size": 1, "normalize": False, "cuda_attention_backend": "auto"}
    if model.mps_model_backend is not None:
        # The full MLX backend runs all Mega heads; torch computes only requested heads.
        params["mps_model_backend"] = model.mps_model_backend
    if settings.low_memory:
        params["chunk_size"] = 352800

    def progress(done, total, message):
        report(f"파트 분리 · {device.upper()} · {message}", done / total if total else None)

    with (
        mps_mel_band_fix(device == "mps"),
        MSSeparator(
            model_type=model.model_type,
            model_path=str(weights),
            config_path=str(config),
            device=device,
            output_format="wav",
            use_tta=False,
            store_dirs={upstream: str(output) for _canonical, upstream in model.stems},
            save_as_folder=False,
            audio_params={"wav_bit_depth": "FLOAT"},
            inference_params=params,
            progress_callback=progress,
        ) as separator,
    ):
        successes = separator.process_folder(str(source))
        if not successes:
            raise RuntimeError("분리 엔진이 결과를 생성하지 못했습니다. 로그를 확인하세요.")
    results = {}
    for canonical, upstream in model.stems:
        generated = output / f"{source.stem}_{upstream}.wav"
        if not generated.is_file():
            raise RuntimeError(f"분리 결과를 찾을 수 없습니다: {upstream}")
        target = output / f"{canonical}.wav"
        generated.replace(target)
        results[canonical] = target
    return device, results


def separate_audio(
    source: Path, output: Path, settings: Settings, paths: Paths, report, *, offline: bool = False
) -> str:
    """Compatibility entry point for callers asking for the vocal-free output."""
    import tempfile

    if not MODELS[settings.model].instrumental_stem:
        raise ValueError("이 모델의 반주는 파트 선택 파이프라인으로 준비하세요.")
    with tempfile.TemporaryDirectory(prefix=".stems-", dir=output.parent) as temporary:
        device, stems = separate_stems(
            source, Path(temporary), settings, paths, report, offline=offline,
        )
        stems["instrumental"].replace(output)
    return device
