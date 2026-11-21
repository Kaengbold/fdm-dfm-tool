// Offline support: after a visit, the app, the Python engine and the libraries it downloaded
// (Pyodide packages, three.js, manifold, OpenCascade, the Muon3D browser slicer) come from this
// cache when the network is gone. Library files have their version in the URL (on the CDN, or
// same-origin under /vendor/ on the self-hosted build), and engine files carry their content hash
// in ?v=<hash>, so unchanged files are served cache-first; the app's own files and manifest.json
// are fetched fresh when online so updates show up immediately.
const CDN = "fdm-cdn-v1";
const ENGINE = "fdm-engine-v1";
const APP = "fdm-app-v1";
const CDN_HOSTS = ["cdn.jsdelivr.net"];

const isHashedEngineFile = (url) =>
  url.origin === self.location.origin &&
  url.pathname.includes("/core/fdmcore/") &&
  !url.pathname.endsWith("/manifest.json") &&
  Boolean(url.searchParams.get("v"));

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil((async () => {
  // A renamed cache (new name above) leaves the old one behind otherwise: ~100 MB each.
  for (const name of await caches.keys()) if (![CDN, ENGINE, APP].includes(name)) await caches.delete(name);
  await self.clients.claim();
})()));

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (CDN_HOSTS.includes(url.hostname) || (url.origin === self.location.origin && url.pathname.includes("/vendor/"))) {
    e.respondWith(cacheFirst(req, e, CDN));
  } else if (isHashedEngineFile(url)) {
    e.respondWith(cacheFirst(req, e, ENGINE));
  } else if (url.origin === self.location.origin) e.respondWith(networkFirst(req));
});

// The same versioned file without its version or content hash: "npm/three/build/three.module.js",
// "pyodide/full/pyodide.asm.wasm", "vendor/muon3d-slicer-engine/engine-st.wasm",
// "vendor/muon3d-slicer-engine/host.js", "engine/checks.py". null for URLs that carry no version.
function unversioned(url) {
  const u = new URL(url);
  const path = u.pathname;
  const npm = path.match(/\/npm\/((?:@[^/]+\/)?[^/@]+)@[^/]+(\/.*)$/);
  if (npm) return "npm/" + npm[1] + npm[2];
  const py = path.match(/\/pyodide\/v[^/]+(\/.*)$/);
  if (py) return "pyodide" + py[1];
  const vend = path.match(/\/vendor\/([^/@]+)@[^/]+(\/.*)$/);
  if (vend) return "vendor/" + vend[1] + vend[2].replace(/\.[0-9a-f]{12,}\.js$/, ".js");
  const eng = u.searchParams.get("v") && path.match(/\/core\/fdmcore\/(.+)$/);
  return eng ? "engine/" + eng[1] : null;
}

async function cacheFirst(req, event, cacheName = CDN) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) {
    await cache.put(req, res.clone());
    event.waitUntil(dropOldVersions(cache, req.url));
  }
  return res;
}

// A library or engine update fetches files under new URLs: drop the older cached copy of the same file.
async function dropOldVersions(cache, url) {
  const file = unversioned(url);
  if (!file) return;
  for (const old of await cache.keys()) {
    if (old.url !== url && unversioned(old.url) === file) await cache.delete(old);
  }
}

async function networkFirst(req) {
  const cache = await caches.open(APP);
  // One entry per file (ignoring any query string) so offline fallback always matches.
  const url = new URL(req.url);
  const key = url.origin + url.pathname;
  try {
    // Revalidate with the server: otherwise the browser's HTTP cache can hand back an old module
    // next to a new one (e.g. a new app.js calling a viewer.js function that isn't there yet, or
    // a stale manifest.json pointing at outdated engine hashes).
    const res = await (req.mode === "navigate" ? fetch(req.url, { cache: "no-cache" }) : fetch(req, { cache: "no-cache" }));
    if (res.ok) cache.put(key, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(key);
    if (hit) return hit;
    throw err;
  }
}
