# SPDX-License-Identifier: GPL-3.0-only
"""Use real PDF/GP geometry and Qt transport to verify continuous centred following."""

import os
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QAbstractAnimation, QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.gp_render import native_layout, prepare_gp
from practice_player.gui import STYLE, Window
from practice_player.score import ScoreTimeline
from practice_player.score_layout import analyze_layout, plan_camera
from tests.test_gui import wait_until
from tests.test_guitar_pro import gp_song
from tests.test_score_follow_resume import assert_current_measure_visible, settle_view, wheel_score
from tests.test_score_geometry import vector_pdf


@pytest.fixture(params=["pdf", "gp"])
def continuous_score(request, tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    paths = Paths(tmp_path / "project")
    paths.create()
    audio = paths.targets / "center-study.wav"
    time = np.arange(8000 * 70) / 8000
    sf.write(audio, 0.02 * np.sin(2 * np.pi * 220 * time), 8000)
    window = Window(paths)

    def dispose():
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()

    request.addfinalizer(dispose)
    window.resize(1300, 930)
    window.show()
    window.source_path, window.source_name = audio, audio.name
    window.load_result({"instrumental": str(audio), "cached": True})
    window.audio_output.setVolume(0)
    panel = window.score_panel
    panel.bind_track(audio, hash_file(audio))
    if request.param == "gp":
        def frets(song):
            song.title = "Continuous Score Study"
            for index, measure in enumerate(song.tracks[0].measures):
                measure.voices[0].beats[0].notes[0].value = index % 12

        source = gp_song(paths.score_targets / "center.gp5", count=120, configure=frets)
        info = prepare_gp(source, paths)
        sizes, bars, _ = native_layout(info)
        panel.accept_gp(info)
    else:
        source = vector_pdf(paths.score_targets / "center.pdf", [
            ((595, 842), [(y, [50, 290, 550], []) for y in (730, 500, 270, 65)], [5]),
        ] * 6)
        sizes, bars, _ = analyze_layout(source)
        panel.open_pdf(source)
    panel.tools_toggle.click()
    panel.zoom.setCurrentIndex(panel.zoom.findData("width"))
    settle_view(app, panel)
    assert len(sizes) >= 3
    cues = [
        {"start": index * .5, "page": bar["page"], "source_index": index,
         "measure": str(index + 1), "visit": 1, "marks": []}
        for index, bar in sorted(bars.items())
    ]
    # A final repeat and coda exercise non-consecutive source navigation as well.
    for source_index in (0, len(bars) - 1):
        cues.append({
            "start": len(cues) * .5, "page": bars[source_index]["page"],
            "source_index": source_index, "measure": str(source_index + 1),
            "visit": 2, "marks": [],
        })
    data = {
        "version": 1, "page_count": len(sizes), "offset": .125,
        "end": len(cues) * .5, "reviewed": True,
        "audio_sha256": hash_file(audio), "pdf_sha256": hash_file(panel.pdf), "cues": cues,
        "scroll": plan_camera(cues, len(cues) * .5, sizes, bars, panel.view.profile()),
    }
    timeline = paths.processed / "center-timeline.json"
    ScoreTimeline(data).save(timeline)
    panel.load_timeline(timeline)
    wait_until(app, window.player.isSeekable)
    settle_view(app, panel)
    protected = {path: hash_file(path) for path in (source, audio, panel.pdf, timeline)}

    def no_analysis(*_args, **_kwargs):
        pytest.fail("Following must use existing geometry without recognition or realignment")

    monkeypatch.setattr("practice_player.score_geometry.detect_pdf_layout", no_analysis)
    monkeypatch.setattr("practice_player.score_layout.analyze_layout", no_analysis)
    monkeypatch.setattr("practice_player.score_layout.plan_camera", no_analysis)
    monkeypatch.setattr("practice_player.gp_render.native_layout", no_analysis)
    yield app, window, request.param
    assert all(hash_file(path) == digest for path, digest in protected.items())


def seek_cue(window, index, within=.1):
    panel = window.score_panel
    window.seek_to(round((panel.timeline.starts[index] + .125 + within) * 1000))
    assert panel.current_index == index
    assert_current_measure_visible(panel)


def test_pdf_gp_first_last_and_page_boundary_remain_centred(continuous_score):
    app, window, kind = continuous_score
    panel, view = window.score_panel, window.score_panel.view
    panel.zoom.setCurrentIndex(panel.zoom.findData("page"))
    settle_view(app, panel)
    seek_cue(window, 0)
    assert view.labels[0].y() >= view.viewport().height() // 2
    boundary = next(i for i, cue in enumerate(panel.timeline.cues) if cue["page"] == 1) - 1
    seek_cue(window, boundary)
    app.processEvents()
    current, following = view.labels[0], view.labels[1]
    assert following.y() - (current.y() + current.height()) == 16
    assert following.y() - view.verticalScrollBar().value() < view.viewport().height()
    assert not following.pixmap().isNull()
    assert panel.follow.isChecked() and panel.highlight.isChecked()
    # Both pages really paint into the viewport before the page boundary.
    image = view.viewport().grab().toImage()
    next_top = following.y() - view.verticalScrollBar().value()
    pixel = image.pixelColor(
        round(image.width() / 2), round((next_top + 2) * image.height() / view.viewport().height()),
    )
    assert min(pixel.red(), pixel.green(), pixel.blue()) > 240
    destination = os.environ.get("MAGIC_RENDER_CHECK_DIR")
    if destination:
        folder = Path(destination)
        folder.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(folder / f"center-follow-{kind}-{sys.platform}.png"))

    last = len(panel.timeline.cues) - 3
    seek_cue(window, last)
    app.processEvents()
    assert len(view.cache) <= 3
    assert view.labels[0].pixmap().isNull()
    assert view.surface.height() - (view.label.y() + view.label.height()) >= (
        view.viewport().height() // 2
    )
    view_height = view.viewport().height()
    for zoom in (3.0, "page"):
        panel.zoom.setCurrentIndex(panel.zoom.findData(zoom))
        settle_view(app, panel)
        assert view.viewport().height() == view_height
        assert_current_measure_visible(panel)
        assert not panel.view_matches()
    # Highlighting off never changes either vertical following or the loaded timeline.
    panel.highlight.click()
    seek_cue(window, boundary)
    assert view.label.highlight_box is None
    panel.highlight.click()
    assert view.label.highlight_box == panel.timeline.data["scroll"]["frames"][boundary]["box"]
    # Repeated visits and the forward coda jump select their actual printed positions.
    seek_cue(window, last + 1)
    assert view.page == 0 and "2회차" in panel.measure.text()
    seek_cue(window, last + 2)
    assert view.page == panel.timeline.cues[last]["page"]


def test_native_playback_eases_forward_then_manual_scroll_cancels(continuous_score):
    app, window, _ = continuous_score
    panel, view = window.score_panel, window.score_panel.view
    frames = panel.timeline.data["scroll"]["frames"]
    transition = next(
        index for index in range(1, len(frames))
        if frames[index]["page"] == frames[index - 1]["page"]
        and frames[index]["system"] != frames[index - 1]["system"]
    )
    boundary = panel.timeline.starts[transition] + .125
    window.seek_to(round((boundary - .12) * 1000))
    start = view.verticalScrollBar().value()
    window.player.play()
    wait_until(app, lambda: view.slide.state() == QAbstractAnimation.State.Running, timeout=5)
    target = view.slide.endValue()
    assert start < target
    wait_until(app, lambda: start < view.verticalScrollBar().value() < target, timeout=1)
    wheel_score(panel)
    assert view.slide.state() == QAbstractAnimation.State.Stopped
    manual = view.verticalScrollBar().value()
    QTest.qWait(100)
    assert view.verticalScrollBar().value() == manual
    QTest.mouseClick(panel.follow, Qt.MouseButton.LeftButton)
    assert view.slide.state() == QAbstractAnimation.State.Stopped
    assert window.player.isPlaying()
    assert_current_measure_visible(panel)
    # Explicit in-playback seeks must never animate across unrelated staves or pages.
    seek_cue(window, len(panel.timeline.cues) - 2)
    assert view.slide.state() == QAbstractAnimation.State.Stopped and view.page == 0
    window.player.pause()
    assert_current_measure_visible(panel)
    assert not panel.camera_timer.isActive()


def test_late_page_render_failure_keeps_loaded_score_and_timing(continuous_score, monkeypatch):
    app, window, _ = continuous_score
    panel, view = window.score_panel, window.score_panel.view
    timeline, position = panel.timeline, window.player.position()
    page = view.document[0]
    page_type = type(page)
    page.close()
    failures = []

    def failed_render(*_args, **_kwargs):
        failures.append(True)
        raise RuntimeError("Synthetic late page-render failure")

    monkeypatch.setattr(page_type, "render", failed_render)
    panel.manual_page(len(view.document))
    app.processEvents()
    assert failures and "표시 실패" in panel.status.text()
    assert "다시 여세요" in view.label.text()
    assert panel.timeline is timeline and window.player.position() == position
    assert not panel.follow.isChecked()
    before = len(failures)
    QTest.qWait(50)
    assert len(failures) == before  # Do not retry a bad page on every playback tick.
