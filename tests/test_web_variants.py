# SPDX-License-Identifier: GPL-3.0-only
"""Real browser multi-audio selection and Session cancellation on one media clock."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import zipfile
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from practice_player.magic_bundle import AUDIO_VARIANT_SPECS, export_magic
from practice_player.score_analysis import analyze_score
from practice_player.score_layout import analyze_scroll
from practice_player.score_view import view_profile
from tests import test_session_player as session_tests
from tests import test_web_player as player_tests
from tests.test_magic_bundle import portable_study
from tests.test_magic_variants import all_variant_study, variant_study
from tests.test_web_audio import attach_music_probe, hold_resume_completion
from tests.test_web_player import frequency, open_study

page = player_tests.fresh_page
web_url = player_tests.web_url
relay_url = session_tests.relay_url

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_WEB_TESTS") != "1", reason="RUN_WEB_TESTS=1 and browser runtimes required"
)


@pytest.fixture(scope="module")
def variant_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("portable-variants")
    result = {}
    for kind in ("pdf", "gp"):
        request, _, _ = variant_study(root / kind, kind)
        for codec in ("mp3", "wav"):
            result[f"{kind}_{codec}"] = export_magic({
                **request, "audio_format": codec, "output": str(root / f"{kind}-{codec}.magic"),
            })
        if kind == "pdf":
            result["single_guitar"] = export_magic({
                **request, "audio_format": "wav", "audio_variants": request["audio_variants"][1:],
                "default_audio_variant": "guitar", "output": str(root / "single-guitar.magic"),
            })
    return result


@pytest.fixture(scope="module")
def combined_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("portable-combinations")
    result = {}
    for kind in ("pdf", "gp"):
        directory = root / kind
        request, prepared, paths = portable_study(directory, kind)
        frames = sf.info(request["audio"]).frames
        times = np.arange(frames, dtype=np.float64) / 44100
        tones = [.12 * np.sin(2 * np.pi * frequency * times) for frequency in (220, 440, 660)]
        source = Path(request["audio"])
        mixed = sum(tones)
        sf.write(source, np.column_stack([mixed, mixed]), 44100, subtype="FLOAT")
        display = ({key: prepared[key] for key in ("pdf", "layout", "source", "signature")}
                   if kind == "gp" else None)
        timeline = analyze_score(
            source, Path(request["pdf"]), paths, musicxml=Path(prepared["source"]),
            gp_track=2, gp_display=display, mode="tempo", bpm=120,
        )
        request["timeline"] = str(analyze_scroll(
            source, Path(request["pdf"]), paths, timeline, view_profile("width", 900, 700),
        ))
        variants = []
        for identifier, label, targets, sound in (
            ("no_vocals", "보컬 없는 반주", ["vocals"], tones[1] + tones[2]),
            ("no_guitar", "기타 없는 반주", ["guitar"], tones[0] + tones[2]),
            ("no_vocals_guitar", "보컬 + 기타 없는 반주", ["vocals", "guitar"], tones[2]),
        ):
            path = directory / f"{identifier}.wav"
            sf.write(path, np.column_stack([sound, sound]), 44100, subtype="FLOAT")
            variant = {"id": identifier, "label": label, "mode": "without",
                       "target": targets[0] if len(targets) == 1 else None, "path": str(path)}
            if len(targets) > 1:
                variant["targets"] = targets
            variants.append(variant)
        request.update(audio_variants=variants, default_audio_variant="no_vocals_guitar")
        for codec in ("mp3", "wav"):
            result[f"{kind}_{codec}"] = export_magic({
                **request, "audio_format": codec, "output": str(root / f"{kind}-{codec}.magic"),
            })
        if kind == "pdf":
            result["legacy_individuals"] = export_magic({
                **request, "audio_variants": variants[:2], "default_audio_variant": "no_vocals",
                "audio_format": "wav", "output": str(root / "individuals.magic"),
            })
    return result


def capture_options(page, name):
    directory = os.environ.get("MAGIC_RENDER_CHECK_DIR")
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        engine = page.context.browser.browser_type.name
        page.screenshot(path=str(target / f"web-options-{engine}-{name}.png"), full_page=True)


@pytest.fixture(scope="module")
def all_options_file(tmp_path_factory):
    request, _, _, frequencies = all_variant_study(tmp_path_factory.mktemp("all-options"))
    return export_magic(request), frequencies


def watch_decoding(page):
    page.evaluate("""() => {
      const Audio = window.AudioContext || window.webkitAudioContext;
      const decode = Audio.prototype.decodeAudioData;
      window.decodeCalls = 0;
      Audio.prototype.decodeAudioData = function (...args) {
        window.decodeCalls++; return decode.apply(this, args);
      };
    }""")


def select_variant(page, identifier):
    page.locator("#audioVariant").select_option(identifier)
    page.wait_for_function("!MagicPlayer.state().loading", timeout=45000)
    assert page.evaluate("MagicPlayer.state().audioVariant") == identifier, (
        page.locator("#status").inner_text()
    )


def assert_tone(page, target):
    page.wait_for_function("""target => {
      const data = new Float32Array(testAnalyser.frequencyBinCount);
      testAnalyser.getFloatFrequencyData(data);
      let peak = 1;
      for (let i = 2; i < data.length; i++) if (data[i] > data[peak]) peak = i;
      const frequency = peak * MagicPlayer.context().sampleRate / testAnalyser.fftSize;
      return data[peak] > -45 && Math.abs(frequency - target) < 4;
    }""", arg=target, timeout=10000)
    observed = frequency(page)
    assert observed["level"] > -45
    assert observed["frequency"] == pytest.approx(target, abs=4)


@pytest.mark.parametrize("kind,codec", [("pdf", "mp3"), ("pdf", "wav"),
                                        ("gp", "mp3"), ("gp", "wav")])
def test_select_audio_lazily_preserves_score_clock_and_settings(page, variant_files, kind, codec):
    watch_decoding(page)
    open_study(page, variant_files[f"{kind}_{codec}"])
    assert page.evaluate("MagicPlayer.archive().manifest.version") == 4
    assert page.evaluate("window.decodeCalls") == 1
    assert page.locator("#audioChoice").is_visible()
    assert page.locator("#audioVariantCount").inner_text() == "파일에 저장된 음원 2개"
    assert page.locator("#audioVariant").is_enabled()
    assert "이 파일에 저장된 음원만" in page.locator("#audioVariantScope").inner_text()
    assert "audioVariantScope" in page.locator("#audioVariant").get_attribute("aria-describedby")
    assert page.locator("#audioVariant option").evaluate_all(
        "options => options.map(option => option.value)"
    ) == ["no_vocals", "guitar"]
    if kind == "gp":
        page.locator("#partSelect").select_option("1")
        page.wait_for_function("MagicPlayer.archive().activePart.track === 1 && "
                               "!document.getElementById('partSelect').disabled")
    page.locator("#key").select_option("2")
    page.locator("#speed").fill("150")
    page.locator("#loopStart").select_option("2")
    page.locator("#loopEnd").select_option("5")
    page.locator("#loop").check()
    page.locator("#highlightEnabled").uncheck()
    page.locator("#follow").uncheck()
    page.evaluate("MagicPlayer.seek(5, false)")
    attach_music_probe(page)
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > 5.3")
    assert_tone(page, 220 * 2 ** (2 / 12))
    page.evaluate("""() => {
      window.originalControls = ['play', 'audioVariant', 'seek']
        .map(id => document.getElementById(id));
      window.beforeHidePosition = MagicPlayer.state().position;
    }""")
    page.locator("#optionsToggle").click()
    assert page.locator("#optionsSidebar").is_hidden()
    assert page.locator("#optionsToggle").get_attribute("aria-expanded") == "false"
    assert page.locator("#focusControls #audioVariant").is_visible()
    assert page.locator("#focusControls #audioVariantCount").is_visible()
    assert page.locator("#focusControls #audioVariantScope").is_visible()
    assert page.locator("#focusControls #play").is_visible()
    assert page.evaluate("MagicPlayer.state().playing")
    assert page.evaluate("MagicPlayer.state().position >= beforeHidePosition")
    page.evaluate("""() => {
      document.getElementById('audioVariant').addEventListener('change', () => {
        window.variantPosition = MagicPlayer.state().position;
      }, {capture: true, once: true});
    }""")
    select_variant(page, "guitar")
    state = page.evaluate("MagicPlayer.state()")
    assert state["ready"] and not state["playing"]
    assert state["position"] == pytest.approx(page.evaluate("window.variantPosition"), abs=.04)
    assert page.evaluate("window.decodeCalls") == 2
    assert page.locator("#key").input_value() == "2"
    assert page.locator("#speed").input_value() == "150"
    assert page.locator("#loop").is_checked()
    assert page.locator("#loopStart").input_value() == "2"
    assert page.locator("#loopEnd").input_value() == "5"
    assert not page.locator("#highlightEnabled").is_checked()
    assert not page.locator("#follow").is_checked()
    if kind == "gp":
        assert page.evaluate("MagicPlayer.archive().activePart.track") == 1
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().playing")
    assert_tone(page, 440 * 2 ** (2 / 12))
    assert page.evaluate("window.decodeCalls") == 2
    page.evaluate("MagicPlayer.pause()")
    if kind == "gp" and codec == "mp3":
        capture_options(page, "desktop-collapsed")
    page.set_viewport_size({"width": 360, "height": 780})
    assert page.locator("#audioVariant").is_visible()
    assert page.locator("#play").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if kind == "gp" and codec == "mp3":
        capture_options(page, "phone-collapsed")
    page.locator('.practice-links a[href="#playerControls"]').click()
    assert page.locator("#optionsSidebar").is_visible()
    assert page.locator("#optionsToggle").get_attribute("aria-expanded") == "true"
    assert page.locator("#playerControls").bounding_box()["y"] < 5
    assert page.evaluate("""originalControls.every((control, index) =>
      control === document.getElementById(['play', 'audioVariant', 'seek'][index]))""")
    assert page.locator("#playerControls #play").is_visible()
    assert page.locator("#optionsSidebar #audioVariant").input_value() == "guitar"




@pytest.mark.parametrize("kind,codec", [("pdf", "mp3"), ("pdf", "wav"),
                                        ("gp", "mp3"), ("gp", "wav")])
def test_combined_removals_play_one_actual_mix_and_preserve_practice(
    page, combined_files, kind, codec,
):
    watch_decoding(page)
    open_study(page, combined_files[f"{kind}_{codec}"])
    assert page.evaluate("MagicPlayer.archive().manifest.version") == 5
    assert page.locator("#audioVariant").input_value() == "no_vocals_guitar"
    assert page.locator('#audioVariant option[value="no_vocals_guitar"]').inner_text() == (
        "보컬 + 기타 없는 반주"
    )
    assert page.locator("#audioVariantCount").inner_text() == "파일에 저장된 음원 3개"
    assert page.locator("#audioCombinationNotice").is_hidden()
    assert "보컬 · 기타" in page.locator("#audioVariantHint").inner_text()
    assert page.evaluate("window.decodeCalls") == 1

    def amplitudes():
        return page.evaluate("""() => {
          const data = decodedAudio.getChannelData(0), rate = decodedAudio.sampleRate;
          return [220, 440, 660].map(frequency => {
            let real = 0, imaginary = 0;
            for (let i = rate; i < 2 * rate; i++) {
              const angle = 2 * Math.PI * frequency * i / rate;
              real += data[i] * Math.cos(angle); imaginary += data[i] * Math.sin(angle);
            }
            return Math.hypot(real, imaginary) * 2 / rate;
          });
        }""")

    # Both removed parts must be absent; a renamed single-removal file would fail.
    observed = amplitudes()
    assert observed[0] < .003 and observed[1] < .003
    assert observed[2] == pytest.approx(.12, abs=.02)
    select_variant(page, "no_vocals")
    observed = amplitudes()
    assert observed[0] < .003
    assert observed[1:] == pytest.approx([.12, .12], abs=.02)
    page.locator("#key").select_option("2")
    page.locator("#speed").fill("125")
    page.locator("#loopStart").select_option("2")
    page.locator("#loopEnd").select_option("5")
    page.locator("#loop").check()
    if kind == "gp":
        page.locator("#partSelect").select_option("1")
        page.wait_for_function("MagicPlayer.archive().activePart.track === 1 && "
                               "!document.getElementById('partSelect').disabled")
    page.evaluate("MagicPlayer.seek(5, false)")
    page.locator("#optionsToggle").click()
    assert page.locator("#focusControls #audioVariant").is_visible()
    select_variant(page, "no_vocals_guitar")
    assert not page.evaluate("MagicPlayer.state().playing")
    assert page.evaluate("MagicPlayer.state().position") == pytest.approx(5, abs=.04)
    assert page.locator("#key").input_value() == "2"
    assert page.locator("#speed").input_value() == "125"
    assert page.locator("#loop").is_checked()
    assert page.locator("#loopStart").input_value() == "2"
    assert page.locator("#loopEnd").input_value() == "5"
    if kind == "gp":
        assert page.evaluate("MagicPlayer.archive().activePart.track") == 1
    assert page.evaluate("window.decodeCalls") == 3
    attach_music_probe(page)
    page.locator("#play").click()
    assert_tone(page, 660 * 2 ** (2 / 12))
    page.evaluate("MagicPlayer.pause()")
    page.set_viewport_size({"width": 360, "height": 780})
    assert page.locator("#audioVariant").is_visible()
    assert page.locator("#play").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if kind == "gp" and codec == "mp3":
        capture_options(page, "combined-phone")


def test_legacy_individual_removals_offer_reexport_guidance_without_fabricating_mix(
    page, combined_files,
):
    watch_decoding(page)
    open_study(page, combined_files["legacy_individuals"])
    assert page.evaluate("MagicPlayer.archive().manifest.version") == 4
    assert page.locator("#audioVariant option").evaluate_all(
        "options => options.map(option => option.value)"
    ) == ["no_vocals", "no_guitar"]
    assert page.locator("#audioCombinationNotice").is_visible()
    assert "다시 저장" in page.locator("#audioCombinationNotice").inner_text()
    assert page.evaluate("window.decodeCalls") == 1
    select_variant(page, "no_guitar")
    assert page.evaluate("window.decodeCalls") == 2


def test_corrupt_combination_keeps_previous_individual_audio(page, combined_files, tmp_path):
    source = combined_files["pdf_wav"]
    with zipfile.ZipFile(source) as archive:
        name = "audio/variants/no_vocals_guitar.wav"
        damaged = bytearray(archive.read(name))
    damaged[0:4] = b"BAD!"
    target = tmp_path / "bad-combination.magic"

    def choose_individual(manifest):
        manifest["default_audio_variant"] = "no_vocals"
        manifest["audio"] = deepcopy(manifest["audio_variants"][0]["audio"])

    rewrite_bundle(source, target, choose_individual, (name, bytes(damaged)))
    open_study(page, target)
    page.evaluate("MagicPlayer.seek(5, false)")
    page.locator("#audioVariant").select_option("no_vocals_guitar")
    page.wait_for_function("!MagicPlayer.state().loading")
    assert page.evaluate("MagicPlayer.state().ready")
    assert page.locator("#audioVariant").input_value() == "no_vocals"
    assert page.evaluate("MagicPlayer.state().position") == pytest.approx(5, abs=.04)
    assert not page.locator("#play").is_disabled()
    assert "WAV" in page.locator("#status").inner_text()


def run_archive_reader(files):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the standalone archive-reader check")
    script = r"""
      const fs = require('node:fs');
      const source = fs.readFileSync(process.argv[1], 'utf8');
      const {MagicArchive, validateWav, validateMp3} = new Function(
        source + ';return {MagicArchive, validateWav, validateMp3};')();
      (async () => {
        const results = [];
        for (const path of process.argv.slice(2)) {
          try {
            const archive = await new MagicArchive(new Blob([fs.readFileSync(path)])).open();
            for (const variant of archive.audioVariants) {
              const data = await (await archive.blob(variant.audio.path)).arrayBuffer();
              const hashed = await crypto.subtle.digest('SHA-256', data);
              const digest = Buffer.from(hashed).toString('hex');
              if (digest !== archive.manifest.entries[variant.audio.path].sha256) {
                throw Error('hash');
              }
              (variant.audio.codec === 'mp3' ? validateMp3 : validateWav)(data, variant.audio);
            }
            results.push({ok: true, version: archive.manifest.version,
              ids: archive.audioVariants.map(v => v.id), active: archive.activeAudioVariant.id,
              targets: archive.activeAudioVariant.targets, parts: archive.parts.length});
          } catch (error) { results.push({ok: false, error: error.message}); }
        }
        process.stdout.write(JSON.stringify(results));
      })().catch(error => { process.stderr.write(error.stack); process.exitCode = 1; });
    """
    result = subprocess.run(
        [node, "-e", script, str(Path(__file__).resolve().parents[1] / "web_player/archive.js"),
         *map(str, files)], capture_output=True, text=True, check=True, timeout=45,
    )
    return json.loads(result.stdout)


def test_combined_archive_reader_checks_real_export_payloads(combined_files):
    keys = ["pdf_wav", "pdf_mp3", "gp_wav", "gp_mp3", "legacy_individuals"]
    results = run_archive_reader([combined_files[key] for key in keys])
    assert all(item["ok"] for item in results), results
    for kind, item in zip(keys[:4], results[:4]):
        assert item["version"] == 5
        assert item["ids"] == ["no_vocals", "no_guitar", "no_vocals_guitar"]
        assert item["active"] == "no_vocals_guitar"
        assert item["targets"] == ["vocals", "guitar"]
        assert item["parts"] == (2 if kind.startswith("gp") else 1)
        with zipfile.ZipFile(combined_files[kind]) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            for variant, expected in zip(manifest["audio_variants"],
                                         ([0, .12, .12], [.12, 0, .12], [0, 0, .12])):
                samples, rate = sf.read(io.BytesIO(archive.read(variant["audio"]["path"])))
                spectrum = np.abs(np.fft.rfft(samples[rate:2 * rate, 0])) * 2 / rate
                assert spectrum[[220, 440, 660]] == pytest.approx(expected, abs=.003)
    assert results[-1]["version"] == 4
    assert results[-1]["ids"] == ["no_vocals", "no_guitar"]


def test_combined_archive_reader_rejects_ambiguous_or_inconsistent_targets(
    combined_files, tmp_path,
):
    def combined(manifest):
        return manifest["audio_variants"][2]

    changes = [
        lambda m: combined(m).pop("targets"),
        lambda m: combined(m).update(targets=["guitar", "vocals"]),
        lambda m: combined(m).update(targets=["vocals", "vocals"]),
        lambda m: combined(m).update(targets=["vocals", "unknown"]),
        lambda m: combined(m).update(targets=["vocals"]),
        lambda m: combined(m).update(target="vocals"),
        lambda m: combined(m).update(mode="stem"),
        lambda m: combined(m).update(id="no_guitar_vocals"),
        lambda m: combined(m).update(id="no_vocals_vocals"),
        lambda m: m["audio_variants"][0].update(targets=["vocals", "guitar"]),
        lambda m: combined(m)["audio"].update(source_sha256="0" * 64),
        lambda m: combined(m)["audio"].update(pitch_semitones=1),
        lambda m: m.update(version=4),
    ]
    files = []
    for index, change in enumerate(changes):
        path = tmp_path / f"invalid-combination-{index}.magic"
        rewrite_bundle(combined_files["pdf_wav"], path, change)
        files.append(path)
    path = tmp_path / "false-combination-format.magic"
    rewrite_bundle(combined_files["legacy_individuals"], path, lambda m: m.update(version=5))
    files.append(path)
    results = run_archive_reader(files)
    assert len(results) == len(files)
    assert all(not item["ok"] for item in results), results


def test_single_available_audio_and_options_preference_survive_reload_and_blocked_storage(
    page, variant_files,
):
    open_study(page, variant_files["single_guitar"])
    assert page.locator("#audioChoice").is_visible()
    assert page.locator("#audioVariantCount").inner_text() == "파일에 저장된 음원 1개"
    assert page.locator("#audioVariant option").count() == 1
    assert page.locator("#audioVariant").input_value() == "guitar"
    assert page.locator("#audioVariant").is_disabled()
    assert page.locator("#play").is_enabled()
    assert "저장된 음원이 1개" in page.locator("#audioVariantScope").inner_text()
    assert "바로 재생" in page.locator("#audioVariantScope").inner_text()
    page.locator("#audioChoiceHelp summary").click()
    assert "체크한 음원 준비" in page.locator("#audioChoiceHelp p").inner_text()
    assert ".magic 저장" in page.locator("#audioChoiceHelp p").inner_text()
    page.locator("#optionsToggle").focus()
    page.locator("#optionsToggle").press("Enter")
    assert page.locator("#optionsSidebar").is_hidden()
    assert page.locator("#optionsToggle").evaluate("el => el === document.activeElement")
    # Complete the service-worker request before replacing its document in WebKit.
    player_tests.wait_for_service_worker(page)
    page.reload()
    page.wait_for_function("typeof MagicPlayer !== 'undefined'")
    assert page.locator("#optionsSidebar").is_hidden()
    assert page.locator("#optionsToggle").inner_text() == "옵션 펼치기"
    assert page.locator("#focusControls #play").is_disabled()
    assert page.locator("#audioChoice").is_hidden()
    page.locator("#optionsToggle").click()
    assert page.locator("#soloFile .file-button").is_visible()
    page.wait_for_function("!document.getElementById('checkUpdate').disabled")
    page.add_init_script("""
      for (const method of ['getItem', 'setItem']) {
        Storage.prototype[method] = () => {
          throw new DOMException('Storage blocked', 'SecurityError');
        };
      }
    """)
    page.reload()
    page.wait_for_function("typeof MagicPlayer !== 'undefined'")
    open_study(page, variant_files["single_guitar"])
    page.locator("#optionsToggle").click()
    assert page.locator("#optionsSidebar").is_hidden()
    assert page.locator("#focusControls #audioVariant").input_value() == "guitar"
    assert page.locator("#focusControls #audioChoiceHelp summary").is_visible()
    page.locator("#optionsToggle").click()
    assert page.locator("#optionsSidebar").is_visible()
    assert page.locator("#optionsSidebar #audioVariant").input_value() == "guitar"
    assert page.locator("#audioVariant").is_disabled()


def test_audio_choices_follow_current_file_and_pending_selection(page, variant_files, tmp_path):
    choices = page.locator("#audioVariant option")
    assert page.locator("#audioChoice").is_hidden()
    assert page.locator("#audioVariant").is_disabled()
    assert choices.count() == 0
    watch_decoding(page)
    open_study(page, variant_files["pdf_wav"])
    assert choices.evaluate_all("items => items.map(item => item.value)") == [
        "no_vocals", "guitar",
    ]
    # An unprepared target cannot become a playable option or trigger decoding.
    rejected = page.evaluate("""async () => {
      try { await MagicPlayer.selectAudioVariant('no_guitar'); return false; }
      catch (_) { return true; }
    }""")
    assert rejected
    assert page.evaluate("window.decodeCalls") == 1
    assert page.evaluate("MagicPlayer.state().audioVariant") == "no_vocals"
    page.evaluate("""() => {
      MagicPlayer.seek(5, false);
      const saved = MagicPlayer.archive(), read = saved.blob.bind(saved);
      saved.blob = async path => {
        if (path === 'audio/variants/guitar.wav') {
          await new Promise(resolve => { window.releaseVariantRead = resolve; });
        }
        return read(path);
      };
    }""")
    page.locator("#audioVariant").select_option("guitar")
    page.wait_for_function("typeof window.releaseVariantRead === 'function'")
    assert page.locator("#audioVariant").is_disabled()
    assert page.locator("#audioChoice").get_attribute("aria-busy") == "true"
    assert "준비 중" in page.locator("#audioVariantScope").inner_text()
    assert choices.count() == 2
    assert page.evaluate("MagicPlayer.state().audioVariant") == "no_vocals"
    assert page.evaluate("window.decodeCalls") == 1
    page.evaluate("window.releaseVariantRead()")
    page.wait_for_function("!MagicPlayer.state().loading")
    assert page.locator("#audioVariant").is_enabled()
    assert page.locator("#audioChoice").get_attribute("aria-busy") == "false"
    assert "이 파일에 저장된 음원만" in page.locator("#audioVariantScope").inner_text()
    assert page.evaluate("MagicPlayer.state().audioVariant") == "guitar"
    assert page.evaluate("MagicPlayer.state().position") == 5
    assert not page.evaluate("MagicPlayer.state().playing")
    assert page.evaluate("window.decodeCalls") == 2

    open_study(page, variant_files["single_guitar"])
    assert choices.evaluate_all("items => items.map(item => item.value)") == ["guitar"]
    assert page.locator("#audioVariant").is_disabled()
    assert page.locator("#play").is_enabled()
    broken = tmp_path / "unavailable.magic"
    broken.write_bytes(b"not a practice archive")
    page.locator("#file").set_input_files(broken)
    page.wait_for_function("!MagicPlayer.state().loading")
    assert page.locator("#audioChoice").is_hidden()
    assert page.locator("#audioVariant").is_disabled()
    assert choices.count() == 0
    assert page.locator("#audioVariantCount").inner_text() == ""
    assert page.locator("#audioVariantScope").inner_text() == ""
    assert page.locator("#audioVariantHint").inner_text() == ""
    open_study(page, variant_files["pdf_wav"])
    assert choices.count() == 2
    assert page.locator("#audioVariant").is_enabled()


def test_fifteen_options_keep_piano_keys_and_synth_distinct_and_decode_lazily(
    page, all_options_file,
):
    archive, frequencies = all_options_file
    watch_decoding(page)
    open_study(page, archive)
    assert page.locator("#audioVariant option").count() == 15
    assert page.evaluate("window.decodeCalls") == 1
    assert page.evaluate("MagicPlayer.state().audioVariant") == "keys"
    assert page.locator('#audioVariant option[value="keys"]').inner_text() == "키보드만 듣기"
    assert page.locator('#audioVariant option[value="piano"]').inner_text() == "피아노만 듣기"
    assert page.locator('#audioVariant option[value="synth"]').inner_text() == "신시사이저만 듣기"
    page.evaluate("MagicPlayer.seek(5, false)")
    attach_music_probe(page)
    page.locator("#play").click()
    assert_tone(page, frequencies["keys"])
    page.evaluate("MagicPlayer.pause()")
    position = page.evaluate("MagicPlayer.state().position")
    for decoded, identifier in enumerate(("no_keys", "piano", "no_piano", "synth", "no_synth"), 2):
        select_variant(page, identifier)
        assert page.evaluate("window.decodeCalls") == decoded
        assert page.evaluate("MagicPlayer.state().position") == pytest.approx(position, abs=.04)
        assert not page.evaluate("MagicPlayer.state().playing")
        assert page.evaluate("MagicPlayer.archive().manifest.default_audio_variant") == "keys"
        assert page.evaluate("MagicPlayer.archive().manifest.audio.path") == (
            "audio/variants/keys.mp3"
        )
    page.locator("#play").click()
    assert_tone(page, frequencies["no_synth"])
    page.evaluate("MagicPlayer.pause()")
    page.set_viewport_size({"width": 360, "height": 780})
    assert page.locator("#audioVariant").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def rewrite_bundle(source, destination, change, payload=None):
    with zipfile.ZipFile(source) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(entries["manifest.json"])
    change(manifest)
    if payload:
        name, value = payload
        entries[name] = value
        manifest["entries"][name] = {
            "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest(),
        }
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def test_recorded_common_gain_is_not_applied_to_embedded_samples_again(
    page, variant_files, tmp_path,
):
    destination = tmp_path / "recorded-gain.magic"
    rewrite_bundle(variant_files["pdf_wav"], destination, lambda m: m.update(audio_gain=.4))
    open_study(page, destination)

    def peak():
        return page.evaluate("""() => decodedAudio.getChannelData(0).reduce(
          (largest, sample) => Math.max(largest, Math.abs(sample)), 0)
        """)

    assert page.evaluate("MagicPlayer.archive().manifest.audio_gain") == .4
    assert peak() == pytest.approx(.16, abs=1 / 32768)
    select_variant(page, "guitar")
    assert peak() == pytest.approx(.12, abs=1 / 32768)


def test_variant_inventory_and_clock_mismatch_reject_before_decode(page, variant_files, tmp_path):
    watch_decoding(page)

    def duplicate(manifest):
        manifest["audio_variants"].append(deepcopy(manifest["audio_variants"][0]))

    def different_frames(manifest):
        audio = manifest["audio_variants"][1]["audio"]
        audio["frames"] += 1
        audio["duration"] = audio["frames"] / 44100

    cases = [
        lambda m: m.update(default_audio_variant="no_bass"), duplicate, different_frames,
        lambda m: m["audio_variants"][1]["audio"].update(source_sha256="0" * 64),
        lambda m: m["audio_variants"][1]["audio"].update(pitch_semitones=2),
        lambda m: m["audio_variants"][1].update(mode="without"),
        lambda m: m["audio_variants"][1].update(target="bass"),
        lambda m: m["audio_variants"][1]["audio"].update(path="audio/instrumental.mp3"),
        lambda m: m["audio"].update(bitrate_kbps=128),
        lambda m: m["audio_variants"].extend(
            deepcopy(m["audio_variants"][0]) for _ in range(len(AUDIO_VARIANT_SPECS))
        ),
        lambda m: m["audio_variants"][1]["audio"].update(
            frames=512 * 1024 * 1024, duration=512 * 1024 * 1024 / 44100,
        ),
        lambda m: m.pop("audio_gain"),
        lambda m: m.update(audio_gain=0),
        lambda m: m.update(audio_gain=1.01),
        lambda m: m.update(audio_gain=True),
    ]
    for index, change in enumerate(cases):
        broken = tmp_path / f"invalid-variant-{index}.magic"
        rewrite_bundle(variant_files["pdf_mp3"], broken, change)
        page.locator("#file").set_input_files(broken)
        page.wait_for_function("!MagicPlayer.state().loading")
        assert not page.evaluate("MagicPlayer.state().ready"), index
        assert page.evaluate("window.decodeCalls") == 0
        assert page.locator("#play").is_disabled()


@pytest.mark.parametrize("codec", ["mp3", "wav"])
def test_corrupt_unselected_audio_is_lazy_and_keeps_working_version(
    page, variant_files, tmp_path, codec,
):
    name = f"audio/variants/guitar.{codec}"
    with zipfile.ZipFile(variant_files[f"pdf_{codec}"]) as archive:
        payload = bytearray(archive.read(name))
    payload[0] ^= 1
    broken = tmp_path / "bad-secondary.magic"
    rewrite_bundle(variant_files[f"pdf_{codec}"], broken, lambda m: None,
                   payload=(name, bytes(payload)))
    watch_decoding(page)
    open_study(page, broken)
    page.locator("#optionsToggle").click()
    assert page.locator("#optionsSidebar").is_hidden()
    assert page.evaluate("window.decodeCalls") == 1
    page.evaluate("MagicPlayer.seek(5, false)")
    page.locator("#audioVariant").select_option("guitar")
    page.wait_for_function("!MagicPlayer.state().loading")
    assert page.evaluate("window.decodeCalls") == 1
    state = page.evaluate("MagicPlayer.state()")
    assert state["ready"] and state["audioVariant"] == "no_vocals"
    assert state["position"] == 5 and not state["playing"]
    assert page.locator("#audioVariant").input_value() == "no_vocals"
    assert page.locator("#status").get_attribute("class") == "file-error"
    assert page.locator("#focusControls #status").is_visible()
    attach_music_probe(page)
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > 5.3")
    assert_tone(page, 220)


def test_native_decode_failure_retains_previous_worklet_and_allows_retry(page, variant_files):
    open_study(page, variant_files["pdf_mp3"])
    attach_music_probe(page)
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .3")
    assert_tone(page, 220)
    page.evaluate("""() => {
      const Audio = window.AudioContext || window.webkitAudioContext;
      window.savedDecoder = Audio.prototype.decodeAudioData;
      window.previousNode = stretch;
      Audio.prototype.decodeAudioData = async () => { throw Error('Synthetic decoder failure'); };
    }""")
    page.locator("#audioVariant").select_option("guitar")
    page.wait_for_function("!MagicPlayer.state().loading")
    assert page.evaluate("MagicPlayer.state().ready && stretch === previousNode")
    assert page.evaluate("MagicPlayer.state().audioVariant") == "no_vocals"
    assert not page.evaluate("MagicPlayer.state().playing")
    page.evaluate("""() => {
      const Audio = window.AudioContext || window.webkitAudioContext;
      Audio.prototype.decodeAudioData = window.savedDecoder;
    }""")
    select_variant(page, "guitar")
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().playing")
    assert_tone(page, 440)


def test_late_audio_preparation_cannot_attach_previous_variant(page, variant_files):
    open_study(page, variant_files["pdf_wav"])
    page.evaluate("""() => {
      const original = SignalsmithStretch;
      window.releasePreviousLatency = null;
      SignalsmithStretch = async (...args) => {
        SignalsmithStretch = original;
        const node = await original(...args), value = await node.latency();
        node.latency = () => {
          window.previousLatencyCompletion = new Promise(resolve => {
            window.releasePreviousLatency = () => resolve(value);
          });
          return window.previousLatencyCompletion;
        };
        return node;
      };
    }""")
    page.locator("#play").click()
    page.wait_for_function("window.releasePreviousLatency !== null")
    page.evaluate("window.previousTask = audioTask")
    select_variant(page, "guitar")
    assert page.evaluate("previousTask.cancelled && stretch === null && !!decodedAudio")
    page.evaluate("""async () => {
      window.releasePreviousLatency();
      await window.previousLatencyCompletion;
    }""")
    assert page.evaluate("stretch === null && !!decodedAudio")
    assert not page.evaluate("MagicPlayer.state().playing")
    attach_music_probe(page)
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .3")
    assert_tone(page, 440)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_session_shared_variants_keep_identity_and_require_fresh_ready(
    engine, web_url, relay_url, variant_files,  # noqa: F811
):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch()
        leader, member = [browser.new_page() for _ in range(2)]
        errors = []
        for participant in (leader, member):
            participant.on("pageerror", lambda error: errors.append(str(error)))
            participant.goto(web_url + "#ensemble")
            participant.locator("#relayURL").evaluate("(e,v) => e.value=v", relay_url)
        open_study(leader, variant_files["gp_mp3"])
        leader.locator("#leaderMode").click()
        leader.wait_for_function("MagicEnsemble.state().role === 'leader'")
        leader.locator("#shareFile").click()
        leader.wait_for_function("ensemble.shared", timeout=45000)
        member.locator("#inviteCode").fill(leader.evaluate("MagicEnsemble.state().code"))
        member.locator("#memberMode").click()
        member.wait_for_function("!document.getElementById('getShared').disabled")
        with session_tests.readiness_diagnostics(
            (("leader", leader), ("member", member)), engine, errors, "shared-variants-file",
        ) as diagnostic:
            member.locator("#getShared").click()
            diagnostic["waiting_for"] = "member file ready"
            member.wait_for_function("MagicPlayer.state().ready && !MagicPlayer.state().loading")
        assert member.locator("#audioChoice").is_visible()
        assert member.locator("#playerControls").is_hidden()
        member.locator("#partSelect").select_option("1")
        member.wait_for_function("MagicPlayer.archive().activePart.track === 1 && "
                                 "!document.getElementById('partSelect').disabled")
        for participant in (leader, member):
            participant.locator("#optionsToggle").click()
            assert participant.locator("#optionsSidebar").is_hidden()
            assert participant.locator("#focusControls #audioVariant").is_visible()
        assert member.locator("#play").is_hidden()
        assert leader.locator("#play").is_visible()
        member.locator("#sessionRoom").click()
        assert member.locator("#optionsSidebar").is_visible()
        assert member.locator("#ensemblePanel").is_visible()
        member.locator("#optionsToggle").click()
        hold_resume_completion(member, "#sessionPrepare")
        member.locator("#sessionPrepare").click()
        member.wait_for_function("window.releaseResume !== null")
        select_variant(member, "guitar")
        member.evaluate("async () => { releaseResume(); await heldAudioAction; }")
        assert not member.evaluate("MagicEnsemble.state().prepared || ensemble.wantsReady")
        assert not member.evaluate("MagicPlayer.state().playing")
        assert leader.evaluate("ensemble.media()") == member.evaluate("ensemble.media()")
        assert leader.evaluate("MagicPlayer.state().audioVariant") == "no_vocals"
        for participant in (leader, member):
            attach_music_probe(participant)
            participant.locator("#sessionPrepare").click()
            participant.wait_for_function("MagicEnsemble.state().prepared")
        leader.wait_for_function("!document.getElementById('play').disabled")
        leader.locator("#play").click()
        for participant in (leader, member):
            participant.wait_for_function("MagicPlayer.state().position > .3", timeout=10000)
        assert_tone(leader, 220)
        assert_tone(member, 440)
        select_variant(member, "no_vocals")
        leader.wait_for_function("!MagicPlayer.state().playing")
        assert not member.evaluate("MagicEnsemble.state().prepared")
        assert leader.evaluate("MagicEnsemble.state().prepared")
        assert member.evaluate("MagicPlayer.archive().activePart.track") == 1
        assert member.locator("#sessionPrepare").get_attribute("aria-pressed") == "false"
        assert leader.locator("#play").is_disabled()
        member.locator("#sessionPrepare").click()
        member.wait_for_function("MagicEnsemble.state().prepared")
        leader.wait_for_function("!document.getElementById('play').disabled")
        leader.locator("#play").click()
        for participant in (leader, member):
            participant.wait_for_function("MagicPlayer.state().playing", timeout=10000)
        assert_tone(member, 220)
        browser.close()
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_session_shares_combined_mix_and_requires_ready_after_switch(
    engine, web_url, relay_url, combined_files,  # noqa: F811
):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch()
        leader, member = [browser.new_page() for _ in range(2)]
        errors = []
        for participant in (leader, member):
            participant.on("pageerror", lambda error: errors.append(str(error)))
            participant.goto(web_url + "#ensemble")
            participant.locator("#relayURL").evaluate("(element, value) => element.value=value",
                                                     relay_url)
        open_study(leader, combined_files["gp_mp3"])
        leader.locator("#leaderMode").click()
        leader.wait_for_function("MagicEnsemble.state().role === 'leader'")
        assert not leader.locator("#shareFile").is_disabled()
        leader.locator("#shareFile").click()
        leader.wait_for_function("ensemble.shared", timeout=45000)
        member.locator("#inviteCode").fill(leader.evaluate("MagicEnsemble.state().code"))
        member.locator("#memberMode").click()
        member.wait_for_function("!document.getElementById('getShared').disabled")
        with session_tests.readiness_diagnostics(
            (("leader", leader), ("member", member)), engine, errors, "shared-combined-file",
        ) as diagnostic:
            member.locator("#getShared").click()
            diagnostic["waiting_for"] = "member file ready"
            member.wait_for_function("MagicPlayer.state().ready && !MagicPlayer.state().loading")
        assert member.locator("#audioVariant").input_value() == "no_vocals_guitar"
        assert member.evaluate("MagicPlayer.archive().parts.length") == 2
        select_variant(member, "no_guitar")
        for participant in (leader, member):
            participant.locator("#sessionPrepare").click()
            participant.wait_for_function("MagicEnsemble.state().prepared")
        select_variant(member, "no_vocals_guitar")
        assert not member.evaluate("MagicEnsemble.state().prepared || ensemble.wantsReady")
        assert leader.evaluate("MagicEnsemble.state().prepared")
        assert leader.evaluate("ensemble.media()") == member.evaluate("ensemble.media()")
        assert not member.evaluate("MagicPlayer.state().playing")
        assert member.evaluate("MagicPlayer.archive().manifest.default_audio_variant") == (
            "no_vocals_guitar"
        )
        browser.close()
        assert errors == []
