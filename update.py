# SPDX-License-Identifier: GPL-3.0-only
"""Explicit, source-only updates from this project's public GitHub releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

from app_guard import app_guard

REPO = "jeongwoo-gobroad/Magic-Instrumental"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES = f"https://github.com/{REPO}/releases/download/"
MAX_ARCHIVE = 80 * 1024 * 1024
MAX_EXPANDED = 160 * 1024 * 1024
DATA_DIRS = {"targets", "score_targets", "processed", "models"}
ROOT_FILES = {
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "AI_CODER_README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "README.en.md",
    "THIRD_PARTY_NOTICES.md",
    "bootstrap.py",
    "main.py",
    "app_guard.py",
    "update.py",
    "pyproject.toml",
    "install.command",
    "install.bat",
    "start.command",
    "start.bat",
    "update.command",
    "update.bat",
    "venv-location.windows.example.txt",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-cuda.txt",
    "requirements-macos.lock.txt",
    "requirements-windows.lock.txt",
}


def version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("올바르지 않은 버전 정보입니다.")
    return tuple(map(int, value.split(".")))


def installed_version(root: Path) -> str:
    content = (root / "practice_player/__init__.py").read_text(encoding="utf-8")
    found = re.search(r'^__version__ = "(\d+\.\d+\.\d+)"', content, re.M)
    if not found:
        raise ValueError("프로그램 버전을 확인할 수 없습니다. 기존 설치 폴더를 선택하세요.")
    return found[1]


def public_path(name: str) -> bool:
    """Defense in depth: release metadata cannot authorize writes to private data."""
    p = PurePosixPath(name)
    if (
        not name
        or p.is_absolute()
        or p.as_posix() != name
        or any(c in name for c in ("\\", ":", "\x00"))
        or any(part in {".", ".."} or part.endswith((" ", ".")) for part in p.parts)
    ):
        return False
    if p.parts[0] in DATA_DIRS:
        return name == f"{p.parts[0]}/.gitkeep"
    if len(p.parts) == 1:
        return name in ROOT_FILES
    if len(p.parts) == 2:
        directory, filename = p.parts
        return (
            directory in {"practice_player", "tests", "tools"}
            and p.suffix == ".py"
            or directory == "docs"
            and p.suffix == ".md"
            or directory == "installers"
            and filename
            in {"runtime.json", "omr.json", "macos.sh", "windows.ps1", "update-windows.ps1"}
            or directory == "relay"
            and filename
            in {
                "__init__.py",
                "server.py",
                "requirements.txt",
                "duckdns_update.py",
                "magic-duckdns.service",
                "magic-duckdns.timer",
            }
            or directory == "landing"
            and filename == "index.html"
            or directory == "web_player"
            and filename
            in {
                "README.md",
                "index.html",
                "template.html",
                "app.js",
                "archive.js",
                "ensemble.js",
                "style.css",
                "sw.js",
                "manifest.webmanifest",
                "icon-192.png",
                "icon-512.png",
            }
        )
    return (
        len(p.parts) == 3
        and p.parts[:2] == ("docs", "images")
        and p.suffix == ".png"
        or name == ".github/workflows/checks.yml"
        or name == "practice_player/assets/check.svg"
        or name
        in {
            "web_player/vendor/SignalsmithStretch.js",
            "web_player/vendor/Signalsmith-LICENSE.txt",
            "web_player/vendor/provenance.json",
        }
    )


def safe_target(root: Path, name: str) -> Path:
    target = root / name
    # Reject symlinks and Windows junctions in every component before reading/writing.
    for component in (target, *target.parents):
        if component == root:
            break
        if component.is_symlink() or component.is_junction():
            raise ValueError(f"링크된 경로는 업데이트하지 않습니다: {name}")
    if root not in target.resolve().parents:
        raise ValueError("설치 폴더 밖의 경로입니다.")
    return target


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise ValueError("HTTPS가 아닌 다운로드 이동은 허용하지 않습니다.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, target: Path, limit: int) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("HTTPS 다운로드만 지원합니다.")
    request = urllib.request.Request(url, headers={"User-Agent": "Magic-Instrumental-Updater"})
    started, total = time.monotonic(), 0
    with urllib.request.build_opener(HTTPSOnly).open(request, timeout=30) as response:
        with target.open("wb") as output:
            while chunk := response.read(128 * 1024):
                total += len(chunk)
                if total > limit or time.monotonic() - started > 300:
                    raise ValueError("다운로드가 크기 또는 시간 제한을 초과했습니다.")
                output.write(chunk)
    return target.read_bytes()


def fetch_release(work: Path) -> tuple[str, Path, dict]:
    release = json.loads(download(API, work / "release.json", 2 * 1024 * 1024))
    tag = release["tag_name"]
    if release.get("draft") or release.get("prerelease") or not tag.startswith("v"):
        raise ValueError("정식 버전을 확인할 수 없습니다.")
    latest = tag[1:]
    version(latest)
    assets = {a["name"]: a for a in release["assets"]}
    name = f"Magic-Instrumental-{latest}.zip"
    data = {}
    for asset_name, limit in [
        (name, MAX_ARCHIVE),
        ("SHA256SUMS.txt", 16384),
        ("PUBLIC_FILES.json", 2 * 1024 * 1024),
    ]:
        asset = assets[asset_name]
        url = RELEASES + tag + "/" + asset_name
        if asset["browser_download_url"] != url:
            raise ValueError("공식 배포 파일 주소가 아닙니다.")
        print(f"받는 중: {asset_name}", flush=True)
        data[asset_name] = download(url, work / asset_name, limit)
        digest = asset.get("digest")
        if digest and digest != "sha256:" + hashlib.sha256(data[asset_name]).hexdigest():
            raise ValueError("다운로드 검증에 실패했습니다. 기존 파일은 유지됩니다.")
    checksum_lines = data["SHA256SUMS.txt"].decode().splitlines()
    checksums = {n.lstrip("*"): d for d, n in (line.split(maxsplit=1) for line in checksum_lines)}
    if checksums.get(name) != hashlib.sha256(data[name]).hexdigest():
        raise ValueError("ZIP 검증에 실패했습니다. 기존 파일은 유지됩니다.")
    manifest = json.loads(data["PUBLIC_FILES.json"])
    if manifest.get("version") != latest:
        raise ValueError("파일 목록과 버전이 다릅니다.")
    return latest, work / name, manifest


def stage_release(archive: Path, manifest: dict, stage: Path) -> list[str]:
    latest = manifest["version"]
    version(latest)
    prefix = f"Magic-Instrumental-{latest}/"
    inventory = {}
    for record in manifest["files"]:
        name, digest = record["path"], record["sha256"]
        if (
            not public_path(name)
            or name.casefold() in inventory
            or not re.fullmatch("[a-f0-9]{64}", digest)
        ):
            raise ValueError(f"배포 파일 목록이 올바르지 않습니다: {name}")
        inventory[name.casefold()] = (name, digest)
    if not {"main.py", "bootstrap.py", "practice_player/__init__.py"} <= {
        n for n, _ in inventory.values()
    }:
        raise ValueError("프로그램 필수 파일이 없습니다.")
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        if len(infos) != len(inventory) or sum(i.file_size for i in infos) > MAX_EXPANDED:
            raise ValueError("ZIP 파일 목록 또는 크기가 올바르지 않습니다.")
        seen, names = set(), []
        for info in infos:
            if not info.filename.startswith(prefix):
                raise ValueError("ZIP 최상위 폴더가 올바르지 않습니다.")
            name = info.filename[len(prefix) :]
            entry = inventory.get(name.casefold())
            kind = stat.S_IFMT(info.external_attr >> 16)
            if (
                not entry
                or entry[0] != name
                or name.casefold() in seen
                or info.is_dir()
                or kind not in {0, stat.S_IFREG}
                or info.flag_bits & 1
            ):
                raise ValueError("ZIP에 허용하지 않은 파일이 있습니다.")
            seen.add(name.casefold())
            content = bundle.read(info)
            if hashlib.sha256(content).hexdigest() != entry[1]:
                raise ValueError(f"파일 검증 실패: {name}")
            # Never even open user-data folders, including their placeholders.
            if PurePosixPath(name).parts[0] in DATA_DIRS:
                if content:
                    raise ValueError("자료 폴더에는 빈 표시 파일만 허용됩니다.")
                continue
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o755 if target.suffix in {".command", ".sh"} else 0o644)
            names.append(name)
    if installed_version(stage) != latest:
        raise ValueError("프로그램 내용과 배포 버전이 다릅니다.")
    return names


def atomic_copy(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".magic-copy-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream, source.open("rb") as original:
            shutil.copyfileobj(original, stream)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copystat(source, temporary)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_journal(root: Path, journal: dict):
    pending = root / ".magic-updates/pending.json"
    temporary = pending.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(journal, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, pending)


def setup(root: Path):
    environment = os.environ.copy()
    environment.update(MAGIC_NO_PAUSE="1", MAGIC_UPDATE_INSTALL="1", PYTHONUTF8="1")
    command = (
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "installers/windows.ps1"),
            "-Mode",
            "Install",
        ]
        if sys.platform == "win32"
        else ["/bin/zsh", str(root / "installers/macos.sh"), "install"]
    )
    subprocess.run(command, cwd=root, env=environment, check=True)


def restore_pending(root: Path, installer=setup):
    pending = safe_target(root, ".magic-updates/pending.json")
    if not pending.exists():
        return
    journal = json.loads(pending.read_text(encoding="utf-8"))
    if not re.fullmatch("[a-f0-9]{32}", journal["transaction"]):
        raise ValueError("업데이트 복구 정보가 올바르지 않습니다.")
    records = journal["files"]
    for record in records:
        name = record["path"]
        if not public_path(name) or PurePosixPath(name).parts[0] in DATA_DIRS:
            raise ValueError("복구 대상이 프로그램 파일이 아닙니다.")
        safe_target(root, name)
        if record["existed"]:
            backup = safe_target(root, f".magic-updates/{journal['transaction']}/backup/{name}")
            if hashlib.sha256(backup.read_bytes()).hexdigest() != record["sha256"]:
                raise ValueError("복구용 프로그램 백업이 손상되었습니다.")
    print("이전 프로그램 파일을 복구합니다. 개인 자료는 그대로 유지합니다.", flush=True)
    for record in records:
        name = record["path"]
        target = safe_target(root, name)
        if record["existed"]:
            atomic_copy(root / ".magic-updates" / journal["transaction"] / "backup" / name, target)
        else:
            target.unlink(missing_ok=True)
    if journal["setup_started"]:
        print("이전 버전의 실행 환경을 확인합니다.", flush=True)
        installer(root)
    pending.unlink()


def apply_release(root: Path, stage: Path, names: list[str], installer=setup):
    transaction = uuid.uuid4().hex
    backup = root / ".magic-updates" / transaction / "backup"
    records = []
    # Finish all backups before the first program-file change.
    for name in names:
        if not public_path(name) or PurePosixPath(name).parts[0] in DATA_DIRS:
            raise ValueError("프로그램 파일만 교체할 수 있습니다.")
        target = safe_target(root, name)
        existed = target.exists()
        if existed and not target.is_file():
            raise ValueError(f"프로그램 파일 위치에 폴더가 있습니다: {name}")
        record = {"path": name, "existed": existed}
        if existed:
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
            record["sha256"] = hashlib.sha256(saved.read_bytes()).hexdigest()
        records.append(record)
    journal = {"transaction": transaction, "files": records, "setup_started": False}
    save_journal(root, journal)
    try:
        for name in names:
            atomic_copy(stage / name, safe_target(root, name))
        journal["setup_started"] = True
        save_journal(root, journal)
        print("실행에 필요한 도구를 확인합니다. 창을 닫지 마세요.", flush=True)
        installer(root)
    except BaseException:
        # Durable journal also handles a power loss: next update restores before fetching.
        restore_pending(root, installer)
        raise
    (root / ".magic-updates/pending.json").unlink()


def check_legacy_processes(root: Path):
    """Older versions do not take the new lock; inspect Python entry points as well."""
    if sys.platform == "win32":
        command = [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
            "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' } | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
        ]
        raw = subprocess.check_output(command, text=True, encoding="utf-8", errors="replace")
        data = json.loads(raw) if raw.strip() else []
        processes = data if isinstance(data, list) else [data]
        lines = [(int(p["ProcessId"]), p.get("CommandLine") or "") for p in processes]
    else:
        raw = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
        lines = [
            (int(p[0]), p[1])
            for line in raw.splitlines()
            if len(p := line.strip().split(None, 1)) == 2
        ]
    for pid, command in lines:
        normalized = command.replace("\\", "/").casefold()
        if pid == os.getpid() or "python" not in normalized:
            continue
        if re.search(r'(?:^|[ /"\x27])(?:main|bootstrap)\.py(?:[ "\x27]|$)', normalized):
            # Bare relative entry points cannot reliably reveal their working directory.
            if str(root).replace("\\", "/").casefold() in normalized or not re.search(
                r"[/\\](?:main|bootstrap)\.py", command
            ):
                raise RuntimeError("기존 프로그램과 설치 창을 닫은 뒤 업데이트를 다시 실행하세요.")


@contextmanager
def update_lock(root: Path, wait_seconds=0):
    """The GUI's launcher may start before both GUI and bootstrap have exited."""
    deadline = time.monotonic() + wait_seconds
    announced = False
    while True:
        stack = ExitStack()
        try:
            check_legacy_processes(root)
            stack.enter_context(app_guard(root, updating=True))
        except RuntimeError:
            stack.close()
            if time.monotonic() >= deadline:
                raise
            if not announced:
                print(
                    "프로그램이 닫히기를 기다립니다. 열려 있는 프로그램·설치 창을 닫아 주세요.",
                    flush=True,
                )
                announced = True
            time.sleep(0.25)
            continue
        with stack:
            yield
        return


def update(root: Path, *, wait_seconds=0):
    root = root.resolve()
    if (root / ".git").exists():
        raise ValueError("개발용 Git 폴더입니다. 일반 ZIP 설치 폴더에서 업데이트하세요.")
    for name in ("main.py", "bootstrap.py", "practice_player/__init__.py"):
        if not safe_target(root, name).is_file():
            raise ValueError("기존 Magic-Instrumental 폴더를 선택하세요.")
    if (
        sys.prefix == sys.base_prefix
        or root == Path(sys.prefix).resolve()
        or root in Path(sys.prefix).resolve().parents
    ):
        raise ValueError("기존 프로그램의 외부 가상환경으로 실행해야 합니다.")
    with update_lock(root, wait_seconds):
        work_root = safe_target(root, ".magic-updates")
        work_root.mkdir(exist_ok=True)
        restore_pending(root)
        current = installed_version(root)
        print(f"현재 버전: {current}\n음원·악보·처리 결과·모델·설정은 유지됩니다.", flush=True)
        with tempfile.TemporaryDirectory(prefix="download-", dir=work_root) as temporary:
            work = Path(temporary)
            latest, archive, manifest = fetch_release(work)
            if version(latest) <= version(current):
                print("이미 최신 버전입니다. 변경하지 않았습니다.", flush=True)
                return
            if shutil.disk_usage(root).free < MAX_EXPANDED * 3:
                raise ValueError("업데이트에 약 500 MB의 여유 공간이 필요합니다.")
            stage = work / "stage"
            names = stage_release(archive, manifest, stage)
            check_legacy_processes(root)
            print(f"{current} → {latest} 업데이트 중입니다.", flush=True)
            apply_release(root, stage, names)
        print("업데이트 완료! 기존 폴더의 start 파일로 실행하세요.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--pause", action="store_true")
    parser.add_argument("--wait-for-app", action="store_true")
    args = parser.parse_args()
    result = 0
    try:
        update(args.project, wait_seconds=60 if args.wait_for_app else 0)
    except (Exception, KeyboardInterrupt) as error:
        print(f"업데이트를 완료하지 못했습니다: {error}", file=sys.stderr, flush=True)
        print(
            "개인 자료는 삭제하지 않았습니다. 창을 닫고 update 파일을 다시 실행하세요.",
            file=sys.stderr,
            flush=True,
        )
        result = 1
    if args.pause and os.environ.get("MAGIC_NO_PAUSE") != "1" and sys.stdin.isatty():
        try:
            input("Enter 키를 누르면 창이 닫힙니다. ")
        except (EOFError, KeyboardInterrupt):
            pass
    return result


if __name__ == "__main__":
    raise SystemExit(main())
