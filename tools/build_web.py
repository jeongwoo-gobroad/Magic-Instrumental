# SPDX-License-Identifier: GPL-3.0-only
"""Build the offline single HTML player from reviewed local sources; no npm/CDN."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web_player"


def build():
    pins = json.loads((WEB / "vendor/provenance.json").read_text())
    for name, digest in pins["files"].items():
        if hashlib.sha256((WEB / "vendor" / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Vendor checksum mismatch: {name}")
    vendor = (WEB / "vendor/SignalsmithStretch.js").read_text()
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
    template = (WEB / "template.html").read_text()
    for key, content in {
        "STYLE": (WEB / "style.css").read_text(),
        "VENDOR": vendor,
        "ARCHIVE": (WEB / "archive.js").read_text(),
        "APP": (WEB / "app.js").read_text(),
        "ENSEMBLE": (WEB / "ensemble.js").read_text(),
    }.items():
        template = template.replace(f"/*{key}*/", content)
    (WEB / "index.html").write_text(template)
    digest = hashlib.sha256(template.encode()).hexdigest()[:16]
    worker = f"""/* SPDX-License-Identifier: GPL-3.0-only */
const CACHE = 'magic-v2-{digest}';
const FILES = ['./', './index.html', './manifest.webmanifest', './icon-192.png', './icon-512.png'];
self.addEventListener('install', event => event.waitUntil(
  caches.open(CACHE).then(cache => cache.addAll(FILES))));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys =>
  Promise.all(keys.filter(k => k.startsWith('magic-v2-') && k !== CACHE)
    .map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', event => {{
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  event.respondWith(caches.match(event.request).then(saved => saved || fetch(event.request)));
}});
"""
    (WEB / "sw.js").write_text(worker)
    return WEB / "index.html"


if __name__ == "__main__":
    print(build())
