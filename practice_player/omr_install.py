# SPDX-License-Identifier: GPL-3.0-only
"""Install official OMR distributions privately, then exercise Java and English OCR.

Called only by the installer, using the project's external venv. No user score
is used for validation, and launch/playback never downloads or runs this probe.
"""

import hashlib
import json
import os
import platform
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from filelock import FileLock

from .omr_runtime import (
    MANIFEST,
    executable,
    managed_ready,
    managed_root,
    signature,
    specification,
    tools_root,
)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(item: dict, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / item["sha256"]
    if destination.is_file() and digest(destination) == item["sha256"]:
        return destination
    url = item["url"]
    if not url.startswith("https://"):
        raise ValueError("Only HTTPS downloads are allowed")
    for attempt in range(3):
        temporary = destination.with_suffix(".part")
        try:
            started, count = time.monotonic(), 0
            request = urllib.request.Request(
                url, headers={"User-Agent": "Magic-Instrumental-Installer"},
            )
            with (
                urllib.request.urlopen(request, timeout=60) as response,
                temporary.open("wb") as out,
            ):
                if not response.url.startswith("https://"):
                    raise ValueError("Insecure download redirect")
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > 256 * 1024 * 1024 or time.monotonic() - started > 600:
                        raise ValueError("OMR download exceeded size/time limit")
                    out.write(chunk)
                    if count // (10 * 1024 * 1024) != (count - len(chunk)) // (10 * 1024 * 1024):
                        print(f"  {count // (1024 * 1024)} MB", flush=True)
            if digest(temporary) != item["sha256"]:
                raise ValueError("다운로드 SHA-256이 일치하지 않습니다. 실행하지 않았습니다.")
            temporary.replace(destination)
            return destination
        except OSError:
            if attempt == 2:
                raise
        finally:
            temporary.unlink(missing_ok=True)
    raise RuntimeError("Download failed")


def extract_macos(archive: Path, destination: Path):
    # hdiutil recognizes the image by content; no extension is required.
    mounted = subprocess.run(
        ["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-plist", str(archive)],
        input=b"Y\n", check=True, capture_output=True, timeout=120,
    )
    # Official DMG prints its AGPL notice and asks to continue before the plist.
    # Preserve upstream notices in the installation; do not alter the image.
    info = plistlib.loads(mounted.stdout[mounted.stdout.index(b"<?xml"):])
    volumes = [Path(e["mount-point"]) for e in info["system-entities"] if "mount-point" in e]
    try:
        apps = [p for volume in volumes for p in volume.glob("Audiveris.app")]
        if len(apps) != 1:
            raise RuntimeError("공식 Audiveris 앱을 설치 파일에서 찾지 못했습니다.")
        shutil.copytree(apps[0], destination / "Audiveris.app", symlinks=True)
    finally:
        for volume in volumes:
            subprocess.run(["/usr/bin/hdiutil", "detach", str(volume)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)


def inventory(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if name == "verified.json":
            continue
        if path.is_symlink():
            # Retain upstream runtime links, but reject links escaping this installation.
            if root.resolve() not in path.resolve().parents:
                raise ValueError("External symlink in OMR installation")
            result[name] = "link:" + os.readlink(path)
        elif path.is_file():
            result[name] = digest(path)
    return result


def intact(root: Path) -> bool:
    if not managed_ready(root):
        return False
    try:
        return inventory(root) == json.loads((root / "verified.json").read_text())["files"]
    except (OSError, ValueError, KeyError):
        return False


def run_logged(command: list[str], environment: dict, log, timeout: int):
    process = subprocess.Popen(command, env=environment, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=os.name != "nt")
    try:
        if process.wait(timeout=timeout):
            raise RuntimeError("Audiveris 실행 검사에 실패했습니다.")
    except BaseException:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=15)
        raise


def make_probe(path: Path):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1800, 1200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((300, 80), "MAGIC PRACTICE", font=ImageFont.load_default(size=50), fill="black")
    for y in (300, 600, 900):
        for n in range(5):
            draw.line((100, y + n * 16, 1700, y + n * 16), fill="black", width=2)
        for x in (100, 500, 900, 1300, 1700):
            draw.line((x, y, x, y + 64), fill="black", width=3)
        for x in range(180, 1680, 100):
            draw.ellipse((x, y + 40, x + 18, y + 52), fill="black")
            draw.line((x + 18, y + 46, x + 18, y - 8), fill="black", width=2)
    image.save(path, dpi=(300, 300))


def probe_result(output: Path) -> bool:
    books = list(output.glob("*.omr"))
    if len(books) != 1:
        return False
    with zipfile.ZipFile(books[0]) as book:
        # The synthetic output must actually contain recognized English words,
        # not just a log saying that an eng.traineddata filename exists.
        xml = book.read("sheet#1/sheet#1.xml").decode("utf-8")
        steps = book.read("book.xml").decode("utf-8")
        return 'value="MAGIC"' in xml and 'value="PRACTICE"' in xml and "TEXTS" in steps


def verify_runtime(root: Path):
    log_path = tools_root() / "install-omr.log"
    environment = os.environ.copy()
    environment["TESSDATA_PREFIX"] = str(root / "tessdata")
    program = str(executable(root))
    with tempfile.TemporaryDirectory(prefix="verify-", dir=tools_root()) as folder:
        probe = Path(folder) / "magic-install-check.png"
        output = Path(folder) / "output"
        make_probe(probe)
        print("Audiveris / Java 실행 및 영어 문자 실제 인식 검사 중…", flush=True)
        with log_path.open("wb") as log:
            run_logged([program, "-version"], environment, log, 60)
            log.flush()
            if specification()["audiveris_version"] not in log_path.read_text(errors="replace"):
                raise RuntimeError("설치된 Audiveris 버전이 지정 버전과 다릅니다.")
            run_logged([
                program, "-batch", "-step", "TEXTS", "-save",
                "-constant", "org.audiveris.omr.text.Language.defaultSpecification=eng",
                "-output", str(output), "--", str(probe),
            ], environment, log, 240)
        if not probe_result(output):
            raise RuntimeError("영어 OCR 실제 인식 검사에 실패했습니다.")


def install():
    supported = ((sys.platform == "darwin" and platform.machine() == "arm64")
                 or (sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64"}))
    if not supported:
        raise RuntimeError("Audiveris 자동 설치는 Apple Silicon Mac / Windows x64를 지원합니다.")
    parent = tools_root()
    parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(parent / "omr-install.lock"), timeout=600):
        target = managed_root()
        if intact(target):
            verify_runtime(target)
            print("Audiveris + 영어 OCR 검사 완료 (기존 설치 재사용).", flush=True)
            return
        spec = specification()
        cache = parent / "downloads"
        kind = "macos_arm64" if sys.platform == "darwin" else "windows_x64"
        print("Audiveris: AGPL-3.0-or-later / 영어 데이터: Apache-2.0\n"
              "원본 라이선스·소스: THIRD_PARTY_NOTICES.md (설치 폴더에도 보존)", flush=True)
        print(f"공식 Audiveris {spec['audiveris_version']} 다운로드·SHA-256 검사…", flush=True)
        archive = download(spec[kind], cache)
        with tempfile.TemporaryDirectory(prefix="omr-stage-", dir=parent) as folder:
            stage = Path(folder) / "runtime"
            stage.mkdir()
            if sys.platform == "darwin":
                extract_macos(archive, stage)
            else:
                from .msi_extract import extract_msi

                extract_msi(archive, stage)
            (stage / "tessdata").mkdir()
            (stage / "licenses").mkdir()
            print("영어 OCR 데이터와 원본 라이선스 다운로드·SHA-256 검사…", flush=True)
            for key, relative in (
                ("eng", "tessdata/eng.traineddata"),
                ("audiveris_license", "licenses/Audiveris-AGPL-3.0.txt"),
                ("tessdata_license", "licenses/tessdata-Apache-2.0.txt"),
            ):
                shutil.copyfile(download(spec[key], cache), stage / relative)
            shutil.copyfile(MANIFEST, stage / "sources.json")
            verify_runtime(stage)
            marker = {"signature": signature(), "files": inventory(stage)}
            (stage / "verified.json").write_text(json.dumps(marker, indent=2), encoding="utf-8")
            # Never touch another user's/system Audiveris. Publish only after success.
            backup = Path(folder) / "previous"
            if target.exists():
                target.rename(backup)
            try:
                stage.rename(target)
            except OSError:
                if backup.exists():
                    backup.rename(target)
                raise
        print("Audiveris + 영어 OCR 자동 설치·실제 인식 검사 완료.", flush=True)


if __name__ == "__main__":
    try:
        install()
    except Exception as error:
        print(f"악보 인식 준비 실패: {error}\n설치 파일을 다시 더블클릭하세요. "
              f"검사 로그: {tools_root() / 'install-omr.log'}", file=sys.stderr)
        raise SystemExit(1)
