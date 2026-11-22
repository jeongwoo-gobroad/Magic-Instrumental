# SPDX-License-Identifier: GPL-3.0-only
"""Actual SW updates in Chromium/WebKit; not physical home-screen certification."""

import asyncio
import base64
import hashlib
import mimetypes
import os
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest
from aiohttp import web

from relay.server import Relay
from tests import test_web_player as player_tests
from tests.test_web_audio import hold_resume_completion
from tests.test_web_player import open_study
from tools import build_web

page = player_tests.page
study_files = player_tests.study_files

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_WEB_TESTS") != "1", reason="RUN_WEB_TESTS=1 and browser runtimes required"
)


@pytest.fixture(scope="module")
def update_builds(tmp_path_factory):
    source = Path(__file__).resolve().parents[1] / "web_player"
    root = tmp_path_factory.mktemp("web-update-builds")
    result = {}
    for name, version in (("old", "9.8.0"), ("new", "9.8.1")):
        target = root / name
        shutil.copytree(source, target)
        template = (target / "template.html").read_text(encoding="utf-8")
        template, count = re.subn(
            r'(<meta name="magic-version" content=")[^"]+(">)',
            lambda match: match[1] + version + match[2], template,
        )
        assert count == 1
        (target / "template.html").write_text(template, encoding="utf-8", newline="\n")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(build_web, "WEB", target)
            build_web.build()
        files = {item.relative_to(target).as_posix(): item.read_bytes()
                 for item in target.rglob("*") if item.is_file()}
        identity = re.search(rb'<meta name="magic-build" content="([a-f0-9]{64})">',
                             files["index.html"])
        assert identity
        result[name] = {"files": files, "version": version, "build": identity[1].decode()}

    # The previous bootstrap had no update UI or version-message listener.
    legacy = b'''<!doctype html><html><body><p id="legacy">Legacy 2.4.7</p>
<script>navigator.serviceWorker.register('./sw.js');</script></body></html>'''
    digest = hashlib.sha256(legacy).digest()
    worker = f"""const CACHE = 'magic-v2-{digest.hex()[:16]}-verified';
self.addEventListener('install', event => event.waitUntil((async () => {{
  const page = await fetch('./index.html?rev={digest.hex()[:16]}', {{
    cache: 'reload', integrity: 'sha256-{base64.b64encode(digest).decode()}'
  }});
  const cache = await caches.open(CACHE);
  await Promise.all([cache.put('./', page.clone()), cache.put('./index.html', page)]);
  await self.skipWaiting();
}})()));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys =>
  Promise.all(keys.filter(key => key.startsWith('magic-v2-') && key !== CACHE)
    .map(key => caches.delete(key)))).then(() => self.clients.claim())));
self.addEventListener('fetch', event => event.respondWith(caches.open(CACHE)
  .then(cache => cache.match(event.request)).then(saved => saved || fetch(event.request))));
""".encode()
    result["legacy"] = {
        "files": {**result["old"]["files"], "index.html": legacy, "sw.js": worker},
        "version": "2.4.7", "build": digest.hex(),
    }
    return result


class UpdateServer:
    def __init__(self, builds, initial):
        self.builds = builds
        self.releases = {"/player/": initial, "/other/": "old"}
        self.corrupt = set()
        self.requests = []
        self.lock = threading.Lock()
        self.origin = ""

    def publish(self, release, scope="/player/", corrupt=False):
        with self.lock:
            self.releases[scope] = release
            if corrupt:
                self.corrupt.add(scope)
            else:
                self.corrupt.discard(scope)

    def response(self, path):
        request = urlsplit(path)
        with self.lock:
            scope = next((scope for scope in self.releases if request.path.startswith(scope)), None)
            if scope is None:
                return None, None
            name = request.path[len(scope):] or "index.html"
            self.requests.append((scope, name, request.query))
            body = self.builds[self.releases[scope]]["files"].get(name)
            if name == "index.html" and request.query and scope in self.corrupt:
                body += b"\n<!-- incomplete deployment -->"
            elif request.path.endswith("/index.html") and not request.query:
                body = b"<title>Stale unversioned CDN page</title>"
            return body, mimetypes.guess_type(name)[0] or "application/octet-stream"


@pytest.fixture
def initial_release(request):
    return getattr(request, "param", "old")


@pytest.fixture
def update_server(update_builds, initial_release):
    state = UpdateServer(update_builds, initial_release)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body, content_type = state.response(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def web_url(update_server):
    return update_server.origin + "/player/"


@pytest.fixture
def update_relay_url(tmp_path, web_url):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def start():
        address = urlsplit(web_url)
        relay = Relay(public_url="https://relay.example", player_url=web_url,
                      storage=tmp_path / "relay", origins=[f"{address.scheme}://{address.netloc}"])
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


def document_build(page):
    return page.locator('meta[name="magic-build"]').get_attribute("content")


def wait_for_initial_install(page, build):
    page.wait_for_function("navigator.serviceWorker.controller !== null")
    page.wait_for_function("!document.getElementById('checkUpdate').disabled")
    assert document_build(page) == build["build"]
    assert build["version"] in page.locator("#appVersion").inner_text()
    assert page.locator("#appUpdate").is_hidden()


def check_for_update(page, build):
    page.locator("#checkUpdate").click()
    page.wait_for_function("version => !document.getElementById('appUpdate').hidden && "
                           "document.getElementById('updateTitle').textContent.includes(version)",
                           arg=build["version"], timeout=30000)
    page.wait_for_function("!document.getElementById('checkUpdate').disabled")


def apply_update(page, build):
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.locator("#applyUpdate").click()
    page.wait_for_function("typeof MagicPlayer !== 'undefined'")
    assert document_build(page) == build["build"]
    assert build["version"] in page.locator("#appVersion").inner_text()
    assert not page.evaluate("MagicPlayer.state().ready")


def cached_digest(page, url):
    return page.evaluate("""async url => {
      const data = await (await fetch(url)).arrayBuffer();
      const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', data));
      return [...hash].map(value => value.toString(16).padStart(2, '0')).join('');
    }""", url)


def verify_landing_layout(page):
    root = Path(__file__).resolve().parents[1]
    player_url = "https://jeongwoo-gobroad.github.io/Magic-Instrumental/"
    page.goto("about:blank")
    page.route("https://raw.githubusercontent.com/**/docs/images/desktop-light.png",
               lambda route: route.fulfill(path=str(root / "docs/images/desktop-light.png")))
    page.set_content((root / "landing/index.html").read_text(encoding="utf-8"))
    install = page.locator("#tablet-install")
    cards = install.locator(".platform")
    assert cards.locator("h3").all_text_contents() == [
        "iPad · Safari", "iPad · Chrome", "Android · Chrome",
    ]
    assert install.locator(".actions a").get_attribute("href") == player_url
    assert "적용 후에는 .magic 파일을 다시 선택" in install.inner_text()
    destination = os.environ.get("MAGIC_RENDER_CHECK_DIR")
    engine = page.context.browser.browser_type.name
    for width in (360, 1280):
        page.set_viewport_size({"width": width, "height": 820})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        for card in cards.all():
            card.scroll_into_view_if_needed()
            box = card.bounding_box()
            assert box and 0 <= box["x"] and box["x"] + box["width"] <= width + 1
            assert card.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
        if destination:
            folder = Path(destination)
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=folder / f"landing-home-screen-{engine}-{width}.png",
                            full_page=True)


def test_update_keeps_playing_until_explicit_offline_apply(page, update_server, study_files):
    old, new = update_server.builds["old"], update_server.builds["new"]
    wait_for_initial_install(page, old)
    open_study(page, study_files["gp"])
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .2")
    before = page.evaluate("MagicPlayer.state().position")
    update_server.publish("new")
    check_for_update(page, new)
    assert document_build(page) == old["build"]
    assert page.evaluate("MagicPlayer.state().playing && MagicPlayer.state().ready")
    assert page.evaluate("MagicPlayer.state().position") > before
    assert ".magic" in page.locator("#updateNotice").inner_text()
    assert "다시 선택" in page.locator("#updateNotice").inner_text()
    page.locator("#applyUpdate").click()
    assert "메트로놈을 끈 뒤" in page.locator("#updateActionStatus").inner_text()
    assert page.evaluate("MagicPlayer.state().playing")

    page.locator("#play").click()
    page.locator("#metro").click()
    page.wait_for_function("MagicPlayer.state().metro")
    page.locator("#applyUpdate").click()
    assert "메트로놈을 끈 뒤" in page.locator("#updateActionStatus").inner_text()
    assert page.evaluate("MagicPlayer.state().metro && !MagicPlayer.state().playing")
    page.locator("#metro").click()
    page.wait_for_function("!MagicPlayer.state().metro")
    page.context.set_offline(True)
    apply_update(page, new)
    expected = hashlib.sha256(new["files"]["index.html"]).hexdigest()
    assert cached_digest(page, update_server.origin + "/player/") == expected
    page.goto(update_server.origin + "/player/index.html")
    assert document_build(page) == new["build"]
    assert cached_digest(page, page.url) == expected
    open_study(page, study_files["pdf"])
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().position > .2")


def test_failed_integrity_keeps_previous_offline_player_and_retries(page, update_server):
    old, new = update_server.builds["old"], update_server.builds["new"]
    wait_for_initial_install(page, old)
    previous_caches = page.evaluate("caches.keys()")
    update_server.publish("new", corrupt=True)
    page.locator("#checkUpdate").click()
    page.wait_for_function("!document.getElementById('checkUpdate').disabled")
    assert "못" in page.locator("#updateStatus").inner_text()
    assert page.locator("#appUpdate").is_hidden()
    assert document_build(page) == old["build"]
    assert page.evaluate("caches.keys()") == previous_caches

    # A cache-miss navigation can receive new HTML before its verified worker.
    page.goto(update_server.origin + "/player/?newer-document")
    page.wait_for_function("!document.getElementById('checkUpdate').disabled")
    assert document_build(page) == new["build"]
    assert page.locator("#appUpdate").is_hidden()
    assert "최신 버전" not in page.locator("#updateStatus").inner_text()
    assert old["version"] in page.locator("#offline").inner_text()
    page.context.set_offline(True)
    page.goto(update_server.origin + "/player/")
    assert document_build(page) == old["build"]
    assert cached_digest(page, page.url) == hashlib.sha256(old["files"]["index.html"]).hexdigest()

    update_server.publish("new")
    page.context.set_offline(False)
    check_for_update(page, new)
    apply_update(page, new)


def test_other_tab_and_scope_survive_update_activation(page, update_server):
    old, new = update_server.builds["old"], update_server.builds["new"]
    wait_for_initial_install(page, old)
    second, other = page.context.new_page(), page.context.new_page()
    second.goto(update_server.origin + "/player/")
    other.goto(update_server.origin + "/other/")
    wait_for_initial_install(second, old)
    wait_for_initial_install(other, old)
    page.evaluate("window.originalUpdateTab = true")
    await_keys = page.evaluate("""async () => {
      await caches.open('magic-v2-legacy-sentinel');
      return caches.keys();
    }""")
    other_prefix = "magic-player:" + quote(update_server.origin + "/other/", safe="~()*!.'-") + ":"
    other_keys = {key for key in await_keys if key.startswith(other_prefix)}
    assert other_keys

    update_server.publish("new")
    check_for_update(page, new)
    second.locator("#appUpdate").wait_for(state="visible")
    apply_update(second, new)
    assert page.evaluate("window.originalUpdateTab")
    assert document_build(page) == old["build"]
    assert page.locator("#appUpdate").is_visible()
    assert document_build(other) == old["build"]
    assert other.locator("#appUpdate").is_hidden()
    current_keys = set(page.evaluate("caches.keys()"))
    assert other_keys <= current_keys
    assert "magic-v2-legacy-sentinel" in current_keys

    page.context.set_offline(True)
    other.reload()
    assert document_build(other) == old["build"]
    apply_update(page, new)


@pytest.mark.parametrize("initial_release", ["legacy"], indirect=True)
def test_legacy_bootstrap_receives_new_player_on_reopen(page, update_server):
    new = update_server.builds["new"]
    page.wait_for_function("navigator.serviceWorker.controller !== null")
    assert page.locator("#legacy").inner_text() == "Legacy 2.4.7"
    legacy_keys = set(page.evaluate("caches.keys()"))
    page.evaluate("window.legacyController = navigator.serviceWorker.controller")
    update_server.publish("new")
    page.evaluate("navigator.serviceWorker.getRegistration().then(reg => reg.update())")
    page.wait_for_function("navigator.serviceWorker.controller !== window.legacyController")
    assert page.locator("#legacy").is_visible()
    assert legacy_keys <= set(page.evaluate("caches.keys()"))
    page.reload()
    page.wait_for_function("typeof MagicPlayer !== 'undefined'")
    assert document_build(page) == new["build"]
    assert page.locator("#appUpdate").is_hidden()
    verify_landing_layout(page)


def test_session_pending_audio_and_late_play_block_apply(
    page, update_server, update_relay_url, study_files,
):
    old, new = update_server.builds["old"], update_server.builds["new"]
    wait_for_initial_install(page, old)
    update_server.publish("new")
    check_for_update(page, new)
    page.evaluate("""() => {
      const read = Blob.prototype.arrayBuffer;
      window.releaseFileRead = null;
      Blob.prototype.arrayBuffer = function (...args) {
        Blob.prototype.arrayBuffer = read;
        return read.apply(this, args).then(data => new Promise(resolve => {
          window.releaseFileRead = () => resolve(data);
        }));
      };
    }""")
    page.locator("#file").set_input_files(study_files["gp"])
    page.wait_for_function("window.releaseFileRead !== null")
    assert page.evaluate("MagicPlayer.state().loading")
    page.locator("#applyUpdate").click()
    assert "파일과 소리 준비" in page.locator("#updateActionStatus").inner_text()
    page.evaluate("releaseFileRead()")
    page.wait_for_function("!MagicPlayer.state().loading", timeout=45000)
    assert page.evaluate("MagicPlayer.state().ready")
    page.locator("#ensemblePanel").evaluate("element => element.open = true")
    page.locator("#relayURL").evaluate("(element, url) => element.value = url", update_relay_url)
    page.locator("#leaderMode").click()
    page.wait_for_function("MagicEnsemble.state().role === 'leader'")
    page.locator("#sessionPrepare").click()
    page.wait_for_function("MagicEnsemble.state().prepared")
    page.locator("#applyUpdate").click()
    assert "Session을 종료" in page.locator("#updateActionStatus").inner_text()
    assert page.evaluate("MagicEnsemble.state().role === 'leader' && "
                         "MagicEnsemble.state().prepared")
    page.locator("#exitSession").click()

    hold_resume_completion(page, "#play")
    page.locator("#play").click()
    page.wait_for_function("window.releaseResume !== null")
    page.locator("#applyUpdate").click()
    assert "파일과 소리 준비" in page.locator("#updateActionStatus").inner_text()
    page.evaluate("async () => { releaseResume(); await heldAudioAction; }")
    page.wait_for_function("MagicPlayer.state().position > .2")
    page.locator("#play").click()

    page.evaluate("""() => {
      const post = ServiceWorker.prototype.postMessage;
      let held = false;
      window.releaseUpdateMessage = null;
      ServiceWorker.prototype.postMessage = function (message, transfer) {
        if (!held && message?.type === 'MAGIC_PLAYER_VERSION') {
          held = true;
          window.releaseUpdateMessage = () => post.call(this, message, transfer);
          return;
        }
        return post.call(this, message, transfer);
      };
      const button = document.getElementById('applyUpdate'), apply = button.onclick;
      button.onclick = () => window.pendingUpdateApply = apply();
    }""")
    page.locator("#applyUpdate").click()
    page.wait_for_function("window.releaseUpdateMessage !== null")
    page.locator("#play").click()
    page.wait_for_function("MagicPlayer.state().playing")
    page.evaluate("async () => { releaseUpdateMessage(); await pendingUpdateApply; }")
    assert "메트로놈을 끈 뒤" in page.locator("#updateActionStatus").inner_text()
    assert document_build(page) == old["build"]
    assert page.evaluate("MagicPlayer.state().playing && MagicPlayer.state().ready")
