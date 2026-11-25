// Run each case/mode in a fresh Node process so previous solves can't raise its memory high-water mark.
// npm install --no-save --no-package-lock --prefix tools pyodide@0.27.8
// node tools/thermal_refine_bench.mjs <pin|bridge|transition> <quick|fine|refined>
import { loadPyodide } from "pyodide";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const [part, mode] = process.argv.slice(2);
if (!["pin", "bridge", "transition"].includes(part) || !["quick", "fine", "refined"].includes(mode)) {
  throw new Error("Expected a case (pin, bridge, transition) and a mode (quick, fine, refined)");
}
const read = (relative, encoding) => fs.readFileSync(fileURLToPath(new URL(relative, import.meta.url)), encoding);
const py = await loadPyodide();
await py.loadPackage(["numpy", "shapely", "pyyaml", "scipy"]);
const manifest = JSON.parse(read("../core/fdmcore/manifest.json", "utf8"));
for (const name of Array.isArray(manifest) ? manifest : Object.keys(manifest)) {
  const file = `/home/pyodide/core/fdmcore/${name}`;
  py.FS.mkdirTree(file.slice(0, file.lastIndexOf("/")));
  py.FS.writeFile(file, read(`../core/fdmcore/${name}`));
}
py.FS.mkdirTree("/home/pyodide/tools");
py.FS.writeFile("/home/pyodide/tools/thermal_refine_bench.py", read("./thermal_refine_bench.py"));
py.FS.writeFile("/pin.gcode.gz", read("../core/tests/gcode/orca.gcode.gz"));
py.globals.set("part", part);
py.globals.set("mode", mode);
py.runPython(`
import sys, json
from pathlib import Path
sys.path.insert(0, "/home/pyodide/tools")
import thermal_refine_bench as bench
inputs = bench.prepare(part, Path("/pin.gcode.gz").read_bytes())
`);
const peakRssMiB = () => process.resourceUsage().maxRSS / 1024;
const wasmMiB = () => py._module.HEAPU8.buffer.byteLength / 2 ** 20;
const baselinePeak = peakRssMiB();
const baselineWasm = wasmMiB();
const stats = JSON.parse(py.runPython("json.dumps(bench.measure(part, mode, inputs))"));
console.log(JSON.stringify({
  ...stats, baseline_peak_mib: baselinePeak, peak_rss_mib: peakRssMiB(),
  baseline_wasm_mib: baselineWasm, wasm_mib: wasmMiB(),
}));
