# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from practice_player import omr_install as installer
from practice_player import omr_runtime as runtime
from practice_player.msi_extract import file_layout


def test_download_rejects_bad_bytes_and_never_publishes(monkeypatch, tmp_path):
    class Response(io.BytesIO):
        url = "https://example.invalid/official"

    data = b"verified official download"
    spec = {"url": Response.url, "sha256": hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: Response(b"corrupt"))
    with pytest.raises(ValueError, match="SHA-256"):
        installer.download(spec, tmp_path)
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: Response(data))
    path = installer.download(spec, tmp_path)
    assert path.read_bytes() == data
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: pytest.fail("Network"))
    assert installer.download(spec, tmp_path) == path


def test_msi_layout_preserves_original_names_and_rejects_escape():
    dirs = [("TARGETDIR", "", "SourceDir"), ("PFiles", "TARGETDIR", "PFiles64"),
            ("INSTALL", "PFiles", "Audiveris"), ("APP", "INSTALL", "app"),
            ("LIB", "APP", "."), ("RUNTIME", "INSTALL", "runtime")]
    components = [("main", "INSTALL"), ("jars", "LIB"), ("jre", "RUNTIME")]
    files = [("file01", "main", "AUDIVE~1.EXE|Audiveris.exe"),
             ("file02", "jars", "audiveris.jar"), ("file03", "jre", "release")]
    assert file_layout(files, components, dirs) == {
        "file01": Path("Audiveris/Audiveris.exe"),
        "file02": Path("Audiveris/app/audiveris.jar"),
        "file03": Path("Audiveris/runtime/release"),
    }
    with pytest.raises(ValueError, match="Unsafe"):
        file_layout([("escape", "main", "../outside")], components, dirs)
    with pytest.raises(ValueError, match="Duplicate"):
        file_layout(files + [("other", "main", "AUDIVERIS.EXE")], components, dirs)


def test_installed_inventory_detects_missing_or_changed_native_files(tmp_path):
    program = runtime.executable(tmp_path)
    program.parent.mkdir(parents=True)
    program.write_bytes(b"official native executable")
    (tmp_path / "tessdata").mkdir()
    (tmp_path / "tessdata/eng.traineddata").write_bytes(b"official English data")
    marker = {"signature": runtime.signature(), "files": installer.inventory(tmp_path)}
    (tmp_path / "verified.json").write_text(json.dumps(marker))
    assert installer.intact(tmp_path)
    program.write_bytes(b"damaged")
    assert not installer.intact(tmp_path)
    program.unlink()
    assert not runtime.managed_ready(tmp_path)


def test_probe_requires_recognized_words_and_completed_text_step(tmp_path):
    def book(words, steps):
        with zipfile.ZipFile(tmp_path / "probe.omr", "w") as z:
            z.writestr("sheet#1/sheet#1.xml", words)
            z.writestr("book.xml", steps)

    book('<word value="MAGIC"/><word value="PRACTICE"/>', "LOAD TEXTS")
    assert installer.probe_result(tmp_path)
    book('<word value="MAGIC"/>', "LOAD TEXTS")
    assert not installer.probe_result(tmp_path)
    book('<word value="MAGIC"/><word value="PRACTICE"/>', "LOAD GRID")
    assert not installer.probe_result(tmp_path)


def test_managed_ocr_uses_verified_data_not_unrelated_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "managed_root", lambda: tmp_path)
    monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path / "unrelated"))
    env = runtime.omr_environment(str(runtime.executable(tmp_path)))
    assert env["TESSDATA_PREFIX"] == str(tmp_path / "tessdata")
    env = runtime.omr_environment(str(tmp_path / "custom-audiveris"))
    assert env["TESSDATA_PREFIX"] == str(tmp_path / "unrelated")


def test_failed_verification_keeps_previous_install_and_does_not_publish(monkeypatch, tmp_path):
    target = tmp_path / "managed"
    target.mkdir()
    (target / "existing").write_text("preserve")
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(installer.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(installer, "tools_root", lambda: tmp_path)
    monkeypatch.setattr(installer, "managed_root", lambda: target)
    downloaded = tmp_path / "downloaded"
    downloaded.write_bytes(b"fixture")
    monkeypatch.setattr(installer, "download", lambda *a: downloaded)
    monkeypatch.setattr(installer, "extract_macos", lambda *a: None)

    def fail(_root):
        raise RuntimeError("English OCR failed")

    monkeypatch.setattr(installer, "verify_runtime", fail)
    with pytest.raises(RuntimeError, match="English OCR failed"):
        installer.install()
    assert (target / "existing").read_text() == "preserve"
    assert not (target / "verified.json").exists()
    assert not list(tmp_path.glob("omr-stage-*"))


def test_managed_program_precedes_path_but_explicit_override_wins(monkeypatch, tmp_path):
    from practice_player import score_analysis

    managed = runtime.executable(tmp_path)
    managed.parent.mkdir(parents=True)
    managed.touch()
    custom = tmp_path / "custom"
    custom.touch()
    monkeypatch.setattr(score_analysis, "managed_root", lambda: tmp_path)
    monkeypatch.setattr(score_analysis, "managed_ready", lambda: True)
    monkeypatch.setattr(score_analysis.shutil, "which", lambda _: str(custom))
    monkeypatch.delenv("AUDIVERIS_EXECUTABLE", raising=False)
    assert score_analysis.audiveris_path() == str(managed.resolve())
    monkeypatch.setenv("AUDIVERIS_EXECUTABLE", str(custom))
    assert score_analysis.audiveris_path() == str(custom.resolve())
