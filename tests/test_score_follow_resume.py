# SPDX-License-Identifier: GPL-3.0-only
"""Resume the current measure after manual browsing, including a changed viewport."""

from copy import deepcopy

import numpy as np
import pytest
import soundfile as sf
from pypdf import PdfWriter
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractSlider, QApplication

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.gui import STYLE, Window
from practice_player.score import ScoreTimeline
from practice_player.score_view import rendered_size
from tests.test_gui import wait_until


def wheel_score(panel):
    viewport = panel.view.viewport()
    point = viewport.rect().center()
    event = QWheelEvent(
        QPointF(point), QPointF(viewport.mapToGlobal(point)), QPoint(), QPoint(0, 1200),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(viewport, event)
    assert not panel.follow.isChecked()


def assert_current_measure_visible(panel):
    frame = panel.timeline.data["scroll"]["frames"][panel.current_index]
    assert panel.view.page == frame["page"]
    box, page, viewport = frame["box"], panel.view.label.contentsRect(), panel.view.viewport()
    for bar, origin, size, extent, first, last in (
        (panel.view.horizontalScrollBar(), page.x(), page.width(), viewport.width(),
         "left", "right"),
        (panel.view.verticalScrollBar(), page.y(), page.height(), viewport.height(),
         "top", "bottom"),
    ):
        start, end = origin + box[first] * size, origin + box[last] * size
        if end - start > extent:
            assert bar.value() == pytest.approx(
                max(bar.minimum(), min(bar.maximum(), start)), abs=1,
            )
        else:
            assert bar.value() <= start + 1
            assert end <= bar.value() + extent + 1


def settle_view(app, panel):
    app.processEvents()
    wait_until(app, lambda: not panel.view.timer.isActive(), timeout=5)


def blank_pdf(path, title):
    writer = PdfWriter()
    for width, height in [(595, 842), (842, 595)]:
        writer.add_blank_page(width=width, height=height)
    writer.add_metadata({"/Title": title})
    with path.open("wb") as output:
        writer.write(output)
    return path


@pytest.fixture
def resume_score(tmp_path, monkeypatch):
    paths = Paths(tmp_path / "project")
    paths.create()
    audio = paths.targets / "study.wav"
    samples = np.arange(8000 * 20) / 8000
    sf.write(audio, 0.02 * np.sin(2 * np.pi * 220 * samples), 8000)
    pdf = blank_pdf(paths.score_targets / "study.pdf", "Follow study")
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    window = Window(paths)
    window.source_path, window.source_name = audio, audio.name
    window.load_result({"instrumental": str(audio), "cached": True})
    window.audio_output.setVolume(0)
    window.resize(1400, 900)
    window.show()
    panel = window.score_panel
    panel.bind_track(audio, hash_file(audio))
    panel.zoom.setCurrentIndex(panel.zoom.findData(2.0))
    wait_until(app, window.player.isSeekable)
    settle_view(app, panel)
    profile = panel.view.profile()
    small = {"left": 0.55, "right": 0.75, "top": 0.5, "bottom": 0.6}
    large = {"left": 0.15, "right": 0.95, "top": 0.1, "bottom": 0.95}
    edge = {"left": 0.82, "right": 0.98, "top": 0.8, "bottom": 0.98}
    data = {
        "version": 1, "page_count": 2, "offset": 0.25, "end": 20, "reviewed": True,
        "audio_sha256": hash_file(audio), "pdf_sha256": hash_file(pdf),
        "cues": [],
        "scroll": {"version": 1, "profile": profile,
                   "page_sizes": [(595, 842), (842, 595)], "frames": []},
    }
    for i, (page, source, visit, box) in enumerate([
        (0, 0, 1, small), (0, 1, 1, large), (1, 2, 1, edge), (0, 0, 2, small),
    ]):
        data["cues"].append({
            "start": i * 5, "page": page, "source_index": source,
            "measure": str(source + 1), "visit": visit, "marks": [],
        })
        width, height = rendered_size(data["scroll"]["page_sizes"][page], profile)
        x = max(0, min(width - profile["width"], box["left"] * width - profile["width"] * .1))
        y = max(0, min(height - profile["height"], box["top"] * height - profile["height"] * .1))
        data["scroll"]["frames"].append({
            "page": page, "source_index": source, "system": source, "box": box,
            "x0": x, "x1": x, "y0": y, "y1": y, "intro_y": y, "settle": 0,
        })
    path = paths.processed / "reviewed-study" / "timeline.json"
    ScoreTimeline(data).save(path)
    sentinel = path.with_name("layout-cache.bin")
    sentinel.write_bytes(b"existing analysis cache")
    panel.load_timeline(path)
    settle_view(app, panel)
    assert panel.view_matches()
    window.seek_to(2250)
    wait_until(app, lambda: panel.current_index == 0)
    protected = {file: hash_file(file) for file in (audio, pdf, path, sentinel)}

    def forbidden(*_args, **_kwargs):
        pytest.fail("Following must not analyze the score or rebuild camera paths")

    monkeypatch.setattr("practice_player.score_geometry.detect_pdf_layout", forbidden)
    monkeypatch.setattr("practice_player.score_layout.analyze_layout", forbidden)
    monkeypatch.setattr("practice_player.score_layout.plan_camera", forbidden)
    try:
        yield app, window, path
        assert all(hash_file(file) == digest for file, digest in protected.items())
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("change", ["fold", "resize", "zoom"])
def test_resume_same_page_after_view_change_without_highlight(resume_score, monkeypatch, change):
    app, window, _ = resume_score
    panel = window.score_panel
    original = deepcopy(panel.timeline.data)
    panel.highlight.setChecked(False)
    if change == "fold":
        panel.tools_toggle.click()
    elif change == "resize":
        window.resize(1050, 800)
    else:
        panel.zoom.setCurrentIndex(panel.zoom.findData(3.0))
    settle_view(app, panel)
    assert not panel.view_matches()
    wheel_score(panel)
    before = panel.view.verticalScrollBar().value()
    QTest.mouseClick(panel.follow, Qt.MouseButton.LeftButton)
    assert panel.view.verticalScrollBar().value() != before
    assert_current_measure_visible(panel)
    assert panel.current_index == 0 and panel.view.label.highlight_box is None
    assert "현재 마디" in panel.view_status.text() and "재분석" in panel.view_status.text()

    moves, reveal = [], panel.view.reveal_box

    def observed(box):
        moves.append(box)
        reveal(box)

    monkeypatch.setattr(panel.view, "reveal_box", observed)
    for seconds in (2.3, 2.5, 3.0):
        panel.follow_position(seconds)
    assert not moves
    window.seek_to(6250)
    assert panel.current_index == 1 and len(moves) == 1
    assert_current_measure_visible(panel)  # A wide, tall measure shows its beginning.
    before = len(moves)
    window.resize(window.width() + 60, window.height() + 60)
    settle_view(app, panel)
    assert len(moves) > before
    assert_current_measure_visible(panel)
    before = len(moves)
    panel.follow_position(6.5)
    panel.follow_position(6.8)
    assert len(moves) == before
    assert panel.timeline.data == original and window.player.position() == 6250


def test_scrollbar_and_manual_page_resume_use_current_player_position(resume_score):
    app, window, _ = resume_score
    panel = window.score_panel
    panel.tools_toggle.click()
    settle_view(app, panel)
    panel.view.verticalScrollBar().triggerAction(QAbstractSlider.SliderAction.SliderToMinimum)
    assert not panel.follow.isChecked()
    # A queued position signal must not make the checkbox resume an older measure.
    window.player.blockSignals(True)
    try:
        window.player.setPosition(6250)
        assert window.player.position() == 6250 and panel.current_index == 0
        QTest.mouseClick(panel.follow, Qt.MouseButton.LeftButton)
        assert panel.current_index == 1 and panel.last_position == 6250
        assert_current_measure_visible(panel)
    finally:
        window.player.blockSignals(False)
    panel.manual_page(2)
    assert panel.view.page == 1 and not panel.follow.isChecked()
    QTest.mouseClick(panel.follow, Qt.MouseButton.LeftButton)
    assert panel.view.page == 0 and panel.current_index == 1
    assert_current_measure_visible(panel)
    window.seek_to(12250)
    assert panel.view.page == 1 and panel.current_index == 2
    assert_current_measure_visible(panel)  # A small box near the page edges fits entirely.
    window.seek_to(17250)
    assert panel.current_index == 3 and "2회차" in panel.measure.text()
    assert_current_measure_visible(panel)


@pytest.mark.parametrize("geometry", ["legacy", "invalidated"])
def test_missing_measure_positions_keep_page_follow_and_analysis_hint(resume_score, geometry):
    app, window, path = resume_score
    panel = window.score_panel
    data = deepcopy(panel.timeline.data)
    if geometry == "legacy":
        for frame in data["scroll"]["frames"]:
            frame.pop("box")
    else:
        data.pop("scroll")
    changed = path.with_name(f"{geometry}.json")
    ScoreTimeline(data).save(changed)
    panel.load_timeline(changed)
    panel.tools_toggle.click()
    settle_view(app, panel)
    wheel_score(panel)
    before = panel.view.verticalScrollBar().value()
    QTest.mouseClick(panel.follow, Qt.MouseButton.LeftButton)
    assert panel.view.verticalScrollBar().value() == before
    assert "분석" in panel.view_status.text() and panel.view.label.highlight_box is None
    window.seek_to(12250)
    assert panel.current_index == 2 and panel.view.page == 1
    assert panel.view.verticalScrollBar().value() == 0


def test_replacing_score_refreshes_same_cue_position(resume_score, tmp_path):
    app, window, path = resume_score
    panel = window.score_panel
    panel.tools_toggle.click()
    settle_view(app, panel)
    replacement = blank_pdf(tmp_path / "replacement.pdf", "Replacement study")
    data = deepcopy(panel.timeline.data)
    data["pdf_sha256"] = hash_file(replacement)
    data["scroll"]["frames"][0]["box"] = {
        "left": 0.1, "right": 0.3, "top": 0.8, "bottom": 0.95,
    }
    changed = path.with_name("replacement.json")
    ScoreTimeline(data).save(changed)
    panel.open_pdf(replacement)
    assert panel.timeline is None and panel.view.label.highlight_box is None
    panel.load_timeline(changed)
    assert panel.current_index == 0 and not panel.view_matches()
    assert_current_measure_visible(panel)
