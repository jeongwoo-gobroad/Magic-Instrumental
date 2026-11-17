# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import bootstrap
from practice_player.config import ROOT


def test_macos_venv_location_is_outside_project(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert bootstrap.environment_path() == tmp_path / ".vocal-separator-and-speed-adj" / ".venv"


def test_windows_config_reads_one_external_path(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(bootstrap, "ROOT", project)
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    config = project / "venv-location.windows.txt"
    config.write_text(f"# comment\n{tmp_path / 'external' / '.venv'}\n", encoding="utf-8")
    assert bootstrap.environment_path() == tmp_path / "external" / ".venv"
    config.write_text(str(project / ".venv"), encoding="utf-8")
    with pytest.raises(ValueError, match="밖"):
        bootstrap.environment_path()
    config.write_text("one\ntwo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="한 줄"):
        bootstrap.environment_path()


def test_global_python_entry_is_rejected():
    result = subprocess.run(
        [sys._base_executable, str(ROOT / "main.py"), "doctor"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 1
    assert "외부 가상환경" in result.stdout


def test_missing_windows_config_uses_public_template(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "external" / ".venv"
    (project / "venv-location.windows.example.txt").write_text(str(target), encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ROOT", project)
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    assert bootstrap.environment_path() == target
    assert (project / "venv-location.windows.txt").read_text() == str(target)


def fake_bootstrap_environment(monkeypatch, tmp_path, profile="default"):
    target = tmp_path / ".venv"
    python = target / "Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (python.parent / "ffmpeg.exe").touch()
    (target / ".magic-instrumental-profile").write_text(profile)
    signature = hashlib.sha256(
        (bootstrap.ROOT / "requirements.txt").read_bytes()
        + (bootstrap.ROOT / "requirements-windows.lock.txt").read_bytes()
        + (bootstrap.ROOT / "requirements-cuda.txt").read_bytes()
        + profile.encode()
    ).hexdigest()
    (target / ".practice-player-requirements").write_text(signature)
    monkeypatch.setattr(bootstrap, "environment_path", lambda: target)
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "nvidia-smi")

    def check_output(command, **_kwargs):
        if "import sys,json;" in command[-1]:
            return json.dumps([str(target), str(tmp_path / "base"), [3, 12]])
        return str(python.parent / "ffmpeg.exe")

    monkeypatch.setattr(bootstrap.subprocess, "check_output", check_output)
    return target


def test_launch_does_not_reinstall_cpu_profile_even_with_nvidia(monkeypatch, tmp_path):
    fake_bootstrap_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap.py", "--launch-only"])
    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda *_a, **_kw: pytest.fail("Launch invoked pip")
    )
    launches = []
    monkeypatch.setattr(
        bootstrap.subprocess, "call", lambda *a, **kw: launches.append((a, kw)) or 0
    )
    assert bootstrap.main() == 0
    assert launches[0][0][0][-1] == "gui"


def test_launch_with_changed_dependencies_requires_explicit_install(monkeypatch, tmp_path):
    target = fake_bootstrap_environment(monkeypatch, tmp_path)
    (target / ".practice-player-requirements").write_text("outdated")
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap.py", "--launch-only"])
    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda *_a, **_kw: pytest.fail("Launch invoked pip")
    )
    with pytest.raises(RuntimeError, match="설치가 필요"):
        bootstrap.main()


def test_switching_to_cuda_updates_install_and_remembered_profile(monkeypatch, tmp_path):
    target = fake_bootstrap_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap.py", "--setup-only", "--cuda"])
    commands = []
    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda command, **_kw: commands.append(command)
    )
    assert bootstrap.main() == 0
    assert any(str(ROOT / "requirements-cuda.txt") in command for command in commands)
    assert (target / ".magic-instrumental-profile").read_text() == "cuda"
