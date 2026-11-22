# SPDX-License-Identifier: GPL-3.0-only
import json
import shutil
import sys
import time
from collections import deque

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication

from app_guard import app_guard
from practice_player.config import Paths, Settings
from practice_player.gui import STYLE, Window
from practice_player.pipeline import process


def wait_until(app, condition, timeout=20):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert condition()


def test_gui_cache_worker_playback_controls_and_export(tone, tmp_path, fake_engine):
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    paths = Paths(tmp_path / "project")
    paths.create()
    source = paths.targets / tone.name
    shutil.copyfile(tone, source)
    result = process(source, Settings(), paths, runner=fake_engine)
    window = Window(paths)
    window.show()
    try:
        assert window.tracks.count() == 1
        window.prepare()
        assert not window.prepare_button.isEnabled()
        wait_until(app, lambda: window.worker is None)
        assert window.output_path == result.instrumental
        assert "캐시" in window.status.text()
        wait_until(app, lambda: window.player.duration() > 0)
        assert window.clock.text() == "00:00 / 00:03"
        assert window.pitch.isChecked()
        window.speed.setValue(50)
        assert window.player.playbackRate() == 0.5
        window.player.setPosition(1000)
        assert window.player.position() == 1000
        window.audio_output.setVolume(0)
        window.toggle_play()
        wait_until(app, lambda: window.player.position() > 1100)
        window.player.pause()
        output = tmp_path / "exported.wav"
        window.start_job(
            [
                "export",
                "--input",
                str(result.instrumental),
                "--output",
                str(output),
                "--speed",
                "0.5",
                "--events",
            ]
        )
        wait_until(app, lambda: window.worker is None)
        assert output.is_file() and "저장 완료" in window.status.text()
    finally:
        window.close()
        app.processEvents()


def test_cancel_stops_worker_and_leaves_ui_usable(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    if sys.platform == "win32":
        from practice_player.config import ROOT

        # A native helper may outlive the venv launcher unless it belongs to the same job.
        child = (
            "from pathlib import Path\nimport sys,time\n"
            f"sys.path.insert(0, {str(ROOT)!r})\nfrom app_guard import app_guard\n"
            "with app_guard(Path.cwd()):\n Path('ready').touch()\n time.sleep(30)\n"
        )
        script = (
            "import os,sys,subprocess\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from practice_player.windows_job import join_worker_job\n"
            "join_worker_job(os.environ.pop('MAGIC_WORKER_JOB'))\n"
            f"subprocess.run([sys.executable, '-c', {child!r}])\n"
        )
    else:
        script = "from pathlib import Path\nimport time\nPath('ready').touch()\ntime.sleep(30)\n"
    (tmp_path / "main.py").write_text(script, encoding="utf-8")
    monkeypatch.setattr("practice_player.gui.ROOT", tmp_path)
    window = Window(Paths(tmp_path))
    window.start_job([])
    wait_until(app, lambda: (tmp_path / "ready").exists())
    window.cancel_button.click()
    wait_until(app, lambda: window.worker is None, timeout=6)
    assert window.cancelled and window.prepare_button.isEnabled()
    assert "취소" in window.status.text()
    if sys.platform == "win32":
        def child_released_lock():
            try:
                with app_guard(tmp_path, updating=True):
                    return True
            except RuntimeError:
                return False

        wait_until(app, child_released_lock, timeout=3)
    window.close()


def test_click_volume_is_independent_persistent_and_does_not_restart_beats(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    now = [100.0]
    monkeypatch.setattr("practice_player.metronome.time.monotonic", lambda: now[0])
    paths = Paths(tmp_path)
    window = Window(paths)
    try:
        bar = window.metronome
        assert bar.volume.value() == 35
        assert all(abs(sound.volume() - 0.35) < 0.001 for sound in bar.sounds)
        window.audio_output.setVolume(0.2)
        bar.toggle.setChecked(True)
        now[0] += 0.5
        bar.tick()
        assert bar.last_beat == 1
        bar.volume.setValue(75)
        assert abs(window.audio_output.volume() - 0.2) < 0.001
        assert all(abs(sound.volume() - 0.75) < 0.001 for sound in bar.sounds)
        now[0] += 0.5
        bar.tick()
        assert bar.last_beat == 2  # changing gain did not restart the clock
        bar.mute.setChecked(True)
        assert bar.state_label.text() == "단독 재생 중 · 무음"
        assert bar.volume.value() == 75 and all(sound.volume() == 0 for sound in bar.sounds)
        bar.volume.setValue(80)
        assert all(sound.volume() == 0 for sound in bar.sounds)
        bar.mute.setChecked(False)
        assert all(abs(sound.volume() - 0.8) < 0.001 for sound in bar.sounds)
        bar.volume.setValue(0)
        assert bar.state_label.text() == "단독 재생 중 · 음량 0%"
        now[0] += 0.5
        bar.tick()
        assert bar.last_beat == 3 and all(sound.volume() == 0 for sound in bar.sounds)
        bar.volume.setValue(80)
        bar.bind_track(tmp_path / "first.json")
        bar.bind_track(tmp_path / "second.json")
        assert bar.volume.value() == 80
    finally:
        window.close()
        app.processEvents()
    reopened = Window(paths)
    try:
        assert reopened.metronome.volume.value() == 80
        assert all(abs(s.volume() - 0.8) < 0.001 for s in reopened.metronome.sounds)
        assert not reopened.metronome.enabled
    finally:
        reopened.close()
        app.processEvents()


def test_real_player_count_in_delays_play_cancels_and_remembers_mode(tone, tmp_path):
    app = QApplication.instance() or QApplication([])
    paths = Paths(tmp_path)
    window = Window(paths)
    count, failures, events = window.count_in, [], deque(maxlen=16)
    started = time.monotonic()

    def record(event):
        events.append({
            "event": event, "ms": round((time.monotonic() - started) * 1000, 3),
            "count_active": count.active, "player": window.player.playbackState().name,
            "media": window.player.mediaStatus().name, "position_ms": window.player.position(),
        })

    count.failed.connect(failures.append)
    count.failed.connect(lambda _: record("count_failed"))
    count.active_changed.connect(lambda _: record("count_active"))
    count.finished.connect(lambda: record("count_finished"))
    window.player.playbackStateChanged.connect(lambda _: record("playback"))
    window.player.mediaStatusChanged.connect(lambda _: record("media"))

    def wait_native(condition, timeout=5):
        def done():
            return bool(failures) or condition()

        if not done():
            loop, poll, deadline = QEventLoop(), QTimer(), QTimer()
            poll.setTimerType(Qt.TimerType.PreciseTimer)
            poll.setInterval(5)
            poll.timeout.connect(lambda: loop.quit() if done() else None)
            deadline.setSingleShot(True)
            deadline.timeout.connect(loop.quit)
            try:
                poll.start()
                deadline.start(round(timeout * 1000))
                loop.exec()
            finally:
                poll.stop()
                deadline.stop()
        assert not failures and condition(), json.dumps({
            "failures": failures, "count_active": count.active, "count_label": count.label.text(),
            "play_button": window.play_button.text(), "status": window.status.text(),
            "player": window.player.playbackState().name, "media": window.player.mediaStatus().name,
            "position_ms": window.player.position(), "duration_ms": window.player.duration(),
            "player_error": window.player.errorString(), "events": list(events),
        }, ensure_ascii=False)

    try:
        window.load_result({"instrumental": str(tone), "cached": True})
        wait_native(lambda: window.player.duration() > 0, timeout=20)
        window.audio_output.setVolume(0)
        window.metronome.mute.setChecked(True)
        window.metronome.beats.setValue(1)
        # Test native transport at practice tempo; test_count_in covers the fast clock boundary.
        window.metronome.bpm.setValue(120)
        assert not count.enabled.isChecked()
        count.enabled.setChecked(True)
        window.toggle_play()
        assert count.active and window.player.position() == 0
        assert window.metronome.state_label.text() == "드럼 카운트 중 · 무음"
        assert window.play_button.text() == "카운트 취소"
        assert window.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
        wait_native(window.player.isPlaying)
        assert not count.active and window.play_button.text() == "일시정지"
        wait_native(lambda: window.player.position() > 20)
        window.toggle_play()
        assert window.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
        window.toggle_play()
        assert count.active
        window.seek_to(1000)
        assert not count.active and window.player.position() == 1000
        assert window.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
        window.toggle_play()
        window.toggle_play()  # pressing the same button again cancels pending playback
        assert not count.active and not window.player.isPlaying()
        count.enabled.setChecked(False)
        window.toggle_play()
        wait_native(window.player.isPlaying)
        window.player.pause()
        count.enabled.setChecked(True)
        count.bars.setValue(2)
        window.toggle_play()
        window.set_busy(True)
        assert not count.active
        window.set_busy(False)
        window.toggle_play()
        assert count.active
    finally:
        window.close()
        app.processEvents()
    assert not window.count_in.active and not window.player.isPlaying()
    assert window.player.source().isEmpty()
    reopened = Window(paths)
    try:
        assert reopened.count_in.enabled.isChecked() and reopened.count_in.bars.value() == 2
        assert not reopened.count_in.active
        assert reopened.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
    finally:
        reopened.close()
        app.processEvents()
