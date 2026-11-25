# SPDX-License-Identifier: GPL-3.0-only
"""Qt owns playback; a separate Python process owns inference and export."""

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

from PySide6.QtCore import QProcess, QProcessEnvironment, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
from .cache_locations import save_cache_locations
from .cli import EVENT_PREFIX
from .config import MODELS, ROOT, Paths
from .count_in import CountIn
from .metronome import MetronomeBar
from .region_loop import RegionLoop
from .score_gui import ScorePanel
from .updates import UpdateBar, open_updater
from .variants import (
    ALL_VARIANTS,
    SUPPORTED_VARIANTS,
    TARGET_LABELS,
    canonical_combination,
    is_combination,
    removal_targets,
    variant_label,
)

STYLE = """
QWidget { background: #11151d; color: #e7edf7; font-size: 14px; }
QMainWindow { background: #11151d; }
QLabel#title { font-size: 26px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #99a9bf; }
QLabel#metronomeState { color: #87ddc4; font-size: 13px; font-weight: 600; }
QLabel#track { font-size: 21px; font-weight: 600; }
QFrame#updateBar { border: 1px solid #35425a; border-radius: 7px; }
QLabel#currentVersion { color: #87ddc4; font-weight: 700; }
QLabel#playbackCacheCount { color: #87ddc4; font-size: 12px; font-weight: 600; }
QLabel#speed { color: #87ddc4; font-size: 32px; font-weight: 700; }
QPushButton, QComboBox { background: #242e3e; border: 1px solid #35425a;
    border-radius: 7px; padding: 6px 10px; }
QPushButton:hover { background: #35425a; }
QPushButton:focus, QComboBox:focus { border-color: #87ddc4; }
QPushButton:disabled { color: #607087; background: #1b2330; }
QComboBox:disabled { color: #99a9bf; background: #1b2330; }
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


def export_variant_request(item):
    request = {key: item[key] for key in ("id", "label", "mode", "target", "path")}
    if "targets" in item:
        request["targets"] = list(item["targets"])
    # Keep the prepared cache identity; a later export must not bless changed bytes.
    if all(key in item for key in ("sha256", "source_hash", "frames")):
        request.update(
            expected_sha256=item["sha256"], expected_source_sha256=item["source_hash"],
            expected_frames=item["frames"],
        )
    return request


def cached_audio_exists(item):
    """Cheap availability check; the switching worker still verifies hashes and frames."""
    try:
        path = Path(item["path"])
        size = path.stat().st_size
        expected = item.get("bytes", 0)
        return (
            not path.is_symlink() and path.is_file() and size > 0
            and (not expected or size == expected)
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


class PlaybackVariantCombo(QComboBox):
    opening = Signal()

    def showPopup(self):
        self.opening.emit()
        if self.isEnabled() and self.count() > 0:
            super().showPopup()


class CacheLocationsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.player_window = window
        self.fields = {}
        self.setObjectName("cacheLocationsDialog")
        self.setWindowTitle("상세 옵션 · 캐시 저장 위치")
        available = window.screen().availableGeometry()
        self.resize(min(740, available.width() - 40), min(500, available.height() - 60))
        layout = QVBoxLayout(self)
        description = QLabel(
            "한 번 준비한 음원과 악보 분석 결과를 저장해 다음에 다시 사용합니다. "
            "저장할 폴더를 고르거나 경로를 직접 입력하세요."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        rows = QVBoxLayout(content)
        rows.setContentsMargins(0, 6, 0, 6)
        self.original = (str(window.paths.audio_cache), str(window.paths.score_cache))
        for name, title, path in (
            ("audioCacheFolder", "음원 캐시 폴더", window.paths.audio_cache),
            ("scoreCacheFolder", "악보 분석 캐시 폴더", window.paths.score_cache),
        ):
            field = self.add_path(rows, name, title, path, folder=True, editable=True)
            self.fields[name] = field
            field.textChanged.connect(self.update_buttons)
        note = QLabel(
            "저장하면 다음 음원 준비·불러오기와 악보 분석부터 적용됩니다. "
            "현재 열린 곡은 그대로 유지되고, 기존 파일은 자동으로 옮기거나 삭제하지 않습니다. "
            "이전 캐시를 쓰려면 이전 폴더를 다시 선택하세요."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        rows.addWidget(note)
        portable_note = QLabel(
            ".magic에서 가져온 음원과 악보는 하나의 묶음으로 음원 캐시 폴더에 저장됩니다."
        )
        portable_note.setWordWrap(True)
        portable_note.setObjectName("muted")
        rows.addWidget(portable_note)
        current_toggle = QPushButton("현재 곡에 사용 중인 파일 보기")
        current_toggle.setObjectName("currentCacheFilesToggle")
        current_toggle.setCheckable(True)
        rows.addWidget(current_toggle)
        current = QWidget()
        current.setObjectName("currentCacheFiles")
        self.current_rows = QVBoxLayout(current)
        self.current_rows.setContentsMargins(0, 6, 0, 0)
        self.refresh_current_files()
        rows.addWidget(current)
        current.hide()
        current_toggle.toggled.connect(current.setVisible)
        current_toggle.toggled.connect(lambda checked: current_toggle.setText(
            "현재 곡에 사용 중인 파일 접기" if checked else "현재 곡에 사용 중인 파일 보기"
        ))
        rows.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.notice = QLabel(window.paths.cache_location_error)
        self.notice.setObjectName("cacheLocationNotice")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        actions = QDialogButtonBox()
        self.defaults = actions.addButton("기본 위치로", QDialogButtonBox.ButtonRole.ResetRole)
        self.defaults.setObjectName("resetCacheLocations")
        self.defaults.clicked.connect(self.restore_defaults)
        self.save_button = actions.addButton("저장", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setObjectName("saveCacheLocations")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        self.close_button = actions.addButton("닫기", QDialogButtonBox.ButtonRole.RejectRole)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(150)
        self.refresh_timer.timeout.connect(self.refresh_editing)
        if window.worker:
            self.notice.setText("현재 작업이 끝나면 저장 위치를 변경할 수 있습니다.")
            self.refresh_timer.start()
        self.update_buttons()

    def refresh_editing(self):
        if self.player_window.worker:
            return
        self.refresh_timer.stop()
        for name, field in self.fields.items():
            field.setEnabled(True)
            self.findChild(QPushButton, name + "Browse").setEnabled(True)
        self.notice.setText(self.player_window.paths.cache_location_error)
        self.refresh_current_files()
        self.update_buttons()

    def refresh_current_files(self):
        while self.current_rows.count():
            widget = self.current_rows.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        window = self.player_window
        song = QLabel(window.source_name or "아직 연습할 곡을 열지 않았습니다.")
        song.setTextFormat(Qt.TextFormat.PlainText)
        song.setWordWrap(True)
        song.setObjectName("cacheCurrentSong")
        self.current_rows.addWidget(song)
        locations = [("audioCache", "음원 캐시 파일", window.base_instrumental,
                      "아직 음원을 준비하지 않았습니다.")]
        if window.output_path and window.output_path != window.base_instrumental:
            locations.append((
                "playbackCache", "키를 적용한 재생 파일", window.output_path, "",
            ))
        locations.append((
            "scoreCache", "악보 분석 캐시 파일", window.score_panel.timeline_path,
            "아직 악보 분석 결과를 불러오지 않았습니다.",
        ))
        for name, title, path, empty in locations:
            row = QWidget()
            contents = QVBoxLayout(row)
            contents.setContentsMargins(0, 0, 0, 0)
            self.add_path(contents, name, title, path, empty=empty)
            self.current_rows.addWidget(row)

    def add_path(self, layout, name, title, path, *, folder=False, editable=False, empty=""):
        field = QLineEdit(str(Path(path).absolute()) if path else "")
        field.setObjectName(name + "Path")
        field.setAccessibleName(title)
        field.setReadOnly(not editable)
        field.setPlaceholderText(empty)
        field.setToolTip(field.text() or empty)
        field.setCursorPosition(0)
        label = QLabel(title)
        label.setBuddy(field)
        actions = QHBoxLayout()
        actions.addWidget(label, 1)
        copy = QPushButton("복사")
        copy.setObjectName(name + "Copy")
        copy.setAccessibleName(title + " 경로 복사")
        copy.setEnabled(bool(path))
        copy.clicked.connect(lambda: QApplication.clipboard().setText(field.text()))
        actions.addWidget(copy)
        open_folder = QPushButton("폴더 열기")
        open_folder.setObjectName(name + "Open")
        open_folder.setAccessibleName(title + " 폴더 열기")

        def directory():
            value = field.text().strip()
            return (Path(value) if folder else Path(value).parent) if value else None

        def update_actions():
            target = directory()
            copy.setEnabled(bool(field.text()))
            try:
                open_folder.setEnabled(target is not None and target.is_dir())
            except (OSError, ValueError):
                open_folder.setEnabled(False)
            field.setToolTip(field.text() or empty)

        open_folder.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(directory().absolute()))
        ) if directory() is not None else None)
        actions.addWidget(open_folder)
        field.textChanged.connect(update_actions)
        update_actions()
        layout.addLayout(actions)
        entry = QHBoxLayout()
        entry.addWidget(field, 1)
        if editable:
            browse = QPushButton("폴더 선택…")
            browse.setObjectName(name + "Browse")
            browse.setAccessibleName(title + " 선택")
            browse.clicked.connect(lambda: self.choose_folder(field, title))
            field.setEnabled(not self.player_window.worker)
            browse.setEnabled(not self.player_window.worker)
            entry.addWidget(browse)
        layout.addLayout(entry)
        layout.addSpacing(8)
        return field

    def choose_folder(self, field, title):
        selected = QFileDialog.getExistingDirectory(self, title, field.text())
        if selected:
            field.setText(selected)
            field.setCursorPosition(0)

    def restore_defaults(self):
        for field in self.fields.values():
            field.setText(str(self.player_window.paths.processed))

    def update_buttons(self):
        if not hasattr(self, "save_button"):
            return
        values = tuple(field.text().strip() for field in self.fields.values())
        dirty = values != self.original or bool(self.player_window.paths.cache_location_error)
        self.save_button.setEnabled(not self.player_window.worker and dirty and all(values))
        self.defaults.setEnabled(not self.player_window.worker)
        self.close_button.setText("취소" if dirty else "닫기")

    def save(self):
        try:
            self.player_window.apply_cache_locations(
                self.fields["audioCacheFolder"].text(), self.fields["scoreCacheFolder"].text()
            )
        except (OSError, ValueError, RuntimeError) as error:
            self.notice.setText(f"저장하지 못했습니다. {error}")
            return
        self.accept()


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
        self.loaded_source_hash = None
        self.prepared_variants = {}
        self.selected_variant = "no_vocals"
        self.active_variant = {}
        self.imported_bundle = None
        self.base_pitch_semitones = 0
        self._pending_variant = None
        self._pending_catalog = None
        self._variant_restore = None
        self._pending_magic = None
        self._prepare_restore = None
        self._magic_export_ids = None
        self._magic_export_default = None
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
        self.current_track.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.connect_player()
        self.refresh_tracks()
        self.set_busy(False)
        if self.paths.cache_location_error:
            self.status.setText(
                "캐시 저장 위치 설정을 읽지 못했습니다. 아래 상세 옵션에서 저장 위치를 확인하세요."
            )

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
        subtitle = QLabel("보컬·기타·드럼·베이스·키보드, 필요한 소리를 골라 연습하세요.")
        subtitle.setWordWrap(True)
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
        self.tracks.setMaximumHeight(160)
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
        self.open_magic_button = QPushButton(".magic 연습 파일 열기")
        self.open_magic_button.setObjectName("openMagicFile")
        self.open_magic_button.clicked.connect(self.choose_magic)
        load_actions = QHBoxLayout()
        load_actions.addWidget(self.open_magic_button)
        self.cached_audio_button = QPushButton("저장된 음원 불러오기")
        self.cached_audio_button.setObjectName("loadCachedAudio")
        self.cached_audio_button.setToolTip(
            "선택한 곡의 저장된 결과를 확인합니다. "
            "새 분리 계산 없이 재생할 음원을 고를 수 있습니다."
        )
        self.cached_audio_button.clicked.connect(self.load_cached_audio)
        load_actions.addWidget(self.cached_audio_button)
        left.addLayout(load_actions)
        splitter.addWidget(library)
        panel = QWidget()
        right = QVBoxLayout(panel)
        right.setContentsMargins(6, 0, 0, 0)
        right.setSpacing(7)
        right.addWidget(QLabel("준비할 음원 · 여러 개 체크할 수 있습니다"))
        selection_actions = QHBoxLayout()
        self.select_all_variants = QPushButton("전체 선택")
        self.select_all_variants.setObjectName("selectAllVariants")
        self.select_all_variants.clicked.connect(lambda: self.select_preparation_variants(True))
        self.clear_variants = QPushButton("선택 해제")
        self.clear_variants.setObjectName("clearVariants")
        self.clear_variants.clicked.connect(lambda: self.select_preparation_variants(False))
        selection_actions.addWidget(self.select_all_variants)
        selection_actions.addWidget(self.clear_variants)
        selection_actions.addStretch()
        right.addLayout(selection_actions)
        self.selection_summary = QLabel()
        self.selection_summary.setObjectName("muted")
        self.selection_summary.setWordWrap(True)
        right.addWidget(self.selection_summary)
        actions = QHBoxLayout()
        self.prepare_button = QPushButton("체크한 음원 준비")
        self.prepare_button.setObjectName("primary")
        self.prepare_button.clicked.connect(self.prepare)
        actions.addWidget(self.prepare_button, 1)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_job)
        right.addLayout(actions)
        choices = QGridLayout()
        choices.setHorizontalSpacing(16)
        choices.setVerticalSpacing(3)
        choices.addWidget(QLabel("파트"), 0, 0)
        choices.addWidget(QLabel("없는 반주"), 0, 1)
        choices.addWidget(QLabel("만 듣기"), 0, 2)
        self.variant_checks = {}
        for row, (target, label) in enumerate(TARGET_LABELS.items(), 1):
            choices.addWidget(QLabel(label), row, 0)
            for column, identifier in enumerate((f"no_{target}", target), 1):
                checkbox = QCheckBox("선택")
                checkbox.setObjectName(f"separation_{identifier}")
                checkbox.setAccessibleName(variant_label(identifier))
                checkbox.setToolTip(variant_label(identifier))
                checkbox.setChecked(identifier == "no_vocals")
                self.variant_checks[identifier] = checkbox
                choices.addWidget(checkbox, row, column)
        original = QCheckBox("원곡 그대로")
        original.setObjectName("separation_original")
        self.variant_checks["original"] = original
        choices.addWidget(original, len(TARGET_LABELS) + 1, 0, 1, 3)
        choices.setColumnStretch(3, 1)
        right.addLayout(choices)
        self.separation_hint = QLabel()
        self.separation_hint.setWordWrap(True)
        self.separation_hint.setObjectName("muted")
        right.addWidget(self.separation_hint)
        for checkbox in self.variant_checks.values():
            checkbox.toggled.connect(self.update_preparation_selection)
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
        self.models.addItem("자동 추천 · 파트별 모델 선택", "auto")
        for key, model in MODELS.items():
            self.models.addItem(model.label, key)
        advanced_layout.addWidget(self.models)
        model_terms = QLabel(
            '<a href="https://github.com/jeongwoo-gobroad/Magic-Instrumental/'
            'blob/main/docs/SEPARATION_MODELS.md">모델 비교·이용 조건</a>'
        )
        model_terms.setOpenExternalLinks(True)
        advanced_layout.addWidget(model_terms)
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
        self.update_preparation_selection()
        self.status = QLabel(
            "필요한 음원을 체크해 함께 준비하세요. 이전 결과는 ‘저장된 음원 불러오기’로 엽니다."
        )
        self.status.setWordWrap(True)
        self.status.setObjectName("muted")
        self.status_slot = QVBoxLayout()
        self.status_slot.addWidget(self.status)
        right.addLayout(self.status_slot)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setMaximumWidth(150)
        self.current_track = QLabel("재생할 음원을 준비하거나 .magic 파일을 여세요")
        self.current_track.setObjectName("track")
        self.current_track.setWordWrap(True)
        right.addWidget(self.separator())
        right.addWidget(self.current_track)
        self.playback_variants = PlaybackVariantCombo()
        self.playback_variants.setObjectName("playbackVariant")
        self.playback_variants.setAccessibleName("준비된 재생 음원 선택")
        self.playback_variants.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.playback_variants.setMinimumContentsLength(10)
        self.playback_variants.setMaximumWidth(300)
        self.playback_variants.setMaxVisibleItems(8)
        self.playback_variants.setPlaceholderText("준비된 음원 없음")
        self.playback_variants.opening.connect(self.refresh_variant_choices)
        self.playback_variants.activated.connect(self.change_variant)
        self.variant_hint = QLabel(
            "음원을 바꾸면 같은 재생 위치에서 멈춥니다. 재생을 눌러 이어서 연습하세요."
        )
        self.variant_hint.setWordWrap(True)
        self.variant_hint.setObjectName("muted")
        right.addWidget(self.variant_hint)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.setAccessibleName("재생 위치")
        self.seek.sliderReleased.connect(lambda: self.seek_to(self.seek.value()))
        self.clock = QLabel("00:00 / 00:00")
        self.clock.setObjectName("muted")
        controls = QHBoxLayout()
        self.play_button = QPushButton("재생")
        self.play_button.setObjectName("primary")
        self.play_button.setMinimumSize(92, 40)
        self.play_button.setToolTip("음원을 재생하거나 일시정지합니다.")
        self.play_button.clicked.connect(self.toggle_play)
        self.play_button.setEnabled(False)
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
        self.key_label = QLabel("반주 키")
        key_row.addWidget(self.key_label)
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
        self.magic_button = QPushButton("PC·태블릿으로 가져가기 · .magic 저장")
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
        self.logs_toggle = QPushButton("처리 내역 보기")
        self.logs_toggle.setObjectName("processingHistory")
        self.logs_toggle.setCheckable(True)
        self.logs_toggle.toggled.connect(self.logs.setVisible)
        layout.addWidget(self.logs_toggle)
        layout.addWidget(self.logs)
        self.details_button = QPushButton("상세 옵션")
        self.details_button.setObjectName("cacheDetails")
        self.details_button.setToolTip("음원과 악보 분석 캐시의 저장 폴더를 확인하고 변경합니다.")
        self.details_button.clicked.connect(self.show_cache_locations)
        layout.addWidget(self.details_button)
        self.logs.setVisible(False)
        self.controller = QScrollArea()
        self.controller.setObjectName("optionsPanel")
        self.controller.setWidgetResizable(True)
        self.controller.setWidget(root)
        self.controller.setMinimumWidth(410)
        self.score_panel = ScorePanel(self.paths, preferences=self.preferences)
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
        self.main_splitter.addWidget(self.controller)
        self.main_splitter.addWidget(self.score_panel)
        self.main_splitter.setSizes([650, 850])
        saved = self.preferences.value("splitter")
        if saved is not None:
            self.main_splitter.restoreState(saved)
        self._expanded_splitter_state = self.main_splitter.saveState()
        self.main_splitter.splitterMoved.connect(self.save_splitter_layout)
        self.score_panel.bpm_provider = lambda: self.metronome.bpm.value()
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(6, 4, 6, 6)
        outer.setSpacing(3)
        self.update_bar = UpdateBar(ROOT)
        self.update_bar.update_requested.connect(self.begin_update)
        outer.addWidget(self.update_bar)
        outer.addWidget(self.metronome)
        self.options_toggle = QPushButton("옵션 숨기기")
        self.options_toggle.setObjectName("toggleOptions")
        self.options_toggle.setCheckable(True)
        self.options_toggle.setChecked(self.preferences.value("options_visible", True, type=bool))
        self.options_toggle.toggled.connect(self.set_options_visible)
        self.score_visible = QCheckBox("악보 표시")
        self.score_visible.setToolTip("가운데 경계를 드래그하면 옵션과 악보의 너비를 조절합니다.")
        self.score_visible.setChecked(self.preferences.value("score_visible", True, type=bool))
        if not self.options_toggle.isChecked() and not self.score_visible.isChecked():
            self.score_visible.setChecked(True)
        self.score_panel.setVisible(self.score_visible.isChecked())
        self.score_visible.toggled.connect(self.set_score_visible)
        self.available_variants = QLabel("음원 없음")
        self.available_variants.setObjectName("muted")
        right.insertWidget(right.indexOf(self.variant_hint), self.available_variants)
        self.playback_choice = QWidget()
        self.playback_choice.setObjectName("cachedPlaybackChoice")
        self.playback_choice.setMaximumWidth(300)
        choice_layout = QVBoxLayout(self.playback_choice)
        choice_layout.setContentsMargins(0, 0, 0, 0)
        choice_layout.setSpacing(2)
        choice_heading = QHBoxLayout()
        choice_label = QLabel("재생 음원")
        choice_label.setBuddy(self.playback_variants)
        choice_heading.addWidget(choice_label)
        self.playback_cache_count = QLabel("캐시 0개")
        self.playback_cache_count.setObjectName("playbackCacheCount")
        self.playback_cache_count.setAccessibleName("현재 곡의 저장된 재생 음원 수")
        choice_heading.addWidget(self.playback_cache_count, 1, Qt.AlignmentFlag.AlignRight)
        choice_layout.addLayout(choice_heading)
        choice_layout.addWidget(self.playback_variants)
        # Keep the transport together, outside the scrollable and foldable options.
        self.transport_bar = QWidget()
        self.transport_bar.setObjectName("playbackTransport")
        transport = QHBoxLayout(self.transport_bar)
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setSpacing(8)
        transport.addWidget(self.play_button)
        transport.addWidget(self.seek, 2)
        transport.addWidget(self.clock)
        transport.addWidget(self.playback_choice, 1)
        transport.addWidget(self.options_toggle)
        transport.addWidget(self.score_visible)
        outer.addWidget(self.transport_bar)
        self.feedback_bar = QWidget()
        self.feedback_row = QHBoxLayout(self.feedback_bar)
        self.feedback_row.setContentsMargins(0, 0, 0, 0)
        self.feedback_row.addWidget(self.progress)
        self.feedback_row.addWidget(self.cancel_button)
        outer.addWidget(self.feedback_bar)
        outer.addWidget(self.main_splitter, 1)
        self.set_options_visible(self.options_toggle.isChecked())
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

    def show_cache_locations(self):
        dialog = CacheLocationsDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def apply_cache_locations(self, audio_folder, score_folder):
        if self.worker:
            raise RuntimeError("현재 작업이 끝난 뒤 다시 저장하세요.")
        updated = save_cache_locations(self.paths, audio_folder, score_folder)
        self.paths = updated
        self.score_panel.paths = updated
        self.refresh_variant_choices()
        self.status.setText(
            "캐시 저장 위치를 저장했습니다. 다음 준비·불러오기부터 적용됩니다. "
            "현재 곡과 기존 파일은 그대로 유지됩니다."
        )

    def save_splitter_layout(self, *_):
        if not self.controller.isHidden() and not self.score_panel.isHidden():
            self._expanded_splitter_state = self.main_splitter.saveState()
            self.preferences.setValue("splitter", self._expanded_splitter_state)

    def set_options_visible(self, visible):
        if not visible:
            self.save_splitter_layout()
            if not self.score_visible.isChecked():
                self.score_visible.setChecked(True)
        self.controller.setVisible(visible)
        if visible:
            self.main_splitter.restoreState(self._expanded_splitter_state)
        self.options_toggle.setText("옵션 숨기기" if visible else "옵션 펼치기")
        self.options_toggle.setAccessibleName(self.options_toggle.text())
        self.preferences.setValue("options_visible", visible)
        self.update_feedback_location()

    def update_feedback_location(self):
        global_status = not self.options_toggle.isChecked() or self.worker is not None
        destination = self.feedback_row if global_status else self.status_slot
        if destination.indexOf(self.status) < 0:
            destination.insertWidget(0, self.status, 1)
        self.feedback_bar.setVisible(global_status)

    def set_score_visible(self, visible):
        if not visible:
            self.save_splitter_layout()
            if not self.options_toggle.isChecked():
                self.options_toggle.setChecked(True)
        self.score_panel.setVisible(visible)
        if visible and self.options_toggle.isChecked():
            self.main_splitter.restoreState(self._expanded_splitter_state)
        self.preferences.setValue("score_visible", visible)

    def connect_player(self):
        self.player.mediaStatusChanged.connect(self.restore_pitch_position)
        self.player.durationChanged.connect(self.duration_changed)
        self.player.seekableChanged.connect(lambda _: self.update_measure_controls())
        self.player.positionChanged.connect(self.position_changed)
        self.player.playbackStateChanged.connect(self.update_play_button)
        self.player.playbackStateChanged.connect(self.region_transport_changed)
        self.player.mediaStatusChanged.connect(self.region_media_status_changed)
        self.player.sourceChanged.connect(self.region_source_changed)
        self.player.durationChanged.connect(lambda _: self.refresh_region())
        self.player.seekableChanged.connect(lambda _: self.refresh_region())
        self.player.seekableChanged.connect(
            lambda _: self.restore_pitch_position(self.player.mediaStatus())
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
        portable = [
            path for path in self.paths.targets.rglob("*")
            if path.is_file() and path.suffix.lower() == ".magic"
        ]
        files = sorted(
            [*discover(self.paths.targets), *portable],
            key=lambda path: str(path.relative_to(self.paths.targets)).casefold(),
        )
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

    def checked_variants(self):
        return [identifier for identifier in SUPPORTED_VARIANTS
                if self.variant_checks[identifier].isChecked()]

    def select_preparation_variants(self, selected):
        for checkbox in self.variant_checks.values():
            checkbox.blockSignals(True)
            checkbox.setChecked(selected)
            checkbox.blockSignals(False)
        self.update_preparation_selection()

    def update_preparation_selection(self, *_):
        selected = self.checked_variants()
        combination = canonical_combination(selected)
        if combination:
            summary = (
                f"{len(selected)}개 선택 · {variant_label(combination)}도 함께 준비합니다. "
                "각 항목과 함께 제거한 조합을 따로 골라 재생할 수 있습니다."
            )
        elif selected:
            summary = f"{len(selected)}개 선택 · 각 항목을 별도 재생 음원으로 준비합니다."
        else:
            summary = "준비할 음원을 하나 이상 체크하세요."
        self.selection_summary.setText(summary)
        self.prepare_button.setEnabled(not self.worker and bool(selected))
        targets = {identifier.removeprefix("no_") for identifier in selected}
        explanations = {
            "keys": "키보드는 피아노·오르간 등 건반 파트입니다. "
            "전자 합성음은 ‘신시사이저’를 선택하세요.",
            "piano": "피아노를 따로 분리합니다. 오르간 등 다른 건반도 포함하려면 "
            "‘키보드’를 선택하세요.",
            "synth": "실험적 분리입니다. 신스 베이스·패드·전자 타악기 등 "
            "전자 합성음이 함께 포함될 수 있습니다.",
        }
        hint = " ".join(message for target, message in explanations.items() if target in targets)
        self.separation_hint.setText(hint)
        self.separation_hint.setVisible(bool(hint))

    def prepare(self, _checked=False):
        if not self.cache_work_ready():
            return
        selected = self.tracks.currentItem()
        if not selected:
            self.status.setText("음원 폴더에 파일을 넣고, 새로고침 후 곡을 선택하세요.")
            return
        source = selected.data(Qt.ItemDataRole.UserRole)
        if Path(source).suffix.lower() == ".magic":
            self.import_magic(Path(source))
            return
        variants = self.checked_variants()
        if not variants:
            self.status.setText("준비할 음원을 하나 이상 체크하세요.")
            return
        self.pending_source = Path(source)
        combination = canonical_combination(variants)
        if combination:
            variants.append(combination)
        selected_variant = combination or (
            self.selected_variant if self.selected_variant in variants else variants[0]
        )
        args = [
            "separate",
            "--input",
            source,
            "--root",
            str(self.paths.root),
            "--model",
            self.models.currentData(),
            "--variant",
            selected_variant,
            "--variants",
            *variants,
            "--device",
            self.devices.currentData(),
            "--events",
        ]
        if self.low_memory.isChecked():
            args.append("--low-memory")
        self._prepare_restore = self.capture_variant_position()
        self.player.pause()
        self.start_job(args)

    def load_cached_audio(self):
        if not self.cache_work_ready():
            return
        selected = self.tracks.currentItem()
        if not selected:
            self.status.setText("저장된 음원을 불러올 곡을 먼저 선택하세요.")
            return
        source = Path(selected.data(Qt.ItemDataRole.UserRole))
        if source.suffix.lower() == ".magic":
            self.import_magic(source)
            return
        self.pending_source = source
        self._prepare_restore = self.capture_variant_position()
        self.player.pause()
        self.start_job([
            "cached-audio", "--input", str(source), "--root", str(self.paths.root),
            "--variant", self.selected_variant, "--events",
        ])

    def choose_magic(self):
        if self.worker:
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, ".magic 연습 파일 열기", str(self.paths.root), "Magic 연습 파일 (*.magic)"
        )
        if filename:
            self.import_magic(Path(filename))

    def import_magic(self, source):
        if not self.cache_work_ready():
            return
        self._pending_magic = Path(source)
        self._prepare_restore = self.capture_variant_position()
        self.player.pause()
        self.start_job([
            "magic-import", "--input", str(source), "--root", str(self.paths.root), "--events",
        ])

    def cache_work_ready(self):
        if self.worker:
            return False
        try:
            self.paths.require_cache_locations()
        except ValueError as error:
            self.status.setText(f"{error} 아래 상세 옵션에서 저장 위치를 확인하세요.")
            return False
        return True

    def start_job(self, args):
        if self.worker:
            return
        if not self.cache_work_ready():
            # Another window can change the folder after an action's first check.
            saved = self._variant_restore or self._prepare_restore
            self._pending_variant = self._pending_catalog = self._prepare_restore = None
            self.pending_source = self._pending_magic = None
            self._variant_restore = saved
            self.refresh_variant_choices()
            if saved:
                self.restore_variant_region()
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
            "magic_imported",
            "cached_audio",
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
            if self.job_result["type"] in {"result", "cached_audio"}:
                if self.job_result["type"] == "cached_audio" and not self.job_result["variants"]:
                    self._variant_restore = self._prepare_restore if self.output_path else None
                    self._prepare_restore = None
                    self.pending_source = None
                    if (self.imported_bundle is None
                            and self.job_result["source_hash"] == self.loaded_source_hash):
                        self.prepared_variants = {}
                        self.refresh_variant_choices()
                        self.update_export_controls()
                    self.restore_variant_region()
                    self.status.setText(
                        "이 곡에 저장된 음원이 없습니다. 필요한 항목을 체크해 먼저 준비하세요."
                    )
                else:
                    self.load_result(self.job_result)
            elif self.job_result["type"] == "magic_imported":
                self.load_imported(self.job_result)
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
        if self.cancelled or code != 0:
            saved = self._variant_restore or self._prepare_restore
            self._pending_variant = None
            self._pending_catalog = None
            self._prepare_restore = None
            self._variant_restore = saved
            self.refresh_variant_choices()
            if saved:
                self.restore_variant_region()
            else:
                self.refresh_region()
        if self.close_pending:
            self.close()

    def load_result(self, result):
        same_song = (
            self.imported_bundle is None and self.loaded_source_hash is not None
            and result.get("source_hash") == self.loaded_source_hash and self.base_instrumental
        )
        variants = result.get("variants") or {
            "no_vocals": {
                "id": "no_vocals", "label": variant_label("no_vocals"),
                "target": "vocals", "mode": "without", "path": result["instrumental"],
            }
        }
        selected = result.get("selected_variant", "no_vocals")
        if same_song:
            if self.pending_source:
                self.source_path = self.pending_source
                self.source_name = self.pending_source.name
                self.current_track.setText(self.source_name)
                self.score_panel.audio = self.source_path
            self.begin_variant_switch(selected, restore=self._prepare_restore, catalog=variants)
            self._prepare_restore = None
            return
        self._loading_track = True
        try:
            self.imported_bundle = None
            self.base_pitch_semitones = 0
            self.loaded_source_hash = result.get("source_hash")
            self.key_label.setText("반주 키")
            self.selected_variant = selected
            self.prepared_variants = variants
            self.active_variant = variants[selected]
            self._pending_catalog = None
            self._variant_restore = self._pending_variant = self._prepare_restore = None
            if self.pending_source:
                self.source_name = self.pending_source.name
            self._load_result(result)
            self.refresh_variant_choices()
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

    def load_imported(self, result):
        descriptor = Path(result["bundle_path"])
        try:
            if descriptor.stat().st_size > 8_000_000:
                raise ValueError("저장된 연습 파일 정보가 너무 큽니다.")
            bundle = json.loads(descriptor.read_text(encoding="utf-8"))
            if (bundle["format"] != "Magic-Instrumental-PC"
                    or bundle["archive_hash"] != result["archive_hash"]):
                raise ValueError("연습 파일의 준비 결과가 일치하지 않습니다.")
            self._loading_track = True
            self.count_in.cancel()
            self.player.pause()
            self._pending_variant = self._variant_restore = self._prepare_restore = None
            self._pending_catalog = None
            self.imported_bundle = bundle
            self.prepared_variants = {item["id"]: item for item in bundle["audio_variants"]}
            self.selected_variant = bundle["default_audio_variant"]
            selected = self.prepared_variants[self.selected_variant]
            self.active_variant = selected
            self.base_pitch_semitones = selected["pitch_semitones"]
            self.key_label.setText("추가 키")
            self.pitch_semitones = 0
            self.key.setValue(0)
            self.loaded_source_hash = result["archive_hash"]
            self.source_path = self._pending_magic
            self.source_name = bundle["title"]
            self.pending_source = None
            self.base_instrumental = self.output_path = Path(selected["path"])
            self._pitch_pending = None
            # Preferences belong to this actual archive, not its claimed recording identity.
            preferences = descriptor.parent.parent
            self.region.bind_track(preferences / "practice_loop.json")
            metronome_settings = preferences / "metronome.json"
            had_settings = metronome_settings.is_file()
            self.metronome.bind_track(metronome_settings)
            if not had_settings:
                practice = bundle["practice"]
                self.metronome.bpm.setValue(practice["bpm"])
                self.metronome.beats.setValue(practice["beats"])
                self.metronome.unit.setCurrentIndex(self.metronome.unit.findData(practice["unit"]))
                self.metronome.origin.setValue(practice["origin"])
            self.score_panel.bind_portable(bundle)
            self.player.setSource(QUrl.fromLocalFile(str(self.output_path)))
            self.player.setPlaybackRate(self.speed.value() / 100)
            self.current_track.setText(self.source_name)
            self.refresh_variant_choices()
            self.status.setText(
                ("캐시에서 .magic 열기 완료" if result["cached"] else ".magic 열기 완료")
                + " · 재생 음원과 악보 파트를 골라 연습하세요."
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.show_error(f".magic 열기 실패: {error}")
        finally:
            self._loading_track = False
            self.set_busy(self.worker is not None)

    def refresh_variant_choices(self):
        # Deleted cache entries must not remain selectable; never scan audio here.
        self.prepared_variants = {
            identifier: item for identifier, item in self.prepared_variants.items()
            if identifier in ALL_VARIANTS and cached_audio_exists(item)
        }
        count = len(self.prepared_variants)
        self.playback_variants.blockSignals(True)
        self.playback_variants.clear()
        for identifier in ALL_VARIANTS:
            if identifier in self.prepared_variants:
                self.playback_variants.addItem(
                    (variant_label(identifier) if is_combination(identifier)
                     else self.prepared_variants[identifier]["label"]), identifier
                )
        self.playback_variants.setCurrentIndex(self.playback_variants.findData(self.selected_variant))
        self.playback_variants.setPlaceholderText(
            "재생할 음원을 선택하세요" if count else "준비된 음원 없음"
        )
        self.playback_variants.blockSignals(False)
        self.playback_variants.setEnabled(
            not self.worker and count > 0
            and (count > 1 or self.selected_variant not in self.prepared_variants)
        )
        origin = ".magic에 저장된 음원" if self.imported_bundle else "캐시에 준비된 음원"
        source = ".magic" if self.imported_bundle else "캐시"
        self.playback_cache_count.setText(f"{source} {count}개")
        self.playback_cache_count.setToolTip(f"{self.source_name}\n{origin}만 표시합니다.".strip())
        self.available_variants.setText(
            f"{source} {count}개 · 준비된 음원만 선택" if count else "준비된 음원 없음"
        )
        self.available_variants.setToolTip(origin)
        selector_help = (
            f"{self.source_name}\n{origin} {count}개만 선택할 수 있습니다. "
            "아래 준비할 음원의 체크 상태와는 별개입니다."
        ).strip()
        self.playback_variants.setToolTip(selector_help)
        self.playback_variants.setAccessibleDescription(selector_help)
        self.update_export_controls()
        if not count:
            self.variant_hint.setText(
                "사용할 수 있는 음원이 없습니다. .magic 파일을 다시 열어 주세요."
                if self.imported_bundle else
                "저장된 음원 불러오기로 캐시를 확인하거나, 필요한 음원을 체크해 준비하세요."
            )
            return
        base = (
            f"파일에 적용된 키 {self.base_pitch_semitones:+d}반음 · "
            if self.imported_bundle else ""
        )
        targets = removal_targets(self.selected_variant)
        if is_combination(self.selected_variant):
            base += (
                f"{variant_label(self.selected_variant)} · "
                "선택한 파트를 함께 제거한 음원입니다. "
            )
        if self.selected_variant in {"synth", "no_synth"} or "synth" in targets:
            base += "실험적 분리: 신스 베이스·패드·전자 타악기 등이 함께 포함될 수 있습니다. "
        elif self.selected_variant in {"keys", "no_keys"} or "keys" in targets:
            base += "키보드에는 피아노·오르간 등 건반 계열이 포함됩니다. "
        self.variant_hint.setText(
            base + (
                "남은 음원을 선택해 재생하세요. 이전 캐시는 다시 불러오거나 준비할 수 있습니다."
                if self.selected_variant not in self.prepared_variants else
                "저장된 음원이 1개입니다. 다른 음원은 PC에서 준비해 .magic에 함께 저장하세요."
                if count == 1 and self.imported_bundle else
                "준비된 음원이 1개입니다. 다른 음원이 필요하면 체크해 준비하세요."
                if count == 1 else
                "준비된 음원만 고를 수 있습니다. 바꾸면 같은 재생 위치에서 멈춥니다. "
                "재생을 눌러 이어서 연습하세요."
            )
        )

    def capture_variant_position(self):
        return {
            "position": self.player.position(), "first": self.region.first.currentData(),
            "last": self.region.last.currentData(), "enabled": self.region.enabled.isChecked(),
        }

    def change_variant(self, _index):
        identifier = self.playback_variants.currentData()
        self.refresh_variant_choices()
        if identifier is not None and identifier not in self.prepared_variants:
            self.status.setText(
                "선택한 캐시 파일이 없습니다. 저장된 음원을 다시 불러오거나 준비해 주세요."
            )
            return
        if (not self.worker and identifier in self.prepared_variants
                and identifier != self.selected_variant):
            self.begin_variant_switch(identifier)

    def begin_variant_switch(self, identifier, *, restore=None, catalog=None):
        candidates = self.prepared_variants if catalog is None else catalog
        if identifier not in candidates or not self.cache_work_ready():
            return
        item = candidates[identifier]
        # A cancelled model change must leave the old audio and its identity together.
        self._pending_catalog = dict(candidates)
        self._variant_restore = restore or self.capture_variant_position()
        self._pitch_position = self._variant_restore["position"]
        self._pending_variant = identifier
        self.refresh_variant_choices()
        self.count_in.cancel()
        self.player.pause()
        args = [
            "pitch", "--input", item["path"], "--semitones", str(self.pitch_semitones),
            "--root", str(self.paths.root), "--events",
        ]
        if item.get("sha256") and item.get("frames"):
            args.extend([
                "--expected-sha256", item["sha256"], "--expected-frames", str(item["frames"]),
            ])
        self.start_job(args)

    def analyze_score(self, options):
        if not self.cache_work_ready():
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
            self.tracks,
            self.refresh_button,
            self.open_magic_button,
            self.cached_audio_button,
            self.select_all_variants,
            self.clear_variants,
            *self.variant_checks.values(),
        ):
            widget.setEnabled(not busy)
        self.prepare_button.setEnabled(not busy and bool(self.checked_variants()))
        self.refresh_variant_choices()
        if busy:
            self.playback_variants.setEnabled(False)
        self.cancel_button.setEnabled(busy)
        self.cancel_button.setVisible(busy)
        self.progress.setVisible(busy)
        self.update_feedback_location()
        self.export_button.setEnabled(not busy and self.output_path is not None)
        self.score_panel.set_busy(busy)
        self.region.setEnabled(not busy)
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
        if not self.cache_work_ready():
            return
        self.player.pause()
        args = ["gp-prepare", "--root", str(self.paths.root), "--events"]
        for key, value in options.items():
            args.extend([f"--{key}", str(value)])
        self.start_job(args)

    def change_key(self):
        if not self.base_instrumental or not self.cache_work_ready():
            return
        self.count_in.cancel()
        self.player.pause()
        self._pitch_position = self.player.position()
        args = [
                "pitch",
                "--root",
                str(self.paths.root),
                "--events",
                "--input",
                str(self.base_instrumental),
                "--semitones",
                str(self.key.value()),
            ]
        item = self.active_variant
        if item.get("sha256") and item.get("frames"):
            args.extend([
                "--expected-sha256", item["sha256"], "--expected-frames", str(item["frames"]),
            ])
        self.start_job(args)

    def load_pitch(self, info):
        self._loading_track = True
        try:
            if self._pending_variant is not None:
                if self._pending_catalog is not None:
                    self.prepared_variants = self._pending_catalog
                    self._pending_catalog = None
                self.selected_variant = self._pending_variant
                self.active_variant = self.prepared_variants[self.selected_variant]
                self.base_instrumental = Path(self.active_variant["path"])
                self._pending_variant = None
            self.output_path = Path(info["path"])
            self.pitch_semitones = info["semitones"]
            self.key.setValue(self.pitch_semitones)
            self._pitch_pending = self._pitch_position
            self.player.setSource(QUrl.fromLocalFile(str(self.output_path)))
            self.player.setPlaybackRate(self.speed.value() / 100)
            self.player.setPosition(self._pitch_position)
            self.metronome.reanchor(self._pitch_position)
            self.status.setText(
                f"{variant_label(self.selected_variant)} · "
                f"원곡 기준 키 {self.base_pitch_semitones + self.pitch_semitones:+d}반음 · "
                "재생을 눌러 이어서 연습하세요."
            )
            self.refresh_variant_choices()
        finally:
            self._loading_track = False
        self.restore_pitch_position(self.player.mediaStatus())
        self.refresh_region()

    def reset_key(self):
        self.key.setValue(0)
        self.change_key()

    def restore_pitch_position(self, status):
        if self._loading_track or not self.player.isSeekable():
            return
        if self._pitch_pending is not None and status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            target, self._pitch_pending = self._pitch_pending, None
            self.player.setPosition(target)
            self.metronome.reanchor(target)
        self.restore_variant_region()

    def restore_variant_region(self):
        saved = self._variant_restore
        if (saved is None or self.worker or self._loading_track or self._pending_variant is not None
                or self._pitch_pending is not None
                or not self.player.isSeekable()
                or self.player.mediaStatus() not in (
                    QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                )):
            return
        self._variant_restore = None
        self.refresh_region()
        self._loading_track = True
        try:
            for combo, key in ((self.region.first, "first"), (self.region.last, "last")):
                index = combo.findData(saved[key])
                if index >= 0:
                    combo.blockSignals(True)
                    combo.setCurrentIndex(index)
                    combo.blockSignals(False)
            self.region.update_bounds()
            if saved["enabled"] and self.region.enabled.isEnabled():
                self.region.enabled.setChecked(True)
        finally:
            self._loading_track = False
        self.score_panel.reset_follow()
        self.score_panel.on_position(self.player.position())

    def update_export_controls(self):
        self.magic_button.setEnabled(
            not self.worker
            and self.output_path is not None
            and self.score_panel.timeline is not None
            and self.imported_bundle is None
            and bool(self.prepared_variants)
        )
        self.magic_button.setToolTip(
            "가져온 .magic은 원래 파일 그대로 PC·웹으로 옮길 수 있습니다. "
            "새 분리 결과나 악보 보정은 원본 곡에서 준비해 저장하세요."
            if self.imported_bundle else
            "원곡과 선택한 분리 음원, 악보를 .magic 파일 하나에 함께 저장합니다."
        )

    def export_magic(self):
        if (self.worker or not self.score_panel.timeline_path or not self.output_path
                or self.imported_bundle is not None or not self.prepared_variants):
            return
        if not self.cache_work_ready():
            return
        self._magic_export_ids = self._magic_export_default = None
        quality = self.magic_audio_options()
        if quality is None:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "PC·태블릿용 연습 파일 저장",
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

        audio_format, _, bitrate = quality.partition(":")
        audio_options = {"audio_format": audio_format, "include_original": True}
        if bitrate:
            audio_options["mp3_bitrate_kbps"] = int(bitrate)
        if self.prepared_variants:
            identifiers = self._magic_export_ids or list(self.prepared_variants)
            default = self._magic_export_default or self.selected_variant
            if default not in identifiers:
                default = identifiers[0]
            audio_options.update(
                audio_variants=[
                    export_variant_request(self.prepared_variants[identifier])
                    for identifier in identifiers
                    if identifier in self.prepared_variants
                ],
                default_audio_variant=default,
            )
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
                **audio_options,
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
        self.preferences.setValue("magic_export/audio_quality", quality)
        self.start_job(["magic-export", "--request", str(request), "--events"])

    def magic_audio_options(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("한 곡을 .magic 파일 하나로 저장")
        dialog.resize(560, min(620, self.screen().availableGeometry().height() - 60))
        layout = QVBoxLayout(dialog)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        form = QFormLayout(content)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        description = QLabel(
            "원곡은 항상 포함합니다. 같은 곡에서 준비한 분리 음원을 함께 골라 담으세요. "
            "PC와 웹에서 파일 하나를 열고 재생 음원을 바꿀 수 있습니다."
        )
        description.setWordWrap(True)
        form.addRow(description)
        choices, first = None, None
        if self.prepared_variants:
            summary = QLabel()
            summary.setObjectName("magicBundleSummary")
            summary.setWordWrap(True)
            form.addRow(summary)
            actions = QHBoxLayout()
            select_all = QPushButton("전체 선택")
            select_all.setObjectName("magicSelectAllVariants")
            clear = QPushButton("선택 해제")
            clear.setObjectName("magicClearVariants")
            for button in (select_all, clear):
                button.setToolTip("함께 담을 분리 음원만 변경합니다. 원곡은 항상 포함합니다.")
                actions.addWidget(button)
            actions.addStretch(1)
            form.addRow("함께 담을 분리 음원", actions)
            choices = QListWidget()
            choices.setObjectName("magicAudioVariants")
            choices.setAccessibleName("함께 저장할 음원")
            choices.setMinimumHeight(128)
            choices.setMaximumHeight(190)
            choices.setWordWrap(True)
            for identifier in ("original", *(i for i in ALL_VARIANTS if i != "original")):
                if identifier == "original" or identifier in self.prepared_variants:
                    label = ("원곡 · 항상 포함" if identifier == "original"
                             else variant_label(identifier) if is_combination(identifier)
                             else self.prepared_variants[identifier]["label"])
                    item = QListWidgetItem(label)
                    item.setData(Qt.ItemDataRole.UserRole, identifier)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked)
                    if identifier == "original":
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                        item.setToolTip("원곡도 다른 음원과 같은 키·음질로 저장합니다.")
                    choices.addItem(item)
            form.addRow(choices)
            first = QComboBox()
            first.setObjectName("magicDefaultVariant")
            first.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            first.setMinimumContentsLength(12)
            form.addRow("처음 재생할 음원", first)

            def update_default():
                previous = first.currentData() or self.selected_variant
                # 원곡은 목록 조작 방식과 관계없이 한 번만 포함합니다.
                choices.blockSignals(True)
                choices.item(0).setCheckState(Qt.CheckState.Checked)
                choices.blockSignals(False)
                first.clear()
                for index in range(choices.count()):
                    item = choices.item(index)
                    if item.checkState() == Qt.CheckState.Checked:
                        identifier = item.data(Qt.ItemDataRole.UserRole)
                        first.addItem(variant_label(identifier) if identifier == "original"
                                      else item.text(), identifier)
                index = first.findData(previous)
                first.setCurrentIndex(index if index >= 0 else 0)
                selected_count = first.count() - 1
                summary.setText(f"원곡 1개 + 분리 음원 {selected_count}개 · .magic 파일 하나")
                select_all.setEnabled(selected_count < choices.count() - 1)
                clear.setEnabled(selected_count > 0)

            def select_variants(selected):
                choices.blockSignals(True)
                for index in range(1, choices.count()):
                    choices.item(index).setCheckState(
                        Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked
                    )
                choices.blockSignals(False)
                update_default()

            select_all.clicked.connect(lambda: select_variants(True))
            clear.clicked.connect(lambda: select_variants(False))
            choices.itemChanged.connect(update_default)
            update_default()
            explanation = QLabel(
                "여러 파트를 함께 뺀 반주는 ‘(보컬 + 기타) 없는 반주’처럼 표시됩니다. "
                "목록에 없는 결과는 창을 닫고 ‘저장된 음원 불러오기’로 확인하거나 먼저 준비하세요."
            )
            explanation.setWordWrap(True)
            form.addRow(explanation)
        quality = QComboBox()
        quality.setObjectName("magicAudioQuality")
        quality.setAccessibleName("음질")
        for bitrate in (128, 192, 256, 320):
            label = f"MP3 {bitrate} kbps" + (" · 권장" if bitrate == 256 else "")
            quality.addItem(label, f"mp3:{bitrate}")
        quality.addItem("WAV · 무손실", "wav")
        saved = str(self.preferences.value("magic_export/audio_quality", "mp3:256"))
        index = quality.findData(saved)
        quality.setCurrentIndex(index if index >= 0 else quality.findData("mp3:256"))
        form.addRow("음질", quality)
        quality_hint = QLabel("MP3는 옮기기 편한 작은 파일, WAV는 압축 손실 없는 큰 파일입니다.")
        quality_hint.setWordWrap(True)
        form.addRow(quality_hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장 위치 선택…")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")

        def accept():
            if first is not None and first.count() == 0:
                QMessageBox.information(
                    dialog, "음원 선택", "함께 저장할 음원을 하나 이상 선택하세요."
                )
                return
            dialog.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        selected = None
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = quality.currentData()
            if choices is not None:
                self._magic_export_ids = [
                    choices.item(index).data(Qt.ItemDataRole.UserRole)
                    for index in range(choices.count())
                    if choices.item(index).checkState() == Qt.CheckState.Checked
                ]
                self._magic_export_default = first.currentData()
        dialog.deleteLater()
        return selected

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

    def duration_changed(self, duration):
        self.seek.setMaximum(duration)
        self.position_changed(self.player.position())

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
        self.score_panel.reset_follow()
        self.metronome.begin_seek()
        try:
            self.player.setPosition(max(0, min(milliseconds, self.player.duration())))
        finally:
            self.metronome.end_seek(self.player.position())
        self.position_changed(self.player.position())

    def refresh_region(self, *, busy=None):
        if self._loading_track or self._variant_restore is not None:
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
        if reason == "enabled" and bounds and not self._loading_track:
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
            if (not self._region_transition and not self._loading_track
                    and self._variant_restore is None):
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
        self.save_splitter_layout()
        self.preferences.setValue("splitter", self._expanded_splitter_state)
        self.preferences.setValue("options_visible", self.options_toggle.isChecked())
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
