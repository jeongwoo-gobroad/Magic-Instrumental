# SPDX-License-Identifier: GPL-3.0-only
"""Build a source-only release from an explicit allowlist; never collect user media."""

import argparse
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = (
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
    "update.command",
    "update.bat",
    "pyproject.toml",
    "install.command",
    "install.bat",
    "start.command",
    "start.bat",
    "venv-location.windows.example.txt",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-cuda.txt",
    "requirements-macos.lock.txt",
    "requirements-windows.lock.txt",
    "installers/runtime.json",
    "installers/omr.json",
    "installers/macos.sh",
    "installers/windows.ps1",
    "installers/update-windows.ps1",
    ".github/workflows/checks.yml",
)
PATTERNS = ("practice_player/*.py", "tests/*.py", "docs/*.md", "tools/*.py")
WEB_FILES = (
    "web_player/README.md",
    "web_player/index.html",
    "web_player/template.html",
    "web_player/app.js",
    "web_player/archive.js",
    "web_player/ensemble.js",
    "web_player/style.css",
    "web_player/sw.js",
    "web_player/manifest.webmanifest",
    "web_player/vendor/SignalsmithStretch.js",
    "web_player/vendor/Signalsmith-LICENSE.txt",
    "web_player/vendor/provenance.json",
    "practice_player/assets/check.svg",
    "relay/__init__.py",
    "relay/server.py",
    "relay/requirements.txt",
    "relay/duckdns_update.py",
    "relay/magic-duckdns.service",
    "relay/magic-duckdns.timer",
    "landing/index.html",
)
PLACEHOLDERS = tuple(
    f"{folder}/.gitkeep"
    for folder in (
        "targets",
        "score_targets",
        "processed",
        "models",
    )
)
IMAGES = (
    "docs/images/player-overview.png",
    "docs/images/score-zoom.png",
    "docs/images/score-highlight.png",
    "docs/images/drum-count-in.png",
    "docs/images/measure-loop.png",
    "docs/images/desktop-light.png",
    "docs/images/update-notice.png",
    "docs/images/desktop-dark.png",
    "docs/images/mobile-player.png",
    "docs/images/session-leader.png",
    "docs/images/session-member.png",
    "web_player/icon-192.png",
    "web_player/icon-512.png",
)


def public_files(root: Path = ROOT) -> list[Path]:
    paths = {root / name for name in (*ROOT_FILES, *PLACEHOLDERS, *IMAGES, *WEB_FILES)}
    for pattern in PATTERNS:
        paths.update(root.glob(pattern))
    for path in sorted(paths):
        if not path.is_file() or path.is_symlink() or root.resolve() not in path.resolve().parents:
            raise ValueError(f"Missing, linked or external public file: {path.relative_to(root)}")
        if path.relative_to(root).as_posix() in IMAGES:
            if not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError(f"Expected PNG screenshot: {path.relative_to(root)}")
            continue
        content = path.read_text(encoding="utf-8")
        if re.search(r"(?:/Users/|/home/)[A-Za-z0-9_.-]+/", content):
            raise ValueError(f"Personal home path in {path.relative_to(root)}")
        if re.search(r"[A-Za-z]:\\Users\\[^\\\s]+\\", content):
            raise ValueError(f"Personal Windows path in {path.relative_to(root)}")
        if re.search(r"(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}", content):
            raise ValueError(f"Possible credential in {path.relative_to(root)}")
        # Explicitly authorized public bug-report address; keep all other email checks.
        emails = re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", content)
        if any(address != "pps.jeongwoo@gmail.com" for address in emails):
            raise ValueError(f"Email address in {path.relative_to(root)}")
    return sorted(paths)


def build(root: Path = ROOT) -> Path:
    files = public_files(root)
    version_text = (root / "practice_player/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__ = "(\d+\.\d+\.\d+)"', version_text)
    if not match:
        raise ValueError("Expected a three-component numeric app version")
    version = match[1]
    output = root / "dist"
    output.mkdir(exist_ok=True)
    prefix = f"Magic-Instrumental-{version}"
    manifest = []
    archive = output / f"{prefix}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            # Ensure batch/PowerShell files work when a source ZIP is extracted directly.
            if path.suffix in {".bat", ".ps1"}:
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            info = zipfile.ZipInfo(f"{prefix}/{relative}", date_time=(2026, 11, 18, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if path.suffix in {".command", ".sh"} else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
            manifest.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest()})
    (output / "PUBLIC_FILES.json").write_text(
        json.dumps({"version": version, "files": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )
    helper = output / "Magic-Instrumental-Updater.zip"
    helper_files = ("update.py", "app_guard.py", "update.command", "update.bat",
                    "installers/update-windows.ps1", "LICENSE", "docs/UPDATING.md")
    with zipfile.ZipFile(helper, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for relative in helper_files:
            data = (root / relative).read_bytes()
            if Path(relative).suffix in {".bat", ".ps1"}:
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            name = "README.md" if relative == "docs/UPDATING.md" else relative
            info = zipfile.ZipInfo(f"Magic-Instrumental-Updater/{name}",
                                   date_time=(2026, 11, 18, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if relative.endswith(".command") else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
    (output / "SHA256SUMS.txt").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in (archive, helper)
    ), encoding="utf-8")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        for file in public_files():
            print(file.relative_to(ROOT).as_posix())
    else:
        print(build().relative_to(ROOT))
