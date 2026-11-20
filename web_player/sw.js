/* SPDX-License-Identifier: GPL-3.0-only */
const CACHE = 'magic-v2-5d22494341cd84e5-verified';
async function installPlayer() {
  const page = await fetch('./index.html?rev=5d22494341cd84e5', {
    cache: 'reload', integrity: 'sha256-XSJJQ0HNhOX8ZsXiCFyFGqbVlQVCENShLdeJ6lqnCX0='
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
  Promise.all(keys.filter(k => k.startsWith('magic-v2-') && k !== CACHE)
    .map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  event.respondWith(caches.open(CACHE).then(cache => cache.match(event.request))
    .then(saved => saved || fetch(event.request)));
});
