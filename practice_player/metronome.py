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


class MetronomeBar(QWidget):
    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self.settings_path = None
        self.enabled = False
        self.last_beat = None
        self.epoch = time.monotonic()
        self.anchor_time = self.epoch
        self.anchor_position = player.position() / 1000
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
            effect.setVolume(0.35)
            self.sounds.append(effect)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 4, 10, 4)
        row.setSpacing(8)
        row.addWidget(QLabel("메트로놈"))
        self.toggle = QPushButton("▶")
        self.toggle.setToolTip("단독 재생 / 음원 연동 클릭 켜기")
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(self.set_running)
        row.addWidget(self.toggle)
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
        self.origin.setToolTip("원곡에서 첫 번째 박자가 시작되는 시간 (♩ BPM 기준)")
        self.origin.setAccessibleName("음원의 첫 박 시작 시간")
        row.addWidget(self.origin)
        self.align = QPushButton("지금 첫 박")
        self.align.clicked.connect(lambda: self.origin.setValue(player.position() / 1000))
        row.addWidget(self.align)
        self.mute = QCheckBox("무음")
        row.addWidget(self.mute)
        for spin in (self.beats, self.bpm, self.origin):
            spin.valueChanged.connect(self.reset)
        self.unit.currentIndexChanged.connect(self.reset)
        self.linked.toggled.connect(self.mode_changed)
        player.positionChanged.connect(self.anchor)
        player.playbackRateChanged.connect(self.rate_changed)
        player.playbackStateChanged.connect(self.transport_changed)
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

    def mode_changed(self, *_):
        linked = self.linked.isChecked()
        self.bpm_label.setText("원곡 BPM ♩" if linked else "BPM ♩")
        self.origin.setEnabled(linked)
        self.align.setEnabled(linked)
        self.anchor(self.player.position())
        self.reset()

    def update_label(self):
        rate = self.player.playbackRate() if self.linked.isChecked() else 1
        self.effective.setText(f"{self.bpm.value() * rate:g} BPM")

    def set_running(self, running):
        self.enabled = running
        self.epoch = time.monotonic()
        self.last_beat = None
        self.toggle.setText("■" if running else "▶")
        if running:
            self.timer.start()
            self.tick()
        else:
            self.timer.stop()
            self.paint_beat(None)
            for sound in self.sounds:
                sound.stop()

    def anchor(self, milliseconds):
        # Regular transport samples correct extrapolation; never accumulate timer deltas.
        self.anchor_position = milliseconds / 1000
        self.anchor_time = time.monotonic()

    def rate_changed(self, *_):
        self.anchor(self.player.position())
        self.update_label()

    def transport_changed(self, *_):
        self.anchor(self.player.position())
        if self.linked.isChecked():
            self.last_beat = None
            self.paint_beat(None)
            for sound in self.sounds:
                sound.stop()

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
            if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                return
            seconds = self.anchor_position + (now - self.anchor_time) * self.player.playbackRate()
        else:
            seconds = now - self.epoch
        number = beat_number(
            seconds, self.bpm.value(), self.unit.currentData(), self.origin.value() if linked else 0
        )
        if number < 0:
            self.last_beat = number
            self.paint_beat(None)
            return
        dot = number % self.beats.value()
        if number != self.last_beat:
            # After seeks or stalls play at most one click, never a catch-up burst.
            # Starting halfway into a linked beat waits for its next boundary.
            phase = (
                (seconds - (self.origin.value() if linked else 0))
                * self.bpm.value()
                * self.unit.currentData()
                / 240
            ) - number
            if not self.mute.isChecked() and phase < 0.18:
                self.sounds[1 if dot == 0 else 0].play()
            self.last_beat = number
            self.paint_beat(dot)

    def shutdown(self):
        self.save_track()
        self.toggle.setChecked(False)
        for sound in self.sounds:
            sound.setSource(QUrl())
        self._click_dir.cleanup()
