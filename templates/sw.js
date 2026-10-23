/* RIO x SOCIAL MEDIA DOWNLOADER - service worker (app shell cache).
   Downloads, lookups and the counter are never cached: they always go to the server. */
const VERSION = 'rio-{{ v }}';
const SHELL = [
  '/',
  '/static/style.css?v={{ v }}',
  '/static/app.js?v={{ v }}',
  '/static/ghost.png?v={{ v }}',
  '/static/favicon.png?v={{ v }}',
  '/static/icons/icon-192.png',
  '/static/icons/apple-touch-icon.png'
];
const LIVE = ['/api/', '/dl/', '/file/', '/healthz', '/sw.js', '/manifest.webmanifest'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      .then((cache) => Promise.all(SHELL.map((u) => cache.add(u).catch(() => {}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;                 // fonts etc. stay with the browser
  if (LIVE.some((p) => url.pathname.startsWith(p))) return;        // never cache live data or downloads
  if (req.headers.has('range')) return;

  // Pages: network first so a new deploy shows up at once, cached copy when offline.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok && url.pathname === '/') { const copy = res.clone(); caches.open(VERSION).then((c) => c.put('/', copy)); }
          return res;
        })
        .catch(() => caches.match('/').then((hit) => hit || new Response('You are offline.', { status: 503, headers: { 'Content-Type': 'text/plain' } })))
    );
    return;
  }

  // Static files: cache first (their URLs carry the version).
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
        return res;
      }))
    );
  }
});
