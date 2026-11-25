# SPDX-License-Identifier: GPL-3.0-only
"""Cache settings and small windows keep score controls usable."""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from practice_player.cache import hash_file
from practice_player.cache_locations import SETTINGS_NAME, save_cache_locations
from practice_player.config import Paths
from practice_player.score import ScoreTimeline
from practice_player.score_analysis import analyze_score
from practice_player.score_gui import ScorePanel
from tests.test_pdf_only_analysis import score_pdf


@pytest.fixture
def score_panel(tmp_path, tone):
    app = QApplication.instance() or QApplication([])
    paths = Paths(tmp_path / "project")
    paths.create()
    panel = ScorePanel(paths)
    panel.audio, panel.source_hash = tone, hash_file(tone)
    pdf = score_pdf(paths.score_targets / "authored-study.pdf")
    try:
        yield panel, paths, pdf
    finally:
        panel.view.clear()
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def change_settings(panel, paths, tmp_path, kind):
    if kind == "corrupt":
        (paths.processed / SETTINGS_NAME).write_text("{broken", encoding="utf-8")
        panel.paths = Paths(paths.root)
    else:
        save_cache_locations(paths, tmp_path / "other audio", tmp_path / "other scores")


@pytest.mark.parametrize("kind", ["corrupt", "other-window"])
def test_cache_settings_error_keeps_new_readable_pdf(score_panel, tmp_path, kind):
    panel, paths, pdf = score_panel
    original = pdf.read_bytes()
    change_settings(panel, paths, tmp_path, kind)
    panel.open_pdf(pdf)
    assert panel.pdf == pdf
    assert panel.view.document is not None and len(panel.view.document) == 1
    assert panel.timeline is None
    assert "악보 화면" in panel.status.text() and "상세 옵션" in panel.status.text()
    assert "악보 읽기 실패" not in panel.status.text()
    assert panel.choose.isEnabled() and panel.refresh.isEnabled()
    assert pdf.read_bytes() == original


@pytest.mark.parametrize("kind", ["corrupt", "other-window"])
def test_cache_settings_error_retains_verified_loaded_timeline(
    score_panel, tmp_path, tone, kind,
):
    panel, paths, pdf = score_panel
    timeline = analyze_score(tone, pdf, paths, mode="pdf", bpm=120)
    reviewed = ScoreTimeline.load(timeline)
    reviewed.data.update(reviewed=True, offset=0.125)
    reviewed.save(timeline)
    original = timeline.read_bytes()
    panel.open_pdf(pdf)
    assert panel.timeline_path == timeline
    change_settings(panel, paths, tmp_path, kind)
    panel.open_pdf(pdf)
    assert panel.pdf == pdf and panel.view.document is not None
    assert panel.timeline_path == timeline
    assert panel.timeline.data["reviewed"]
    assert panel.timeline.data["offset"] == 0.125
    assert "기존 마디 시간표" in panel.status.text() and "상세 옵션" in panel.status.text()
    assert timeline.read_bytes() == original


def test_cache_settings_error_does_not_hide_invalid_pdf(score_panel, tmp_path):
    panel, paths, _ = score_panel
    invalid = paths.score_targets / "invalid.pdf"
    invalid.write_bytes(b"not a PDF")
    change_settings(panel, paths, tmp_path, "corrupt")
    panel.open_pdf(invalid)
    assert panel.pdf is None and panel.view.document is None and panel.timeline is None
    assert "악보 읽기 실패" in panel.status.text()


@pytest.mark.parametrize("kind", ["corrupt", "other-window"])
def test_analysis_options_reports_settings_error_without_losing_score(
    score_panel, tmp_path, monkeypatch, kind,
):
    from PySide6.QtWidgets import QDialog

    panel, paths, pdf = score_panel
    panel.open_pdf(pdf)
    document = panel.view.document
    requested = []
    panel.analysis_requested.connect(requested.append)
    change_settings(panel, paths, tmp_path, kind)

    def unexpected_dialog(_):
        pytest.fail("Invalid cache settings must be explained before analysis options open")

    monkeypatch.setattr(QDialog, "exec", unexpected_dialog)
    panel.analysis_options()
    assert panel.pdf == pdf and panel.view.document is document
    assert "상세 옵션" in panel.status.text() and "악보 사전 분석" in panel.status.text()
    assert requested == []


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("size", [(800, 600), (1500, 930)])
def test_score_tools_keep_readable_rows_and_scroll_into_reach(
    tmp_path, fake_engine, monkeypatch, mode, size,
):
    from PySide6.QtWidgets import QDialog

    from practice_player.config import Settings
    from practice_player.gui import Window
    from practice_player.pipeline import process
    from tests.test_gui import wait_until
    from tests.test_magic_bundle import portable_study

    app = QApplication.instance() or QApplication([])
    request, prepared, paths = portable_study(tmp_path / "authored", count=4)
    source = Path(request["audio"])
    result = process(source, Settings("hyperace"), paths, runner=fake_engine)
    window = Window(paths)
    window.resize(*size)
    window.source_path, window.source_name = source, source.name
    window.load_result(result.to_dict())
    panel = window.score_panel
    panel.audio, panel.source_hash = source, hash_file(source)
    panel.accept_gp(prepared)
    panel.load_timeline(Path(request["timeline"]))
    window.audio_output.setVolume(0)
    window.theme.setCurrentIndex(window.theme.findData(mode))
    window.show()
    try:
        wait_until(app, window.player.isSeekable)
        available = None
        actual_size = size
        if app.platformName() not in {"offscreen", "minimal"}:
            # Native CI desktops can be smaller than the requested large viewport.
            available = window.screen().availableGeometry()
            frame_size = window.frameGeometry().size() - window.size()
            client_limit = available.size() - frame_size
            actual_size = (min(size[0], client_limit.width()), min(size[1], client_limit.height()))
            assert actual_size[0] >= 800 and actual_size[1] >= 600
            window.resize(*actual_size)
            window.move(available.topLeft())
        QTest.qWait(120)
        assert window.size().toTuple() == actual_size
        for control in (panel.choose, panel.refresh, panel.parts, panel.analyze, panel.review):
            assert control.height() >= control.sizeHint().height()
            panel.tools.ensureWidgetVisible(control, 0, 0)
            QTest.qWait(20)
            bounds = QRect(control.mapTo(window, QPoint()), control.size())
            viewport = QRect(panel.tools.viewport().mapTo(window, QPoint()),
                             panel.tools.viewport().size())
            assert viewport.contains(bounds), (control.accessibleName(), bounds, viewport)
            assert window.rect().contains(bounds)
            assert window.childAt(bounds.center()) is control
            if available is not None:
                on_screen = QRect(control.mapToGlobal(QPoint()), control.size())
                assert available.contains(on_screen), (
                    control.accessibleName(), on_screen, available,
                )
            assert control.isEnabled()
            if control is panel.parts:
                QTest.mouseClick(control, Qt.MouseButton.LeftButton)
                assert control.view().isVisible()
                control.hidePopup()
            elif control is panel.analyze:
                opened = []

                def inspect_dialog(dialog):
                    opened.append(dialog.windowTitle())
                    return QDialog.DialogCode.Rejected

                monkeypatch.setattr(QDialog, "exec", inspect_dialog)
                QTest.mouseClick(control, Qt.MouseButton.LeftButton)
                assert opened == ["악보 사전 분석 설정"]
        if size == (800, 600):
            assert actual_size == size
            assert panel.tools.verticalScrollBar().maximum() > 0
        elif actual_size == size:
            assert panel.tools.verticalScrollBar().maximum() == 0, (
                panel.tools.size(), panel.tools.sizeHint(), panel.tools.widget().sizeHint(),
                panel.tools.widget().minimumSize(),
                panel.tools.widget().heightForWidth(panel.tools.viewport().width()),
            )
        for control in (window.play_button, window.playback_variants):
            bounds = QRect(control.mapTo(window, QPoint()), control.size())
            assert window.rect().contains(bounds)
            assert window.childAt(bounds.center()) is control
            if available is not None:
                on_screen = QRect(control.mapToGlobal(QPoint()), control.size())
                assert available.contains(on_screen), (
                    control.accessibleName(), on_screen, available,
                )
        assert panel.view.viewport().height() > 0
        screenshots = os.environ.get("MAGIC_RENDER_CHECK_DIR")
        if screenshots:
            directory = Path(screenshots)
            directory.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(directory / f"score-tools-{actual_size[0]}x{actual_size[1]}-{mode}.png"))
        before = panel.view.viewport().height()
        timeline = panel.timeline
        source_url, position = window.player.source(), window.player.position()
        QTest.mouseClick(panel.tools_toggle, Qt.MouseButton.LeftButton)
        QTest.qWait(80)
        assert panel.tools.isHidden()
        assert panel.view.viewport().height() > before
        assert window.play_button.isEnabled() and window.playback_variants.isEnabled()
        assert panel.timeline is timeline and window.player.source() == source_url
        assert window.player.position() == position
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
