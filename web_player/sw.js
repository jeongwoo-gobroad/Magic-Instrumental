/* SPDX-License-Identifier: GPL-3.0-only */
const VERSION = '3.0.0';
const BUILD = 'a2ffc585a652f5e2f7f3784801a12abb194308e03d12323e651942f5d1eb429a';
const CACHE_PREFIX = 'magic-player:' + encodeURIComponent(self.registration.scope) + ':';
const CACHE = CACHE_PREFIX + '5aeeae702356f3f1-verified';
async function installPlayer() {
  const page = await fetch('./index.html?rev=5aeeae702356f3f1', {
    cache: 'reload', integrity: 'sha256-Wu6ucCNW8/He+GCWW+bvutdBau0zF9rvUN6n9Z6gvEk='
  });
  if (!page.ok) throw new Error('The new player is unavailable.');
  const cache = await caches.open(CACHE);
  await cache.addAll(['./manifest.webmanifest', './icon-192.png', './icon-512.png']
    .map(path => new Request(path, {cache: 'reload'})));
  await Promise.all([cache.put('./', page.clone()), cache.put('./index.html', page)]);
  await self.skipWaiting();
}
self.addEventListener('install', event => event.waitUntil(installPlayer()));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys =>
  Promise.all(keys.filter(k => k.startsWith(CACHE_PREFIX) && k !== CACHE)
    .map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('message', event => {
  if (event.data?.type === 'MAGIC_PLAYER_VERSION' && event.ports?.[0])
    event.ports[0].postMessage({version: VERSION, build: BUILD});
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  event.respondWith(caches.open(CACHE).then(cache => cache.match(event.request))
    .then(saved => saved || fetch(event.request)));
});
