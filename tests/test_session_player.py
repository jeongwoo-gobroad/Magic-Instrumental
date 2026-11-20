# SPDX-License-Identifier: GPL-3.0-only
"""Two actual browser participants, delayed commands and independent GP parts."""

import asyncio
import os
import threading

import pytest
from aiohttp import web

from relay.server import Relay
from tests.test_web_player import open_study, study_files, web_url  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("RUN_WEB_TESTS") != "1",
                                reason="RUN_WEB_TESTS=1 and browser runtimes required")


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
        for page in (leader, member):
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(web_url)
            page.locator("#ensemblePanel").evaluate("e => e.open = true")
            page.locator("#relayURL").evaluate("(e,v) => e.value=v", relay_url)
        # A wall-clock error must not affect the monotonic server-clock estimate.
        member.evaluate("Date.now = () => 1234567890000")
        open_study(leader, study_files["gp"])
        leader.locator("#leaderMode").click()
        leader.wait_for_function("MagicEnsemble.state().role === 'leader'")
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
        if engine == "webkit":
            assert member.locator("#getShared").is_disabled()
            assert member.locator("#sessionPrepare").is_disabled()
            leader.locator("#shareFile").click()
        member.wait_for_function("!document.getElementById('getShared').disabled")
        member.locator("#getShared").click()
        member.wait_for_function("MagicPlayer.state().ready", timeout=45000)
        assert not leader.evaluate("MagicEnsemble.state().prepared")
        assert "준비가 해제" not in leader.locator("#sessionLive").inner_text()
        member.locator("#partSelect").select_option("1")
        member.wait_for_function("MagicPlayer.archive().activePart.track === 1")
        assert leader.evaluate("MagicPlayer.archive().activePart.track") == 2
        for page in (leader, member):
            page.locator("#sessionPrepare").click()
        for participant, waiting_for in ((leader, "리더"), (member, "멤버")):
            for page in (leader, member):
                page.get_by_text("모두 준비 완료", exact=False).wait_for()
            participant.locator("#sessionPrepare").click()
            other = member if participant == leader else leader
            expect(other.locator("#sessionReadiness")).to_have_text(
                f"준비 대기 중인 참가자 · {waiting_for}"
            )
            assert other.locator("#sessionPrepare").get_attribute("aria-pressed") == "true"
            assert participant.locator("#sessionPrepare").get_attribute("aria-pressed") == "false"
            assert leader.locator("#play").is_disabled()
            participant.locator("#sessionPrepare").click()
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
        leader.locator("#step").fill("5")
        leader.locator("#forward").click()
        member.wait_for_function("MagicPlayer.state().position >= 10", timeout=7000)
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
        leader.locator("#loopStart").select_option("2")
        leader.locator("#loopEnd").select_option("3")
        leader.locator("#loop").check()
        leader.locator("#loopCount").check()
        leader.locator("#speed").fill("150")
        leader.locator("#play").click()
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

        # The relay has published the file, but its HTTP reply has not reached the leader.
        held = []

        def hold_response(route):
            response = route.fetch()
            assert response.ok
            held.append((route, response))

        leader.route("**/api/rooms/*/file", hold_response)
        leader.locator("#shareFile").click()
        for member in (first, second):
            expect(member.locator("#sessionPrepare")).to_have_text("리더 파일 받고 준비")
        leader.locator("#sessionPrepare").click(timeout=4000)
        # One member is already downloading. Ready must reuse that transfer.
        first.route("**/api/rooms/*/file", hold_response)
        first.locator("#getShared").click()
        first.locator("#sessionPrepare").click(timeout=4000)
        second.locator("#sessionPrepare").click()
        expect(second.locator("#sessionPrepare")).to_have_text("준비 완료 · 해제하기", timeout=45000)
        assert len(held) == 2
        for route, response in held:
            route.fulfill(response=response)
        for page in (leader, first, second):
            expect(page.locator("#sessionReadiness")).to_contain_text("모두 준비 완료", timeout=45000)

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
        browser.close()
