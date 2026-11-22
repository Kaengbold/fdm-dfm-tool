"""How reliable are B-rep design edits on real STEP parts? (T-535)

For every part in core/tests/corpus_step/ the engine makes one edit of each kind it can (resize up to three holes,
teardrops, rounds at up to four inside corners, a slab on top, a hidden void, ribs where a boss gets them), with
their intents (T-528). Then web/brep.js applies each in headless Edge (tools/brep_bench.html), as the write-back
worker does: exact, or faceted when that fails, or left out. Records per edit how it was made and the time, per
part the OpenCascade memory, and writes research/brep_robustness.md.

    python tools/step_robustness.py              # needs Node + tools/node_modules (see check_step_faces.mjs), playwright
"""

from __future__ import annotations

import base64
import functools
import gzip
import http.server
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from fdmcore import api  # noqa: E402

CORPUS = ROOT / "core" / "tests" / "corpus_step"
REPORT = ROOT / "research" / "brep_robustness.md"
UP = [0, 0, 1]
MAX_HOLES, MAX_CORNERS = 3, 4


def plain(e: dict) -> dict:
    return {k: (np.frombuffer(v, "<f4").tolist() if k == "mesh" else v) for k, v in e.items() if not k.startswith("_")}


def inside_box(mesh, size: float):
    """A box of ``size`` mm deep inside the part (the voxel furthest from any surface), or None."""
    from scipy.ndimage import distance_transform_edt

    from fdmcore.voxel import voxelize

    grid = voxelize(mesh, 60_000)
    depth = distance_transform_edt(grid.solid) * grid.h
    if depth.max() < size:
        return None
    k = np.unravel_index(np.argmax(depth), depth.shape)
    c = grid.origin + (np.asarray(k) + 0.5) * grid.h
    return (c - size / 2).tolist(), (c + size / 2).tolist()


def edits_for(stl: bytes, faces: list[int]) -> list[dict]:
    """One edit of each kind the engine makes on this part, with ids naming the kind."""
    api.load(stl, "part", faces)
    s = api._require()
    regions = api.regions()
    out = []
    holes = [r for r in regions["regions"] if r.get("hole_diameter")][:MAX_HOLES]
    for r in holes:
        api.set_fit(r["id"], UP, partner_mm=r["hole_diameter"] + 0.4, fit="loose")
    design = api.design(UP)["edits"]
    out += [e for e in design if e["id"].startswith(("fit-", "teardrop-", "boss-ribs-"))]
    corners = sorted((m for m in regions["concave_edges"]["meta"] if m.get("faces")), key=lambda m: -m["length_mm"])[:MAX_CORNERS]
    for i, m in enumerate(corners):
        s.weak_spot, s.strength_verdict = (tuple(m["midpoint"]), 1.0), "likely to break"
        found = [e for e in api.design(UP)["edits"] if e["id"] == "stress-chamfer"]
        out += [{**e, "id": f"corner-{i}"} for e in found[:1]]
    s.weak_spot = s.strength_verdict = None
    lo, hi = s.mesh.triangles.reshape(-1, 3).min(axis=0), s.mesh.triangles.reshape(-1, 3).max(axis=0)
    c = (lo + hi) / 2
    boxes = [{"kind": "add_solid", "lo": [c[0] - 5, c[1] - 5, hi[2] - 0.5], "hi": [c[0] + 5, c[1] + 5, hi[2] + 2]}]
    inner = inside_box(s.mesh, 2.0)
    if inner:
        boxes.append({"kind": "cut_solid", "lo": inner[0], "hi": inner[1]})
    out += api.volume_solid(UP, boxes)  # build frame = part frame for up +z
    return out


def serve() -> int:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


def main() -> None:
    from playwright.sync_api import sync_playwright

    node = shutil.which("node")
    dump = Path(tempfile.mkdtemp())
    subprocess.run([node, str(ROOT / "tools" / "check_step_faces.mjs"), "--dump", str(dump)], check=True, capture_output=True)
    parts = sorted(p for p in CORPUS.iterdir() if p.name.endswith((".step", ".stp", ".step.gz", ".stp.gz")))
    port = serve()
    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        for part in parts:
            name = part.name.split(".")[0]
            stl, faces = (dump / f"{name}.stl").read_bytes(), json.loads((dump / f"{name}_faces.json").read_text())
            edits = edits_for(stl, faces)
            raw = part.read_bytes()
            step = gzip.decompress(raw) if part.name.endswith(".gz") else raw
            page = browser.new_page()  # a fresh page per part: its memory is that part's alone
            page.goto(f"http://127.0.0.1:{port}/tools/brep_bench.html")
            page.evaluate("() => window.benchReady")
            try:
                r = page.evaluate("([s, e]) => window.runPart(s, e)", [base64.b64encode(step).decode(), [plain(e) for e in edits]])
            except Exception as err:  # OpenCascade aborted (out of memory, uncaught C++ exception): record it, go on
                r = {"faces": 0, "readMs": 0, "one": [], "together": {"error": str(err).splitlines()[0]}, "heapMb": None}
            page.close()
            results[part.name] = r
            print(part.name, r["faces"], "faces;", " ".join(f"{x['id']}:{x['how']}" for x in r["one"]), "| together", r["together"].get("valid"), f"{r['heapMb']} MB", flush=True)
        browser.close()
    write_report(results)


KINDS = ["hole_resize", "teardrop", "slab", "void", "rib", "fillet", "chamfer"]
SOURCES = ("The parts: `nist_*` are the eleven NIST MBE PMI validation and conformance test parts (CTC 1–5, FTC 6–11; "
           "from NIST's MBE PMI Validation and Conformance Testing project; US government work, public domain), "
           "geometry-only AP203 files, gzipped; `occt_screw` is "
           "OpenCascade's screw sample (LGPL-2.1 with exception, from the OCCT data folder); `gen_*` are made by "
           "`tools/make_step_corpus.mjs`.")
RELIABLE = 0.9  # exact this often and never left out: on by default


def verdict(kind: str, k: dict) -> str:
    n = k["exact"] + k["mesh"] + k["skipped"] + k["error"]
    left = k["skipped"] + k["error"]
    if not left and k["exact"] >= RELIABLE * n:
        return "on by default: exact almost always, never left out"
    if not left:
        return "on by default: always in the file, sometimes faceted"
    return f"exact when it works, else left out of the STEP file and named in the status ({left} of {n} here)"


def write_report(results: dict) -> None:
    per = defaultdict(lambda: {"exact": 0, "smaller": 0, "mesh": 0, "skipped": 0, "error": 0, "ms": [], "why": defaultdict(int)})
    for r in results.values():
        for x in r["one"]:
            k = per[x["kind"]]
            k[x["how"] if x["how"] in k else "error"] += 1
            k["smaller"] += bool(x.get("scale"))
            k["ms"].append(x["ms"])
            if x.get("why") and x["how"] != "exact":
                k["why"][x["why"]] += 1
    lines = ["# B-rep design edits on real STEP parts (T-535)", "",
             "Made by `python tools/step_robustness.py`: every part in `core/tests/corpus_step/`, one engine edit of each kind it "
             "makes there (up to 3 resized holes, teardrops, rounds or chamfers at up to 4 inside corners, a slab on top, a hidden "
             "void, ribs), each applied alone by `web/brep.js` in headless Edge as the STEP write-back worker does it: exact, or "
             "faceted (its triangle pieces) when the exact edit fails, or left out. Then all together.", "",
             "## By kind of edit", "",
             "| Edit | Tried | Exact | of them smaller | Faceted instead | Left out | Median time | Slowest |", "|---|---|---|---|---|---|---|---|"]
    for kind in KINDS:
        k = per.get(kind)
        if not k:
            lines.append(f"| {kind} | 0 | – | – | – | – | – | – |")
            continue
        n = k["exact"] + k["mesh"] + k["skipped"] + k["error"]
        lines.append(f"| {kind} | {n} | {k['exact']} ({100 * k['exact'] // n} %) | {k['smaller']} | {k['mesh']} | {k['skipped'] + k['error']} | "
                     f"{statistics.median(k['ms']):.0f} ms | {max(k['ms'])} ms |")
    lines += ["", "## Verdict", "", f"An edit kind is on by default when it came out exact at least {RELIABLE:.0%} of the time "
              "and was never left out. Every kind stays on: what can't be made in the STEP file is named in the status, and "
              "the STL / 3MF export always has every change.", ""]
    lines += [f"- **{kind}**: {verdict(kind, per[kind])}." for kind in KINDS if kind in per]
    lines += ["", "## By part", "", "| Part | Faces | Read | Edits: exact / faceted / left out | All together | OpenCascade memory |",
              "|---|---|---|---|---|---|"]
    for name, r in results.items():
        hows = [x["how"] for x in r["one"]]
        t = r["together"]
        mem = f"{r['heapMb']} MB" if r["heapMb"] else "–"
        both = f"valid, {t['faces']} faces, {t['ms']} ms" if t.get("valid") else f"not valid ({t.get('error', 'invalid solid')})"
        lines.append(f"| {name} | {r['faces']} | {r['readMs']} ms | {hows.count('exact')} / {hows.count('mesh')} / "
                     f"{len(hows) - hows.count('exact') - hows.count('mesh')} | {both} | {mem} |")
    reasons = [(kind, why, n) for kind, k in per.items() for why, n in k["why"].items()]
    if reasons:
        lines += ["", "## Why exact edits fell back", "", "| Edit | Why | Times |", "|---|---|---|"]
        lines += [f"| {kind} | {why} | {n} |" for kind, why, n in sorted(reasons)]
    lines += ["", "## Sources", "", SOURCES]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (REPORT.with_suffix(".json")).write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"wrote {REPORT}")


if __name__ == "__main__":
    main()
