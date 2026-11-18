# SPDX-License-Identifier: GPL-3.0-only
import shutil
import time

from PySide6.QtWidgets import QApplication

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
    (tmp_path / "main.py").write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr("practice_player.gui.ROOT", tmp_path)
    window = Window(Paths(tmp_path))
    window.start_job([])
    wait_until(app, lambda: window.worker.processId() != 0)
    window.cancel_job()
    wait_until(app, lambda: window.worker is None, timeout=6)
    assert window.cancelled and window.prepare_button.isEnabled()
    assert "취소" in window.status.text()
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
        assert bar.volume.value() == 75 and all(sound.volume() == 0 for sound in bar.sounds)
        bar.volume.setValue(80)
        assert all(sound.volume() == 0 for sound in bar.sounds)
        bar.mute.setChecked(False)
        assert all(abs(sound.volume() - 0.8) < 0.001 for sound in bar.sounds)
        bar.volume.setValue(0)
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
