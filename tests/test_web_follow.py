# SPDX-License-Identifier: GPL-3.0-only
"""Continuous PDF/GP camera paths in actual Chromium and WebKit viewers."""

import hashlib
import json
import os
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from practice_player.config import Paths
from practice_player.magic_bundle import export_magic
from practice_player.score_analysis import analyze_score
from practice_player.score_layout import analyze_scroll
from practice_player.score_view import view_profile
from tests import test_web_player as player_tests
from tests.test_magic_bundle import portable_study
from tests.test_score_geometry import vector_pdf
from tests.test_web_player import open_study

page = player_tests.page
web_url = player_tests.web_url

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_WEB_TESTS") != "1", reason="RUN_WEB_TESTS=1 and browser runtimes required"
)


@pytest.fixture(scope="module")
def follow_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("continuous-score")
    paths = Paths(root / "pdf")
    paths.create()
    audio = paths.targets / "Continuous Study.wav"
    rate = 44100
    times = np.arange(rate * 96) / rate
    tone = (0.1 * np.sin(2 * np.pi * 220 * times)).astype(np.float32)
    sf.write(audio, np.column_stack([tone, tone]), rate, subtype="PCM_16")
    pdf = paths.score_targets / "Continuous Study.pdf"
    vector_pdf(pdf, [
        ((595, 842), [(700, [50, 170, 290, 410, 540], []),
                       (400, [50, 170, 290, 410, 540], []),
                       (90, [50, 170, 290, 410, 540], [])], [5]),
        ((640, 880), [(740, [50, 180, 310, 440, 590], []),
                       (410, [50, 180, 310, 440, 590], []),
                       (90, [50, 180, 310, 440, 590], [])], [5]),
        ((595, 842), [(700, [50, 170, 290, 410, 540], []),
                       (400, [50, 170, 290, 410, 540], []),
                       (90, [50, 170, 290, 410, 540], [])], [5]),
    ])
    timeline = analyze_score(
        audio, pdf, paths, mode="pdf", bpm=120, route_override="1-36,1-4,29-36"
    )
    files = {"pdf": export_magic({
        "root": str(paths.root), "audio": str(audio), "instrumental": str(audio),
        "pdf": str(pdf), "timeline": str(timeline),
        "output": str(root / "continuous-pdf.magic"), "title": "Continuous PDF Study",
    })}
    request, prepared, gp_paths = portable_study(root / "gp", "gp", count=48)
    display = {key: prepared[key] for key in ("pdf", "layout", "source", "signature")}
    timeline = analyze_score(
        Path(request["audio"]), Path(request["pdf"]), gp_paths,
        musicxml=Path(prepared["source"]), gp_track=2, gp_display=display,
        mode="tempo", bpm=120, route_override="1-36,1-4,41-48",
    )
    request["timeline"] = str(analyze_scroll(
        Path(request["audio"]), Path(request["pdf"]), gp_paths, timeline,
        view_profile("width", 900, 700),
    ))
    files["gp"] = export_magic(request)
    files["bad_page"] = root / "damaged-neighbor.magic"
    with zipfile.ZipFile(files["pdf"]) as source, zipfile.ZipFile(files["bad_page"], "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        name = manifest["pages"][1]["path"]
        data = source.read(name)[:24] + b"This is not a complete PNG image."
        manifest["entries"][name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for entry in source.namelist():
            if entry != "manifest.json":
                target.writestr(entry, data if entry == name else source.read(entry))
        target.writestr("manifest.json", json.dumps(manifest))
    files["offset_pdf"] = root / "negative-offset.magic"
    with (
        zipfile.ZipFile(files["pdf"]) as source,
        zipfile.ZipFile(files["offset_pdf"], "w") as target,
    ):
        manifest = json.loads(source.read("manifest.json"))
        timing = json.loads(source.read("timeline.json"))
        timing["offset"] = -48
        data = json.dumps(timing).encode()
        manifest["entries"]["timeline.json"] = {
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        }
        for entry in source.namelist():
            if entry != "manifest.json":
                target.writestr(entry, data if entry == "timeline.json" else source.read(entry))
        target.writestr("manifest.json", json.dumps(manifest))
    return files


def wait_centered(page):
    page.wait_for_function("""() => {
      const cue = MagicPlayer.archive().timing.cues[MagicPlayer.state().cue];
      const slot = document.querySelector(`.score-page[data-page="${cue.page}"]`);
      const view = document.getElementById('viewport'), box = slot.getBoundingClientRect();
      const image = slot.querySelector('img');
      const center = box.top + (cue.box.top + cue.box.bottom) * box.height / 2;
      return image.complete && image.naturalWidth > 0 && !image.hidden &&
        Math.abs(center - view.getBoundingClientRect().top - view.clientTop -
          view.clientHeight / 2) < 2;
    }""", timeout=10000)


def score_snapshot(page):
    return page.evaluate("""() => {
      const view = document.getElementById('viewport'), rect = view.getBoundingClientRect();
      const pages = [...document.querySelectorAll('.score-page')];
      const visible = pages.filter(el => {
        const box = el.getBoundingClientRect();
        return box.bottom > rect.top && box.top < rect.bottom;
      });
      return {top: view.scrollTop, left: view.scrollLeft, height: view.clientHeight,
        visible: visible.map(el => Number(el.dataset.page)),
        loaded: pages.filter(el => el.querySelector('img').hasAttribute('src'))
          .map(el => Number(el.dataset.page)),
        timing: JSON.stringify(MagicPlayer.archive().timing), state: MagicPlayer.state()};
    }""")


@pytest.mark.parametrize("kind", ["pdf", "gp"])
def test_centers_edges_resizes_parts_and_immediately_resumes_follow(page, follow_files, kind):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    open_study(page, follow_files[kind])
    original = score_snapshot(page)["timing"]
    cues = page.evaluate("MagicPlayer.archive().timing.cues")
    for index in (0, len(cues) - 1):
        page.evaluate("t => MagicPlayer.seek(t, false)", cues[index]["start"] + 0.2)
        wait_centered(page)
        assert page.locator("#follow").is_checked()
    before = page.evaluate("MagicPlayer.state().position")
    page.locator("#follow").uncheck()
    page.evaluate("""() => {
      const view = document.getElementById('viewport');
      const last = document.querySelector('.score-page[data-page="2"]').getBoundingClientRect();
      view.scrollTop += last.top - view.getBoundingClientRect().top - view.clientTop - 8;
      return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""")
    page.wait_for_function("pageLoads.size === 0")
    gap = score_snapshot(page)
    assert gap["visible"] == [2]
    assert 0 not in gap["loaded"], "The page before the off-screen neighbor stayed decoded"
    page.locator("#follow").check()
    wait_centered(page)
    page.locator("#viewport").hover()
    wheel_start = page.locator("#viewport").evaluate("el => el.scrollTop")
    assert wheel_start >= 250, "The fixture must allow the full upward wheel movement"
    wheel_target = wheel_start - 250
    page.mouse.wheel(0, -250)
    page.wait_for_function("!document.getElementById('follow').checked")
    # The wheel event disables follow before the browser finishes scrolling.
    try:
        page.wait_for_function("""expected =>
          Math.abs(document.getElementById('viewport').scrollTop - expected) < 1
        """, arg=wheel_target)
    except PlaywrightTimeoutError as error:
        actual = page.locator("#viewport").evaluate("el => el.scrollTop")
        raise AssertionError(
            f"Wheel movement did not finish: start={wheel_start}, "
            f"expected={wheel_target}, actual={actual}"
        ) from error
    manual = score_snapshot(page)
    anchor_snapshot = """reference => {
      const view = document.getElementById('viewport'), rect = view.getBoundingClientRect();
      const center = rect.top + view.clientTop + view.clientHeight / 2;
      const slot = reference == null ?
        [...document.querySelectorAll('.score-page')].find(element => {
          const box = element.getBoundingClientRect();
          return box.top <= center && box.bottom > center;
        }) : document.querySelector(`.score-page[data-page="${reference}"]`);
      const box = slot?.getBoundingClientRect(), state = MagicPlayer.state();
      return {page: slot?.dataset.page ?? null,
        fraction: box ? (center - box.top) / box.height : null,
        scrollTop: view.scrollTop, scrollLeft: view.scrollLeft,
        width: view.clientWidth, height: view.clientHeight, viewportTop: rect.top,
        pageTop: box?.top ?? null, pageHeight: box?.height ?? null,
        windowScrollY: window.scrollY, follow: document.getElementById('follow').checked,
        position: state.position, playing: state.playing,
        optionsVisible: !document.getElementById('optionsSidebar').hidden};
    }"""
    anchor = page.evaluate(anchor_snapshot)
    assert anchor["page"] is not None, anchor
    for toggle in range(2):
        # Observe the real click immediately before the app changes its layout.
        page.evaluate(f"""reference => {{
          const snapshot = {anchor_snapshot};
          window.__magicTestSidebarClick = null;
          document.getElementById('optionsToggle').addEventListener('click', () => {{
            window.__magicTestSidebarClick = snapshot(reference);
          }}, {{capture: true, once: true}});
        }}""", arg=anchor["page"])
        page.locator("#optionsToggle").click()
        before_toggle = page.evaluate("window.__magicTestSidebarClick")
        after_toggle = page.evaluate(anchor_snapshot, arg=anchor["page"])
        try:
            page.wait_for_function("""expected => {
              const view = document.getElementById('viewport'), rect = view.getBoundingClientRect();
              const slot = document.querySelector(`.score-page[data-page="${expected.page}"]`);
              const box = slot.getBoundingClientRect();
              const center = rect.top + view.clientTop + view.clientHeight / 2;
              const fraction = (center - box.top) / box.height;
              return Math.abs(fraction - expected.fraction) < .005;
            }""", arg=anchor)
        except PlaywrightTimeoutError as error:
            diagnostics = {
                "toggle": toggle + 1, "baseline": anchor, "before_handler": before_toggle,
                "after_toggle": after_toggle,
                "at_timeout": page.evaluate(anchor_snapshot, arg=anchor["page"]),
            }
            raise AssertionError(
                f"Sidebar did not preserve the manual anchor: {json.dumps(diagnostics)}"
            ) from error
        assert not page.locator("#follow").is_checked()
        assert page.evaluate("MagicPlayer.state().position") == before
    page.locator("#highlightEnabled").uncheck()
    page.locator("#follow").check()
    wait_centered(page)
    assert abs(score_snapshot(page)["top"] - manual["top"]) > 100
    assert page.evaluate("MagicPlayer.state().position") == before
    assert not page.evaluate("MagicPlayer.state().playing")
    assert page.locator("#highlight").is_hidden()
    page.locator("#highlightEnabled").check()

    for width, height, zoom in ((768, 1024, "1"), (1180, 820, "3")):
        page.set_viewport_size({"width": width, "height": height})
        page.locator("#zoom").select_option(zoom)
        wait_centered(page)
        assert page.evaluate("MagicPlayer.state().position") == before
        snapshot = score_snapshot(page)
        assert set(snapshot["loaded"]) <= {
            n for visible in snapshot["visible"] for n in (visible - 1, visible, visible + 1)
        }
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        capture_score(page, kind, f"center-{width}")
        original_width = page.locator("#viewport").bounding_box()["width"]
        page.locator("#optionsToggle").click()
        wait_centered(page)
        assert page.locator("#viewport").bounding_box()["width"] > original_width
        assert page.locator("#highlight").is_visible()
        assert page.locator("#follow").is_checked()
        assert page.evaluate("MagicPlayer.state().position") == before
        capture_score(page, kind, f"options-collapsed-{width}")
        page.locator("#optionsToggle").click()
        wait_centered(page)
        if width == 768:
            geometry = page.locator("#viewport").evaluate("""el => [
              el.clientWidth, el.clientHeight, el.scrollWidth, el.scrollHeight,
              el.scrollLeft, el.scrollTop
            ]""")
            # Preserve both paints to diagnose WebKit's deferred resize interpolation.
            page.wait_for_timeout(750)
            wait_centered(page)
            after = page.locator("#viewport").evaluate("""el => [
              el.clientWidth, el.clientHeight, el.scrollWidth, el.scrollHeight,
              el.scrollLeft, el.scrollTop
            ]""")
            assert after == pytest.approx(geometry, abs=1)
            assert page.evaluate("MagicPlayer.state().position") == before
            assert not page.evaluate("MagicPlayer.state().playing")
            capture_score(page, kind, "center-768-settled")

    # Session mode changes viewport dimensions without a window resize event.
    page.evaluate("document.body.dataset.session = 'member'")
    wait_centered(page)
    page.evaluate("delete document.body.dataset.session")
    wait_centered(page)
    assert score_snapshot(page)["timing"] == original
    if kind == "gp":
        page.locator("#partSelect").select_option("1")
        page.wait_for_function("MagicPlayer.archive().activePart.track === 1 && "
                               "!document.getElementById('partSelect').disabled")
        wait_centered(page)
        assert page.evaluate("MagicPlayer.state().position") == before


@pytest.mark.parametrize("kind", ["pdf", "gp"])
def test_playback_flows_across_rows_pages_and_snaps_navigation(page, follow_files, kind):
    page.emulate_media(reduced_motion="no-preference")
    page.set_viewport_size({"width": 768, "height": 1024})
    open_study(page, follow_files[kind])
    cues = page.evaluate("MagicPlayer.archive().timing.cues")
    row = next(i for i in range(1, len(cues)) if cues[i]["page"] == cues[i - 1]["page"]
               and cues[i]["box"]["top"] > cues[i - 1]["box"]["top"] + 0.05)
    boundary = next(i for i in range(1, len(cues)) if cues[i]["page"] == cues[i - 1]["page"] + 1)
    page.locator("#speed").fill("50")
    for index in (row, boundary):
        page.evaluate("t => MagicPlayer.seek(t, false)", cues[index]["start"] - 0.3)
        wait_centered(page)
        if index == boundary:
            page.wait_for_function("""next => {
              const slot = document.querySelector(`.score-page[data-page="${next}"]`);
              const image = slot.querySelector('img'), box = slot.getBoundingClientRect();
              const view = document.getElementById('viewport').getBoundingClientRect();
              return image.complete && image.naturalWidth > 0 &&
                !image.hidden && box.top < view.bottom;
            }""", arg=cues[index]["page"])
            capture_score(page, kind, "next-page")
        page.evaluate("""target => {
          window.followSamples = []; window.followFinished = false; window.followDiscarded = 0;
          const cue = MagicPlayer.archive().timing.cues[target.index];
          const sample = rafTime => {
            const time = performance.now(), state = MagicPlayer.state();
            const view = document.getElementById('viewport');
            const slot = document.querySelector(`.score-page[data-page="${cue.page}"]`);
            const box = slot.getBoundingClientRect(), viewport = view.getBoundingClientRect();
            const center = box.top + (cue.box.top + cue.box.bottom) * box.height / 2;
            const targetTop = view.scrollTop + center - viewport.top - view.clientTop -
              view.clientHeight / 2;
            followSamples.push({position: state.position, cue: state.cue, page: state.page,
              top: view.scrollTop, time, rafTime, targetTop, targetCenter: center,
              viewportTop: viewport.top, width: view.clientWidth, height: view.clientHeight,
              pageHeight: box.height, playing: state.playing,
              follow: document.getElementById('follow').checked});
            if (followSamples.length > 1200) { followSamples.shift(); followDiscarded++; }
            if (state.playing && state.position >= target.start + .3) followFinished = true;
            else requestAnimationFrame(sample);
          }; requestAnimationFrame(sample);
        }""", {"index": index, "start": cues[index]["start"]})
        page.locator("#play").click()
        try:
            page.wait_for_function("window.followFinished", timeout=10000)
        finally:
            samples = page.evaluate("window.followSamples")
            save_follow_trace(page, kind, "row" if index == row else "page", index, samples)
        page.locator("#play").click()
        wait_centered(page)
        assert_smooth_follow(samples, index)

    # Repeated visits and a forward jump choose the target immediately, not every intervening page.
    jumps = [i for i in range(1, len(cues))
             if cues[i]["source_index"] != cues[i - 1]["source_index"] + 1]
    assert len(jumps) >= 2
    for index in jumps[:2]:
        page.evaluate("t => MagicPlayer.seek(t, false)", cues[index]["start"] - 0.1)
        wait_centered(page)
        page.evaluate("""target => {
          window.jumpError = null;
          const sample = () => {
            if (MagicPlayer.state().cue < target) { requestAnimationFrame(sample); return; }
            const cue = MagicPlayer.archive().timing.cues[target];
            const view = document.getElementById('viewport');
            const slot = document.querySelector(`.score-page[data-page="${cue.page}"]`);
            const rect = slot.getBoundingClientRect();
            jumpError = Math.abs(rect.top + (cue.box.top + cue.box.bottom) * rect.height / 2 -
              view.getBoundingClientRect().top - view.clientTop - view.clientHeight / 2);
          }; requestAnimationFrame(sample);
        }""", index)
        page.locator("#play").click()
        page.wait_for_function("window.jumpError !== null", timeout=10000)
        assert page.evaluate("window.jumpError") < 2
        page.locator("#play").click()
        wait_centered(page)



def assert_smooth_follow(samples, index):
    before = next(sample["top"] for sample in reversed(samples) if sample["cue"] < index)
    transition = [sample for sample in samples if sample["cue"] == index]
    assert transition, "Playback never reached the next cue"
    first = transition[0]
    target = first["targetTop"]
    distance = target - before
    assert distance > 20
    assert abs(first["top"] - before) <= 2, "The first frame jumped to the target"
    assert all(sample["follow"] for sample in transition)
    assert all((sample["width"], sample["height"]) == (first["width"], first["height"])
               for sample in transition), "The viewport resized during the transition"
    assert all(abs(sample["targetTop"] - target) < 2 for sample in transition)
    assert all(left["top"] <= right["top"] + 1
               for left, right in zip(transition, transition[1:])), "The camera moved backwards"
    between = [sample for sample in transition if before + 2 < sample["top"] < target - 2]
    # A 200 ms transition sampled every 100 ms has only one intermediate frame.
    assert between, "The camera never displayed an intermediate position"
    start = first["time"]
    for sample in transition:
        elapsed = sample["time"] - start
        bounds = []
        # DOM sampling follows the app callback; allow a small scheduling/rounding margin.
        for shift in (-20, 20):
            progress = min(1, max(0, (elapsed + shift) / 200))
            bounds.append(before + distance * progress * progress * (3 - 2 * progress))
        assert bounds[0] - 2 <= sample["top"] <= bounds[1] + 2, {
            "elapsed_ms": elapsed, "actual": sample["top"], "expected_range": bounds,
        }
    settled = next((sample for sample in transition if abs(sample["top"] - target) <= 2), None)
    assert settled, "The camera did not reach the measure center"
    gaps = [right["time"] - left["time"] for left, right in zip(transition, transition[1:])]
    elapsed = settled["time"] - start
    assert 160 <= elapsed <= 220 + max(gaps), {"settled_ms": elapsed, "frame_gaps": gaps}
    assert abs(transition[-1]["top"] - target) <= 2


def save_follow_trace(page, kind, label, index, samples):
    destination = os.environ.get("MAGIC_RENDER_CHECK_DIR")
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        engine = page.context.browser.browser_type.name
        payload = {"kind": kind, "engine": engine, "transition": label, "cue": index,
                   "expected_duration_ms": 200, "samples": samples,
                   "discarded": page.evaluate("window.followDiscarded")}
        (path / f"continuous-{kind}-{label}-{engine}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )


def capture_score(page, kind, label):
    destination = os.environ.get("MAGIC_RENDER_CHECK_DIR")
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        engine = page.context.browser.browser_type.name
        page.locator("#scoreArea").screenshot(path=path / f"continuous-{kind}-{label}-{engine}.png")


def test_late_page_load_cannot_attach_after_part_or_file_replacement(page, follow_files):
    page.evaluate("""() => {
      const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
      window.createdPageUrls = []; window.revokedPageUrls = [];
      URL.createObjectURL = value => {
        const url = create(value); createdPageUrls.push(url); return url;
      };
      URL.revokeObjectURL = url => { revokedPageUrls.push(url); revoke(url); };
      const read = MagicArchive.prototype.blob;
      window.delayNextPage = true; window.delayedPageRead = false;
      MagicArchive.prototype.blob = async function(path, ...rest) {
        if (delayNextPage && path.endsWith('/0002.png')) {
          delayNextPage = false; delayedPageRead = true;
          await new Promise(resolve => { window.releasePageRead = resolve; });
        }
        return read.call(this, path, ...rest);
      };
    }""")
    open_study(page, follow_files["gp"])
    page.wait_for_function("window.delayedPageRead")
    old_urls = page.evaluate("createdPageUrls.slice()")
    page.locator("#partSelect").select_option("1")
    page.wait_for_function("MagicPlayer.archive().activePart.track === 1 && "
                               "!document.getElementById('partSelect').disabled")
    wait_centered(page)
    page.evaluate("releasePageRead()")
    page.wait_for_timeout(100)
    assert set(old_urls) <= set(page.evaluate("revokedPageUrls"))
    current_urls = page.locator(".score-image[src]").evaluate_all("els => els.map(el => el.src)")
    assert not set(old_urls) & set(current_urls)

    page.evaluate("delayNextPage = true; delayedPageRead = false")
    open_study(page, follow_files["gp"])
    page.wait_for_function("window.delayedPageRead")
    old_urls = page.locator(".score-image[src]").evaluate_all("els => els.map(el => el.src)")
    open_study(page, follow_files["offset_pdf"])
    wait_centered(page)
    assert page.evaluate("MagicPlayer.state().page") == 2
    assert page.evaluate("MagicPlayer.state().position") == 0
    page.evaluate("releasePageRead()")
    page.wait_for_timeout(100)
    assert set(old_urls) <= set(page.evaluate("revokedPageUrls"))
    current_urls = page.locator(".score-image[src]").evaluate_all("els => els.map(el => el.src)")
    assert not set(old_urls) & set(current_urls)
    assert page.locator("#part").inner_text() == "PDF 악보"


def test_corrupt_prefetched_page_does_not_block_open_and_can_be_replaced(page, follow_files):
    open_study(page, follow_files["bad_page"])
    wait_centered(page)
    page.wait_for_timeout(100)
    assert page.evaluate("MagicPlayer.state().ready")
    assert not page.locator("#status").evaluate("el => el.classList.contains('file-error')")
    page.locator("#nextPage").click()
    page.wait_for_function("document.getElementById('status').classList.contains('file-error')")
    assert "악보 이미지" in page.locator("#status").inner_text()
    assert page.evaluate("MagicPlayer.state().ready && !MagicPlayer.state().loading")
    open_study(page, follow_files["pdf"])
    page.locator("#follow").check()
    wait_centered(page)
