# SPDX-License-Identifier: GPL-3.0-only
"""Lightweight discovery of the private native OMR runtime; never downloads."""

import hashlib
import json
import os
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "installers/omr.json"
INSTALLER_REVISION = 1


def specification() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def signature() -> str:
    return hashlib.sha256(MANIFEST.read_bytes() + str(INSTALLER_REVISION).encode()).hexdigest()


def tools_root() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise RuntimeError("Windows 사용자 폴더 LOCALAPPDATA를 찾을 수 없습니다.")
        return Path(local) / "Magic-Instrumental/tools"
    return Path.home() / ".vocal-separator-and-speed-adj/tools"


def managed_root() -> Path:
    return tools_root() / f"omr-{signature()[:16]}"


def executable(root: Path) -> Path:
    if sys.platform == "win32":
        return root / "Audiveris/Audiveris.exe"
    return root / "Audiveris.app/Contents/MacOS/Audiveris"


def managed_ready(root: Path | None = None) -> bool:
    root = root or managed_root()
    try:
        marker = json.loads((root / "verified.json").read_text(encoding="utf-8"))
        return (
            marker["signature"] == signature()
            and executable(root).is_file()
            and (root / "tessdata/eng.traineddata").is_file()
            and bool(marker["files"])
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def omr_environment(program: str) -> dict[str, str]:
    environment = os.environ.copy()
    if Path(program).resolve() == executable(managed_root()).resolve():
        # Our verified runtime must use its own verified data, not another app's settings.
        environment["TESSDATA_PREFIX"] = str(managed_root() / "tessdata")
    else:
        legacy = Path.home() / ".vocal-separator-and-speed-adj/tools/tessdata"
        if legacy.is_dir():
            environment.setdefault("TESSDATA_PREFIX", str(legacy))
    return environment
