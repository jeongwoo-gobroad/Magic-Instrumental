# SPDX-License-Identifier: GPL-3.0-only
"""Browser audio-session and lifecycle regressions, not physical speaker checks.

Lifecycle events and unavailable output states are injected into real browser
contexts; successful recovery still uses native decoding and worklet output.
"""

import os

import pytest

from tests import test_web_player as player_tests
from tests.test_web_player import frequency, open_study

page = player_tests.page
study_files = player_tests.study_files
web_url = player_tests.web_url

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_WEB_TESTS") != "1", reason="RUN_WEB_TESTS=1 and browser runtimes required"
)


def install_audio_session_probe(page, mode="available"):
    page.evaluate("""mode => {
      let type = 'auto';
      window.audioSessionEvents = [];
      const session = {};
      Object.defineProperty(session, 'type', {
        get: () => type,
        set(value) {
          audioSessionEvents.push({operation: 'session', value});
          if (mode === 'throws') throw new Error('Audio session cannot be configured');
          type = value;
        }
      });
      Object.defineProperty(navigator, 'audioSession', {
        configurable: true, value: mode === 'absent' ? undefined : session
      });
      const Audio = window.AudioContext || window.webkitAudioContext;
      const resume = Audio.prototype.resume;
      Audio.prototype.resume = function (...args) {
        audioSessionEvents.push({operation: 'resume', type, state: this.state});
        return resume.apply(this, args);
      };
    }""", mode)


def assert_audio_session_order(page, mode="available"):
    events = page.evaluate("window.audioSessionEvents")
    resumes = [index for index, event in enumerate(events) if event["operation"] == "resume"]
    assert resumes, events
    if mode == "absent":
        assert len(resumes) == len(events), events
    else:
        for index in resumes:
            assert index > 0 and events[index - 1] == {
                "operation": "session", "value": "playback",
            }, events
            assert events[index]["type"] == ("auto" if mode == "throws" else "playback"), events


def attach_music_probe(page):
    page.evaluate("""() => {
      window.testAnalyser = MagicPlayer.context().createAnalyser();
      testAnalyser.fftSize = 16384;
      testAnalyser.smoothingTimeConstant = 0;
      musicGain.connect(testAnalyser);
    }""")


def background_page(page, event="visibilitychange"):
    page.evaluate("""event => {
      window.simulatedHidden = true;
      Object.defineProperty(document, 'hidden', {
        configurable: true, get: () => window.simulatedHidden
      });
      if (event === 'pagehide') window.dispatchEvent(new Event('pagehide'));
      else document.dispatchEvent(new Event('visibilitychange'));
    }""", event)


def foreground_page(page):
    page.evaluate("""() => {
      window.simulatedHidden = false;
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event('pageshow'));
    }""")


def hold_resume_completion(page, control):
    page.evaluate("""control => {
      const Audio = window.AudioContext || window.webkitAudioContext;
      const resume = Audio.prototype.resume;
      window.resumeBeforeGate = resume;
      window.releaseResume = null;
      let held = false;
      Audio.prototype.resume = function (...args) {
        const result = resume.apply(this, args);
        if (held) return result;
        held = true;
        return result.then(() => new Promise(resolve => { window.releaseResume = resolve; }));
      };
      const button = document.querySelector(control), action = button.onclick;
      button.onclick = function (event) {
        return window.heldAudioAction = Promise.resolve(action.call(this, event));
      };
    }""", control)


def release_resume_after_foreground(page):
    foreground_page(page)
    page.evaluate("""async () => {
      // Model the platform recovering its context before the old request settles.
      await window.resumeBeforeGate.call(MagicPlayer.context());
      window.releaseResume();
      await window.heldAudioAction;
    }""")


@pytest.mark.parametrize("mode", ["available", "absent", "throws"])
def test_audio_actions_declare_playback_and_resume_running_context(page, study_files, mode):
    install_audio_session_probe(page, mode)
    open_study(page, study_files["pdf"])
    assert page.evaluate("window.audioSessionEvents") == []
    attach_music_probe(page)

    page.locator("#metro").click()
    page.wait_for_function("MagicPlayer.state().metro")
    assert_audio_session_order(page, mode)
    page.locator("#metro").click()
    page.wait_for_function("!MagicPlayer.state().metro")
    assert page.evaluate("MagicPlayer.context().state") == "running"
    page.evaluate("window.audioSessionEvents = []")

    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .5")
    assert_audio_session_order(page, mode)
    first_resume = page.evaluate("audioSessionEvents.find(e => e.operation === 'resume')")
    assert first_resume["state"] == "running"
    actual = frequency(page)
    assert actual["level"] > -60 and actual["frequency"] == pytest.approx(220, abs=4)


def test_stopped_audio_clock_preserves_song_and_retries(page, study_files):
    open_study(page, study_files["gp"])
    page.locator("#partSelect").select_option("1")
    page.wait_for_function("MagicPlayer.archive().activePart.track === 1")
    page.locator("#speed").evaluate("""input => {
      input.value = '75';
      input.dispatchEvent(new Event('input', {bubbles: true}));
      input.dispatchEvent(new Event('change', {bubbles: true}));
    }""")
    page.locator("#key").select_option("2")
    page.locator("#volume").evaluate("""input => {
      input.value = '65';
      input.dispatchEvent(new Event('input', {bubbles: true}));
    }""")
    page.locator("#loopStart").select_option("2")
    page.locator("#loopEnd").select_option("3")
    page.locator("#loop").check()
    page.evaluate("MagicPlayer.seek(5.25, false)")
    attach_music_probe(page)
    page.evaluate("""() => {
      const context = MagicPlayer.context(), stoppedAt = context.currentTime;
      window.songBeforeClockFailure = MagicPlayer.archive();
      Object.defineProperty(context, 'currentTime', {configurable: true, get: () => stoppedAt});
    }""")
    page.locator("#play").click()
    page.wait_for_function("document.getElementById('status').classList.contains('file-error')",
                           timeout=10000)
    assert "소리 출력이 멈췄습니다" in page.locator("#status").inner_text()
    page.wait_for_function("MagicPlayer.context().state === 'suspended'")
    assert page.evaluate("MagicPlayer.state().ready && !MagicPlayer.state().loading")
    assert not page.evaluate("MagicPlayer.state().playing")
    assert page.evaluate("MagicPlayer.state().position") == 5.25
    assert page.evaluate("MagicPlayer.archive() === window.songBeforeClockFailure")
    assert page.locator("#partSelect").input_value() == "1"
    assert page.locator("#speed").input_value() == "75"
    assert page.locator("#key").input_value() == "2"
    assert page.locator("#volume").input_value() == "65"
    assert page.locator("#loop").is_checked()
    assert page.locator("#loopStart").input_value() == "2"
    assert page.locator("#loopEnd").input_value() == "3"
    assert page.locator("#file").is_enabled() and page.locator("#play").is_enabled()

    page.evaluate("delete MagicPlayer.context().currentTime")
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > 5.75")
    actual = frequency(page)
    assert actual["level"] > -60
    assert actual["frequency"] == pytest.approx(220 * 2 ** (2 / 12), abs=5)


@pytest.mark.parametrize("event", ["visibilitychange", "pagehide"])
def test_background_suspends_output_until_explicit_replay(page, study_files, event):
    open_study(page, study_files["pdf"])
    attach_music_probe(page)
    page.locator("#metro").click()
    page.wait_for_function("MagicPlayer.state().metro")
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .3")

    background_page(page, event)
    page.wait_for_function("MagicPlayer.context().state === 'suspended'")
    paused = page.evaluate("MagicPlayer.state().position")
    assert page.evaluate("""MagicPlayer.state().ready &&
        !MagicPlayer.state().playing && !MagicPlayer.state().metro""")
    foreground_page(page)
    page.wait_for_timeout(200)
    assert page.evaluate("MagicPlayer.context().state") == "suspended"
    assert page.evaluate("MagicPlayer.state().position") == paused
    assert not page.evaluate("MagicPlayer.state().playing || MagicPlayer.state().metro")

    page.locator("#play").click()
    page.wait_for_function("position => MagicPlayer.state().position > position + .5", arg=paused)
    actual = frequency(page)
    assert actual["level"] > -60 and actual["frequency"] == pytest.approx(220, abs=4)


def test_background_cancels_pending_metronome_activation(page, study_files):
    open_study(page, study_files["pdf"])
    hold_resume_completion(page, "#metro")
    page.locator("#metro").click()
    page.wait_for_function("window.releaseResume !== null")
    background_page(page)
    page.wait_for_function("MagicPlayer.context().state === 'suspended'")
    release_resume_after_foreground(page)
    assert not page.evaluate("MagicPlayer.state().metro || MagicPlayer.state().playing")
    assert page.evaluate("MagicPlayer.state().ready")

    page.locator("#metro").click()
    page.wait_for_function("MagicPlayer.state().metro")
