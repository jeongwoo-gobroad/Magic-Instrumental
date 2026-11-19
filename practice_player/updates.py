# SPDX-License-Identifier: GPL-3.0-only
"""Bounded asynchronous release checks and an explicit desktop update handoff."""

import json
import os
import re
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from . import __version__

REPO = "jeongwoo-gobroad/Magic-Instrumental"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES = f"https://github.com/{REPO}/releases/"
MAX_RESPONSE = 1024 * 1024


def numeric_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", value):
        raise ValueError("정식 버전 번호가 아닙니다.")
    return tuple(map(int, value.split(".")))


def release_info(data: bytes, current=__version__):
    release = json.loads(data)
    tag = release["tag_name"]
    if (
        release.get("draft") is not False
        or release.get("prerelease") is not False
        or not isinstance(tag, str)
        or not tag.startswith("v")
    ):
        raise ValueError("공개 정식 버전이 아닙니다.")
    latest = tag[1:]
    available = numeric_version(latest) > numeric_version(current)
    # A newly created release may not yet have its updater inputs uploaded.
    assets = {item["name"]: item for item in release["assets"]}
    for name in (f"Magic-Instrumental-{latest}.zip", "PUBLIC_FILES.json", "SHA256SUMS.txt"):
        asset = assets[name]
        if (
            asset.get("state") != "uploaded"
            or asset.get("size", 0) <= 0
            or asset.get("browser_download_url") != RELEASES + f"download/{tag}/{name}"
        ):
            raise ValueError("업데이트 파일이 아직 준비되지 않았습니다.")
    # Never display release-body HTML or follow a URL supplied by the response.
    return {"latest": latest, "available": available, "url": RELEASES + f"tag/{tag}"}


class ReleaseChecker(QObject):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None, *, endpoint=API, timeout_ms=8000):
        super().__init__(parent)
        self.endpoint = endpoint
        self.timeout_ms = timeout_ms
        self.manager = QNetworkAccessManager(self)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        self.reply = None
        self.buffer = bytearray()
        self.current = __version__
        self.closed = False

    def check(self, current=__version__):
        if self.closed or self.reply is not None:
            return False
        self.current = current
        self.buffer.clear()
        request = QNetworkRequest(QUrl(self.endpoint))
        request.setRawHeader(b"User-Agent", b"Magic-Instrumental")
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setAttribute(
            QNetworkRequest.Attribute.CookieLoadControlAttribute, QNetworkRequest.LoadControl.Manual
        )
        request.setAttribute(
            QNetworkRequest.Attribute.CookieSaveControlAttribute, QNetworkRequest.LoadControl.Manual
        )
        request.setTransferTimeout(self.timeout_ms)
        reply = self.manager.get(request)
        self.reply = reply
        reply.setReadBufferSize(MAX_RESPONSE + 1)
        reply.readyRead.connect(lambda: self._read(reply))
        reply.finished.connect(lambda: self._finish(reply))
        self.timer.start(self.timeout_ms)
        return True

    def _read(self, reply):
        if self.reply is not reply:
            return
        self.buffer.extend(bytes(reply.readAll()))
        if len(self.buffer) > MAX_RESPONSE:
            self._fail("버전 정보를 확인하지 못했습니다. 다시 확인해 주세요.")

    def _finish(self, reply):
        if self.reply is not reply:
            return
        self._read(reply)
        if self.reply is not reply:
            return
        self.reply = None
        self.timer.stop()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.error()
        reply.deleteLater()
        if error != QNetworkReply.NetworkError.NoError or status != 200:
            self.failed.emit(
                "새 버전 확인 불가 · 인터넷 연결 후 다시 확인하세요. 연습은 계속할 수 있습니다."
            )
            return
        try:
            info = release_info(bytes(self.buffer), self.current)
        except (ValueError, KeyError, TypeError, UnicodeError):
            self.failed.emit("새 버전 정보를 확인하지 못했습니다. 잠시 후 다시 확인해 주세요.")
            return
        self.completed.emit(info)

    def _fail(self, message):
        self.timer.stop()
        reply, self.reply = self.reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()
        if not self.closed:
            self.failed.emit(message)

    def _timeout(self):
        self._fail(
            "새 버전 확인 시간이 지났습니다. 연습은 계속할 수 있으며 다시 확인할 수 있습니다."
        )

    def stop(self):
        self.closed = True
        self._fail("")


def open_updater(root: Path):
    if (root / ".git").exists():
        raise RuntimeError("개발용 Git 폴더는 자동 교체하지 않습니다. 개발자 문서를 확인하세요.")
    launcher = root / ("update.bat" if sys.platform == "win32" else "update.command")
    if not launcher.is_file() or launcher.is_symlink():
        raise RuntimeError("update 파일이 없습니다. 소개 페이지의 업데이트 도구를 사용하세요.")
    if sys.platform == "win32":
        # ShellExecute opens a visible console and handles spaces/metacharacters as a file path.
        os.startfile(str(launcher), "open", cwd=str(root))
    elif sys.platform == "darwin":
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(launcher))):
            raise RuntimeError("업데이트 창을 열지 못했습니다. update.command를 직접 실행하세요.")
    else:
        raise RuntimeError("더블클릭 업데이트는 Mac·Windows에서 지원합니다.")


class UpdateBar(QFrame):
    update_requested = Signal()

    def __init__(self, root: Path, parent=None, *, current=__version__, checker=None):
        super().__init__(parent)
        self.root = root
        self.current = current
        self.info = None
        self.busy = False
        self.setObjectName("updateBar")
        self.checker = checker or ReleaseChecker(self)
        self.checker.completed.connect(self.show_release)
        self.checker.failed.connect(self.show_failure)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        self.version_label = QLabel(f"현재 v{current}")
        self.version_label.setObjectName("currentVersion")
        layout.addWidget(self.version_label)
        self.status = QLabel("새 버전을 확인할 수 있습니다.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setObjectName("updateStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status, 1)
        self.retry = QPushButton("업데이트 확인")
        self.retry.clicked.connect(self.check)
        layout.addWidget(self.retry)
        self.details = QPushButton("변경 내용")
        self.details.clicked.connect(self.open_details)
        self.details.hide()
        layout.addWidget(self.details)
        self.install = QPushButton("종료하고 업데이트")
        self.install.setObjectName("primary")
        self.install.setToolTip(
            "프로그램을 종료하고 업데이트 창을 엽니다. 음원·악보·처리 결과는 유지됩니다."
        )
        self.install.clicked.connect(self.update_requested)
        self.install.hide()
        layout.addWidget(self.install)

    def check(self):
        if self.checker.check(self.current):
            self.retry.setEnabled(False)
            self.status.setText("새 버전 확인 중… 연습은 바로 시작하셔도 됩니다.")

    def show_release(self, info):
        self.info = info
        self.retry.setEnabled(True)
        self.retry.setText("다시 확인")
        available = info["available"]
        self.details.setVisible(available)
        self.install.setVisible(available)
        self.set_busy(self.busy)

    def set_busy(self, busy):
        self.busy = busy
        self.install.setEnabled(not busy)
        if not self.info:
            return
        latest = self.info["latest"]
        if self.info["available"]:
            suffix = (
                "작업 완료 후 업데이트할 수 있습니다."
                if busy
                else "음원·악보·처리 결과는 유지됩니다."
            )
            self.status.setText(f"새 버전 v{latest} · {suffix}")
        else:
            self.status.setText(f"최신 공개 버전 v{latest} · 새 업데이트가 없습니다.")

    def show_failure(self, message):
        self.retry.setEnabled(True)
        self.retry.setText("다시 확인")
        self.status.setText(message)

    def open_details(self):
        if self.info:
            QDesktopServices.openUrl(QUrl(self.info["url"]))

    def stop(self):
        self.checker.stop()
