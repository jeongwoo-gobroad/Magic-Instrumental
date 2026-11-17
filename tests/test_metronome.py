# SPDX-License-Identifier: GPL-3.0-only
from PySide6.QtCore import QObject, Signal
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication

from practice_player.metronome import MetronomeBar, beat_number


def test_beat_math_meter_pickup_and_speed():
    assert beat_number(0.5, 120) == 1
    assert beat_number(0.5, 120, 8) == 2
    assert beat_number(1, 120, 4, 2) < 0
    # 107 BPM at 0.75x: 80.25 beats/minute of wall time.
    assert beat_number(60 * 0.75, 107) == 80


class Transport(QObject):
    positionChanged = Signal(int)
    playbackRateChanged = Signal(float)
    playbackStateChanged = Signal(object)

    def __init__(self):
        super().__init__()
        self.pos = 0
        self.rate = 1
        self.playing = False

    def position(self):
        return self.pos

    def playbackRate(self):
        return self.rate

    def playbackState(self):
        return (
            QMediaPlayer.PlaybackState.PlayingState
            if self.playing
            else QMediaPlayer.PlaybackState.PausedState
        )


def test_independent_linked_pause_seek_and_rate(monkeypatch):
    app = QApplication.instance() or QApplication([])
    now = [100.0]
    monkeypatch.setattr("practice_player.metronome.time.monotonic", lambda: now[0])
    transport = Transport()
    bar = MetronomeBar(transport)
    bar.mute.setChecked(True)
    try:
        bar.toggle.setChecked(True)
        now[0] += 0.5
        bar.tick()
        assert bar.last_beat == 1  # independent while audio paused
        bar.linked.setChecked(True)
        bar.bpm.setValue(107)
        transport.rate = 0.75
        transport.playbackRateChanged.emit(0.75)
        assert bar.effective.text() == "80.25 BPM"
        assert bar.bpm.value() == 107
        bar.tick()
        assert bar.last_beat is None
        transport.playing = True
        transport.playbackStateChanged.emit(transport.playbackState())
        now[0] += 60 / 80.25
        bar.tick()
        assert bar.last_beat == 1
        transport.pos = 60000
        transport.positionChanged.emit(60000)
        bar.tick()
        assert bar.last_beat == 107  # seek uses media time, not media time * rate
        transport.playing = False
        transport.playbackStateChanged.emit(transport.playbackState())
        now[0] += 10
        bar.tick()
        assert bar.last_beat is None
        bar.beats.setValue(7)
        assert len(bar.dot_labels) == 7
        bar.linked.setChecked(False)
        bar.tick()
        assert bar.last_beat == 0
        assert bar.effective.text() == "107 BPM"
    finally:
        bar.shutdown()
        bar.close()
        app.processEvents()


def test_metronome_preferences_are_per_track(tmp_path):
    app = QApplication.instance() or QApplication([])
    bar = MetronomeBar(Transport())
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    try:
        bar.bind_track(first)
        bar.bpm.setValue(107)
        bar.beats.setValue(6)
        bar.origin.setValue(1.234)
        bar.bind_track(second)
        assert bar.bpm.value() == 120
        bar.bind_track(first)
        assert bar.bpm.value() == 107 and bar.beats.value() == 6
        assert bar.origin.value() == 1.234
        second.write_text('{"bpm": null}')
        bar.bind_track(second)
        assert bar.bpm.value() == 120
    finally:
        bar.shutdown()
        bar.close()
        app.processEvents()
