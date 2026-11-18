# SPDX-License-Identifier: GPL-3.0-only
"""Qt owns playback; a separate Python process owns inference and export."""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

from PySide6.QtCore import QProcess, QProcessEnvironment, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .audio import discover
from .cli import EVENT_PREFIX
from .config import MODELS, ROOT, Paths
from .metronome import MetronomeBar
from .score_gui import ScorePanel

STYLE = """
QWidget { background: #11151d; color: #e7edf7; font-size: 14px; }
QMainWindow { background: #11151d; }
QLabel#title { font-size: 26px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #99a9bf; }
QLabel#track { font-size: 21px; font-weight: 600; }
QLabel#speed { color: #87ddc4; font-size: 32px; font-weight: 700; }
QPushButton, QComboBox { background: #242e3e; border: 1px solid #35425a;
    border-radius: 7px; padding: 6px 10px; }
QPushButton:hover { background: #35425a; }
QPushButton:disabled { color: #607087; background: #1b2330; }
QPushButton#primary { background: #8cddc5; color: #102c26; font-weight: 700; }
QPushButton#primary:disabled { background: #355b50; color: #8caaa2; }
QListWidget, QPlainTextEdit { background: #19212d; border: 1px solid #2c384b;
    border-radius: 8px; padding: 7px; }
QListWidget::item { padding: 12px 8px; border-radius: 5px; }
QListWidget::item:selected { background: #304b50; color: #a2f5d9; }
QSlider::groove:horizontal { background: #2b384b; height: 6px; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #8cddc5; border-radius: 3px; }
QSlider::handle:horizontal { background: #c4f7e8; width: 17px; margin: -6px 0;
    border-radius: 8px; }
QProgressBar { border: 1px solid #35425a; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: #477c70; }
QSplitter::handle { background: #11151d; width: 14px; }
QToolTip { color: #e7edf7; background: #242e3e; border: 1px solid #35425a; }
"""


def time_label(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class Window(QMainWindow):
    def __init__(self, paths: Paths | None = None):
        super().__init__()
        self.paths = paths or Paths()
        self.paths.create()
        self.worker = None
        self.output_path = None
        self.raw_buffer = b""
        self.job_result = None
        self.job_error = ""
        self.cancelled = False
        self.close_pending = False
        self.source_name = ""
        self.source_path = None
        self.pending_source = None
        self.preferences = QSettings(
            str(self.paths.processed / "ui.ini"), QSettings.Format.IniFormat
        )
        self.setWindowTitle("Magic-Instrumental · 인스트루먼탈 연습실")
        self.resize(
            self.preferences.value("window_width", 1500, type=int),
            self.preferences.value("window_height", 930, type=int),
        )
        self.setMinimumSize(960, 700)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.75)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.build_ui()
        self.connect_player()
        self.refresh_tracks()

    def build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)
        title = QLabel("Magic-Instrumental")
        title.setObjectName("title")
        layout.addWidget(title)
        subtitle = QLabel("보컬은 덜어내고, 나의 속도로 연습하세요.")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)
        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter, 1)
        library = QWidget()
        left = QVBoxLayout(library)
        left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(QLabel("음원 목록  ·  targets"))
        self.tracks = QListWidget()
        self.tracks.setAccessibleName("targets 음원 목록")
        self.tracks.setMaximumHeight(90)
        self.tracks.itemDoubleClicked.connect(lambda _: self.prepare())
        left.addWidget(self.tracks, 1)
        self.track_count = QLabel()
        self.track_count.setObjectName("muted")
        left.addWidget(self.track_count)
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("새로고침")
        self.refresh_button.clicked.connect(self.refresh_tracks)
        buttons.addWidget(self.refresh_button)
        folder = QPushButton("음원 폴더 열기")
        folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.targets)))
        )
        buttons.addWidget(folder)
        left.addLayout(buttons)
        splitter.addWidget(library)
        panel = QWidget()
        right = QVBoxLayout(panel)
        right.setContentsMargins(6, 0, 0, 0)
        right.setSpacing(7)
        right.addWidget(QLabel("분리 모델"))
        self.models = QComboBox()
        for key, model in MODELS.items():
            self.models.addItem(model.label, key)
        right.addWidget(self.models)
        options = QHBoxLayout()
        self.devices = QComboBox()
        for label, value in [
            ("GPU 자동 선택", "auto"),
            ("NVIDIA CUDA", "cuda"),
            ("Apple GPU · MPS", "mps"),
            ("CPU", "cpu"),
        ]:
            self.devices.addItem(label, value)
        options.addWidget(self.devices)
        self.low_memory = QCheckBox("메모리 절약")
        self.low_memory.setToolTip(
            "8초 청크로 처리합니다. 기본 설정보다 품질이 달라질 수 있습니다."
        )
        options.addWidget(self.low_memory)
        right.addLayout(options)
        actions = QHBoxLayout()
        self.prepare_button = QPushButton("선택 곡 준비 · 분리 / 캐시 불러오기")
        self.prepare_button.setObjectName("primary")
        self.prepare_button.clicked.connect(self.prepare)
        actions.addWidget(self.prepare_button, 1)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_job)
        actions.addWidget(self.cancel_button)
        right.addLayout(actions)
        self.status = QLabel("왼쪽에서 곡을 선택하세요. 최초 분리 시 모델을 다운로드합니다.")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(42)
        self.status.setObjectName("muted")
        right.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        right.addWidget(self.progress)
        self.current_track = QLabel("재생할 인스트루먼탈을 준비하세요")
        self.current_track.setObjectName("track")
        self.current_track.setWordWrap(True)
        right.addWidget(self.current_track)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.setAccessibleName("재생 위치")
        self.seek.sliderReleased.connect(lambda: self.seek_to(self.seek.value()))
        right.addWidget(self.seek)
        self.clock = QLabel("00:00 / 00:00")
        self.clock.setObjectName("muted")
        right.addWidget(self.clock)
        controls = QHBoxLayout()
        self.play_button = QPushButton("재생")
        self.play_button.clicked.connect(self.toggle_play)
        self.play_button.setEnabled(False)
        controls.addWidget(self.play_button)
        stop = QPushButton("처음으로")
        stop.clicked.connect(lambda: self.seek_to(0))
        controls.addWidget(stop)
        self.loop = QCheckBox("반복")
        self.loop.toggled.connect(lambda on: self.player.setLoops(-1 if on else 1))
        controls.addWidget(self.loop)
        controls.addStretch()
        right.addLayout(controls)
        measure_row = QHBoxLayout()
        self.previous_measure = QPushButton("◀ 이전")
        self.previous_measure.setAccessibleName("지정한 마디 수만큼 이전으로 이동")
        self.previous_measure.clicked.connect(lambda: self.jump_measures(-1))
        measure_row.addWidget(self.previous_measure)
        self.measure_step = QSpinBox()
        self.measure_step.setRange(1, 10000)
        self.measure_step.setValue(self.preferences.value("measure_step", 1, type=int))
        self.measure_step.setSuffix(" 마디씩")
        self.measure_step.setAccessibleName("한 번에 이동할 마디 수")
        self.measure_step.setToolTip(
            "현재 마디에서 실제 연주 순서로 이동합니다. 반복 회차도 포함합니다."
        )
        measure_row.addWidget(self.measure_step)
        self.next_measure = QPushButton("건너뛰기 ▶")
        self.next_measure.setAccessibleName("지정한 마디 수만큼 앞으로 건너뛰기")
        self.next_measure.clicked.connect(lambda: self.jump_measures(1))
        measure_row.addWidget(self.next_measure)
        right.addLayout(measure_row)
        self.measure_hint = QLabel("악보 사전 분석 후 마디 단위로 이동할 수 있습니다.")
        self.measure_hint.setObjectName("muted")
        self.measure_hint.setWordWrap(True)
        right.addWidget(self.measure_hint)
        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("재생 속도"))
        self.speed_label = QLabel("1.00×")
        self.speed_label.setObjectName("speed")
        speed_row.addStretch()
        speed_row.addWidget(self.speed_label)
        reset = QPushButton("1×")
        reset.clicked.connect(lambda: self.speed.setValue(100))
        speed_row.addWidget(reset)
        right.addLayout(speed_row)
        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(25, 200)
        self.speed.setValue(100)
        self.speed.setAccessibleName("재생 속도 0.25배에서 2배")
        self.speed.valueChanged.connect(self.change_speed)
        right.addWidget(self.speed)
        self.pitch = QCheckBox("음정 유지")
        self.pitch.setChecked(True)
        self.pitch.toggled.connect(self.player.setPitchCompensation)
        right.addWidget(self.pitch)
        volume_row = QHBoxLayout()
        volume_row.addWidget(QLabel("볼륨"))
        volume = QSlider(Qt.Orientation.Horizontal)
        volume.setAccessibleName("볼륨")
        volume.setRange(0, 100)
        volume.setValue(75)
        volume.valueChanged.connect(lambda value: self.audio_output.setVolume(value / 100))
        volume_row.addWidget(volume)
        right.addLayout(volume_row)
        self.export_button = QPushButton("현재 속도로 저장 · WAV / FLAC")
        self.export_button.setToolTip("음정을 유지한 속도로 새 파일을 저장합니다.")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_audio)
        right.addWidget(self.export_button)
        right.addStretch()
        splitter.addWidget(panel)
        splitter.setSizes([170, 600])
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(300)
        self.logs.setMaximumHeight(75)
        self.logs.setPlaceholderText("처리 로그")
        layout.addWidget(self.logs)
        controller = QScrollArea()
        controller.setWidgetResizable(True)
        controller.setWidget(root)
        controller.setMinimumWidth(410)
        self.score_panel = ScorePanel(self.paths)
        self.score_panel.attach_player(self.player)
        self.score_panel.setMinimumWidth(300)
        self.score_panel.analysis_requested.connect(self.analyze_score)
        self.score_panel.seek_requested.connect(self.seek_to)
        self.score_panel.timeline_changed.connect(self.update_measure_controls)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.addWidget(controller)
        self.main_splitter.addWidget(self.score_panel)
        self.main_splitter.setSizes([650, 850])
        saved = self.preferences.value("splitter")
        if saved is not None:
            self.main_splitter.restoreState(saved)
        self.main_splitter.splitterMoved.connect(
            lambda *_: self.preferences.setValue("splitter", self.main_splitter.saveState())
        )
        self.metronome = MetronomeBar(self.player)
        self.score_panel.bpm_provider = lambda: self.metronome.bpm.value()
        top = QScrollArea()
        top.setWidget(self.metronome)
        top.setWidgetResizable(True)
        top.setFixedHeight(66)
        top.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(6, 4, 6, 6)
        outer.setSpacing(3)
        outer.addWidget(top)
        self.score_visible = QCheckBox("악보 표시 · 가운데 경계를 드래그하여 너비 조절")
        self.score_visible.setChecked(self.preferences.value("score_visible", True, type=bool))
        self.score_panel.setVisible(self.score_visible.isChecked())
        self.score_visible.toggled.connect(self.score_panel.setVisible)
        outer.addWidget(self.score_visible)
        outer.addWidget(self.main_splitter, 1)
        self.setCentralWidget(container)
        self.update_measure_controls()

    def connect_player(self):
        self.player.durationChanged.connect(lambda value: self.seek.setMaximum(value))
        self.player.durationChanged.connect(lambda _: self.update_measure_controls())
        self.player.seekableChanged.connect(lambda _: self.update_measure_controls())
        self.player.positionChanged.connect(self.position_changed)
        self.player.playbackStateChanged.connect(
            lambda state: self.play_button.setText(
                "일시정지" if state == QMediaPlayer.PlaybackState.PlayingState else "재생"
            )
        )
        self.player.errorOccurred.connect(
            lambda _, message: self.show_error(f"재생 오류: {message}")
        )
        availability = self.player.pitchCompensationAvailability()
        if availability == QMediaPlayer.PitchCompensationAvailability.Unavailable:
            self.pitch.setChecked(False)
            self.pitch.setEnabled(False)
            self.pitch.setText("이 재생 장치에서는 음정 유지 불가 · 파일 저장은 가능")
        else:
            self.player.setPitchCompensation(True)
            if availability == QMediaPlayer.PitchCompensationAvailability.AlwaysOn:
                self.pitch.setEnabled(False)

    def refresh_tracks(self):
        selected = self.tracks.currentItem()
        old_path = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.tracks.clear()
        files = discover(self.paths.targets)
        for path in files:
            item = QListWidgetItem(str(path.relative_to(self.paths.targets)))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            self.tracks.addItem(item)
            if str(path) == old_path:
                self.tracks.setCurrentItem(item)
        if self.tracks.currentRow() < 0 and files:
            self.tracks.setCurrentRow(0)
        self.track_count.setText(
            f"{len(files)}곡" if files else "음원 폴더에 파일을 넣고 새로고침하세요."
        )

    def prepare(self):
        if self.worker:
            return
        selected = self.tracks.currentItem()
        if not selected:
            self.status.setText("음원 폴더에 파일을 넣고, 새로고침 후 곡을 선택하세요.")
            return
        source = selected.data(Qt.ItemDataRole.UserRole)
        self.source_name = Path(source).name
        self.pending_source = Path(source)
        args = [
            "separate",
            "--input",
            source,
            "--root",
            str(self.paths.root),
            "--model",
            self.models.currentData(),
            "--device",
            self.devices.currentData(),
            "--events",
        ]
        if self.low_memory.isChecked():
            args.append("--low-memory")
        self.start_job(args)

    def start_job(self, args):
        self.job_result = None
        self.job_error = ""
        self.cancelled = False
        self.raw_buffer = b""
        self.logs.clear()
        self.status.setText("작업을 시작하는 중…")
        self.progress.setRange(0, 0)
        self.set_busy(True)
        process = QProcess(self)
        self.worker = process
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(str(ROOT))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(self.read_stdout)
        process.readyReadStandardError.connect(
            lambda: self.logs.appendPlainText(
                bytes(process.readAllStandardError()).decode("utf-8", "replace").rstrip()
            )
        )
        process.finished.connect(self.job_finished)
        process.errorOccurred.connect(self.process_error)
        process.start(sys.executable, [str(ROOT / "main.py"), *args])

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.job_error = "작업용 Python 프로세스를 시작하지 못했습니다. 가상환경을 확인하세요."
            self.job_finished(1, QProcess.ExitStatus.CrashExit)

    def read_stdout(self):
        if not self.worker:
            return
        # Buffer bytes to avoid splitting a UTF-8 Korean character at a read boundary.
        if not hasattr(self, "raw_buffer"):
            self.raw_buffer = b""
        self.raw_buffer += bytes(self.worker.readAllStandardOutput())
        while b"\n" in self.raw_buffer:
            line, self.raw_buffer = self.raw_buffer.split(b"\n", 1)
            self.consume_line(line.decode("utf-8", "replace").strip())

    def consume_line(self, line):
        if not line.startswith(EVENT_PREFIX):
            if line:
                self.logs.appendPlainText(line)
            return
        try:
            data = json.loads(line[len(EVENT_PREFIX) :])
        except json.JSONDecodeError:
            self.logs.appendPlainText(line)
            return
        kind = data.get("type")
        if kind == "progress":
            self.status.setText(data["message"])
            value = data.get("fraction")
            self.progress.setRange(0, 0 if value is None else 100)
            if value is not None:
                self.progress.setValue(max(0, min(100, round(value * 100))))
        elif kind in ("result", "exported", "score_result"):
            self.job_result = data
        elif kind == "error":
            self.job_error = data["message"]

    def job_finished(self, code, _status):
        if not self.worker:
            return
        self.read_stdout()
        if getattr(self, "raw_buffer", b""):
            self.consume_line(self.raw_buffer.decode("utf-8", "replace").strip())
        self.raw_buffer = b""
        self.worker.deleteLater()
        self.worker = None
        self.set_busy(False)
        self.progress.setRange(0, 100)
        if self.cancelled:
            self.progress.setValue(0)
            self.status.setText("작업을 취소했습니다. 완료된 캐시는 유지됩니다.")
        elif code == 0 and self.job_result:
            self.progress.setValue(100)
            if self.job_result["type"] == "result":
                self.load_result(self.job_result)
            elif self.job_result["type"] == "score_result":
                try:
                    self.score_panel.load_timeline(self.job_result["path"])
                    self.status.setText("악보 사전 분석 완료 · 시간표에서 결과를 확인하세요.")
                except (ValueError, OSError, KeyError) as error:
                    self.show_error(str(error))
            else:
                self.status.setText(f"저장 완료: {self.job_result['path']}")
        else:
            self.progress.setValue(0)
            self.show_error(self.job_error or "처리에 실패했습니다. 아래 로그를 확인하세요.")
        if self.close_pending:
            self.close()

    def load_result(self, result):
        self.output_path = Path(result["instrumental"])
        self.player.setSource(QUrl.fromLocalFile(str(self.output_path)))
        self.player.setPlaybackRate(self.speed.value() / 100)
        self.current_track.setText(self.source_name)
        mode = (
            "캐시 사용 · 재분리 없음"
            if result["cached"]
            else f"분리 완료 · {result['device'].upper()}"
        )
        self.status.setText(f"{mode} · 재생 버튼을 누르세요.")
        self.play_button.setEnabled(True)
        self.export_button.setEnabled(True)
        if self.pending_source:
            self.source_path = self.pending_source
            self.score_panel.bind_track(self.source_path, result["source_hash"])
            self.metronome.bind_track(
                self.paths.processed / result["source_hash"] / "metronome.json"
            )

    def analyze_score(self, options):
        if self.worker:
            return
        self.player.pause()
        args = ["score-analyze", "--root", str(self.paths.root), "--events"]
        for key, value in options.items():
            if key == "force":
                if value:
                    args.append("--force")
            elif value != "":
                args.extend([f"--{key}", str(value)])
        self.start_job(args)

    def set_busy(self, busy):
        for widget in (
            self.models,
            self.devices,
            self.low_memory,
            self.prepare_button,
            self.tracks,
            self.refresh_button,
        ):
            widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.export_button.setEnabled(not busy and self.output_path is not None)
        self.score_panel.set_busy(busy)
        self.play_button.setEnabled(not busy and self.output_path is not None)
        self.update_measure_controls(busy)

    def cancel_job(self):
        process = self.worker
        if not process:
            return
        self.cancelled = True
        self.status.setText("작업을 취소하는 중…")
        self.cancel_button.setEnabled(False)
        if os.name == "nt":
            # Windows terminate() does not stop a console Python worker.
            QProcess.execute("taskkill", ["/PID", str(process.processId()), "/T", "/F"])
        else:
            process.terminate()
            QTimer.singleShot(2500, lambda: process.kill() if self.worker is process else None)

    def change_speed(self, value):
        self.speed_label.setText(f"{value / 100:.2f}×")
        self.player.setPlaybackRate(value / 100)

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def position_changed(self, position):
        if not self.seek.isSliderDown():
            self.seek.setValue(position)
        self.clock.setText(f"{time_label(position)} / {time_label(self.player.duration())}")
        self.score_panel.on_position(position)
        self.update_measure_controls()

    def update_measure_controls(self, busy=None):
        timeline = self.score_panel.timeline
        busy = self.worker is not None if busy is None else busy
        duration = self.player.duration() / 1000
        limits = timeline.playable_measures(duration) if timeline else None
        enabled = bool(not busy and self.output_path and limits and self.player.isSeekable())
        self.previous_measure.setEnabled(enabled)
        self.next_measure.setEnabled(enabled)
        if timeline is None:
            self.measure_hint.setText("악보 사전 분석 후 마디 단위로 이동할 수 있습니다.")
        elif busy:
            self.measure_hint.setText("작업 완료 후 마디 단위로 이동할 수 있습니다.")
        elif duration > 0 and limits is None:
            self.measure_hint.setText("음원 길이 안에 마디가 없습니다. 시간표 보정을 확인하세요.")
        elif not enabled:
            self.measure_hint.setText("음원의 재생·탐색을 준비하는 중입니다.")
        else:
            index = timeline.index_at(self.player.position() / 1000, timeline.data.get("offset", 0))
            if index is None:
                self.measure_hint.setText("악보 시작 전 / 종료 · 마디 이동으로 악보에 진입합니다.")
            else:
                cue = timeline.cues[index]
                self.measure_hint.setText(
                    f"{cue['measure']}마디 · {cue['visit']}회차 · "
                    f"연주 순서 {index + 1}/{len(timeline.cues)}"
                )

    def jump_measures(self, direction):
        timeline = self.score_panel.timeline
        if self.worker or not self.output_path or timeline is None or not self.player.isSeekable():
            return
        target = timeline.measure_target(
            self.player.position() / 1000,
            direction * self.measure_step.value(),
            self.player.duration() / 1000,
        )
        if target is not None:
            self.seek_to(target[1])

    def seek_to(self, milliseconds):
        if not self.player.isSeekable():
            return
        # All navigation paths explicitly resynchronize even when seeking within
        # the same beat. Preserve transport state and original-media coordinates.
        self.metronome.begin_seek()
        try:
            self.player.setPosition(max(0, min(milliseconds, self.player.duration())))
        finally:
            self.metronome.end_seek(self.player.position())
        self.position_changed(self.player.position())

    def export_audio(self):
        if not self.output_path or self.worker:
            return
        speed = self.speed.value() / 100
        suggested = self.output_path.parent / f"practice_{speed:.2f}x.wav"
        filename, selected_filter = QFileDialog.getSaveFileName(
            self,
            "음정을 유지한 연습 음원 저장",
            str(suggested),
            "WAV (*.wav);;FLAC (*.flac)",
        )
        if filename:
            destination = Path(filename)
            if not destination.suffix:
                destination = destination.with_suffix(
                    ".flac" if selected_filter.startswith("FLAC") else ".wav"
                )
                if (
                    destination.exists()
                    and QMessageBox.question(
                        self, "파일 교체", f"기존 파일을 교체할까요?\n{destination}"
                    )
                    != QMessageBox.StandardButton.Yes
                ):
                    return
            self.start_job(
                [
                    "export",
                    "--input",
                    str(self.output_path),
                    "--output",
                    str(destination),
                    "--speed",
                    str(speed),
                    "--events",
                    # QFileDialog (or the appended-extension dialog) confirmed replacement.
                    *(["--overwrite"] if destination.exists() else []),
                ]
            )

    def show_error(self, message):
        self.status.setText(message)
        self.logs.appendPlainText(message)

    def closeEvent(self, event):
        if self.worker:
            self.close_pending = True
            self.cancel_job()
            event.ignore()
            return
        self.player.stop()
        self.preferences.setValue("splitter", self.main_splitter.saveState())
        self.preferences.setValue("score_visible", self.score_visible.isChecked())
        self.preferences.setValue("window_width", self.width())
        self.preferences.setValue("window_height", self.height())
        self.preferences.setValue("measure_step", self.measure_step.value())
        self.preferences.sync()
        self.metronome.shutdown()
        self.score_panel.view.clear()
        self.score_panel.camera_timer.stop()
        event.accept()


def run() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Magic-Instrumental")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = Window()
    window.show()
    return app.exec()
