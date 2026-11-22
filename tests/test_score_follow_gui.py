# SPDX-License-Identifier: GPL-3.0-only
"""Verify the first analysis result reaches the visible, playing PDF view."""

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDialogButtonBox, QDoubleSpinBox

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.gui import STYLE, Window
from tests.test_gui import wait_until
from tests.test_pdf_navigation_regression import direction
from tests.test_score import xml_file
from tests.test_score_geometry import vector_pdf


@pytest.mark.parametrize("size", [(1500, 930), (800, 600)])
def test_first_worker_result_follows_real_playback_and_resized_pages(tmp_path, monkeypatch, size):
    paths = Paths(tmp_path / "project")
    paths.create()
    audio = paths.targets / "study.wav"
    samples = np.arange(8000 * 20) / 8000
    sf.write(audio, 0.02 * np.sin(2 * np.pi * 220 * samples), 8000)
    pdf = vector_pdf(
        paths.score_targets / "study.pdf",
        [((595, 842), [(y, [50, 550], []) for y in (700, 450, 200)], [5])] * 2,
    )
    xml_file(paths.score_targets, [
        "", direction('segno="s"'), direction('tocoda="c"'),
        '<print new-page="yes"/>' + direction('dalsegno="s"'),
        direction('coda="c"'), "",
    ]).rename(pdf.with_suffix(".musicxml"))
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    window = Window(paths)
    window.resize(*size)
    window.show()
    panel = window.score_panel
    requests, dialog_errors = [], []
    panel.analysis_requested.connect(requests.append)

    def configure_analysis():
        dialog = app.activeModalWidget()
        mode = dialog.findChild(QComboBox, "scoreAlignmentMode")
        if mode is None or mode.findData("tempo") < 0:
            dialog_errors.append("PDF analysis must expose its fixed-tempo mode")
            dialog.reject()
            return
        mode.setCurrentIndex(mode.findData("tempo"))
        bpm = next(x for x in dialog.findChildren(QDoubleSpinBox) if x.minimum() == 20)
        bpm.setValue(120)
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

    def play_at(seconds, index, page):
        matching_view = panel.view_matches()
        milliseconds = round(seconds * 1000)
        window.player.setPosition(milliseconds)
        window.player.play()
        wait_until(app, lambda: (
            window.player.position() >= milliseconds + 80
            and panel.current_index == index and panel.view.page == page
        ), timeout=10)
        assert panel.camera_timer.isActive()
        assert not matching_view or panel.view_matches()
        window.player.pause()
        assert not panel.camera_timer.isActive()
        return panel.view.verticalScrollBar().value()

    try:
        panel.bind_track(audio, hash_file(audio))
        assert panel.pdf == pdf.resolve() and panel.timeline is None
        panel.zoom.setCurrentIndex(panel.zoom.findData(2.0))
        window.audio_output.setVolume(0)
        window.player.setSource(QUrl.fromLocalFile(str(audio)))
        wait_until(app, window.player.isSeekable)
        QTest.qWait(150)
        profile = panel.view.profile()
        QTimer.singleShot(0, configure_analysis)
        QTest.mouseClick(panel.analyze, Qt.MouseButton.LeftButton)
        assert not dialog_errors
        wait_until(app, lambda: window.worker is None, timeout=45)
        assert not window.job_error, window.logs.toPlainText()
        QTest.qWait(150)  # Let queued layout changes and the 80 ms resize timer finish.
        assert len(requests) == 1
        assert requests[0]["view-width"] == profile["width"]
        assert requests[0]["view-height"] == profile["height"]
        assert panel.timeline.data["scroll"]["profile"] == profile
        assert panel.view_matches(), (profile, panel.view.profile(), panel.status.text())
        assert "같은 이름의 MusicXML" in panel.status.text()
        assert [cue["source_index"] for cue in panel.timeline.cues] == [0, 1, 2, 3, 1, 2, 4, 5]

        def no_analysis(*_args, **_kwargs):
            pytest.fail("Playback and viewport changes must not analyze the PDF")

        monkeypatch.setattr("practice_player.score_layout.analyze_layout", no_analysis)
        monkeypatch.setattr("practice_player.score_geometry.detect_pdf_layout", no_analysis)
        top = play_at(0.25, 0, 0)
        bottom = play_at(4.25, 2, 0)
        assert bottom > top
        play_at(6.25, 3, 1)
        returned = play_at(8.25, 4, 0)
        assert returned < bottom and "2회차" in panel.measure.text()
        play_at(12.25, 6, 1)
        assert "5마디" in panel.measure.text()

        panel.tools_toggle.click()
        QTest.qWait(150)
        assert not panel.view_matches() and panel.follow.isChecked()
        before = panel.view.verticalScrollBar().value()
        assert play_at(14.25, 7, 1) == before
        assert "6마디" in panel.measure.text() and "재분석" in panel.view_status.text()
        play_at(0.25, 0, 0)
        assert "1마디" in panel.measure.text()

        # Long warnings remain readable without changing the saved viewport again.
        before = panel.view.profile()
        warning = "\n".join(["반복·코다와 마디 시간표를 확인하세요."] * 6)
        panel.status.setText(warning)
        QTest.qWait(100)
        assert panel.view.profile() == before
        assert panel.status_area.verticalScrollBar().maximum() > 0
        assert panel.status.height() >= panel.status.heightForWidth(panel.status.width())
        assert panel.status_area.accessibleName()
        panel.status_area.verticalScrollBar().setValue(
            panel.status_area.verticalScrollBar().maximum()
        )
        assert panel.status.text() == warning and panel.follow.isChecked()

        panel.manual_page(2)
        assert not panel.follow.isChecked()
        window.player.setPosition(250)
        assert panel.view.page == 1
    finally:
        if window.worker:
            window.cancel_job()
            wait_until(app, lambda: window.worker is None)
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
