/* TubeGrab service worker. Made by Jhames Martin.
 * - The page loads network-first, so a new deploy shows up right away; the cached copy is used offline.
 * - Icons, fonts and the manifest are cache-first.
 * - /api/* (video info, downloads) always goes to the network and is never cached.
 */
const VERSION = "tubegrab-v6";
const SHELL = ["/", "/offline", "/static/manifest.webmanifest", "/static/icons/icon-192.png", "/static/icons/icon-512.png", "/static/icons/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "skipWaiting") self.skipWaiting();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // Never touch downloads, API calls or YouTube thumbnails.
  if (url.origin === location.origin && (url.pathname.startsWith("/api/") || url.pathname === "/share")) return;

  // Pages: network first, cached shell when offline.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok && url.pathname === "/") {
            const copy = res.clone();
            caches.open(VERSION).then((c) => c.put("/", copy));
          }
          return res;
        })
        .catch(async () => (await caches.match("/")) || (await caches.match("/offline")))
    );
    return;
  }

  // Static files and Google Fonts: cache first, then network (and remember it).
  const isStatic = url.origin === location.origin && url.pathname.startsWith("/static/");
  const isFont = url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com";
  if (isStatic || isFont) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok || res.type === "opaque") {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(req, copy));
        }
        return res;
      }))
    );
  }
});
