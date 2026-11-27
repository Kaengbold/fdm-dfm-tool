"""Unit and parity tests for the browser OrcaSlicer integration (T-525).

Runs fully offline without downloading the engine or launching a browser:
- tools/vendor.py Muon3D tarball pinning, extraction, cache hit, and tamper rejection
- web/slicer-worker.js integrity pins matching tools/vendor.py
- web/sw.js cache-eviction keys for versioned /vendor/muon3d-slicer-engine@<ver>/ files
- 100% parity between JS matchOrcaProfiles/bedType and Python slicer_cli across all presets
- input-hash caching, settings routing, cancellation, and fallback reasons via a mock worker
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from fdmcore.profile import presets  # noqa: E402
from fdmcore.slicer_cli import FILAMENT_TYPE, bed_type, bundled_profiles, match_profiles  # noqa: E402
import vendor  # noqa: E402

NODE = shutil.which("node")


def _run_node(script: str, *args: str) -> str:
    if not NODE:
        pytest.skip("node not installed")
    proc = subprocess.run([NODE, "--input-type=module", "-e", script, "--", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"node exited {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc.stdout


def test_vendor_muon3d_extracts_and_rejects_tampering(tmp_path, monkeypatch):
    """fetch_muon3d extracts only MUON3D_FILES, verifies tarball & file sha256s, and rejects tampering."""
    contents = {name: f"synthetic-{name}\n".encode() for name in vendor.MUON3D_FILES}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in contents.items():
            info = tarfile.TarInfo(vendor.MUON3D_PREFIX + name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        # Extra file (e.g. engine-mt.wasm) must NOT be extracted.
        extra = tarfile.TarInfo(vendor.MUON3D_PREFIX + "engine-mt.wasm")
        extra.size = 4
        tf.addfile(extra, io.BytesIO(b"skip"))
    tgz_bytes = buf.getvalue()
    tgz_path = tmp_path / "muon3d.tgz"
    tgz_path.write_bytes(tgz_bytes)

    fake_hashes = dict(vendor.HASHES)
    for name, data in contents.items():
        fake_hashes[f"{vendor.MUON3D_DIR}/{name}"] = hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(vendor, "HASHES", fake_hashes)
    monkeypatch.setattr(vendor, "MUON3D_TARBALL_SHA256", hashlib.sha256(tgz_bytes).hexdigest())

    out = tmp_path / "vendor"
    vendor.fetch_muon3d(out, tarball=tgz_path)
    dest = out / vendor.MUON3D_DIR
    assert sorted(p.name for p in dest.iterdir()) == sorted(vendor.MUON3D_FILES)
    assert not (dest / "engine-mt.wasm").exists()

    # Cached run succeeds without needing the tarball again.
    vendor.fetch_muon3d(out, tarball=tmp_path / "missing.tgz")

    # Tampering with a cached file must fail and delete the tampered file.
    (dest / "engine-st.wasm").write_bytes(b"tampered")
    with pytest.raises(SystemExit):
        vendor.fetch_muon3d(out, tarball=tgz_path)
    assert not (dest / "engine-st.wasm").exists()

    # Tampering with the tarball itself must fail before extracting.
    bad_tgz = tmp_path / "bad.tgz"
    bad_tgz.write_bytes(tgz_bytes + b"x")
    with pytest.raises(SystemExit):
        vendor.fetch_muon3d(tmp_path / "vendor2", tarball=bad_tgz)


def test_slicer_worker_pins_match_vendor_py():
    """Every pinned sha256 & SRI integrity hash in web/slicer-worker.js matches tools/vendor.py."""
    text = (ROOT / "web" / "slicer-worker.js").read_text(encoding="utf-8")
    assert f'ENGINE_VERSION = "{vendor.MUON3D_VERSION}"' in text
    host_file = next(name for name in vendor.MUON3D_FILES if name.startswith("host."))
    for fname in ("manifest.json", host_file, "engine-st.mjs", "engine-st.wasm"):
        hex_hash = vendor.HASHES[f"{vendor.MUON3D_DIR}/{fname}"]
        sri = "sha256-" + base64.b64encode(bytes.fromhex(hex_hash)).decode()
        assert hex_hash in text, fname
        assert sri in text, fname


def test_sw_unversioned_handles_vendored_slicer():
    """web/sw.js drops old cached Muon3D versions (including content-hashed host chunks) on upgrade."""
    sw_path = (ROOT / "web" / "sw.js").as_posix()
    script = """
import fs from "node:fs";
const src = fs.readFileSync(process.argv[1], "utf8");
const m = src.match(/function unversioned\\(url\\) \\{[\\s\\S]*?\\n\\}/);
if (!m) throw new Error("unversioned() not found in sw.js");
const unversioned = new Function("url", m[0] + "\\nreturn unversioned(url);");
const cases = [
  "https://example.com/vendor/muon3d-slicer-engine@0.6.0-m1.1/engine-st.wasm",
  "https://example.com/vendor/muon3d-slicer-engine@0.7.0/engine-st.wasm",
  "https://example.com/vendor/muon3d-slicer-engine@0.6.0-m1.1/host.446c6d131da98dbf.js",
  "https://example.com/vendor/muon3d-slicer-engine@0.7.0/host.abcdef0123456789.js",
  "https://example.com/vendor/muon3d-slicer-engine@0.6.0-m1.1/host-chunk.bd6a59f31a9394b2.js",
  "https://example.com/vendor/npm/three@0.170.0/build/three.module.js",
  "https://example.com/vendor/pyodide/v0.27.2/full/pyodide.asm.wasm",
];
console.log(JSON.stringify(cases.map((u) => unversioned(u))));
"""
    got = json.loads(_run_node(script, sw_path))
    assert got[0] == "vendor/muon3d-slicer-engine/engine-st.wasm"
    assert got[0] == got[1]
    assert got[2] == "vendor/muon3d-slicer-engine/host.js"
    assert got[2] == got[3]
    assert got[4] == "vendor/muon3d-slicer-engine/host-chunk.js"
    assert got[5] == "npm/three/build/three.module.js"
    assert got[6] == "pyodide/full/pyodide.asm.wasm"


def test_js_profile_matching_matches_python_for_all_presets(tmp_path):
    """JS matchOrcaProfiles + bedType pick the exact same profiles as Python slicer_cli for every preset."""
    profiles = bundled_profiles()
    expected = {}
    cases = []
    for printer in presets()["printers"].values():
        for material in FILAMENT_TYPE:
            names = match_profiles(profiles, printer["name"], "0.4", material, 0.2)
            key = f"{printer['name']}|0.4|{material}"
            expected[key] = {**names, "bed": bed_type(profiles[names["filament"]]["data"])}
            cases.append([printer["name"], "0.4", material, 0.2])

    worker_mjs = tmp_path / "slicer-worker.mjs"
    shutil.copyfile(ROOT / "web" / "slicer-worker.js", worker_mjs)
    worker_url = worker_mjs.as_uri()
    profiles_gz = str(ROOT / "core" / "fdmcore" / "data" / "orca_profiles.json.gz")
    script = """
import fs from "node:fs";
import zlib from "node:zlib";
const { matchOrcaProfiles, bedType } = await import(process.argv[1]);
const snap = JSON.parse(zlib.gunzipSync(fs.readFileSync(process.argv[2])).toString("utf8"));
const cases = JSON.parse(process.argv[3]);
const out = {};
for (const [printer, nozzle, material, lh] of cases) {
  const m = matchOrcaProfiles(snap, printer, nozzle, material, lh);
  out[`${printer}|${nozzle}|${material}`] = {
    machine: m.machine.name,
    process: m.process.name,
    filament: m.filament.name,
    bed: bedType(m.filament.data),
  };
}
console.log(JSON.stringify(out));
"""
    got = json.loads(_run_node(script, worker_url, profiles_gz, json.dumps(cases)))
    assert got == expected


def test_slicer_worker_cache_overrides_and_fallbacks(tmp_path):
    """Verify settings routing, input-hash caching, cancellation, and all fallback reasons with a mock worker."""
    worker_mjs = tmp_path / "slicer-worker.mjs"
    shutil.copyfile(ROOT / "web" / "slicer-worker.js", worker_mjs)
    worker_url = worker_mjs.as_uri()
    profiles_gz = str(ROOT / "core" / "fdmcore" / "data" / "orca_profiles.json.gz")
    script = """
import fs from "node:fs";
import zlib from "node:zlib";
import { EventEmitter } from "node:events";

const mod = await import(process.argv[1]);
const snap = JSON.parse(zlib.gunzipSync(fs.readFileSync(process.argv[2])).toString("utf8"));

// 1. Settings override routing (process scalar, process vector, filament vector)
const chosen = mod.matchOrcaProfiles(snap, "Bambu Lab P1S", "0.4", "pla", 0.2);
const cfgs = mod.applySettingsOverrides(chosen, {
  wall_loops: 4,
  sparse_infill_density: "25%",
  bridge_speed: 25,
  slow_down_for_layer_cooling: 1,
});
if (cfgs.process.wall_loops !== "4") throw new Error("wall_loops");
if (cfgs.process.sparse_infill_density !== "25%") throw new Error("infill");
if (!Array.isArray(cfgs.process.bridge_speed) || cfgs.process.bridge_speed[0] !== "25") throw new Error("bridge_speed");
if (!Array.isArray(cfgs.filaments[0].slow_down_for_layer_cooling) || cfgs.filaments[0].slow_down_for_layer_cooling[0] !== "1") {
  throw new Error("slow_down_for_layer_cooling");
}

// 2. Mock worker tests: caching, progress, fallback on Orca error, OOM -> too_big, and cancellation
const cube = new Float32Array([
  0,0,0, 10,0,0, 10,10,0,  0,0,0, 10,10,0, 0,10,0,
  0,0,10, 10,10,10, 10,0,10,  0,0,10, 0,10,10, 10,10,10,
]);

let sliceCalls = 0;
let mode = "ok";
class MockWorker extends EventEmitter {
  postMessage(msg) {
    queueMicrotask(() => {
      if (msg.op === "hello") {
        this.emit("message", { v: 2, id: msg.id, kind: "result", result: {
          engine: { name: "Mock", version: mod.ENGINE_VERSION, orca: { version: "2.5.0-dev" }, license: "AGPL-3.0-only" },
          limits: { maxHeapBytes: 4e9 },
        }});
      } else if (msg.op === "load") {
        this.emit("message", { v: 2, id: msg.id, kind: "result", result: { variant: "st", initMs: 12 } });
      } else if (msg.op === "config.definitions") {
        this.emit("message", { v: 2, id: msg.id, kind: "result", result: { presetKeys: { filament: ["slow_down_for_layer_cooling"] }, options: {} } });
      } else if (msg.op === "slice") {
        sliceCalls++;
        if (mode === "hang") return; // waits for cancel
        if (mode === "oom") {
          this.emit("message", { v: 2, id: msg.id, kind: "error", error: { code: 2, message: "Out of memory" } });
          return;
        }
        if (mode === "outside") {
          this.emit("message", { v: 2, id: msg.id, kind: "error", error: { code: -52, message: "Part outside plate" } });
          return;
        }
        this.emit("message", { v: 2, id: msg.id, kind: "progress", progress: { percent: 50, stage: "slice", message: "Slicing" } });
        this.emit("message", { v: 2, id: msg.id, kind: "result", result: {
          gcode: new TextEncoder().encode("; generated by OrcaSlicer\\nG1 X1 Y1 E0.1\\n"),
          stats: { layers: 50, printTimeText: "10m", filamentG: 2.5 },
          warnings: [],
          timings: { total: 40 },
        }});
      }
    });
  }
  terminate() { this.terminated = true; }
}

const manifestJson = {
  version: mod.ENGINE_VERSION,
  host: { file: "host.js", sha256: mod.ENGINE_SHA256.host },
  variants: { st: { mjs: "engine-st.mjs", wasm: "engine-st.wasm", wasmBytes: 100, sha256: { mjs: mod.ENGINE_SHA256.mjs, wasm: mod.ENGINE_SHA256.wasm } } },
};

let okManifestFetches = 0;
const slicer = mod.createBrowserSlicer({
  engineBase: "http://127.0.0.1/vendor/muon3d/",
  loadProfiles: async () => snap,
  fetchImpl: async () => { okManifestFetches++; return { ok: true, status: 200, json: async () => manifestJson }; },
  workerFactory: () => new MockWorker(),
});

const seenProgress = [];
const r1 = await slicer.slice(
  { mesh: cube, printer: "Bambu Lab P1S", nozzle: "0.4", material: "pla", settings: { wall_loops: 3 } },
  { onProgress: (p) => seenProgress.push(p.percent) },
);
const r2 = await slicer.slice(
  { mesh: cube, printer: "Bambu Lab P1S", nozzle: "0.4", material: "pla", settings: { wall_loops: 3 } },
);
if (!r1.ok || r1.cached || !r2.ok || !r2.cached || sliceCalls !== 1 || !seenProgress.includes(50)) {
  throw new Error("cache/progress failed: " + JSON.stringify({ r1, r2, sliceCalls, seenProgress }));
}

// Too many triangles rejected before calling worker
const big = await slicer.slice({ mesh: cube, printer: "Bambu Lab P1S", nozzle: "0.4", material: "pla", maxTriangles: 2 });
if (big.ok || !big.fallback || big.reason !== "too_big") throw new Error("too_big failed");

// Orca negative error code -> fallback failed
mode = "outside";
const outErr = await slicer.slice({ mesh: cube, printer: "Bambu Lab P1S", nozzle: "0.4", material: "pla", settings: { wall_loops: 4 } });
if (outErr.ok || !outErr.fallback || outErr.reason !== "failed" || outErr.code !== -52) throw new Error("outside failed");

// Engine OOM code 2 -> fallback too_big
mode = "oom";
const oomErr = await slicer.slice({ mesh: cube, printer: "Bambu Lab P1S", nozzle: "0.4", material: "pla", settings: { wall_loops: 5 } });
if (oomErr.ok || !oomErr.fallback || oomErr.reason !== "too_big" || oomErr.code !== 2) throw new Error("oom failed");

// Cancellation via AbortController
mode = "hang";
const ac = new AbortController();
const pCancel = slicer.slice({ mesh: cube, printer: "Bambu Lab P1S", nozzle: "0.4", material: "pla", settings: { wall_loops: 6 } }, { signal: ac.signal });
setTimeout(() => ac.abort(), 10);
const canc = await pCancel;
if (canc.ok || !canc.cancelled || canc.reason !== "cancelled") throw new Error("cancel failed: " + JSON.stringify(canc));

// Unavailable engine (404): slice() and checkAvailable() share the cached manifest probe (T-633)
let deadManifestFetches = 0;
const dead = mod.createBrowserSlicer({
  engineBase: "http://127.0.0.1/vendor/missing/",
  loadProfiles: async () => snap,
  fetchImpl: async () => { deadManifestFetches++; return { ok: false, status: 404 }; },
});
const unavail = await dead.slice({ mesh: cube, printer: "Bambu Lab P1S", nozzle: "0.4", material: "pla" });
if (unavail.ok || !unavail.fallback || unavail.reason !== "unavailable") throw new Error("unavail failed");
const avail1 = await dead.checkAvailable();
const avail2 = await dead.checkAvailable();
if (avail1.available || avail2.available || deadManifestFetches !== 1) {
  throw new Error("checkAvailable cache failed: " + JSON.stringify({ avail1, avail2, deadManifestFetches }));
}

// Available engine: checkAvailable() after slice() also reuses the single manifest fetch
const okAvail1 = await slicer.checkAvailable();
const okAvail2 = await slicer.checkAvailable();
if (!okAvail1.available || !okAvail2.available || okManifestFetches !== 1) {
  throw new Error("ok checkAvailable cache failed: " + JSON.stringify({ okAvail1, okAvail2, okManifestFetches }));
}

// Unavailable engine (offline / SRI rejection throws TypeError)
const offline = mod.createBrowserSlicer({
  engineBase: "http://127.0.0.1/vendor/missing/",
  loadProfiles: async () => snap,
  fetchImpl: async () => { throw new TypeError("Failed to fetch"); },
});
const offRes = await offline.slice({ mesh: cube, printer: "Bambu Lab P1S", nozzle: "0.4", material: "pla" });
if (offRes.ok || !offRes.fallback || offRes.reason !== "unavailable") throw new Error("offline failed");

slicer.dispose();
console.log("OK");
"""
    assert _run_node(script, worker_url, profiles_gz).strip() == "OK"
