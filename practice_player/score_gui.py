# SPDX-License-Identifier: GPL-3.0-only
"""PDF display and review UI; playback only reads an already computed schedule."""

import json
import time
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
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
    QLayout,
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


class ScoreToolsArea(QScrollArea):
    def eventFilter(self, watched, event):
        handled = super().eventFilter(watched, event)
        if watched is self.widget() and event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()
        return handled

    def sizeHint(self):
        content = self.widget()
        if content is None:
            return super().sizeHint()
        # 큰 창에서는 모든 도구를, 작은 창에서는 스크롤 가능한 행을 보여 준다.
        return content.sizeHint() + QSize(2 * self.frameWidth(), 2 * self.frameWidth())


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
    page_changed = Signal(int)
    render_failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.document = None
        self.page = 0
        self.zoom = "page"
        self.cache = OrderedDict()
        self.page_sizes = []
        self.labels = []
        self._rendered = {}
        self._center_y = None
        self._laying_out = False
        self._browsing = False
        self.highlight_enabled = True
        self.highlight_page = None
        self.highlight_box = None
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            bar.sliderPressed.connect(self.manual_move)
            bar.actionTriggered.connect(lambda _: self.manual_move())
        self.surface = QWidget()
        self.empty_label = ScorePageLabel("score_targets에 음원과 같은 이름의 PDF를 넣으세요.")
        self.empty_label.setParent(self.surface)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = self.empty_label
        self.setWidget(self.surface)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.resized)
        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self.render_visible)
        self.slide = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self.slide.setDuration(200)
        self.slide.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.verticalScrollBar().valueChanged.connect(self.scrolled)

    def stop_motion(self):
        self.slide.stop()
        self._center_y = None

    def manual_move(self):
        self.stop_motion()
        self._browsing = True
        self.manual_navigation.emit()

    def profile(self):
        return view_profile(
            self.zoom, max(100, self.viewport().width()), max(100, self.viewport().height())
        )

    def resized(self):
        self.render()
        self.geometry_changed.emit()

    def wheelEvent(self, event):
        self.manual_move()
        super().wheelEvent(event)

    def clear(self):
        self.timer.stop()
        self.render_timer.stop()
        self.stop_motion()
        self._browsing = self._laying_out = False
        self.cache.clear()
        self.set_highlight(None, None)
        for label in self.labels:
            label.clear()
            label.hide()
            label.deleteLater()
        self.labels.clear()
        self.page_sizes.clear()
        self._rendered.clear()
        self.label = self.empty_label
        self.empty_label.clear()
        self.empty_label.show()
        self.surface.resize(self.viewport().size())
        if self.document:
            self.document.close()
        self.document = None

    def load(self, path):
        import pypdfium2

        self.load_document(pypdfium2.PdfDocument(str(path)))

    def load_pages(self, pages):
        from .page_images import PageImages

        self.load_document(PageImages(pages))

    def load_document(self, document):
        self.clear()
        self.document = document
        if not 1 <= len(self.document) <= 200:
            self.clear()
            raise ValueError("1~200페이지의 PDF가 필요합니다.")
        self.empty_label.hide()
        for number in range(len(self.document)):
            page = self.document[number]
            try:
                self.page_sizes.append(page.get_size())
            finally:
                page.close()
            label = ScorePageLabel("")
            label.setParent(self.surface)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet("background: white;")
            label.show()
            self.labels.append(label)
        self.page = 0
        self.label = self.labels[0]
        self.render()
        self.show_page(0)

    def set_page(self, page):
        if self.document and 0 <= page < len(self.labels):
            self._browsing = False
            self.page = page
            self.label = self.labels[page]
            self.update_highlight()
            self.render_timer.start(0)

    def show_page(self, page):
        if self.document and 0 <= page < len(self.document):
            self.stop_motion()
            self.set_page(page)
            self.verticalScrollBar().setValue(self.label.y())
            self.render_visible()

    def render(self):
        if not self.document:
            self.surface.resize(self.viewport().size())
            self.empty_label.resize(self.viewport().size())
            return
        self.stop_motion()
        self._laying_out = True
        profile = self.profile()
        sizes = [rendered_size(size, profile) for size in self.page_sizes]
        width = max(self.viewport().width(), *(size[0] for size in sizes))
        # End padding lets even the first and last staff reach the viewport centre.
        y = self.viewport().height() // 2
        for label, (page_width, page_height) in zip(self.labels, sizes):
            label.setGeometry((width - page_width) // 2, y, page_width, page_height)
            y += page_height + 16
        self.surface.resize(width, y - 16 + self.viewport().height() // 2)
        self._laying_out = False
        self.render_visible()
        self.update_highlight()

    def scrolled(self):
        if not self.document or self._laying_out:
            return
        if self._browsing:
            middle = self.verticalScrollBar().value() + self.viewport().height() / 2
            page = min(
                range(len(self.labels)),
                key=lambda index: max(
                    self.labels[index].y() - middle,
                    middle - self.labels[index].y() - self.labels[index].height(), 0,
                ),
            )
            if page != self.page:
                self.page = page
                self.label = self.labels[page]
                self.update_highlight()
                self.page_changed.emit(page)
        self.render_timer.start(0)

    def render_visible(self):
        if not self.document or self._laying_out:
            return
        top = self.verticalScrollBar().value()
        bottom = top + self.viewport().height()
        visible = [
            index for index, label in enumerate(self.labels)
            if label.y() <= bottom and label.y() + label.height() >= top
        ]
        wanted = set(visible)
        if visible:
            wanted.add(max(0, visible[0] - 1))
            wanted.add(min(len(self.labels) - 1, visible[-1] + 1))
        wanted.add(self.page)
        for index in tuple(self._rendered):
            if index not in wanted:
                self.labels[index].clear()
                del self._rendered[index]
        for index in sorted(wanted):
            try:
                self.render_page(index)
            except Exception as error:
                label = self.labels[index]
                label.clear()
                label.setText(f"{index + 1}쪽을 표시할 수 없습니다. 악보를 다시 여세요.")
                self._rendered[index] = (label.width(), label.height(), self.devicePixelRatioF())
                self.render_failed.emit(f"{index + 1}쪽 표시 실패: {error}")
        self.update_highlight()

    def render_page(self, number):
        label, size = self.labels[number], self.page_sizes[number]
        width, height = label.width(), label.height()
        rendered = (width, height, self.devicePixelRatioF())
        if self._rendered.get(number) == rendered:
            return
        # Re-render vectors for enlargement; bounded resolution/cache controls memory.
        scale = min(width / size[0] * self.devicePixelRatioF(), 4096 / max(size))
        key = (number, round(scale, 2))
        if key not in self.cache:
            page = self.document[number]
            try:
                bitmap = page.render(scale=scale)
                try:
                    pil = bitmap.to_pil().convert("RGB")
                    image = QImage(
                        pil.tobytes(), pil.width, pil.height, pil.width * 3,
                        QImage.Format.Format_RGB888,
                    ).copy()
                finally:
                    bitmap.close()
            finally:
                page.close()
            self.cache[key] = QPixmap.fromImage(image)
            if len(self.cache) > 3:
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        pixmap = self.cache[key]
        scaled = pixmap.scaled(
            QSize(width, height),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(scaled)
        self._rendered[number] = rendered

    def set_highlight(self, page, box):
        self.highlight_page, self.highlight_box = page, box
        self.update_highlight()

    def set_highlight_enabled(self, enabled):
        self.highlight_enabled = enabled
        self.update_highlight()

    def update_highlight(self):
        for index, label in enumerate(self.labels):
            box = self.highlight_box if (
                self.highlight_enabled and index == self.highlight_page
            ) else None
            label.set_highlight(box)

    def move_camera(self, x, y):
        self.stop_motion()
        self._browsing = False
        self.horizontalScrollBar().setValue(self.label.x() + x)
        self.verticalScrollBar().setValue(self.label.y() + y)

    def center_box(self, box, fraction=0, *, animate=False, snap=False):
        """Map saved measure geometry into the continuous, current-size score surface."""
        self._browsing = False
        page, viewport = self.label.geometry(), self.viewport()
        left = page.x() + box["left"] * page.width()
        right = page.x() + box["right"] * page.width()
        horizontal = (left + right) / 2
        if right - left > viewport.width():
            horizontal = left + (right - left) * min(1, max(0, fraction))
        self.horizontalScrollBar().setValue(round(horizontal - viewport.width() / 2))
        y = round(
            page.y() + (box["top"] + box["bottom"]) * page.height() / 2
            - viewport.height() / 2
        )
        if snap or self._center_y != y:
            bar = self.verticalScrollBar()
            self.slide.stop()
            self._center_y = y
            if animate and not snap and abs(y - bar.value()) <= viewport.height():
                self.slide.setStartValue(bar.value())
                self.slide.setEndValue(y)
                self.slide.start()
            else:
                bar.setValue(y)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.timer.start(80)


class ScorePanel(QWidget):
    gp_prepare_requested = Signal(dict)
    analysis_requested = Signal(dict)
    seek_requested = Signal(int)
    timeline_changed = Signal()

    def __init__(self, paths, preferences=None):
        super().__init__()
        self.paths = paths
        self.preferences = preferences
        self.audio = None
        self.source_hash = ""
        self.pdf = None
        self.gp_source = None
        self.gp_info = None
        self.timeline = None
        self.timeline_path = None
        self.portable_bundle = None
        self.busy = False
        self.current_index = None
        self._follow_index = None
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
        tools = ScoreToolsArea()
        tools.setWidgetResizable(True)
        tools.setFrameShape(QFrame.Shape.NoFrame)
        tools.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tools.setMinimumHeight(44)
        tools.setAccessibleName("악보 준비 도구 · 작은 창에서는 이 영역을 스크롤하세요")
        tools_content = QWidget()
        setup = QVBoxLayout(tools_content)
        setup.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        tools.setWidget(tools_content)
        self.tools, self.setup = tools, setup
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
        self.name.setTextFormat(Qt.TextFormat.PlainText)
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
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setObjectName("muted")
        self.status_area = status_area(self.status, "악보 분석 상태")
        row = QHBoxLayout()
        row.addWidget(self.status_area, 1)
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
        row = QHBoxLayout()
        self.follow = QCheckBox("자동 넘김 · 스크롤")
        self.follow.setChecked(True)
        self.follow.setToolTip(
            "현재 마디를 악보 영역 중앙에 두고 페이지를 이어서 스크롤합니다. "
            "끄면 직접 이동하며, 다시 켜면 현재 재생 위치로 돌아갑니다."
        )
        self.follow.toggled.connect(self.change_follow)
        row.addWidget(self.follow)
        self.highlight = QCheckBox("마디 강조")
        self.highlight.setObjectName("scoreHighlight")
        self.highlight.setChecked(
            preferences.value("score_highlight", True, type=bool)
            if preferences is not None else True
        )
        self.highlight.setToolTip(
            "현재 마디의 초록색 표시를 켜거나 끕니다. 자동 넘김과 스크롤은 그대로 유지합니다."
        )
        row.addWidget(self.highlight)
        self.measure = QLabel("현재 마디 · 회차")
        self.measure.setTextFormat(Qt.TextFormat.PlainText)
        self.measure.setWordWrap(True)
        self.measure_area = status_area(self.measure, "현재 마디와 반복 회차")
        self.measure.setText("—")
        row.addWidget(self.measure_area, 1)
        layout.addLayout(row)
        self.view = PdfView()
        self.view.set_highlight_enabled(self.highlight.isChecked())
        self.highlight.toggled.connect(self.change_highlight)
        self.view.geometry_changed.connect(self.view_changed)
        self.view.manual_navigation.connect(lambda: self.follow.setChecked(False))
        self.view.page_changed.connect(self.page_scrolled)
        self.view.render_failed.connect(self.status.setText)
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
            "저장된 마디 위치가 있으면 배율·악보 영역을 바꿔도 중앙 따라가기가 유지됩니다."
        )
        layout.addLayout(row)
        self.set_busy(False)

    def change_highlight(self, enabled):
        self.view.set_highlight_enabled(enabled)
        if self.preferences is not None:
            self.preferences.setValue("score_highlight", enabled)

    def reset_follow(self):
        self._follow_index = None
        self.view.stop_motion()

    def change_follow(self, _enabled):
        self.reset_follow()
        self.on_position(self.player.position() if self.player else self.last_position)

    def bind_track(self, audio, source_hash):
        if self.source_hash != source_hash or self.portable_bundle is not None:
            self.clear_score()
        self.audio, self.source_hash = Path(audio), source_hash
        self.find_pdf()

    def clear_score(self):
        if self.portable_bundle is not None:
            self.layout().removeWidget(self.parts)
            self.setup.insertWidget(2, self.parts)
            self.tools.setVisible(not self.tools_toggle.isChecked())
            self.tools_toggle.show()
        self.portable_bundle = None
        self.pdf = self.timeline = self.timeline_path = self.current_index = None
        self._follow_index = None
        self.gp_source = self.gp_info = None
        self.parts.setVisible(False)
        self.parts.setToolTip("")
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
        previous_pdf, previous_timeline = self.pdf, self.timeline_path
        try:
            self.view.load(pdf)
            self.pdf = pdf
            self.timeline = self.timeline_path = self.current_index = None
            self._follow_index = None
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
            try:
                cached = load_last_timeline(pdf, self.paths, self.source_hash)
            except ValueError as error:
                # 캐시 설정에 문제가 있어도 읽을 수 있는 악보는 그대로 보여 준다.
                if previous_pdf == pdf and previous_timeline is not None:
                    try:
                        self.load_timeline(previous_timeline)
                    except (OSError, ValueError, TypeError, KeyError):
                        pass
                kept = ("악보와 기존 마디 시간표를 유지합니다." if self.timeline is not None
                        else "악보 화면을 유지합니다.")
                self.status.setText(
                    f"{kept} 캐시 저장 위치를 확인해 주세요.\n"
                    f"{error} 상세 옵션에서 저장 위치를 확인한 뒤 다시 찾기를 누르세요."
                )
            else:
                if cached:
                    self.load_timeline(cached[0])
        except Exception as error:
            self.clear_score()
            self.status.setText(f"악보 읽기 실패: {error}")
        self.set_busy(self.busy)
        self.timeline_changed.emit()

    def change_part(self, _index):
        if self.portable_bundle is not None and not self.busy:
            try:
                self.open_portable_part(self.parts.currentData())
            except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
                self.status.setText(f"저장된 악보를 열지 못했습니다. .magic을 다시 여세요: {error}")
            return
        if self.gp_source and not self.busy:
            self.gp_prepare_requested.emit(
                {"gp": str(self.gp_source), "gp-track": self.parts.currentData()}
            )

    def bind_portable(self, bundle):
        self.clear_score()
        self.portable_bundle = bundle
        # Stored scores need only part selection; omit source-analysis controls.
        self.setup.removeWidget(self.parts)
        self.layout().insertWidget(1, self.parts)
        self.tools.hide()
        self.tools_toggle.hide()
        self.source_hash = bundle["archive_hash"]
        self.audio = None
        self.parts.blockSignals(True)
        self.parts.clear()
        for part in bundle["parts"]:
            self.parts.addItem(
                part["name"] + (" · 타악기" if part["percussion"] else ""), part["track"]
            )
        self.parts.blockSignals(False)
        self.parts.setToolTip("파일에 함께 저장된 악보 파트를 선택합니다. 재분석하지 않습니다.")
        self.parts.setVisible(bundle["score"]["kind"] == "gp")
        self.name.setText(bundle["title"] + " · .magic에 저장된 악보")
        self.name.setToolTip("")
        self.open_portable_part(bundle["default_part"])

    def open_portable_part(self, track):
        bundle = self.portable_bundle
        if bundle is None:
            return
        part = next(item for item in bundle["parts"] if item["track"] == track)
        plan = ScoreTimeline.load(Path(part["timeline"]))
        if (plan.data["audio_sha256"] != bundle["archive_hash"]
                or plan.data["pdf_sha256"] != part["page_inventory_sha256"]):
            raise ValueError("저장된 악보가 변경되었습니다. .magic 파일을 다시 여세요.")
        self.view.load_pages(part["pages"])
        self.timeline = plan
        self.timeline_path = Path(part["timeline"])
        self.current_index = self._follow_index = None
        self.parts.setCurrentIndex(self.parts.findData(track))
        self.page.blockSignals(True)
        self.page.setRange(1, len(part["pages"]))
        self.page.setSuffix(f" / {len(part['pages'])}")
        self.page.setValue(1)
        self.page.blockSignals(False)
        state = "보정 저장됨" if plan.data.get("reviewed") else "시간표 확인 권장"
        self.status.setText(f"저장된 악보 · {len(plan.cues)}개 연주 마디 · {state}")
        self.status.setToolTip("\n".join(plan.data.get("warnings", [])))
        self.set_busy(self.busy)
        self.update_view_status()
        self.on_position(self.player.position() if self.player else self.last_position)
        self.timeline_changed.emit()

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
        self._follow_index = None
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
        self.tools.updateGeometry()
        editable = self.portable_bundle is None
        self.choose.setEnabled(not busy and editable and self.audio is not None)
        self.refresh.setEnabled(not busy and editable and self.audio is not None)
        self.analyze.setEnabled(not busy and editable and self.pdf is not None)
        self.review.setEnabled(not busy and editable and self.timeline is not None)
        self.review.setToolTip(
            "원본 곡에서 보정한 뒤 .magic을 다시 저장하면 반영됩니다." if not editable else ""
        )
        self.parts.setEnabled(not busy)
        if not busy and self.gp_info:
            self.parts.setCurrentIndex(self.parts.findData(self.gp_info["metadata"]["track"]))

    def change_zoom(self):
        self.view.zoom = self.zoom.currentData()
        self.view.render()
        self.view_changed()

    def view_changed(self):
        self.reset_follow()
        self.update_view_status()
        self.on_position(self.player.position() if self.player else self.last_position)

    def view_matches(self):
        return bool(
            self.timeline
            and self.timeline.data.get("scroll")
            and self.timeline.data["scroll"]["profile"] == self.view.profile()
        )

    def update_view_status(self):
        frames = (self.timeline.data.get("scroll") or {}).get("frames", []) if self.timeline else []
        if frames and all(frame.get("box") for frame in frames):
            self.view_status.setText(
                "현재 마디를 중앙에 두고 PDF·GP 페이지를 이어서 따라갑니다. "
                "배율·악보 영역을 바꿔도 재분석 없이 적용됩니다."
            )
        elif not self.timeline or not self.timeline.data.get("scroll"):
            self.view_status.setText("현재 마디 중앙 따라가기: 먼저 악보 사전 분석을 실행하세요.")
        elif self.view_matches():
            self.view_status.setText("마디 위치가 없는 구간은 기존 화면 이동 경로로 재생합니다.")
        elif any(frame.get("box") for frame in frames):
            self.view_status.setText(
                "마디 위치가 있는 구간은 중앙을 따라갑니다. 없는 구간은 "
                "기존 시간표를 유지하고 화면 이동 경로를 다시 분석하세요."
            )
        else:
            self.view_status.setText(
                "페이지는 자동으로 넘깁니다. 확대 스크롤은 사전 분석에서 "
                "기존 시간표를 유지하고 재분석하세요."
            )
        if self.timeline and (not frames or not all(frame.get("box") for frame in frames)):
            self.view_status.setText(
                self.view_status.text()
                + "\n중앙 따라가기에 필요한 마디 위치를 추가하려면 "
                "기존 시간표를 유지하고 화면 이동 경로를 다시 분석하세요."
            )
        self.view_status.setToolTip(self.view_status.text())

    def attach_player(self, player):
        self.player = player
        player.playbackStateChanged.connect(self.transport_changed)
        player.playbackRateChanged.connect(lambda _: self.on_position(player.position()))

    def transport_changed(self, state):
        from PySide6.QtMultimedia import QMediaPlayer

        self.reset_follow()
        self.on_position(self.player.position())
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.camera_timer.start()
        else:
            self.camera_timer.stop()

    def follow_clock(self):
        # Only read saved timing and geometry; page rendering is queued separately.
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

    def page_scrolled(self, page):
        self.page.blockSignals(True)
        self.page.setValue(page + 1)
        self.page.blockSignals(False)

    def jump_page(self, page, *, navigate=True):
        self.page_scrolled(page)
        if self.view.page != page:
            if navigate:
                self.view.show_page(page)
            else:
                self.view.set_page(page)

    def on_position(self, milliseconds):
        playing = self.player is not None and self.player.isPlaying()
        expected = self.last_position / 1000
        if playing:
            expected += (time.monotonic() - self.anchor_time) * self.player.playbackRate()
        snap = not playing or abs(milliseconds / 1000 - expected) > 0.35
        self.last_position = milliseconds
        self.anchor_time = time.monotonic()
        self.follow_position(milliseconds / 1000, snap=snap)

    def follow_position(self, seconds, *, snap=False):
        if not self.timeline:
            self.view.set_highlight(None, None)
            return
        index = self.timeline.index_at(seconds, self.timeline.data.get("offset", 0))
        if index is None:
            self.measure.setText("악보 시작 전 / 종료")
            self.current_index = None
            self.reset_follow()
            self.view.set_highlight(None, None)
            return
        cue = self.timeline.cues[index]
        self.current_index = index
        self.measure.setText(f"{cue['measure']}마디 · {cue['visit']}회차")
        frames = (self.timeline.data.get("scroll") or {}).get("frames", [])
        box = frames[index].get("box") if frames else None
        if self.follow.isChecked():
            self.jump_page(cue["page"], navigate=not box)
        self.view.set_highlight(cue["page"], box)
        if not self.follow.isChecked():
            return
        if box:
            previous = self._follow_index
            animate = (
                previous is not None and previous + 1 == index
                and self.timeline.cues[previous]["source_index"] + 1 == cue["source_index"]
                and 0 <= cue["page"] - self.timeline.cues[previous]["page"] <= 1
                and self.player is not None and self.player.isPlaying()
            )
            end = self.timeline.starts[index + 1] if index + 1 < len(frames) else self.timeline.end
            fraction = (
                seconds - self.timeline.data.get("offset", 0) - self.timeline.starts[index]
            ) / (end - self.timeline.starts[index])
            self.view.center_box(
                box, fraction, animate=animate, snap=snap or previous is None,
            )
            self._follow_index = index
        elif self.view_matches():
            self._follow_index = None
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
        try:
            previous_xml = (recover_notation_source(self.pdf, self.paths, self.source_hash)
                            if not self.gp_source else None)
        except ValueError as error:
            self.status.setText(
                f"악보는 그대로 유지합니다. {error}\n"
                "상세 옵션에서 저장 위치를 확인한 뒤 악보 사전 분석을 다시 누르세요."
            )
            dialog.deleteLater()
            return
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
