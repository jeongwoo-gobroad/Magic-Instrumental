# SPDX-License-Identifier: GPL-3.0-only
"""Actual Chromium/WebKit audio, local GP/PDF package and offline flows.

Opt in: RUN_WEB_TESTS=1, install playwright's chromium and webkit browsers first.
"""

import hashlib
import json
import os
import threading
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

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
        files[f"wav_{kind}"] = export_magic({
            **request, "audio_format": "wav", "output": str(root / f"wav-{kind}.magic"),
        })
        if kind == "pdf":
            marker_request = request
    rate = 44100
    markers = np.zeros(16 * rate + 137, dtype=np.float32)
    pulse = 0.6 * np.hanning(100)
    for start in (1000, 4 * rate + 6000, 16 * rate - 2000):
        markers[start:start + len(pulse)] = pulse
    marker_wav = root / "marker-instrumental.wav"
    sf.write(marker_wav, np.column_stack([markers, markers]), rate, subtype="PCM_16")
    for bitrate in (128, 192, 256, 320):
        files[f"markers_{bitrate}"] = export_magic({
            **marker_request, "instrumental": str(marker_wav), "mp3_bitrate_kbps": bitrate,
            "output": str(root / f"markers-{bitrate}.magic"),
        })
    # A genuine v1 layout containing only the selected part must remain readable.
    files["legacy"] = root / "legacy.magic"
    with (
        zipfile.ZipFile(files["wav_gp"]) as source,
        zipfile.ZipFile(files["legacy"], "w") as target,
    ):
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
    page.wait_for_function("!MagicPlayer.state().loading", timeout=45000)
    assert page.evaluate("MagicPlayer.state().ready"), page.evaluate("""() => {
      const b = window.decodedForTest;
      return {status: document.getElementById('status').textContent,
              nativeError: window.nativeDecodeError,
              decoded: b && {frames: b.length, rate: b.sampleRate, channels: b.numberOfChannels}};
    }""")


def choose_file(page, payload, button="#soloFile .file-button"):
    with page.expect_file_chooser() as opened:
        page.locator(button).click()
    chooser = opened.value
    # iPad's native picker must not filter an unregistered extension by MIME type.
    assert not chooser.element.get_attribute("accept")
    assert chooser.element.is_visible()
    chooser.set_files(payload)


def hold_audio_resume(page):
    # A native picker does not promise an audio-unlocking gesture on return.
    # Leave the real decoder intact and model resume() waiting for permission.
    page.evaluate("""() => {
      const Audio = window.AudioContext || window.webkitAudioContext;
      const resume = Audio.prototype.resume;
      window.audioResumeCalls = 0;
      window.audioResumeAllowed = false;
      Audio.prototype.resume = function (...args) {
        window.audioResumeCalls++;
        return window.audioResumeAllowed ? resume.apply(this, args) : new Promise(() => {});
      };
    }""")


def wait_for_file_without_audio(page):
    page.wait_for_function("MagicPlayer.state().ready || window.audioResumeCalls > 0",
                           timeout=45000)
    assert page.evaluate("window.audioResumeCalls") == 0, page.evaluate("""() => ({
      state: MagicPlayer.state(), disabled: document.getElementById('file').disabled,
      status: document.getElementById('status').textContent
    })""")
    assert page.evaluate("MagicPlayer.state().ready && !MagicPlayer.state().loading")


@pytest.mark.parametrize("kind", ["pdf", "wav_gp"])
def test_native_selection_loads_without_audio_permission_and_retries(page, study_files, kind):
    hold_audio_resume(page)
    page.evaluate("""() => {
      window.fileSelections = 0;
      document.getElementById('file').addEventListener('change', () => window.fileSelections++);
    }""")
    payload = {"name": "연습곡.magic", "mimeType": "",
               "buffer": study_files[kind].read_bytes()}
    choose_file(page, payload)
    wait_for_file_without_audio(page)
    assert page.locator("#play").is_enabled()
    assert page.locator("#file").is_enabled()
    page.locator("#scoreImage").wait_for(state="visible")
    assert not page.evaluate("MagicPlayer.state().playing")

    # Cancel keeps the loaded song; repeating the same choice must still load it.
    choose_file(page, [])
    assert page.evaluate("MagicPlayer.state().ready && !MagicPlayer.state().loading")
    before = page.evaluate("window.fileSelections")
    choose_file(page, payload)
    wait_for_file_without_audio(page)
    assert page.evaluate("window.fileSelections") == before + 1
    assert page.locator("#file").input_value() == ""

    choose_file(page, {"name": "notes.txt", "mimeType": "text/plain",
                       "buffer": b"This is a text document, not a Magic practice archive."})
    page.wait_for_function("!MagicPlayer.state().loading || window.audioResumeCalls > 0")
    assert page.evaluate("window.audioResumeCalls") == 0
    assert not page.evaluate("MagicPlayer.state().ready")
    assert "올바른 .magic 파일이 아닙니다" in page.locator("#status").inner_text()
    assert page.locator("#file").is_enabled()
    choose_file(page, payload)
    wait_for_file_without_audio(page)

    # The explicit Play gesture can now unlock the actual audio implementation.
    page.evaluate("MagicPlayer.context().suspend()")
    page.evaluate("window.audioResumeAllowed = true")
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .2")
    assert page.evaluate("window.audioResumeCalls") > 0


def test_audio_permission_timeout_preserves_loaded_file_and_allows_play_retry(page, study_files):
    hold_audio_resume(page)
    choose_file(page, study_files["pdf"])
    wait_for_file_without_audio(page)
    expected_duration = page.evaluate("MagicPlayer.state().duration")
    page.evaluate("MagicPlayer.context().suspend()")
    page.locator("#play").click()
    page.wait_for_function("document.getElementById('status').classList.contains('file-error')",
                           timeout=10000)
    assert "재생" in page.locator("#status").inner_text()
    assert page.evaluate("MagicPlayer.state().ready && !MagicPlayer.state().loading")
    assert not page.evaluate("MagicPlayer.state().playing")
    assert page.evaluate("MagicPlayer.state().duration") == expected_duration
    assert page.locator("#scoreImage").is_visible()
    assert page.locator("#file").is_enabled() and page.locator("#play").is_enabled()
    page.evaluate("window.audioResumeAllowed = true")
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .2")


def test_suspended_worklet_cleanup_does_not_block_file_replacement(page, study_files):
    open_study(page, study_files["gp"])
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .2")
    page.locator("#play").click()
    page.evaluate("""async () => {
      stretch.dropBuffers = () => new Promise(() => {});
      await MagicPlayer.context().suspend();
    }""")
    hold_audio_resume(page)
    choose_file(page, study_files["wav_pdf"])
    wait_for_file_without_audio(page)
    assert page.locator("#part").inner_text() == "PDF 악보"
    assert page.locator("#partChoice").is_hidden()
    assert page.locator("#file").is_enabled()
    assert page.locator("#scoreImage").is_visible()
    assert not page.evaluate("MagicPlayer.state().playing")
    page.evaluate("window.audioResumeAllowed = true")
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .2")


@pytest.mark.parametrize("kind,mime_type", [("gp", ""), ("pdf", "application/octet-stream")])
def test_file_button_reads_unknown_types_and_recovers(page, study_files, kind, mime_type):
    payload = {"name": "연습곡.magic", "mimeType": mime_type,
               "buffer": study_files[kind].read_bytes()}
    choose_file(page, payload)
    page.wait_for_function("MagicPlayer.state().ready", timeout=45000)
    page.locator("#scoreImage").wait_for(state="visible")
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .2")
    page.locator("#play").click()

    choose_file(page, {"name": "notes.txt", "mimeType": "text/plain",
                       "buffer": b"This is a text document, not a Magic practice archive."})
    page.wait_for_function("!MagicPlayer.state().loading")
    assert "올바른 .magic 파일이 아닙니다" in page.locator("#status").inner_text()
    assert not page.evaluate("MagicPlayer.state().ready")
    assert page.locator("#play").is_disabled()

    choose_file(page, payload)
    page.wait_for_function("MagicPlayer.state().ready", timeout=45000)
    assert page.locator("#title").inner_text() == "Practice Study"
    assert page.locator("#play").is_enabled()


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


@pytest.mark.parametrize("kind,version", [("gp", 2), ("pdf", 1)])
def test_wav_archives_still_play(page, study_files, kind, version):
    open_study(page, study_files[f"wav_{kind}"])
    assert page.evaluate("MagicPlayer.archive().manifest.version") == version
    assert page.evaluate("MagicPlayer.archive().manifest.audio.path") == "audio/instrumental.wav"
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .2")
    page.evaluate("MagicPlayer.seek(4, false)")
    assert page.evaluate("MagicPlayer.state().cue") == 2


@pytest.mark.parametrize("bitrate", [128, 192, 256, 320])
def test_mp3_gapless_markers_and_paused_seek(page, study_files, bitrate):
    page.evaluate("""() => {
      const Audio = window.AudioContext || window.webkitAudioContext;
      const decode = Audio.prototype.decodeAudioData;
      Audio.prototype.decodeAudioData = async function (...args) {
        try {
          const buffer = await decode.apply(this, args);
          window.decodedForTest = buffer;
          return buffer;
        } catch (error) { window.nativeDecodeError = String(error); throw error; }
      };
    }""")
    open_study(page, study_files[f"markers_{bitrate}"])
    actual = page.evaluate("""() => {
      const buffer = window.decodedForTest, samples = buffer.getChannelData(0);
      const rate = buffer.sampleRate;
      const peaks = [1000, 4 * rate + 6000, 16 * rate - 2000].map(start => {
        let peak = start - 200;
        for (let i = start - 199; i < start + 300; i++) {
          if (Math.abs(samples[i]) > Math.abs(samples[peak])) peak = i;
        }
        return peak;
      });
      return {frames: buffer.length, rate, channels: buffer.numberOfChannels,
              duration: MagicPlayer.state().duration, peaks,
              version: MagicPlayer.archive().manifest.version,
              bitrate: MagicPlayer.archive().manifest.audio.bitrate_kbps};
    }""")
    assert actual["version"] == 3 and actual["bitrate"] == bitrate
    assert actual["frames"] == 16 * 44100 + 137 and actual["rate"] == 44100
    assert actual["channels"] == 2 and actual["duration"] == (16 * 44100 + 137) / 44100
    assert actual["peaks"] == pytest.approx([1049.5, 4 * 44100 + 6049.5,
                                           16 * 44100 - 1950.5], abs=2)
    for seconds, cue in [(0, 0), (4, 2), (15.99, 7)]:
        page.evaluate("seconds => MagicPlayer.seek(seconds, false)", seconds)
        assert page.evaluate("MagicPlayer.state().cue") == cue
        assert page.evaluate("MagicPlayer.state().position") == seconds


def test_mp3_marker_stays_at_cue_through_count_and_loop(page, study_files):
    open_study(page, study_files["markers_256"])
    page.locator(".metronome summary").click()
    page.locator("#bpm").fill("240")
    page.locator("#bpm").dispatch_event("change")
    page.locator(".metronome summary").click()
    page.locator("#loopStart").select_option("2")
    page.locator("#loopEnd").select_option("2")
    page.locator("#loop").check()
    page.locator("#count").check()
    page.locator("#loopCount").check()
    page.evaluate("""async () => {
      const source = `class MarkerProbe extends AudioWorkletProcessor {
        constructor() { super(); this.last = -Infinity; }
        process(inputs) {
          const data = inputs[0]?.[0];
          if (data) for (let i = 0; i < data.length; i++) {
            const time = currentTime + i / sampleRate;
            if (Math.abs(data[i]) > .03 && time - this.last > .5) {
              this.port.postMessage(time); this.last = time;
            }
          }
          return true;
        }
      }; registerProcessor('marker-probe', MarkerProbe);`;
      const url = URL.createObjectURL(new Blob([source], {type: 'text/javascript'}));
      try { await ctx.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
      window.markerVisits = [];
      window.markerNode = new AudioWorkletNode(ctx, 'marker-probe');
      markerNode.port.onmessage = event => {
        const position = nowPosition(event.data);
        markerVisits.push({position, cue: cueAt(position), audioTime: event.data});
      };
      musicGain.connect(markerNode); markerNode.connect(ctx.destination);
    }""")
    page.locator("#play").click()
    page.wait_for_function("window.markerVisits.length >= 2", timeout=10000)
    visits = page.evaluate("window.markerVisits.slice(0, 2)")
    page.evaluate("""() => {
      MagicPlayer.pause(); musicGain.disconnect(markerNode);
      markerNode.disconnect(); markerNode.port.close();
    }""")
    assert [visit["cue"] for visit in visits] == [2, 2]
    assert [visit["position"] for visit in visits] == pytest.approx([4.137, 4.137], abs=.1)
    assert visits[1]["audioTime"] - visits[0]["audioTime"] >= 2.8


def test_mp3_metadata_limits_reject_before_decode(page, study_files, tmp_path):
    page.evaluate("""() => {
      const Audio = window.AudioContext || window.webkitAudioContext;
      window.decodeCalls = 0;
      Audio.prototype.decodeAudioData = async () => {
        window.decodeCalls++; throw Error('Unexpected decode');
      };
    }""")
    changes = [
        {"codec": "wav"}, {"path": "audio/instrumental.wav"}, {"bitrate_kbps": 224},
        {"frames": True}, {"frames": -1}, {"frames": 134217729, "duration": 134217729 / 44100},
        {"frames": 44100, "duration": 16}, {"sample_rate": 48000},
    ]
    for index, change in enumerate(changes):
        broken = tmp_path / f"bad-mp3-metadata-{index}.magic"
        with zipfile.ZipFile(study_files["pdf"]) as source, zipfile.ZipFile(broken, "w") as target:
            manifest = json.loads(source.read("manifest.json"))
            manifest["audio"].update(change)
            for name in source.namelist():
                target.writestr(name, json.dumps(manifest) if name == "manifest.json"
                                else source.read(name))
        page.locator("#file").set_input_files(broken)
        page.wait_for_function("!MagicPlayer.state().loading")
        assert not page.evaluate("MagicPlayer.state().ready")
        assert page.evaluate("window.decodeCalls") == 0
        assert page.locator("#play").is_disabled()


def test_mp3_payload_must_match_declared_codec_frames_and_bitrate(page, study_files, tmp_path):
    page.evaluate("""() => {
      const Audio = window.AudioContext || window.webkitAudioContext;
      window.decodeCalls = 0;
      Audio.prototype.decodeAudioData = async () => {
        window.decodeCalls++; throw Error('Unexpected decode');
      };
    }""")
    with zipfile.ZipFile(study_files["pdf"]) as source:
        original = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(original["manifest.json"])
    audio_path = manifest["audio"]["path"]
    mp3 = original[audio_path]
    with zipfile.ZipFile(study_files["wav_pdf"]) as source:
        wav = source.read("audio/instrumental.wav")
    offsets, at = [], 0
    while at < len(mp3):
        offsets.append(at)
        header = int.from_bytes(mp3[at:at + 4], "big")
        at += 144000 * 256 // 44100 + ((header >> 9) & 1)
    middle = bytearray(mp3)
    middle[offsets[len(offsets) // 2]] = 0
    no_gapless = bytearray(mp3)
    no_gapless[156:160] = b"xxxx"
    cases = [
        ("wav-disguised-as-mp3", wav, {}),
        ("false-short-duration", mp3, {"frames": 44100, "duration": 1}),
        ("false-low-bitrate", mp3, {"bitrate_kbps": 128}),
        ("broken-middle-header", middle, {}),
        ("truncated", mp3[:-1], {}),
        ("extra-bytes", mp3 + b"\0", {}),
        ("missing-gapless-metadata", no_gapless, {}),
    ]
    for name, payload, change in cases:
        changed = json.loads(original["manifest.json"])
        changed["audio"].update(change)
        changed["entries"][audio_path] = {
            "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
        }
        broken = tmp_path / f"{name}.magic"
        with zipfile.ZipFile(broken, "w") as target:
            for entry, data in original.items():
                if entry == "manifest.json":
                    data = json.dumps(changed)
                elif entry == audio_path:
                    data = payload
                target.writestr(entry, data)
        page.locator("#file").set_input_files(broken)
        page.wait_for_function("!MagicPlayer.state().loading")
        assert not page.evaluate("MagicPlayer.state().ready"), name
        assert page.evaluate("window.decodeCalls") == 0, name
        assert "MP3 반주 정보" in page.locator("#status").inner_text(), name


def test_mp3_decode_failure_or_timing_change_offers_wav_recovery(page, study_files):
    page.evaluate("""() => {
      const Audio = window.AudioContext || window.webkitAudioContext;
      window.originalDecode = Audio.prototype.decodeAudioData;
    }""")
    for mode in ("failure", "frames", "rate", "channels"):
        page.evaluate("""mode => {
          const Audio = window.AudioContext || window.webkitAudioContext;
          Audio.prototype.decodeAudioData = async function () {
            if (mode === 'failure') throw Error('Unsupported native decoder');
            return this.createBuffer(mode === 'channels' ? 1 : 2,
              16 * 44100 + (mode === 'frames' ? 1 : 0), mode === 'rate' ? 48000 : 44100);
          };
        }""", mode)
        page.locator("#file").set_input_files(study_files["pdf"])
        page.wait_for_function("!MagicPlayer.state().loading")
        assert not page.evaluate("MagicPlayer.state().ready")
        assert "WAV로 저장" in page.locator("#status").inner_text()
        assert page.locator("#play").is_disabled()
    page.evaluate("""() => {
      const Audio = window.AudioContext || window.webkitAudioContext;
      Audio.prototype.decodeAudioData = window.originalDecode;
    }""")
    open_study(page, study_files["wav_pdf"])
    assert page.locator("#play").is_enabled()


def test_phone_file_and_score_navigation(page, study_files):
    page.set_viewport_size({"width": 360, "height": 780})
    file_button = page.locator("#soloFile .file-button")
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
    page.wait_for_function("MagicPlayer.state().playing")
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
    page.wait_for_function("MagicPlayer.state().position > .2")


def test_corrupt_packages_rejected_without_executing_text(page, study_files, tmp_path):
    source = study_files["gp"]
    open_study(page, source)
    corrupted = bytearray(source.read_bytes())
    with zipfile.ZipFile(source) as z:
        entry = z.getinfo(json.loads(z.read("manifest.json"))["audio"]["path"])
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
