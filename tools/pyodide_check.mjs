// Runs the engine in Pyodide under Node: loads samples, ranks orientations and checks each one.
// Catches crashes that only happen in Pyodide (older GEOS; a GEOS error there aborts the engine).
//
//   npm install --no-save --prefix tools pyodide@0.27.2     (once)
//   node tools/pyodide_check.mjs [sample,sample,...]
import { loadPyodide } from "pyodide";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const samples = (process.argv[2] || fs.readdirSync(`${root}/web/samples`).filter((f) => f.endsWith(".stl")).map((f) => f.slice(0, -4)).join(",")).split(",");
const py = await loadPyodide();
await py.loadPackage(["numpy", "shapely", "pyyaml"]);
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
for (const s of samples) {
  const t = performance.now();
  call("load", { name: s }, new Uint8Array(fs.readFileSync(`${root}/web/samples/${s}.stl`)));
  const { orientations } = call("orientations", { profile: {} });
  const counts = orientations.map((o) => call("check", { up: o.up, profile: {}, mode: "both" }).findings.length);
  console.log(`ok ${s}: ${orientations.length} orientations, findings ${counts.join("/")}, ${Math.round(performance.now() - t)} ms`);
}
