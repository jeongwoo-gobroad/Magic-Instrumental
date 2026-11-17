# SPDX-License-Identifier: GPL-3.0-only
"""Pinned upstream artifacts, verified before loading; no runtime code download."""

import os
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

from filelock import FileLock

from .cache import hash_file
from .config import Model


def ensure_model(
    model: Model, model_root: Path, report, *, offline: bool = False
) -> tuple[Path, Path]:
    folder = model_root / model.key / model.revision
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    with FileLock(str(folder / ".download.lock"), timeout=3600):
        for relative, checksum in [
            (model.weights, model.weights_sha256),
            (model.config, model.config_sha256),
        ]:
            destination = folder / Path(relative).name
            report(f"모델 파일 확인: {destination.name}", None)
            if destination.is_file() and hash_file(destination) == checksum:
                paths.append(destination)
                continue
            if offline:
                raise RuntimeError(
                    f"오프라인 모드: 모델 파일이 없거나 손상되었습니다: {destination}"
                )
            url = f"https://huggingface.co/{model.repo}/resolve/{model.revision}/{relative}"
            temporary = folder / f".{uuid.uuid4().hex}.download"
            try:
                request = Request(url, headers={"User-Agent": "InstrumentalPractice/0.1"})
                with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
                    total = int(response.headers.get("Content-Length", "0"))
                    done = 0
                    while block := response.read(1024 * 1024):
                        output.write(block)
                        done += len(block)
                        report(
                            f"모델 다운로드: {done / 1024**2:.0f} MB",
                            done / total if total else None,
                        )
                    output.flush()
                    os.fsync(output.fileno())
                if hash_file(temporary) != checksum:
                    raise RuntimeError("모델 SHA-256 검증에 실패했습니다. 다시 다운로드하세요.")
                temporary.replace(destination)
                paths.append(destination)
            finally:
                temporary.unlink(missing_ok=True)
    return paths[0], paths[1]
