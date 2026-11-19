# SPDX-License-Identifier: GPL-3.0-only
"""Optional drum count before transport playback; no media/score rewriting."""

import io
import math
import random
import struct
import time
import wave
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer, QSoundEffect
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QSpinBox, QWidget

from .metronome import wav_url


class CountClock:
    """N meter beats ending at the existing original-media beat phase.

    At a fractional beat, shorten the final interval to enter at that fraction.
    Beat numbers may be negative during the count or before the first downbeat.
    """

    def __init__(self, *, now, position, origin, bpm, beats, denominator, rate, bars):
        if not (20 <= bpm <= 400 and 1 <= beats <= 12 and denominator in (2, 4, 8, 16)
                and 0.25 <= rate <= 2 and 1 <= bars <= 4):
            raise ValueError("잘못된 카운트 박자/배속 설정입니다.")
        self.started = now
        self.period = 240 / (bpm * denominator * rate)
        self.total = beats * bars
        self.beats = beats
        target = (position - origin) * bpm * denominator / 240
        if abs(target - round(target)) < 1e-7:
            target = round(target)
        self.first = math.ceil(target) - self.total
        self.duration = (target - self.first) * self.period
        self.handled = -1
        self.finished = False

    def advance(self, now):
        """Return (absolute beat or None, finished); reject stalls, never burst."""
        if self.finished:
            return None, False
        elapsed = max(0, now - self.started)
        tolerance = max(0.04, min(0.1, self.period * 0.35))
        if elapsed + 1e-8 >= self.duration:
            if self.handled != self.total - 1 or elapsed - self.duration > tolerance:
                raise RuntimeError("카운트가 지연되어 취소했습니다. 재생을 다시 누르세요.")
            self.finished = True
            return None, True
        index = min(self.total - 1, math.floor(elapsed / self.period + 1e-8))
        if index <= self.handled:
            return None, False
        if index != self.handled + 1 or elapsed - index * self.period > tolerance:
            raise RuntimeError("카운트가 지연되어 취소했습니다. 재생을 다시 누르세요.")
        self.handled = index
        return self.first + index, False


def write_drum(path: Path | io.BytesIO, accent: bool):
    """Original short closed-hat/snare sound; accented beats add a low kick."""
    rate, frames = 44100, 3528
    noise = random.Random(712 if accent else 241)
    samples = bytearray()
    previous = 0
    for i in range(frames):
        t = i / rate
        current = noise.uniform(-1, 1)
        high = (current - previous) / 2
        previous = current
        hat = high * math.exp(-t * 95)
        snare = current * math.exp(-t * 55) if accent else 0
        kick = math.sin(2 * math.pi * (80 * t + 0.45 * (1 - math.exp(-40 * t))))
        value = 0.55 * hat + (0.22 * snare + 0.5 * kick * math.exp(-t * 50) if accent else 0)
        value *= min(1, i / 20) * min(1, (frames - 1 - i) / 100)
        samples.extend(struct.pack("<h", int(max(-1, min(1, value)) * 26000)))
    with wave.open(str(path) if isinstance(path, Path) else path, "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(samples)


class CountIn(QWidget):
    active_changed = Signal(bool)
    finished = Signal()
    failed = Signal(str)

    def __init__(self, metronome, preferences, parent=None):
        super().__init__(parent)
        self.metronome = metronome
        self.player = metronome.player
        self.preferences = preferences
        self.active = False
        self.external_enabled = False
        self.clock = None
        self.requested = 0
        self.position = 0
        self.sounds = []
        for accent in (False, True):
            buffer = io.BytesIO()
            write_drum(buffer, accent)
            sound = QSoundEffect(self)
            sound.setSource(wav_url(buffer))
            self.sounds.append(sound)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.enabled = QCheckBox("드럼 카운트")
        self.enabled.setToolTip(
            "재생·일시정지 후 재개 전에 예비 박을 듣습니다. 상단의 원곡 BPM·박자·첫 박과 "
            "현재 배속을 사용합니다. 음원 연동 체크와 관계없이 사용할 수 있습니다."
        )
        saved = str(preferences.value("count_in/enabled", "false")).lower()
        self.enabled.setChecked(saved in {"true", "1"})
        self.bars = QSpinBox()
        self.bars.setRange(1, 4)
        self.bars.setSuffix(" 마디")
        self.bars.setAccessibleName("드럼 카운트 마디 수")
        try:
            bars = int(preferences.value("count_in/bars", 1))
        except (ValueError, TypeError, OverflowError):
            bars = 1
        self.bars.setValue(bars if 1 <= bars <= 4 else 1)
        self.bars.setEnabled(self.enabled.isChecked())
        self.label = QLabel("재생 전 준비")
        self.label.setObjectName("muted")
        self.label.setToolTip("카운트 음량과 무음은 상단 메트로놈 설정을 따릅니다.")
        row.addWidget(self.enabled)
        row.addWidget(self.bars)
        row.addWidget(self.label, 1)
        self.enabled.toggled.connect(self.settings_changed)
        self.bars.valueChanged.connect(self.settings_changed)
        for spin in (metronome.bpm, metronome.beats, metronome.origin):
            spin.valueChanged.connect(self.cancel)
        metronome.unit.currentIndexChanged.connect(self.cancel)
        metronome.linked.toggled.connect(self.cancel)
        metronome.volume.valueChanged.connect(self.update_volume)
        metronome.mute.toggled.connect(self.update_volume)
        self.player.playbackRateChanged.connect(self.cancel)
        self.player.sourceChanged.connect(self.cancel)
        self.player.positionChanged.connect(self.position_changed)
        self.player.playbackStateChanged.connect(self.transport_changed)
        self.player.mediaStatusChanged.connect(self.media_status_changed)
        self.player.errorOccurred.connect(self.cancel)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(5)
        self.timer.timeout.connect(self.tick)
        self.update_volume()

    def settings_changed(self, *_):
        self.cancel()
        self.bars.setEnabled(self.enabled.isChecked() or self.external_enabled)
        self.preferences.setValue("count_in/enabled", self.enabled.isChecked())
        self.preferences.setValue("count_in/bars", self.bars.value())

    def update_volume(self, *_):
        bar = self.metronome
        gain = 0 if bar.mute.isChecked() else bar.volume.value() / 100
        for sound in self.sounds:
            sound.setVolume(gain)
            if gain == 0:
                sound.stop()

    def set_external_enabled(self, enabled):
        self.external_enabled = enabled
        self.bars.setEnabled(self.enabled.isChecked() or enabled)

    def start(self, *, force=False) -> bool:
        """True means count-in owns this play request, including any failure."""
        if not (force or self.enabled.isChecked()):
            return False
        self.cancel()
        for sound in self.sounds:
            sound.stop()
        self.position = self.player.position()
        self.requested = time.monotonic()
        self.active = True
        self.metronome.hold_count_in(True)
        self.label.setText("카운트 준비 중…")
        self.active_changed.emit(True)
        self.timer.start()
        self.tick()
        return True

    def cancel(self, *_, stop_sounds=True):
        if not self.active:
            return
        self.active = False
        self.clock = None
        self.timer.stop()
        if stop_sounds:
            for sound in self.sounds:
                sound.stop()
        self.metronome.hold_count_in(False)
        self.label.setText("재생 전 준비")
        self.active_changed.emit(False)

    def position_changed(self, position):
        if self.active and position != self.position:
            self.cancel()

    def transport_changed(self, state):
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.cancel()

    def media_status_changed(self, status):
        if status in {QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.InvalidMedia,
                      QMediaPlayer.MediaStatus.StalledMedia, QMediaPlayer.MediaStatus.EndOfMedia}:
            self.cancel()
        elif self.clock is not None and status == QMediaPlayer.MediaStatus.LoadingMedia:
            self.cancel()

    def tick(self):
        if not self.active:
            return
        now = time.monotonic()
        bar = self.metronome
        audible = not bar.mute.isChecked() and bar.volume.value() > 0
        try:
            if audible and any(s.status() == QSoundEffect.Status.Error for s in self.sounds):
                raise RuntimeError("드럼 소리를 준비하지 못했습니다. 오디오 장치를 확인하세요.")
            if self.clock is None:
                if now - self.requested > 15:
                    raise RuntimeError("카운트 준비 시간이 초과됐습니다. 재생을 다시 누르세요.")
                ready = self.player.mediaStatus() in {
                    QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                }
                if not ready or (audible and any(
                    s.status() != QSoundEffect.Status.Ready for s in self.sounds
                )):
                    return
                self.clock = CountClock(
                    now=now, position=self.position / 1000, origin=bar.origin.value(),
                    bpm=bar.bpm.value(), beats=bar.beats.value(),
                    denominator=bar.unit.currentData(),
                    rate=self.player.playbackRate(), bars=self.bars.value(),
                )
            elif audible and any(s.status() != QSoundEffect.Status.Ready for s in self.sounds):
                raise RuntimeError("드럼 소리가 아직 준비되지 않았습니다. 재생을 다시 누르세요.")
            number, finished = self.clock.advance(now)
            if finished:
                last = self.clock.first + self.clock.total - 1
                self.cancel(stop_sounds=False)
                if bar.linked.isChecked():
                    # A fractional entry just after a beat must not click again
                    # inside the linked metronome's first-tick tolerance.
                    bar.scheduler.handled = last
                self.finished.emit()
                return
            if number is not None:
                dot = number % self.clock.beats
                measure = self.clock.handled // self.clock.beats + 1
                tempo = bar.bpm.value() * self.player.playbackRate()
                self.label.setText(f"{measure}/{self.bars.value()} · {dot + 1}박 · {tempo:g} BPM")
                bar.paint_beat(dot)
                if audible:
                    self.sounds[1 if dot == 0 else 0].play()
        except (ValueError, RuntimeError) as error:
            self.cancel()
            self.label.setText("카운트 취소됨")
            self.failed.emit(str(error))

    def shutdown(self):
        self.cancel()
        for sound in self.sounds:
            sound.setSource(QUrl())
