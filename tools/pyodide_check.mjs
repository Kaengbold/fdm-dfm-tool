// Runs the engine in Pyodide under Node: loads samples, ranks orientations, checks each one and
// suggests design edits for the best one.
// Catches crashes that only happen in Pyodide (older GEOS; a GEOS error there aborts the engine).
//
//   npm install --no-save --prefix tools pyodide@0.27.2     (once)
//   node tools/pyodide_check.mjs [sample,sample,...]   (or paths to .stl/.3mf files, e.g. to time big parts)
import { loadPyodide } from "pyodide";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const arg = process.argv[2] || fs.readdirSync(`${root}/web/samples`).filter((f) => f.endsWith(".stl")).map((f) => f.slice(0, -4)).join(",");
const files = arg.split(",").map((s) => (/\.(stl|3mf)$/i.test(s) ? s : `${root}/web/samples/${s}.stl`));
const py = await loadPyodide();
await py.loadPackage(["numpy", "shapely", "pyyaml", "scipy"]);
for (const name of JSON.parse(fs.readFileSync(`${root}/core/fdmcore/manifest.json`, "utf8"))) {
  const file = `/home/pyodide/fdmcore/${name}`;
  py.FS.mkdirTree(file.slice(0, file.lastIndexOf("/")));
  py.FS.writeFile(file, fs.readFileSync(`${root}/core/fdmcore/${name}`, "utf8"));
}
py.runPython("import sys; sys.path.insert(0, '/home/pyodide')");
const dispatch = py.pyimport("fdmcore.api").dispatch;
const call = (method, args = {}, data = null) => {
  const out = dispatch(method, JSON.stringify(args), data);
  if (!out?.toJs) return out;
  const r = out.toJs({ dict_converter: Object.fromEntries });
  out.destroy();
  return r;
};
const secs = (t) => ((performance.now() - t) / 1000).toFixed(1);
for (const file of files) {
  const name = path.basename(file);
  let t = performance.now();
  const info = call("load", { name }, new Uint8Array(fs.readFileSync(file)));
  const load = secs(t);
  t = performance.now();
  const { orientations } = call("orientations", { profile: {} });
  const orient = secs(t);
  t = performance.now();
  const counts = orientations.map((o) => call("check", { up: o.up, profile: {}, mode: "both" }).findings.length);
  const check = (secs(t) / orientations.length).toFixed(1);
  t = performance.now();
  const { edits } = call("design", { up: orientations[0].up, profile: { material: "abs" } });
  const design = secs(t);
  console.log(`ok ${name}: ${info.faces} faces, ${orientations.length} orientations, findings ${counts.join("/")}, ${edits.length} edits | load ${load} s, orient ${orient} s, check ${check} s each, design ${design} s`);
}
