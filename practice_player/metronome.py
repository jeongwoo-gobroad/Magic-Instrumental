# SPDX-License-Identifier: GPL-3.0-only
"""Monotonic beat clock and a compact, independent/transport-linked metronome."""

import math
import struct
import tempfile
import time
import wave
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtMultimedia import QMediaPlayer, QSoundEffect
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QWidget,
)

from .score import atomic_json


def beat_number(seconds: float, bpm: float, denominator: int = 4, origin: float = 0) -> int:
    """BPM is always quarter notes/minute; dots represent denominator-sized beats."""
    return math.floor((seconds - origin) * bpm * denominator / 240 + 1e-8)


class ClickScheduler:
    """One click per crossed beat in a transport segment, never catch up in bursts."""

    def __init__(self):
        self.handled = None

    def reset(self):
        # Only an explicit discontinuity/settings change starts a new segment.
        # Pause/resume, rate changes and ordinary position samples do not.
        self.handled = None

    def advance(self, seconds, bpm, denominator=4, origin=0, rate=1):
        period = 240 / (bpm * denominator)
        number = beat_number(seconds, bpm, denominator, origin)
        lateness = max(0, seconds - origin - number * period) / rate
        if number < 0:
            self.handled = number if self.handled is None else max(number, self.handled)
            return number, False
        if self.handled is None:
            # Starting/seeking mid-beat shows that beat, but waits for the next
            # boundary. A 5 ms tolerance covers the first timer tick/seek rounding.
            self.handled = number - 1 if lateness <= 0.005 else number
        crossed = number > self.handled
        self.handled = max(number, self.handled)
        # Transport corrections can move back across the same boundary. The
        # high-water mark prevents a second click until a real seek resets it.
        return number, crossed and lateness <= min(0.035, period / rate * 0.35)


class MetronomeBar(QWidget):
    def __init__(self, player, parent=None, *, preferences=None):
        super().__init__(parent)
        self.player = player
        self.preferences = preferences
        self.settings_path = None
        self.enabled = False
        self.last_beat = None
        self.scheduler = ClickScheduler()
        self.epoch = time.monotonic()
        self.anchor_time = self.epoch
        self.anchor_position = player.position() / 1000
        self.last_sample = self.anchor_position
        self.seeking = False
        self.suspended = False
        self._click_dir = tempfile.TemporaryDirectory(prefix="practice-click-")
        self.sounds = []
        for frequency in (1100, 1700):
            path = Path(self._click_dir.name) / f"{frequency}.wav"
            rate, frames = 44100, 1323
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                output.writeframes(
                    b"".join(
                        struct.pack(
                            "<h",
                            int(
                                18000
                                * math.sin(2 * math.pi * frequency * i / rate)
                                * math.exp(-i / 230)
                                * min(1, i / 30)
                            ),
                        )
                        for i in range(frames)
                    )
                )
            effect = QSoundEffect(self)
            effect.setSource(QUrl.fromLocalFile(str(path)))
            self.sounds.append(effect)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 4, 10, 4)
        row.setSpacing(8)
        row.addWidget(QLabel("메트로놈"))
        self.toggle = QPushButton("▶")
        self.toggle.setToolTip("메트로놈 켜기/끄기 · 음원 연동 시 재생·일시정지를 따라갑니다.")
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(self.set_running)
        row.addWidget(self.toggle)
        row.addWidget(QLabel("음량"))
        self.volume = QSpinBox()
        self.volume.setRange(0, 100)
        self.volume.setSingleStep(5)
        self.volume.setSuffix("%")
        self.volume.setAccessibleName("메트로놈 음량")
        self.volume.setToolTip("클릭 음량만 조절합니다. 곡을 바꾸거나 앱을 다시 열어도 유지됩니다.")
        try:
            volume = int(preferences.value("metronome/volume", 35)) if preferences else 35
            if not 0 <= volume <= 100:
                raise ValueError("invalid click volume")
        except (ValueError, TypeError, OverflowError):
            volume = 35
        self.volume.setValue(volume)
        row.addWidget(self.volume)
        row.addWidget(QLabel("박자"))
        self.beats = QSpinBox()
        self.beats.setRange(1, 12)
        self.beats.setValue(4)
        self.beats.setAccessibleName("한 마디의 박자 수")
        row.addWidget(self.beats)
        self.unit = QComboBox()
        for value in (4, 8, 2, 16):
            self.unit.addItem(f"/ {value}", value)
        row.addWidget(self.unit)
        self.bpm_label = QLabel("BPM ♩")
        row.addWidget(self.bpm_label)
        self.bpm = QDoubleSpinBox()
        self.bpm.setRange(20, 400)
        self.bpm.setDecimals(1)
        self.bpm.setValue(120)
        self.bpm.setAccessibleName("BPM 또는 연동 시 원곡 BPM")
        row.addWidget(self.bpm)
        self.linked = QCheckBox("음원과 연동")
        self.linked.setToolTip("체크하면 클릭을 준비하고 음원 시작·탐색·반복·배속을 따라갑니다.")
        row.addWidget(self.linked)
        self.effective = QLabel("120 BPM")
        self.effective.setMinimumWidth(85)
        row.addWidget(self.effective)
        self.dots = QWidget()
        self.dot_layout = QHBoxLayout(self.dots)
        self.dot_layout.setContentsMargins(0, 0, 0, 0)
        self.dot_layout.setSpacing(4)
        self.dot_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.dot_labels = []
        row.addWidget(self.dots, 1)
        self.origin = QDoubleSpinBox()
        self.origin.setRange(-30, 1800)
        self.origin.setDecimals(3)
        self.origin.setSuffix(" 초")
        self.origin.setToolTip(
            "원곡에서 첫 마디의 첫 박이 시작되는 시간. 탐색해도 이 기준을 유지합니다."
        )
        self.origin.setAccessibleName("음원의 첫 박 시작 시간")
        row.addWidget(self.origin)
        self.align = QPushButton("지금 첫 박")
        self.align.clicked.connect(self.align_now)
        row.addWidget(self.align)
        self.mute = QCheckBox("무음")
        self.mute.setToolTip("선택한 음량은 유지하고 클릭 소리만 잠시 끕니다.")
        row.addWidget(self.mute)
        self.volume.valueChanged.connect(self.update_volume)
        self.mute.toggled.connect(self.update_volume)
        self.update_volume()
        for spin in (self.beats, self.bpm, self.origin):
            spin.valueChanged.connect(self.reset)
        self.unit.currentIndexChanged.connect(self.reset)
        self.linked.toggled.connect(self.mode_changed)
        player.positionChanged.connect(self.anchor)
        player.playbackRateChanged.connect(self.rate_changed)
        player.playbackStateChanged.connect(self.transport_changed)
        player.mediaStatusChanged.connect(self.media_status_changed)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(5)
        self.timer.timeout.connect(self.tick)
        self.mode_changed()

    def bind_track(self, settings_path):
        import json

        self.save_track()
        self.settings_path = settings_path
        try:
            values = json.loads(settings_path.read_text(encoding="utf-8"))
            bpm, beats = float(values.get("bpm", 120)), int(values.get("beats", 4))
            origin = float(values.get("origin", 0))
            if not (20 <= bpm <= 400 and 1 <= beats <= 12 and -30 <= origin <= 1800):
                raise ValueError("invalid metronome settings")
        except (OSError, ValueError, TypeError, AttributeError):
            values = {}
            bpm, beats, origin = 120, 4, 0
        self.bpm.setValue(bpm)
        self.beats.setValue(beats)
        index = self.unit.findData(values.get("unit", 4))
        self.unit.setCurrentIndex(max(0, index))
        self.origin.setValue(origin)
        self.reanchor(self.player.position())
        self.reset()

    def save_track(self):
        if self.settings_path:
            atomic_json(
                self.settings_path,
                {
                    "bpm": self.bpm.value(),
                    "beats": self.beats.value(),
                    "unit": self.unit.currentData(),
                    "origin": self.origin.value(),
                },
            )

    def reset(self, *_):
        self.epoch = time.monotonic()
        self.last_beat = None
        self.scheduler.reset()
        self.silence()
        while self.dot_layout.count():
            self.dot_layout.takeAt(0).widget().deleteLater()
        self.dot_labels = []
        for i in range(self.beats.value()):
            dot = QLabel("●")
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dot.setToolTip(f"{i + 1}박" + (" · 강박" if i == 0 else ""))
            dot.setFixedWidth(20)
            self.dot_layout.addWidget(dot)
            self.dot_labels.append(dot)
        self.update_label()
        self.paint_beat(None)

    def silence(self):
        for sound in self.sounds:
            sound.stop()

    def update_volume(self, *_):
        # Gain changes must never reset the beat phase or touch the music output.
        level = 0 if self.mute.isChecked() else self.volume.value() / 100
        for sound in self.sounds:
            sound.setVolume(level)
        if level == 0:
            self.silence()
        if self.preferences is not None:
            self.preferences.setValue("metronome/volume", self.volume.value())

    def align_now(self):
        self.origin.setValue(self.media_seconds(time.monotonic()))

    def mode_changed(self, *_):
        linked = self.linked.isChecked()
        self.bpm_label.setText("원곡 BPM ♩" if linked else "BPM ♩")
        self.origin.setEnabled(linked)
        self.align.setEnabled(linked)
        self.reanchor(self.player.position())
        self.reset()
        if linked:
            # A single checkbox arms synchronization; it never starts the song.
            self.toggle.setChecked(True)

    def update_label(self):
        rate = self.player.playbackRate() if self.linked.isChecked() else 1
        self.effective.setText(f"{self.bpm.value() * rate:g} BPM")

    def set_running(self, running):
        self.enabled = running
        self.epoch = time.monotonic()
        self.last_beat = None
        self.scheduler.reset()
        self.reanchor(self.player.position())
        self.toggle.setText("■" if running else "▶")
        if running:
            self.timer.start()
            self.tick()
        else:
            self.timer.stop()
            self.paint_beat(None)
            self.silence()

    def reanchor(self, milliseconds):
        self.anchor_position = milliseconds / 1000
        self.last_sample = self.anchor_position
        self.anchor_time = time.monotonic()

    def media_seconds(self, now):
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState or self.suspended:
            return self.player.position() / 1000
        seconds = self.anchor_position + (now - self.anchor_time) * self.player.playbackRate()
        duration = self.player.duration() / 1000
        return min(seconds, duration) if duration > 0 else seconds

    def begin_seek(self):
        self.seeking = True
        if self.linked.isChecked():
            self.silence()

    def end_seek(self, milliseconds):
        self.seeking = False
        self.reanchor(milliseconds)
        if self.linked.isChecked():
            self.scheduler.reset()
            self.last_beat = None
            self.paint_beat(None)
            self.tick()

    def anchor(self, milliseconds):
        if self.seeking:
            return
        # Explicit GUI seeks use begin/end_seek. Detect loops/external jumps too;
        # small regular transport corrections retain the handled-beat watermark.
        position = milliseconds / 1000
        predicted = self.media_seconds(time.monotonic())
        jump = position < self.last_sample - 0.08 or position > predicted + 0.3
        self.reanchor(milliseconds)
        if jump and self.linked.isChecked():
            self.scheduler.reset()
            self.last_beat = None
            self.silence()

    def rate_changed(self, *_):
        self.reanchor(self.player.position())
        self.update_label()

    def transport_changed(self, *_):
        # Resuming after a long pause is not a seek. Extrapolation must start
        # here rather than include the paused wall-clock interval.
        self.reanchor(self.player.position())
        if self.linked.isChecked():
            if self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
                self.scheduler.reset()
            self.last_beat = None
            self.paint_beat(None)
            self.silence()
            self.tick()

    def media_status_changed(self, status):
        self.suspended = status in {
            QMediaPlayer.MediaStatus.NoMedia,
            QMediaPlayer.MediaStatus.LoadingMedia,
            QMediaPlayer.MediaStatus.StalledMedia,
            QMediaPlayer.MediaStatus.EndOfMedia,
            QMediaPlayer.MediaStatus.InvalidMedia,
        }
        # A loop may publish a buffering/status event before positionChanged.
        # Inspect that jump before overwriting the previous position sample.
        self.anchor(self.player.position())
        if self.linked.isChecked() and self.suspended:
            if status != QMediaPlayer.MediaStatus.StalledMedia:
                self.scheduler.reset()
            self.silence()
            self.last_beat = None
            self.paint_beat(None)

    def paint_beat(self, beat):
        for i, dot in enumerate(self.dot_labels):
            color = "#ffcf78" if i == 0 else "#8cddc5"
            dot.setStyleSheet(f"font-size: 20px; color: {color if i == beat else '#45546a'};")

    def tick(self):
        if not self.enabled:
            return
        now = time.monotonic()
        linked = self.linked.isChecked()
        if linked:
            if (
                self.seeking
                or self.suspended
                or self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
            ):
                return
            seconds = self.media_seconds(now)
        else:
            seconds = now - self.epoch
        number, click = self.scheduler.advance(
            seconds,
            self.bpm.value(),
            self.unit.currentData(),
            self.origin.value() if linked else 0,
            self.player.playbackRate() if linked else 1,
        )
        if number < 0:
            self.last_beat = number
            self.paint_beat(None)
            return
        dot = number % self.beats.value()
        if click and not self.mute.isChecked() and self.volume.value() > 0:
            self.sounds[1 if dot == 0 else 0].play()
        if number != self.last_beat:
            self.last_beat = number
            self.paint_beat(dot)

    def shutdown(self):
        self.save_track()
        self.toggle.setChecked(False)
        for sound in self.sounds:
            sound.setSource(QUrl())
        self._click_dir.cleanup()
