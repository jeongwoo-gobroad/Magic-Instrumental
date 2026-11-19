# SPDX-License-Identifier: GPL-3.0-only
"""Standard-library-only bootstrap. Installs exclusively in an external venv."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import venv
from contextlib import nullcontext
from pathlib import Path

from app_guard import app_guard
from practice_player.omr_runtime import managed_ready

ROOT = Path(__file__).resolve().parent


def environment_path() -> Path:
    if sys.platform == "win32":
        config = ROOT / "venv-location.windows.txt"
        if not config.exists():
            shutil.copyfile(ROOT / "venv-location.windows.example.txt", config)
        lines = [
            line.strip()
            for line in config.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if len(lines) != 1:
            raise ValueError(
                "venv-location.windows.txt에 가상환경의 전체 경로를 한 줄로 지정하세요."
            )
        path = Path(os.path.expandvars(lines[0])).expanduser()
        if not path.is_absolute():
            raise ValueError("Windows 가상환경 경로는 절대 경로여야 합니다.")
    else:
        path = Path.home() / ".vocal-separator-and-speed-adj" / ".venv"
    path = path.resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError("가상환경은 프로젝트 폴더 밖에 위치해야 합니다.")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="외부 가상환경 설치 및 GUI 실행")
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--launch-only", action="store_true", help="설치 없이 준비된 환경 실행")
    parser.add_argument("--reinstall", action="store_true")
    hardware = parser.add_mutually_exclusive_group()
    hardware.add_argument("--cuda", action="store_true")
    hardware.add_argument("--cpu", action="store_true")
    args = parser.parse_args()
    if args.launch_only and (args.setup_only or args.reinstall or args.cuda or args.cpu):
        parser.error("--launch-only에는 설치 옵션을 함께 사용할 수 없습니다.")
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Python 3.12를 설치하고 실행하세요. 이 프로젝트는 3.12로 검증합니다.")
    target = environment_path()
    python = target / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    print(f"가상환경: {target}", flush=True)
    if not python.exists():
        if args.launch_only:
            raise RuntimeError("install.command 또는 install.bat을 먼저 실행하세요.")
        if target.exists() and any(target.iterdir()):
            raise RuntimeError(f"가상환경이 아닌 기존 폴더는 변경하지 않습니다: {target}")
        venv.EnvBuilder(with_pip=True).create(target)
    identity = json.loads(
        subprocess.check_output(
            [
                str(python),
                "-c",
                "import sys,json; "
                "print(json.dumps([sys.prefix,sys.base_prefix,list(sys.version_info[:2])]))",
            ],
            text=True,
        )
    )
    if (
        Path(identity[0]).resolve() != target
        or identity[0] == identity[1]
        or identity[2] != [3, 12]
    ):
        raise RuntimeError(
            "지정 경로가 Python 3.12 가상환경이 아닙니다. 다른 빈 경로를 지정하세요."
        )
    requirements = ROOT / "requirements.txt"
    lock = ROOT / (
        "requirements-windows.lock.txt"
        if sys.platform == "win32"
        else "requirements-macos.lock.txt"
    )
    # The GPU wheel is installed first. The common torch pin accepts its +cu130 local version.
    profile_file = target / ".magic-instrumental-profile"
    previous_profile = profile_file.read_text().strip() if profile_file.exists() else ""
    use_cuda = sys.platform == "win32" and (
        args.cuda
        or (
            not args.cpu
            and (
                previous_profile == "cuda"
                or (not previous_profile and bool(shutil.which("nvidia-smi")))
            )
        )
    )
    profile = "cuda" if use_cuda else "default"
    signature = hashlib.sha256(
        requirements.read_bytes()
        + lock.read_bytes()
        + (ROOT / "requirements-cuda.txt").read_bytes()
        + profile.encode()
    ).hexdigest()
    marker = target / ".practice-player-requirements"
    previous = marker.read_text() if marker.exists() else ""
    if args.launch_only and previous != signature:
        raise RuntimeError(
            "이 버전의 설치가 필요합니다. install.command 또는 install.bat을 실행하세요."
        )
    if args.launch_only and not managed_ready():
        raise RuntimeError(
            "악보 인식 도구 준비가 필요합니다. install.command 또는 install.bat을 "
            "다시 더블클릭하세요. Audiveris와 영어 데이터도 자동으로 설치합니다."
        )
    if args.reinstall or previous != signature:
        subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
        if use_cuda:
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--force-reinstall",
                    "--no-deps",
                    "-r",
                    str(ROOT / "requirements-cuda.txt"),
                ],
                check=True,
            )
        elif sys.platform == "win32":
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--force-reinstall",
                    "--no-deps",
                    "torch==2.14.1",
                    "--index-url",
                    "https://download.pytorch.org/whl/cpu",
                ],
                check=True,
            )
        subprocess.run(
            [str(python), "-m", "pip", "install", "--only-binary", ":all:", "-r", str(lock)],
            check=True,
        )
        subprocess.run([str(python), "-m", "pip", "check"], check=True)
        marker.write_text(signature)
    if not args.launch_only:
        profile_file.write_text(profile)
    # Keep FFmpeg's executable alias inside the external environment too.
    ffmpeg_path = subprocess.check_output(
        [str(python), "-c", "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
        text=True,
    ).strip()
    ffmpeg_alias = python.parent / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if not ffmpeg_alias.exists():
        if sys.platform == "win32":
            shutil.copy2(ffmpeg_path, ffmpeg_alias)
        else:
            ffmpeg_alias.symlink_to(ffmpeg_path)
    if not args.launch_only:
        subprocess.run([str(python), "-m", "practice_player.omr_install"], cwd=ROOT, check=True)
    if not args.launch_only:
        subprocess.run(
            [
                str(python),
                "-c",
                "from practice_player.gp_render import verify_runtime; verify_runtime()",
            ],
            check=True,
            cwd=ROOT,
        )
    if args.setup_only:
        print("설치 완료. 시작 스크립트로 GUI를 실행하세요.")
        return 0
    environment = os.environ.copy()
    environment["PATH"] = str(python.parent) + os.pathsep + environment.get("PATH", "")
    return subprocess.call([str(python), str(ROOT / "main.py"), "gui"], cwd=ROOT, env=environment)


if __name__ == "__main__":
    try:
        # Only the updater's setup child may run under its parent's exclusive lock.
        updating = os.environ.get("MAGIC_UPDATE_INSTALL") == "1" and "--setup-only" in sys.argv
        with nullcontext() if updating else app_guard(ROOT):
            raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"설치/실행 실패: {error}", file=sys.stderr)
        raise SystemExit(1)
