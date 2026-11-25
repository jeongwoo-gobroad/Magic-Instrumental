# SPDX-License-Identifier: GPL-3.0-only
import base64
import hashlib
import re
import shutil
import stat
import zipfile

import pytest

from practice_player import __version__
from tools.build_release import ROOT, build, public_files


def test_committed_web_version_and_integrity_match_release():
    web = ROOT / "web_player"
    template = (web / "template.html").read_text(encoding="utf-8")
    html = (web / "index.html").read_bytes()
    document = html.decode("utf-8")
    worker = (web / "sw.js").read_text(encoding="utf-8")
    marker = r'<meta name="magic-version" content="([^"]+)">'
    for name, content in (("template.html", template), ("index.html", document)):
        assert re.findall(marker, content) == [__version__], name
    assert re.findall(r'<b id="appVersion">v([^<]+)</b>', document) == [__version__]
    assert re.findall(r"^const VERSION = '([^']+)';$", worker, re.MULTILINE) == [__version__]
    identities = re.findall(r'<meta name="magic-build" content="([a-f0-9]{64})">', document)
    assert len(identities) == 1
    identity = identities[0]
    unstamped = document.replace(f'content="{identity}"', 'content="/*BUILD*/"', 1)
    assert hashlib.sha256(unstamped.encode()).hexdigest() == identity
    assert re.findall(r"^const BUILD = '([^']+)';$", worker, re.MULTILINE) == [identity]
    digest = hashlib.sha256(html).digest()
    assert f"./index.html?rev={digest.hex()[:16]}" in worker
    assert f"integrity: 'sha256-{base64.b64encode(digest).decode()}'" in worker


def test_public_bundle_excludes_media_settings_and_preserves_launch_modes(tmp_path):
    for source in public_files():
        target = tmp_path / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for relative in (
        "targets/Private Track.wav", "score_targets/Private Track.pdf", "processed/ui.ini",
        "models/private.ckpt", "venv-location.windows.txt", ".env", "docs/images/private.png",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private data", encoding="utf-8")
    with zipfile.ZipFile(build(tmp_path)) as archive:
        names = archive.namelist()
        assert not any("Private" in name or "private" in name for name in names)
        assert not any(name.endswith(("ui.ini", "windows.txt", ".env")) for name in names)
        command = next(
            info for info in archive.infolist() if info.filename.endswith("install.command")
        )
        assert (command.external_attr >> 16) & stat.S_IXUSR
        batch = next(name for name in names if name.endswith("start.bat"))
        assert b"\r\n" in archive.read(batch)
    helper = tmp_path / 'dist/Magic-Instrumental-Updater.zip'
    with zipfile.ZipFile(helper) as archive:
        assert set(name.split('/', 1)[1] for name in archive.namelist()) == {
            'update.py', 'app_guard.py', 'update.command', 'update.bat',
            'installers/update-windows.ps1', 'LICENSE', 'README.md'}
        command_info = archive.getinfo('Magic-Instrumental-Updater/update.command')
        assert command_info.external_attr >> 16 & 0o100
    checksums = (tmp_path / 'dist/SHA256SUMS.txt').read_text()
    assert hashlib.sha256(helper.read_bytes()).hexdigest() in checksums
    # Only the user's explicitly authorized public support address is exempt.
    (tmp_path / "README.md").write_text("pps.jeongwoo@gmail.com", encoding="utf-8")
    public_files(tmp_path)
    (tmp_path / "README.md").write_text("private" + "@" + "example.invalid", encoding="utf-8")
    with pytest.raises(ValueError, match="Email address"):
        public_files(tmp_path)
    # A path leak in an allowlisted source is a release error, not silently ignored.
    (tmp_path / "README.md").write_text("/" + "Users" + "/fixture/private/", encoding="utf-8")
    with pytest.raises(ValueError, match="Personal home path"):
        public_files(tmp_path)
