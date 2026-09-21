/* Nerve service worker.
 *
 * Its job is narrow: make the app installable and make a cold launch from the
 * home screen instant. It is NOT an offline mode. Nerve is a gateway to an
 * agent running in the cluster — with no network there is nothing to say, so
 * the offline case is a shell that renders and then reports a dead socket.
 *
 * Three rules, and the reasoning matters more than the code:
 *
 *  1. API and websocket traffic is never touched. A cached agent reply is a
 *     lie, and a cached 401 is unrecoverable without clearing site data.
 *  2. /assets/* is cache-first. Vite content-hashes those filenames, so a
 *     given URL's bytes never change; a new build produces new URLs.
 *  3. Navigations are network-first. index.html is the one file whose name is
 *     stable across builds, so serving it from cache by preference would pin
 *     the app to whatever shell was cached at install time and keep it there
 *     after every deploy. Cache is the fallback, not the source.
 */

const VERSION = 'nerve-v1';
const SHELL = `${VERSION}-shell`;
const ASSETS = `${VERSION}-assets`;

// Take over immediately rather than waiting for every tab to close. The
// alternative is a phone that sits on last week's build indefinitely, because
// an installed PWA's "tab" is almost never closed.
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL)
      .then((cache) => cache.addAll(['/', '/manifest.webmanifest']))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  // Cross-origin, the API, and the websocket all pass straight through.
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/ws')) return;
  if (url.pathname.startsWith('/mcp')) return;

  // Rule 3 — navigations: network first, cached shell as the fallback.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(SHELL).then((cache) => cache.put('/', copy));
          return response;
        })
        .catch(() => caches.match('/', { ignoreSearch: true })),
    );
    return;
  }

  // Rule 2 — hashed build output: cache first, it cannot go stale.
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(
      caches.match(request).then(
        (hit) =>
          hit ||
          fetch(request).then((response) => {
            if (response.ok) {
              const copy = response.clone();
              caches.open(ASSETS).then((cache) => cache.put(request, copy));
            }
            return response;
          }),
      ),
    );
  }

  // Everything else (icons, the manifest, uploads) falls through to the
  // network unhandled. Deliberate: they are small, rare, or must be fresh.
});
