# SPDX-License-Identifier: GPL-3.0-only
"""Actual desktop/tablet screenshots from wholly project-authored synthetic studies."""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen")
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from practice_player.cache import hash_file  # noqa: E402
from practice_player.config import Settings  # noqa: E402
from practice_player.gui import Window, export_variant_request  # noqa: E402
from practice_player.magic_bundle import export_magic  # noqa: E402
from practice_player.pipeline import process  # noqa: E402
from practice_player.score_layout import analyze_scroll  # noqa: E402
from tests.test_audio_variants import SixStemEngine  # noqa: E402
from tests.test_magic_bundle import portable_study  # noqa: E402


def pump(app, seconds=0.2):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        app.processEvents()
        time.sleep(0.01)


def capture(*, desktop_only=False):
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    dest = ROOT / "docs/images"
    dest.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="magic-public-demo-") as temporary:
        request, prepared, paths = portable_study(Path(temporary), count=32)
        selected = ("no_vocals", "no_guitar", "bass")
        result = process(
            Path(request["audio"]), Settings("sw6"), paths,
            stem_runner=SixStemEngine(), variant_id="no_guitar", variant_ids=selected,
        )
        request.update(
            audio_variants=[export_variant_request(result.variants[item].to_dict())
                            for item in selected],
            default_audio_variant="no_guitar",
        )
        bundle = export_magic(request)
        app = QApplication.instance() or QApplication([])
        window = Window(paths)
        window.resize(1600, 1100)
        window.source_path = Path(request["audio"])
        window.source_name = "Practice Study · 직접 만든 연습곡"
        window.load_result(result.to_dict())
        window.select_preparation_variants(False)
        for identifier in selected:
            window.variant_checks[identifier].setChecked(True)
        window.options_toggle.setChecked(True)
        panel = window.score_panel
        panel.audio, panel.source_hash = window.source_path, hash_file(window.source_path)
        panel.accept_gp(prepared)
        window.show()
        pump(app)
        timeline = analyze_scroll(
            Path(request["audio"]),
            Path(request["pdf"]),
            paths,
            Path(request["timeline"]),
            panel.view.profile(),
        )
        panel.load_timeline(timeline)
        window.progress.setValue(100)
        window.status.setText("직접 만든 예제 · 반주와 GP 파트 준비 완료")
        window.metronome.linked.setChecked(True)
        window.audio_output.setVolume(0)
        window.player.setPosition(4000)
        window.theme.setCurrentIndex(window.theme.findData("light"))
        pump(app)
        window.grab().save(str(dest / "desktop-light.png"))
        window.theme.setCurrentIndex(window.theme.findData("dark"))
        panel.zoom.setCurrentIndex(panel.zoom.findData(2.0))
        pump(app)
        timeline = analyze_scroll(
            Path(request["audio"]), Path(request["pdf"]), paths, timeline, panel.view.profile()
        )
        panel.load_timeline(timeline)
        panel.on_position(4000)
        window.main_splitter.widget(0).ensureWidgetVisible(window.magic_button)
        pump(app)
        window.grab().save(str(dest / "desktop-dark.png"))
        window.close()
        pump(app)
        if desktop_only:
            return
        from playwright.sync_api import sync_playwright

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=ROOT / "web_player")
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.webkit.launch()
                page = browser.new_page(viewport={"width": 1366, "height": 1100}, has_touch=True)
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.locator("#file").set_input_files(bundle)
                page.wait_for_function("MagicPlayer.state().ready")
                page.evaluate("MagicPlayer.seek(6,false)")
                page.locator("#speed").fill("75")
                page.locator("#speed").dispatch_event("change")
                page.locator("#key").select_option("2")
                page.evaluate("document.activeElement.blur()")
                page.wait_for_timeout(500)
                page.screenshot(path=dest / "mobile-player.png")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop-only", action="store_true",
                        help="Capture Qt screens without launching a browser")
    capture(desktop_only=parser.parse_args().desktop_only)
