// Runs the Python engine (fdmcore) inside Pyodide, off the UI thread.
// Protocol: {id, method, args, data?} -> {id, result} | {id, error}; plus {type: "ready"} once booted
// and {type: "progress", id, fraction, text} while a call runs. To cancel, the page kills the worker.
import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v0.27.2/full/pyodide.mjs";

const ENGINE = new URL("../core/fdmcore/", import.meta.url);

async function boot() {
  const py = await loadPyodide();
  await py.loadPackage(["numpy", "shapely", "pyyaml"]);
  const files = await (await fetch(new URL(`manifest.json?v=${Date.now()}`, ENGINE))).json();
  for (const name of files) {
    const res = await fetch(new URL(`${name}?v=${Date.now()}`, ENGINE));
    if (!res.ok) throw new Error(`Could not load engine file ${name}`);
    const path = `/home/pyodide/fdmcore/${name}`;
    py.FS.mkdirTree(path.slice(0, path.lastIndexOf("/")));
    py.FS.writeFile(path, await res.text());
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
const NEEDS_SCIPY = new Set(["regions", "set_gap", "snap_model", "strength", "strength_variant", "strength_options", "optimise_strength", "design", "tag_feature", "warping", "dynamics", "box_heat", "topology"]);
// Loads and marked faces need face regions (scipy) too.
const marksFaces = (args) => args?.faces && Object.values(args.faces).some((ids) => ids?.length);
const needsScipy = (method, args) =>
  NEEDS_SCIPY.has(method) || (method === "orientations" && (args?.loads?.length || marksFaces(args))) || (method === "check" && marksFaces(args)) ||
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
    postMessage({ id, error: msg });
  }
};
