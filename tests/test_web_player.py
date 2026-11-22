# SPDX-License-Identifier: GPL-3.0-only
"""Actual Chromium/WebKit audio, local GP/PDF package and offline flows.

Opt in: RUN_WEB_TESTS=1, install playwright's chromium and webkit browsers first.
"""

import json
import os
import threading
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from practice_player.magic_bundle import export_magic
from tests.test_magic_bundle import portable_study

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_WEB_TESTS") != "1", reason="RUN_WEB_TESTS=1 and browser runtimes required"
)


@pytest.fixture(scope="module")
def study_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("portable-player")
    files = {}
    for kind in ("gp", "pdf"):
        request, _, _ = portable_study(root / kind, kind, count=32 if kind == "gp" else 8)
        files[kind] = export_magic(request)
    # A genuine v1 layout containing only the selected part must remain readable.
    files["legacy"] = root / "legacy.magic"
    with zipfile.ZipFile(files["gp"]) as source, zipfile.ZipFile(files["legacy"], "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        manifest["version"] = 1
        manifest["practice"]["origin"] = -0.25
        manifest.pop("parts")
        manifest.pop("timing_basis")
        manifest["entries"] = {k: v for k, v in manifest["entries"].items()
                               if not k.startswith("parts/")}
        for name in manifest["entries"]:
            target.writestr(name, source.read(name))
        target.writestr("manifest.json", json.dumps(manifest))
    return files


@pytest.fixture(scope="module")
def web_url():
    root = Path(__file__).resolve().parents[1]

    class Quiet(SimpleHTTPRequestHandler):
        def do_GET(self):
            # A CDN can still serve yesterday's unversioned HTML after a deployment.
            if self.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<title>Previous player</title>")
                return
            super().do_GET()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=root / "web_player"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture(params=["chromium", "webkit"])
def page(request, web_url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = getattr(playwright, request.param).launch()
        context = browser.new_context(viewport={"width": 1180, "height": 820}, has_touch=True)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(web_url)
        yield page
        browser.close()
        assert errors == []


def open_study(page, path):
    page.locator("#file").set_input_files(path)
    page.wait_for_function("MagicPlayer.state().ready", timeout=45000)


def frequency(page):
    return page.evaluate("""() => {
        const a = window.testAnalyser, data = new Float32Array(a.frequencyBinCount);
        a.getFloatFrequencyData(data); let peak = 1;
        for (let i=2;i<data.length;i++) if(data[i]>data[peak]) peak=i;
        return {frequency:peak*MagicPlayer.context().sampleRate/a.fftSize, level:data[peak]};
    }""")


def test_legacy_single_part_archive_still_plays(page, study_files):
    open_study(page, study_files["legacy"])
    assert float(page.locator("#origin").input_value()) == -0.25
    assert page.locator("#partChoice").is_hidden()
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .2")


def test_phone_file_and_score_navigation(page, study_files):
    page.set_viewport_size({"width": 360, "height": 780})
    file_button = page.locator(".file-button")
    score = page.locator("#scoreArea")
    assert file_button.bounding_box()["y"] < score.bounding_box()["y"]
    open_study(page, study_files["gp"])
    page.locator("#scoreImage").wait_for(state="visible")
    for target in ("#playerControls", "#scoreArea"):
        page.locator(f'.practice-links a[href="{target}"]').click()
        assert page.locator(target).bounding_box()["y"] < 5
    assert page.get_by_label("마디 강조", exact=True).is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_highlight_toggle_preserves_paused_follow_and_camera(page, study_files):
    open_study(page, study_files["gp"])
    toggle, highlight = page.locator("#highlightEnabled"), page.locator("#highlight")
    assert toggle.is_checked() and highlight.is_visible()
    # Inspect within the change event's turn, before another animation frame can hide it.
    assert toggle.evaluate("""el => {
      el.checked = false; el.dispatchEvent(new Event('change'));
      return document.getElementById('highlight').hidden;
    }""")
    assert page.locator("#follow").is_checked()
    page.locator("#zoom").select_option("3")
    page.evaluate("MagicPlayer.seek(52, false)")
    page.wait_for_function("MagicPlayer.state().page > 0")
    page.wait_for_function("document.getElementById('viewport').scrollTop > 20")
    assert highlight.is_hidden() and not page.evaluate("MagicPlayer.state().playing")
    assert "27마디" in page.locator("#measure").inner_text()

    page.locator("#follow").uncheck()
    shown = page.evaluate("MagicPlayer.state().page")
    page.evaluate("MagicPlayer.seek(0, false)")
    toggle.check()
    assert page.evaluate("MagicPlayer.state().page") == shown
    assert highlight.is_hidden() and "1마디" in page.locator("#measure").inner_text()
    while shown > 0:
        page.locator("#prevPage").click()
        page.wait_for_function("page => MagicPlayer.state().page === page", arg=shown - 1)
        shown -= 1
    assert highlight.is_visible()

    camera = page.locator("#viewport").evaluate("el => [el.scrollLeft, el.scrollTop]")
    page.evaluate("MagicPlayer.seek(4, false)")
    assert "3마디" in page.locator("#measure").inner_text()
    assert highlight.evaluate("""el => {
      const box = MagicPlayer.archive().timing.cues[MagicPlayer.state().cue].box;
      return Math.abs(parseFloat(el.style.left) - box.left * 100) < .0001 &&
             Math.abs(parseFloat(el.style.top) - box.top * 100) < .0001;
    }""")
    toggle.uncheck()
    assert toggle.evaluate("""el => {
      el.checked = true; el.dispatchEvent(new Event('change'));
      return !document.getElementById('highlight').hidden;
    }""")
    assert not page.locator("#follow").is_checked()
    assert page.locator("#viewport").evaluate("el => [el.scrollLeft, el.scrollTop]") == camera
    assert page.evaluate("MagicPlayer.state().position") == 4
    assert not page.evaluate("MagicPlayer.state().playing")


def test_highlight_preference_survives_parts_files_and_reload(page, study_files):
    open_study(page, study_files["gp"])
    toggle, highlight = page.locator("#highlightEnabled"), page.locator("#highlight")
    toggle.uncheck()
    page.locator("#partSelect").select_option("1")
    page.wait_for_function("MagicPlayer.archive().activePart.track === 1")
    assert not toggle.is_checked() and highlight.is_hidden()
    open_study(page, study_files["pdf"])
    assert not toggle.is_checked() and highlight.is_hidden()
    assert page.evaluate("localStorage.getItem('magic-measure-highlight')") == "false"
    page.reload()
    page.wait_for_function("typeof MagicPlayer !== 'undefined'")
    assert not toggle.is_checked()
    open_study(page, study_files["pdf"])
    assert highlight.is_hidden()
    toggle.check()
    assert highlight.is_visible()
    assert page.evaluate("localStorage.getItem('magic-measure-highlight')") == "true"
    page.reload()
    page.wait_for_function("typeof MagicPlayer !== 'undefined'")
    assert toggle.is_checked()
    open_study(page, study_files["pdf"])
    assert highlight.is_visible()


def test_highlight_toggle_works_without_local_storage(page, study_files):
    page.add_init_script("""
      for (const method of ['getItem', 'setItem']) {
        Storage.prototype[method] = () => {
          throw new DOMException('Storage blocked', 'SecurityError');
        };
      }
    """)
    page.reload()
    page.wait_for_function("typeof MagicPlayer !== 'undefined'")
    open_study(page, study_files["pdf"])
    toggle, highlight = page.locator("#highlightEnabled"), page.locator("#highlight")
    assert toggle.is_checked() and highlight.is_visible()
    toggle.uncheck()
    assert highlight.is_hidden()
    page.evaluate("MagicPlayer.seek(4, false)")
    assert highlight.is_hidden() and "3마디" in page.locator("#measure").inner_text()
    toggle.check()
    assert highlight.is_visible() and not page.evaluate("MagicPlayer.state().playing")


def test_gp_opens_plays_pitch_rate_seeks_and_follows(page, study_files):
    open_study(page, study_files["gp"])
    assert page.locator("#part").inner_text() == "GP 파트 · Lead Guitar"
    assert page.locator("#partSelect option").count() == 2
    page.evaluate("""() => { window.testAnalyser=MagicPlayer.context().createAnalyser();
        testAnalyser.fftSize=16384; testAnalyser.smoothingTimeConstant=0;
        musicGain.connect(testAnalyser); }""")
    page.locator("#play").click()
    page.wait_for_timeout(1000)
    actual = frequency(page)
    assert actual["level"] > -60 and actual["frequency"] == pytest.approx(220, abs=4)
    before_part = page.evaluate("MagicPlayer.state().position")
    page.locator("#partSelect").select_option("1")
    page.wait_for_function("MagicPlayer.archive().activePart.track === 1")
    assert page.evaluate("MagicPlayer.state().playing")
    assert page.evaluate("MagicPlayer.state().position") >= before_part
    page.locator("#seek").focus()
    # Playwright's range fill emits both input and change. A second synthetic change
    # can cancel the asynchronous resume started by the first commit in WebKit.
    page.locator("#seek").fill("3")
    page.wait_for_timeout(600)
    assert float(page.locator("#seek").input_value()) > 3.2
    page.locator("#speed").fill("50")
    page.wait_for_timeout(400)
    before = page.evaluate("MagicPlayer.state().position")
    page.wait_for_timeout(1000)
    assert page.evaluate("MagicPlayer.state().position") - before == pytest.approx(0.5, abs=0.1)
    assert frequency(page)["frequency"] == pytest.approx(220, abs=4)
    page.locator("#key").select_option("12")
    page.wait_for_timeout(800)
    assert frequency(page)["frequency"] == pytest.approx(440, abs=5)
    assert "원곡 대비 +12" in page.locator("#keyText").inner_text()
    page.evaluate("MagicPlayer.pause()")
    page.evaluate("MagicPlayer.seek(52,false)")
    page.wait_for_function("MagicPlayer.state().page > 0")
    assert page.locator("#scoreImage").evaluate("el => el.complete && el.naturalWidth > 0")
    page.locator("#zoom").select_option("2")
    page.wait_for_timeout(400)
    assert page.locator("#paper").evaluate("el => el.clientWidth") > page.locator(
        "#viewport"
    ).evaluate("el => el.clientWidth")
    current = page.evaluate("MagicPlayer.state().cue")
    page.locator("#step").fill("3")
    page.locator("#back").click()
    page.wait_for_timeout(300)
    assert page.evaluate("MagicPlayer.state().cue") == current - 3
    page.set_viewport_size({"width": 600, "height": 960})
    page.wait_for_timeout(100)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_pdf_count_loop_and_offline_reload(page, web_url, study_files):
    open_study(page, study_files["pdf"])
    assert page.locator("#part").inner_text() == "PDF 악보"
    page.locator(".metronome summary").click()
    page.locator("#bpm").fill("240")
    page.locator("#bpm").dispatch_event("change")
    page.locator(".metronome summary").click()
    page.locator("#count").check()
    page.locator("#loopStart").select_option("1")
    page.locator("#loopEnd").select_option("1")
    page.locator("#loop").check()
    page.locator("#loopCount").check()
    page.locator("#metro").click()
    page.locator("#play").click()
    page.wait_for_timeout(450)
    assert page.evaluate("MagicPlayer.state().position") == pytest.approx(2, abs=0.01)
    page.wait_for_function("MagicPlayer.state().position > 2.2 && "
                           "MagicPlayer.state().position < 4", timeout=5000)
    # Observe the actual audio clock across the repeat boundary. Device startup latency
    # can put a fixed wall-clock sleep just before the end instead of inside the count.
    page.evaluate("""() => {
      window.loopProof = {returned: false, held: false, finished: false};
      let previous = MagicPlayer.state().position, returnedAt = null;
      const timer = setInterval(() => {
        const position = MagicPlayer.state().position, time = MagicPlayer.context().currentTime;
        if (returnedAt === null && previous > 2.2 && Math.abs(position - 2) < .05) {
          returnedAt = time; loopProof.returned = true;
        }
        if (returnedAt !== null && time >= returnedAt + .4) {
          loopProof.held = Math.abs(position - 2) < .05 && MagicPlayer.state().playing;
          loopProof.finished = true; clearInterval(timer);
        }
        previous = position;
      }, 20);
    }""")
    page.wait_for_function("window.loopProof.finished", timeout=8000)
    assert page.evaluate("loopProof.returned && loopProof.held")
    page.locator("#play").click()
    before = page.evaluate("MagicPlayer.state().position")
    page.wait_for_timeout(1300)
    assert page.evaluate("MagicPlayer.state().position") == before
    page.wait_for_function("navigator.serviceWorker.controller !== null")
    page.context.set_offline(True)
    page.reload()
    page.wait_for_function("typeof MagicPlayer !== 'undefined'")
    page.goto(web_url + "index.html")
    page.wait_for_function("typeof MagicPlayer !== 'undefined'")
    open_study(page, study_files["pdf"])
    page.locator("#play").click()
    page.wait_for_timeout(500)
    assert page.evaluate("MagicPlayer.state().playing")


def test_corrupt_packages_rejected_without_executing_text(page, study_files, tmp_path):
    source = study_files["gp"]
    open_study(page, source)
    corrupted = bytearray(source.read_bytes())
    with zipfile.ZipFile(source) as z:
        entry = z.getinfo("audio/instrumental.wav")
        at = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
    corrupted[at + 100] ^= 1
    broken = tmp_path / "broken.magic"
    broken.write_bytes(corrupted)
    page.locator("#file").set_input_files(broken)
    page.wait_for_function("!MagicPlayer.state().loading")
    assert "손상" in page.locator("#status").inner_text()
    assert not page.evaluate("MagicPlayer.state().ready")
    assert page.locator("#title").inner_text() == "준비한 곡을 열어 주세요"
    assert page.locator("#clock").inner_text() == "00:00 / 00:00"
    assert page.locator("#pageText").inner_text() == "0 / 0"
    assert page.locator("#partChoice").is_hidden() and page.locator("#empty").is_visible()
    assert page.locator("#play").is_disabled()
    open_study(page, study_files["pdf"])
    bad = tmp_path / "bad-path.magic"
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(bad, "w") as changed:
        for entry in original.infolist():
            changed.writestr(entry.filename, original.read(entry.filename))
        changed.writestr("../escape.js", "window.executed=true")
    page.locator("#file").set_input_files(bad)
    page.wait_for_function("!MagicPlayer.state().loading")
    assert "허용되지 않은" in page.locator("#status").inner_text()
    assert not page.evaluate("window.executed || false")


def test_standalone_metronome_hint_and_real_on_off_audio(page):
    footer = page.locator('.support-footer')
    assert '버그 및 개선 사항 제보:' in footer.inner_text()
    assert footer.locator('a').get_attribute('href') == 'mailto:pps.jeongwoo@gmail.com'
    assert "해제" in page.locator(".metro-help").inner_text()
    page.locator("#metro").click()
    page.wait_for_function("MagicPlayer.state().metro")
    page.wait_for_function("document.querySelector('#metroState').textContent === '반주 대기 중'")
    page.locator("#linked").uncheck()
    page.wait_for_function(
        "document.querySelector('#metroState').textContent === '메트로놈 단독 재생 중'"
    )
    page.evaluate("""() => {
      window.clickAnalyser=ctx.createAnalyser(); clickAnalyser.fftSize=2048;
      clickGain.connect(clickAnalyser); window.clickPeak=0;
      window.clickSampler=setInterval(()=>{const a=new Float32Array(2048);
        clickAnalyser.getFloatTimeDomainData(a);
        for(const n of a) clickPeak=Math.max(clickPeak,Math.abs(n));},5);
    }""")
    page.wait_for_timeout(1500)
    assert page.evaluate("clickPeak") > 0.05
    page.locator("#metro").click()
    page.wait_for_timeout(200)
    page.evaluate("clickPeak=0")
    page.wait_for_timeout(700)
    assert page.evaluate("clickPeak") < 0.001
    assert page.locator("#metroState").inner_text() == "꺼짐"
