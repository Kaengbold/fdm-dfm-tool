"""Headless browser test for the vendored browser OrcaSlicer (T-525).

Serves the repository locally (no external network at test time), loads ``web/slicer-worker.js``
in headless Edge/Chrome via Playwright, slices a sample STL in its dedicated Web Worker, checks
that the main thread stays responsive, verifies progress/caching/fallbacks, parses the resulting
G-code with ``core/fdmcore/gcode.py``, and compares time/grams against desktop OrcaSlicer when
installed.

Prerequisites:
    python tools/vendor.py --slicer-only vendor
    pip install playwright

Usage:
    python tools/test_browser_slicer.py [--all-samples] [--browser edge|chrome|chromium]
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import mimetypes
import re
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "tools"))

from fdmcore import gcode  # noqa: E402
from fdmcore.export3mf import export_3mf  # noqa: E402
from fdmcore.mesh import Mesh, load_stl  # noqa: E402
from fdmcore.slicer_cli import find_orca, parse_gcode, pick_profiles, slice_3mf  # noqa: E402
from vendor import MUON3D_DIR, MUON3D_VERSION  # noqa: E402

DEFAULT_SAMPLE = "l_bracket.stl"
ALL_SAMPLES = ["l_bracket.stl", "phone_stand.stl", "hole_block.stl", "enclosure.stl", "cable_clip.stl"]
TEST_SETTINGS = {
    "wall_loops": "3",
    "sparse_infill_density": "15%",
    "sparse_infill_pattern": "gyroid",
}


class QuietMjsHandler(http.server.SimpleHTTPRequestHandler):
    # Windows registries often map .mjs to text/plain, which breaks ES module imports in Workers.
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".wasm": "application/wasm",
    }

    def log_message(self, *args):
        pass


def serve_local() -> tuple[socketserver.ThreadingTCPServer, int]:
    mimetypes.add_type("text/javascript", ".mjs")
    mimetypes.add_type("application/wasm", ".wasm")
    handler = functools.partial(QuietMjsHandler, directory=str(ROOT))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def launch_browser(p, choice: str):
    if choice == "edge":
        return p.chromium.launch(channel="msedge", headless=True)
    if choice == "chrome":
        return p.chromium.launch(channel="chrome", headless=True)
    return p.chromium.launch(headless=True)


def gcode_grams(text: str) -> float:
    m = re.search(r"total filament used \[g\]\s*=\s*([\d.]+)", text[-60000:])
    if m:
        return float(m.group(1))
    meta = parse_gcode(text)
    return round(meta.get("filament_cm3", 0.0) * 1.24, 2)


def desktop_reference(sample_name: str, orca: Path) -> dict:
    import numpy as np

    mesh = load_stl((ROOT / "web" / "samples" / sample_name).read_bytes())
    lo, hi = mesh.bounds()
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    shifted = Mesh(mesh.triangles - np.array([cx, cy, lo[2]], dtype=np.float32))
    chosen = pick_profiles(None, "Bambu Lab P1S", "0.4", "pla", 0.2)
    threemf = export_3mf(shifted, name=sample_name, settings=TEST_SETTINGS, bed_center=(128.0, 128.0))
    res = slice_3mf(threemf, chosen, orca, keep_gcode=True)
    gc = res.pop("gcode")
    tp = gcode.parse(gc)
    return {
        "print_time_min": res["print_time_min"],
        "layers": res["layers"],
        "grams": gcode_grams(gc),
        "tp_lines": len(tp.p0),
        "tp_vol_cm3": round(float(tp.volume[tp.part()].sum()) / 1000, 2),
    }


BROWSER_DRIVER = """
async ({ samples, settings }) => {
  const mod = await import('/web/slicer-worker.js');

  function parseBinaryStl(buf) {
    const dv = new DataView(buf);
    const n = dv.getUint32(80, true);
    const out = new Float32Array(n * 9);
    for (let i = 0; i < n; i++) {
      const off = 84 + i * 50 + 12;
      for (let j = 0; j < 9; j++) out[i * 9 + j] = dv.getFloat32(off + j * 4, true);
    }
    return out;
  }

  // 1. Availability check
  const avail = await mod.checkAvailable();
  if (!avail.available) throw new Error('Expected browser slicer available, got: ' + JSON.stringify(avail));

  // 2. Fallback checks (missing engine URL, oversized mesh, unknown printer)
  const missingSlicer = mod.createBrowserSlicer({
    engineBase: new URL('/vendor/nonexistent-engine/', location.href).href,
  });
  const missingAvail = await missingSlicer.checkAvailable();
  const sampleBuf0 = await (await fetch('/web/samples/' + samples[0])).arrayBuffer();
  const sampleMesh0 = parseBinaryStl(sampleBuf0);

  const missingSlice = await missingSlicer.slice({
    mesh: sampleMesh0,
    name: samples[0],
    printer: 'Bambu Lab P1S',
    nozzle: '0.4',
    material: 'pla',
    settings,
  });
  const tooBigSlice = await mod.sliceInBrowser({
    mesh: sampleMesh0,
    name: samples[0],
    printer: 'Bambu Lab P1S',
    nozzle: '0.4',
    material: 'pla',
    settings,
    maxTriangles: 10,
  });
  const noProfileSlice = await mod.sliceInBrowser({
    mesh: sampleMesh0,
    name: samples[0],
    printer: 'Unknown Imaginary Printer 9000',
    nozzle: '0.4',
    material: 'pla',
    settings,
  });

  // 3. Lazy warmup measurement
  const warmT0 = performance.now();
  const warm = await mod.warmup();
  const warmMs = Math.round(performance.now() - warmT0);
  if (!warm.ok) throw new Error('Warmup failed: ' + JSON.stringify(warm));

  // 4. Slice requested sample(s), tracking main-thread responsiveness and progress
  const results = [];
  for (const name of samples) {
    const buf = await (await fetch('/web/samples/' + name)).arrayBuffer();
    const mesh = parseBinaryStl(buf);
    const progress = [];
    let ticks = 0;
    let maxGapMs = 0;
    let lastTick = performance.now();
    const timer = setInterval(() => {
      const now = performance.now();
      maxGapMs = Math.max(maxGapMs, now - lastTick);
      lastTick = now;
      ticks++;
    }, 50);

    const t0 = performance.now();
    const res = await mod.sliceInBrowser(
      {
        mesh,
        name,
        printer: 'Bambu Lab P1S',
        nozzle: '0.4',
        material: 'pla',
        layerHeight: 0.2,
        settings,
      },
      { onProgress: (p) => progress.push(p.percent) },
    );
    const wallMs = Math.round(performance.now() - t0);
    clearInterval(timer);

    if (!res.ok) throw new Error(`Slice of ${name} failed: ` + JSON.stringify(res));

    // Check cache hit on the first sample
    let cacheCheck = null;
    if (name === samples[0]) {
      const c0 = performance.now();
      const hit = await mod.sliceInBrowser({
        mesh,
        name,
        printer: 'Bambu Lab P1S',
        nozzle: '0.4',
        material: 'pla',
        layerHeight: 0.2,
        settings,
      });
      cacheCheck = {
        ok: hit.ok,
        cached: hit.cached,
        sameHash: hit.hash === res.hash,
        sameGcode: hit.gcode === res.gcode,
        elapsedMs: Math.round(performance.now() - c0),
      };
    }

    results.push({
      name,
      tris: mesh.length / 9,
      wallMs,
      timings: res.timings,
      stats: res.stats,
      profiles: res.profiles,
      engine: res.engine,
      hash: res.hash,
      byteLength: res.byteLength,
      progressCount: progress.length,
      progressMonotonic: progress.every((v, i) => i === 0 || v >= progress[i - 1]),
      mainThreadTicks: ticks,
      maxMainThreadGapMs: Math.round(maxGapMs),
      cacheCheck,
      gcode: res.gcode,
    });
  }

  mod.disposeSlicer();
  return {
    avail,
    warm: { ...warm, wallMs: warmMs },
    fallbacks: { missingAvail, missingSlice, tooBigSlice, noProfileSlice },
    results,
  };
}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all-samples", action="store_true", help="slice all 5 showcase samples (for benchmarking)")
    ap.add_argument("--browser", choices=("edge", "chrome", "chromium"), default="edge")
    args = ap.parse_args()

    engine_dir = ROOT / "vendor" / MUON3D_DIR
    if not (engine_dir / "engine-st.wasm").is_file():
        print(f"FAIL: missing {engine_dir}. Run `python tools/vendor.py --slicer-only vendor` first.", file=sys.stderr)
        return 1

    from playwright.sync_api import sync_playwright

    samples = ALL_SAMPLES if args.all_samples else [DEFAULT_SAMPLE]
    httpd, port = serve_local()
    try:
        with sync_playwright() as p:
            browser = launch_browser(p, args.browser)
            page = browser.new_page()
            console_errors: list[str] = []
            page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
            # Block every external request so the test proves zero external network use.
            page.route(
                "**/*",
                lambda route: route.continue_()
                if route.request.url.startswith(f"http://127.0.0.1:{port}/")
                else route.abort(),
            )
            page.goto(f"http://127.0.0.1:{port}/web/sw.js", wait_until="domcontentloaded")
            # Navigate to a clean blank document on the same origin without loading the legacy app.
            page.set_content("<!doctype html><title>browser slicer test</title>")
            data = page.evaluate(BROWSER_DRIVER, {"samples": samples, "settings": TEST_SETTINGS})
            browser.close()
    finally:
        httpd.shutdown()

    assert not console_errors, f"Unexpected page errors: {console_errors}"
    assert data["avail"]["version"] == MUON3D_VERSION
    assert data["warm"]["ok"] is True

    fb = data["fallbacks"]
    assert fb["missingAvail"]["available"] is False and fb["missingAvail"]["reason"] == "unavailable", fb["missingAvail"]
    assert fb["missingSlice"]["ok"] is False and fb["missingSlice"]["fallback"] is True and fb["missingSlice"]["reason"] == "unavailable", fb["missingSlice"]
    assert fb["tooBigSlice"]["ok"] is False and fb["tooBigSlice"]["fallback"] is True and fb["tooBigSlice"]["reason"] == "too_big", fb["tooBigSlice"]
    assert fb["noProfileSlice"]["ok"] is False and fb["noProfileSlice"]["fallback"] is True and fb["noProfileSlice"]["reason"] == "no_profile", fb["noProfileSlice"]

    orca = find_orca()
    print(f"Browser slicer warmup: init={round(data['warm']['initMs'])} ms, wall={data['warm']['wallMs']} ms "
          f"(engine {data['warm']['engine']['version']}, Orca {data['warm']['engine']['orca']}, {data['warm']['engine']['license']})")

    for r in data["results"]:
        gc = r["gcode"]
        meta = parse_gcode(gc)
        tp = gcode.parse(gc)
        grams = gcode_grams(gc)
        vol_cm3 = round(float(tp.volume[tp.part()].sum()) / 1000, 2)

        assert tp.slicer == "orca", f"Expected slicer 'orca', got {tp.slicer!r}"
        assert len(tp.p0) > 1000, f"Too few extruded lines: {len(tp.p0)}"
        assert len(tp.layer_z) == meta["layers"], f"Layer count mismatch: {len(tp.layer_z)} vs {meta['layers']}"
        assert r["progressCount"] >= 5 and r["progressMonotonic"], f"Bad progress: {r['progressCount']}"
        assert r["mainThreadTicks"] >= 5, f"Main thread blocked during slicing (only {r['mainThreadTicks']} ticks)"
        if r["cacheCheck"] is not None:
            cc = r["cacheCheck"]
            assert cc["ok"] and cc["cached"] and cc["sameHash"] and cc["sameGcode"], f"Cache check failed: {cc}"
            assert cc["elapsedMs"] < 500, f"Cache hit too slow: {cc['elapsedMs']} ms"

        row = {
            "part": r["name"],
            "tris": r["tris"],
            "browser_slice_ms": r["wallMs"],
            "layers": meta["layers"],
            "browser_time_min": meta["print_time_min"],
            "browser_grams": grams,
            "tp_lines": len(tp.p0),
            "tp_vol_cm3": vol_cm3,
            "main_thread_ticks": r["mainThreadTicks"],
            "max_gap_ms": r["maxMainThreadGapMs"],
        }

        if orca is not None:
            desk = desktop_reference(r["name"], orca)
            time_diff_pct = abs(meta["print_time_min"] - desk["print_time_min"]) / desk["print_time_min"] * 100
            gram_diff_pct = abs(grams - desk["grams"]) / desk["grams"] * 100
            row["desktop_time_min"] = desk["print_time_min"]
            row["desktop_grams"] = desk["grams"]
            row["time_diff_pct"] = round(time_diff_pct, 2)
            row["gram_diff_pct"] = round(gram_diff_pct, 2)
            assert meta["layers"] == desk["layers"], f"{r['name']}: layer mismatch {meta['layers']} vs {desk['layers']}"
            assert time_diff_pct < 10.0, f"{r['name']}: time diverged by {time_diff_pct:.1f}%"
            assert gram_diff_pct < 5.0, f"{r['name']}: grams diverged by {gram_diff_pct:.1f}%"
        elif r["name"] == DEFAULT_SAMPLE:
            # Reference envelope for l_bracket.stl (Bambu Lab P1S 0.4, PLA, 3 walls, 15% gyroid)
            assert meta["layers"] == 200
            assert 25.0 <= meta["print_time_min"] <= 32.0
            assert 9.0 <= grams <= 10.5

        print("PASS", json.dumps(row))

    print("ALL BROWSER SLICER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
