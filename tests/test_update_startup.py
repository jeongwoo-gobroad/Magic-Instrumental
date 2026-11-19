# SPDX-License-Identifier: GPL-3.0-only
"""Real HTTPS + multimedia startup/shutdown in a fresh, bounded interpreter."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get("RUN_UPDATE_HTTPS_TESTS") != "1", reason="live GitHub HTTPS")
def test_real_https_with_media_gui_and_clean_process_exit(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = r'''
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication
from practice_player.config import Paths
from practice_player.gui import Window
app = QApplication([])
window = Window(Paths(Path(sys.argv[1])))
window.show()
beats, results, failures = [], [], []
heartbeat = QTimer()
heartbeat.timeout.connect(lambda: beats.append(True))
heartbeat.start(10)
window.update_bar.checker.completed.connect(results.append)
window.update_bar.checker.failed.connect(failures.append)
def finish():
    if not (results or failures):
        return
    poll.stop()
    heartbeat.stop()
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.quit()
poll = QTimer()
poll.timeout.connect(finish)
poll.start(20)
QTimer.singleShot(0, window.update_bar.check)
QTimer.singleShot(12000, app.quit)
app.exec()
assert not failures, failures
assert len(results) == 1, results
assert results[0]['url'].startswith('https://github.com/jeongwoo-gobroad/Magic-Instrumental/releases/tag/v')
assert len(beats) > 0
print('HTTPS metadata received; multimedia GUI remained responsive and closed cleanly')
'''
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script, str(tmp_path)],
        cwd=root, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "closed cleanly" in result.stdout
