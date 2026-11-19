# SPDX-License-Identifier: GPL-3.0-only
"""Capture the real update UI with explicit example version metadata and no user files."""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'windows' if sys.platform == 'win32' else 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from practice_player import __version__  # noqa: E402
from practice_player.cache import hash_file  # noqa: E402
from practice_player.gui import Window  # noqa: E402
from practice_player.score_layout import analyze_scroll  # noqa: E402
from practice_player.updates import RELEASES  # noqa: E402
from tests.test_magic_bundle import portable_study  # noqa: E402


def capture():
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    with tempfile.TemporaryDirectory(prefix='magic-update-example-') as temp:
        request, prepared, paths = portable_study(Path(temp), count=8)
        window = Window(paths)
        window.resize(1200, 820)
        window.source_path = Path(request['audio'])
        window.source_name = "Practice Study"
        window.load_result({'instrumental': request['instrumental'], 'cached': True})
        window.score_panel.audio = window.source_path
        window.score_panel.source_hash = hash_file(window.source_path)
        window.score_panel.accept_gp(prepared)
        window.score_panel.load_timeline(request['timeline'])
        window.theme.setCurrentIndex(window.theme.findData('light'))
        window.update_bar.current = '2.2.0'
        window.update_bar.version_label.setText('현재 v2.2.0')
        window.update_bar.show_release({'latest': __version__, 'available': True,
                                        'url': RELEASES + f'tag/v{__version__}'})
        window.show()
        until = time.monotonic() + 2
        while time.monotonic() < until:
            app.processEvents()
            time.sleep(.01)
        panel = window.score_panel
        timeline = analyze_scroll(window.source_path, Path(prepared['pdf']), paths,
                                  Path(request['timeline']), panel.view.profile())
        panel.load_timeline(timeline)
        window.progress.setValue(100)
        app.processEvents()
        assert window.update_bar.install.isVisible()
        assert window.update_bar.install.geometry().right() <= window.update_bar.width()
        path = ROOT / 'docs/images/update-notice.png'
        window.grab().save(str(path))
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        print(path)


if __name__ == '__main__':
    capture()
