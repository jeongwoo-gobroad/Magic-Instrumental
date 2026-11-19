# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile

import pytest

import update
from app_guard import app_guard
from tools.build_release import ROOT, public_files


def release(tmp_path, extra=None):
    contents = {
        "main.py": b"new main\n",
        "bootstrap.py": b"new setup\n",
        "practice_player/__init__.py": b'__version__ = "2.2.0"\n',
        "update.command": b"#!/bin/zsh\n",
        "targets/.gitkeep": b"",
    }
    contents.update(extra or {})
    archive = tmp_path / "release.zip"
    manifest = {"version": "2.2.0", "files": []}
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, data in contents.items():
            info = zipfile.ZipInfo(f"Magic-Instrumental-2.2.0/{name}")
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            bundle.writestr(info, data)
            manifest["files"].append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
    return archive, manifest


def old_install(root):
    root.mkdir()
    (root / "practice_player").mkdir()
    (root / ".magic-updates").mkdir()
    (root / "main.py").write_bytes(b"old main\n")
    (root / "bootstrap.py").write_bytes(b"old setup\n")
    (root / "practice_player/__init__.py").write_bytes(b'__version__ = "2.1.0"\n')
    for name in [
        "targets/Original.wav",
        "score_targets/Score.gp5",
        "processed/reviewed.json",
        "models/model.bin",
        "venv-location.windows.txt",
        "custom.magic",
        "my-notes.txt",
    ]:
        target = root / name
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_bytes(("private: " + name).encode())
    return snapshot(root)


def snapshot(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".magic-updates" not in p.parts and p.name != ".magic-update.lock"
    }


def test_release_allowlist_and_updater_policy_agree():
    assert all(update.public_path(p.relative_to(ROOT).as_posix()) for p in public_files())


def test_update_preserves_every_private_file_and_installs_in_same_root(tmp_path):
    root = tmp_path / "Existing install 한글"
    before = old_install(root)
    archive, manifest = release(tmp_path)
    stage = tmp_path / "stage"
    names = update.stage_release(archive, manifest, stage)
    installed = []
    update.apply_release(root, stage, names, installed.append)
    assert installed == [root]
    assert update.installed_version(root) == "2.2.0"
    for name, content in before.items():
        if name not in names:
            assert (root / name).read_bytes() == content
    assert not (root / "targets/.gitkeep").exists()
    assert not (root / ".magic-updates/pending.json").exists()
    assert next((root / ".magic-updates").glob("*/backup/main.py")).read_bytes() == b"old main\n"
    if os.name != "nt":
        assert (root / "update.command").stat().st_mode & stat.S_IXUSR


def test_install_failure_restores_code_and_prior_dependencies(tmp_path):
    root = tmp_path / "existing"
    before = old_install(root)
    archive, manifest = release(tmp_path)
    stage = tmp_path / "stage"
    names = update.stage_release(archive, manifest, stage)
    seen = []

    def installer(path):
        seen.append(update.installed_version(path))
        if len(seen) == 1:
            raise RuntimeError("network failure")

    with pytest.raises(RuntimeError, match="network failure"):
        update.apply_release(root, stage, names, installer)
    assert seen == ["2.2.0", "2.1.0"]
    assert snapshot(root) == before
    assert not (root / ".magic-updates/pending.json").exists()


def test_interrupted_copy_and_failed_repair_can_be_retried(tmp_path, monkeypatch):
    root = tmp_path / "existing"
    before = old_install(root)
    archive, manifest = release(tmp_path)
    stage = tmp_path / "stage"
    names = update.stage_release(archive, manifest, stage)
    # Simulate a process death: rollback itself cannot run in this process.
    original = update.restore_pending
    monkeypatch.setattr(update, "restore_pending", lambda *_: None)
    with pytest.raises(RuntimeError):
        update.apply_release(
            root, stage, names, lambda _: (_ for _ in ()).throw(RuntimeError("power loss"))
        )
    monkeypatch.setattr(update, "restore_pending", original)
    assert (root / ".magic-updates/pending.json").exists()
    with pytest.raises(RuntimeError, match="offline"):
        update.restore_pending(root, lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    assert snapshot(root) == before
    assert (root / ".magic-updates/pending.json").exists()
    update.restore_pending(root, lambda _: None)
    assert snapshot(root) == before
    assert not (root / ".magic-updates/pending.json").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../outside.py",
        "/absolute.py",
        "targets/song.wav",
        "processed/reviewed.json",
        "venv-location.windows.txt",
        ".env",
        ".git/config",
        "practice_player/../targets/song.wav",
        "practice_player/file.py:stream",
        "practice_player\\file.py",
        "docs/name.md.",
    ],
)
def test_unsafe_release_names_rejected_before_install(tmp_path, name):
    archive, manifest = release(tmp_path, {name: b"bad"})
    with pytest.raises(ValueError):
        update.stage_release(archive, manifest, tmp_path / "stage")


def test_corrupt_archive_member_and_links_rejected(tmp_path):
    archive, manifest = release(tmp_path)
    manifest["files"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="검증"):
        update.stage_release(archive, manifest, tmp_path / "stage")
    archive, manifest = release(tmp_path)
    # Store a link at an otherwise allowed name.
    with zipfile.ZipFile(archive, "a") as bundle:
        info = zipfile.ZipInfo("Magic-Instrumental-2.2.0/tools/link.py")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(info, b"outside")
        manifest["files"].append(
            {"path": "tools/link.py", "sha256": hashlib.sha256(b"outside").hexdigest()}
        )
    with pytest.raises(ValueError, match="허용하지"):
        update.stage_release(archive, manifest, tmp_path / "stage2")


def test_linked_install_directory_never_written(tmp_path):
    root = tmp_path / "existing"
    old_install(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.rmtree(root / "practice_player")
    try:
        (root / "practice_player").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating symlinks for this test account")
    with pytest.raises(ValueError, match="링크"):
        update.safe_target(root, "practice_player/__init__.py")
    assert not list(outside.iterdir())


def test_file_write_failure_restores_all_program_files(tmp_path, monkeypatch):
    root = tmp_path / "existing"
    before = old_install(root)
    archive, manifest = release(tmp_path)
    stage = tmp_path / "stage"
    names = update.stage_release(archive, manifest, stage)
    original = update.atomic_copy
    counter = 0

    def copy(source, target):
        nonlocal counter
        counter += 1
        if counter == 2:
            raise PermissionError("locked file")
        original(source, target)

    monkeypatch.setattr(update, "atomic_copy", copy)
    with pytest.raises(PermissionError):
        update.apply_release(root, stage, names, lambda _: pytest.fail("unexpected setup"))
    assert snapshot(root) == before


def test_os_lock_excludes_updater_and_allows_multiple_app_processes(tmp_path):
    code = (
        "from pathlib import Path; from app_guard import app_guard; "
        'import sys;\nwith app_guard(Path(sys.argv[1]), updating=sys.argv[2]=="yes"): '
        'print("acquired")'
    )

    def child(exclusive):
        return subprocess.run(
            [sys.executable, "-c", code, str(tmp_path), exclusive],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )

    with app_guard(tmp_path):
        assert child("no").returncode == 0
        assert child("yes").returncode != 0
    with app_guard(tmp_path, updating=True):
        assert child("no").returncode != 0
        assert child("yes").returncode != 0
    assert child("yes").returncode == 0


def test_pending_update_blocks_normal_launch(tmp_path):
    (tmp_path / ".magic-updates").mkdir()
    (tmp_path / ".magic-updates/pending.json").write_text("{}")
    with pytest.raises(RuntimeError, match="중단된"):
        with app_guard(tmp_path):
            pytest.fail("launched partial version")


def test_release_download_checks_full_checksum_before_staging(tmp_path, monkeypatch):
    archive, manifest = release(tmp_path)
    name = "Magic-Instrumental-2.2.0.zip"
    assets = [name, "SHA256SUMS.txt", "PUBLIC_FILES.json"]
    release_data = {
        "tag_name": "v2.2.0",
        "assets": [
            {"name": n, "browser_download_url": update.RELEASES + "v2.2.0/" + n} for n in assets
        ],
    }
    content = {
        update.API: json.dumps(release_data).encode(),
        name: archive.read_bytes(),
        "PUBLIC_FILES.json": json.dumps(manifest).encode(),
        "SHA256SUMS.txt": ("0" * 64 + "  " + name + "\n").encode(),
    }

    def download(url, path, limit):
        data = content[url if url == update.API else url.rsplit("/", 1)[-1]]
        path.write_bytes(data)
        return data

    monkeypatch.setattr(update, "download", download)
    with pytest.raises(ValueError, match="ZIP 검증"):
        update.fetch_release(tmp_path)


@pytest.mark.parametrize("current", ["2.2.0", "9.0.0"])
def test_same_or_older_release_is_a_noop(tmp_path, monkeypatch, current):
    root = tmp_path / "existing"
    old_install(root)
    (root / "practice_player/__init__.py").write_text(f'__version__ = "{current}"\n')
    before = snapshot(root)
    monkeypatch.setattr(update, "check_legacy_processes", lambda _: None)
    monkeypatch.setattr(update, "fetch_release", lambda _: ("2.2.0", None, {}))
    monkeypatch.setattr(update, "apply_release", lambda *_: pytest.fail("unneeded update"))
    update.update(root)
    assert snapshot(root) == before


def test_legacy_process_prevents_update(monkeypatch, tmp_path):
    monkeypatch.setattr(update.sys, "platform", "darwin")
    monkeypatch.setattr(
        update.subprocess,
        "check_output",
        lambda *_a, **_kw: f'12345 /external/python "{tmp_path}/main.py" gui\n',
    )
    with pytest.raises(RuntimeError, match="기존 프로그램"):
        update.check_legacy_processes(tmp_path)


def test_full_update_orchestration_keeps_existing_paths(tmp_path, monkeypatch):
    root = tmp_path / "existing"
    before = old_install(root)
    archive, manifest = release(tmp_path)
    monkeypatch.setattr(update, "check_legacy_processes", lambda _: None)
    monkeypatch.setattr(update, "fetch_release", lambda _: ("2.2.0", archive, manifest))
    apply = update.apply_release
    monkeypatch.setattr(
        update,
        "apply_release",
        lambda root, stage, names: apply(root, stage, names, lambda _: None),
    )
    update.update(root)
    assert update.installed_version(root) == "2.2.0"
    for name in [
        "targets/Original.wav",
        "score_targets/Score.gp5",
        "processed/reviewed.json",
        "models/model.bin",
        "venv-location.windows.txt",
        "custom.magic",
        "my-notes.txt",
    ]:
        assert (root / name).read_bytes() == before[name]


@pytest.mark.skipif(sys.platform not in {"darwin", "win32"}, reason="native launchers")
def test_double_click_launcher_keeps_path_and_survives_self_replacement(tmp_path):
    kit = tmp_path / "Application 한글 space"
    kit.mkdir()
    (kit / "installers").mkdir()
    (kit / "main.py").touch()
    (kit / "venv-location.windows.txt").write_text(sys.prefix, encoding="utf-8")
    for name in ["update.command", "update.bat", "installers/update-windows.ps1"]:
        shutil.copyfile(ROOT / name, kit / name)
    # No actual download or modification of the real application/venv.
    (kit / "update.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "root=Path(__file__).parent\n"
        'print("LAUNCH_PROOF="+json.dumps(sys.argv[1:], ensure_ascii=True))\n'
        'for name in ["update.bat", "update.command"]:\n'
        ' (root/name).write_text("echo SHOULD_NOT_EXECUTE\\n"*30)\n',
        encoding="utf-8",
    )
    command = (
        ["cmd.exe", "/d", "/c", str(kit / "update.bat")]
        if sys.platform == "win32"
        else ["/bin/zsh", str(kit / "update.command")]
    )
    result = subprocess.run(
        command,
        env={**os.environ, "MAGIC_NO_PAUSE": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    proof = next(
        line.split("=", 1)[1]
        for line in result.stdout.splitlines()
        if line.startswith("LAUNCH_PROOF=")
    )
    assert json.loads(proof) == ["--project", str(kit), "--pause", "--wait-for-app"]
    assert "SHOULD_NOT_EXECUTE" not in result.stdout


def test_gui_handoff_waits_for_app_lock_then_proceeds(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(update, "check_legacy_processes", lambda _: None)
    code = (
        "from pathlib import Path; from app_guard import app_guard; import sys,time;\n"
        "with app_guard(Path(sys.argv[1])):\n"
        " print('READY',flush=True); time.sleep(.6)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(tmp_path)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout.readline().strip() == "READY"
    started = time.monotonic()
    try:
        with update.update_lock(tmp_path, wait_seconds=3):
            assert time.monotonic() - started >= 0.4
    finally:
        child.wait(timeout=3)
        child.stdout.close()
    # A second instance that stays open must time out without altering data.
    with app_guard(tmp_path):
        with pytest.raises(RuntimeError):
            with update.update_lock(tmp_path, wait_seconds=0.1):
                pytest.fail("updated with app still open")
