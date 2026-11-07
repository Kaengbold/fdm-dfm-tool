"""Do slicers apply the settings in our 3MF export? (T-001, T-002)

Exports a 20 mm cube with a small overhanging tab twice, with two clearly different
per-object setting sets, slices both with each installed slicer's command line and
measures the G-code: wall loops in a middle layer, sparse infill present, support
present, brim present. A setting counts as applied when the measurement follows it.
A second part, a plate with a Ø6 mm hole, checks the hole helpers: polyholes (fewer corners around the hole; OrcaSlicer only, Bambu Studio
and PrusaSlicer don't have them) and elephant-foot compensation (a smaller first layer; not
per object in PrusaSlicer). A third, a 40 × 20 × 10 mm block at 10 % infill with a zone (modifier
volume) at 100 % over one half, checks that each slicer fills the zone (T-150; the file layout
differs per slicer, see export3mf.py).

    python tools/slicer_check.py            # all slicers found in the default install folders
    python tools/slicer_check.py --keep     # keep the G-code in e2e-output/slicer-check

Dev-only (needs the slicers installed; Windows paths by default, override with --orca etc.).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from fdmcore import api  # noqa: E402
from fdmcore.mesh import Mesh, to_binary_stl  # noqa: E402

import numpy as np  # noqa: E402

# Two setting sets that differ in every checked respect (OrcaSlicer / Bambu Studio keys).
SETS = {
    "A": {"wall_loops": "2", "sparse_infill_density": "0%", "enable_support": "0", "brim_type": "no_brim", "brim_width": "0"},
    "B": {"wall_loops": "5", "sparse_infill_density": "40%", "enable_support": "1", "brim_type": "outer_only", "brim_width": "6"},
}

# Hole helpers (T-136), on the hole plate.
HOLE_SETS = {
    "A": {"hole_to_polyhole": "0", "elefant_foot_compensation": "0"},
    "B": {"hole_to_polyhole": "1", "elefant_foot_compensation": "0.3"},
}
PF = Path("C:/Program Files")
SLICERS = {
    "orca": {
        "exe": PF / "OrcaSlicer/orca-slicer.exe",
        "profiles": PF / "OrcaSlicer/resources/profiles/BBL",
    },
    "bambu": {
        "exe": PF / "Bambu Studio/bambu-studio.exe",
        "profiles": PF / "Bambu Studio/resources/profiles/BBL",
    },
    "prusa": {
        "exe": PF / "Prusa3D/PrusaSlicer/prusa-slicer-console.exe",
    },
}
BBL_MACHINE = "machine/Bambu Lab P1S 0.4 nozzle.json"
BBL_PROCESS = "process/0.20mm Standard @BBL X1C.json"
BBL_FILAMENT = "filament/Bambu PLA Basic @BBL X1C.json"


def test_part() -> bytes:
    """One closed solid: a 20 mm cube with a 12 mm tab sticking out at the top (needs support below it).
    Side profile (x, z) extruded along y."""
    from shapely.geometry import Polygon

    from fdmcore.additions import extrude

    profile = Polygon([(0, 0), (20, 0), (20, 14), (32, 14), (32, 20), (0, 20)])
    tris = extrude(profile, 0, 20)[:, :, [0, 2, 1]]  # (x, z, y) -> (x, y, z); swapping two axes mirrors,
    return to_binary_stl(Mesh(tris[:, ::-1]))  # so flip the winding back


def hole_plate() -> bytes:
    """30 × 30 × 4 mm plate with a Ø6 mm hole in the middle."""
    from shapely.geometry import Point, box

    from fdmcore.additions import extrude

    return to_binary_stl(Mesh(extrude(box(0, 0, 30, 30).difference(Point(15, 15).buffer(3, 64)), 0, 4)))


def export(settings: dict, part: bytes | None = None) -> bytes:
    api.load(part or test_part(), "slicer_check")
    return api.export([0, 0, 1], {}, settings)


def slice_with(name: str, cfg: dict, model: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    if name == "prusa":
        cmd = [str(cfg["exe"]), "--export-gcode", "--output", str(out / "plate.gcode"), str(model)]
    else:
        p = cfg["profiles"]
        cmd = [str(cfg["exe"]), "--slice", "0", "--outputdir", str(out),
               "--load-settings", f"{p / BBL_MACHINE};{p / BBL_PROCESS}", "--load-filaments", str(p / BBL_FILAMENT), str(model)]
    subprocess.run(cmd, capture_output=True, timeout=600, cwd=out)  # the slicer leaves a 00000.log where it runs
    found = sorted(out.glob("*.gcode"))
    if not found:
        raise RuntimeError(f"{name} produced no G-code ({' '.join(cmd)})")
    return found[0]


WALL = re.compile(r"^;\s*(FEATURE|TYPE):\s*(Outer wall|Inner wall|External perimeter|Perimeter)\s*$")
FEATURE = re.compile(r"^;\s*(FEATURE|TYPE):\s*(.+?)\s*$")
LAYER = re.compile(r"^;\s*(Z_HEIGHT|Z):\s*([\d.]+)")


def measure(gcode: Path) -> dict:
    """Wall loops around the cube in the layer nearest z = 10 mm, loops of brim on the first layer
    (PrusaSlicer prints skirt and brim as one feature), and which features appear at all."""
    features: set[str] = set()
    z, first_z = 0.0, None
    walls: dict[float, int] = {}
    brim = 0
    kind = None  # "wall", "brim" or None
    was_extruding = False
    for line in gcode.read_text(errors="replace").splitlines():
        if m := LAYER.match(line):
            z = float(m.group(2))
            first_z = z if first_z is None else first_z
            continue
        if m := FEATURE.match(line):
            name = m.group(2).lower()
            features.add(name)
            kind = "wall" if WALL.match(line) else "brim" if "brim" in name else None
            was_extruding = False
            continue
        if not line.startswith(("G1", "G2", "G3")):
            continue
        extruding = " E" in line and " E-" not in line and re.search(r" [XY]", line) is not None
        if extruding and not was_extruding:
            if kind == "wall" and abs(z - 10) < 0.3:
                walls[z] = walls.get(z, 0) + 1
            elif kind == "brim" and z == first_z:
                brim += 1
        was_extruding = extruding
    best = min(walls, key=lambda k: abs(k - 10)) if walls else None
    return {
        "walls": walls.get(best, 0),
        "infill": any("sparse infill" in f or f == "internal infill" for f in features),
        "support": any(f.startswith("support") for f in features),
        "brim": brim >= 4,  # a 6 mm brim is ~13 loops; a skirt is 1–3
    }


ZONE = {"lo": [-1, -1, 0], "hi": [20, 21, 10], "walls": 2, "infill": 1.0}


def zone_block(layout: str) -> bytes:
    from fdmcore.additions import extrude
    from shapely.geometry import box

    api.load(to_binary_stl(Mesh(extrude(box(0, 0, 40, 20), 0, 10))), "zone_block")
    return api.export([0, 0, 1], {}, {"sparse_infill_density": "10%", "wall_loops": "2"}, zones=[ZONE], slicer=layout)


def measure_zone(gcode: Path) -> dict:
    """Infill plastic (cm³) in the half with the zone and in the other half."""
    from fdmcore import gcode as gc

    tp = gc.parse(gcode.read_text(errors="replace"))
    a, b = gc.bead_centres(tp)
    x = (a[:, 0] + b[:, 0]) / 2
    part = tp.part()
    cx = (x[part].min() + x[part].max()) / 2
    inner = np.isin(tp.kind, [gc.SPARSE, gc.SOLID])
    return {"zone": round(float(tp.volume[inner & (x < cx)].sum()) / 1000, 2), "rest": round(float(tp.volume[inner & (x >= cx)].sum()) / 1000, 2),
            "width": round(float(np.ptp(np.concatenate([a[part, 1], b[part, 1]]))), 1)}


def measure_holes(gcode: Path) -> dict:
    """Corners of the hole's outer wall in a middle layer, and how much smaller the first layer's
    outline is than the middle layer's (mm, per side)."""
    from fdmcore import gcode as gc

    tp = gc.parse(gcode.read_text(errors="replace"))
    outer = tp.kind == gc.WALL
    pts = tp.p1[outer]
    centre = (pts[:, :2].min(axis=0) + pts[:, :2].max(axis=0)) / 2
    mid = tp.layer[outer] == len(tp.layer_z) // 2
    first = tp.layer[outer] == 0
    r = np.linalg.norm(pts[:, :2] - centre, axis=1)
    hole = mid & (r < 4.5)
    corners = len({tuple(np.round(p, 2)) for p in pts[hole & (r < r[hole].min() + 0.2), :2]})
    width = lambda sel: float(np.ptp(pts[sel, 0]))
    return {"hole_corners": corners, "foot_mm": round((width(mid) - width(first)) / 2, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    for name in SLICERS:
        ap.add_argument(f"--{name}", type=Path, help=f"path to the {name} executable")
    args = ap.parse_args()
    work = ROOT / "e2e-output" / "slicer-check" if args.keep else Path(tempfile.mkdtemp())
    ok = True
    for name, cfg in SLICERS.items():
        cfg = {**cfg, "exe": getattr(args, name) or cfg["exe"]}
        if not Path(cfg["exe"]).exists():
            print(f"skip {name}: not installed at {cfg['exe']}")
            continue
        got = {}
        for label, settings in SETS.items():
            model = work / f"{name}_{label}.3mf"
            model.parent.mkdir(parents=True, exist_ok=True)
            model.write_bytes(export(settings))
            got[label] = measure(slice_with(name, cfg, model, work / f"{name}_{label}"))
        a, b = got["A"], got["B"]
        checks = {
            "wall_loops": (a["walls"], b["walls"], a["walls"] == 2 and b["walls"] == 5),
            "sparse_infill_density": (a["infill"], b["infill"], not a["infill"] and b["infill"]),
            "enable_support": (a["support"], b["support"], not a["support"] and b["support"]),
            "brim_type": (a["brim"], b["brim"], not a["brim"] and b["brim"]),
        }
        if name != "prusa":
            holes = {}
            for label, settings in HOLE_SETS.items():
                model = work / f"{name}_hole_{label}.3mf"
                model.write_bytes(export(settings, hole_plate()))
                holes[label] = measure_holes(slice_with(name, cfg, model, work / f"{name}_hole_{label}"))
            a, b = holes["A"], holes["B"]
            if name == "orca":  # Bambu Studio has no polyholes (an OrcaSlicer feature): it ignores the key
                checks["hole_to_polyhole"] = (a["hole_corners"], b["hole_corners"], b["hole_corners"] <= a["hole_corners"] * 0.6)
            checks["elefant_foot_compensation"] = (a["foot_mm"], b["foot_mm"], b["foot_mm"] - a["foot_mm"] >= 0.15)
        layout = "prusa" if name == "prusa" else "orca"
        model = work / f"{name}_zone.3mf"
        model.write_bytes(zone_block(layout))
        z = measure_zone(slice_with(name, cfg, model, work / f"{name}_zone"))
        # The zone half is filled (≥ 2.5× the plastic of the 10 % half), and the part didn't grow by the zone's box.
        checks["zone_modifier"] = (z["zone"], z["rest"], z["zone"] >= 2.5 * z["rest"] and z["width"] < 20.0)
        for key, (va, vb, applied) in checks.items():
            ok &= applied
            print(f"{'PASS' if applied else 'FAIL'} {name} {key}: A={va} B={vb}")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
