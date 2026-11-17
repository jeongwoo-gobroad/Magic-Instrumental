# SPDX-License-Identifier: GPL-3.0-only
import shutil
import stat
import zipfile

import pytest

from tools.build_release import ROOT, build, public_files


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
    # A path leak in an allowlisted source is a release error, not silently ignored.
    (tmp_path / "README.md").write_text("/" + "Users" + "/fixture/private/", encoding="utf-8")
    with pytest.raises(ValueError, match="Personal home path"):
        public_files(tmp_path)
