# SPDX-License-Identifier: GPL-3.0-only
"""Choose portable audio quality without disturbing the score or cancelled saves."""

import json

import pytest
from pypdf import PdfWriter
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDialogButtonBox, QFileDialog, QMessageBox

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.gui import Window
from practice_player.score import ScoreTimeline
from tests.test_gui import wait_until


def schedule_magic_quality(app, selection=None, accept=True):
    observed = {"errors": []}

    def choose():
        dialog = app.activeModalWidget()
        try:
            quality = dialog.findChild(QComboBox, "magicAudioQuality")
            assert quality is not None
            observed["initial"] = quality.currentData()
            observed["choices"] = [quality.itemData(i) for i in range(quality.count())]
            if selection is not None:
                index = quality.findData(selection)
                assert index >= 0
                quality.setCurrentIndex(index)
            button = (QDialogButtonBox.StandardButton.Save if accept
                      else QDialogButtonBox.StandardButton.Cancel)
            QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(button),
                             Qt.MouseButton.LeftButton)
        except Exception as error:
            observed["errors"].append(str(error))
            if dialog is not None:
                dialog.reject()

    QTimer.singleShot(0, choose)
    return observed


def dispose(window, app):
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture
def export_window(tmp_path, tone, monkeypatch):
    app = QApplication.instance() or QApplication([])
    paths = Paths(tmp_path / "project")
    window = Window(paths)
    window.source_path, window.source_name = tone, tone.name
    window.load_result({"instrumental": str(tone), "cached": True})
    window.audio_output.setVolume(0)
    pdf = paths.score_targets / "study.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with pdf.open("wb") as stream:
        writer.write(stream)
    panel = window.score_panel
    panel.audio, panel.source_hash = tone, hash_file(tone)
    panel.open_pdf(pdf)
    timeline = paths.processed / "reviewed-timeline.json"
    ScoreTimeline({
        "version": 1, "page_count": 1, "end": 3, "reviewed": True,
        "audio_sha256": hash_file(tone), "pdf_sha256": hash_file(pdf),
        "cues": [{"start": 0, "page": 0, "source_index": 0,
                  "measure": "1", "visit": 1, "marks": []}],
    }).save(timeline)
    panel.load_timeline(timeline)
    window.resize(800, 600)
    window.show()
    wait_until(app, window.player.isSeekable)
    window.player.setPosition(750)
    QTest.qWait(120)
    jobs = []
    monkeypatch.setattr(window, "start_job", jobs.append)
    try:
        yield app, window, jobs
    finally:
        dispose(window, app)


@pytest.mark.parametrize("selection", ["mp3:128", "mp3:192", "mp3:320", "wav"])
def test_magic_audio_selection_reaches_worker_and_is_remembered(
    export_window, tmp_path, monkeypatch, selection,
):
    app, window, jobs = export_window
    target = tmp_path / "practice.magic"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_: (str(target), "Magic"))
    before = window.score_panel.view.profile(), window.player.position()
    sources = [window.source_path, window.score_panel.pdf, window.score_panel.timeline_path]
    original = [path.read_bytes() for path in sources]
    chosen = schedule_magic_quality(app, selection)
    QTest.mouseClick(window.magic_button, Qt.MouseButton.LeftButton)
    assert not chosen["errors"]
    assert chosen["initial"] == "mp3:256"
    assert chosen["choices"] == ["mp3:128", "mp3:192", "mp3:256", "mp3:320", "wav"]
    request = window.paths.processed / "magic-export-request.json"
    data = json.loads(request.read_text())
    assert data["audio_format"] == ("wav" if selection == "wav" else "mp3")
    if selection == "wav":
        assert "mp3_bitrate_kbps" not in data
    else:
        assert data["mp3_bitrate_kbps"] == int(selection.partition(":")[2])
    assert jobs == [["magic-export", "--request", str(request), "--events"]]
    assert not target.exists()
    assert [path.read_bytes() for path in sources] == original
    assert (window.score_panel.view.profile(), window.player.position()) == before
    window.preferences.sync()

    reopened = Window(window.paths)
    try:
        remembered = schedule_magic_quality(app, accept=False)
        assert reopened.magic_audio_options() is None
        assert not remembered["errors"] and remembered["initial"] == selection
    finally:
        dispose(reopened, app)


@pytest.mark.parametrize("cancel_at", ["quality", "filename", "overwrite"])
def test_cancelled_magic_save_preserves_request_settings_files_and_position(
    export_window, tmp_path, monkeypatch, cancel_at,
):
    app, window, jobs = export_window
    target = tmp_path / "existing.magic"
    target.write_bytes(b"previous practice bundle")
    request = window.paths.processed / "magic-export-request.json"
    request.write_bytes(b"previous request")
    window.preferences.setValue("magic_export/audio_quality", "mp3:128")
    window.preferences.sync()
    settings = {key: window.preferences.value(key) for key in window.preferences.allKeys()}
    before = (window.score_panel.view.profile(), window.player.position(),
              window.player.playbackState())
    sources = [target, request, window.source_path, window.score_panel.pdf,
               window.score_panel.timeline_path]
    original = [path.read_bytes() for path in sources]
    file_dialogs, confirmations = [], []

    def filename(*_):
        file_dialogs.append(True)
        return ("" if cancel_at == "filename" else str(target)), "Magic"

    def decline(*_):
        confirmations.append(True)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QFileDialog, "getSaveFileName", filename)
    monkeypatch.setattr(QMessageBox, "question", decline)
    chosen = schedule_magic_quality(app, "wav", accept=cancel_at != "quality")
    QTest.mouseClick(window.magic_button, Qt.MouseButton.LeftButton)
    assert not chosen["errors"] and chosen["initial"] == "mp3:128"
    app.processEvents()
    assert jobs == [] and window.worker is None
    assert len(file_dialogs) == (cancel_at != "quality")
    assert len(confirmations) == (cancel_at == "overwrite")
    assert {key: window.preferences.value(key) for key in window.preferences.allKeys()} == settings
    assert [path.read_bytes() for path in sources] == original
    assert (window.score_panel.view.profile(), window.player.position(),
            window.player.playbackState()) == before
