# SPDX-License-Identifier: GPL-3.0-only
"""Desktop entry point; CLI commands work without importing Qt."""

import sys
from pathlib import Path

from app_guard import app_guard

if __name__ == "__main__":
    project = Path(__file__).resolve().parent
    environment = Path(sys.prefix).resolve()
    if sys.prefix == sys.base_prefix or environment == project or project in environment.parents:
        print("외부 가상환경이 필요합니다. start.command 또는 start.bat을 실행하세요.")
        raise SystemExit(1)
    try:
        with app_guard(project):
            from practice_player.cli import main
            raise SystemExit(main())
    except RuntimeError as error:
        print(error)
        raise SystemExit(1)
