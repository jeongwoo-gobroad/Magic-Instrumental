/* SPDX-License-Identifier: GPL-3.0-only */
const VERSION = '3.0.1';
const BUILD = 'e270f391b0be60d247452c0d3f3077ae42d150b97f4cf05f185e89bae02d7085';
const CACHE_PREFIX = 'magic-player:' + encodeURIComponent(self.registration.scope) + ':';
const CACHE = CACHE_PREFIX + '4477274c40cc7f1e-verified';
async function installPlayer() {
  const page = await fetch('./index.html?rev=4477274c40cc7f1e', {
    cache: 'reload', integrity: 'sha256-RHcnTEDMfx6ph9b/TVdI2uurHp6YFJCNhBl+5SQWMco='
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
