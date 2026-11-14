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
    # Measured size errors (T-345): holes 0.2 mm bigger in diameter, the outline 0.2 mm smaller.
    "C": {"hole_to_polyhole": "0", "elefant_foot_compensation": "0", "xy_hole_compensation": "0.1", "xy_contour_compensation": "-0.1"},
}
PF = Path("C:/Program Files")
SLICERS = {
    "orca": {
        "exe": PF / "OrcaSlicer/orca-slicer.exe",
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
    elif name == "orca":  # 2.5 packs its profiles, so the ones slicer_cli picks (installed or ours)
        from fdmcore.slicer_cli import pick_profiles, write_profiles

        f = write_profiles(pick_profiles(Path(cfg["exe"]), "Bambu Lab P1S", "0.4", "pla", 0.2), out)
        cmd = [str(cfg["exe"]), "--slice", "0", "--outputdir", str(out),
               "--load-settings", f"{f['machine']};{f['process']}", "--load-filaments", str(f["filament"]), str(model)]
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


def pause_block(layout: str) -> bytes:
    """20 mm cube with a closed 8 mm pocket at z 6–9, exported with a pause before the roof."""
    import numpy as np

    from fdmcore.additions import extrude
    from shapely.geometry import box

    outer = extrude(box(0, 0, 20, 20), 0, 20)
    pocket = extrude(box(6, 6, 14, 14), 6, 9)[:, ::-1]  # inner shell: wound inward
    api.load(to_binary_stl(Mesh(np.concatenate([outer, pocket]))), "pause_check")
    return api.export([0, 0, 1], {}, {}, slicer=layout, pauses=[9.0])


def slope_block(layout: str) -> bytes:
    """40 × 40 mm block, 10 mm high, with a roof sloping 10° (to 17 mm): exported with thinner layers over the roof."""
    from shapely.geometry import Polygon

    from fdmcore.additions import extrude

    rise = 40 * np.tan(np.radians(10))
    tri = extrude(Polygon([(0, 0), (40, 0), (40, 10 + rise), (0, 10)]), 0, 40)[..., [0, 2, 1]][:, ::-1]  # profile stands up
    api.load(to_binary_stl(Mesh(tri)), "slope_check")
    ranges = [f["metrics"] for f in api.check([0, 0, 1], {})["findings"] if f["rule"] == "shallow_slope_stairs"]
    return api.export([0, 0, 1], {}, {}, slicer=layout, fine_layers=ranges)


def measure_layers(gcode: Path) -> list[float]:
    """Heights of the layers, in order."""
    return [float(m.group(2)) for line in gcode.read_text(errors="replace").splitlines() if (m := LAYER.match(line))]


OUTER = re.compile(r"^;\s*(FEATURE|TYPE):\s*(Outer wall|External perimeter)\s*$")


def seam_block(mask_of) -> bytes:
    """20 mm cube (centred), exported with the faces ``mask_of(normals)`` painted as seam blockers."""
    from fdmcore.checks import orient
    from fdmcore.export3mf import export_3mf

    api.load(to_binary_stl(Mesh(test_part_box())), "seam_check")
    mesh = orient(api._require().mesh, np.eye(3))
    paint = mask_of(mesh.normals_and_areas()[0]) if mask_of else None
    return export_3mf(mesh, "seam_check", {}, seam_block=paint)


def test_part_box() -> np.ndarray:
    from shapely.geometry import box

    from fdmcore.additions import extrude

    return extrude(box(0, 0, 20, 20), 0, 20)


def measure_seams(gcode: Path) -> np.ndarray:
    """Where each outer wall loop starts (x, y), relative to the part's centre."""
    x = y = 0.0
    starts, waiting, outer, pts = [], False, False, []
    for line in gcode.read_text(errors="replace").splitlines():
        if FEATURE.match(line):
            outer = waiting = bool(OUTER.match(line))
            continue
        if line.startswith(("G1", "G0", "G2", "G3")):
            w = dict((t[0], float(t[1:])) for t in line.split(";")[0].split()[1:] if t[0] in "XYE" and len(t) > 1)
            if waiting and w.get("E", 0) > 0 and ("X" in w or "Y" in w):
                starts.append((x, y))
                waiting = False
            x, y = w.get("X", x), w.get("Y", y)
            if outer and w.get("E", 0) > 0:  # the part's own outline (not the purge line at the bed's edge)
                pts.append((x, y))
    p = np.array(pts)
    return np.array(starts) - (p.min(axis=0) + p.max(axis=0)) / 2


PAUSE = re.compile(r"^(; PAUSE_PRINTING|;PAUSE_PRINT|M601)\b")


def measure_pause(gcode: Path) -> list[float]:
    """Layer heights (Z of the layer being started) where the G-code pauses."""
    z, at = None, []
    for line in gcode.read_text(errors="replace").splitlines():
        if (m := LAYER.match(line)) is not None:
            z = float(m.group(2))
        elif PAUSE.match(line) and z is not None and z not in at:
            at.append(z)
    return at


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
    return {"hole_corners": corners, "foot_mm": round((width(mid) - width(first)) / 2, 2),
            "hole_r": round(float(np.median(r[hole])), 3), "outer_mm": round(width(mid & (r > 4.5)), 3)}


def grip_cylinder(layout: str, grip: bool) -> bytes:
    """Ø20 mm cylinder, 10 mm tall, exported with or without the grip texture (fuzzy skin)."""
    from shapely.geometry import Point

    from fdmcore.additions import extrude

    api.load(to_binary_stl(Mesh(extrude(Point(0, 0).buffer(10, quad_segs=64), 0, 10))), "knob")
    return api.export([0, 0, 1], {}, {}, slicer=layout, grip=grip)


def measure_fuzz(gcode: Path) -> float:
    """How far the outer wall's points stray from round at 4-6 mm height (mm, standard deviation of the radius)."""
    x = y = z = 0.0
    outer, pts = False, []
    for line in gcode.read_text(errors="replace").splitlines():
        if (m := LAYER.match(line)) is not None:
            z = float(m.group(2))
        elif FEATURE.match(line):
            outer = bool(OUTER.match(line))
        elif line.startswith(("G1", "G2", "G3")):
            w = dict((t[0], float(t[1:])) for t in line.split(";")[0].split()[1:] if t[0] in "XYE" and len(t) > 1)
            x, y = w.get("X", x), w.get("Y", y)
            if outer and 4 < z < 6 and w.get("E", 0) > 0 and ("X" in w or "Y" in w):
                pts.append((x, y))
    p = np.array(pts)
    r = np.linalg.norm(p - (p.min(axis=0) + p.max(axis=0)) / 2, axis=1)
    return round(float(np.std(r[np.abs(r - np.median(r)) < 1])), 3)


def copies_block(layout: str, n: int) -> bytes:
    """20 mm cube, ``n`` copies on the plate."""
    from fdmcore.additions import extrude
    from shapely.geometry import box

    api.load(to_binary_stl(Mesh(extrude(box(0, 0, 20, 20), 0, 5))), "copies_check")
    return api.export([0, 0, 1], {}, {}, slicer=layout, copies=n)


def count_first_layer_parts(gcode: Path) -> int:
    """Outer wall blocks on the first layer: one per cube."""
    first, n = None, 0
    for line in gcode.read_text(errors="replace").splitlines():
        if (m := LAYER.match(line)) is not None:
            if first is not None and float(m.group(2)) > first:
                break
            first = float(m.group(2))
        elif first is not None and OUTER.match(line):
            n += 1
    return n


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
            c = holes["C"]
            # OrcaSlicer opens the hole by the value; Bambu Studio 1.8 by about 2.7× it (checked): there only "it grows".
            grow = c["hole_r"] - a["hole_r"]
            checks["xy_hole_compensation"] = (a["hole_r"], c["hole_r"], 0.07 <= grow <= 0.13 if name == "orca" else grow > 0.05)
            checks["xy_contour_compensation"] = (a["outer_mm"], c["outer_mm"], 0.14 <= a["outer_mm"] - c["outer_mm"] <= 0.26)
        layout = "prusa" if name == "prusa" else "orca"
        model = work / f"{name}_zone.3mf"
        model.write_bytes(zone_block(layout))
        z = measure_zone(slice_with(name, cfg, model, work / f"{name}_zone"))
        # The zone half is filled (≥ 2.5× the plastic of the 10 % half), and the part didn't grow by the zone's box.
        checks["zone_modifier"] = (z["zone"], z["rest"], z["zone"] >= 2.5 * z["rest"] and z["width"] < 20.0)
        model = work / f"{name}_seam_free.3mf"
        model.write_bytes(seam_block(None))
        free = measure_seams(slice_with(name, cfg, model, work / f"{name}_seam_free"))
        corner = np.sign(np.median(free, axis=0))  # where the slicer puts it by itself
        model = work / f"{name}_seam_paint.3mf"
        model.write_bytes(seam_block(lambda n: (n[:, :2] @ corner) > 0.5))  # block the two faces at that corner
        painted = measure_seams(slice_with(name, cfg, model, work / f"{name}_seam_paint"))
        on_blocked = np.mean(np.max(painted * corner, axis=1) > 9.0)  # on a blocked face (≥ 9 of 10 mm out)
        checks["seam_blocker"] = (corner.tolist(), f"{on_blocked:.0%} on blocked faces", len(painted) > 0 and on_blocked < 0.1)
        model = work / f"{name}_slope.3mf"
        model.write_bytes(slope_block(layout))
        zs = np.array(measure_layers(slice_with(name, cfg, model, work / f"{name}_slope")))
        dz = np.diff(zs)
        roof, below = dz[(zs[1:] > 11) & (zs[1:] < 16)], dz[(zs[1:] > 2) & (zs[1:] < 9)]
        # Thinner layers over the sloped roof (10–17 mm), the normal ones below it.
        checks["fine_layer_range"] = (round(float(np.median(roof)), 3) if len(roof) else None, round(float(np.median(below)), 3) if len(below) else None,
                                      bool(len(roof) and len(below) and np.median(roof) < 0.11 and np.median(below) > 0.15))
        model = work / f"{name}_pause.3mf"
        model.write_bytes(pause_block(layout))
        gcode = slice_with(name, cfg, model, work / f"{name}_pause")
        if name == "bambu":
            # Bambu Studio reads no pause from a 3MF it didn't write: the app adds it to the sliced file instead.
            print(f"note {name}: pauses read from the 3MF at {measure_pause(gcode)} (none expected)")
            gcode.write_bytes(api.pause_gcode(gcode.read_bytes(), [9.0])["data"])
        at = measure_pause(gcode)
        # Before the first layer above the pocket (top 9 mm): 9.2 on a 0.2 mm grid, 9.05 on 0.15 mm.
        checks["pause_at_layer"] = (at, "first layer over 9.0", len(at) == 1 and 9.0 < at[0] <= 9.21)
        model = work / f"{name}_copies.3mf"
        model.write_bytes(copies_block(layout, 3))
        n = count_first_layer_parts(slice_with(name, cfg, model, work / f"{name}_copies"))
        checks["copies"] = (3, n, n == 3)
        fuzz = {}
        for grip in (False, True):
            model = work / f"{name}_grip_{int(grip)}.3mf"
            model.write_bytes(grip_cylinder(layout, grip))
            fuzz[grip] = measure_fuzz(slice_with(name, cfg, model, work / f"{name}_grip_{int(grip)}"))
        # Fuzzy skin roughens the outer wall: its points stray several times further from round.
        checks["fuzzy_skin"] = (fuzz[False], fuzz[True], fuzz[True] > 0.05 and fuzz[True] > 3 * fuzz[False])
        for key, (va, vb, applied) in checks.items():
            ok &= applied
            print(f"{'PASS' if applied else 'FAIL'} {name} {key}: A={va} B={vb}")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
