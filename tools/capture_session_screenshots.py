# SPDX-License-Identifier: GPL-3.0-only
"""Capture real Session views with two clients and original synthetic GP music."""

import asyncio
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from practice_player.magic_bundle import export_magic  # noqa: E402
from relay.server import Relay  # noqa: E402
from tests.test_magic_bundle import portable_study  # noqa: E402


def capture():
    with tempfile.TemporaryDirectory(prefix="magic-session-demo-") as folder:
        root = Path(folder)
        request, _, _ = portable_study(root / "study", count=32)
        bundle = export_magic(request)
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        async def start():
            relay = Relay(public_url="http://127.0.0.1", player_url="http://127.0.0.1/",
                          storage=root / "transfer")
            runner = web.AppRunner(relay.app())
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            relay.public_url, relay.player_url = origin, origin + "/"
            relay.origins.add(origin)
            return runner, origin

        runner, origin = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
        try:
            with sync_playwright() as playwright:
                browser = playwright.webkit.launch()
                leader = browser.new_context(viewport={"width": 1366, "height": 1100}).new_page()
                member = browser.new_context(viewport={"width": 1366, "height": 1100}).new_page()
                for page, name in ((leader, "Lead Guitar"), (member, "Rhythm Guitar")):
                    page.goto(origin)
                    page.locator("#file").set_input_files(bundle)
                    page.wait_for_function("MagicPlayer.state().ready")
                    page.evaluate("theme('light')")
                    page.locator("#ensemblePanel").evaluate("e => e.open=true")
                    page.locator("#relayURL").evaluate("(e,v) => e.value=v", origin)
                    page.locator("#memberName").fill(name)
                leader.locator("#leaderMode").click()
                leader.wait_for_function("MagicEnsemble.state().role === 'leader'")
                member.locator("#inviteCode").fill(leader.evaluate("MagicEnsemble.state().code"))
                member.locator("#memberMode").click()
                member.wait_for_function("MagicEnsemble.state().role === 'member'")
                member.locator("#partSelect").select_option("1")
                for page in (leader, member):
                    page.wait_for_function("!document.getElementById('sessionPrepare').disabled")
                    page.locator("#sessionPrepare").click()
                leader.wait_for_function("!document.getElementById('play').disabled")
                for page in (leader, member):
                    page.evaluate("document.activeElement.blur(); window.scrollTo(0,0)")
                    page.wait_for_timeout(250)
                leader.screenshot(path=ROOT / "docs/images/session-leader.png")
                member.screenshot(path=ROOT / "docs/images/session-member.png")
                browser.close()
        finally:
            asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
            loop.call_soon_threadsafe(loop.stop)
            thread.join()
            loop.close()


if __name__ == "__main__":
    capture()
