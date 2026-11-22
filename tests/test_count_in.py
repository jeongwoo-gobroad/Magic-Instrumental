# SPDX-License-Identifier: GPL-3.0-only
import wave

import numpy as np
import pytest
from PySide6.QtCore import QObject, QSettings, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer, QSoundEffect
from PySide6.QtWidgets import QApplication

from practice_player.count_in import CountClock, CountIn, write_drum
from practice_player.metronome import MetronomeBar


def count_clock(**overrides):
    values = dict(now=100, position=0, origin=0, bpm=120, beats=4, denominator=4, rate=1, bars=1)
    return CountClock(**(values | overrides))


@pytest.mark.parametrize("rate,beats,unit,bars", [(1, 4, 4, 1), (0.5, 3, 4, 2),
                                               (0.75, 6, 8, 1), (2, 7, 8, 4), (1, 2, 2, 1)])
def test_count_length_meter_and_speed(rate, beats, unit, bars):
    clock = count_clock(rate=rate, beats=beats, denominator=unit, bars=bars)
    numbers = []
    for index in range(beats * bars):
        number, finished = clock.advance(100 + index * clock.period)
        assert not finished
        numbers.append(number % beats)
        assert clock.advance(100 + index * clock.period) == (None, False)
    assert numbers == list(range(beats)) * bars
    assert clock.duration == pytest.approx(bars * beats * 240 / (120 * unit * rate))
    assert clock.advance(100 + clock.duration) == (None, True)
    assert clock.advance(101 + clock.duration) == (None, False)


@pytest.mark.parametrize("position,origin", [(1.25, 0), (0, 1.25), (51.333, -0.125), (0.501, 0)])
def test_count_preserves_fractional_entry_and_first_downbeat(position, origin):
    clock = count_clock(position=position, origin=origin, rate=0.75)
    for index in range(clock.total):
        clock.advance(100 + index * clock.period)
    # Extrapolate the count's fictitious media clock: it must land at the
    # requested position, not at position multiplied by playback speed.
    assert origin + (clock.first * clock.period + clock.duration) * 0.75 == pytest.approx(position)
    assert clock.advance(100 + clock.duration) == (None, True)


def test_count_stall_cannot_burst_beats_or_start_music_without_count():
    clock = count_clock()
    assert clock.advance(100) == (-4, False)
    with pytest.raises(RuntimeError, match="지연"):
        clock.advance(101.6)
    with pytest.raises(RuntimeError, match="지연"):
        clock.advance(102)


@pytest.mark.parametrize("lateness,rejected", [(0, False), (0.052, False), (0.053, True)])
def test_fast_single_beat_count_preserves_handoff_lateness_guard(lateness, rejected):
    clock = count_clock(bpm=400, beats=1)
    assert clock.duration == pytest.approx(0.15)
    assert clock.advance(100) == (-1, False)
    handoff = 100 + 0.15 + lateness
    if rejected:
        with pytest.raises(RuntimeError, match="지연"):
            clock.advance(handoff)
        assert not clock.finished
    else:
        assert clock.advance(handoff) == (None, True)


def test_synthetic_drums_are_bounded_and_accent_has_audible_low_end(tmp_path):
    energies = []
    for accent in (False, True):
        path = tmp_path / f"{accent}.wav"
        write_drum(path, accent)
        with wave.open(str(path)) as audio:
            assert audio.getnchannels() == 1 and audio.getframerate() == 44100
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(float)
        assert 0.05 < len(samples) / 44100 < 0.1
        assert np.max(np.abs(samples)) < 32767 and samples[0] == samples[-1] == 0
        spectrum = np.abs(np.fft.rfft(samples)) ** 2
        frequencies = np.fft.rfftfreq(len(samples), 1 / 44100)
        energies.append(spectrum[frequencies < 250].sum())
    assert energies[1] > energies[0] * 10


class Transport(QObject):
    positionChanged = Signal(int)
    playbackRateChanged = Signal(float)
    playbackStateChanged = Signal(object)
    mediaStatusChanged = Signal(object)
    sourceChanged = Signal(QUrl)
    errorOccurred = Signal(object, str)

    def __init__(self):
        super().__init__()
        self.pos, self.rate, self.played = 0, 1, 0
        self.playing = False
        self.status = QMediaPlayer.MediaStatus.LoadedMedia

    def position(self):
        return self.pos

    def playbackRate(self):
        return self.rate

    def duration(self):
        return 180000

    def playbackState(self):
        return (QMediaPlayer.PlaybackState.PlayingState if self.playing
                else QMediaPlayer.PlaybackState.PausedState)

    def mediaStatus(self):
        return self.status

    def play(self):
        self.played += 1
        self.playing = True
        self.playbackStateChanged.emit(self.playbackState())


class Sound:
    def __init__(self):
        self.played, self.gain = 0, 0.35
        self.state = QSoundEffect.Status.Ready

    def play(self):
        self.played += 1

    def stop(self):
        pass

    def setSource(self, _):
        pass

    def setVolume(self, gain):
        self.gain = gain

    def status(self):
        return self.state


@pytest.fixture
def rig(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    now = [100.0]
    monkeypatch.setattr("practice_player.count_in.time.monotonic", lambda: now[0])
    player = Transport()
    preferences = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    bar = MetronomeBar(player)
    count = CountIn(bar, preferences)
    bar.sounds = [Sound(), Sound()]
    count.sounds = [Sound(), Sound()]
    count.finished.connect(player.play)
    count.enabled.setChecked(True)
    yield app, now, player, bar, count
    count.shutdown()
    bar.shutdown()
    count.close()
    bar.close()
    app.processEvents()


def test_count_owns_transport_then_hands_off_without_metronome_overlap(rig):
    _, now, player, bar, count = rig
    bar.linked.setChecked(True)
    assert count.start()
    assert count.active and bar.count_in_active and player.played == 0
    for index in range(1, 4):
        now[0] = 100 + index * 0.5
        count.tick()
        bar.tick()
    assert [s.played for s in count.sounds] == [3, 1]
    assert sum(s.played for s in bar.sounds) == 0
    now[0] = 102
    count.tick()
    assert player.played == 1 and not count.active and not bar.count_in_active
    assert bar.sounds[1].played == 1  # first media downbeat
    count.tick()
    assert player.played == 1


def test_fractional_handoff_does_not_repeat_last_drum_beat(rig):
    _, now, player, bar, count = rig
    player.pos = 501
    bar.linked.setChecked(True)
    count.start()
    for index in range(1, 4):
        now[0] = 100 + index * 0.5
        count.tick()
    now[0] = 100 + count.clock.duration
    count.tick()
    assert player.played == 1 and sum(s.played for s in bar.sounds) == 0
    now[0] += 0.499
    bar.tick()
    assert bar.sounds[0].played == 1


@pytest.mark.parametrize("action", ["disable", "bpm", "meter", "origin", "rate", "seek", "source",
                                    "error", "stall", "bars", "cancel"])
def test_changes_cancel_pending_play_without_delayed_start(rig, action):
    _, now, player, bar, count = rig
    count.start()
    if action == "disable":
        count.enabled.setChecked(False)
    elif action == "bpm":
        bar.bpm.setValue(107)
    elif action == "meter":
        bar.beats.setValue(3)
    elif action == "origin":
        bar.origin.setValue(1)
    elif action == "rate":
        player.playbackRateChanged.emit(0.75)
    elif action == "seek":
        player.positionChanged.emit(1000)
    elif action == "source":
        player.sourceChanged.emit(QUrl())
    elif action == "error":
        player.errorOccurred.emit(None, "error")
    elif action == "stall":
        player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.StalledMedia)
    elif action == "bars":
        count.bars.setValue(2)
    else:
        count.cancel()
    now[0] += 10
    count.tick()
    assert not count.active and not bar.count_in_active and player.played == 0


def test_preparation_waits_for_sound_and_media_and_failure_does_not_play(rig):
    _, now, player, _, count = rig
    count.sounds[0].state = QSoundEffect.Status.Loading
    player.status = QMediaPlayer.MediaStatus.LoadingMedia
    count.start()
    now[0] += 1
    count.tick()
    assert count.clock is None and player.played == 0
    count.sounds[0].state = QSoundEffect.Status.Ready
    player.status = QMediaPlayer.MediaStatus.LoadedMedia
    count.tick()
    assert count.clock.started == 101
    count.sounds[0].state = QSoundEffect.Status.Error
    failures = []
    count.failed.connect(failures.append)
    count.tick()
    assert failures and not count.active and player.played == 0


def test_muting_and_gain_changes_preserve_count_and_do_not_toggle_normal_metronome(rig):
    _, now, player, bar, count = rig
    assert not bar.enabled and not bar.linked.isChecked()
    count.start()
    started = count.clock.started
    bar.volume.setValue(70)
    assert all(s.gain == 0.7 for s in count.sounds)
    bar.mute.setChecked(True)
    assert all(s.gain == 0 for s in count.sounds)
    for index in range(1, 5):
        now[0] = started + index * 0.5
        count.tick()
    assert sum(s.played for s in count.sounds) == 1
    assert player.played == 1 and not bar.enabled and not bar.linked.isChecked()
