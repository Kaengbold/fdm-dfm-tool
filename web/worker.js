// Runs the Python engine (fdmcore) inside Pyodide, off the UI thread.
// Protocol: {id, method, args, data?} -> {id, result} | {id, error}; plus {type: "ready"} once booted
// and {type: "progress", id, fraction, text} while a call runs. To cancel, the page kills the worker.
import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v0.27.2/full/pyodide.mjs";

const ENGINE = new URL("../core/fdmcore/", import.meta.url);

async function fetchEngineFiles() {
  // Always revalidate the manifest so a deploy's new hashes are seen right away;
  // offline, sw.js serves the last manifest it saw via networkFirst.
  const manifestRes = await fetch(new URL("manifest.json", ENGINE), { cache: "no-cache" });
  if (!manifestRes.ok) throw new Error("Could not load engine manifest");
  const manifest = await manifestRes.json();
  const entries = Array.isArray(manifest) ? manifest.map((name) => [name, null]) : Object.entries(manifest);
  return Promise.all(entries.map(async ([name, hash]) => {
    // Each URL carries its content hash, so unchanged files can come straight from cache.
    const url = hash ? new URL(`${name}?v=${hash}`, ENGINE) : new URL(name, ENGINE);
    const res = await fetch(url, { cache: hash ? "force-cache" : "no-cache" });
    if (!res.ok) throw new Error(`Could not load engine file ${name}`);
    return [name, await res.text()];
  }));
}

async function boot() {
  const py = await loadPyodide();
  // Overlap engine file fetches with Pyodide package loading; starting after loadPyodide also
  // gives sw.js time to claim this worker on a first visit so files land in the offline cache.
  const [, files] = await Promise.all([
    py.loadPackage(["numpy", "shapely", "pyyaml"]),
    fetchEngineFiles(),
  ]);
  for (const [name, text] of files) {
    const path = `/home/pyodide/fdmcore/${name}`;
    py.FS.mkdirTree(path.slice(0, path.lastIndexOf("/")));
    py.FS.writeFile(path, text);
  }
  py.runPython("import sys; sys.path.insert(0, '/home/pyodide')");
  py.pyimport("fdmcore.progress").set_sink(progress);
  return { py, dispatch: py.pyimport("fdmcore.api").dispatch };
}

let current = null; // id of the call being run
let lastPost = 0;

/** Called from Python; throttled so a fast loop doesn't flood the page. */
function progress(fraction, text) {
  const now = performance.now();
  if (now - lastPost < 100) return;
  lastPost = now;
  postMessage({ type: "progress", id: current, fraction, text });
}

const ready = boot();
ready.then(
  () => postMessage({ type: "ready" }),
  (err) => postMessage({ type: "boot-error", error: String(err) }),
);

// Methods that need scipy; it is a big download, so it's fetched on first use.
const NEEDS_SCIPY = new Set(["clearance", "regions", "set_gap", "set_fastener", "fastener_suggestions", "snap_model", "stronger_orientation", "strength", "strength_variant", "strength_options", "optimise_strength", "optimize", "design", "tag_feature", "warping", "dynamics", "box_heat", "topology", "heat_model", "importance", "optimize_cooling"]);
// Loads and marked faces need face regions (scipy) too.
const marksFaces = (args) => args?.faces && Object.values(args.faces).some((ids) => ids?.length);
const needsScipy = (method, args) =>
  NEEDS_SCIPY.has(method) || (method === "orientations" && (args?.loads?.length || marksFaces(args))) || (method === "check" && marksFaces(args)) || (method === "export" && marksFaces(args)) ||
  (method === "material_advice" && args?.loads?.length);

onmessage = async ({ data: { id, method, args, data } }) => {
  try {
    const { py, dispatch } = await ready;
    if (needsScipy(method, args)) await py.loadPackage("scipy");
    current = id;
    lastPost = 0;
    const out = dispatch(method, JSON.stringify(args ?? {}), data ?? null);
    let result = out;
    if (out?.toJs) {
      result = out.toJs({ dict_converter: Object.fromEntries });
      out.destroy();
    }
    postMessage({ id, result });
  } catch (err) {
    // Python tracebacks are long; the last line is the useful one.
    const msg = String(err.message ?? err).trim().split("\n").pop();
    // A C++ error (GEOS) aborts Pyodide for good: the page has to start a fresh engine.
    postMessage({ id, error: msg, fatal: Boolean(err.pyodide_fatal_error) });
  }
};
