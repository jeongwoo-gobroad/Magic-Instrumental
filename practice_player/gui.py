# SPDX-License-Identifier: GPL-3.0-only
"""Qt owns playback; a separate Python process owns inference and export."""

import json
import os
import sys
import time
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
    QFrame,
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
from .count_in import CountIn
from .metronome import MetronomeBar
from .region_loop import RegionLoop
from .score_gui import ScorePanel
from .updates import UpdateBar, open_updater

STYLE = """
QWidget { background: #11151d; color: #e7edf7; font-size: 14px; }
QMainWindow { background: #11151d; }
QLabel#title { font-size: 26px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #99a9bf; }
QLabel#metronomeState { color: #87ddc4; font-size: 13px; font-weight: 600; }
QLabel#track { font-size: 21px; font-weight: 600; }
QFrame#updateBar { border: 1px solid #35425a; border-radius: 7px; }
QLabel#currentVersion { color: #87ddc4; font-weight: 700; }
QLabel#speed { color: #87ddc4; font-size: 32px; font-weight: 700; }
QPushButton, QComboBox { background: #242e3e; border: 1px solid #35425a;
    border-radius: 7px; padding: 6px 10px; }
QPushButton:hover { background: #35425a; }
QPushButton:focus, QComboBox:focus { border-color: #87ddc4; }
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
        self._worker_job = None
        self.output_path = None
        self.base_instrumental = None
        self.pitch_semitones = 0
        self._pitch_position = 0
        self._pitch_pending = None
        self.raw_buffer = b""
        self.job_result = None
        self.job_error = ""
        self.cancelled = False
        self.close_pending = False
        self._loading_track = False
        self._region_transition = False
        self._region_bounds = None
        self._region_playing = False
        self._region_generation = 0
        self._region_closed = False
        self.source_name = ""
        self.source_path = None
        self.pending_source = None
        self.preferences = QSettings(
            str(self.paths.processed / "ui.ini"), QSettings.Format.IniFormat
        )
        self.setWindowTitle("Magic-Instrumental · 인스트루먼탈 연습실")
        available = self.screen().availableGeometry()
        self.setMinimumSize(800, 560)
        self.resize(
            min(self.preferences.value("window_width", 1500, type=int), available.width() - 24),
            min(self.preferences.value("window_height", 930, type=int), available.height() - 56),
        )
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.75)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.region_timer = QTimer(self)
        self.region_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.region_timer.setInterval(5)
        self.region_timer.timeout.connect(self.check_region)
        self.build_ui()
        self.connect_player()
        self.refresh_tracks()
        self.set_busy(False)

    def build_ui(self):
        self.metronome = MetronomeBar(self.player, preferences=self.preferences)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)
        title = QLabel("Magic-Instrumental")
        title.setObjectName("title")
        heading = QHBoxLayout()
        heading.addWidget(title, 1)
        self.theme = QComboBox()
        self.theme.addItem("밝은 화면", "light")
        self.theme.addItem("어두운 화면", "dark")
        self.theme.setCurrentIndex(self.theme.findData(self.preferences.value("theme", "dark")))
        self.theme.currentIndexChanged.connect(self.apply_theme)
        heading.addWidget(self.theme)
        layout.addLayout(heading)
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
        self.tracks.setWordWrap(True)
        self.tracks.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.tracks.setMinimumHeight(105)
        self.tracks.setMaximumHeight(420)
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
        self.advanced_button = QPushButton("▸ 반주 만들기 설정")
        self.advanced_button.setCheckable(True)
        right.addWidget(self.advanced_button)
        advanced = QWidget()
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.addWidget(QLabel("분리 모델"))
        advanced.setVisible(False)
        self.advanced_button.toggled.connect(advanced.setVisible)
        self.advanced_button.toggled.connect(
            lambda on: self.advanced_button.setText(("▾" if on else "▸") + " 반주 만들기 설정")
        )
        right.addWidget(advanced)
        self.models = QComboBox()
        for key, model in MODELS.items():
            self.models.addItem(model.label, key)
        advanced_layout.addWidget(self.models)
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
        advanced_layout.addLayout(options)
        actions = QHBoxLayout()
        self.prepare_button = QPushButton("선택한 곡의 반주 준비")
        self.prepare_button.setObjectName("primary")
        self.prepare_button.clicked.connect(self.prepare)
        actions.addWidget(self.prepare_button, 1)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_job)
        actions.addWidget(self.cancel_button)
        right.addLayout(actions)
        self.status = QLabel(
            "위 목록에서 곡을 선택하고 ‘반주 준비’를 누르세요. 처음에는 다운로드가 필요합니다."
        )
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
        right.addWidget(self.separator())
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
        self.loop = QCheckBox("곡 전체 반복")
        self.loop.toggled.connect(self.whole_loop_changed)
        controls.addWidget(self.loop)
        controls.addStretch()
        right.addLayout(controls)
        self.count_in = CountIn(self.metronome, self.preferences)
        self.count_in.active_changed.connect(self.update_play_button)
        self.count_in.finished.connect(self.play_after_count)
        self.count_in.failed.connect(self.show_error)
        right.addWidget(self.count_in)
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
        self.region = RegionLoop(self.preferences)
        self.region.changed.connect(self.region_changed)
        self.region.current_requested.connect(
            lambda start: self.region.select_current(start, self.player.position())
        )
        right.addWidget(self.separator())
        right.addWidget(self.region)
        right.addWidget(self.separator())
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
        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("반주 키"))
        self.key = QSpinBox()
        self.key.setRange(-12, 12)
        self.key.setSuffix(" 반음")
        self.key.setToolTip("반주의 높낮이를 바꿉니다. 악보에 적힌 음표는 그대로입니다.")
        key_row.addWidget(self.key)
        self.key_apply = QPushButton("키 적용")
        self.key_apply.clicked.connect(self.change_key)
        key_row.addWidget(self.key_apply)
        self.key_reset = QPushButton("원곡 키")
        self.key_reset.clicked.connect(self.reset_key)
        key_row.addWidget(self.key_reset)
        right.addLayout(key_row)
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
        right.addWidget(self.separator())
        right.addWidget(self.export_button)
        self.magic_button = QPushButton("태블릿으로 가져가기 · .magic 저장")
        self.magic_button.setToolTip(
            "현재 반주 키, GP 전체 파트(또는 PDF)와 분석 결과를 한 파일로 저장합니다."
        )
        self.magic_button.clicked.connect(self.export_magic)
        self.magic_button.setEnabled(False)
        right.addWidget(self.magic_button)
        self.ensemble_button = QPushButton("Session 모드 · 함께 연습하기")
        self.ensemble_button.setToolTip(
            ".magic을 저장한 뒤 합주 화면에서 엽니다. "
            "PC·태블릿 모두 이 화면에서 리더 또는 멤버로 참가합니다."
        )
        self.ensemble_button.clicked.connect(self.open_ensemble)
        right.addWidget(self.ensemble_button)
        right.addStretch()
        splitter.addWidget(panel)
        splitter.setSizes([290, 680])
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(300)
        self.logs.setMaximumHeight(75)
        self.logs.setPlaceholderText("처리 로그")
        logs_toggle = QPushButton("처리 내역 보기")
        logs_toggle.setCheckable(True)
        logs_toggle.toggled.connect(self.logs.setVisible)
        layout.addWidget(logs_toggle)
        layout.addWidget(self.logs)
        self.logs.setVisible(False)
        controller = QScrollArea()
        controller.setWidgetResizable(True)
        controller.setWidget(root)
        controller.setMinimumWidth(410)
        self.score_panel = ScorePanel(self.paths)
        self.score_panel.attach_player(self.player)
        self.score_panel.setMinimumWidth(300)
        self.score_panel.analysis_requested.connect(self.analyze_score)
        self.score_panel.gp_prepare_requested.connect(self.prepare_gp)
        self.score_panel.seek_requested.connect(self.seek_to)
        self.score_panel.timeline_changed.connect(self.update_measure_controls)
        self.score_panel.timeline_changed.connect(self.refresh_region)
        self.score_panel.timeline_changed.connect(self.update_export_controls)
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
        self.score_panel.bpm_provider = lambda: self.metronome.bpm.value()
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(6, 4, 6, 6)
        outer.setSpacing(3)
        self.update_bar = UpdateBar(ROOT)
        self.update_bar.update_requested.connect(self.begin_update)
        outer.addWidget(self.update_bar)
        outer.addWidget(self.metronome)
        self.score_visible = QCheckBox("악보 표시 · 가운데 경계를 드래그하여 너비 조절")
        self.score_visible.setChecked(self.preferences.value("score_visible", True, type=bool))
        self.score_panel.setVisible(self.score_visible.isChecked())
        self.score_visible.toggled.connect(self.score_panel.setVisible)
        outer.addWidget(self.score_visible)
        outer.addWidget(self.main_splitter, 1)
        self.support = QLabel(
            '버그 및 개선 사항 제보: '
            '<a href="mailto:pps.jeongwoo@gmail.com">pps.jeongwoo@gmail.com</a>'
        )
        self.support.setObjectName("muted")
        self.support.setOpenExternalLinks(True)
        self.support.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.support)
        self.setCentralWidget(container)
        self.update_measure_controls()
        self.apply_theme()

    def connect_player(self):
        self.player.mediaStatusChanged.connect(self.restore_pitch_position)
        self.player.durationChanged.connect(lambda value: self.seek.setMaximum(value))
        self.player.durationChanged.connect(lambda _: self.update_measure_controls())
        self.player.seekableChanged.connect(lambda _: self.update_measure_controls())
        self.player.positionChanged.connect(self.position_changed)
        self.player.playbackStateChanged.connect(self.update_play_button)
        self.player.playbackStateChanged.connect(self.region_transport_changed)
        self.player.mediaStatusChanged.connect(self.region_media_status_changed)
        self.player.sourceChanged.connect(self.region_source_changed)
        self.player.durationChanged.connect(lambda _: self.refresh_region())
        self.player.seekableChanged.connect(lambda _: self.refresh_region())
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
        if self.worker:
            return
        environment = QProcessEnvironment.systemEnvironment()
        if os.name == "nt":
            from .windows_job import WorkerJob

            try:
                self._worker_job = WorkerJob()
            except OSError as error:
                self.show_error(f"Windows 작업 프로세스를 준비하지 못했습니다: {error}")
                return
            environment.insert("MAGIC_WORKER_JOB", self._worker_job.name)
        self.job_result = None
        self.job_error = ""
        self.cancelled = False
        self.raw_buffer = b""
        self.logs.clear()
        self.status.setText("작업을 시작하는 중…")
        self.progress.setRange(0, 0)
        process = QProcess(self)
        self.worker = process
        self.set_busy(True)
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
        elif kind in (
            "result",
            "exported",
            "score_result",
            "gp_prepared",
            "pitch_result",
            "magic_result",
        ):
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
        if self._worker_job:
            self._worker_job.close()
            self._worker_job = None
        self.set_busy(False)
        self.progress.setRange(0, 100)
        if self.cancelled:
            self.progress.setValue(0)
            self.status.setText("작업을 취소했습니다. 완료된 캐시는 유지됩니다.")
        elif code == 0 and self.job_result:
            self.progress.setValue(100)
            if self.job_result["type"] == "result":
                self.load_result(self.job_result)
            elif self.job_result["type"] == "gp_prepared":
                self.score_panel.accept_gp(self.job_result)
                self.status.setText("GP 파트가 준비되었습니다. 악보 사전 분석을 눌러 주세요.")
            elif self.job_result["type"] == "pitch_result":
                self.load_pitch(self.job_result)
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
        self._loading_track = True
        try:
            self._load_result(result)
        finally:
            self._loading_track = False
            self.set_busy(self.worker is not None)

    def _load_result(self, result):
        self.count_in.cancel()
        source_hash = result.get("source_hash")
        self.region.bind_track(
            self.paths.processed / source_hash / "practice_loop.json" if source_hash else None
        )
        self.output_path = Path(result["instrumental"])
        self._pitch_pending = None
        self.base_instrumental = self.output_path
        self.pitch_semitones = 0
        self.key.setValue(0)
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
        self.update_bar.set_busy(busy)
        if busy:
            self.count_in.cancel()
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
        self.key_apply.setEnabled(not busy and self.base_instrumental is not None)
        self.key.setEnabled(not busy)
        self.key_reset.setEnabled(not busy and self.base_instrumental is not None)
        self.update_export_controls()
        self.play_button.setEnabled(not busy and self.output_path is not None)
        self.update_measure_controls(busy)
        self.refresh_region(busy=busy)

    def separator(self):
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("separator")
        return line

    def apply_theme(self, *_):
        from .theme import stylesheet

        mode = self.theme.currentData()
        self.setStyleSheet(stylesheet(STYLE, mode))
        link_color = "#12624e" if mode == "light" else "#87ddc4"
        self.support.setText(
            '버그 및 개선 사항 제보: '
            f'<a style="color:{link_color}" href="mailto:pps.jeongwoo@gmail.com">'
            'pps.jeongwoo@gmail.com</a>'
        )
        self.preferences.setValue("theme", mode)

    def prepare_gp(self, options):
        if self.worker:
            return
        self.player.pause()
        args = ["gp-prepare", "--root", str(self.paths.root), "--events"]
        for key, value in options.items():
            args.extend([f"--{key}", str(value)])
        self.start_job(args)

    def change_key(self):
        if self.worker or not self.base_instrumental:
            return
        self.count_in.cancel()
        self.player.pause()
        self._pitch_position = self.player.position()
        self.start_job(
            [
                "pitch",
                "--root",
                str(self.paths.root),
                "--events",
                "--input",
                str(self.base_instrumental),
                "--semitones",
                str(self.key.value()),
            ]
        )

    def load_pitch(self, info):
        self._loading_track = True
        try:
            self.output_path = Path(info["path"])
            self.pitch_semitones = info["semitones"]
            self.key.setValue(self.pitch_semitones)
            self._pitch_pending = self._pitch_position
            self.player.setSource(QUrl.fromLocalFile(str(self.output_path)))
            self.player.setPlaybackRate(self.speed.value() / 100)
            self.player.setPosition(self._pitch_position)
            self.metronome.reanchor(self._pitch_position)
            self.status.setText(
                f"반주 키 {self.pitch_semitones:+d}반음 · 재생을 눌러 이어서 연습하세요."
            )
        finally:
            self._loading_track = False
        self.refresh_region()

    def reset_key(self):
        self.key.setValue(0)
        self.change_key()

    def restore_pitch_position(self, status):
        if self._pitch_pending is not None and status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            target, self._pitch_pending = self._pitch_pending, None
            self.player.setPosition(target)
            self.metronome.reanchor(target)

    def update_export_controls(self):
        self.magic_button.setEnabled(
            not self.worker
            and self.output_path is not None
            and self.score_panel.timeline is not None
        )

    def export_magic(self):
        if self.worker or not self.score_panel.timeline_path or not self.output_path:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "태블릿용 연습 파일 저장",
            str(self.paths.root / (self.source_path.stem + ".magic")),
            "Magic 연습 파일 (*.magic)",
        )
        if not filename:
            return
        target = Path(filename).with_suffix(".magic")
        overwrite = False
        if target.exists():
            overwrite = (
                QMessageBox.question(self, "파일 교체", f"{target.name}을 교체할까요?")
                == QMessageBox.StandardButton.Yes
            )
            if not overwrite:
                return
        from .score import atomic_json

        self.player.pause()
        request = self.paths.processed / "magic-export-request.json"
        atomic_json(
            request,
            {
                "root": str(self.paths.root),
                "audio": str(self.source_path),
                "instrumental": str(self.output_path),
                "pdf": str(self.score_panel.pdf),
                "timeline": str(self.score_panel.timeline_path),
                "output": str(target),
                "overwrite": overwrite,
                "title": self.source_path.stem,
                "pitch_semitones": self.pitch_semitones,
                "practice": {
                    "bpm": self.metronome.bpm.value(),
                    "beats": self.metronome.beats.value(),
                    "unit": self.metronome.unit.currentData(),
                    "origin": self.metronome.origin.value(),
                },
            },
        )
        self.start_job(["magic-export", "--request", str(request), "--events"])

    def open_ensemble(self):
        self.count_in.cancel()
        self.player.pause()
        self.metronome.set_running(False)
        if not QDesktopServices.openUrl(QUrl(
            "https://jeongwoo-gobroad.github.io/Magic-Instrumental/#ensemble"
        )):
            self.show_error(
                "기본 브라우저를 열지 못했습니다. README의 태블릿 재생기를 열어 주세요."
            )

    def cancel_job(self):
        process = self.worker
        if not process:
            return
        self.cancelled = True
        self.status.setText("작업을 취소하는 중…")
        self.cancel_button.setEnabled(False)
        if os.name == "nt":
            if self._worker_job:
                self._worker_job.close()
            process.kill()  # Also stop a venv launcher that has not entered main.py.
        else:
            process.terminate()
            QTimer.singleShot(2500, lambda: process.kill() if self.worker is process else None)

    def change_speed(self, value):
        self.speed_label.setText(f"{value / 100:.2f}×")
        self.player.setPlaybackRate(value / 100)

    def toggle_play(self):
        self._region_generation += 1
        if self.count_in.active:
            self.count_in.cancel()
            self._region_playing = False
            return
        if self.worker or not self.output_path:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            bounds = self.region.bounds()
            if bounds and not bounds[0] <= self.player.position() < bounds[1]:
                self.seek_to(bounds[0])
            elif self.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                self.seek_to(0)
            force_count = bool(bounds and self.region.count_each.isChecked())
            if not self.count_in.start(force=force_count):
                self.player.play()

    def play_after_count(self):
        if not self.worker and not self.close_pending and self.output_path:
            self.player.play()

    def update_play_button(self, *_):
        self.play_button.setText(
            "카운트 취소"
            if self.count_in.active
            else "일시정지"
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            else "재생"
        )

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
        self._region_generation += 1
        self.count_in.cancel()
        self._region_playing = self.player.isPlaying()
        if not self.player.isSeekable():
            return
        bounds = self.region.bounds()
        if bounds:
            milliseconds = max(bounds[0], min(milliseconds, bounds[1] - 1))
        # All navigation paths explicitly resynchronize even when seeking within
        # the same beat. Preserve transport state and original-media coordinates.
        self.metronome.begin_seek()
        try:
            self.player.setPosition(max(0, min(milliseconds, self.player.duration())))
        finally:
            self.metronome.end_seek(self.player.position())
        self.position_changed(self.player.position())

    def refresh_region(self, *, busy=None):
        if self._loading_track:
            return
        busy = self.worker is not None if busy is None else busy
        self.region.set_context(
            self.score_panel.timeline,
            self.player.duration() / 1000,
            bool(not busy and self.output_path and self.player.isSeekable()),
        )

    def region_source_changed(self, *_):
        self._region_generation += 1
        self._region_playing = False
        self.count_in.cancel()
        if not self._loading_track:
            self.region.bind_track(None)

    def whole_loop_changed(self, on):
        if on:
            self.region.enabled.setChecked(False)
        self.player.setLoops(-1 if on else 1)

    def region_changed(self, reason):
        previous = self._region_bounds
        self._region_generation += 1
        self.count_in.cancel()
        bounds = self.region.bounds()
        self._region_bounds = bounds
        self.count_in.set_external_enabled(bool(bounds and self.region.count_each.isChecked()))
        if bounds:
            self.loop.setChecked(False)
            self.player.setLoops(1)
            self.region_timer.start()
        else:
            self.region_timer.stop()
        if reason == "enabled" and bounds:
            self.player.pause()
            self.seek_to(bounds[0])
            self.region.save_selection()
        elif reason in {"range", "context"} and previous:
            # Editing a running range must not silently jump/play a different interval.
            self.player.pause()
        self._region_playing = self.player.isPlaying()

    def region_transport_changed(self, state):
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._region_playing = True
        elif state == QMediaPlayer.PlaybackState.PausedState and not self._region_transition:
            self._region_playing = False

    def region_media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self._region_bounds and self._region_playing:
                generation = self._region_generation
                QTimer.singleShot(0, lambda: self.region_eof(generation))
        elif status in {
            QMediaPlayer.MediaStatus.NoMedia,
            QMediaPlayer.MediaStatus.InvalidMedia,
            QMediaPlayer.MediaStatus.LoadingMedia,
        }:
            self._region_playing = False
            if not self._region_transition:
                self.region.enabled.setChecked(False)

    def region_eof(self, generation):
        if (
            generation == self._region_generation
            and self._region_playing
            and self.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia
        ):
            self.restart_region()

    def check_region(self):
        if (
            not self._region_bounds
            or self._region_transition
            or self.count_in.active
            or self.worker
            or not self.player.isPlaying()
            or self.metronome.suspended
        ):
            return
        start, end = self._region_bounds
        position = self.metronome.media_seconds(time.monotonic()) * 1000
        if position >= end or position < start - 1:
            self.restart_region()

    def restart_region(self):
        bounds = self.region.bounds()
        if (
            not bounds
            or self._region_transition
            or self.count_in.active
            or self.worker
            or self._region_closed
            or not self.player.isSeekable()
        ):
            return
        self._region_transition = True
        try:
            self.player.pause()
            self.seek_to(bounds[0])
            if self.region.count_each.isChecked():
                self.count_in.start(force=True)
            else:
                self.player.play()
        finally:
            self._region_transition = False

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
            if destination.exists() and (
                destination.resolve() == self.source_path
                or destination.resolve().is_relative_to(self.paths.targets.resolve())
            ):
                self.show_error("원본 음원은 덮어쓸 수 없습니다. 다른 파일 이름을 선택하세요.")
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

    def begin_update(self):
        if self.worker or self.close_pending:
            return
        try:
            open_updater(ROOT)
        except (OSError, RuntimeError) as error:
            self.update_bar.show_failure(str(error))
            return
        self.close()

    def closeEvent(self, event):
        self._region_closed = True
        self._region_generation += 1
        self.region_timer.stop()
        self.count_in.cancel()
        if self.worker:
            self.close_pending = True
            self.cancel_job()
            event.ignore()
            return
        # setSource stops playback and releases decoder/output workers itself.
        # PySide explicitly releases the GIL for setSource, unlike stop(); a
        # separate stop can deadlock with native renderer teardown on Windows.
        # Clear media before releasing the click sound sources as well.
        self.player.setSource(QUrl())
        self.preferences.setValue("splitter", self.main_splitter.saveState())
        self.preferences.setValue("score_visible", self.score_visible.isChecked())
        self.preferences.setValue("window_width", self.width())
        self.preferences.setValue("window_height", self.height())
        self.preferences.setValue("measure_step", self.measure_step.value())
        self.preferences.sync()
        self.count_in.shutdown()
        self.metronome.shutdown()
        self.score_panel.view.clear()
        self.score_panel.camera_timer.stop()
        self.update_bar.stop()
        event.accept()


def run() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Magic-Instrumental")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = Window()
    window.show()
    QTimer.singleShot(0, window.update_bar.check)
    return app.exec()
