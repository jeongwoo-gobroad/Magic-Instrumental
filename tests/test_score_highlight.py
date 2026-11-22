# SPDX-License-Identifier: GPL-3.0-only
"""Current-measure ink stays tied to the displayed page, independently of following."""

from copy import deepcopy

import pytest
from pypdf import PdfWriter
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QTableWidget

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.gui import STYLE
from practice_player.score import ScoreTimeline
from practice_player.score_gui import PdfView, ScorePanel
from practice_player.theme import stylesheet
from tests.test_gui import wait_until

FIRST = {"left": 0.15, "right": 0.55, "top": 0.2, "bottom": 0.35}
SECOND = {"left": 0.45, "right": 0.8, "top": 0.4, "bottom": 0.6}
CODA = {"left": 0.2, "right": 0.4, "top": 0.7, "bottom": 0.9}
PAGE_SIZES = [(595, 842), (842, 595)]


@pytest.fixture
def blank_score(tmp_path):
    path = tmp_path / "highlight-study.pdf"
    writer = PdfWriter()
    for width, height in PAGE_SIZES:
        writer.add_blank_page(width=width, height=height)
    with path.open("wb") as output:
        writer.write(output)
    return path


def dispose(widget, app):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def check_box(label, box):
    assert label.highlight_box == box
    rect, page = label.highlight_rect(), label.contentsRect()
    assert rect.left() == pytest.approx(page.left() + box["left"] * page.width())
    assert rect.top() == pytest.approx(page.top() + box["top"] * page.height())
    assert rect.width() == pytest.approx((box["right"] - box["left"]) * page.width())
    assert rect.height() == pytest.approx((box["bottom"] - box["top"]) * page.height())
    return rect


def pixel_at(label, image, x, y):
    # Qt grabs device pixels; highlight_rect uses logical widget coordinates.
    return image.pixelColor(
        round(x * image.width() / label.width()),
        round(y * image.height() / label.height()),
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_highlight_pixels_scale_without_changing_pdf_pixmap(blank_score, theme):
    app = QApplication.instance() or QApplication([])
    view = PdfView()
    view.setStyleSheet(stylesheet(STYLE, theme))
    view.resize(520, 640)
    view.show()
    try:
        view.zoom = 1.0
        view.load(blank_score)
        QTest.qWait(120)
        label = view.label
        original = label.pixmap().toImage()
        cache_key = label.pixmap().cacheKey()
        view.set_highlight(0, FIRST)
        rect = check_box(label, FIRST)
        image = label.grab().toImage()
        center = rect.center()
        fill = pixel_at(label, image, center.x(), center.y())
        outline = pixel_at(label, image, center.x(), rect.top() + 1)
        outside = pixel_at(label, image, rect.left() - 5, center.y())
        assert 200 < fill.red() < 250 and fill.green() > fill.blue() > fill.red()
        assert outline.green() > outline.red() + 50 and outline.red() < fill.red() - 50
        assert (outside.red(), outside.green(), outside.blue()) == (255, 255, 255)
        assert label.pixmap().cacheKey() == cache_key
        assert label.pixmap().toImage() == original

        view.zoom = 2.0
        view.render()
        enlarged = check_box(label, FIRST)
        assert enlarged.width() == pytest.approx(rect.width() * 2)
        view.zoom = "page"
        view.render()
        before = check_box(label, FIRST)
        view.resize(760, 840)
        wait_until(app, lambda: label.highlight_rect().width() > before.width(), timeout=5)
        assert check_box(label, FIRST).width() > before.width()

        view.show_page(1)
        assert label.highlight_box is None and label.highlight_rect().isEmpty()
        view.show_page(0)
        check_box(label, FIRST)
        view.clear()
        assert label.highlight_box is None and label.highlight_rect().isEmpty()
        assert view.highlight_page is None and view.highlight_box is None
    finally:
        view.clear()
        dispose(view, app)


@pytest.fixture
def shown_score(tmp_path, blank_score, tone):
    app = QApplication.instance() or QApplication([])
    panel = ScorePanel(Paths(tmp_path / "project"))
    panel.paths.create()
    panel.setStyleSheet(stylesheet(STYLE, "light"))
    panel.resize(620, 900)
    panel.show()
    panel.audio, panel.source_hash = tone, hash_file(tone)
    panel.open_pdf(blank_score)
    panel.zoom.setCurrentIndex(panel.zoom.findData(2.0))
    QTest.qWait(120)
    data = {
        "version": 1, "page_count": 2,
        "audio_sha256": hash_file(tone), "pdf_sha256": hash_file(blank_score),
        "offset": 0.25, "end": 9, "reviewed": True,
        "cues": [
            {"start": 1, "page": 0, "source_index": 0, "measure": "1", "visit": 1},
            {"start": 3, "page": 1, "source_index": 1, "measure": "2", "visit": 1},
            {"start": 5, "page": 0, "source_index": 0, "measure": "1", "visit": 2},
            {"start": 7, "page": 1, "source_index": 2, "measure": "3", "visit": 1},
        ],
        "scroll": {
            "version": 1, "profile": panel.view.profile(),
            "page_sizes": PAGE_SIZES,
            "frames": [
                {
                    "page": page, "source_index": source, "system": 0,
                    "x0": 0, "x1": 0, "y0": 0, "y1": 0, "intro_y": 0, "settle": 0,
                    "box": box,
                }
                for page, source, box in [(0, 0, FIRST), (1, 1, SECOND),
                                          (0, 0, FIRST), (1, 2, CODA)]
            ],
        },
    }
    for cue in data["cues"]:
        cue["marks"] = []
    path = tmp_path / "highlight-timeline.json"
    ScoreTimeline(data).save(path)
    panel.load_timeline(path)
    try:
        yield app, panel, path
    finally:
        panel.view.clear()
        dispose(panel, app)


def test_highlight_tracks_seek_repeat_offset_and_manual_page_while_paused(shown_score):
    app, panel, path = shown_score
    label = panel.view.label
    assert label.highlight_box is None
    panel.on_position(2250)
    check_box(label, FIRST)
    panel.follow.setChecked(False)
    panel.on_position(4250)
    assert panel.current_index == 1 and "2마디" in panel.measure.text()
    assert panel.view.page == 0 and label.highlight_box is None

    # No playback tick is available while paused: page changes must update immediately.
    panel.manual_page(2)
    check_box(label, SECOND)
    panel.manual_page(1)
    assert label.highlight_box is None
    panel.manual_page(2)
    check_box(label, SECOND)
    panel.zoom.setCurrentIndex(panel.zoom.findData(3.0))
    assert not panel.view_matches()
    check_box(label, SECOND)
    panel.resize(760, 980)
    QTest.qWait(120)
    check_box(label, SECOND)

    panel.on_position(6250)
    assert label.highlight_box is None and panel.view.page == 1
    panel.follow.setChecked(True)
    assert panel.current_index == 2 and "2회차" in panel.measure.text()
    assert panel.view.page == 0
    check_box(label, FIRST)
    panel.on_position(7250)
    check_box(label, CODA)
    changed = ScoreTimeline.load(path)
    changed.data["offset"] = 1.25
    changed.save(path)
    panel.load_timeline(path)
    assert panel.current_index == 2 and panel.view.page == 0
    check_box(label, FIRST)
    panel.on_position(10250)
    assert panel.current_index is None and label.highlight_box is None
    panel.on_position(2250)
    check_box(label, FIRST)
    panel.clear_score()
    assert panel.timeline is None and label.highlight_box is None


def test_reviewed_page_edit_removes_stale_highlight_geometry(shown_score):
    app, panel, _ = shown_score
    panel.on_position(2250)
    check_box(panel.view.label, FIRST)

    def change_page():
        dialog = app.activeModalWidget()
        table = dialog.findChild(QTableWidget)
        table.item(0, 3).setText("2")
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()

    QTimer.singleShot(0, change_page)
    panel.edit_timeline()
    assert "scroll" not in panel.timeline.data
    assert panel.current_index == 0 and panel.view.page == 1
    assert panel.view.label.highlight_box is None


def test_legacy_scroll_without_boxes_keeps_timing_and_camera(shown_score):
    _, panel, path = shown_score
    legacy = deepcopy(panel.timeline.data)
    for frame in legacy["scroll"]["frames"]:
        frame.pop("box")
    ScoreTimeline(legacy).save(path)
    panel.load_timeline(path)
    panel.on_position(6250)
    assert panel.current_index == 2 and panel.view.page == 0
    assert panel.view_matches() and "2회차" in panel.measure.text()
    assert panel.view.label.highlight_box is None
