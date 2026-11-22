# SPDX-License-Identifier: GPL-3.0-only
"""Build the offline single HTML player from reviewed local sources; no npm/CDN."""

import base64
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web_player"


def build():
    pins = json.loads((WEB / "vendor/provenance.json").read_text(encoding='utf-8'))
    for name, digest in pins["files"].items():
        if hashlib.sha256((WEB / "vendor" / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Vendor checksum mismatch: {name}")
    vendor = (WEB / "vendor/SignalsmithStretch.js").read_text(encoding='utf-8')
    # 1.3.2 uses currentTime to discard scheduled future segments even when an
    # explicit output is supplied. Retain previous segments and only advance
    # the current segment at actual currentTime. See THIRD_PARTY_NOTICES.md.
    before = "let outputTime = ('outputTime' in objIn) ? objIn.outputTime : currentTime;"
    after = (
        "let outputTime = ('outputTime' in objIn) ? objIn.outputTime : "
        "('output' in objIn ? objIn.output : currentTime);"
    )
    assert vendor.count(before) == 1
    vendor = vendor.replace(before, after)
    before = "while (this.timeMap.length > 1 && this.timeMap[1].output <= outputTime) {"
    assert vendor.count(before) == 2
    vendor = vendor.replace(
        before, "while (this.timeMap.length > 1 && this.timeMap[1].output <= currentTime) {", 1
    )
    template = (WEB / "template.html").read_text(encoding='utf-8')
    versions = re.findall(r'<meta name="magic-version" content="(\d+\.\d+\.\d+)">', template)
    if len(versions) != 1 or template.count('/*BUILD*/') != 1:
        raise ValueError("The player template must declare one version and build identity.")
    version = versions[0]
    for key, content in {
        "STYLE": (WEB / "style.css").read_text(encoding='utf-8'),
        "VENDOR": vendor,
        "ARCHIVE": (WEB / "archive.js").read_text(encoding='utf-8'),
        "APP": (WEB / "app.js").read_text(encoding='utf-8'),
        "ENSEMBLE": (WEB / "ensemble.js").read_text(encoding='utf-8'),
    }.items():
        template = template.replace(f"/*{key}*/", content)
    template = template.replace('/*VERSION*/', version)
    # Hash before inserting the identity so HTML and worker can compare one build.
    build_id = hashlib.sha256(template.encode()).hexdigest()
    template = template.replace('/*BUILD*/', build_id)
    (WEB / "index.html").write_text(template, encoding='utf-8', newline='\n')
    digest = hashlib.sha256(template.encode()).digest()
    revision = digest.hex()[:16]
    integrity = base64.b64encode(digest).decode("ascii")
    worker = f"""/* SPDX-License-Identifier: GPL-3.0-only */
const VERSION = '{version}';
const BUILD = '{build_id}';
const CACHE_PREFIX = 'magic-player:' + encodeURIComponent(self.registration.scope) + ':';
const CACHE = CACHE_PREFIX + '{revision}-verified';
async function installPlayer() {{
  const page = await fetch('./index.html?rev={revision}', {{
    cache: 'reload', integrity: 'sha256-{integrity}'
  }});
  if (!page.ok) throw new Error('The new player is unavailable.');
  const cache = await caches.open(CACHE);
  await cache.addAll(['./manifest.webmanifest', './icon-192.png', './icon-512.png']
    .map(path => new Request(path, {{cache: 'reload'}})));
  await Promise.all([cache.put('./', page.clone()), cache.put('./index.html', page)]);
  await self.skipWaiting();
}}
self.addEventListener('install', event => event.waitUntil(installPlayer()));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys =>
  Promise.all(keys.filter(k => k.startsWith(CACHE_PREFIX) && k !== CACHE)
    .map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('message', event => {{
  if (event.data?.type === 'MAGIC_PLAYER_VERSION' && event.ports?.[0])
    event.ports[0].postMessage({{version: VERSION, build: BUILD}});
}});
self.addEventListener('fetch', event => {{
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  event.respondWith(caches.open(CACHE).then(cache => cache.match(event.request))
    .then(saved => saved || fetch(event.request)));
}});
"""
    (WEB / "sw.js").write_text(worker, encoding='utf-8', newline='\n')
    return WEB / "index.html"


if __name__ == "__main__":
    print(build())
