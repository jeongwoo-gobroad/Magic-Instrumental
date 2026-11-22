# SPDX-License-Identifier: GPL-3.0-only
"""CI runner: isolate native multimedia lifetime between test modules."""

import os
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    for module in sorted((root / "tests").glob("test_*.py")):
        print(f"\nTesting {module.name} in a fresh process", flush=True)
        child_env = None
        if sys.platform == "darwin" and module.name in {
            "test_session_player.py", "test_web_player.py",
        }:
            # Preserve native browser exit diagnostics without changing test behavior.
            child_env = os.environ.copy()
            child_env["DEBUG"] = f"{child_env.get('DEBUG', '')},pw:browser".strip(",")
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    str(module),
                    "-o",
                    "faulthandler_timeout=60",
                ],
                cwd=root,
                env=child_env,
                timeout=240,
            )
        except subprocess.TimeoutExpired:
            print(f"Timed out: {module.name}. Review the faulthandler trace above.", flush=True)
            return 1
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
