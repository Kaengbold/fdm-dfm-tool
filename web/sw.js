// Offline support: after a visit, the app, the Python engine and the libraries it downloaded
// (Pyodide packages, three.js, manifold, OpenCascade) come from this cache when the network is gone.
// CDN files are versioned in their URLs, so they are cached for good; the app's own files are
// fetched fresh when online, so updates show up.
const CDN = "fdm-cdn-v1";
const APP = "fdm-app-v1";
const CDN_HOSTS = ["cdn.jsdelivr.net"];

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (CDN_HOSTS.includes(url.hostname)) e.respondWith(cacheFirst(req));
  else if (url.origin === self.location.origin) e.respondWith(networkFirst(req));
});

async function cacheFirst(req) {
  const cache = await caches.open(CDN);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) cache.put(req, res.clone());
  return res;
}

async function networkFirst(req) {
  const cache = await caches.open(APP);
  // One entry per file: the engine loader adds ?v=<time> to every request to skip the HTTP cache.
  const url = new URL(req.url);
  const key = url.origin + url.pathname;
  try {
    // Revalidate with the server: otherwise the browser's HTTP cache can hand back an old module
    // next to a new one (e.g. a new app.js calling a viewer.js function that isn't there yet).
    const res = await (req.mode === "navigate" ? fetch(req.url, { cache: "no-cache" }) : fetch(req, { cache: "no-cache" }));
    if (res.ok) cache.put(key, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(key);
    if (hit) return hit;
    throw err;
  }
}
