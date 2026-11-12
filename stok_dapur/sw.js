/* Offline app shell. API calls (supabase.co) always go to the network; the
   app keeps its own offline queue in localStorage. Bump VERSION on deploy. */
const VERSION = "stok-v1";
const SHELL = ["./", "index.html", "style.css", "config.js", "core.js", "app.js", "manifest.webmanifest",
  "icons/icon-192.png", "icons/icon-512.png", "panduan.html"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.endsWith("office.html") || url.pathname.endsWith("office.js")) return; // office is online-only
  // network first (fresh deploys reach the kitchen), cache as fallback offline
  e.respondWith(fetch(e.request).then((r) => {
    const copy = r.clone();
    caches.open(VERSION).then((c) => c.put(e.request, copy));
    return r;
  }).catch(() => caches.match(e.request).then((r) => r || caches.match("index.html"))));
});
