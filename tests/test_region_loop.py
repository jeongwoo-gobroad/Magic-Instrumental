# SPDX-License-Identifier: GPL-3.0-only
import time
from collections import deque
from copy import deepcopy

import pytest
from pypdf import PdfWriter
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QSettings, Qt, QTimer
from PySide6.QtWidgets import QApplication

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.count_in import CountClock
from practice_player.gui import Window
from practice_player.region_loop import RegionLoop, timing_signature
from practice_player.score import ScoreTimeline
from tests.test_measure_navigation import performed_score


def wait_until(_app, condition, timeout=20, *, trace=()):
    if condition():
        return
    loop, poll, deadline = QEventLoop(), QTimer(), QTimer()
    poll.setTimerType(Qt.TimerType.PreciseTimer)
    poll.setInterval(5)
    poll.timeout.connect(lambda: loop.quit() if condition() else None)
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    try:
        poll.start()
        deadline.start(round(timeout * 1000))
        loop.exec()
    finally:
        poll.stop()
        deadline.stop()
    assert condition(), list(trace)


@pytest.fixture
def count_trace(loop_window, monkeypatch):
    _, window, _ = loop_window
    trace, last_ticks = deque(maxlen=48), {}
    started = time.monotonic()
    advance = CountClock.advance

    def record(event, now, **details):
        trace.append({"event": event, "ms": round((now - started) * 1000, 3), **details})

    def observed(clock, now):
        previous = last_ticks.get(clock, now)
        last_ticks[clock] = now
        details = {
            "elapsed_ms": round((now - clock.started) * 1000, 3),
            "gap_ms": round((now - previous) * 1000, 3),
            "handled": clock.handled, "next_beat": clock.handled + 1,
            "period_ms": round(clock.period * 1000, 3),
            "duration_ms": round(clock.duration * 1000, 3), "total": clock.total,
        }
        try:
            result = advance(clock, now)
        except RuntimeError:
            record("count-failed", now, **details)
            raise
        if result[0] is not None or result[1]:
            record("count-finished" if result[1] else "count-beat", now, **details)
        return result

    monkeypatch.setattr(CountClock, "advance", observed)
    window.player.mediaStatusChanged.connect(
        lambda status: record("media", time.monotonic(), status=status.name,
                              position=window.player.position())
    )
    window.count_in.active_changed.connect(
        lambda active: record("count-active", time.monotonic(), active=active,
                              position=window.player.position())
    )
    return trace


def test_range_includes_whole_last_measure_and_distinguishes_repeat_visits():
    plan = performed_score()
    assert plan.measure_range(0, 2, 10) == (125, 3125)
    assert plan.measure_range(3, 4, 10) == (3125, 6375)
    assert plan.measure_range(5, 5, 10) == (6375, 7625)
    assert plan.measure_range(2, 3, 4) == (1875, 4000)
    for first, last in [(3, 2), (-1, 1), (0, 6), (True, 1)]:
        with pytest.raises(ValueError):
            plan.measure_range(first, last, 10)
    for duration in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            plan.measure_range(0, 0, duration)
    data = deepcopy(plan.data)
    data["offset"] = -3.25
    assert ScoreTimeline(data).measure_range(3, 4, 3) == (0, 3000)
    assert ScoreTimeline(data).measure_range(3, 5, 3.1) == (0, 3100)
    with pytest.raises(ValueError):
        ScoreTimeline(data).measure_range(3, 5, 3)


def test_range_rounding_and_signature_excludes_camera_but_includes_timing():
    plan = performed_score()
    changed = deepcopy(plan.data)
    changed["cues"][1]["start"] = 0.5001
    adjusted = ScoreTimeline(changed)
    assert adjusted.measure_range(1, 1, 10) == (626, 1875)
    assert timing_signature(plan) != timing_signature(adjusted)
    changed = deepcopy(plan.data)
    changed["pdf_layout"] = {"diagnostics": ["new viewport"]}
    changed["cues"][0]["page"] = 1
    assert timing_signature(plan) == timing_signature(ScoreTimeline(changed))


def test_selection_restores_for_same_score_but_never_auto_enables(tmp_path):
    app = QApplication.instance() or QApplication([])
    preferences = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    panel = RegionLoop(preferences)
    path = tmp_path / "practice_loop.json"
    plan = performed_score()
    try:
        panel.bind_track(path)
        assert not panel.enabled.isEnabled()
        panel.set_context(plan, 10, True)
        panel.first.setCurrentIndex(3)
        panel.last.setCurrentIndex(4)
        assert "2회차" in panel.first.currentText()
        panel.enabled.setChecked(True)
        assert panel.bounds() == (3125, 6375)
        # Camera-only context refresh retains the active range.
        panel.set_context(ScoreTimeline(deepcopy(plan.data)), 10, True)
        assert panel.enabled.isChecked()
        panel.bind_track(path)
        panel.set_context(plan, 10, True)
        assert panel.first.currentData() == 3 and panel.last.currentData() == 4
        assert not panel.enabled.isChecked()
        panel.last.setCurrentIndex(0)
        assert not panel.enabled.isEnabled()
        assert "순서대로" in panel.hint.text()
        changed = deepcopy(plan.data)
        changed["offset"] += 0.2
        panel.set_context(ScoreTimeline(changed), 10, True)
        assert panel.first.currentData() == 0 and panel.last.currentData() == 5
        panel.set_context(None, 10, True)
        assert not panel.enabled.isEnabled() and panel.bounds() is None
    finally:
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.fixture
def loop_window(tmp_path, tone):
    app = QApplication.instance() or QApplication([])
    paths = Paths(tmp_path / "project")
    paths.create()
    pdf = paths.score_targets / f"{tone.stem}.pdf"
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=595, height=842)
    with pdf.open("wb") as stream:
        writer.write(stream)
    window = Window(paths)
    window.pending_source, window.source_name = tone, tone.name
    window.load_result({"instrumental": str(tone), "source_hash": hash_file(tone), "cached": True})
    window.audio_output.setVolume(0)
    window.metronome.mute.setChecked(True)
    # Keep native transport checks at a normal tempo; fractional handoff and
    # stall rejection have deterministic clock coverage in test_count_in.
    window.metronome.bpm.setValue(120)
    window.metronome.beats.setValue(4)
    wait_until(app, lambda: window.player.duration() > 0 and window.player.isSeekable())
    data = deepcopy(performed_score().data)
    for cue, start in zip(data["cues"], (0.1, 0.5, 0.95, 1.4, 1.95, 2.5)):
        cue["start"] = start
    data.update(offset=0, end=3.0, audio_sha256=hash_file(tone), pdf_sha256=hash_file(pdf))
    path = tmp_path / "timeline.json"
    ScoreTimeline(data).save(path)
    window.score_panel.load_timeline(path)
    try:
        yield app, window, path
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_gui_range_clamps_seeks_and_whole_track_loop_is_exclusive(loop_window):
    _, window, _ = loop_window
    region = window.region
    region.first.setCurrentIndex(3)
    region.last.setCurrentIndex(4)
    region.enabled.setChecked(True)
    assert window.player.position() == 1400 and not window.player.isPlaying()
    assert region.bounds() == (1400, 2500)
    window.seek_to(0)
    assert window.player.position() == 1400
    window.seek_to(99999)
    assert window.player.position() == 2499
    window.seek_to(2000)
    region.current_first.click()
    assert region.first.currentData() == 4 and not region.enabled.isChecked()
    region.enabled.setChecked(True)
    window.loop.setChecked(True)
    assert not region.enabled.isChecked() and window.player.loops() == -1
    region.enabled.setChecked(True)
    assert not window.loop.isChecked() and window.player.loops() == 1


@pytest.mark.parametrize("count_each", [False, True])
def test_repeated_ranges_optionally_count_independent_of_manual_setting(
    loop_window, monkeypatch, count_each, count_trace,
):
    app, window, _ = loop_window
    window.speed.setValue(200)
    # 100 ms 진입도 첫 박에 맞춰 마지막 카운트가 50 ms 안에 잘리지 않게 한다.
    window.metronome.origin.setValue(0.1)
    window.region.last.setCurrentIndex(0)
    window.region.count_each.setChecked(count_each)
    # Opposite options prove manual and per-pass count settings are independent.
    window.count_in.enabled.setChecked(not count_each)
    window.region.enabled.setChecked(True)
    assert window.count_in.bars.isEnabled()
    counts, restarts = [], []
    window.count_in.active_changed.connect(
        lambda active: counts.append(window.player.position()) if active else None
    )
    original = window.restart_region

    def restart():
        restarts.append(window.player.position())
        original()

    monkeypatch.setattr(window, "restart_region", restart)
    failures = []
    window.count_in.failed.connect(failures.append)
    window.toggle_play()
    wait_until(app, lambda: len(restarts) >= 2 or bool(failures), timeout=10, trace=count_trace)
    assert not failures, {"failures": failures, "trace": list(count_trace)}
    assert counts == ([100, 100, 100] if count_each else [100])
    assert window.count_in.enabled.isChecked() != count_each
    if window.count_in.active:
        assert not window.player.isPlaying()
        window.toggle_play()
        assert not window.count_in.active and not window.player.isPlaying()
    else:
        window.toggle_play()
        assert not window.player.isPlaying()
    assert window.player.playbackRate() == 2


@pytest.mark.parametrize("count_each", [False, True])
def test_last_measure_repeats_after_native_end_of_media(
    loop_window, monkeypatch, count_each, count_trace,
):
    app, window, _ = loop_window
    window.speed.setValue(200)
    window.region.first.setCurrentIndex(5)
    window.region.count_each.setChecked(count_each)
    window.region.enabled.setChecked(True)
    window.region_timer.stop()  # Exercise native EOF fallback, not predicted timer cutoff.
    restarts = []
    original = window.restart_region

    def restart():
        restarts.append(window.player.position())
        original()

    monkeypatch.setattr(window, "restart_region", restart)
    failures = []
    window.count_in.failed.connect(failures.append)
    window.toggle_play()
    wait_until(app, lambda: len(restarts) >= 1 or bool(failures), timeout=10, trace=count_trace)
    assert not failures, {"failures": failures, "trace": list(count_trace)}
    if count_each:
        assert window.count_in.active and window.player.position() == 2500
        wait_until(app, lambda: window.player.isPlaying() or bool(failures), timeout=5,
                   trace=count_trace)
        assert not failures, {"failures": failures, "trace": list(count_trace)}
    assert window.player.position() >= 2500
    assert window.player.isPlaying()


def test_timing_edits_jobs_and_mode_changes_cancel_pending_loop_count(loop_window):
    app, window, path = loop_window
    window.region.last.setCurrentIndex(0)
    window.region.count_each.setChecked(True)
    window.region.enabled.setChecked(True)
    window.count_in.bars.setValue(4)
    window.toggle_play()
    assert window.count_in.active
    window.region.count_each.setChecked(False)
    assert not window.count_in.active and not window.player.isPlaying()
    window.region.count_each.setChecked(True)
    window.toggle_play()
    data = deepcopy(window.score_panel.timeline.data)
    data["offset"] = 0.05
    ScoreTimeline(data).save(path)
    window.score_panel.load_timeline(path)
    assert not window.region.enabled.isChecked() and not window.count_in.active
    window.region.enabled.setChecked(True)
    window.toggle_play()
    assert window.count_in.active
    window.set_busy(True)
    assert not window.region.enabled.isEnabled() and not window.count_in.active
    window.set_busy(False)
    assert window.region.enabled.isEnabled() and not window.region.enabled.isChecked()
    generation = window._region_generation
    window.region_eof(generation - 1)
    app.processEvents()
    assert not window.player.isPlaying()


def test_region_count_preference_restores_without_turning_on_loop(tmp_path):
    app = QApplication.instance() or QApplication([])
    paths = Paths(tmp_path)
    window = Window(paths)
    window.region.count_each.setChecked(True)
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    reopened = Window(paths)
    try:
        assert reopened.region.count_each.isChecked()
        assert not reopened.region.enabled.isChecked() and not reopened.region_timer.isActive()
    finally:
        reopened.close()
        reopened.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
