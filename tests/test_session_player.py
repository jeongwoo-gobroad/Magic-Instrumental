# SPDX-License-Identifier: GPL-3.0-only
"""Two actual browser participants, delayed commands and independent GP parts."""

import asyncio
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from aiohttp import web

from relay.server import Relay
from tests.test_web_audio import (
    assert_audio_session_order,
    background_page,
    hold_resume_completion,
    install_audio_session_probe,
    release_resume_after_foreground,
)
from tests.test_web_player import (  # noqa: F401
    choose_file,
    hold_audio_resume,
    open_study,
    study_files,
    wait_for_file_without_audio,
    web_url,
)

pytestmark = pytest.mark.skipif(os.environ.get("RUN_WEB_TESTS") != "1",
                                reason="RUN_WEB_TESTS=1 and browser runtimes required")


def install_readiness_trace(page, participant, shared_receive=False):
    page.evaluate("""({participant, sharedReceive}) => {
      const history = [], limit = 160;
      let discarded = 0, active = true;
      const sharedInput = sharedReceive ? {
        events: [], refreshes: [], calls: {download: 0, openShared: 0}
      } : null;
      const pointerEvents = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'];
      const targetName = target => target && ({id: target.id || '', tag: target.tagName || ''});
      const controlState = event => {
        const button = $('getShared'), rect = button.getBoundingClientRect();
        return {disabled: button.disabled, children: button.childNodes.length,
          rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
          hit: targetName(document.elementFromPoint(event.clientX, event.clientY))};
      };
      const pointerEvent = event => {
        if (sharedInput.events.length === 32) sharedInput.events.shift();
        sharedInput.events.push({event: event.type, trusted: event.isTrusted,
          target: targetName(event.target), x: event.clientX, y: event.clientY,
          button: event.button, control: controlState(event)});
      };
      let refreshControl = null, refreshOnPress = null;
      if (sharedInput) {
        for (const event of pointerEvents) document.addEventListener(event, pointerEvent, true);
      }
      const segmentState = value => value && ({
        input: value.input, out: value.out, rate: value.rate, end: value.end,
        until: value.until, countStart: value.countStart, countBeats: value.countBeats
      });
      const snapshot = () => {
        const now = performance.now(), context = MagicPlayer.context();
        return {
          at: now, hidden: document.hidden, visibility: document.visibilityState,
          focused: document.hasFocus(), simulatedHidden: window.simulatedHidden,
          ready, loading, playing,
          audio: {state: context?.state, time: context?.currentTime,
            pending: audioPending, generation: audioGeneration,
            task: !!audioTask, stretch: !!stretch,
            decodedFrames: decodedAudio?.length || 0,
            resumeAllowed: window.audioResumeAllowed, resumeCalls: window.audioResumeCalls},
          file: {archive: !!archive, version: archive?.manifest.version,
            audioChoices: archive?.audioVariants.length || 0,
            page: shownPage, pendingPage, pageLoads: pageLoads.size},
          transport: {position, duration, latency, token,
            segment: segmentState(segment), upcoming: segmentState(upcoming),
            rate: $('speed').value, loop: $('loop').checked,
            loopStart: $('loopStart').value, loopEnd: $('loopEnd').value,
            loopCount: $('loopCount').checked, count: $('count').checked,
            countBars: $('countBars').value},
          session: {role: ensemble.role, generation: ensemble.generation,
            preparing: ensemble.preparing, wantsReady: ensemble.wantsReady,
            prepared: ensemble.prepared, pending: !!ensemble.pending,
            command: ensemble.pending && {action: ensemble.pending.data.action,
              sequence: ensemble.pending.data.seq, at: ensemble.pending.at,
              armed: ensemble.pending.armed, next: segmentState(ensemble.pending.next)},
            socket: ensemble.ws?.readyState, downloading: !!ensemble.downloadTask,
            uploading: ensemble.uploading, shared: !!ensemble.shared, busy: ensemble.busy,
            peers: ensemble.peers.map(peer => ({leader: peer.leader,
              ready: peer.ready, connected: peer.connected})),
            samples: ensemble.recentSamples().map(sample => ({
              age: now - sample.sent, rtt: sample.rtt})),
            probeAges: [...ensemble.probes.values()].map(sent => now - sent)},
          ui: {disabled: $('sessionPrepare').disabled,
            pressed: $('sessionPrepare').getAttribute('aria-pressed'),
            label: $('sessionPrepare').textContent, live: $('sessionLive').textContent,
            readiness: $('sessionReadiness').textContent,
            bannerHidden: $('sessionBanner').hidden, status: $('status').textContent,
            playDisabled: $('play').disabled, playLabel: $('play').textContent,
            fileStatus: $('sessionFileStatus').textContent,
            downloadDisabled: $('getShared').disabled,
            fileError: $('status').classList.contains('file-error')}
        };
      };
      const record = (event, message) => {
        if (!active) return;
        if (history.length === limit) { history.shift(); discarded++; }
        history.push({event, ...(message ? {message: String(message).slice(0, 800)} : {}),
          ...snapshot()});
      };
      const before = snapshot(), originals = new Map();
      for (const name of ['prepare', 'error', 'status', 'unready', 'clearReady', 'syncReady',
                          'download', 'openShared', 'command', 'accept']) {
        const original = ensemble[name];
        originals.set(name, original);
        ensemble[name] = function (...args) {
          if (sharedInput && ['download', 'openShared'].includes(name)) sharedInput.calls[name]++;
          record(name + ':start',
            ['error', 'status'].includes(name) ? args[0]?.message || args[0] : null);
          let result;
          try { result = original.apply(this, args); }
          catch (error) { record(name + ':throw', error.message); throw error; }
          if (result?.then) {
            result.then(() => record(name + ':settled'),
              error => record(name + ':rejected', error.message));
          } else record(name + ':return');
          // Observe completion without replacing the original action's promise.
          return result;
        };
      }
      const context = MagicPlayer.context();
      const stateChanged = () => record('audio:statechange');
      const visibilityChanged = () => record('visibilitychange');
      const pageHidden = () => record('pagehide');
      const pageShown = () => record('pageshow');
      context?.addEventListener('statechange', stateChanged);
      document.addEventListener('visibilitychange', visibilityChanged);
      window.addEventListener('pagehide', pageHidden);
      window.addEventListener('pageshow', pageShown);
      record('phase:before');
      const timer = setInterval(() => record('sample'), 500);
      window.sessionReadinessTrace = {sharedInput, armSharedUpdate: () => {
        if (!sharedInput || refreshControl) throw Error('Shared pointer observer already armed');
        refreshControl = $('getShared');
        refreshOnPress = event => {
          const child = refreshControl.firstChild;
          const refresh = {before: controlState(event)};
          sharedInput.refreshes.push(refresh);
          // Put the normal periodic refresh between the native press and release.
          try { ensemble.update(); }
          finally {
            refresh.after = controlState(event);
            refresh.sameTextChild = child === refreshControl.firstChild;
          }
        };
        refreshControl.addEventListener('mousedown', refreshOnPress, {capture: true, once: true});
      }, finish: () => {
        record('phase:after');
        const after = snapshot();
        active = false; clearInterval(timer);
        for (const [name, original] of originals) ensemble[name] = original;
        context?.removeEventListener('statechange', stateChanged);
        document.removeEventListener('visibilitychange', visibilityChanged);
        window.removeEventListener('pagehide', pageHidden);
        window.removeEventListener('pageshow', pageShown);
        if (sharedInput) {
          for (const event of pointerEvents) {
            document.removeEventListener(event, pointerEvent, true);
          }
          refreshControl?.removeEventListener('mousedown', refreshOnPress, true);
        }
        return {participant, before, after, history, discarded,
          ...(sharedInput ? {shared_input: sharedInput} : {}),
          audioSessionEvents: (window.audioSessionEvents || []).slice(-32)};
      }};
    }""", {"participant": participant, "sharedReceive": shared_receive})


@contextmanager
def readiness_diagnostics(participants, engine, errors, phase="prepare_after_foreground"):
    shared_receive = phase == "shared-file-receive"
    report = {"engine": engine, "phase": phase, "participants": []}
    listeners = []

    def is_shared_get(request):
        path = urlsplit(request.url).path
        return (request.method == "GET" and path.startswith("/api/rooms/")
                and path.endswith("/file") and len(path.split("/")) == 5)

    for participant, page in participants:
        install_readiness_trace(page, participant, shared_receive)
        if shared_receive:
            transfer = {"requests": 0, "statuses": []}
            report.setdefault("shared_files", {})[participant] = transfer

            def requested(request, transfer=transfer):
                if is_shared_get(request):
                    transfer["requests"] += 1

            def responded(response, transfer=transfer):
                if is_shared_get(response.request) and len(transfer["statuses"]) < 8:
                    transfer["statuses"].append(response.status)

            for event, callback in (("request", requested), ("response", responded)):
                page.on(event, callback)
                listeners.append((page, event, callback))
    failure = None
    try:
        yield report
    except Exception as error:
        failure = error
        raise
    finally:
        for page, event, callback in listeners:
            page.remove_listener(event, callback)
        report.update(passed=failure is None, pageerrors=errors[-20:])
        for participant, page in participants:
            try:
                report["participants"].append(page.evaluate("sessionReadinessTrace.finish()"))
            except Exception as error:
                report["participants"].append({"participant": participant,
                                               "capture_error": str(error)[:1000]})
        destination = os.environ.get("MAGIC_RENDER_CHECK_DIR")
        if destination:
            try:
                path = Path(destination)
                path.mkdir(parents=True, exist_ok=True)
                suffix = "" if phase == "prepare_after_foreground" else f"-{phase}"
                (path / f"session-readiness-{engine}{suffix}.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError as error:
                if failure is None:
                    raise
                report["artifact_error"] = str(error)
        if failure is not None:
            summary = {key: value for key, value in report.items() if key != "participants"}
            summary["participants"] = [
                {**{key: value for key, value in participant.items()
                    if key in {"participant", "after", "capture_error", "discarded",
                               "shared_input"}},
                 "errors": [{key: event[key] for key in ("at", "event", "message")}
                            for event in participant.get("history", []) if "message" in event][-8:]}
                for participant in report["participants"]
            ]
            failure.add_note("Session readiness diagnostics:\n" +
                             json.dumps(summary, ensure_ascii=False))


@pytest.fixture(scope="module")
def relay_url(tmp_path_factory, web_url):  # noqa: F811
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def start():
        relay = Relay(public_url="https://relay.example", player_url=web_url,
                      storage=tmp_path_factory.mktemp("session-relay"),
                      origins=[web_url.rstrip("/")])
        runner = web.AppRunner(relay.app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, site._server.sockets[0].getsockname()[1]

    runner, port = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
    yield f"http://127.0.0.1:{port}"
    asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    thread.join()
    loop.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_session_scheduled_transport_parts_and_share(engine, web_url, relay_url, study_files):  # noqa: F811
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch()
        first = browser.new_context(viewport={"width": 1280, "height": 900})
        second = browser.new_context(viewport={"width": 1180, "height": 820})
        leader, member = first.new_page(), second.new_page()
        errors = []
        participants = (("leader", leader), ("member", member))
        for participant, page in participants:
            page.on("pageerror", lambda error, participant=participant: errors.append(
                {"participant": participant, "message": str(error)}
            ))
            page.goto(web_url)
            page.locator("#ensemblePanel").evaluate("e => e.open = true")
            page.locator("#relayURL").evaluate("(e,v) => e.value=v", relay_url)
            install_audio_session_probe(page)
        # A wall-clock error must not affect the monotonic server-clock estimate.
        member.evaluate("Date.now = () => 1234567890000")
        open_study(leader, study_files["gp"])
        leader.locator("#highlightEnabled").uncheck()
        leader.locator("#leaderMode").click()
        leader.wait_for_function("MagicEnsemble.state().role === 'leader'")
        assert not leader.locator("#highlightEnabled").is_checked()
        if engine == "chromium":
            leader.locator("#shareFile").click()
            expect(leader.locator("#sessionLive")).to_contain_text("공유 완료", timeout=45000)
        code = leader.evaluate("MagicEnsemble.state().code")
        member.locator("#inviteCode").fill(code)
        member.locator("#memberMode").click()
        member.wait_for_function("MagicEnsemble.state().role === 'member'")
        assert member.locator("#playerControls").is_hidden()
        assert member.locator("#loopControls").is_hidden()
        assert "멤버 활성" in member.locator("#sessionRole").inner_text()
        member.locator("#sessionRoom").click()
        assert member.locator("#ensemblePanel").is_hidden()
        assert member.locator("#getShared").is_visible()
        with readiness_diagnostics(
            participants, engine, errors, "shared-file-receive"
        ) as diagnostic:
            if engine == "webkit":
                assert member.locator("#getShared").is_disabled()
                assert member.locator("#sessionPrepare").is_disabled()
                leader.locator("#shareFile").click()
            member.wait_for_function("!document.getElementById('getShared').disabled")
            hold_audio_resume(member)
            member.evaluate("sessionReadinessTrace.armSharedUpdate()")
            member.locator("#getShared").click()
            wait_for_file_without_audio(member)
            pointer = member.evaluate("sessionReadinessTrace.sharedInput")
            assert len(pointer["refreshes"]) == 1, pointer
            refresh = pointer["refreshes"][0]
            assert refresh["sameTextChild"], pointer
            assert refresh["before"] == refresh["after"], pointer
            assert not refresh["before"]["disabled"], pointer
            assert refresh["before"]["hit"] == {"id": "getShared", "tag": "BUTTON"}, pointer
            for event in ("pointerdown", "mousedown", "pointerup", "mouseup", "click"):
                received = [item for item in pointer["events"] if item["event"] == event]
                assert len(received) == 1, pointer
                assert received[0]["trusted"], pointer
                assert received[0]["target"]["id"] == "getShared", pointer
            assert pointer["calls"] == {"download": 1, "openShared": 1}, pointer
            assert diagnostic["shared_files"]["member"] == {"requests": 1, "statuses": [200]}
        assert not leader.evaluate("MagicEnsemble.state().prepared")
        assert "준비가 해제" not in leader.locator("#sessionLive").inner_text()
        member.evaluate("MagicPlayer.context().suspend()")
        choose_file(member, {"name": "연습곡.magic", "mimeType": "",
                             "buffer": study_files["wav_gp"].read_bytes()}, "#sessionOpenFile")
        wait_for_file_without_audio(member)
        assert member.locator("#sessionFile").is_enabled()
        member.evaluate("window.audioResumeAllowed = true")
        assert leader.evaluate("MagicPlayer.archive().manifest.version") == 3
        assert member.evaluate("MagicPlayer.archive().manifest.version") == 2
        assert leader.evaluate("ensemble.media().source") == member.evaluate(
            "ensemble.media().source"
        )
        member.locator("#partSelect").select_option("1")
        member.wait_for_function("MagicPlayer.archive().activePart.track === 1")
        assert leader.evaluate("MagicPlayer.archive().activePart.track") == 2
        assert member.locator("#highlightEnabled").is_checked()
        assert not leader.locator("#highlightEnabled").is_checked()

        with readiness_diagnostics(participants, engine, errors) as diagnostic:
            hold_resume_completion(member, "#sessionPrepare")
            member.locator("#sessionPrepare").click()
            member.wait_for_function("window.releaseResume !== null")
            background_page(member)
            member.wait_for_function("MagicPlayer.context().state === 'suspended'")
            release_resume_after_foreground(member)
            assert not member.evaluate("MagicEnsemble.state().prepared")
            assert member.locator("#sessionPrepare").get_attribute("aria-pressed") == "false"
            for page in (leader, member):
                page.evaluate("window.audioSessionEvents = []")
            for page in (leader, member):
                page.locator("#sessionPrepare").click()
            for participant, page in participants:
                diagnostic["waiting_for"] = participant
                page.wait_for_function("MagicEnsemble.state().prepared")
                assert_audio_session_order(page)
        with readiness_diagnostics(
            participants, engine, errors, "ready-toggle-roundtrip"
        ) as diagnostic:
            for participant, waiting_for in ((leader, "리더"), (member, "멤버")):
                for page in (leader, member):
                    observer = "leader" if page == leader else "member"
                    diagnostic["waiting_for"] = f"{observer} all ready before {waiting_for} toggle"
                    page.get_by_text("모두 준비 완료", exact=False).wait_for()
                diagnostic["waiting_for"] = f"{waiting_for} cancel readiness"
                participant.locator("#sessionPrepare").click()
                other = member if participant == leader else leader
                diagnostic["waiting_for"] = f"other participant sees {waiting_for} unready"
                expect(other.locator("#sessionReadiness")).to_have_text(
                    f"준비 대기 중인 참가자 · {waiting_for}"
                )
                diagnostic["waiting_for"] = f"readiness controls after {waiting_for} cancellation"
                assert other.locator("#sessionPrepare").get_attribute("aria-pressed") == "true"
                assert participant.locator("#sessionPrepare").get_attribute(
                    "aria-pressed"
                ) == "false"
                assert leader.locator("#play").is_disabled()
                diagnostic["waiting_for"] = f"{waiting_for} prepare again"
                participant.locator("#sessionPrepare").click()
            diagnostic["waiting_for"] = "leader play enabled after both readiness toggles"
            leader.wait_for_function("!document.getElementById('play').disabled")
        leader.locator("#play").click()
        leader.wait_for_function("MagicEnsemble.state().pending")
        assert not member.evaluate("MagicPlayer.state().playing")
        leader.wait_for_function("MagicPlayer.state().playing", timeout=7000)
        member.wait_for_function("MagicPlayer.state().playing", timeout=7000)
        leader.wait_for_timeout(800)
        left = leader.evaluate("MagicPlayer.state().position")
        right = member.evaluate("MagicPlayer.state().position")
        assert abs(left - right) < .13
        member.locator("#highlightEnabled").uncheck()
        leader.locator("#highlightEnabled").check()
        assert member.locator("#highlight").is_hidden()
        assert leader.locator("#highlight").is_visible()
        assert member.evaluate("MagicPlayer.state().playing && MagicEnsemble.state().prepared")
        leader.locator("#step").fill("5")
        leader.locator("#forward").click()
        member.wait_for_function("MagicPlayer.state().position >= 10", timeout=7000)
        for participant in (leader, member):
            participant.wait_for_function("""() => {
              const state = MagicPlayer.state(), cue = MagicPlayer.archive().timing.cues[state.cue];
              const slot = document.querySelector(`.score-page[data-page="${cue.page}"]`);
              const view = document.getElementById('viewport'), box = slot.getBoundingClientRect();
              const center = box.top + (cue.box.top + cue.box.bottom) * box.height / 2;
              return Math.abs(center - view.getBoundingClientRect().top - view.clientTop -
                view.clientHeight / 2) < 2;
            }""")
        leader.wait_for_timeout(250)
        assert abs(leader.evaluate("MagicPlayer.state().position") -
                   member.evaluate("MagicPlayer.state().position")) < .13
        leader.locator("#play").click()
        member.wait_for_function("!MagicPlayer.state().playing", timeout=7000)
        leader.wait_for_function("!MagicPlayer.state().playing", timeout=7000)
        assert abs(leader.evaluate("MagicPlayer.state().position") -
                   member.evaluate("MagicPlayer.state().position")) < .13
        # A member's internal command cannot bypass server authorization.
        member.evaluate("ensemble.send({type:'command',action:'play',state:ensemble.snapshot()})")
        member.wait_for_function("document.getElementById('ensembleStatus').textContent.includes('리더만')")
        assert not leader.evaluate("MagicPlayer.state().playing")
        # A repeated two-bar range with a count at each visit uses the same clock.
        with readiness_diagnostics(participants, engine, errors, "loop-count-play") as diagnostic:
            leader.locator("#loopStart").select_option("2")
            leader.locator("#loopEnd").select_option("3")
            leader.locator("#loop").check()
            leader.locator("#loopCount").check()
            leader.locator("#speed").fill("150")
            leader.locator("#play").click()
            diagnostic["waiting_for"] = "leader playing"
            leader.wait_for_function("MagicPlayer.state().playing", timeout=7000)
        leader.wait_for_timeout(6000)
        assert leader.evaluate("MagicPlayer.state().playing")
        assert member.evaluate("MagicPlayer.state().playing")
        assert abs(leader.evaluate("MagicPlayer.state().position") -
                   member.evaluate("MagicPlayer.state().position")) < .15
        # Losing readiness invalidates future commands and stops both participants.
        member.locator("#sessionPrepare").click()
        leader.wait_for_function("!MagicPlayer.state().playing")
        assert not member.evaluate("MagicPlayer.state().playing")
        member.locator("#sessionPrepare").click()
        leader.locator("#play").click()
        for page in (leader, member):
            page.wait_for_function("MagicPlayer.state().playing", timeout=7000)
        leader.locator("#exitSession").click()
        member.wait_for_function("MagicEnsemble.state().role === ''")
        assert member.locator("#playerControls").is_visible()
        assert not member.locator("#highlightEnabled").is_checked()
        assert leader.locator("#highlightEnabled").is_checked()
        for page in (leader, member):
            assert page.locator("#members li").count() == 0
            assert page.locator("#inviteCode").input_value() == ""
            assert page.locator("#inviteLink").get_attribute("href") is None
        browser.close()
        assert not errors


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_ready_while_shared_transfer_finishes(engine, web_url, relay_url, study_files):  # noqa: F811
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch()
        leader, first, second = [browser.new_page() for _ in range(3)]
        for page in (leader, first, second):
            page.goto(web_url + "#ensemble")
            page.locator("#relayURL").evaluate("(e,v) => e.value=v", relay_url)
        open_study(leader, study_files["gp"])
        leader.locator("#leaderMode").click()
        leader.wait_for_function("MagicEnsemble.state().peers.length === 1")
        code = leader.locator("#roomCode").inner_text()
        for member in (first, second):
            member.locator("#inviteCode").fill(code)
            member.locator("#memberMode").click()
            expect(member.locator("#sessionPrepare")).to_be_disabled()

        # Hold the actual browser response after the relay has published the file.
        # Forwarding the upload via Playwright can change its binary body in WebKit.
        for page in (leader, first):
            page.evaluate("""() => {
                const fetchFile = window.fetch.bind(window);
                const gate = new Promise(resolve => { window.releaseSharedFile = resolve; });
                window.sharedResponses = 0;
                window.fetch = async (...args) => {
                    const response = await fetchFile(...args);
                    if (response.ok && new URL(response.url).pathname.endsWith('/file')) {
                        window.sharedResponses++;
                        await gate;
                    }
                    return response;
                };
            }""")
        leader.locator("#shareFile").click()
        for member in (first, second):
            expect(member.locator("#sessionPrepare")).to_have_text("리더 파일 받고 준비")
        leader.locator("#sessionPrepare").click(timeout=4000)
        # One member is already downloading. Ready must reuse that transfer.
        first.locator("#getShared").click()
        first.locator("#sessionPrepare").click(timeout=4000)
        second.locator("#sessionPrepare").click()
        expect(second.locator("#sessionPrepare")).to_have_text(
            "준비 완료 · 해제하기", timeout=45000
        )
        for page in (leader, first):
            page.wait_for_function("window.sharedResponses === 1")
            page.evaluate("window.releaseSharedFile()")
        for page in (leader, first, second):
            expect(page.locator("#sessionReadiness")).to_contain_text(
                "모두 준비 완료", timeout=45000
            )

        # Slow probes during a transfer can pause readiness. Two newer good
        # measurements restore the original request without another Ready click.
        first.evaluate("""() => {
            const now = performance.now();
            const offset = ensemble.serverNow() - now;
            ensemble.samples = [5900, 4000, 2000].map(age => ({
                sent: now - age, rtt: 1600, offset
            }));
            ensemble.syncReady();
            if (ensemble.prepared || !ensemble.wantsReady) throw Error('Lost readiness request');
        }""")
        expect(leader.locator("#play")).to_be_disabled()
        first.evaluate("""() => {
            const now = performance.now();
            const offset = ensemble.samples[0].offset;
            for (const age of [70, 45]) {
                const id = now - age;
                ensemble.probes.set(id, id);
                ensemble.receive({type:'pong', id, time:now - age / 2 + offset});
            }
            if (!ensemble.prepared) throw Error('Did not recover after transfer');
        }""")
        for page in (leader, first, second):
            page.wait_for_function("MagicEnsemble.state().peers.filter(p => p.ready).length === 3")
        leader.locator("#play").click()
        for page in (leader, first, second):
            page.wait_for_function("MagicPlayer.state().playing", timeout=7000)
        assert first.evaluate("window.sharedResponses") == 1
        browser.close()
