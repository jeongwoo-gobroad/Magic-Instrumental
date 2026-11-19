# SPDX-License-Identifier: GPL-3.0-only
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication

from practice_player import updates
from practice_player.config import Paths
from practice_player.gui import Window


def payload(latest="2.3.0"):
    names = [f"Magic-Instrumental-{latest}.zip", "PUBLIC_FILES.json", "SHA256SUMS.txt"]
    return {
        "tag_name": f"v{latest}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": name,
                "state": "uploaded",
                "size": 100,
                "browser_download_url": updates.RELEASES + f"download/v{latest}/{name}",
            }
            for name in names
        ],
    }


def pump(app, predicate, timeout=3):
    until = time.monotonic() + timeout
    while not predicate() and time.monotonic() < until:
        app.processEvents()
        time.sleep(0.005)
    assert predicate()


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def endpoint():
    config = {"status": 200, "body": json.dumps(payload()).encode(), "delay": 0.1, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            config["requests"].append((self.path, dict(self.headers)))
            time.sleep(config["delay"])
            self.send_response(config["status"])
            if config.get("location"):
                self.send_header("Location", config["location"])
            self.end_headers()
            try:
                self.wfile.write(config["body"])
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/releases/latest", config
    server.shutdown()
    server.server_close()
    thread.join()


def test_numeric_versions_require_ready_official_stable_assets():
    assert updates.release_info(json.dumps(payload("2.10.0")).encode(), "2.9.9")["available"]
    assert not updates.release_info(json.dumps(payload()).encode(), "2.3.0")["available"]
    assert not updates.release_info(json.dumps(payload()).encode(), "3.0.0")["available"]
    for change in [
        dict(draft=True),
        dict(prerelease=True),
        dict(tag_name="v2.3.0-rc1"),
        dict(assets=[]),
        dict(tag_name="<b>9</b>"),
    ]:
        data = payload()
        data.update(change)
        with pytest.raises((ValueError, KeyError)):
            updates.release_info(json.dumps(data).encode(), "2.2.0")
    data = payload()
    data["assets"][0]["browser_download_url"] = "https://example.invalid/untrusted.zip"
    with pytest.raises(ValueError):
        updates.release_info(json.dumps(data).encode(), "2.2.0")


def test_release_check_is_async_bounded_and_does_not_send_song_data(app, endpoint):
    url, config = endpoint
    checker = updates.ReleaseChecker(endpoint=url)
    completed, failed, heartbeats = [], [], []
    checker.completed.connect(completed.append)
    checker.failed.connect(failed.append)
    timer = QTimer()
    timer.timeout.connect(lambda: heartbeats.append(True))
    timer.start(5)
    try:
        start = time.monotonic()
        assert checker.check("2.2.0")
        assert time.monotonic() - start < 0.15
        assert not checker.check("2.2.0")  # no concurrent requests
        pump(app, lambda: bool(completed or failed))
        assert not failed and completed[0]["latest"] == "2.3.0"
        assert len(heartbeats) > 3
        assert len(config["requests"]) == 1
        path, headers = config["requests"][0]
        assert path == "/releases/latest"
        assert "Authorization" not in headers and "Cookie" not in headers
        assert headers["User-Agent"] == "Magic-Instrumental"
    finally:
        timer.stop()
        checker.stop()
        checker.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("failure", ["http", "malformed", "oversize", "timeout"])
def test_check_failures_are_inline_and_retryable(app, endpoint, failure):
    url, config = endpoint
    if failure == "http":
        config["status"] = 403
    if failure == "malformed":
        config["body"] = b"not JSON"
    if failure == "oversize":
        config["body"] = b"x" * (updates.MAX_RESPONSE + 1)
    if failure == "timeout":
        config["delay"] = 0.35
    checker = updates.ReleaseChecker(endpoint=url, timeout_ms=60 if failure == "timeout" else 2000)
    bar = updates.UpdateBar(Paths().root, current="2.2.0", checker=checker)
    bar.show()
    try:
        bar.check()
        pump(app, lambda: bar.retry.isEnabled())
        assert not bar.install.isVisible()
        assert "확인" in bar.status.text()
        if failure == "http":
            assert "HTTP 403" in bar.status.text()
        assert "2.2.0" in bar.version_label.text()
        # The same checker can recover after network errors, without restarting the app.
        checker.timeout_ms = 2000
        config.update(status=200, body=json.dumps(payload()).encode(), delay=0)
        bar.check()
        pump(app, lambda: bar.info is not None)
        assert bar.install.isEnabled() and bar.install.isVisible()
    finally:
        bar.stop()
        bar.close()
        bar.deleteLater()
        checker.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_shutdown_aborts_pending_check_without_late_ui_signal(app, endpoint):
    url, config = endpoint
    config["delay"] = 0.3
    checker = updates.ReleaseChecker(endpoint=url)
    events = []
    checker.completed.connect(events.append)
    checker.failed.connect(events.append)
    checker.check()
    checker.stop()
    assert checker.process is None and not checker.timer.isActive()
    assert not checker.check()
    until = time.monotonic() + 0.4
    while time.monotonic() < until:
        app.processEvents()
        time.sleep(0.01)
    assert not events
    checker.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_gui_update_button_preserves_busy_work_and_reports_launch_errors(
    app, tmp_path, monkeypatch
):
    window = Window(Paths(tmp_path))
    window.show()
    calls = []
    monkeypatch.setattr("practice_player.gui.open_updater", lambda root: calls.append(root))
    try:
        bar = window.update_bar
        bar.show_release(updates.release_info(json.dumps(payload("9.0.0")).encode()))
        window.worker = object()
        window.set_busy(True)
        assert not bar.install.isEnabled() and "작업 완료" in bar.status.text()
        window.begin_update()
        assert not calls and window.isVisible()
        window.worker = None
        window.set_busy(False)

        def fail(_):
            raise OSError("업데이트 창 열기 실패")

        monkeypatch.setattr("practice_player.gui.open_updater", fail)
        bar.install.click()
        assert window.isVisible() and "열기 실패" in bar.status.text()
        monkeypatch.setattr("practice_player.gui.open_updater", lambda root: calls.append(root))
        bar.install.click()
        assert len(calls) == 1 and not window.isVisible()
        assert bar.checker.closed
        assert "버그 및 개선 사항 제보:" in window.support.text()
        assert "mailto:pps.jeongwoo@gmail.com" in window.support.text()
    finally:
        window.worker = None
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_native_handoff_uses_file_paths_and_refuses_git_checkout(tmp_path, monkeypatch):
    root = tmp_path / "App & 한국어"
    root.mkdir()
    for name in ("update.command", "update.bat"):
        (root / name).touch()
    calls = []
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(
        updates.os, "startfile", lambda *a, **kw: calls.append((a, kw)), raising=False
    )
    updates.open_updater(root)
    assert calls[0] == ((str(root / "update.bat"), "open"), {"cwd": str(root)})
    (root / ".git").mkdir()
    with pytest.raises(RuntimeError, match="Git"):
        updates.open_updater(root)


@pytest.mark.parametrize("failure", ["hung", "killed", "exit", "oversize", "missing_python"])
def test_worker_failures_do_not_take_down_gui_and_can_retry(
    app, endpoint, tmp_path, monkeypatch, failure
):
    url, _ = endpoint
    real_worker, real_python = updates.WORKER, updates.sys.executable
    worker = tmp_path / "worker.py"
    scripts = {
        "hung": "import time; time.sleep(30)",
        "killed": "import time; time.sleep(30)",
        "exit": "import os; os._exit(7)",
        "oversize": "print('x' * 20000, flush=True)",
        "missing_python": "",
    }
    worker.write_text(scripts[failure])
    monkeypatch.setattr(updates, "WORKER", worker)
    if failure == "missing_python":
        monkeypatch.setattr(updates.sys, "executable", str(tmp_path / "missing-python"))
    checker = updates.ReleaseChecker(endpoint=url, timeout_ms=300)
    results, errors, heartbeats = [], [], []
    checker.completed.connect(results.append)
    checker.failed.connect(errors.append)
    timer = QTimer()
    timer.timeout.connect(lambda: heartbeats.append(True))
    timer.start(5)
    try:
        started = checker.check("2.2.0")
        assert started or errors  # Windows may fail synchronously before check returns.
        if failure == "killed":
            pump(app, lambda: checker.process.processId() > 0)
            checker.process.kill()
        pump(app, lambda: bool(errors))
        assert checker.process is None and not checker.timer.isActive()
        if failure == "hung":
            assert len(heartbeats) > 10 and "시간" in errors[0]
        monkeypatch.setattr(updates, "WORKER", real_worker)
        monkeypatch.setattr(updates.sys, "executable", real_python)
        checker.timeout_ms = 2000
        assert checker.check("2.2.0")
        pump(app, lambda: bool(results))
        assert len(errors) == 1 and results[0]["available"]
    finally:
        timer.stop()
        checker.stop()
        checker.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_running_worker_is_killed_on_window_shutdown(app, endpoint):
    url, config = endpoint
    config["delay"] = 0.4
    checker = updates.ReleaseChecker(endpoint=url)
    events = []
    checker.completed.connect(events.append)
    checker.failed.connect(events.append)
    try:
        checker.check()
        pump(app, lambda: bool(config["requests"]))
        process = checker.process
        assert process.processId() > 0
        checker.stop()
        assert checker.process is None
        assert process.state() == updates.QProcess.ProcessState.NotRunning
        assert not events
    finally:
        checker.stop()
        checker.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_release_worker_rejects_redirects_without_following(app, endpoint):
    # urllib's default redirect following must not replace the official endpoint.
    url, config = endpoint
    config.update(status=302, location=url + "/elsewhere")
    checker = updates.ReleaseChecker(endpoint=url)
    errors = []
    checker.failed.connect(errors.append)
    try:
        checker.check()
        pump(app, lambda: bool(errors))
        assert len(config["requests"]) == 1
    finally:
        checker.stop()
        checker.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)



def test_synchronous_start_failure_is_not_overwritten_by_checking_label(
    app, endpoint, monkeypatch
):
    original = updates.QProcess

    class ImmediateFailure(original):
        def start(self):
            self.errorOccurred.emit(self.ProcessError.FailedToStart)

    monkeypatch.setattr(updates, "QProcess", ImmediateFailure)
    checker = updates.ReleaseChecker(endpoint=endpoint[0])
    bar = updates.UpdateBar(Paths().root, current="2.2.0", checker=checker)
    try:
        bar.check()
        assert bar.retry.isEnabled()
        assert "확인 불가" in bar.status.text()
        assert checker.process is None
        monkeypatch.setattr(updates, "QProcess", original)
        bar.check()
        pump(app, lambda: bar.info is not None)
        assert bar.retry.isEnabled() and bar.info["available"]
    finally:
        bar.stop()
        bar.deleteLater()
        checker.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
