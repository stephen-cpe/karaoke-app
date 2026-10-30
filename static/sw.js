// KARAOKE APP — phone remote service worker.
// Caches the remote shell only. Session state, songs, lyrics, and transcoded
// audio always come from the network so stale data can never control the TV.

const CACHE_VERSION = "karaoke-remote-v3";
const CORE_ASSETS = [
  "/remote",
  "/manifest.webmanifest",
  "/static/remote.css",
  "/static/remote.js",
  "/static/confirm-dialog.css",
  "/static/confirm-dialog.js",
  "/static/logo.webp",
  "/static/favicon.ico",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/icon-maskable-512.png",
  "/static/apple-touch-icon.png"
];

const NETWORK_ONLY_PREFIXES = [
  "/api/",
  "/lyrics/",
  "/songs/",
  "/transposed/"
];

function isRemotePage(pathname) {
  // The host page at "/" is not part of the remote shell and must never fall
  // back to the phone UI when offline.
  return pathname === "/remote" || pathname === "/remote/";
}

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then(cache => cache.addAll(CORE_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key !== CACHE_VERSION)
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (NETWORK_ONLY_PREFIXES.some(prefix => url.pathname.startsWith(prefix))) return;
  if (url.pathname === "/transpose" || url.pathname.startsWith("/transpose/")) return;

  // Pages: always try the network first, then fall back to the remote shell.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then(response => {
          if (response.ok && isRemotePage(url.pathname)) {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then(cache => cache.put("/remote", copy));
          }
          return response;
        })
        .catch(() => {
          if (isRemotePage(url.pathname)) {
            return caches.match("/remote").then(cached =>
              cached || new Response("Offline", {
                status: 503,
                headers: { "Content-Type": "text/plain; charset=utf-8" }
              })
            );
          }
          return Response.error();
        })
    );
    return;
  }

  // Scripts/styles: network first so a deploy is picked up immediately.
  if (url.pathname === "/static/remote.js" ||
      url.pathname === "/static/remote.css" ||
      url.pathname === "/static/confirm-dialog.js" ||
      url.pathname === "/static/confirm-dialog.css") {
    event.respondWith(
      fetch(request)
        .then(response => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then(cache => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  // Other local static assets: cache first, refresh in the background.
  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(
      caches.match(request).then(cached => {
        const network = fetch(request)
          .then(response => {
            if (response && response.ok) {
              const copy = response.clone();
              caches.open(CACHE_VERSION).then(cache => cache.put(request, copy));
            }
            return response;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
  }
});
