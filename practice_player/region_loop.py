# SPDX-License-Identifier: GPL-3.0-only
"""Measure-range selection in performed order; no recognition during playback."""

import hashlib
import json

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .score import atomic_json


def timing_signature(timeline):
    # Camera/page-only changes do not move the selected audio interval.
    data = {
        "audio": timeline.data.get("audio_sha256"),
        "pdf": timeline.data.get("portable_timing_sha256", timeline.data.get("pdf_sha256")),
        "offset": timeline.data.get("offset", 0),
        "end": timeline.end,
        "cues": [[cue.get(key) for key in ("start", "source_index", "measure", "visit")]
                 for cue in timeline.cues],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class RegionLoop(QWidget):
    changed = Signal(str)
    current_requested = Signal(bool)

    def __init__(self, preferences, parent=None):
        super().__init__(parent)
        self.preferences = preferences
        self.timeline = None
        self.duration = 0
        self.signature = None
        self.settings_path = None
        self.available = False
        self._limits = None
        self._bounds = None
        self._context = None
        self._updating = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QHBoxLayout()
        self.enabled = QCheckBox("마디 구간 반복")
        self.enabled.setToolTip("시작·끝 마디를 고른 뒤 체크하고 재생하세요. 끝 마디도 포함합니다.")
        self.count_each = QCheckBox("매회 드럼 카운트")
        self.count_each.setToolTip(
            "처음 진입과 매 반복마다 카운트합니다. 위의 카운트 마디 수·메트로놈 음량을 사용합니다."
        )
        self.count_each.setChecked(
            str(preferences.value("region_loop/count_each", "false")).lower() in {"true", "1"}
        )
        row.addWidget(self.enabled)
        row.addWidget(self.count_each)
        row.addStretch()
        layout.addLayout(row)
        self.first, self.last = QComboBox(), QComboBox()
        self.current_first, self.current_last = QPushButton("현재 마디"), QPushButton("현재 마디")
        for label, combo, button in (("시작", self.first, self.current_first),
                                     ("끝", self.last, self.current_last)):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
            combo.setMaxVisibleItems(16)
            combo.setAccessibleName(f"구간 반복 {label} 마디와 연주 회차")
            button.setAccessibleName(f"현재 마디를 구간 {label}으로 지정")
            line = QHBoxLayout()
            line.addWidget(QLabel(label))
            line.addWidget(combo, 1)
            line.addWidget(button)
            layout.addLayout(line)
        self.hint = QLabel()
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.first.currentIndexChanged.connect(self.range_changed)
        self.last.currentIndexChanged.connect(self.range_changed)
        self.current_first.clicked.connect(lambda: self.current_requested.emit(True))
        self.current_last.clicked.connect(lambda: self.current_requested.emit(False))
        self.enabled.toggled.connect(lambda _: self.changed.emit("enabled"))
        self.count_each.toggled.connect(self.count_changed)
        self.update_bounds()

    def bind_track(self, path):
        self.settings_path = path
        self._context = None
        self.set_context(None, 0, False)

    def set_context(self, timeline, duration, available):
        key = (id(timeline), duration, available)
        if key == self._context:
            return
        self._context = key
        signature = timing_signature(timeline) if timeline else None
        limits = timeline.playable_measures(duration) if timeline else None
        rebuild = signature != self.signature or limits != self._limits
        changed = rebuild or available != self.available or duration != self.duration
        self.timeline, self.duration, self.available = timeline, duration, available
        self.signature, self._limits = signature, limits
        self._updating = True
        try:
            if changed:
                self.enabled.blockSignals(True)
                self.enabled.setChecked(False)
                self.enabled.blockSignals(False)
            if rebuild:
                self.first.clear()
                self.last.clear()
                if limits is not None:
                    for index in range(limits[0], limits[1] + 1):
                        cue = timeline.cues[index]
                        label = (f"{cue['measure']}마디 · {cue['visit']}회차 "
                                 f"(연주 순서 {index + 1})")
                        self.first.addItem(label, index)
                        self.last.addItem(label, index)
                    first, last = self.saved_selection(limits)
                    self.first.setCurrentIndex(self.first.findData(first))
                    self.last.setCurrentIndex(self.last.findData(last))
        finally:
            self._updating = False
        self.update_bounds()
        if changed:
            self.changed.emit("context")

    def saved_selection(self, fallback):
        if self.settings_path:
            try:
                data = json.loads(self.settings_path.read_text(encoding="utf-8"))
                first, last = data["first"], data["last"]
                if (data["signature"] == self.signature and type(first) is int and type(last) is int
                        and fallback[0] <= first <= last <= fallback[1]):
                    return first, last
            except (OSError, ValueError, TypeError, KeyError):
                pass
        return fallback

    def save_selection(self):
        if self.settings_path and self._bounds is not None:
            try:
                atomic_json(self.settings_path, {
                    "signature": self.signature, "first": self.first.currentData(),
                    "last": self.last.currentData(),
                })
            except OSError:
                self.hint.setText("구간은 사용할 수 있지만 설정을 저장하지 못했습니다.")

    def range_changed(self, *_):
        if self._updating:
            return
        self.enabled.blockSignals(True)
        self.enabled.setChecked(False)
        self.enabled.blockSignals(False)
        self.update_bounds()
        self.save_selection()
        self.changed.emit("range")

    def count_changed(self, *_):
        self.preferences.setValue("region_loop/count_each", self.count_each.isChecked())
        self.changed.emit("count")

    def update_bounds(self):
        self._bounds = None
        ready = bool(self.available and self.timeline and self._limits is not None)
        if ready:
            try:
                self._bounds = self.timeline.measure_range(
                    self.first.currentData(), self.last.currentData(), self.duration,
                )
                start, end = self._bounds
                self.hint.setText(
                    f"{start / 1000:.3f} ~ {end / 1000:.3f}초 · 끝 마디 포함 · 시간표 확인 권장"
                )
            except ValueError as error:
                self.hint.setText(str(error))
        else:
            self.hint.setText("음원 준비와 악보 사전 분석 후 구간을 선택할 수 있습니다.")
        self.enabled.setEnabled(self._bounds is not None)
        for widget in (
            self.first, self.last, self.current_first, self.current_last, self.count_each,
        ):
            widget.setEnabled(ready)

    def bounds(self):
        return self._bounds if self.enabled.isChecked() else None

    def select_current(self, start, milliseconds):
        if self.timeline is None or self._limits is None:
            return
        index = self.timeline.index_at(milliseconds / 1000, self.timeline.data.get("offset", 0))
        if index is None or not self._limits[0] <= index <= self._limits[1]:
            self.hint.setText("현재 재생 위치에 분석된 마디가 없습니다.")
            return
        combo = self.first if start else self.last
        combo.setCurrentIndex(combo.findData(index))
