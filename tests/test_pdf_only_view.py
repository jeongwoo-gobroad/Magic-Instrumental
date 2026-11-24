# SPDX-License-Identifier: GPL-3.0-only
"""PDF-only timing, portable views, and simultaneous-staff camera regressions."""

import hashlib
import json
import os
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from practice_player.cache import hash_file
from practice_player.config import Paths
from practice_player.magic_bundle import export_magic
from practice_player.score import ScoreTimeline
from practice_player.score_analysis import analyze_score
from practice_player.score_layout import analyze_scroll, plan_camera
from practice_player.score_view import view_profile
from tests.test_score_geometry import vector_pdf
from tests.test_web_player import open_study
from tests.test_web_player import web_url as web_url


def full_score_pdf(path):
    rows = [(900, [50, 300, 550], []), (400, [50, 300, 550], [])]
    vector_pdf(path, [((600, 1100), rows, [5, 6, 5, 4])])
    reader = PdfReader(path)
    writer = PdfWriter()
    page = reader.pages[0]
    stream = DecodedStreamObject()
    connectors = "\n".join(f"50 {y - 255} m 50 {y + 28} l S" for y, _, _ in rows)
    stream.set_data(page.get_contents().get_data() + b"\n" + connectors.encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_page(page)
    with path.open("wb") as output:
        writer.write(output)
    return path


@pytest.fixture
def pdf_only_study(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.create()
    audio = paths.targets / "Full Score Study.wav"
    sf.write(audio, np.zeros((80000, 2), dtype=np.int16), 8000)
    pdf = full_score_pdf(paths.score_targets / "Full Score Study.pdf")

    def forbidden(*_args, **_kwargs):
        pytest.fail("PDF-only analysis must not invoke OMR, MusicXML, or pitch alignment")

    for name in ("run_omr", "read_musicxml", "align_audio"):
        monkeypatch.setattr(f"practice_player.score_analysis.{name}", forbidden)
    timeline = analyze_score(
        audio, pdf, paths, mode="pdf", bpm=120, meter_beats=4, meter_unit=4
    )
    plan = ScoreTimeline.load(timeline)
    plan.data.update(offset=0.125, reviewed=True)
    plan.data["cues"][1]["start"] += 0.1
    plan.save(timeline)
    return paths, audio, pdf, timeline


@pytest.mark.parametrize("bottom", [0.14, 0.48])
def test_pdf_only_camera_keeps_simultaneous_staves_stationary(bottom):
    bars = {
        i: {
            "page": 0, "system": i // 2, "left": 0.1 + 0.4 * (i % 2),
            "right": 0.5 + 0.4 * (i % 2), "top": 0.1 + 0.5 * (i // 2),
            "bottom": bottom + 0.5 * (i // 2), "next_top": 0.6 if i < 2 else 0.98,
            "fraction0": (i % 2) / 2, "fraction1": ((i % 2) + 1) / 2,
        }
        for i in range(4)
    }
    cues = [{"source_index": i, "page": 0, "start": i * 2} for i in range(4)]
    frames = plan_camera(
        cues, 8, [[600, 1100]], bars, view_profile(2, 420, 360), keep_system=True
    )["frames"]
    assert all(frame["y0"] == frame["y1"] for frame in frames)
    assert frames[0]["y0"] == frames[1]["y0"]
    assert frames[2]["y0"] == frames[3]["y0"] > frames[0]["y0"]
    assert frames[0]["x1"] > frames[0]["x0"]


def test_pdf_only_view_reanalysis_preserves_reviewed_times_without_xml(pdf_only_study):
    paths, audio, pdf, base = pdf_only_study
    original = ScoreTimeline.load(base)
    profile = view_profile(2, 420, 360)
    result = analyze_scroll(audio, pdf, paths, base, profile)
    plan = ScoreTimeline.load(result)
    assert plan.cues == original.cues
    assert plan.data["offset"] == 0.125 and plan.data["reviewed"]
    assert plan.data["scroll"]["layout_method"] == "pdf-only-staves-barlines"
    assert plan.scroll_at(0.25)[2] == plan.scroll_at(3.9)[2]
    assert plan.scroll_at(7)[2] > plan.scroll_at(3.9)[2]
    resized = analyze_scroll(audio, pdf, paths, result, view_profile(2, 620, 360))
    assert resized != result
    assert ScoreTimeline.load(resized).cues == original.cues
    assert base.is_file()


def export_study(study):
    paths, audio, pdf, timeline = study
    return export_magic({
        "root": str(paths.root), "audio": str(audio), "instrumental": str(audio),
        "pdf": str(pdf), "timeline": str(timeline),
        "output": str(paths.root / "full-score.magic"), "title": "Full Score Study",
    })


def test_pdf_only_bundle_keeps_original_score_and_reviewed_timing(pdf_only_study):
    paths, audio, pdf, timeline = pdf_only_study
    hashes = [hash_file(p) for p in (audio, pdf, timeline)]
    output = export_study(pdf_only_study)
    plan = ScoreTimeline.load(timeline)
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        timing = json.loads(archive.read("timeline.json"))
        assert archive.read("score/source.pdf") == pdf.read_bytes()
        assert manifest["version"] == 3 and manifest["score"]["kind"] == "pdf"
        assert manifest["audio"]["codec"] == "mp3"
        assert "parts" not in manifest
        assert timing["camera_mode"] == "system"
        assert [c["start"] for c in timing["cues"]] == plan.starts
        assert timing["offset"] == 0.125 and timing["reviewed"]
        assert len(timing["cues"]) == 4
        assert timing["cues"][0]["box"]["top"] == timing["cues"][1]["box"]["top"]
        assert timing["cues"][2]["box"]["top"] > timing["cues"][1]["box"]["top"]
        assert str(paths.root) not in archive.read("timeline.json").decode()
    assert [hash_file(p) for p in (audio, pdf, timeline)] == hashes


def test_pdf_only_export_rejects_changed_measure_inventory(pdf_only_study):
    plan = ScoreTimeline.load(pdf_only_study[3])
    plan.data["originals"].pop()
    plan.save(pdf_only_study[3])
    with pytest.raises(ValueError, match="마디 수"):
        export_study(pdf_only_study)


def changed_camera_bundle(source, mode):
    target = source.with_name("invalid-camera.magic")
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as edited:
        manifest = json.loads(original.read("manifest.json"))
        timing = json.loads(original.read("timeline.json"))
        timing["camera_mode"] = mode
        data = json.dumps(timing).encode()
        manifest["entries"]["timeline.json"] = {
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        }
        for name in original.namelist():
            if name not in ("manifest.json", "timeline.json"):
                edited.writestr(name, original.read(name))
        edited.writestr("timeline.json", data)
        edited.writestr("manifest.json", json.dumps(manifest))
    return target


@pytest.mark.skipif(os.environ.get("RUN_WEB_TESTS") != "1", reason="RUN_WEB_TESTS=1 required")
@pytest.mark.parametrize("browser_name", ["chromium", "webkit"])
def test_browser_pdf_only_system_camera_and_invalid_mode(pdf_only_study, web_url, browser_name):
    from playwright.sync_api import sync_playwright

    output = export_study(pdf_only_study)
    channel = os.environ.get("PDF_ONLY_BROWSER_CHANNEL") if browser_name == "chromium" else None
    with sync_playwright() as playwright:
        browser = getattr(playwright, browser_name).launch(
            **({"channel": channel} if channel else {})
        )
        page = browser.new_page(viewport={"width": 1180, "height": 820})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(web_url)
        if not page.evaluate(
            "typeof AudioContext === 'function' && typeof AudioWorkletNode === 'function'"
        ):
            browser.close()
            pytest.skip(f"{browser_name} runtime does not expose required Web Audio APIs")
        open_study(page, output)
        page.locator("#zoom").select_option("2")

        def at(seconds):
            page.evaluate("t => MagicPlayer.seek(t, false)", seconds)
            page.wait_for_function("""() => {
                const image = document.getElementById('scoreImage');
                return image.complete && image.naturalWidth > 0;
            }""", timeout=10000)
            # Wait for the actual viewport and image dimensions to settle after resizing.
            return page.evaluate("""() => new Promise((resolve, reject) => {
                const view = document.getElementById('viewport');
                const paper = document.getElementById('paper');
                let previous = [], stable = 0, frame;
                const timer = setTimeout(() => {
                    cancelAnimationFrame(frame);
                    reject(new Error('Score camera did not settle'));
                }, 10000);
                const sample = () => {
                    const current = [view.scrollLeft, view.scrollTop,
                        view.clientWidth, view.clientHeight, paper.clientWidth, paper.clientHeight];
                    stable = current.every((value, i) => value === previous[i]) ? stable + 1 : 0;
                    previous = current;
                    if (stable >= 3) {
                        clearTimeout(timer);
                        resolve(view.scrollTop);
                    } else frame = requestAnimationFrame(sample);
                };
                frame = requestAnimationFrame(sample);
            })""")

        first = at(0.25)
        assert at(1.9) == pytest.approx(first, abs=2)
        assert at(3.9) == pytest.approx(first, abs=2)
        assert at(7) > first + 100
        page.set_viewport_size({"width": 600, "height": 960})
        resized = at(0.25)
        assert at(3.9) == pytest.approx(resized, abs=2)
        # Legacy bundles without camera_mode also keep simultaneous staves together.
        page.evaluate("delete MagicPlayer.archive().timing.camera_mode")
        page.set_viewport_size({"width": 1180, "height": 820})
        legacy_start = at(0.25)
        assert at(1.9) == pytest.approx(legacy_start, abs=2)
        bad = changed_camera_bundle(Path(output), "unknown")
        page.locator("#file").set_input_files(bad)
        page.wait_for_function("!MagicPlayer.state().loading")
        assert "화면 이동 방식" in page.locator("#status").inner_text()
        assert not page.evaluate("MagicPlayer.state().ready")
        browser.close()
        assert not errors
