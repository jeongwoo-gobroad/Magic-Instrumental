# SPDX-License-Identifier: GPL-3.0-only
"""PDF display and review UI; playback only reads an already computed schedule."""

import json
import time
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
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
from .score import (
    ScoreTimeline,
    associate_score,
    load_last_timeline,
    matching_score,
    recover_notation_source,
    stale_pdf_fallback,
)
from .score_view import rendered_size, view_profile


def status_area(label, name):
    """Keep message wrapping from changing the pre-analyzed PDF viewport."""
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    area = QScrollArea()
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(label)
    label.ensurePolished()
    # Korean fallback glyphs can be taller than the base font's line spacing.
    line_height = max(label.fontMetrics().lineSpacing(), label.heightForWidth(12000))
    area.setFixedHeight(2 * line_height + 2)
    area.setAccessibleName(name)
    area.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return area


class ScorePageLabel(QLabel):
    """Paint the current measure over the page without changing the cached image."""

    def __init__(self, text):
        super().__init__(text)
        self.highlight_box = None

    def set_highlight(self, box):
        if self.highlight_box != box:
            self.highlight_box = box
            self.update()

    def highlight_rect(self):
        pixmap = self.pixmap()
        if self.highlight_box is None or pixmap is None or pixmap.isNull():
            return QRectF()
        box, page = self.highlight_box, self.contentsRect()
        return QRectF(
            page.left() + box["left"] * page.width(),
            page.top() + box["top"] * page.height(),
            (box["right"] - box["left"]) * page.width(),
            (box["bottom"] - box["top"]) * page.height(),
        )

    def paintEvent(self, event):
        super().paintEvent(event)
        rect = self.highlight_rect()
        if rect.isEmpty():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#169873"), 2))
        painter.setBrush(QColor(80, 218, 169, 28))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 4, 4)
        painter.end()


class PdfView(QScrollArea):
    geometry_changed = Signal()
    manual_navigation = Signal()

    def __init__(self):
        super().__init__()
        self.document = None
        self.page = 0
        self.zoom = "page"
        self.cache = OrderedDict()
        self.highlight_page = None
        self.highlight_box = None
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            bar.sliderPressed.connect(self.manual_navigation)
            bar.actionTriggered.connect(lambda _: self.manual_navigation.emit())
        self.label = ScorePageLabel("score_targets에 음원과 같은 이름의 PDF를 넣으세요.")
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
        self.set_highlight(None, None)
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
        self.update_highlight()

    def set_highlight(self, page, box):
        self.highlight_page, self.highlight_box = page, box
        self.update_highlight()

    def update_highlight(self):
        box = self.highlight_box if self.document and self.highlight_page == self.page else None
        self.label.set_highlight(box)

    def move_camera(self, x, y):
        self.horizontalScrollBar().setValue(x)
        self.verticalScrollBar().setValue(y)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.timer.start(80)


class ScorePanel(QWidget):
    gp_prepare_requested = Signal(dict)
    analysis_requested = Signal(dict)
    seek_requested = Signal(int)
    timeline_changed = Signal()

    def __init__(self, paths):
        super().__init__()
        self.paths = paths
        self.audio = None
        self.source_hash = ""
        self.pdf = None
        self.gp_source = None
        self.gp_info = None
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
        tools = QWidget()
        setup = QVBoxLayout(tools)
        setup.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(tools)
        row = QHBoxLayout()
        row.addWidget(QLabel("악보"))
        self.choose = QPushButton("PDF / GP 열기")
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
        setup.addLayout(row)
        self.name = QLabel("곡을 준비하면 같은 이름의 악보를 찾습니다.")
        self.name.setWordWrap(True)
        setup.addWidget(self.name)
        self.parts = QComboBox()
        self.parts.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.parts.setMinimumContentsLength(12)
        self.parts.setAccessibleName("GP 악기 파트 선택")
        self.parts.setToolTip("연습할 파트를 이름으로 선택하세요. 변경 후 사전 분석이 필요합니다.")
        self.parts.setVisible(False)
        self.parts.activated.connect(self.change_part)
        setup.addWidget(self.parts)
        row = QHBoxLayout()
        self.analyze = QPushButton("악보 사전 분석")
        self.analyze.clicked.connect(self.analysis_options)
        row.addWidget(self.analyze)
        self.review = QPushButton("시간표 · 보정")
        self.review.clicked.connect(self.edit_timeline)
        row.addWidget(self.review)
        setup.addLayout(row)
        self.status = QLabel("재생 전에 기호 인식과 음원 정렬을 완료하세요.")
        self.status.setWordWrap(True)
        self.status.setObjectName("muted")
        self.status_area = status_area(self.status, "악보 분석 상태")
        layout.addWidget(self.status_area)
        row = QHBoxLayout()
        self.follow = QCheckBox("자동 넘김 · 스크롤")
        self.follow.setChecked(True)
        self.follow.setToolTip(
            "끄면 페이지와 스크롤을 직접 조절합니다. 현재 마디 표시는 계속 갱신됩니다."
        )
        self.follow.toggled.connect(lambda _: self.on_position(self.last_position))
        row.addWidget(self.follow)
        self.measure = QLabel("현재 마디 · 회차")
        self.measure.setWordWrap(True)
        self.measure_area = status_area(self.measure, "현재 마디와 반복 회차")
        self.measure.setText("—")
        row.addWidget(self.measure_area, 1)
        self.tools_toggle = QPushButton("준비 도구 접기")
        self.tools_toggle.setCheckable(True)
        self.tools_toggle.toggled.connect(tools.setHidden)
        self.tools_toggle.toggled.connect(
            lambda folded: self.tools_toggle.setText(
                "준비 도구 펼치기" if folded else "준비 도구 접기"
            )
        )
        row.addWidget(self.tools_toggle)
        layout.addLayout(row)
        self.view = PdfView()
        self.view.geometry_changed.connect(self.update_view_status)
        self.view.manual_navigation.connect(lambda: self.follow.setChecked(False))
        layout.addWidget(self.view, 1)
        self.view_status = QLabel("배율과 악보 영역을 정한 뒤 사전 분석하세요.")
        self.view_status.setWordWrap(True)
        self.view_status.setObjectName("muted")
        self.view_status_area = status_area(self.view_status, "자동 스크롤 상태")
        layout.addWidget(self.view_status_area)
        row = QHBoxLayout()
        previous = QPushButton("‹")
        previous.setAccessibleName("이전 악보 페이지")
        previous.clicked.connect(lambda: self.manual_page(self.page.value() - 1))
        row.addWidget(previous)
        self.page = QSpinBox()
        self.page.setRange(1, 1)
        self.page.setSuffix(" / 0")
        self.page.valueChanged.connect(self.manual_page)
        row.addWidget(self.page)
        following = QPushButton("›")
        following.setAccessibleName("다음 악보 페이지")
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
        if self.source_hash != source_hash:
            self.clear_score()
        self.audio, self.source_hash = Path(audio), source_hash
        self.find_pdf()

    def clear_score(self):
        self.pdf = self.timeline = self.timeline_path = self.current_index = None
        self.gp_source = self.gp_info = None
        self.parts.setVisible(False)
        self.view.clear()
        self.page.setRange(1, 1)
        self.page.setSuffix("")
        self.name.setToolTip("")
        self.name.setText("PDF / GP 열기로 이 곡에 사용할 악보를 선택하세요.")
        self.status.setText("같은 이름의 PDF·GP3/4/5를 score_targets에 넣어도 됩니다.")
        self.measure.setText("—")
        self.set_busy(self.busy)
        self.timeline_changed.emit()

    def find_pdf(self):
        if not self.audio:
            return
        pdf = matching_score(self.audio, self.paths, self.source_hash)
        if pdf:
            self.open_pdf(pdf)
        else:
            self.clear_score()

    def choose_pdf(self):
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "곡에 연결할 악보",
            str(self.paths.score_targets),
            "악보 (*.pdf *.gp3 *.gp4 *.gp5);;PDF (*.pdf);;Guitar Pro (*.gp3 *.gp4 *.gp5)",
        )
        if filename:
            self.open_pdf(Path(filename))

    def open_pdf(self, pdf, prepared=None):
        if pdf.suffix.lower() in {".gp3", ".gp4", ".gp5"}:
            track = 1
            try:
                link = json.loads(
                    (self.paths.processed / self.source_hash / "score-link.json")
                    .read_text(encoding="utf-8")
                )
                if (self.paths.root / link["pdf"]).resolve() == pdf.resolve():
                    track = int(link.get("track", 1))
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self.gp_prepare_requested.emit({"gp": str(pdf), "gp-track": track})
            return
        try:
            self.view.load(pdf)
            self.pdf = pdf
            self.timeline = self.timeline_path = self.current_index = None
            self.gp_source = Path(prepared["source"]) if prepared else None
            self.gp_info = prepared
            self.parts.blockSignals(True)
            self.parts.clear()
            if prepared:
                for part in prepared["tracks"]:
                    self.parts.addItem(
                        f"{part['name']}" + (" · 타악기" if part["percussion"] else ""),
                        part["number"],
                    )
                self.parts.setCurrentIndex(self.parts.findData(prepared["metadata"]["track"]))
            self.parts.blockSignals(False)
            self.parts.setVisible(bool(prepared))
            source = self.gp_source or pdf
            associate_score(
                source,
                self.paths,
                self.source_hash,
                prepared["metadata"]["track"] if prepared else 1,
            )
            self.name.setText(source.name)
            self.name.setToolTip(str(source))
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
            self.clear_score()
            self.status.setText(f"악보 읽기 실패: {error}")
        self.set_busy(self.busy)
        self.timeline_changed.emit()

    def change_part(self, _index):
        if self.gp_source and not self.busy:
            self.gp_prepare_requested.emit(
                {"gp": str(self.gp_source), "gp-track": self.parts.currentData()}
            )

    def accept_gp(self, info):
        self.open_pdf(Path(info["pdf"]), prepared=info)
        if self.pdf and not self.timeline:
            self.status.setText("파트 준비 완료 · 악보 사전 분석으로 반주와 연결하세요.")

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
            "pdf-staves-barlines": "PDF 마디 위치",
            "pdf-only-staves-barlines": "PDF 마디선 + 입력 BPM·박자",
            "musicxml-assisted": "MusicXML 보조 배치",
            "guitar-pro-native": "마디 직접 연결",
        }.get(geometry.get("method"), "")
        source = ""
        if (plan.data.get("gp_display")
                or plan.data.get("notation_source", {}).get("method") == "guitar-pro"):
            source = "GP 원본"
        elif plan.data.get("xml_sha256"):
            source = {
                "explicit": "선택한 MusicXML", "matching": "같은 이름의 MusicXML",
                "recovered": "이전 분석의 MusicXML", "reviewed": "보정한 MusicXML",
                "embedded": "PDF 내장 MusicXML", "omr": "PDF 음표·기호 인식",
            }.get(plan.data.get("notation_selection"), "MusicXML")
        method = " · ".join(part for part in (source, method) if part)
        if geometry.get("measure_count") is not None:
            method = " · ".join(filter(None, (method, f"원본 {geometry['measure_count']}마디")))
        review_lines = len({
            (item["page"], item["system"]) for item in geometry.get("review_required", [])
        })
        self.status.setText(
            f"{len(plan.cues)}개 연주 마디 · {state}"
            + (f"\n{method}" if method else "")
            + ("\n음표 인식 대신 마디선·BPM으로 계산 · 시간표 확인"
               if plan.data.get("analysis_fallback") else "")
            + (f"\n배치 확인 필요 {review_lines}줄" if review_lines else "")
            + ("\n이전 버전의 반복·코다 누락 가능성 · 악보 사전 분석을 다시 실행하세요."
               if stale_pdf_fallback(plan.data) else "")
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
        self.status.setToolTip("\n".join([
            self.status.text(), *details, *plan.data.get("warnings", []),
        ]))
        self.set_busy(self.busy)
        self.update_view_status()
        self.on_position(self.player.position() if self.player else self.last_position)
        self.timeline_changed.emit()

    def set_busy(self, busy):
        self.busy = busy
        self.choose.setEnabled(not busy and self.audio is not None)
        self.refresh.setEnabled(not busy and self.audio is not None)
        self.analyze.setEnabled(not busy and self.pdf is not None)
        self.review.setEnabled(not busy and self.timeline is not None)
        self.parts.setEnabled(not busy)
        if not busy and self.gp_info:
            self.parts.setCurrentIndex(self.parts.findData(self.gp_info["metadata"]["track"]))

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
            self.view_status.setText("현재 마디 표시·확대 스크롤: 배율을 정한 뒤 사전 분석하세요.")
        elif self.view_matches():
            self.view_status.setText("확대 스크롤 준비 완료 · 저장된 이동 경로로 재생합니다.")
        else:
            self.view_status.setText(
                "페이지는 자동으로 넘깁니다. 확대 스크롤은 사전 분석에서 "
                "기존 시간표를 유지하고 재분석하세요."
            )
        frames = (self.timeline.data.get("scroll") or {}).get("frames", []) if self.timeline else []
        if self.timeline and (not frames or not all(frame.get("box") for frame in frames)):
            self.view_status.setText(
                self.view_status.text()
                + "\n현재 마디 강조: 기존 시간표를 유지하고 화면 이동 경로를 다시 분석하세요."
            )
        self.view_status.setToolTip(self.view_status.text())

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
        if not self.timeline:
            self.view.set_highlight(None, None)
            return
        index = self.timeline.index_at(seconds, self.timeline.data.get("offset", 0))
        if index is None:
            self.measure.setText("악보 시작 전 / 종료")
            self.current_index = None
            self.view.set_highlight(None, None)
            return
        cue = self.timeline.cues[index]
        self.current_index = index
        self.measure.setText(f"{cue['measure']}마디 · {cue['visit']}회차")
        if self.follow.isChecked():
            self.jump_page(cue["page"])
        frames = (self.timeline.data.get("scroll") or {}).get("frames", [])
        self.view.set_highlight(cue["page"], frames[index].get("box") if frames else None)
        # Page/measure timing remains valid when only the saved camera size changed.
        if not self.follow.isChecked() or not self.view_matches():
            return
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
        stale_fallback = self.timeline is not None and stale_pdf_fallback(self.timeline.data)
        reuse.setChecked(self.timeline is not None and (
            not stale_fallback or self.timeline.data.get("reviewed")
        ))
        reuse.setEnabled(self.timeline is not None)
        form.addRow(reuse)
        if stale_fallback:
            notice = QLabel(
                "이전 자동 대체 분석에서 반복·코다가 빠졌을 수 있습니다. "
                "직접 보정하지 않은 결과는 전체 분석을 다시 하며, 기존 파일은 보존합니다."
            )
            notice.setWordWrap(True)
            form.addRow(notice)
        saved = self.timeline.data.get("signature", {}) if self.timeline else {}
        previous_xml = (recover_notation_source(self.pdf, self.paths, self.source_hash)
                        if not self.gp_source else None)
        if previous_xml is not None and self.timeline_path is not None:
            recognition = (self.timeline_path.parent.parent / "recognition").resolve()
            generated = {recognition / "score.mxl"}
            version = saved.get("omr")
            if type(version) is int and 1 <= version <= 100:
                generated.add(recognition / f"v{version}/score.mxl")
            if previous_xml in generated and (recognition / "reviewed.musicxml").is_file():
                # 자동 캐시를 명시적 선택으로 보내면 새 보정본이 가려진다.
                previous_xml = None
        previous_mode = saved.get("mode")
        if previous_mode not in {"auto", "audio", "tempo"}:
            previous_mode = "auto"
        if previous_xml is not None:
            label = ("기존 인식 결과 · 일정 BPM으로 시간 계산" if previous_mode == "tempo"
                     else "기존 인식 결과의 반복·코다 순서 사용")
            form.addRow("이어 쓸 분석", QLabel(label))
        mode = QComboBox()
        mode.setObjectName("scoreAlignmentMode")
        if not self.gp_source:
            mode.addItem("자동 · MusicXML 우선, 없으면 PDF 인식", "auto")
        mode.addItem("음표와 음원을 자동 정렬 (추정)", "audio")
        mode.addItem("일정 BPM으로 마디 시간 계산", "tempo")
        previous_index = mode.findData(previous_mode)
        mode.setCurrentIndex(max(0, previous_index))
        if self.gp_info and self.gp_info["metadata"]["percussion"]:
            mode.setCurrentIndex(mode.findData("tempo"))
        form.addRow("정렬", mode)
        xml = browse = None
        if not self.gp_source:
            xml = QLineEdit(str(previous_xml) if previous_xml is not None else "")
            xml.setObjectName("scoreMusicXML")
            xml.setPlaceholderText("자동: 같은 이름·이전 인식·PDF 내장 MusicXML 확인")
            browse = QPushButton("MusicXML 선택")

            def select_xml():
                path, _ = QFileDialog.getOpenFileName(
                    dialog, "MusicXML 선택", str(self.paths.score_targets),
                    "MusicXML (*.mxl *.musicxml *.xml)",
                )
                if path:
                    xml.setText(path)

            browse.clicked.connect(select_xml)
            source_row = QHBoxLayout()
            source_row.addWidget(xml, 1)
            source_row.addWidget(browse)
            form.addRow("MusicXML (선택)", source_row)
        bpm = QDoubleSpinBox()
        bpm.setRange(20, 400)
        bpm.setValue(saved.get("bpm", self.bpm_provider()))
        form.addRow("원곡 BPM ♩", bpm)
        meter_beats = QSpinBox()
        meter_beats.setObjectName("scoreMeterBeats")
        meter_beats.setRange(1, 32)
        meter_beats.setValue(saved.get("meter_beats", 4))
        meter_unit = QComboBox()
        meter_unit.setObjectName("scoreMeterUnit")
        for unit in (1, 2, 4, 8, 16, 32):
            meter_unit.addItem(str(unit), unit)
        meter_unit.setCurrentIndex(meter_unit.findData(saved.get("meter_unit", 4)))
        meter = QHBoxLayout()
        meter.addWidget(meter_beats)
        meter.addWidget(QLabel("/"))
        meter.addWidget(meter_unit)
        if not self.gp_source:
            form.addRow("마디 박자", meter)
        lead = QDoubleSpinBox()
        lead.setRange(0, 1800)
        lead.setDecimals(3)
        lead.setValue(saved.get("lead_in", 0))
        form.addRow("악보 첫 박 (초)", lead)
        route = QLineEdit()
        route.setPlaceholderText("자동 인식 · 보정 예: 1-8,1-6,9-16")
        route.setToolTip("인쇄 번호가 아닌 원본 마디 순번(1부터). 반복을 모두 펼친 순서입니다.")
        route.setText(saved.get("route", ""))
        form.addRow("연주 순서 직접 보정", route)
        force = QCheckBox("시간 정렬 다시 계산 (기존 보정본 보존)")
        form.addRow(force)

        def controls_changed(*_):
            checked = reuse.isChecked()
            for widget in (bpm, lead, route, meter_beats, meter_unit, force):
                widget.setEnabled(not checked)
            if mode is not None:
                mode.setEnabled(not checked and not (
                    self.gp_info and self.gp_info["metadata"]["percussion"]
                ))
            if xml is not None:
                xml.setEnabled(not checked)
                browse.setEnabled(not checked)

        reuse.toggled.connect(controls_changed)
        controls_changed()

        hint = QLabel(
            ("선택한 MusicXML과 기존 인식 결과를 우선 사용합니다.\n"
             "원본이 없으면 PDF 내장 MusicXML을 확인한 뒤 음표·인쇄 기호를 읽습니다.\n"
             "자동 모드의 PDF 음표 인식 실패에만 마디선·BPM 추정을 사용합니다.\n"
             "반복·이동 기호가 불완전하면 연주 순서 보정을 요청합니다. "
             "못갖춘마디·박자/템포 변화는 "
             "시간표에서 보정하세요.\n" if not self.gp_source else
             "GP의 음표·박자·반복을 직접 읽어 반주와 연결합니다.\n")
            + "분석 후 마디 수·연주 순서·시간을 확인하고 자동 스크롤을 켜세요."
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
                "meter-beats": meter_beats.value(),
                "meter-unit": meter_unit.currentData(),
                "lead-in": lead.value(),
                "route": route.text().strip(),
                "musicxml": xml.text().strip() if xml is not None else "",
                "gp-track": 1,
                "force": force.isChecked(),
                "zoom": profile["zoom"],
                "view-width": profile["width"],
                "view-height": profile["height"],
            }
            if reuse.isChecked():
                options["base-timeline"] = str(self.timeline_path)
            if self.gp_source:
                options["gp"] = str(self.gp_source)
                options["gp-track"] = self.parts.currentData()
                options["musicxml"] = ""
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
