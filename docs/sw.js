// v2: network-first for BOTH the shell and data.json (cache only as offline fallback).
// v1 served index.html cache-first, which froze the UI at install time — bad.
const CACHE = "farehunter-v2";
const SHELL = ["./", "index.html", "manifest.webmanifest"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks =>
    Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok && e.request.method === "GET") {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return r;
    }).catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
