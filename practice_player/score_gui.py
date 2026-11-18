# SPDX-License-Identifier: GPL-3.0-only
"""PDF display and review UI; playback only reads an already computed schedule."""

import time
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cache import hash_file
from .score import ScoreTimeline, associate_score, load_last_timeline, matching_score
from .score_view import rendered_size, view_profile


class PdfView(QScrollArea):
    geometry_changed = Signal()
    manual_navigation = Signal()

    def __init__(self):
        super().__init__()
        self.document = None
        self.page = 0
        self.zoom = "page"
        self.cache = OrderedDict()
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            bar.sliderPressed.connect(self.manual_navigation)
            bar.actionTriggered.connect(lambda _: self.manual_navigation.emit())
        self.label = QLabel("score_targets에 음원과 같은 이름의 PDF를 넣으세요.")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWidget(self.label)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.resized)

    def profile(self):
        return view_profile(
            self.zoom, max(100, self.viewport().width()), max(100, self.viewport().height())
        )

    def resized(self):
        self.render()
        self.geometry_changed.emit()

    def wheelEvent(self, event):
        self.manual_navigation.emit()
        super().wheelEvent(event)

    def clear(self):
        self.timer.stop()
        self.cache.clear()
        self.label.clear()
        if self.document:
            self.document.close()
        self.document = None

    def load(self, path):
        import pypdfium2

        self.clear()
        self.document = pypdfium2.PdfDocument(str(path))
        if not 1 <= len(self.document) <= 200:
            self.clear()
            raise ValueError("1~200페이지의 PDF가 필요합니다.")
        self.page = 0
        self.render()

    def show_page(self, page):
        if self.document and 0 <= page < len(self.document):
            self.page = page
            self.render()
            self.verticalScrollBar().setValue(0)

    def render(self):
        if not self.document:
            return
        page = self.document[self.page]
        size = page.get_size()
        width, height = rendered_size(size, self.profile())
        # Re-render vectors for enlargement; bounded resolution/cache controls memory.
        scale = min(width / size[0] * self.devicePixelRatioF(), 4096 / max(size))
        key = (self.page, round(scale, 2))
        if key not in self.cache:
            bitmap = page.render(scale=scale)
            pil = bitmap.to_pil().convert("RGB")
            image = QImage(
                pil.tobytes(), pil.width, pil.height, pil.width * 3, QImage.Format.Format_RGB888
            ).copy()
            self.cache[key] = QPixmap.fromImage(image)
            bitmap.close()
            if len(self.cache) > 3:
                self.cache.popitem(last=False)
        page.close()
        pixmap = self.cache[key]
        scaled = pixmap.scaled(
            QSize(width, height),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.label.setPixmap(scaled)
        self.label.resize(scaled.size())

    def move_camera(self, x, y):
        self.horizontalScrollBar().setValue(x)
        self.verticalScrollBar().setValue(y)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.timer.start(80)


class ScorePanel(QWidget):
    analysis_requested = Signal(dict)
    seek_requested = Signal(int)
    timeline_changed = Signal()

    def __init__(self, paths):
        super().__init__()
        self.paths = paths
        self.audio = None
        self.source_hash = ""
        self.pdf = None
        self.timeline = None
        self.timeline_path = None
        self.busy = False
        self.current_index = None
        self.last_position = 0
        self.anchor_time = time.monotonic()
        self.player = None
        self.camera_timer = QTimer(self)
        self.camera_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.camera_timer.setInterval(16)
        self.camera_timer.timeout.connect(self.follow_clock)
        self.bpm_provider = lambda: 120
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 8, 10)
        row = QHBoxLayout()
        row.addWidget(QLabel("악보"))
        self.choose = QPushButton("PDF 선택")
        self.choose.clicked.connect(self.choose_pdf)
        row.addWidget(self.choose)
        self.refresh = QPushButton("다시 찾기")
        self.refresh.clicked.connect(self.find_pdf)
        row.addWidget(self.refresh)
        folder = QPushButton("폴더")
        folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.score_targets)))
        )
        row.addWidget(folder)
        layout.addLayout(row)
        self.name = QLabel("곡을 준비하면 같은 이름의 악보를 찾습니다.")
        self.name.setWordWrap(True)
        layout.addWidget(self.name)
        row = QHBoxLayout()
        self.analyze = QPushButton("악보 사전 분석")
        self.analyze.clicked.connect(self.analysis_options)
        row.addWidget(self.analyze)
        self.review = QPushButton("시간표 · 보정")
        self.review.clicked.connect(self.edit_timeline)
        row.addWidget(self.review)
        layout.addLayout(row)
        self.status = QLabel("재생 전에 기호 인식과 음원 정렬을 완료하세요.")
        self.status.setWordWrap(True)
        self.status.setFixedHeight(42)
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.follow = QCheckBox("자동 넘김 · 스크롤")
        self.follow.setChecked(True)
        self.follow.toggled.connect(lambda _: self.on_position(self.last_position))
        row.addWidget(self.follow)
        self.measure = QLabel("—")
        row.addWidget(self.measure, 1)
        layout.addLayout(row)
        self.view = PdfView()
        self.view.geometry_changed.connect(self.update_view_status)
        self.view.manual_navigation.connect(lambda: self.follow.setChecked(False))
        layout.addWidget(self.view, 1)
        self.view_status = QLabel("배율과 악보 영역을 정한 뒤 사전 분석하세요.")
        self.view_status.setWordWrap(True)
        self.view_status.setFixedHeight(40)
        self.view_status.setObjectName("muted")
        layout.addWidget(self.view_status)
        row = QHBoxLayout()
        previous = QPushButton("‹")
        previous.clicked.connect(lambda: self.manual_page(self.page.value() - 1))
        row.addWidget(previous)
        self.page = QSpinBox()
        self.page.setRange(1, 1)
        self.page.setSuffix(" / 0")
        self.page.valueChanged.connect(self.manual_page)
        row.addWidget(self.page)
        following = QPushButton("›")
        following.clicked.connect(lambda: self.manual_page(self.page.value() + 1))
        row.addWidget(following)
        self.zoom = QComboBox()
        for label, value in [
            ("한 페이지", "page"),
            ("너비 맞춤", "width"),
            ("100%", 1.0),
            ("150%", 1.5),
            ("200%", 2.0),
            ("250%", 2.5),
            ("300%", 3.0),
            ("400%", 4.0),
            ("500%", 5.0),
            ("600%", 6.0),
        ]:
            self.zoom.addItem(label, value)
        self.zoom.currentIndexChanged.connect(self.change_zoom)
        row.addWidget(self.zoom)
        self.zoom.setToolTip(
            "분석 전에 배율을 정하세요. 배율·악보 영역 변경 시 스크롤 재분석이 필요합니다."
        )
        layout.addLayout(row)
        self.set_busy(False)

    def bind_track(self, audio, source_hash):
        self.audio, self.source_hash = Path(audio), source_hash
        self.find_pdf()

    def find_pdf(self):
        if not self.audio:
            return
        pdf = matching_score(self.audio, self.paths, self.source_hash)
        if pdf:
            self.open_pdf(pdf)
        else:
            self.pdf = self.timeline = self.timeline_path = None
            self.view.clear()
            self.name.setText("같은 이름의 PDF가 없습니다. PDF 선택으로 연결할 수 있습니다.")
            self.status.setText("score_targets에 PDF를 넣고 ‘다시 찾기’를 누르세요.")
            self.measure.setText("—")
            self.set_busy(False)
            self.timeline_changed.emit()

    def choose_pdf(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "곡에 연결할 PDF", str(self.paths.score_targets), "PDF (*.pdf)"
        )
        if filename:
            self.open_pdf(Path(filename))

    def open_pdf(self, pdf):
        try:
            self.view.load(pdf)
            self.pdf = pdf
            self.timeline = self.timeline_path = self.current_index = None
            associate_score(pdf, self.paths, self.source_hash)
            self.name.setText(pdf.name)
            self.name.setToolTip(str(pdf))
            self.page.blockSignals(True)
            self.page.setRange(1, len(self.view.document))
            self.page.setSuffix(f" / {len(self.view.document)}")
            self.page.setValue(1)
            self.page.blockSignals(False)
            self.status.setText("PDF 준비 완료 · 사전 분석으로 반복 순서와 마디 시간을 계산하세요.")
            self.measure.setText("—")
            cached = load_last_timeline(pdf, self.paths, self.source_hash)
            if cached:
                self.load_timeline(cached[0])
        except Exception as error:
            self.pdf = self.timeline = self.timeline_path = None
            self.view.clear()
            self.status.setText(f"악보 읽기 실패: {error}")
        self.set_busy(self.busy)
        self.timeline_changed.emit()

    def load_timeline(self, path):
        plan = ScoreTimeline.load(Path(path))
        if (
            not self.pdf
            or plan.data["audio_sha256"] != self.source_hash
            or plan.data["pdf_sha256"] != hash_file(self.pdf)
        ):
            raise ValueError("현재 곡·PDF와 분석 결과가 일치하지 않습니다.")
        self.timeline_path, self.timeline = Path(path), plan
        self.current_index = None
        if plan.data.get("scroll"):
            index = self.zoom.findData(plan.data["scroll"]["profile"]["zoom"])
            if index < 0:
                value = plan.data["scroll"]["profile"]["zoom"]
                self.zoom.addItem(f"{value * 100:g}%", value)
                index = self.zoom.count() - 1
            self.zoom.blockSignals(True)
            self.zoom.setCurrentIndex(index)
            self.zoom.blockSignals(False)
            self.view.zoom = self.zoom.currentData()
            self.view.render()
        state = "보정 저장됨" if plan.data.get("reviewed") else "자동 추정 · 시간표 확인 권장"
        geometry = plan.data.get("pdf_layout", {})
        method = {
            "pdf-staves-barlines": "PDF 마디선 인식",
            "musicxml-assisted": "MusicXML 보조 배치",
        }.get(geometry.get("method"), "")
        self.status.setText(
            f"{len(plan.cues)}개 연주 마디 · {state}"
            + (f"\n{method} · 원본 {geometry['measure_count']}마디" if method else "")
        )
        details = []
        for index, page in enumerate(geometry.get("pages", [])):
            width, height = page["size_points"]
            orientation = "가로" if page["orientation"] == "landscape" else "세로"
            paper = page.get("format", "custom").replace("custom", "사용자 규격")
            details.append(
                f"{index + 1}쪽: {paper} {orientation}, {width:g} × {height:g} pt, "
                f"{page['systems']}줄, 기울기 보정 {page['deskew_degrees']:g}°"
            )
        self.status.setToolTip("\n".join(details + plan.data.get("warnings", [])))
        self.set_busy(self.busy)
        self.update_view_status()
        self.timeline_changed.emit()

    def set_busy(self, busy):
        self.busy = busy
        self.choose.setEnabled(not busy and self.audio is not None)
        self.refresh.setEnabled(not busy and self.audio is not None)
        self.analyze.setEnabled(not busy and self.pdf is not None)
        self.review.setEnabled(not busy and self.timeline is not None)

    def change_zoom(self):
        self.view.zoom = self.zoom.currentData()
        self.view.render()
        self.update_view_status()

    def view_matches(self):
        return bool(
            self.timeline
            and self.timeline.data.get("scroll")
            and self.timeline.data["scroll"]["profile"] == self.view.profile()
        )

    def update_view_status(self):
        if not self.timeline or not self.timeline.data.get("scroll"):
            self.view_status.setText("확대 스크롤: 배율·악보 영역을 정한 뒤 사전 분석하세요.")
        elif self.view_matches():
            self.view_status.setText("확대 스크롤 준비 완료 · 저장된 이동 경로로 재생합니다.")
        else:
            self.view_status.setText(
                "배율 또는 악보 영역이 바뀌었습니다. 스크롤 재분석이 필요합니다."
            )

    def attach_player(self, player):
        self.player = player
        player.playbackStateChanged.connect(self.transport_changed)
        player.playbackRateChanged.connect(lambda _: self.on_position(player.position()))

    def transport_changed(self, state):
        from PySide6.QtMultimedia import QMediaPlayer

        self.on_position(self.player.position())
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.camera_timer.start()
        else:
            self.camera_timer.stop()

    def follow_clock(self):
        # Only interpolate the saved path. No PDF inspection or layout analysis here.
        if self.player:
            seconds = (
                self.last_position / 1000
                + (time.monotonic() - self.anchor_time) * self.player.playbackRate()
            )
            self.follow_position(seconds)

    def manual_page(self, value):
        if not self.view.document:
            return
        self.follow.setChecked(False)
        self.jump_page(max(0, min(len(self.view.document) - 1, value - 1)))

    def jump_page(self, page):
        self.page.blockSignals(True)
        self.page.setValue(page + 1)
        self.page.blockSignals(False)
        if self.view.page != page:
            self.view.show_page(page)

    def on_position(self, milliseconds):
        self.last_position = milliseconds
        self.anchor_time = time.monotonic()
        self.follow_position(milliseconds / 1000)

    def follow_position(self, seconds):
        if not self.timeline or not self.follow.isChecked():
            return
        # A path is valid only for its pre-analyzed zoom and viewport.
        if self.timeline.data.get("scroll") and not self.view_matches():
            return
        index = self.timeline.index_at(seconds, self.timeline.data.get("offset", 0))
        if index is None:
            self.measure.setText("악보 시작 전 / 종료")
            self.current_index = None
            return
        cue = self.timeline.cues[index]
        self.current_index = index
        self.measure.setText(f"{cue['measure']}마디 · {cue['visit']}회차")
        self.jump_page(cue["page"])
        camera = self.timeline.scroll_at(seconds)
        if camera:
            self.view.move_camera(camera[1], camera[2])

    def analysis_options(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("악보 사전 분석 설정")
        form = QFormLayout(dialog)
        profile = self.view.profile()
        form.addRow(
            "분석할 보기",
            QLabel(f"{self.zoom.currentText()} · {profile['width']} × {profile['height']} px"),
        )
        reuse = QCheckBox("기존 마디 시간표 유지 · 화면 이동 경로만 분석")
        reuse.setChecked(self.timeline is not None)
        reuse.setEnabled(self.timeline is not None)
        form.addRow(reuse)
        mode = QComboBox()
        mode.addItem("음표와 음원을 자동 정렬 (추정)", "audio")
        mode.addItem("일정 BPM으로 마디 시간 계산", "tempo")
        form.addRow("정렬", mode)
        bpm = QDoubleSpinBox()
        bpm.setRange(20, 400)
        bpm.setValue(self.bpm_provider())
        form.addRow("원곡 BPM ♩", bpm)
        lead = QDoubleSpinBox()
        lead.setRange(0, 1800)
        lead.setDecimals(3)
        form.addRow("악보 첫 박 (초, BPM 모드)", lead)
        route = QLineEdit()
        route.setPlaceholderText("자동 인식 · 보정 예: 1-8,1-6,9-16")
        route.setToolTip("인쇄 번호가 아닌 원본 마디 순번(1부터). 반복을 모두 펼친 순서입니다.")
        form.addRow("연주 순서 직접 보정", route)
        xml = QLineEdit()
        xml.setPlaceholderText("비우면 같은 이름 MusicXML 또는 PDF 인식")
        browse = QPushButton("MusicXML 선택")

        def select_xml():
            path, _ = QFileDialog.getOpenFileName(
                dialog,
                "MusicXML",
                str(self.paths.score_targets),
                "MusicXML (*.mxl *.musicxml *.xml)",
            )
            if path:
                xml.setText(path)

        browse.clicked.connect(select_xml)
        form.addRow(xml, browse)
        force = QCheckBox("시간 정렬 다시 계산 (기존 보정본 보존)")
        form.addRow(force)

        def reuse_changed(checked):
            for widget in (mode, bpm, lead, route, xml, browse, force):
                widget.setEnabled(not checked)

        reuse.toggled.connect(reuse_changed)
        reuse_changed(reuse.isChecked())
        hint = QLabel(
            "PDF 인식 도구와 영어 데이터는 설치 파일에서 자동 준비합니다.\n"
            "PDF의 규격·보표·마디선을 분석하므로 "
            "줄바꿈 정보가 없는 MusicXML도 사용할 수 있습니다.\n"
            "복잡한 기호는 인식 후 시간표와 연주 순서를 확인하세요."
        )
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            options = {
                "audio": str(self.audio),
                "pdf": str(self.pdf),
                "mode": mode.currentData(),
                "bpm": bpm.value(),
                "lead-in": lead.value(),
                "route": route.text().strip(),
                "musicxml": xml.text().strip(),
                "force": force.isChecked(),
                "zoom": profile["zoom"],
                "view-width": profile["width"],
                "view-height": profile["height"],
            }
            if reuse.isChecked():
                options["base-timeline"] = str(self.timeline_path)
            self.analysis_requested.emit(options)

    def edit_timeline(self):
        if not self.timeline:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("마디 시간표 · 원곡 시간 기준으로 보정")
        dialog.resize(800, 650)
        layout = QVBoxLayout(dialog)
        notice = QLabel(
            "자동 결과는 추정입니다. 시작 시간·PDF 페이지를 수정하고 저장하세요.\n"
            "행을 더블클릭하면 해당 시간으로 이동합니다. 반복 순서는 분석 설정에서 보정합니다."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        warnings = QLabel("\n".join(self.timeline.data.get("warnings", [])[:8]))
        warnings.setWordWrap(True)
        layout.addWidget(warnings)
        table = QTableWidget(len(self.timeline.cues), 6)
        table.setHorizontalHeaderLabels(["원본 순번", "마디", "회차", "페이지", "시작 초", "기호"])
        for row, cue in enumerate(self.timeline.cues):
            for col, value in enumerate(
                [
                    cue["source_index"] + 1,
                    cue["measure"],
                    cue["visit"],
                    cue["page"] + 1,
                    cue["start"],
                    ", ".join(cue["marks"]),
                ]
            ):
                item = QTableWidgetItem(str(value))
                if col not in (3, 4):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row, col, item)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(table, 1)
        form = QFormLayout()
        offset = QDoubleSpinBox()
        offset.setRange(-1800, 1800)
        offset.setDecimals(3)
        offset.setValue(self.timeline.data.get("offset", 0))
        form.addRow("전체 시작 지연 (+초)", offset)
        end = QDoubleSpinBox()
        end.setRange(0, 86400)
        end.setDecimals(3)
        end.setValue(self.timeline.end)
        form.addRow("악보 종료 시간 (초)", end)
        layout.addLayout(form)

        def seek_row(row, _):
            try:
                self.seek_requested.emit(
                    round((float(table.item(row, 4).text()) + offset.value()) * 1000)
                )
            except ValueError:
                pass

        table.cellDoubleClicked.connect(seek_row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )

        def save():
            try:
                data = deepcopy(self.timeline.data)
                for row, cue in enumerate(data["cues"]):
                    cue["start"] = float(table.item(row, 4).text())
                    cue["page"] = int(table.item(row, 3).text()) - 1
                if any(a["page"] != b["page"] for a, b in zip(data["cues"], self.timeline.cues)):
                    data.pop("scroll", None)
                    data.pop("view_signature", None)
                data.update(offset=offset.value(), end=end.value(), reviewed=True)
                ScoreTimeline(data).save(self.timeline_path)
                self.load_timeline(self.timeline_path)
                dialog.accept()
            except (ValueError, OSError) as error:
                QMessageBox.warning(dialog, "보정 값 확인", str(error))

        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
