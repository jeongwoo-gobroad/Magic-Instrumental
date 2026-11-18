# SPDX-License-Identifier: GPL-3.0-only
from PySide6.QtCore import QObject, Signal
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication

from practice_player.metronome import ClickScheduler, MetronomeBar, beat_number


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
    mediaStatusChanged = Signal(object)

    def __init__(self):
        super().__init__()
        self.pos = 0
        self.rate = 1
        self.playing = False

    def position(self):
        return self.pos

    def playbackRate(self):
        return self.rate

    def duration(self):
        return 180000

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


class RecordingClick:
    def __init__(self):
        self.played = 0
        self.stopped = 0

    def play(self):
        self.played += 1

    def stop(self):
        self.stopped += 1

    def setSource(self, _):
        pass


def test_click_scheduler_rejects_midbeat_and_duplicate_transport_samples():
    clock = ClickScheduler()
    assert clock.advance(0.15, 120) == (0, False)
    assert clock.advance(0.503, 120) == (1, True)
    assert clock.advance(0.49, 120) == (0, False)
    assert clock.advance(0.507, 120) == (1, False)
    assert clock.advance(4.2, 120) == (8, False)  # late stall: no queued clicks
    assert clock.advance(4.501, 120) == (9, True)
    clock.reset()
    assert clock.advance(1.5, 120) == (3, True)  # backward seek starts new segment
    assert clock.advance(1.5, 120) == (3, False)
    clock.reset()
    assert clock.advance(-0.001, 120) == (-1, False)
    assert clock.advance(0.001, 120) == (0, True)
    assert clock.advance(-0.001, 120) == (-1, False)
    assert clock.advance(0.001, 120) == (0, False)


def setup_clock(monkeypatch):
    app = QApplication.instance() or QApplication([])
    now = [100.0]
    monkeypatch.setattr("practice_player.metronome.time.monotonic", lambda: now[0])
    transport = Transport()
    bar = MetronomeBar(transport)
    bar.sounds = [RecordingClick(), RecordingClick()]
    return app, now, transport, bar


def seek_clock(bar, transport, milliseconds):
    bar.begin_seek()
    transport.pos = milliseconds
    transport.positionChanged.emit(milliseconds)
    bar.end_seek(milliseconds)


def start_clock(transport):
    transport.playing = True
    transport.playbackStateChanged.emit(transport.playbackState())


def test_link_checkbox_arms_start_and_first_beat_offset(monkeypatch):
    app, now, transport, bar = setup_clock(monkeypatch)
    try:
        bar.origin.setValue(1.25)
        bar.linked.setChecked(True)
        assert bar.enabled and bar.toggle.isChecked()
        assert sum(s.played for s in bar.sounds) == 0
        start_clock(transport)
        now[0] += 1
        bar.tick()
        assert sum(s.played for s in bar.sounds) == 0
        now[0] += 0.25
        bar.tick()
        assert bar.last_beat == 0 and bar.sounds[1].played == 1
        now[0] += 0.5
        bar.tick()
        assert bar.last_beat == 1 and bar.sounds[0].played == 1
        bar.toggle.setChecked(False)
        transport.playing = False
        transport.playbackStateChanged.emit(transport.playbackState())
        start_clock(transport)
        assert not bar.enabled  # respect an explicit click mute/stop
    finally:
        bar.shutdown()
        bar.close()
        app.processEvents()


def test_pause_resume_and_clock_corrections_do_not_double_click(monkeypatch):
    app, now, transport, bar = setup_clock(monkeypatch)
    try:
        bar.linked.setChecked(True)
        start_clock(transport)
        assert bar.sounds[1].played == 1
        transport.playing = False
        transport.playbackStateChanged.emit(transport.playbackState())
        now[0] += 10
        bar.tick()
        start_clock(transport)
        assert bar.sounds[1].played == 1
        now[0] += 0.5
        bar.tick()
        assert bar.sounds[0].played == 1
        transport.pos = 490  # ordinary position sample just before the clicked beat
        transport.positionChanged.emit(490)
        bar.tick()
        now[0] += 0.01
        bar.tick()
        assert bar.sounds[0].played == 1
        now[0] += 0.5
        bar.tick()
        assert bar.sounds[0].played == 2
    finally:
        bar.shutdown()
        bar.close()
        app.processEvents()


def test_forward_backward_same_beat_seeks_and_loop_keep_meter(monkeypatch):
    app, now, transport, bar = setup_clock(monkeypatch)
    try:
        bar.linked.setChecked(True)
        start_clock(transport)
        seek_clock(bar, transport, 4250)
        assert bar.last_beat == 8 and sum(s.played for s in bar.sounds) == 1
        now[0] += 0.25
        bar.tick()
        assert bar.last_beat == 9 and bar.sounds[0].played == 1
        seek_clock(bar, transport, 1500)
        assert bar.last_beat == 3 and bar.sounds[0].played == 2
        seek_clock(bar, transport, 1510)
        assert bar.sounds[0].played == 2
        seek_clock(bar, transport, 1500)
        assert bar.sounds[0].played == 3
        transport.pos = 0  # QMediaPlayer loop, without the explicit GUI seek wrapper
        transport.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.BufferedMedia)
        transport.positionChanged.emit(0)
        bar.tick()
        assert bar.last_beat == 0 and bar.sounds[1].played == 2
    finally:
        bar.shutdown()
        bar.close()
        app.processEvents()


def test_rate_changes_stalls_and_align_now_use_original_media_time(monkeypatch):
    app, now, transport, bar = setup_clock(monkeypatch)
    try:
        bar.linked.setChecked(True)
        start_clock(transport)
        now[0] += 0.25
        transport.pos = 250
        transport.rate = 2
        transport.playbackRateChanged.emit(2)
        now[0] += 0.125
        bar.tick()
        assert bar.last_beat == 1 and bar.sounds[0].played == 1
        seek_clock(bar, transport, 6500)
        assert bar.last_beat == 13 and bar.sounds[0].played == 2
        transport.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.StalledMedia)
        now[0] += 10
        bar.tick()
        assert sum(s.played for s in bar.sounds) == 3
        transport.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.BufferedMedia)
        bar.tick()
        assert sum(s.played for s in bar.sounds) == 3
        now[0] += 0.125
        bar.align.click()
        assert bar.origin.value() == 6.75
        bar.tick()
        assert bar.last_beat == 0 and bar.sounds[1].played == 2
    finally:
        bar.shutdown()
        bar.close()
        app.processEvents()


def test_independent_clock_ignores_transport_seek_and_stall(monkeypatch):
    app, now, transport, bar = setup_clock(monkeypatch)
    try:
        bar.toggle.setChecked(True)
        stopped = sum(s.stopped for s in bar.sounds)
        seek_clock(bar, transport, 60000)
        transport.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.StalledMedia)
        now[0] += 0.5
        bar.tick()
        assert bar.last_beat == 1 and bar.sounds[0].played == 1
        assert sum(s.stopped for s in bar.sounds) == stopped
    finally:
        bar.shutdown()
        bar.close()
        app.processEvents()


def test_finished_track_replays_from_first_beat_and_track_bind_resets(tmp_path, monkeypatch):
    app, now, transport, bar = setup_clock(monkeypatch)
    try:
        bar.linked.setChecked(True)
        start_clock(transport)
        seek_clock(bar, transport, 2000)
        assert bar.sounds[1].played == 2
        transport.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.EndOfMedia)
        now[0] += 10
        bar.tick()
        assert bar.sounds[1].played == 2
        transport.pos = 0
        transport.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadedMedia)
        start_clock(transport)
        assert bar.last_beat == 0 and bar.sounds[1].played == 3
        bar.scheduler.handled = 999
        bar.bind_track(tmp_path / "new-track.json")  # same default values still reset the grid
        bar.tick()
        assert bar.last_beat == 0 and bar.sounds[1].played == 4
    finally:
        bar.shutdown()
        bar.close()
        app.processEvents()
