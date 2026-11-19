# SPDX-License-Identifier: GPL-3.0-only
"""Bounded asynchronous release checks and an explicit desktop update handoff."""

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from . import __version__
from .release_check import API, RELEASES, numeric_version
from .release_check import MAX_RESPONSE as MAX_RESPONSE
from .release_check import release_info as parse_release

WORKER = Path(__file__).with_name("release_check.py")
MAX_WORKER_OUTPUT = 16 * 1024
CHECK_FAILED = "새 버전 확인 불가 · 다시 확인할 수 있습니다. 연습은 계속할 수 있습니다."


def release_info(data, current=__version__):
    return parse_release(data, current)


class ReleaseChecker(QObject):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None, *, endpoint=API, timeout_ms=8000):
        super().__init__(parent)
        self.endpoint = endpoint
        self.timeout_ms = timeout_ms
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        self.process = None
        self.buffer = bytearray()
        self.failure = ""
        self.current = __version__
        self.closed = False

    def check(self, current=__version__):
        if self.closed or self.process is not None:
            return False
        self.current = current
        self.buffer.clear()
        self.failure = ""
        process = QProcess(self)
        self.process = process
        # No shell, site packages, PYTHONPATH or Qt networking in the child.
        process.setProgram(sys.executable)
        process.setArguments([
            "-I", "-S", str(WORKER), "--current", current, "--endpoint", self.endpoint,
        ])
        process.setStandardErrorFile(QProcess.nullDevice())
        process.readyReadStandardOutput.connect(lambda: self._read(process))
        process.finished.connect(lambda code, status: self._finish(process, code, status))
        process.errorOccurred.connect(lambda error: self._error(process, error))
        self.timer.start(self.timeout_ms)
        process.start()
        return True

    def _read(self, process):
        if self.process is not process:
            return
        data = bytes(process.readAllStandardOutput())
        if len(self.buffer) + len(data) > MAX_WORKER_OUTPUT:
            self._kill(CHECK_FAILED)
        elif not self.failure:
            self.buffer.extend(data)

    def _release(self, process):
        self.timer.stop()
        self.process = None
        process.deleteLater()

    def _finish(self, process, code, status):
        if self.process is not process:
            return
        self._read(process)
        self._release(process)
        if self.closed:
            return
        if self.failure or code != 0 or status != QProcess.ExitStatus.NormalExit:
            self.failed.emit(self.failure or CHECK_FAILED)
            return
        try:
            result = json.loads(bytes(self.buffer))
            latest = result["latest"]
            available = numeric_version(latest) > numeric_version(self.current)
            # Even child output cannot supply an arbitrary link or UI markup.
            info = {"latest": latest, "available": available, "url": RELEASES + f"tag/v{latest}"}
        except (ValueError, KeyError, TypeError, UnicodeError):
            self.failed.emit(CHECK_FAILED)
            return
        self.completed.emit(info)

    def _error(self, process, error):
        if self.process is process and error == QProcess.ProcessError.FailedToStart:
            # FailedToStart does not emit finished. CrashExit does; reap it there.
            self._release(process)
            if not self.closed:
                self.failed.emit(CHECK_FAILED)

    def _kill(self, message):
        self.timer.stop()
        self.failure = message
        if self.process is not None:
            self.process.kill()
        # Wait for finished before enabling retry: at most one child at a time.

    def _timeout(self):
        self._kill(
            "새 버전 확인 시간이 지났습니다. 연습은 계속할 수 있으며 다시 확인할 수 있습니다."
        )

    def stop(self):
        self.closed = True
        self._kill("")
        process = self.process
        if process is not None:
            # Shutdown only; no synchronous wait during normal GUI operation.
            process.waitForFinished(1000)
            if self.process is process and process.state() == QProcess.ProcessState.NotRunning:
                self._release(process)


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
