"""Entry points used by the web worker, the future local server and MCP tools.

Results are plain dicts (JSON-friendly), except raw arrays that are returned as
``bytes`` to keep transfers small: triangles as float32, face classes as uint8.

Strength features need scipy; they import it lazily so the rest runs without it
(the web worker loads scipy only when the user asks for a strength check).

Modes decide which fixes are shown:
  ``print``  — "I can't change the model": slicer + process fixes
  ``design`` — "someone else slices": geometry + process fixes
  ``both``   — everything
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

from .checks import RULE_IMPACT, orient, run_checks, slice_mesh
from .export3mf import export_3mf
from .formats import read_bodies, unit_hint
from .mesh import Mesh, to_binary_stl
from .orientation import OrientationSettings, classify_faces, evaluate, find_orientations, rotation_to_z
from .placement import print_rotation
from .profile import DATA as DATA_DIR, Profile, load_data, presets
from . import progress
from .progress import stage
from .recommend import goal_plan, grip_settings, recommend_settings
from .service import required_safety

MODES = {"print": {"slicer", "process"}, "design": {"geometry", "process"}, "both": {"geometry", "slicer", "process"}}


@dataclass
class Session:
    mesh: Mesh
    name: str
    regions: np.ndarray | None = None  # region id per triangle, computed on demand
    load_case: tuple | None = None  # (loads json, prepared LoadCase) cache
    solved: tuple | None = None  # (key, result): the last static strength solve, reused by the next check
    bodies: list = field(default_factory=list)  # (name, Mesh) per body in the file
    body: int = 0  # which body is loaded
    tags: dict = field(default_factory=dict)  # region id -> what the user says the feature is (features.TAGS)
    weak_spot: tuple | None = None  # (critical point in part coords, voxel size) from the last strength check
    hot_spots: tuple | None = None  # (points, share of the highest stress) where it is nearly as high (features.HOT_SHARE)
    gcode: dict | None = None  # the sliced part (load_gcode): toolpath, orientation and placement
    fits: dict = field(default_factory=dict)  # region id -> {"partner_mm", "fit"}: what goes into a hole / onto a post
    fasteners: dict = field(default_factory=dict)  # region id -> {"size", "use"}: screw / insert / nut holes (set_fastener)
    gaps: dict = field(default_factory=dict)  # (face, face) -> {"partner_mm", "fit", "warp_mm"}: slots and tabs (set_gap)
    warp: dict | None = None  # the last warping simulation: up, model, displacement (part frame)
    coarse: list | None = None  # curves exported with too few facets (checks.coarse_surfaces), computed on demand


_session: Session | None = None


def _require() -> Session:
    if _session is None:
        raise RuntimeError("No part loaded")
    return _session


def get_presets() -> dict:
    """All presets, plus how to use each calibration print (``calibration``) for the My materials form."""
    return {**presets(), "calibration": CALIBRATION_HOW}


def custom_presets(printers: dict | None = None, materials: dict | None = None) -> dict:
    """Set the user's own printers / materials (see profile.set_custom_presets); returns all presets."""
    from .profile import set_custom_presets

    set_custom_presets(printers, materials)
    return get_presets()


# Analysis grids (Expert settings): more cubes are more exact and slower (about linear in the browser).
ANALYSIS = {
    "strength_voxels": (12_000, 2_000, 200_000, "Cubes in the strength grid (Will it hold?, shaking, the optimiser). Seconds in the browser at 12 000."),
    "fine_voxels": (40_000, 5_000, 400_000, "Cubes for \"Check accuracy with a finer grid\": about 1.5× finer than the normal grid."),
    "warp_voxels": (6_000, 1_000, 100_000, "Cubes in the warping simulation: it solves once per group of layers, so a coarser grid."),
}
_analysis: dict[str, float] = {}


def _grid(key: str) -> int:
    return int(_analysis.get(key, ANALYSIS[key][0]))


def expert_settings() -> dict:
    """Every number the checks use, for the Expert settings window: the analysis grids and all of
    thresholds.yaml, grouped by section, each with its default, the value in use and the note from the
    file (what it does and its source). Nothing hidden."""
    from .profile import _expert, _read_data

    analysis = {"name": "analysis", "title": "Analysis grids", "about": "How finely the part is cut into cubes for the simulations.",
                "values": [{"path": f"analysis.{k}", "default": d, "value": _grid(k), "min": lo, "max": hi, "note": note}
                           for k, (d, lo, hi, note) in ANALYSIS.items()]}
    notes, about = _yaml_notes(DATA_DIR / "thresholds.yaml")
    over = _expert.get("thresholds", {})
    sections = {}

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, [*path, str(k)])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, [*path, str(i)])
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            key = ".".join(path)
            note = next((notes[".".join(path[:n])] for n in range(len(path), 0, -1) if ".".join(path[:n]) in notes), "")
            sections.setdefault(path[0], []).append({"path": f"thresholds.{key}", "default": node, "value": over.get(key, node), "note": note})

    walk(_read_data("thresholds"), [])
    out = [analysis] + [{"name": k, "title": k.replace("_", " ").capitalize(), "about": about.get(k, ""), "values": v} for k, v in sections.items()]
    return {"sections": out}


def _yaml_notes(path) -> tuple[dict[str, str], dict[str, str]]:
    """Comments of a YAML file: the one after each ``key: value`` (by dotted path) and the full-line
    comments inside each top-level section (its description)."""
    import re

    notes, about, stack = {}, {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        indent = len(line) - len(line.lstrip())
        text = line.strip()
        if text.startswith("#"):
            if stack:
                about[stack[0][1]] = (about.get(stack[0][1], "") + " " + text.lstrip("# ")).strip()
            continue
        m = re.match(r"([\w-]+):\s*([^#]*?)\s*(?:#\s*(.*))?$", text)
        if not m:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, m.group(1)))
        if m.group(3):
            notes[".".join(k for _, k in stack)] = m.group(3)
    return notes, about


def set_expert(values: dict[str, float]) -> dict:
    """Apply the Expert settings window: ``{"thresholds.features.vanish_below_nozzle": 0.6,
    "analysis.strength_voxels": 20000}``; values not given go back to their defaults."""
    from .profile import set_expert as set_data

    grids, data = {}, {}
    for path, value in (values or {}).items():
        group, _, rest = path.partition(".")
        if group == "analysis":
            if rest not in ANALYSIS:
                raise KeyError(f"Unknown setting {path}")
            lo, hi = ANALYSIS[rest][1:3]
            grids[rest] = min(max(float(value), lo), hi)
        else:
            data.setdefault(group, {})[rest] = float(value)
    set_data(data)
    _analysis.clear()
    _analysis.update(grids)
    if _session is not None:
        _session.solved = None  # results depend on these
    return expert_settings()


CALIBRATION = ("layer_strength", "warping", "fit", "overhang", "bridge")
# What to print and measure per test print: shown in the web app's My materials form and by the MCP tool.
CALIBRATION_HOW = {
    "layer_strength": "A 60 mm bar with a hole near each end. Print one lying flat and one standing on its end, hang each "
                      "by one hole and add weight at the other until it breaks. Standing ÷ flat is your layer strength.",
    "warping": "Bars 40, 80, 120 and 160 mm long. Print them without a brim (in your enclosure if you have one): "
               "the longest one that stays flat on the bed is how long a part can get before its corners lift.",
    "fit": "A 6 mm peg and holes 6.0 … 6.5 mm (the notched end is 6.0). Try the peg in each hole: "
           "note the one it pushes into snugly and the one it slides and turns in.",
    "overhang": "Seven blocks leaning 40° … 70° (the short end is 40°), printed without support: "
                "the steepest one that's still clean is how far this material can lean out.",
    "bridge": "Bridges 10 … 50 mm long between pillars, printed without support: "
              "the longest one that doesn't sag is how far it can bridge.",
}
FIT_GAUGE_GAPS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5)  # hole sizes of the fit gauge: 6 mm + these
OVERHANG_TEST_DEG = (40, 45, 50, 55, 60, 65, 70)  # from vertical
BRIDGE_TEST_MM = (10, 20, 30, 40, 50)


def calibration_part(kind: str) -> dict:
    """Test print for measuring the user's own material on their printer, as STL bytes, with ``how``
    (``CALIBRATION_HOW``: what to print and measure). Upright / flat break weight is the layer strength
    factor, the longest flat warping bar the warping length, the fit holes the fit gaps, the steepest
    clean block the max overhang, the longest straight bridge the max bridge."""
    from shapely.geometry import Point, Polygon, box as sbox

    from .additions import extrude
    from .mesh import to_binary_stl

    def lying(profile_xz, y0, depth):
        """A profile drawn in x/z, extruded along y from y0 (extrude works in x/y, then turned up)."""
        tris = extrude(profile_xz, y0, y0 + depth)[:, :, [0, 2, 1]]  # (x, z, y) -> (x, y, z)
        return tris[:, ::-1]  # swapping two axes mirrors: flip the winding back

    if kind == "fit":
        strip = sbox(0, 0, 10 * len(FIT_GAUGE_GAPS) + 4, 12).difference(Polygon([(0, 0), (4, 0), (0, 4)]))  # notch: the 6.0 end
        for i, gap in enumerate(FIT_GAUGE_GAPS):
            strip = strip.difference(Point(7 + 10 * i, 6).buffer((6 + gap) / 2, 64))
        peg = extrude(Point(0, 0).buffer(3, 64), 0, 12) + [30, 25, 0]
        return {"stl": to_binary_stl(Mesh(np.concatenate([extrude(strip, 0, 5), peg]))), "name": "fit_test.stl", "how": CALIBRATION_HOW[kind]}
    if kind == "overhang":
        tris = [lying(Polygon([(0, 0), (8, 0), (8 + 15 * np.tan(np.radians(a)), 15), (0, 15)]), i * 12, 8)
                for i, a in enumerate(OVERHANG_TEST_DEG)]
        return {"stl": to_binary_stl(Mesh(np.concatenate(tris))), "name": "overhang_test.stl", "how": CALIBRATION_HOW[kind]}
    if kind == "bridge":
        tris = []
        for i, span in enumerate(BRIDGE_TEST_MM):
            shape = sbox(0, 0, 5, 10).union(sbox(5 + span, 0, 10 + span, 10)).union(sbox(0, 10, 10 + span, 11.2))
            tris.append(lying(shape, i * 14, 8))
        return {"stl": to_binary_stl(Mesh(np.concatenate(tris))), "name": "bridge_test.stl", "how": CALIBRATION_HOW[kind]}

    if kind == "layer_strength":
        from shapely.geometry import Point, box as sbox

        from .additions import extrude

        outline = sbox(0, 0, 60, 10).difference(Point(6, 5).buffer(3, 32)).difference(Point(54, 5).buffer(3, 32))
        return {"stl": to_binary_stl(Mesh(extrude(outline, 0, 6))), "name": "layer_strength_test.stl", "how": CALIBRATION_HOW[kind]}
    if kind == "warping":
        tris = []
        for i, length in enumerate((40, 80, 120, 160)):
            v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * [length, 10, 3]
            v += [0, i * 20, 0]
            f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
            tris.append(v[np.array(f)])
        return {"stl": to_binary_stl(Mesh(np.concatenate(tris))), "name": "warping_test.stl", "how": CALIBRATION_HOW[kind]}
    raise ValueError(f"kind must be one of {CALIBRATION}")


def load(data: bytes, name: str = "part", face_ids: list[int] | None = None) -> dict:
    """Load an STL or 3MF; returns mesh info and the triangles for display.

    ``face_ids`` (one per triangle, e.g. CAD faces from a STEP file) replace the
    guessed face regions with exact ones. Files with several bodies load the largest;
    ``bodies`` lists them all so the user can switch with ``select_body``.
    """
    global _session
    bodies = read_bodies(bytes(data))
    if face_ids is not None:
        if len(bodies) > 1 or len(face_ids) != bodies[0][1].face_count:
            raise ValueError("face_ids must have one entry per triangle")
        regions = np.unique(np.asarray(face_ids), return_inverse=True)[1].ravel()
    else:
        regions = None
    _session = Session(bodies[0][1], name, regions, bodies=bodies)
    return _info()


def select_body(index: int) -> dict:
    """Analyse another body of a multi-body file."""
    s = _require()
    if not 0 <= index < len(s.bodies):
        raise ValueError("No such body")
    s.mesh, s.body, s.regions, s.load_case, s.tags = s.bodies[index][1], index, None, None, {}
    return _info()


def scale(factor: float) -> dict:
    """Rescale the loaded part (e.g. inch -> mm). Every body is scaled, so switching keeps the units."""
    s = _require()
    if not 1e-4 <= factor <= 1e4:
        raise ValueError("Scale factor out of range")
    s.bodies = [(n, Mesh(m.triangles * factor)) for n, m in s.bodies]
    s.mesh, s.load_case = s.bodies[s.body][1], None
    return _info()


def _info() -> dict:
    s = _require()
    lo, hi = s.mesh.bounds()
    size = hi - lo
    biggest_bed = max(max(p["bed"]) for p in presets()["printers"].values())
    return {
        "name": s.name,
        "faces": s.mesh.face_count,
        "volume_mm3": abs(s.mesh.volume()),
        "size_mm": size.tolist(),
        "triangles": s.mesh.triangles.astype("<f4").tobytes(),
        "body": s.body,
        "bodies": [{"name": n, "faces": m.face_count, "volume_mm3": abs(m.volume())} for n, m in s.bodies] if len(s.bodies) > 1 else [],
        "unit_hint": unit_hint(size, biggest_bed),
    }


def orientations(profile: dict | None = None, top: int = 5, loads: list[dict] | None = None, faces: dict | None = None) -> dict:
    """Best build directions for the loaded part, plus how it scores as loaded.

    With ``loads``, candidates are also ranked by strength: a weak orientation is
    penalised even when it prints more easily. ``faces`` = {"clean": [region ids],
    "bed": [...], "vertical": [...]}: faces that should stay free of support marks / sit on
    the bed / stand upright (a flat face upright, or a round hole or pin with its axis up).
    """
    s = _require()
    p = Profile.from_dict(profile)
    settings = orientation_settings(p)
    progress.report(0, "Scoring directions")
    candidates = find_orientations(s.mesh, settings, top=3 * top)  # extra, some get filtered out below
    as_loaded = evaluate(s.mesh, [0, 0, 1], settings)
    marked = _marked_faces(faces)
    if marked:
        # Lying on a face the user wants on the bed may not be among the generic best: add it.
        candidates += [evaluate(s.mesh, -_region_normal(s.mesh, m), settings) for m in marked["bed"]]
        for m in marked["vertical"]:
            candidates += [evaluate(s.mesh, up, settings) for up in _upright_directions(s.mesh, m, [c.up for c in candidates])]
        for r in [*candidates, as_loaded]:
            _face_penalty(s.mesh, r, marked, settings)
    progress.report(0.2, "Scoring directions")
    safety = {}
    if loads:
        from .strength import cases_safety, verdict

        case = _load_case(loads)
        ups = np.array([r.up for r in candidates] + [as_loaded.up])
        with stage(0.2, 0.95):
            sfs = cases_safety(case, ups, p, _shells(p))
        strongest = float(max(sfs[:-1].max(initial=0.0), 1e-9))
        relative = STRENGTH_RATIO_WEIGHT.get(p.goal, STRENGTH_RATIO_WEIGHT["balanced"])
        need = required_safety(p)
        for r, sf in zip([*candidates, as_loaded], sfs):
            sf = float(sf)
            safety[id(r)] = sf
            r.reasons.append(_strength_reason(sf, verdict(sf, need), strongest))
            # Below ×2 it may break; above, a much stronger direction is still worth something
            # (how much depends on the goal), so "strong enough either way" doesn't hide a 2× difference.
            r.penalize("strength", STRENGTH_WEIGHT * max(0.0, need - sf) + relative * np.log2(strongest / max(sf, 1e-9)))
        candidates.sort(key=lambda r: r.score)
        strongest_option = max(candidates, key=lambda r: safety[id(r)])
    elif p.goal == "strength":
        with stage(0.2, 0.95):
            _rank_by_weak_spots(s.mesh, candidates, p, extra=[as_loaded])

    if marked:
        candidates.sort(key=lambda r: r.score)
    # Hide options that don't sit on any flat face or are far worse than the best — unless
    # nothing better exists (e.g. a sphere). Users shouldn't have to wade through bad choices.
    flat = [r for r in candidates if r.bed_area > 0]
    if flat:
        best = min(r.score for r in flat)
        keep = [r for r in flat if r.score <= best + MAX_SCORE_GAP] or flat[:1]
        if loads and all(r is not strongest_option for r in keep):  # always offer the strongest direction for the user's load
            keep.append(strongest_option)
        candidates = keep

    # Mirror-image directions of symmetric parts score identically: show one of them.
    unique, seen = [], set()
    for r in candidates:
        key = (round(r.bed_area), round(r.support_area), round(r.height, 1))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    candidates = unique

    candidates = candidates[: top + 2]
    placed = {id(r): _placement(s.mesh, r, p) for r in [*candidates, as_loaded]}
    with stage(0.95 if loads or p.goal == "strength" else 0.2, 1.0):
        _refine(s.mesh, [*candidates, as_loaded], placed, settings, p)
    candidates.sort(key=lambda r: (not placed[id(r)][1]["fits"], r.score))
    candidates = candidates[:top]

    def pack(r, label):
        out = _pack(s.mesh, r, label, settings, placed[id(r)][0], p, safety.get(id(r)))
        if candidates and r is not candidates[0]:
            out["why_lower"] = why_lower(r.terms, candidates[0].terms)
        return out

    return {
        "as_loaded": pack(as_loaded, "As loaded"),
        "orientations": [pack(r, f"Option {i + 1}") for i, r in enumerate(candidates)],
    }


# How each score part reads when an option is worse on it than the recommended one.
WORSE = {
    "support": "needs more support",
    "height": "prints taller (takes longer)",
    "bed": "has less contact with the bed",
    "stability": "stands tall on a narrow base (can tip or wobble)",
    "first_layer": "has a trickier first layer (small spots or thin strips on the bed)",
    "weak": "has more weak spots along the layers",
    "strength": "is weaker under your load",
    "faces": "doesn't keep to the faces you marked",
}


def why_lower(terms: dict[str, float], best: dict[str, float], min_gap: float = 0.05) -> str:
    """Plain-language reason an option ranks below the recommended one, biggest difference first."""
    gaps = sorted(((terms.get(k, 0.0) - best.get(k, 0.0), k) for k in WORSE), reverse=True)
    worse = [WORSE[k] for gap, k in gaps if gap > min_gap][:2]
    if not worse:
        return "Almost as good as the recommended option"
    return "Ranked lower: " + " and ".join(worse)


CLEAN_FACE_WEIGHT = 3.0  # full penalty when a "clean" face is all supported (a strong preference)
BED_FACE_WEIGHT = 10.0  # a face the user wants on the bed isn't on it (an instruction: beats everything else)
VERTICAL_FACE_WEIGHT = 10.0  # same for a face that should stand upright
VERTICAL_TOL = 0.05  # |normal · up| still counted as upright (~3°)
MARK_KINDS = ("clean", "bed", "vertical")


def _marked_faces(faces: dict | None) -> dict | None:
    """Triangle masks for the user's marked faces (regions), or None if nothing is marked."""
    if not faces or not any(faces.get(k) for k in MARK_KINDS):
        return None
    from .regions import find_regions

    s = _require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    return {kind: [s.regions == r for r in faces.get(kind, [])] for kind in MARK_KINDS}


def _normal_spread(mesh: Mesh, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Eigenvalues (ascending, sum 1) and eigenvectors of the area-weighted Σ n nᵀ of a face.
    A flat face has one direction (its normal); a cylinder's normals avoid only its axis."""
    normals, areas = mesh.normals_and_areas()
    n, a = normals[mask], areas[mask]
    w, v = np.linalg.eigh((n * a[:, None]).T @ n)
    return w / w.sum(), v


def _tilt(mesh: Mesh, mask: np.ndarray, up) -> float:
    """RMS of normal · up over the face: 0 when it stands upright."""
    normals, areas = mesh.normals_and_areas()
    d = normals[mask] @ (np.asarray(up) / np.linalg.norm(up))
    return float(np.sqrt((areas[mask] * d * d).sum() / areas[mask].sum()))


def _upright_directions(mesh: Mesh, mask: np.ndarray, ups) -> list[np.ndarray]:
    """Build directions that keep the face upright: each candidate projected onto the directions the
    face's normals don't use (for a round hole, its axis)."""
    w, v = _normal_spread(mesh, mask)
    used = v[:, w > 0.05]
    out = []
    for up in ups:
        u = np.asarray(up, float)
        u = u - used @ (used.T @ u)
        if np.linalg.norm(u) > 0.1:
            out.append(u / np.linalg.norm(u))
    if not out:
        out = [v[:, 0], -v[:, 0]]
    return out


def _region_normal(mesh: Mesh, mask: np.ndarray) -> np.ndarray:
    normals, areas = mesh.normals_and_areas()
    n = (normals[mask] * areas[mask, None]).sum(axis=0)
    return n / np.linalg.norm(n)


def _face_penalty(mesh: Mesh, r, marked: dict, settings: OrientationSettings) -> None:
    from .orientation import BED, NEEDS_SUPPORT

    _, areas = mesh.normals_and_areas()
    classes = classify_faces(mesh, r.up, settings)
    penalty, notes = 0.0, []
    for m in marked["clean"]:
        frac = areas[m & (classes == NEEDS_SUPPORT)].sum() / areas[m].sum()
        penalty += CLEAN_FACE_WEIGHT * frac
        if frac > 0.05:
            notes.append(f"Supports would touch a face you want clean ({frac:.0%} of it)")
    for m in marked["bed"]:
        frac = areas[m & (classes == BED)].sum() / areas[m].sum()
        penalty += BED_FACE_WEIGHT * (1 - frac)
        notes.append("The face you picked sits on the bed" if frac > 0.9 else "The face you want on the bed is not on the bed")
    for m in marked["vertical"]:
        tilt = _tilt(mesh, m, r.up)
        penalty += VERTICAL_FACE_WEIGHT * min(1.0, max(0.0, tilt - VERTICAL_TOL) / 0.2)
        notes.append("The face you picked stands upright" if tilt <= VERTICAL_TOL else "The face you want upright is tilted or lying")
    r.penalize("faces", penalty)
    r.reasons += notes


SCAR_WEIGHT = 1.0  # support resting on the part: rough marks on a visible surface, hard to remove
REFINE_BUDGET = 3e7  # faces × layers for all finalists together


def _refine(mesh: Mesh, results: list, placed: dict, settings: OrientationSettings, p: Profile) -> None:
    """Re-score the finalists with sliced layers: real bed contact, real support volume, and
    support that lands on the part instead of the bed (see refine.py)."""
    from .orientation import _size, stability_term
    from .refine import support_stats

    L = _size(mesh)
    # Slicing cost ~ faces × layers: keep the whole refinement within a fixed budget on big parts.
    layers = int(np.clip(REFINE_BUDGET / (mesh.face_count * len(results)), 8, 40))
    for i, r in enumerate(results):
        progress.report(i / len(results), "Checking supports layer by layer")
        st = support_stats(orient(mesh, placed[id(r)][0]), p, max_layers=layers)
        frac = st["bed_area"] / L**2
        new = {
            "support": settings.w_support_area * st["support_area"] / L**2
            + settings.w_support_volume * st["support_volume"] / L**3
            + SCAR_WEIGHT * st["scar_area"] / L**2,
            "bed": settings.w_small_bed * float(np.clip(1.0 - frac / settings.good_bed_fraction, 0.0, 1.0))
            - settings.w_bed_contact * min(frac, 1.0),
            "stability": float(stability_term(r.height, st["bed_area"], settings)),
            "first_layer": settings.w_small_spots * min(st["small_spots"], 3) + settings.w_thin_first_layer * st["thin_share"],
        }
        for k, v in new.items():
            r.penalize(k, v - r.terms.get(k, 0.0))
        r.bed_area, r.support_volume = st["bed_area"], st["support_volume"]
        if st["small_spots"]:
            n = st["small_spots"]
            r.reasons.append(f"{n} small spot{'s' if n > 1 else ''} on the bed (a tip, post or corner): can come loose while printing")
        if st["thin_share"] > 0.3:
            r.reasons.append(f"Much of the first layer is thin strips ({st['thin_share']:.0%}): they stick badly")
        if st["scar_area"] > max(0.02 * L**2, 25.0):
            r.reasons.append(f"Some supports stand on the part itself (~{st['scar_area']:.0f} mm² of rough marks)")


def _moving(p: Profile) -> bool:
    """The printer moves its bed back and forth (a bed slinger): tall thin parts are turned on it (placement)."""
    return bool(p.printer.get("moving_bed", False))


def _placement(mesh: Mesh, r, p: Profile):
    """Print rotation for a candidate; adds a reason when it doesn't fit or had to be turned."""
    rotation, fit = print_rotation(mesh, r.up, p.printer["bed"], _moving(p))
    if not fit["fits"]:
        r.reasons.insert(0, f"Too big for the {p.printer['name']}: {fit['reason']}")
    elif fit["turned_deg"]:
        r.reasons.append(f"Turned {fit['turned_deg']:.0f}° on the bed " + ("so it doesn't wobble as the bed moves" if fit.get("for_bed_motion")
                                                                         else "so it fits"))
    return rotation, fit


BED_CONTACT_WEIGHT = {"low": 0.0, "medium": 0.25, "high": 0.5}  # by the material's warp_risk


def orientation_settings(p: Profile) -> OrientationSettings:
    """Scoring weights for the user's goal. Multi-material makes supports cheaper (clean interfaces).
    A footprint bigger than needed only helps materials that warp (ABS, ASA, PC, nylon): PLA and
    PETG stick fine on any decent flat face, so there the other terms decide."""
    s = OrientationSettings(max_overhang_deg=p.material["max_overhang_deg"],
                            w_bed_contact=BED_CONTACT_WEIGHT[p.material.get("warp_risk", "high")])
    if p.goal == "speed":
        s.w_height, s.w_support_volume = 0.4, 1.5
    elif p.goal == "material":
        s.w_support_volume, s.w_height = 2.5, 0.1
    if p.multi_material:
        s.w_support_area *= 0.5
    return s


WEAK_SPOT_PENALTY = 1.0  # per weak finding (upright pin, neck, single-line wall) when the goal is strength


def _rank_by_weak_spots(mesh: Mesh, candidates: list, p: Profile, extra: list = (), check_top: int = 6):
    """Without loads, 'strength' means: avoid orientations that print pins upright or make necks.
    Runs a quick check on the best few candidates (and ``extra``, e.g. as-loaded) and re-sorts them."""
    todo = [*candidates[:check_top], *extra]
    for i, r in enumerate(todo):
        rotation, _ = print_rotation(mesh, r.up, p.printer["bed"], _moving(p))
        with stage(i / len(todo), (i + 1) / len(todo)):
            found = run_checks(orient(mesh, rotation), p, max_layers=50)["findings"]
        weak = [f for f in found if f["impact"] == "weak"]
        r.penalize("weak", WEAK_SPOT_PENALTY * min(len(weak), 3))
        if weak:
            r.reasons.append(f"{len(weak)} weak spot(s) in this orientation (e.g. {weak[0]['rule'].replace('_', ' ')})")
        else:
            r.reasons.append("No weak spots along the layers")
    candidates[:check_top] = sorted(candidates[:check_top], key=lambda r: r.score)


def orientation(up, profile: dict | None = None, label: str = "Custom") -> dict:
    """One specific build direction, packed like the entries of ``orientations``."""
    s = _require()
    p = Profile.from_dict(profile)
    settings = orientation_settings(p)
    r = evaluate(s.mesh, up, settings)
    placed = _placement(s.mesh, r, p)
    _refine(s.mesh, [r], {id(r): placed}, settings, p)  # sliced support, as the other options have
    return _pack(s.mesh, r, label, settings, placed[0], p)


def _pack(mesh: Mesh, r, label: str, settings: OrientationSettings, rotation: np.ndarray, p: Profile,
          safety: float | None = None) -> dict:
    """``support_g``: rough grams of support for this direction (as check's stats), so each option can
    say what it costs next to the part itself."""
    return {
        "label": label,
        "up": r.up.tolist(),
        "rotation": rotation.tolist(),
        "score": r.score,
        "support_area_mm2": r.support_area,
        "bed_area_mm2": r.bed_area,
        "height_mm": r.height,
        "safety_factor": safety,
        "support_g": round(r.support_volume * SUPPORT_FILL / 1000 * p.material["density_g_cm3"], 1),
        "reasons": r.reasons,
        "score_terms": {k: round(v, 3) for k, v in r.terms.items()},
        "face_classes": classify_faces(mesh, r.up, settings).tobytes(),
    }


STRENGTH_WEIGHT = 1.5  # score penalty per unit of safety factor below 2
# Penalty per halving of safety compared with the strongest direction, by goal: with "strength"
# a twice-as-strong direction beats a lot of support; balanced weighs it against support and time.
STRENGTH_RATIO_WEIGHT = {"strength": 1.5, "balanced": 1.0, "speed": 0.4, "material": 0.4}
MAX_SCORE_GAP = 3.0  # options scoring this much worse than the best are not shown


def _strength_reason(sf: float, verdict: str, strongest: float | None = None) -> str:
    out = f"Strong enough for your load (safety ×{sf:.1f})" if verdict == "fine" else f"{verdict.capitalize()} under your load (safety ×{sf:.1f})"
    if strongest and strongest / max(sf, 1e-9) >= 1.2:
        out += f" — the strongest direction holds {strongest / sf:.1f}× more"
    elif strongest:
        out += " — the strongest direction for your load"
    return out


def _strength_story(mesh: Mesh, up, r: dict, p: Profile) -> str:
    """The result in plain words: will it hold, where it gives way, and why there."""
    sf, z = r["safety_factor"], r["critical_point_print"][2]
    if not np.isfinite(sf):
        return "The load hardly stresses the part. It will hold."
    head = {
        "fine": f"It should hold: it takes about {sf:.1f}× the load you set before it breaks.",
        "borderline": f"It might break: it takes only {sf:.1f}× the load you set, and real prints vary by about that much.",
        "likely to break": f"It will most likely break: it holds only about {sf:.0%} of the load you set.",
    }[r["verdict"]]
    why = {
        "layers": "There the layers get pulled or slid apart. The bond between layers is the weakest part of any print, so turning the part helps most.",
        "material": "There the plastic itself is overloaded, not the bond between layers. More walls, more infill or a thicker design help most.",
        "lines": ("There the printed lines lying side by side get pulled apart, like a screw tearing out through the walls."
                  " More walls around that spot, more material there, or turning the part so the lines run along the pull help most."),
    }[r["mode"]]
    near = (" It's right next to where the part is held: a real screw, clamp or glue spreads the load a bit,"
            " so it is probably somewhat stronger there than shown.") if r.get("near_support") else ""
    return f"{head} The weakest spot is about {max(z, 0):.0f} mm above the bed (red arrow). {why}{near}{_use_story(p)}"


def _use_story(p: Profile) -> str:
    """What heat and a load that stays on do to the result, in plain words."""
    from .service import CONSTANT_LOAD_SAFETY, heat_factor, heat_limit

    out = ""
    k = heat_factor(p)
    if k < 0.999:
        name, limit, t = p.material["name"], heat_limit(p), p.use_temp_c
        if t >= limit:
            out += (f" At {t:.0f} °C {name} is past the point where it goes soft (about {limit:.0f} °C): it keeps only about {k:.0%} of"
                    " its stiffness and strength and will sag or bend over time even under a small load. A material that takes more heat is the real fix.")
        else:
            out += f" At {t:.0f} °C {name} keeps only about {k:.0%} of its strength and stiffness (already included above)."
    if p.constant_load:
        out += (f" The load stays on: plastic slowly stretches under a constant load (creep), so it needs about ×{CONSTANT_LOAD_SAFETY:.0f}"
                " instead of ×2. Warmth makes creep much worse.")
    return out


OPT_TARGET = {"strength": 3.0}  # safety the optimiser aims for, by goal (default 2.0)
OPT_WALLS = range(2, 11)
OPT_LAYERS = range(3, 13)
OPT_INFILL = (0.10, 0.15, 0.20, 0.25, 0.35, 0.50, 0.75, 1.0)
OPT_MAX_STEPS = 16


def optimise_strength(up, loads: list[dict], profile: dict | None = None, target: float | None = None, zones: bool = False) -> dict:
    """The least plastic that reaches ``target`` safety (default ×2, ×3 for the strength goal):
    start from a light print and add, one step at a time, whatever buys the most safety per gram
    — walls, top/bottom layers or infill. Tried for this orientation and the strongest one.
    ``zones``: also try stronger zones only around the most stressed places (slicer modifier
    volumes), the rest of the part light; returned as ``zoned`` with the plastic it saves."""
    from .strength import cases_safety, evaluate_cases, find_zones

    s = _require()
    p = Profile.from_dict(profile)
    cases = _load_case(loads)
    target = float(target or max(OPT_TARGET.get(p.goal, 2.0), required_safety(p)))
    up = (np.asarray(up, float) / np.linalg.norm(up)).tolist()
    axes = np.vstack([np.eye(3), -np.eye(3)])
    strongest = axes[int(np.argmax(cases_safety(cases, axes, p, _shells(p))))]
    ups = [up] + ([strongest.tolist()] if abs(float(np.dot(strongest, up))) < 0.99 else [])
    plans = []
    for i, u in enumerate(ups):
        with stage(i / len(ups), (i + 1) / len(ups)):
            plans.append(_climb(cases, u, p, target, s.mesh))
    for plan, u in zip(plans, ups):
        plan["up"] = u
        plan["orientation"] = "this one" if u is up else "strongest"
    # Reaching the target wins; then the lighter print; if neither does, the safer one.
    best = min(plans, key=lambda q: (not q["met"], q["grams"] if q["met"] else -q["safety_factor"]))
    best["alternatives"] = [q for q in plans if q is not best]
    best["target"] = target
    best["explanation"] = _optimise_story(best, target, len(plans) > 1)
    if not best["met"]:  # settings alone aren't enough: what else would do it
        from .strength import Shells

        sh = Shells(walls=best["walls"], infill=best["infill"], top=best["top"], bottom=best["bottom"])
        best["beyond"] = _strongest_first(_beyond_settings(evaluate_cases(cases, best["up"], p, sh), p, profile, loads, target))
    if zones:
        with stage(0.9, 1.0):
            light = _shells(p, walls=OPT_WALLS[0], infill=OPT_INFILL[0])
            r = evaluate_cases(cases, best["up"], p, light)
            worst = cases[[c.number for c in cases].index(r["case"])]
            boxes = find_zones(cases[0].model, r["fi"], worst.ignore, best["up"], OPT_WALLS[0], OPT_INFILL[0])
            if boxes:
                zoned = _climb(cases, best["up"], p, target, s.mesh, boxes)
                zoned["up"] = best["up"]
                zoned["saves_grams"] = round(best["grams"] - zoned["grams"], 1)
                zoned["saves_min"] = round(_grams_minutes(zoned["saves_grams"], p), 1)
                zoned["explanation"] = _zoned_story(zoned, best, target)
                best["zoned"] = zoned
    return best


def _zone_info(z, mesh: Mesh, up, bed, moving: bool = False) -> dict:
    """A zone for the UI and the export: its box (build frame), settings and corners on the bed."""
    from itertools import product

    r0 = rotation_to_z(up)
    rot = print_rotation(mesh, up, bed, moving)[0]
    corners = np.array(list(product(*zip(z.lo, z.hi)))) @ r0  # build frame -> part
    return {"lo": list(z.lo), "hi": list(z.hi), "walls": z.walls, "infill": z.infill, "shape": z.shape,
            "corners_print": [_to_print_frame(mesh, rot, c) for c in corners]}


ZONE_MARGIN_MM = 3.0  # a zone reaches this far past the picked face, so the slicer's walls there are inside it


def zone_at(up, region: int | None = None, point=None, margin: float = ZONE_MARGIN_MM, walls: int | None = None,
            infill: float | None = None, profile: dict | None = None, shape: str | None = None) -> dict:
    """A zone the user picks: an upright box around a face (``region``) or a spot (``point``, part
    coordinates), ``margin`` mm bigger on every side, printed with more walls and infill (default
    two more walls and 40 %). Same form as the optimiser's zones, so strength and export take it.
    ``shape`` "box", "cylinder" or "ball" (strength.ZONE_SHAPES); default a cylinder around a round
    hole or post that stands upright, else a box."""
    from .regions import find_regions
    from .strength import ZONE_SHAPES, Zone

    if shape is not None and shape not in ZONE_SHAPES:
        raise ValueError(f"shape must be one of {ZONE_SHAPES}")

    s = _require()
    p = Profile.from_dict(profile)
    if region is not None:
        if s.regions is None:
            s.regions = find_regions(s.mesh)
        pts = s.mesh.triangles[np.asarray(s.regions) == int(region)].reshape(-1, 3)
        if not len(pts):
            raise ValueError(f"Unknown face {region}")
    elif point is not None:
        pts = np.asarray([point], float)
    else:
        raise ValueError("Give a face (region) or a point")
    built = pts @ rotation_to_z(up).T
    plan = goal_plan(p)
    if shape is None:
        shape = "cylinder" if region is not None and _upright_round(int(region), up) else "box"
    lo, hi = built.min(axis=0) - margin, built.max(axis=0) + margin
    if shape != "box":  # the round shape sits inside its box: grow the box so it still reaches past the face
        grow = (hi - lo) * (np.sqrt(2) - 1) / 2 if shape == "ball" else np.r_[(hi - lo)[:2] * (np.sqrt(2) - 1) / 2, 0.0]
        if region is not None and shape == "cylinder" and _upright_round(int(region), up):
            grow[:2] = 0.0  # a circle around a round face already fits it
        lo, hi = lo - grow, hi + grow
    z = Zone(tuple(lo.tolist()), tuple(hi.tolist()),
             int(walls if walls is not None else plan["walls"] + 2), float(infill if infill is not None else max(0.4, plan["infill"] / 100)),
             shape=shape)
    return _zone_info(z, s.mesh, up, p.printer["bed"], _moving(p))


VOLUME_KINDS = {"support": "Support here", "blocker": "No support here", "void": "Hidden gap (extra walls)", "keep_clear": "Keep clear",
                "add_solid": "Add plastic", "cut_solid": "Cut away"}
SOLID_KINDS = ("add_solid", "cut_solid")  # change the model itself (volume_solid), not for the slicer
DESIGN_ONLY = ("keep_clear", *SOLID_KINDS)  # not in the 3MF
VOID_MM = 0.6  # a gap picked on a spot: a thin upright pin hole, the "holes that force walls" trick
VOID_COVER_MM = 1.0  # plastic left above and below such a pin hole, so it doesn't show


def volume_at(up, kind: str, region: int | None = None, point=None, point2=None, margin: float | None = None,
              profile: dict | None = None, shape: str = "box") -> dict:
    """An advanced design box, upright in the build frame like a zone: around a face (``region``),
    a spot (``point``) or between two spots (``point`` and ``point2``, part coordinates), ``margin``
    mm bigger on every side. ``kind``: "support" / "blocker" (support enforcer / blocker in the 3MF),
    "void" (a hidden gap: a negative volume the slicer prints walls round; on one spot a thin pin
    hole up through the part that stays inside it), "keep_clear" (space that must stay empty; not
    exported), "add_solid" / "cut_solid" (a block of plastic added to or cut from the model itself: see
    volume_solid). Returns the box, its corners on the bed and plain notes on what it does here. ``shape``:
    "box", "cylinder" or "ball" inside that box (strength.ZONE_SHAPES)."""
    from .regions import find_regions

    if kind not in VOLUME_KINDS:
        raise ValueError(f"Unknown kind {kind!r}: use one of {', '.join(VOLUME_KINDS)}")
    s = _require()
    p = Profile.from_dict(profile)
    r0 = rotation_to_z(up)
    if region is not None:
        if s.regions is None:
            s.regions = find_regions(s.mesh)
        pts = s.mesh.triangles[np.asarray(s.regions) == int(region)].reshape(-1, 3)
        if not len(pts):
            raise ValueError(f"Unknown face {region}")
    elif point is not None:
        pts = np.asarray([point] + ([point2] if point2 is not None else []), float)
    else:
        raise ValueError("Give a face (region) or a point")
    built = pts @ r0.T
    zs = (s.mesh.triangles.reshape(-1, 3) @ r0.T)[:, 2]
    pin = kind == "void" and region is None and point2 is None
    if margin is None:
        margin = VOID_MM / 2 if pin else ZONE_MARGIN_MM
    lo, hi = built.min(axis=0) - margin, built.max(axis=0) + margin
    inside = _inside_test(s.mesh, r0)
    if pin:
        lo[2], hi[2] = _solid_run(inside, built[0], zs.min(), zs.max())
    # Slicer boxes stay between the bed and the top of the part (see _zone_box); keep-clear space may be above it.
    lo[2], hi[2] = max(lo[2], zs.min()), (hi[2] if kind in DESIGN_ONLY else min(hi[2], zs.max()))
    if np.any(hi - lo <= 0):
        raise ValueError("That box is outside the part's height")
    from .strength import ZONE_SHAPES

    if shape not in ZONE_SHAPES:
        raise ValueError(f"shape must be one of {ZONE_SHAPES}")
    box = {"kind": kind, "lo": lo.tolist(), "hi": hi.tolist(), "walls": 0, "infill": 0.0, "shape": shape}
    corners = _zone_info(_box_zone(box), s.mesh, up, p.printer["bed"], _moving(p))["corners_print"]
    return {**box, "corners_print": corners, "notes": _volume_notes(s.mesh, r0, inside, box, p)}


def volume_solid(up, boxes: list[dict]) -> list[dict]:
    """The "add plastic" / "cut away" boxes (volume_at) as design edits in part coordinates, for the
    web app to apply like any design change (a new version of the model)."""
    r0 = rotation_to_z(up)
    unit = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)
    faces = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    out = []
    for i, b in enumerate(boxes):
        if b.get("kind") not in SOLID_KINDS:
            continue
        lo, hi = np.asarray(b["lo"], float), np.asarray(b["hi"], float)
        tris = ((unit * (hi - lo) + lo)[faces]) @ r0  # build frame -> part
        add = b["kind"] == "add_solid"
        size = " × ".join(f"{v:.0f}" for v in hi - lo)
        out.append({"id": f"solid-{i}", "rule": "design_change", "title": f"{'Add' if add else 'Cut away'} a {size} mm block",
                    "description": "Placed under Advanced design changes.", "op": "add" if add else "cut", "param": None,
                    "mesh": tris.astype("<f4").tobytes(), "pieces": [len(tris)]})
    return out


SNAP_SINK_MM = 0.05  # the snapped model reaches this far into the part, so the union joins them


def snap_model(data: bytes, point, normal, spin_deg: float = 0.0, name: str = "other part") -> dict:
    """Put another model (STL or 3MF bytes) onto the part: its biggest flat face goes flat onto
    the part's face at ``point`` (part coordinates, ``normal`` pointing out of the part), centred there
    and turned ``spin_deg`` about that normal. Returns a design edit (``op: "add"``) the web app joins
    to the part as a new version: an assembly made into one print, or a bracket stuck onto a box."""
    from .regions import find_regions, region_info

    other = max(read_bodies(bytes(data)), key=lambda b: abs(b[1].volume()))[1]
    info = region_info(other, find_regions(other))
    face = max((r for r in info if r["kind"] == "flat"), key=lambda r: r["area_mm2"], default=None)
    if face is None:
        raise ValueError("The other model has no flat face to put down")
    n_to = -np.asarray(normal, float) / np.linalg.norm(normal)  # its face looks into the part
    turn = _rotation_between(np.asarray(face["normal"], float), n_to)
    spin = _axis_rotation(n_to, np.radians(spin_deg))
    tris = (other.triangles - np.asarray(face["centroid"], float)) @ (spin @ turn).T + np.asarray(point, float) + n_to * SNAP_SINK_MM
    lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
    return {"id": "snap", "rule": "design_change", "title": f"Add {name}",
            "description": f"Its biggest flat face ({face['area_mm2']:.0f} mm²) sits on the clicked face; it is {hi[0] - lo[0]:.0f} × {hi[1] - lo[1]:.0f} × {hi[2] - lo[2]:.0f} mm.",
            "op": "add", "param": None, "mesh": tris.astype("<f4").tobytes(), "pieces": [len(tris)]}


def _rotation_between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Rotation matrix turning unit vector ``a`` onto ``b`` (a half turn about any perpendicular when opposite)."""
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v, c = np.cross(a, b), float(a @ b)
    if c < -0.999999:
        p = np.cross(a, [1.0, 0, 0] if abs(a[0]) < 0.9 else [0, 1.0, 0])
        return _axis_rotation(p / np.linalg.norm(p), np.pi)
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + k + k @ k / (1 + c)


def _axis_rotation(axis: np.ndarray, angle: float) -> np.ndarray:
    axis = axis / np.linalg.norm(axis)
    k = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + np.sin(angle) * k + (1 - np.cos(angle)) * k @ k


def _keep_clear_clash(edits: list[dict], up, keep_clear: list[dict] | None) -> list[dict]:
    """Design edits that add plastic into a keep-clear box get ``clash`` (plain words) so the app
    leaves them unticked: the user said that space has to stay empty."""
    if not keep_clear:
        return edits
    r0 = rotation_to_z(up)
    for e in edits:
        if e["op"] != "add":
            continue
        tris = np.frombuffer(e["mesh"], "<f4").reshape(-1, 3, 3).astype(float)
        pts = np.concatenate([tris.reshape(-1, 3), tris.mean(axis=1), (tris + np.roll(tris, 1, axis=1)).reshape(-1, 3) / 2]) @ r0.T
        for b in keep_clear:
            if np.all((pts >= np.asarray(b["lo"]) - 1e-6) & (pts <= np.asarray(b["hi"]) + 1e-6), axis=1).any():
                e["clash"] = "It reaches into a space you marked to keep clear, so it is not ticked."
                break
    return edits


def _box_zone(box: dict):
    """A zone or advanced design box (dict) as the strength model's Zone."""
    from .strength import Zone

    return Zone(tuple(box["lo"]), tuple(box["hi"]), int(box.get("walls", 0)), float(box.get("infill", 0)), box.get("kind") == "void",
                box.get("shape", "box"))


def _upright_round(region: int, up) -> bool:
    """Is face ``region`` a round hole or post whose axis stands upright for this build direction?"""
    from .features import accepted, detect_features

    s = _require()
    for f in accepted(detect_features(s.mesh, s.regions, s.tags)):
        if f["region"] == region:
            return abs(np.dot(f["axis"], np.asarray(up, float) / np.linalg.norm(up))) > 0.98
    return False


def _inside_test(mesh: Mesh, r0: np.ndarray):
    """Is a point (build frame) inside the part? A voxel test: fine enough for sizes and notes."""
    from .voxel import voxelize

    grid = voxelize(mesh, 60_000)

    def inside(built):
        idx = grid.index_of(np.asarray(built, float) @ r0)
        ok = np.all((idx >= 0) & (idx < np.array(grid.solid.shape)), axis=1)
        out = np.zeros(len(idx), dtype=bool)
        out[ok] = grid.solid[tuple(idx[ok].T)]
        return out

    inside.h = grid.h
    return inside


def _solid_run(inside, at: np.ndarray, zmin: float, zmax: float) -> tuple[float, float]:
    """Heights of the solid run straight up and down through ``at`` (build frame), less
    ``VOID_COVER_MM`` at both ends: where a pin hole can go without showing."""
    zs = np.linspace(zmin, zmax, 400)
    solid = inside(np.column_stack([np.full_like(zs, at[0]), np.full_like(zs, at[1]), zs]))
    if not solid.any():
        raise ValueError("That spot isn't on the part")
    rows = np.flatnonzero(solid)
    k = int(rows[np.argmin(np.abs(zs[rows] - at[2]))])  # a click on the surface: the nearest solid point
    a = b = k
    while a > 0 and solid[a - 1]:
        a -= 1
    while b < len(zs) - 1 and solid[b + 1]:
        b += 1
    lo, hi = zs[a] + VOID_COVER_MM, zs[b] - VOID_COVER_MM
    if hi <= lo:
        raise ValueError("The part is too thin there for a hidden gap")
    return lo, hi


SHAPE_VOLUME = {"box": 1.0, "cylinder": np.pi / 4, "ball": np.pi / 6}  # share of the box it fills


def _volume_notes(mesh: Mesh, r0: np.ndarray, inside, box: dict, p: Profile) -> list[str]:
    """What an advanced design box does on this part, in plain words."""
    lo, hi = np.asarray(box["lo"]), np.asarray(box["hi"])
    kind, zone = box["kind"], _box_zone(box)
    if kind in ("void", "keep_clear"):
        n = np.clip(np.ceil((hi - lo) / (inside.h / 2)).astype(int) + 1, 2, 40)
        g = np.stack(np.meshgrid(*[np.linspace(lo[i], hi[i], n[i]) for i in range(3)], indexing="ij"), -1).reshape(-1, 3)
        g = g[zone.gap(g) <= 1e-9]
        filled = inside(g)
        if kind == "keep_clear":
            if filled.mean() < 0.01:
                return ["The part stays out of it."]
            volume = float(np.prod(hi - lo)) * SHAPE_VOLUME[zone.shape]
            return [f"The part fills about {filled.mean():.0%} of this space ({filled.mean() * volume / 1000:.1f} cm³): "
                    "change the design to clear it."]
        notes = ["The slicer prints walls all round it, so the plastic next to it is solid: stronger there, a little heavier. "
                 "The strength check counts it (roughly)."]
        c, r = (lo + hi) / 2, (hi - lo) / 2
        rim = _box_zone({**box, "lo": c - 0.9 * r, "hi": c + 0.9 * r}).gap(g) > 1e-9  # the outer tenth of the shape
        if not filled[rim].all():
            notes.append("It reaches the outside of the part, so it shows as a hole in the surface. Keep it inside if it should stay hidden.")
        return notes
    if kind in SOLID_KINDS:
        size = float(np.prod(hi - lo))
        n = np.clip(np.ceil((hi - lo) / (inside.h / 2)).astype(int) + 1, 2, 40)
        g = np.stack(np.meshgrid(*[np.linspace(lo[i], hi[i], n[i]) for i in range(3)], indexing="ij"), -1).reshape(-1, 3)
        filled = float(inside(g).mean())
        if kind == "add_solid":
            new = (1 - filled) * size / 1000
            if filled == 0:
                return [f"Adds about {new:.1f} cm³ of plastic, but it doesn't touch the part: move it so it overlaps."]
            return [f"Adds about {new:.1f} cm³ of plastic. Apply it to make a new version of the model (Undo goes back)."]
        if filled < 0.01:
            return ["The part isn't in this box: nothing to cut."]
        return [f"Cuts away about {filled * size / 1000:.1f} cm³ of the part. Apply it to make a new version of the model (Undo goes back)."]
    normals, areas = mesh.normals_and_areas()
    c = mesh.triangles.mean(axis=1) @ r0.T
    inbox = zone.gap(c) <= 1e-9
    steep = (normals @ r0.T)[:, 2] < -np.cos(np.radians(p.material["max_overhang_deg"]))
    above_bed = c[:, 2] > (mesh.triangles.reshape(-1, 3) @ r0.T)[:, 2].min() + 0.2
    hang = float(areas[inbox & steep & above_bed].sum())
    if kind == "support":
        return ["Supports are printed here, even with the slicer's automatic supports off."
                + ("" if hang > 1 else " Nothing here overhangs much: check it's the right place.")]
    if hang > 1:
        return [f"About {hang:.0f} mm² here hangs too far out to print in the air: it may sag or fail without support."]
    return ["No support is printed here."]


def _grams_minutes(grams: float, p: Profile) -> float:
    """About how long ``grams`` of plastic take to print (checks.print_minutes)."""
    from .checks import print_minutes

    return print_minutes(grams / p.material["density_g_cm3"] * 1000, p)


def _zoned_story(zoned: dict, plain: dict, target: float) -> str:
    z = zoned["zones"][0]
    head = (f"Stronger only where it's needed: {len(zoned['zones'])} zone{'s' if len(zoned['zones']) > 1 else ''} with "
            f"{z['infill']:.0%} infill and {z['walls']} walls, the rest {zoned['walls']} walls and {zoned['infill']:.0%} infill")
    if not zoned["met"]:
        return f"{head} only reach ×{zoned['safety_factor']:.1f} (you need ×{target:.0f})."
    if zoned["saves_grams"] > 0.5:
        time = f" and about {zoned['saves_min']:.0f} min" if zoned.get("saves_min", 0) >= 1 else ""
        return (f"{head}: ×{zoned['safety_factor']:.1f} with {zoned['grams']:.0f} g — {zoned['saves_grams']:.0f} g{time} less than the same settings"
                " everywhere (checked again with the zones).")
    return f"{head}: ×{zoned['safety_factor']:.1f}, but it saves almost nothing here — the load spreads through most of the part."


def _climb(cases, up, p: Profile, target: float, mesh: Mesh, zones: list | None = None) -> dict:
    """Greedy search from a light print: each step takes the change with the most safety per gram.
    With ``zones`` the zones' own infill and walls are steps too."""
    from dataclasses import replace

    from .strength import Shells, evaluate_cases, material_volume

    model = cases[0].model
    to_grams = abs(mesh.volume()) / (len(model.elems) * model.grid.h**3) / 1000 * p.material["density_g_cm3"]
    memo: dict = {}

    def sf(sh):
        if sh not in memo:
            memo[sh] = evaluate_cases(cases, up, p, sh)["safety_factor"]
        return memo[sh]

    def grams(sh):
        return material_volume(model, up, sh, p) * to_grams

    sh = Shells(walls=OPT_WALLS[0], infill=OPT_INFILL[0], top=OPT_LAYERS[0], bottom=OPT_LAYERS[0], zones=tuple(zones or ()))
    solid = Shells(walls=OPT_WALLS[-1], infill=OPT_INFILL[-1], top=OPT_LAYERS[-1], bottom=OPT_LAYERS[-1],
                   zones=tuple(replace(z, walls=OPT_WALLS[-1], infill=OPT_INFILL[-1]) for z in zones or ()))
    if sf(solid) < target:  # not even a solid print is enough: no need to search
        sh = solid
    steps = []
    for n in range(OPT_MAX_STEPS):
        progress.report(n / OPT_MAX_STEPS, "Trying settings")
        if sf(sh) >= target:
            break
        moves = []
        if sh.walls + 1 in OPT_WALLS:
            moves.append((replace(sh, walls=sh.walls + 1), f"{sh.walls + 1} walls"))
        if sh.top + 1 in OPT_LAYERS:
            moves.append((replace(sh, top=sh.top + 1, bottom=sh.bottom + 1), f"{sh.top + 1} top and bottom layers"))
        k = OPT_INFILL.index(sh.infill)
        if k + 1 < len(OPT_INFILL):
            moves.append((replace(sh, infill=OPT_INFILL[k + 1]), f"{OPT_INFILL[k + 1]:.0%} infill"))
        if sh.zones:  # all zones step together
            z = sh.zones[0]
            k = OPT_INFILL.index(z.infill)
            if k + 1 < len(OPT_INFILL):
                moves.append((replace(sh, zones=tuple(replace(q, infill=OPT_INFILL[k + 1]) for q in sh.zones)), f"{OPT_INFILL[k + 1]:.0%} infill in the zones"))
            if z.walls + 1 in OPT_WALLS:
                moves.append((replace(sh, zones=tuple(replace(q, walls=z.walls + 1) for q in sh.zones)), f"{z.walls + 1} walls in the zones"))
        gain = [(np.log(sf(m) / sf(sh)) / max(grams(m) - grams(sh), 0.05), m, label) for m, label in moves if np.isfinite(sf(m))]
        gain = [g for g in gain if g[0] > 1e-4]
        if not gain:
            break
        _, sh, label = max(gain, key=lambda g: g[0])
        steps.append({"change": label, "safety_factor": float(sf(sh))})
    settings = {"wall_loops": str(sh.walls), "top_shell_layers": str(sh.top), "bottom_shell_layers": str(sh.bottom),
                "sparse_infill_density": f"{sh.infill:.0%}"}
    if p.goal == "material":  # lightning infill only holds up the top surface, not a load
        settings["sparse_infill_pattern"] = "gyroid"
    out = {"met": bool(sf(sh) >= target), "safety_factor": float(sf(sh)), "grams": round(grams(sh), 1),
           "walls": sh.walls, "top": sh.top, "bottom": sh.bottom, "infill": sh.infill, "settings": settings, "steps": steps}
    if sh.zones:
        out["zones"] = [_zone_info(replace(z, walls=max(z.walls, sh.walls), infill=max(z.infill, sh.infill)), mesh, up, p.printer["bed"], _moving(p))
                        for z in sh.zones]
    return out


def _optimise_story(best: dict, target: float, compared: bool) -> str:
    where = " Printed in the strongest orientation." if best["orientation"] == "strongest" else ""
    setting = f"{best['walls']} walls, {best['top']} top / {best['bottom']} bottom layers and {best['infill']:.0%} infill"
    if best["met"]:
        out = f"{setting} reach ×{best['safety_factor']:.1f} (you need ×{target:.0f}) with about {best['grams']:.0f} g of plastic.{where}"
        alt = best["alternatives"]
        if compared and alt and alt[0]["met"]:
            out += f" The other orientation needs {alt[0]['grams']:.0f} g for the same."
        return out
    return (f"Even {setting} only reach ×{best['safety_factor']:.1f} (you need ×{target:.0f}).{where} "
            "Settings alone can't make it strong enough: make the weak spot thicker, round its corners, use a stronger material, or let it carry less.")


WARP_VERDICT = ((0.7, "fine"), (1.3, "might lift"))  # lift ratio below these, like the warping check's length ratio
_warp_reference: dict = {}


def warping(up, profile: dict | None = None, detailed: bool = False) -> dict:
    """Warping simulation (warp.py): how hard the base wants to curl off the bed, as a ratio to the
    longest bar that stays flat (1 = like that bar), where it lifts first, and the curled shape
    (exaggerated) for the FEA view: ``field_triangles``, ``vertex_disp`` (part frame), ``vertex_value`` (mm);
    the stress left inside after it cools: ``stress_value`` (MPa per vertex), ``stress_max`` (MPa, 99th percentile
    of the elements: single voxel corners spike) and ``stress_share`` (of what the layers hold).
    ``detailed``: with the loaded G-code's real layer times (layers that come quickly are still warm
    when the next go on and shrink less); ``from_gcode`` says whether it was used."""
    from . import warp
    from .strength import refine, sample_nodes, surface_values

    s = _require()
    p = Profile.from_dict(profile)
    sh = _shells(p)
    warm = _warm_layers(up, p) if detailed else None
    r = warp.simulate(s.mesh, up, p, sh, _grid("warp_voxels"), warm=warm)
    model, turn = r["model"], r["rotation"]  # model in the print frame; turn: part -> print
    s.warp = {"up": np.asarray(up, float) / np.linalg.norm(up), "model": model, "u": r["u"] @ turn, "turn": turn}  # set_fit / set_gap use it
    key = (p.material_id, json.dumps(p.material, sort_keys=True), p.enclosure, round(model.grid.h, 3), sh)
    if key not in _warp_reference:
        _warp_reference[key] = warp.reference(p, model.grid.h, sh)
    ref = _warp_reference[key]
    curl = warp.curl(r)
    ratio = float(np.sqrt(curl / ref)) if ref else None
    verdict = next((v for limit, v in WARP_VERDICT if ratio is not None and ratio < limit), "likely to lift")
    # Shape for the view: the part's own movement without the rigid shift and tilt it gets off the bed.
    pos = (model.grid.origin + np.argwhere(model.node_index >= 0) * model.grid.h) @ turn  # part frame
    u = _without_rigid_motion(pos, r["u"] @ turn)
    fine = refine(s.mesh.triangles, model.grid.h)
    disp = sample_nodes(model, u, fine.reshape(-1, 3) @ turn.T)
    size = np.linalg.norm(disp, axis=1)
    rot = print_rotation(s.mesh, up, p.printer["bed"], _moving(p))[0]
    pull = r["pull"]
    order = np.argsort(-pull)
    spots = []
    for i in order[: 40]:
        if pull[i] <= 0 or len(spots) == 3:
            break
        if all(np.linalg.norm(pos[i] - q) > 10 for q in spots):
            spots.append(pos[i])
    out = {"lift_ratio": ratio, "verdict": verdict, "curl_mm": round(curl, 3), "reference_mm": warp.warp_limit(p),
           "field_triangles": fine.astype(np.float32).tobytes(), "vertex_disp": disp.astype(np.float32).tobytes(),
           "vertex_value": size.astype(np.float32).tobytes(), "max_mm": float(size.max(initial=0)),
           "lift_points_print": [_to_print_frame(s.mesh, rot, q) for q in spots], "material": p.material["name"],
           "from_gcode": warm is not None}
    vm = warp.residual_stress(r)
    out["stress_value"] = surface_values(model, vm, fine @ turn.T).astype(np.float32).tobytes()
    out["stress_max"] = round(float(np.percentile(vm, 99)), 2) if len(vm) else 0.0
    out["stress_share"] = round(out["stress_max"] / r["material"].strength_z, 2)
    out["explanation"] = _warp_story(out, p)
    return out


def _warm_layers(up, p: Profile) -> tuple | None:
    """(heights, °C) from the layer heat model with the loaded G-code's layer times, if it is for ``up``."""
    from .checks import _layer_heat
    from .gcode import layer_times
    from .slicer import slice_mesh

    s = _require()
    if s.gcode is None or np.dot(s.gcode["up"], np.asarray(up, float) / np.linalg.norm(up)) < 0.999:
        return None
    rotation = print_rotation(s.mesh, up, p.printer["bed"], _moving(p))[0]
    layers = slice_mesh(orient(s.mesh, rotation), p.layer_height)
    return np.array([l.z for l in layers]), _layer_heat(layers, p, layer_times(s.gcode["toolpath"]))


def _without_rigid_motion(pos: np.ndarray, u: np.ndarray) -> np.ndarray:
    """``u`` minus the best-fitting shift and small rotation (u ≈ t + ω × (x − centre))."""
    x = pos - pos.mean(axis=0)
    n = len(x)
    a = np.zeros((3 * n, 6))
    for i in range(3):
        a[i::3, i] = 1.0
    # ω × x = [ω1 x3 - ω2 x2 ... ] written out per component
    a[0::3, 4], a[0::3, 5] = x[:, 2], -x[:, 1]
    a[1::3, 3], a[1::3, 5] = -x[:, 2], x[:, 0]
    a[2::3, 3], a[2::3, 4] = x[:, 1], -x[:, 0]
    coef = np.linalg.lstsq(a, u.ravel(), rcond=None)[0]
    return u - (a @ coef).reshape(-1, 3)


def _warp_story(r: dict, p: Profile) -> str:
    ratio, ref, name = r["lift_ratio"], r["reference_mm"], p.material["name"]
    where = f"{p.printer['name']}" + {"none": " (open)", "passive": " (enclosed)", "heated": " (heated chamber)"}[p.enclosure]
    if ratio is None:
        return "No warping reference for this material and printer."
    head = {
        "fine": f"It should stay flat: the base pulls on the bed about like a {ratio * ref:.0f} mm bar, and in {name} on the {where} bars up to about {ref:.0f} mm stay down.",
        "might lift": f"The corners might lift: the base pulls on the bed like a {ratio * ref:.0f} mm bar, and in {name} on the {where} bars longer than about {ref:.0f} mm tend to lift.",
        "likely to lift": f"The corners will likely lift: the base pulls on the bed like a {ratio * ref:.0f} mm bar, past the {ref:.0f} mm that stays down in {name} on the {where}.",
    }[r["verdict"]]
    fix = "" if r["verdict"] == "fine" else (" A brim or mouse ears at the marked spots, a warmer bed or an enclosure, glue stick,"
                                              " or rounding the corners of the base help. Upper layers shrinking onto the warm bottom cause it.")
    return head + fix + " The shape shown is exaggerated." + _stress_story(r)


def _stress_story(r: dict) -> str:
    """One sentence on the stress the shrinking leaves inside the part."""
    share = r.get("stress_share")
    if share is None:
        return ""
    if share < 0.25:
        return f" Little stress stays inside once it cools (up to about {r['stress_max']:.0f} MPa)."
    tail = ("it can crack along a layer later, or bend when it gets warm. Rounded inside corners, an enclosure or annealing lower it."
            if share >= 0.5 else "fine for most parts, but heat (a hot car) can let it bend a little.")
    return (f" Shrinking leaves up to about {r['stress_max']:.0f} MPa of stress inside, {share:.0%} of what the layers hold "
            f"(rough, real plastic relaxes some of it): {tail}")


def regions() -> dict:
    """Clickable faces: region id per triangle (uint32 bytes) and region summaries."""
    from .regions import find_regions, region_info

    s = _require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    info = region_info(s.mesh, s.regions)
    # Name round holes, so the UI can say "held by a screw here" when one is picked.
    from .features import accepted, detect_features

    for f in accepted(detect_features(s.mesh, s.regions, s.tags)):
        if f["type"] == "hole":
            info[f["region"]]["hole_diameter"] = f["diameter"]
    return {"face_regions": s.regions.astype("<u4").tobytes(), "regions": info}


def _load_case(loads: list[dict], voxels: int | None = None):
    """Prepared load cases (list, see strength.prepare_cases), cached per load set and grid size."""
    from .regions import find_regions
    from .strength import prepare_cases

    s = _require()
    voxels = voxels or _grid("strength_voxels")
    key = json.dumps([loads, voxels], sort_keys=True)
    if s.load_case is None or s.load_case[0] != key:
        if s.regions is None:
            s.regions = find_regions(s.mesh)
        s.load_case = (key, prepare_cases(s.mesh, s.regions, loads, voxels))
    return s.load_case[1]


TOPOLOGY_VOXELS = {False: 3_000, True: 12_000}  # Quick (~1 min in the browser) / Fine (~3 min)


def topology(up, loads: list[dict], profile: dict | None = None, keep: float = 0.4, fine: bool = False) -> dict:
    """A suggested lighter shape (topopt.py): where the plastic carries ``loads`` when only ``keep`` (0.15–0.9)
    of the part's volume may stay. Only removes plastic; cubes at loads and held faces stay. ``fine``: a
    finer grid (12 000 cubes instead of 3 000, about 4× slower). Returns the blocky shape as ``stl`` and
    ``triangles`` (float32, part coordinates), its plastic against the part's, and the safety factor of
    both printed solid on the same grid (a fair comparison, not the real print's)."""
    from . import topopt
    from .progress import stage

    s = _require()
    p = Profile.from_dict(profile)
    cases = _load_case(loads, TOPOLOGY_VOXELS[bool(fine)])
    model = cases[0].model
    with stage(0.0, 0.9):
        r = topopt.optimise(cases, up, p, float(keep))
    solid = topopt.keep_connected(model, r["density"] > topopt.SOLID, r["passive"])
    tris = topopt.voxel_surface(model.grid, model.elems[solid])
    with stage(0.9, 1.0):
        before = min(topopt.safety_solid(c, up, p, np.ones(len(solid), bool)) for c in cases)
        after = min(topopt.safety_solid(c, up, p, solid) for c in cases)
    grams = p.material["density_g_cm3"] / 1000
    part_g, shape_g = abs(s.mesh.volume()) * grams, abs(Mesh(tris).volume()) * grams
    need = required_safety(p)
    text = (f"A lighter shape that keeps the plastic where the load runs: about {shape_g:.0f} g instead of {part_g:.0f} g "
            f"({100 * shape_g / part_g:.0f} %). Printed solid it holds about ×{after:.1f} the load (the whole part solid: ×{before:.1f}). ")
    text += ("That's still enough. " if after >= need else
             f"That's below ×{need:g}: keep more plastic, or thicken the thinnest struts when you redesign it. ")
    text += "It's blocky and not a finished part: use it as a guide to redesign the part in CAD."
    return {"keep": float(keep), "rounds": r["rounds"], "cubes": int(len(solid)), "part_g": round(part_g, 1),
            "shape_g": round(shape_g, 1), "safety_solid_part": before, "safety_solid_shape": after, "enough": after >= need,
            "explanation": text, "stl": to_binary_stl(Mesh(tris)), "triangles": tris.astype(np.float32).tobytes()}


def _evaluate(cases, up, p: Profile, sh) -> dict:
    """``strength.evaluate_cases`` for the cached load cases, remembered once: "Will it hold?", the
    knock check, the stronger options and the material advisor solve the same thing one after another."""
    from .strength import evaluate_cases

    s = _require()
    ours = s.load_case is not None and s.load_case[1] is cases
    key = (s.load_case[0] if ours else None, tuple(np.round(np.asarray(up, float), 9)), p, sh)
    if ours and s.solved is not None and s.solved[0] == key:
        return dict(s.solved[1])
    r = evaluate_cases(cases, up, p, sh)
    if ours:
        s.solved = (key, r)
    return dict(r)


def _shells(p: Profile, walls: int | None = None, infill: float | None = None, top: int | None = None, bottom: int | None = None,
            zones: list[dict] | None = None):
    """How the part will be printed; anything not given comes from the goal's usual settings.
    ``zones``: stronger zones (lo, hi, walls, infill, as optimise_strength returns them) and the
    advanced design boxes (``kind``, see volume_at): hidden gaps count, supports don't change the part."""
    from .strength import Shells

    plan = goal_plan(p)
    return Shells(walls=int(plan["walls"] if walls is None else walls), infill=float(plan["infill"] / 100 if infill is None else infill),
                  top=int(plan["top"] if top is None else top), bottom=int(plan["bottom"] if bottom is None else bottom),
                  zones=tuple(_box_zone(z) for z in zones or () if z.get("kind", "stronger") in ("stronger", "void")))


def pause_gcode(data: bytes | str, pauses: list[float]) -> dict:
    """A sliced file with a pause before the first layer above each pocket top (``pauses``: mm,
    ``top_z`` of pause_to_insert), for printers whose slicer doesn't take the pause from our 3MF
    (Bambu Studio). Plain G-code or a ``.gcode.3mf`` (each G-code inside, and its .md5 that Bambu
    printers check). Returns ``data`` (same kind of file) and ``pauses`` (where each went)."""
    from .gcode import add_pauses

    if not pauses:
        raise ValueError("No pause to add: this part has no closed pocket")
    if isinstance(data, str):
        data = data.encode()
    if data[:4] == b"GCDE":
        raise ValueError("This is binary G-code (.bgcode). Turn off “binary G-code” in PrusaSlicer's printer settings and export again")
    if data[:2] != b"PK":
        text, where = add_pauses(data.decode("utf-8", "replace"), pauses)
        return {"data": text.encode(), "pauses": where}
    import hashlib
    import io
    import zipfile

    from .formats import read_zip

    src = zipfile.ZipFile(io.BytesIO(data))
    names = [n for n in src.namelist() if n.lower().endswith(".gcode")]
    if not names:
        raise ValueError("This 3MF has no G-code inside: slice it and use “Export plate sliced file”")
    files, where = {}, []
    for n in names:
        text, w = add_pauses(read_zip(src, n).decode("utf-8", "replace"), pauses)
        files[n] = text.encode()
        where += w
        if n + ".md5" in src.namelist():  # the printer refuses a file whose checksum doesn't match
            old = read_zip(src, n + ".md5").decode().strip()
            md5 = hashlib.md5(files[n]).hexdigest()
            files[n + ".md5"] = (md5.upper() if old.isupper() else md5).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            z.writestr(info, files.get(info.filename) or read_zip(src, info.filename), compress_type=info.compress_type)
    return {"data": out.getvalue(), "pauses": where}


def load_gcode(data: bytes | str, up, profile: dict | None = None) -> dict:
    """Read the G-code the part was sliced to (from our 3MF, in this orientation ``up``) for the
    detailed level: strength with the real walls, infill and line directions. Checks that it is this
    part (sizes, and most of the plastic inside the model; a quarter turn on the bed is found).
    Plain G-code, or a Bambu Studio / OrcaSlicer ``.gcode.3mf`` (the G-code is inside)."""
    from .gcode import KIND_NAMES, PART_KINDS, parse, placement
    from .voxel import voxelize

    s = _require()
    p = Profile.from_dict(profile)
    if isinstance(data, (bytes, bytearray)) and data[:2] == b"PK":
        import io
        import zipfile

        from .formats import read_zip

        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = sorted(n for n in z.namelist() if n.lower().endswith(".gcode"))
            if not names:
                raise ValueError("This 3MF has no G-code inside: slice it and use “Export plate sliced file”")
            data = read_zip(z, names[0])
    if isinstance(data, (bytes, bytearray)) and data[:4] == b"GCDE":
        raise ValueError("This is binary G-code (.bgcode). Turn off “binary G-code” in PrusaSlicer's printer settings and export again")
    tp = parse(data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else data)
    rot = print_rotation(s.mesh, up, p.printer["bed"], _moving(p))[0]
    turned = s.mesh.triangles.reshape(-1, 3) @ rot.T
    lo, hi = turned.min(axis=0), turned.max(axis=0)
    base = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])  # orient() moves the part by -base
    grid = voxelize(orient(s.mesh, rot), 30_000)

    def inside(pts):
        idx = grid.index_of(pts)
        ok = np.all((idx >= 0) & (idx < np.array(grid.solid.shape)), axis=1)
        out = np.zeros(len(pts), dtype=bool)
        out[ok] = grid.solid[tuple(idx[ok].T)]
        return out

    q, shift = placement(tp, lo - base, hi - base, inside)
    s.gcode = {"up": np.asarray(up, float) / np.linalg.norm(up), "toolpath": tp, "q": q, "shift": shift + base, "rot": rot, "printed": {}}
    vol = {KIND_NAMES[k]: round(float(tp.volume[tp.kind == k].sum()) / 1000, 2) for k in range(len(KIND_NAMES))}
    return {"slicer": tp.slicer, "layers": int(len(tp.layer_z)), "lines": int(tp.part().sum()),
            "print_time_min": round(tp.total_s / 60, 1), "plastic_cm3": vol,
            "part_grams": round(sum(vol[KIND_NAMES[k]] for k in PART_KINDS) * p.material["density_g_cm3"], 1),
            "turned_deg": int(round(np.degrees(np.arctan2(q[1, 0], q[0, 0])))) % 360}


def _printed(model):
    """What the loaded G-code puts in each cube of ``model`` (cached per analysis grid)."""
    from .gcode import deposit
    from .strength import printed_from
    from .voxel import inside_fraction

    s = _require()
    g = s.gcode
    if id(model) not in g["printed"]:
        q, shift, rot = g["q"], g["shift"], g["rot"]

        def to_part(x, is_direction):  # printer -> part: part = rotᵀ (q x + shift)
            return (x @ q.T) @ rot if is_direction else (x @ q.T + shift) @ rot

        dep = deposit(g["toolpath"], model.grid, model.elems, to_part)
        pr = printed_from(dep, model, inside_fraction(s.mesh, model.grid))
        g["printed"] = {id(model): (pr, dep["missed"])}  # one grid at a time: old ones are not used again
    return g["printed"][id(model)]


def strength(up, loads: list[dict], profile: dict | None = None, walls: int | None = None, infill: float | None = None,
             top: int | None = None, bottom: int | None = None, fine: bool = False, detailed: bool = False,
             zones: list[dict] | None = None) -> dict:
    """Will it hold? Safety factor, failure mode and a per-face heat map (uint8, 255 = breaks).
    ``walls`` (lines), ``infill`` (0–1), ``top``/``bottom`` (solid layers) say how it will be printed
    (default: the goal's usual settings). With several load cases (``case`` on forces) the weakest
    decides; ``cases`` lists them all. ``fine`` uses a finer grid (slower): if the answer barely
    changes, the coarse one can be trusted (the usual mesh-convergence check in FEA). ``detailed``
    uses the G-code from ``load_gcode`` instead of walls/infill/top/bottom: the plastic really in
    each spot and which way its lines run (lines pulled apart sideways become a third way to break).
    ``zones``: stronger zones from optimise_strength (slicer modifier volumes)."""
    from .strength import evaluate_cases, face_values, refine, sample_nodes, surface_values

    s = _require()
    p = Profile.from_dict(profile)
    cases = _load_case(loads, _grid("fine_voxels") if fine else None)
    sh = _shells(p, walls, infill, top, bottom, zones)
    model = cases[0].model
    printed = None
    if detailed:
        if s.gcode is None:
            raise ValueError("Load the sliced G-code first (detailed check)")
        if np.dot(s.gcode["up"], np.asarray(up, float) / np.linalg.norm(up)) < 0.999:
            raise ValueError("The G-code was sliced for another orientation: slice this one, or switch back")
        printed, missed = _printed(model)
    r = evaluate_cases(cases, up, p, sh, printed) if printed is not None else _evaluate(cases, up, p, sh)
    r["fine"] = bool(fine)
    if printed is not None:
        r["detailed"] = {"missed_pct": round(missed * 100, 1), "fill_pct": round(float(printed.fill.mean()) * 100, 1)}
    fi, vm, u = r.pop("fi"), r.pop("vm"), r.pop("u")
    heat = face_values(s.mesh, model.grid, model, fi)
    r["face_heat"] = (np.clip(heat / 1.25, 0, 1) * 255).astype(np.uint8).tobytes()
    # FEA view: a finer copy of the surface with smooth fields at every corner — % of strength used,
    # von Mises stress, displacement.
    fine = refine(s.mesh.triangles, model.grid.h)
    corners = fine.reshape(-1, 3)
    r["field_triangles"] = fine.astype(np.float32).tobytes()
    r["vertex_fi"] = (surface_values(model, fi, fine) * 100).astype(np.float32).tobytes()
    r["vertex_vm"] = surface_values(model, vm, fine).astype(np.float32).tobytes()
    r["vertex_disp"] = sample_nodes(model, u, corners).astype(np.float32).tobytes()
    # The analysis cubes themselves, for the section view (peaks are often inside the part).
    r["elements"] = {"centers": model.grid.centers(model.elems).astype(np.float32).tobytes(), "size_mm": float(model.grid.h),
                     "fi": (fi * 100).astype(np.float32).tobytes(), "vm": vm.astype(np.float32).tobytes()}
    worst = cases[[c.number for c in cases].index(r["case"])]
    ok = ~worst.ignore  # stresses right at the supports and loads are not meaningful (they grow as the grid gets finer)
    r["fi_max_pct"] = float(fi[ok].max(initial=0) * 100)
    r["vm_max_mpa"] = float(vm[ok].max(initial=0))
    r["mesh"] = {"voxels": int(len(model.elems)), "voxel_mm": float(model.grid.h), "nodes": int(model.n_nodes),
                 "load_n": float(np.linalg.norm(worst.forces.sum(axis=0)))}
    rot = print_rotation(s.mesh, up, p.printer["bed"], _moving(p))[0]
    r["critical_point_print"] = _to_print_frame(s.mesh, rot, r["critical_point"])
    # Where to point the "weakest spot" arrow: the surface nearest to it (the spot itself is often inside).
    # Not a face on the bed (an arrow from below the bed can't be seen): the nearest face you can see.
    normals = np.cross(fine[:, 1] - fine[:, 0], fine[:, 2] - fine[:, 0])
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    dist = np.linalg.norm(fine.mean(axis=1) - r["critical_point"], axis=1)
    dist[normals @ rot[2] < -0.7] += 1e6
    tri = fine[int(np.argmin(dist))]
    n = np.cross(tri[1] - tri[0], tri[2] - tri[0])
    r["weak_marker"] = {"point": _to_print_frame(s.mesh, rot, tri.mean(axis=0)), "normal": (rot @ (n / max(np.linalg.norm(n), 1e-12))).tolist()}
    r.update(walls=sh.walls, infill=sh.infill, top=sh.top, bottom=sh.bottom, material=p.material["name"])
    r["stress_zones"] = _stress_zones(model, fi, worst.ignore, up, sh, s.mesh, p)
    s.weak_spot = (r["critical_point"], float(model.grid.h))  # design() suggests a chamfer there
    s.hot_spots = _hot_spots(model, fi, ok)  # ... and at the other inside corners that are nearly as weak
    r["explanation"] = _strength_story(s.mesh, up, r, p)
    return r


def strength_variant(up, loads: list[dict], data: bytes, profile: dict | None = None, walls: int | None = None,
                     infill: float | None = None, top: int | None = None, bottom: int | None = None,
                     zones: list[dict] | None = None) -> dict:
    """The strength of a changed copy of the loaded part (a design variant, as STL / 3MF ``data``) without
    replacing the part: for trying design changes in the background and showing only the one the user
    picks. Held faces and loads move to the variant's matching face (facing the same way, middle nearest
    the old one's, as the web app does for a new version; spots keep their point). Returns the safety
    factor, its verdict and how much plastic the change adds (``added_g``, solid)."""
    global _session
    from .regions import find_regions, region_info

    s = _require()
    p = Profile.from_dict(profile)
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    old = region_info(s.mesh, s.regions)
    mesh = read_bodies(bytes(data))[0][1]
    variant = Session(mesh, f"{s.name} (variant)", find_regions(mesh))
    new = region_info(mesh, variant.regions)
    moved = []
    for load in loads:
        if load.get("point") is not None or "region" not in load:
            moved.append(load)
            continue
        was = old[load["region"]]
        same = [r for r in new if np.dot(r["normal"], was["normal"]) > 0.9]
        if not same:
            raise ValueError("A held or loaded face is gone in this design change")
        near = min(same, key=lambda r: np.linalg.norm(np.subtract(r["centroid"], was["centroid"])))
        moved.append({**load, "region": near["id"]})
    _session = variant
    try:
        r = strength(up, moved, profile, walls, infill, top, bottom, zones=zones)
    finally:
        _session = s
    added = (abs(mesh.volume()) - abs(s.mesh.volume())) / 1000 * p.material["density_g_cm3"]
    return {"safety_factor": r["safety_factor"], "verdict": r["verdict"], "added_g": float(added)}

def _stress_zones(model, fi: np.ndarray, ignore: np.ndarray, up, sh, mesh: Mesh, p: Profile) -> list[dict]:
    """"Stronger in one area" zones where this result's stress is high, instead of the user's guess:
    the optimiser's boxes round the most stressed cubes, printed like a picked zone (two more walls,
    at least 40 % infill) and never weaker than the rest of the part."""
    from .strength import find_zones

    boxes = find_zones(model, fi, ignore, up, sh.walls + 2, max(0.4, sh.infill))
    return [{**_zone_info(z, mesh, up, p.printer["bed"], _moving(p)), "share_pct": round(share * 100)}
            for z, share in zip(boxes, _zone_shares(model, fi, ignore, up, boxes))]


def _zone_shares(model, fi: np.ndarray, ignore: np.ndarray, up, boxes: list) -> list[float]:
    """How hard the plastic in each zone works at its peak, as a share of the worst spot: tells the
    user which zone matters most."""
    worst = fi[~ignore].max(initial=0)
    if worst <= 0:
        return [0.0] * len(boxes)
    c = model.grid.centers(model.elems)
    return [float(fi[(z.gap(c @ rotation_to_z(up).T) <= 1e-9) & ~ignore].max(initial=0) / worst) for z in boxes]


def _hot_spots(model, fi: np.ndarray, ok: np.ndarray, most: int = 1500) -> tuple | None:
    """Centres of the analysis cubes with at least features.HOT_SHARE of the highest stress (not right at
    a support or load) and their share of it, at most ``most`` of the highest."""
    from .features import HOT_SHARE

    top = fi[ok].max(initial=0)
    if top <= 0:
        return None
    idx = np.flatnonzero(ok & (fi >= HOT_SHARE * top))
    idx = idx[np.argsort(-fi[idx])[:most]]
    return model.grid.centers(model.elems[idx]), fi[idx] / top


def dynamics(up, loads: list[dict], profile: dict | None = None, drop_mm: float = 0.0, mounted_kg: float = 0.0, shake: bool = True,
             walls: int | None = None, infill: float | None = None, top: int | None = None, bottom: int | None = None,
             zones: list[dict] | None = None) -> dict:
    """Simplified dynamics: the lowest natural frequency as held (shakes along at that rate: a motor,
    fan or printer running near it makes it resonate) and its swing shape for the FEA view (``shake``);
    and the load falling ``drop_mm`` onto the part (or the part being knocked): the impact factor and
    the safety factor that leaves. ``mounted_kg``: something fixed on the loaded face that swings with
    it. Printed like the strength check (``walls`` … ``zones``), whose static solve it reuses."""
    from .strength import impact_factor, refine, sample_nodes, vibration

    s = _require()
    p = Profile.from_dict(profile)
    cases = _load_case(loads)
    sh = _shells(p, walls, infill, top, bottom, zones)
    model = cases[0].model
    out = {"drop_mm": drop_mm, "frequency_hz": None, "sag_mm": None, "impact_factor": None, "static_safety": None, "impact_safety": None}
    if shake:
        hz, shape = vibration(cases[0], up, p, sh, mounted_kg)
        fine = refine(s.mesh.triangles, model.grid.h)
        disp = sample_nodes(model, shape, fine.reshape(-1, 3))
        out.update(frequency_hz=round(hz, 1), field_triangles=fine.astype(np.float32).tobytes(), vertex_disp=disp.astype(np.float32).tobytes(),
                   vertex_value=np.linalg.norm(disp, axis=1).astype(np.float32).tobytes())
    if drop_mm > 0:
        r = _evaluate(cases, up, p, sh)
        worst = cases[[c.number for c in cases].index(r["case"])]
        loaded = np.linalg.norm(worst.forces, axis=1) > 0
        sag = float(np.abs(np.einsum("ij,ij->i", r["u"][loaded], worst.forces[loaded] / np.linalg.norm(worst.forces[loaded], axis=1, keepdims=True))).mean())
        factor = impact_factor(drop_mm, sag)
        out.update(sag_mm=round(sag, 3), impact_factor=round(factor, 2), static_safety=r["safety_factor"],
                   impact_safety=r["safety_factor"] / factor if np.isfinite(r["safety_factor"]) else None)
    out["explanation"] = _dynamics_story(out)
    return out


def _dynamics_story(r: dict) -> str:
    hz, out = r["frequency_hz"], ""
    if hz is not None:
        where = ("very low: it sways visibly and anything shaking nearby can rock it" if hz < 20 else
                 "in the range of motors, fans and printers (20–200 Hz): if something near it runs at that rate it can resonate and rattle loose"
                 if hz < 200 else "high: everyday shaking won't make it resonate")
        out = f"It swings fastest-free at about {hz:.0f} Hz — {where}. Shown: how it swings (exaggerated)."
    if r["drop_mm"] > 0 and r["impact_safety"] is not None:
        isf = r["impact_safety"]
        out += (f"{' ' if out else ''}If the load drops {r['drop_mm']:.0f} mm onto it, it hits about {r['impact_factor']:.1f}× as hard as when held still"
                f" (the part gives only {r['sag_mm']:.2f} mm), so the safety drops from ×{r['static_safety']:.1f} to ×{isf:.1f}"
                + (": still fine." if isf >= 2 else ": it may break when that happens." if isf >= 1 else ": it will likely break."))
    return out


def strength_options(up, loads: list[dict], profile: dict | None = None, walls: int | None = None, infill: float | None = None,
                     top: int | None = None, bottom: int | None = None) -> list[dict]:
    """Every way to make it stronger, each with its safety factor, what it costs and why it helps:
    slicer settings (more walls, infill, top/bottom layers), the strongest orientation, a design change
    at the weak spot (from the last strength check), other materials and a lighter load. Sorted strongest
    first, a lighter load always last. ``base_safety`` on each: today's safety with the same shell model,
    so the +/- % compares like with like (the G-code strength check gives its own, different number)."""
    from dataclasses import replace

    from .strength import cases_safety, material_volume, verdict
    from .strength import evaluate_cases as evaluate_strength

    s = _require()
    p = Profile.from_dict(profile)
    case = _load_case(loads)
    sh = _shells(p, walls, infill, top, bottom)
    model = case[0].model
    to_grams = abs(s.mesh.volume()) / (len(model.elems) * model.grid.h**3) / 1000 * p.material["density_g_cm3"]
    base = _evaluate(case, up, p, sh)
    grams = material_volume(model, up, sh, p) * to_grams
    out = []

    def add(change, kind, sf, why, cost="", **extra):
        out.append({"change": change, "kind": kind, "safety_factor": None if sf is None else float(sf),
                    "verdict": None if sf is None else verdict(sf, required_safety(p)), "why": why, "cost": cost, **extra})

    def slicer(change, shells, why, settings):
        sf = evaluate_strength(case, up, p, shells)["safety_factor"]
        extra = material_volume(model, up, shells, p) * to_grams - grams
        add(change, "slicer", sf, why, f"+{extra:.0f} g plastic, a bit longer to print", settings=settings)

    slicer(f"{sh.walls + 2} walls instead of {sh.walls}", replace(sh, walls=sh.walls + 2),
           "Walls are the solid outside of the print and carry most of the load.", {"wall_loops": str(sh.walls + 2)})
    dense = max(0.4, sh.infill)
    if dense > sh.infill:
        slicer(f"{dense:.0%} infill instead of {sh.infill:.0%}", replace(sh, infill=dense),
               "Denser infill backs up the walls inside the part.", {"sparse_infill_density": f"{dense:.0%}"})
    more = replace(sh, top=sh.top + 3, bottom=sh.bottom + 3)
    slicer(f"{more.top} top and {more.bottom} bottom layers instead of {sh.top} and {sh.bottom}", more,
           "Solid top and bottom skins carry bending, like the flanges of a beam.",
           {"top_shell_layers": str(more.top), "bottom_shell_layers": str(more.bottom)})

    axes = np.vstack([np.eye(3), -np.eye(3)])
    sfs = cases_safety(case, axes, p, sh)
    best = int(np.argmax(sfs))
    if abs(float(np.dot(axes[best], np.asarray(up, float) / np.linalg.norm(up)))) < 0.99:
        add("Print in the strongest orientation", "orientation", sfs[best],
            "Turns the part so the load runs along the layers instead of pulling them apart.",
            "may need more support or take longer", up=axes[best].tolist())

    out += _beyond_settings(base, p, profile, loads)
    for o in out:
        o["base_safety"] = float(base["safety_factor"])
    return _strongest_first(out)


def _strongest_first(options: list[dict]) -> list[dict]:
    """Strongest first, but carrying less always last: it doesn't make the part any stronger, it's
    the way out when nothing else is enough — so it never leads, even with the best number."""
    return sorted(options, key=lambda o: (o["kind"] == "load", -(o["safety_factor"] or 0)))


def _beyond_settings(base: dict, p: Profile, profile: dict | None, loads: list[dict], need: float | None = None) -> list[dict]:
    """What helps besides slicer settings and orientation: a design change at the weak spot, another
    material, or a lighter load (that still reaches ``need``, default the usual safety). Also the way
    out when even the strongest settings aren't enough."""
    from .features import concave_edges
    from .strength import verdict

    s = _require()
    need = need or required_safety(p)
    out = []

    def add(change, kind, sf, why, cost="", **extra):
        out.append({"change": change, "kind": kind, "safety_factor": None if sf is None else float(sf),
                    "verdict": None if sf is None else verdict(sf, need), "why": why, "cost": cost, **extra})

    # Design: a chamfer at the inside corner next to the weak spot (see features._stress_chamfers).
    if s.weak_spot is not None:
        spot = np.asarray(s.weak_spot[0])
        edges = concave_edges(s.mesh)
        if edges and min(np.linalg.norm((e["a"] + e["b"]) / 2 - spot) for e in edges) <= 8 * s.weak_spot[1]:
            add("Round off the weak inside corners (design change)", "design", None,
                "The most stress sits at a sharp inside corner; a 2–4 mm round spreads it out (a 45° chamfer where a "
                "round would hang over in this orientation). Every inside corner that is nearly as weak gets one too, "
                "so the weak spot doesn't just move to the next corner. On a test part a 2 mm chamfer gave about +15 %, "
                "4 mm about +40 %. Preview it under Design changes, then check strength again.",
                "changes the shape a little", edit="stress-chamfer")
    if s.weak_spot is not None:
        add("Make the weak spot thicker (design change)", "design", None,
            ("The plastic itself is the limit there: more cross-section is the direct fix. " if base["mode"] == "material" else "")
            + "Adds 1.5 mm of plastic all round the weak spot, tapering out either side (size under Design changes). "
            "Test it to see how much it helps.", "changes the shape", edit="thicken-weak")

    # Other materials: stresses don't depend on stiffness here, only the strengths change. Fibre filled
    # ones (PLA-CF, PA-CF…) are never "stronger": the fibres make it stiffer, but more brittle and weaker
    # between the layers than the plain plastic (rules.yaml fibre_filled).
    from .profile import fibre

    for mid, m in presets()["materials"].items():
        if mid == p.material_id or mid.startswith("tpu") or m.get("based_on", "").startswith("tpu") or fibre(m):
            continue
        other = Profile.from_dict({**(profile or {}), "material": mid})
        sf = _scaled_safety(base, p, other)
        if sf <= base["safety_factor"] * 1.15:  # only clearly stronger ones
            continue
        needs = []
        if m["warp_risk"] == "high" and p.enclosure == "none":
            needs.append("an enclosed printer (warps on an open one)")
        if m["abrasive"] and not p.printer.get("hardened_nozzle"):
            needs.append("a hardened nozzle")
        if m["hygroscopic"] == "high":
            needs.append("dry filament")
        add(f"Print it in {m['name']}", "material", sf,
            f"{m['name']} is stronger{' and its layers bond better' if other.z_strength_factor > p.z_strength_factor else ''}.",
            "needs " + ", ".join(needs) if needs else "", material=mid)

    # Less load: stresses grow in step with the load, so the safety does the opposite.
    sf = base["safety_factor"]
    forces = [float(l.get("kg", 1)) for l in loads if l.get("kind") == "force"]
    if forces and np.isfinite(sf) and sf < need:
        share = sf / need
        kg = f"about {forces[0] * share:.1f} kg instead of {forces[0]:g} kg" if len(forces) == 1 else f"about {share:.0%} of every load you set"
        add(f"Carry less: {kg}", "load", need,
            "The part holds a smaller load with the same margin. Spread the weight over two parts, add a support, or hang less on it.",
            "it carries less", load_share=share)
    return out


def _scaled_safety(base: dict, p: Profile, other: Profile) -> float:
    """Safety factor of ``base`` (a strength result in ``p``'s material) printed in ``other``'s
    material instead: stresses barely depend on stiffness here, only the strengths change — at room
    temperature and at the temperature it's used at (service.heat_factor)."""
    from .service import heat_factor

    cur, m = p.material, other.material
    k = heat_factor(other) / max(heat_factor(p), 1e-9)
    worst = max(base["fi_layers_max"] * cur["tensile_xy_mpa"] * p.z_strength_factor / (m["tensile_xy_mpa"] * other.z_strength_factor),
                base["fi_material_max"] * cur["tensile_xy_mpa"] / m["tensile_xy_mpa"]) / k
    return 1 / worst if worst > 0 else float("inf")


UV_WORDS = {"good": "made for sunlight", "fair": "fades a little in sunlight over the years", "poor": "turns brittle and yellow in sunlight"}


def material_advice(profile: dict | None = None, up=None, loads: list[dict] | None = None, outdoors: bool = False,
                    flexible: bool = False) -> list[dict]:
    """Which material to print it in: every material with "good" / "with care" / "not suitable" and
    the reasons — heat where it's used (profile ``use_temp_c``), sunlight (``outdoors``), a flexible
    part, what this printer can print (enclosure, hardened nozzle), and with ``up`` and ``loads``
    the safety factor for the load. Best first."""
    from .profile import fibre
    from .service import heat_factor, heat_limit, required_safety
    from .strength import verdict

    p = Profile.from_dict(profile)
    base = None
    if up is not None and loads:
        from .strength import evaluate_cases

        base = _evaluate(_load_case(loads), up, p, _shells(p))
    need = required_safety(p)
    out = []
    for mid, m in presets()["materials"].items():
        other = Profile.from_dict({**(profile or {}), "material": mid})
        bad, care, good = [], [], []
        is_flexible = mid.startswith("tpu") or m.get("based_on", "").startswith("tpu")
        if flexible != is_flexible:
            bad.append("flexible (rubbery)" if is_flexible else "stiff, not flexible")
        elif flexible:
            good.append("flexible")
        t = p.use_temp_c
        if t is not None and t > 23:
            limit = heat_limit(other)
            if t >= limit:
                bad.append(f"goes soft around {limit:.0f} °C (you need {t:.0f} °C)")
            elif t >= limit - 15:
                care.append(f"close to where it goes soft (~{limit:.0f} °C)")
            else:
                good.append(f"takes the heat (soft only from ~{limit:.0f} °C)")
        if outdoors:
            uv = m.get("uv_resistance", "fair")
            (bad if uv == "poor" else care if uv == "fair" else good).append(UV_WORDS[uv])
        if m["abrasive"] and not p.printer.get("hardened_nozzle"):
            bad.append(f"needs a hardened nozzle (the {p.printer['name']} doesn't have one)")
        if m["warp_risk"] == "high" and p.enclosure == "none":
            care.append("warps on an open printer: needs an enclosure")
        elif m["warp_risk"] != "low" and p.enclosure == "none":
            care.append("may warp on an open printer")
        if m["hygroscopic"] == "high":
            care.append("keep the filament dry")
        if fibre(m):
            care.append(f"{fibre(m)} fibre: stiffer, not stronger: more brittle and weaker between the layers")
        sf = None
        if base is not None:
            sf = float(_scaled_safety(base, p, other))
            v = verdict(sf, need)
            (good if v == "fine" else care if v == "borderline" else bad).append(f"holds your load ×{sf:.1f}")
        rating = "not suitable" if bad else "with care" if care else "good"
        fuss = {"low": 0, "medium": 1, "high": 2}[m["warp_risk"]] + (m["hygroscopic"] == "high") + bool(m["abrasive"])
        out.append({"material": mid, "name": m["name"], "rating": rating, "safety_factor": sf,
                    "reasons": bad + care + good, "current": mid == p.material_id, "_fuss": fuss})
    order = {"good": 0, "with care": 1, "not suitable": 2}
    out.sort(key=lambda r: (order[r["rating"]], r["_fuss"], -(r["safety_factor"] or 0)))  # easiest of the good ones first
    for r in out:
        del r["_fuss"]
    return out


# Heat inside a closed box: everything the electronics use ends up as heat that has to leave through
# the walls. Overall heat transfer of an enclosure wall to still room air, both sides: about 4.6 W/m²K
# (Elliott Electric's sealed-enclosure chart: 7.1 W/ft² gives a 16.7 °C rise; Firgelli: 3–6 W/m²K in
# still air); with a fan blowing through, 20 W/m²K (Firgelli: 20–40 forced). Printed plastic conducts
# about 0.2 W/m·K (PLA 0.13 … PETG 0.2): a 2 mm wall adds only ~7 %.
BOX_HEAT_U = {"closed": 4.6, "fan": 20.0}
PLASTIC_K = 0.2
# Walls next to the electronics get warmer than the air: part of the heat reaches them straight away
# (radiation, the warm air rising off a chip). Rough: a third of it, spread like light from a point
# (cos / 4πd²), the wall passing it on to the air on both sides; the plastic spreads a hot spot over
# about WALL_SPREAD_MM. Good enough to see which wall gets warmest, not for exact numbers.
RADIANT_SHARE = 0.3
WALL_SPREAD_MM = 15.0
SOURCE_LIFT_MM = 10.0  # a board sits on standoffs: its heat starts this far off the face it's placed on


def _outer_area_mm2(mesh: Mesh) -> float:
    """Outside surface of the part as a prism around its outline (hull × height + top and bottom),
    along whichever axis fits tightest: exact for boxes and cylinders, and it ignores the inside walls."""
    import shapely

    pts = mesh.triangles.reshape(-1, 3)
    best = np.inf
    for axis in range(3):
        a, b = [i for i in range(3) if i != axis]
        hull = shapely.MultiPoint(pts[:, [a, b]]).convex_hull
        height = float(np.ptp(pts[:, axis]))
        best = min(best, hull.length * height + 2 * hull.area)
    return float(best)


COLD_C = 0.0  # below this, printed plastic gets brittle: knocks break it more easily


def heat_curve(profile: dict | None = None, use_temp_c: float | None = None) -> dict:
    """How much of its strength the material keeps from a winter's day to past its heat limit
    (service.heat_factor), for the "strength vs. temperature" chart; ``use_temp_c`` (default the
    profile's) is marked on it. No part needed: it depends only on the material."""
    from dataclasses import replace

    from .service import ROOM_C, heat_factor, heat_limit

    p = Profile.from_dict(profile)
    t = p.use_temp_c if use_temp_c is None else float(use_temp_c)
    limit = heat_limit(p)
    top = max(limit + 20.0, 100.0)
    temps = np.unique(np.r_[np.arange(-20.0, top + 0.1, 5.0), ROOM_C, limit, p.material["softening_c"]])
    shares = [heat_factor(replace(p, use_temp_c=float(c))) for c in temps]
    at = t if t is not None else ROOM_C
    k = heat_factor(replace(p, use_temp_c=at))
    name = p.material["name"]
    if at < COLD_C:
        story = (f"At {at:.0f} °C {name} is about as strong as at room temperature, but more brittle: a knock or a drop breaks it more easily.")
    elif k > 0.999:
        story = f"At {at:.0f} °C {name} keeps its full strength."
    elif at >= limit:
        story = f"At {at:.0f} °C {name} is past the point where it goes soft (about {limit:.0f} °C): only about {k:.0%} of its strength is left."
    else:
        story = f"At {at:.0f} °C {name} keeps about {k:.0%} of its strength. It goes soft at about {limit:.0f} °C."
    return {"material": name, "temps_c": temps.round(1).tolist(), "share_pct": [round(x * 100, 1) for x in shares],
            "room_c": ROOM_C, "softening_c": float(p.material["softening_c"]), "soft_c": float(limit), "cold_c": COLD_C,
            "use_temp_c": at, "share_at_pct": round(k * 100, 1), "explanation": story}


def _heat_source(mesh: Mesh, point=None, face: int | None = None) -> np.ndarray:
    """Where the electronics sit (part coordinates): ``SOURCE_LIFT_MM`` off the clicked ``point`` on
    ``face`` (into the box: a face of the inside wall points into the room inside), or the middle of the part."""
    tri = mesh.triangles
    if point is None:
        return (tri.reshape(-1, 3).min(axis=0) + tri.reshape(-1, 3).max(axis=0)) / 2
    p = np.asarray(point, float)
    if face is None:
        return p
    n = np.cross(tri[face, 1] - tri[face, 0], tri[face, 2] - tri[face, 0])
    return p + SOURCE_LIFT_MM * n / max(np.linalg.norm(n), 1e-12)


def _wall_temps(tris: np.ndarray, source: np.ndarray, power_w: float, u: float, air_c: float, corners: bool = False) -> np.ndarray:
    """Temperature (°C) of every triangle (or of its three ``corners``): the air inside, plus what
    reaches that wall straight from the source (RADIANT_SHARE). The wall is thin, so its outside face
    gets as warm as its inside one."""
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    c = tris.reshape(-1, 3) if corners else tris.mean(axis=1)
    if corners:
        n = np.repeat(n, 3, axis=0)
    d = source - c
    dist = np.linalg.norm(d, axis=1)
    cos = np.abs(np.einsum("ij,ij->i", n, d)) / np.maximum(dist, 1e-9)
    r2 = (dist**2 + WALL_SPREAD_MM**2) * 1e-6
    q = RADIANT_SHARE * float(power_w) * cos / (4 * np.pi * r2)  # W/m² reaching the wall
    return air_c + q / (2 * u)  # it leaves to the air on both sides


def box_heat(power_w: float, ambient_c: float = 25.0, cooling: str = "closed", profile: dict | None = None,
             point=None, face: int | None = None) -> dict:
    """Air temperature inside the part used as a box for electronics that use ``power_w`` watts, with
    the room at ``ambient_c``: the heat leaves through the outside surface (``BOX_HEAT_U`` for a
    closed box or one with a fan) and the printed wall. Compared with where the plastic goes soft.
    Vents help too, but how much depends on their size: between the two.
    ``point`` / ``face``: where the electronics sit (a click inside the box; default the middle).
    ``face_temp_c`` (float32 per face) shows which walls get warmest (``_wall_temps``); for the 3D view
    also on a finer copy of the surface (``field_triangles``, ``vertex_temp_c`` at every corner)."""
    from .service import heat_limit
    from .strength import refine

    if cooling not in BOX_HEAT_U:
        raise ValueError(f"cooling must be one of {', '.join(BOX_HEAT_U)}")
    s = _require()
    p = Profile.from_dict(profile)
    area = _outer_area_mm2(s.mesh)
    wall_m = goal_plan(p)["walls"] * p.line_width / 1000
    u = 1 / (1 / BOX_HEAT_U[cooling] + wall_m / PLASTIC_K)
    rise = float(power_w) / (u * area * 1e-6)
    inside = float(ambient_c) + rise
    # Same limits as "Where is it used?": soft past heat_limit, creeping from the softening point.
    limit = heat_limit(p)
    soft = min(float(p.material["softening_c"]), limit - 10)
    verdict = "too hot" if inside >= limit else "warm" if inside >= soft else "fine"
    name = p.material["name"]
    how = {"closed": "closed", "fan": "with a fan blowing through"}[cooling]
    text = (f"{power_w:g} W inside this box ({how}, {area / 100:.0f} cm² outside) warm the air inside about {rise:.0f} °C above the room: "
            f"about {inside:.0f} °C at {ambient_c:g} °C room. ")
    text += {"fine": f"{name} stays below where it starts to soften ({soft:.0f} °C).",
             "warm": f"{name} starts to soften from about {soft:.0f} °C: screw bosses and clips may slowly give way. Add vents or a fan, or print it in PETG or ASA.",
             "too hot": f"{name} goes soft at about {limit:.0f} °C: the box will sag and clips loosen. Add a fan or vents, or print it in ASA, ABS or PC."}[verdict]
    if inside >= 70:  # commercial-grade chips are rated 0–70 °C (industrial 85 °C), whatever the box is printed in
        text += " That's too hot for the electronics too: this needs a fan or a much bigger box."
    source = _heat_source(s.mesh, point, face)
    walls = _wall_temps(s.mesh.triangles, source, power_w, u, inside)
    fine = refine(s.mesh.triangles, max(float(np.ptp(s.mesh.triangles.reshape(-1, 3), axis=0).max()) / 40, 1.0))
    hot = int(np.argmax(walls))
    wall_c = float(walls[hot])
    if wall_c >= max(soft, inside + 5):
        text += (f" The wall closest to the electronics gets warmer still, about {wall_c:.0f} °C (red on the part)"
                 + (": it can sag there first. Move the electronics away from the wall or add a vent there." if wall_c >= limit
                    else ": it may slowly give way there first." if wall_c >= soft else "."))
    return {"power_w": float(power_w), "ambient_c": float(ambient_c), "cooling": cooling, "area_cm2": round(area / 100, 1),
            "rise_c": round(rise, 1), "inside_c": round(inside, 1), "softening_c": soft, "heat_limit_c": limit,
            "verdict": verdict, "explanation": text, "source": source.tolist(), "wall_max_c": round(wall_c, 1),
            "hottest_point": s.mesh.triangles[hot].mean(axis=0).tolist(), "face_temp_c": walls.astype(np.float32).tobytes(),
            "field_triangles": fine.astype(np.float32).tobytes(),
            "vertex_temp_c": _wall_temps(fine, source, power_w, u, inside, corners=True).astype(np.float32).tobytes()}


def design(up, profile: dict | None = None, params: dict | None = None, keep_clear: list[dict] | None = None) -> dict:
    """Detected features (holes, bosses) and suggested geometry edits for this orientation.
    Each edit has a ``mesh`` (float32 triangles, part coordinates) made of closed ``pieces`` (triangle
    counts) to add to the part (``op: "add"``) or cut away (``op: "cut"``).
    ``params`` adjusts edit sizes (see features.PARAMS); the values used come back in ``params``.
    ``keep_clear``: boxes (volume_at, kind "keep_clear") that must stay empty: edits adding plastic
    there come back with ``clash``."""
    from .features import PARAMS, design_edits, detect_features, edit_params
    from .regions import find_regions

    s = _require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    p = Profile.from_dict(profile)
    return {
        "features": detect_features(s.mesh, s.regions, s.tags),
        "edits": _keep_clear_clash(design_edits(s.mesh, s.regions, up, p, params, s.tags, *(s.weak_spot or (None, 1.0)), fits=s.fits,
                                                gaps=s.gaps, hot_spots=s.hot_spots, fasteners=s.fasteners), up, keep_clear),
        "params": edit_params(params),
        "param_ranges": {k: {"min": lo, "max": hi} for k, (_, lo, hi) in PARAMS.items()},
    }


def set_fit(region: int, up, partner_mm: float | None = None, fit: str = "sliding", profile: dict | None = None) -> dict:
    """Say what goes into a round hole (or what a round post goes into): the mating part's diameter
    and the fit ("press", "snug", "sliding", "loose"). Returns the diameter to model and why; design()
    then offers the resize as an edit. ``partner_mm`` None forgets it."""
    from .features import accepted, detect_features, fit_size
    from .regions import find_regions

    s = _require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    feats = {f["region"]: f for f in accepted(detect_features(s.mesh, s.regions, s.tags))}
    if region not in feats:
        raise ValueError(f"Face {region} is not a round hole or post")
    if partner_mm is None:
        s.fits.pop(region, None)
        return {"region": region}
    squeeze = _warp_squeeze_hole(feats[region], up)
    size, why = fit_size(feats[region], float(partner_mm), fit, up, Profile.from_dict(profile), squeeze)
    s.fits[region] = {"partner_mm": float(partner_mm), "fit": fit, "warp_mm": squeeze}
    s.fasteners.pop(region, None)  # one size per hole
    why += _coarse_note(region)
    return {"region": region, "suggested_mm": round(size, 2), "modelled_mm": round(feats[region]["diameter"], 2), "explanation": why,
            "warp_mm": round(squeeze, 3)}


def set_fastener(region: int, up, size: str | None = "M3", use: str = "screw", profile: dict | None = None) -> dict:
    """Say what fastens into a round hole: ``size`` ("M2"–"M6") and ``use`` ("screw" cut straight into
    the plastic, "insert" heat-set insert, "nut_top" bolt with a nut in a pocket at the upper end,
    "nut_side" nut pushed in from the side, "bolt" through). Returns the hole size, depth, wall and
    nut pocket to model, and the slicer fix; design() then offers the changes. ``size`` None forgets it."""
    from .features import accepted, detect_features, fastener_plan
    from .regions import find_regions

    s = _require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    feats = {f["region"]: f for f in accepted(detect_features(s.mesh, s.regions, s.tags))}
    if region not in feats or feats[region]["type"] != "hole":
        raise ValueError(f"Face {region} is not a round hole")
    if size is None:
        s.fasteners.pop(region, None)
        return {"region": region}
    plan = fastener_plan(s.mesh, feats[region], size, use, up, Profile.from_dict(profile))
    s.fasteners[region] = {"size": size, "use": use}
    s.fits.pop(region, None)  # one size per hole: the fastener's
    return {"region": region, **plan, "explanation": plan["explanation"] + _coarse_note(region)}


def _coarse_note(region: int) -> str:
    """A warning to add when the hole / post ``region`` was exported with too few facets (coarse_facets):
    its measured size, and so the advice, is off by about the facets' error."""
    from .checks import coarse_surfaces

    s = _require()
    if s.coarse is None:
        s.coarse = coarse_surfaces(s.mesh)
    pts = s.mesh.triangles[s.regions == region].reshape(-1, 3)
    for c in s.coarse:
        lo, hi = np.array(c["bbox"]) + [[-0.01], [0.01]]
        if len(pts) and np.all((pts >= lo) & (pts <= hi)):
            return (f" This curve was exported with few facets (up to {c['error_mm']:.2f} mm off round), so the size measured here "
                    "can be off by about that much: export a finer STL or use the STEP file.")
    return ""


def split(profile: dict | None = None, joint: str = "pins", purpose: str = "fit") -> dict:
    """A part too big for the printer, cut flat into pieces that fit (split.py), joined with alignment
    pins or (``joint`` "dovetail") a sliding dovetail: each piece's ``edits`` (cut the rest away, then
    the pin holes) to apply to the part and its print ``rotation``, the pin as triangles
    (``pin.stl_triangles``, float32) and how many, and a plain explanation. Keeps the cut away from the
    weak spot of the last strength check, and uses the snug gap from the fit test print. ``purpose``
    "supports": cut a part that fits so each piece, turned its own best way, needs much less support
    (split.support_cut; each piece's ``rotation`` is then that way); raises when no cut helps enough."""
    from .split import split_plan

    s = _require()
    p = Profile.from_dict(profile)
    gap = (p.material.get("fit_gap_mm") or {}).get("snug")
    if joint not in ("pins", "dovetail"):
        raise ValueError('joint is "pins" or "dovetail"')
    weak = s.weak_spot[0] if s.weak_spot else None
    if purpose == "fit":
        return split_plan(s.mesh, p.printer["bed"], weak, gap, joint)
    if purpose != "supports":
        raise ValueError('purpose is "fit" or "supports"')
    from .split import _half, support_cut

    cut = support_cut(s.mesh, orientation_settings(p), p)
    if cut is None:
        raise ValueError("No straight cut makes the pieces need much less support: print it in one piece")
    plan = split_plan(s.mesh, p.printer["bed"], weak, gap, joint, force=(cut["axis"], [cut["cut"]]))
    for i, (piece, up) in enumerate(zip(plan["pieces"], cut["ups"])):
        piece["rotation"] = print_rotation(_half(s.mesh, cut["axis"], cut["cut"], i == 0), up, p.printer["bed"], _moving(p))[0].tolist()
    note, rest = plan["explanation"].split("Cut ", 1)
    plan["explanation"] = (note + f"Cut in two so each piece prints its own best way with much less support: about "
                           f"{cut['pieces_mm3'] / 1000:.1f} cm³ of support instead of {cut['whole_mm3'] / 1000:.1f} cm³. "
                           + rest.split(" pieces that each fit the bed. ", 1)[-1])
    plan["support_mm3"] = {"whole": round(cut["whole_mm3"]), "pieces": round(cut["pieces_mm3"])}
    return plan


def set_gap(face_a: int, face_b: int, up, partner_mm: float | None = None, fit: str = "sliding", profile: dict | None = None) -> dict:
    """A slot or tab between two flat faces the user picked: their distance and which it is (slot:
    empty between them, tab: plastic between). With ``partner_mm`` (thickness of what goes in, or of
    the slot the tab goes into) and ``fit``: the size to model and why; design() then offers moving both
    faces as an edit. If a warping simulation ran for this orientation, how much the gap closes as the
    part curls is added. ``partner_mm`` None only measures (and forgets a size given before)."""
    from .features import flat_pair, gap_size
    from .regions import find_regions

    s = _require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    a, b = sorted((int(face_a), int(face_b)))
    pair = flat_pair(s.mesh, s.regions, a, b)
    out = {"faces": [a, b], "kind": pair["kind"], "distance_mm": round(pair["distance_mm"], 3)}
    if partner_mm is None:
        s.gaps.pop((a, b), None)
        return out
    closing = _warp_closing_pair(pair, a, b, up)
    size, why = gap_size(pair, float(partner_mm), fit, up, Profile.from_dict(profile), closing)
    s.gaps[(a, b)] = {"partner_mm": float(partner_mm), "fit": fit, "warp_mm": closing}
    return {**out, "suggested_mm": round(size, 2), "explanation": why, "warp_mm": round(closing, 3)}


def _warp_points(up):
    """(model, displacement at points) from the last warping run, if it was for ``up``."""
    s = _require()
    w = s.warp
    if w is None or np.dot(w["up"], np.asarray(up, float) / np.linalg.norm(up)) < 0.999:
        return None
    from .strength import sample_nodes

    return lambda pts: sample_nodes(w["model"], w["u"], np.asarray(pts, float) @ w["turn"].T)


def _warp_closing_pair(pair: dict, a: int, b: int, up) -> float:
    """How much a slot narrows (or a tab thickens) as the part curls, by the warping simulation (mm, ≥ 0)."""
    at = _warp_points(up)
    if at is None:
        return 0.0
    s = _require()
    ua = at(s.mesh.triangles[s.regions == a].mean(axis=1)).mean(axis=0)
    ub = at(s.mesh.triangles[s.regions == b].mean(axis=1)).mean(axis=0)
    e = pair["centre_b"] - pair["centre_a"]
    e = e / np.linalg.norm(e)
    grow = float((ub - ua) @ e)  # how much the faces move apart
    return max(-grow if pair["kind"] == "slot" else grow, 0.0)


def _warp_squeeze_hole(feature: dict, up) -> float:
    """How much a hole closes (or a post grows) across its narrowest way as the part curls (mm, ≥ 0):
    the wall's movement toward the axis on opposite sides."""
    at = _warp_points(up)
    if at is None:
        return 0.0
    s = _require()
    tris = s.mesh.triangles[s.regions == feature["region"]]
    pts = tris.mean(axis=1)
    axis = np.asarray(feature["axis"], float)
    radial = pts - np.asarray(feature["center"], float)
    radial -= np.outer(radial @ axis, axis)
    radial /= np.maximum(np.linalg.norm(radial, axis=1, keepdims=True), 1e-9)
    inward = -(at(pts) * radial).sum(axis=1)  # + = the wall moves toward the axis
    # Diameter change per direction: both sides of the hole along it.
    angle = np.arctan2(radial @ np.cross(axis, radial[0]), radial @ radial[0])
    bins = np.round(angle / (np.pi / 8)).astype(int) % 8  # opposite sides share a bin
    change = np.bincount(bins, inward, minlength=8) / np.maximum(np.bincount(bins, minlength=8), 1) * 2
    squeeze = float(change.max())
    return max(squeeze if feature["type"] == "hole" else -float(change.min()), 0.0)


def tag_feature(region: int, tag: str | None) -> dict:
    """Confirm or correct a detected feature: ``tag`` "hole", "boss" or "none" (not a feature);
    None forgets the answer. Rejected holes get no edits and no hole findings."""
    from .features import TAGS, detect_features
    from .regions import find_regions

    s = _require()
    if tag is not None and tag not in TAGS:
        raise ValueError(f"tag must be one of {TAGS}")
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    if tag is None:
        s.tags.pop(int(region), None)
    else:
        s.tags[int(region)] = tag
    return {"features": detect_features(s.mesh, s.regions, s.tags)}


def session_state() -> dict:
    """What the user told the engine about this part and what the last strength check found, as JSON:
    the app keeps it and gives it back with ``restore_state`` when the engine restarts (Cancel), so
    hole / post answers, fits, gaps and the weak spot don't silently vanish. The warping result is
    too big to keep: it is simply run again."""
    s = _require()
    return {
        "tags": {str(k): v for k, v in s.tags.items()},
        "fits": {str(k): v for k, v in s.fits.items()},
        "gaps": [[a, b, v] for (a, b), v in s.gaps.items()],
        "fasteners": {str(k): v for k, v in s.fasteners.items()},
        "weak_spot": None if s.weak_spot is None else [np.asarray(s.weak_spot[0], float).tolist(), s.weak_spot[1]],
        "hot_spots": None if s.hot_spots is None else [np.asarray(x, float).tolist() for x in s.hot_spots],
    }


def restore_state(state: dict) -> dict:
    """Put back what ``session_state`` gave (after the same part was loaded again)."""
    s = _require()
    s.tags = {int(k): v for k, v in state.get("tags", {}).items()}
    s.fits = {int(k): v for k, v in state.get("fits", {}).items()}
    s.gaps = {(int(a), int(b)): v for a, b, v in state.get("gaps", [])}
    s.fasteners = {int(k): v for k, v in state.get("fasteners", {}).items()}
    w = state.get("weak_spot")
    s.weak_spot = None if w is None else (np.asarray(w[0], float), float(w[1]))
    h = state.get("hot_spots")
    s.hot_spots = None if h is None else tuple(np.asarray(x, float) for x in h)
    return {"restored": True}


def holes_state() -> dict:
    """What the user sized, for a project file: fits (hole / post → what goes in, set_fit), screws,
    inserts and nuts (set_fastener) and slots (set_gap). restore_holes sizes them again."""
    s = _require()
    plain = lambda v: {"partner_mm": v["partner_mm"], "fit": v["fit"]}  # noqa: E731 - warp_mm is worked out again
    return {"fits": {str(k): plain(v) for k, v in s.fits.items()},
            "fasteners": {str(k): {"size": v["size"], "use": v["use"]} for k, v in s.fasteners.items()},
            "gaps": [[a, b, plain(v)] for (a, b), v in s.gaps.items()]}


def restore_holes(holes: dict, up, profile: dict | None = None) -> dict:
    """Size again what a project file's ``holes`` (holes_state) says, for build direction ``up``:
    the same results as set_fit / set_fastener / set_gap, by region (fits, fasteners) or in order (gaps).
    A hole that isn't found any more (the part changed) is left out, not an error."""
    out = {"fits": {}, "fasteners": {}, "gaps": [], "skipped": 0}

    def attempt(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except ValueError:
            out["skipped"] += 1
            return None

    for k, v in (holes.get("fits") or {}).items():
        if (r := attempt(set_fit, int(k), up, v["partner_mm"], v["fit"], profile)) is not None:
            out["fits"][k] = r
    for k, v in (holes.get("fasteners") or {}).items():
        if (r := attempt(set_fastener, int(k), up, v["size"], v["use"], profile)) is not None:
            out["fasteners"][k] = r
    for a, b, v in holes.get("gaps") or []:
        if (r := attempt(set_gap, int(a), int(b), up, v["partner_mm"], v["fit"], profile)) is not None:
            out["gaps"].append({**r, "partner_mm": v["partner_mm"], "fit": v["fit"]})
    return out


HOLE_RULES = {"hole_closes", "small_hole_undersized", "insert_wall_thin", "screw_wall_thin", "sideways_hole_oval", "bridge_sag", "hole_starts_midair"}


def _drop_rejected(findings: list[dict], mesh: Mesh, r: np.ndarray) -> list[dict]:
    """Remove hole findings at features the user said aren't holes (by distance to the feature's axis)."""
    s = _require()
    if not any(t == "none" for t in s.tags.values()) or s.regions is None:
        return findings
    from .features import detect_features

    rejected = []
    for f in detect_features(mesh, s.regions, s.tags):
        if f["tag"] == "none":
            rejected.append((np.asarray(_to_print_frame(mesh, r, f["center"])), r @ np.asarray(f["axis"]), f["radius"], f["length"]))

    def at_rejected(finding) -> bool:
        if finding["rule"] not in HOLE_RULES or not finding.get("bbox"):
            return False
        mid = (np.asarray(finding["bbox"][0]) + np.asarray(finding["bbox"][1])) / 2
        for c, axis, radius, length in rejected:
            along = (mid - c) @ axis
            if np.linalg.norm(mid - c - along * axis) <= radius + 1.0 and abs(along) <= length / 2 + radius:
                return True
        return False

    return [f for f in findings if not at_rejected(f)]


def _to_print_frame(mesh: Mesh, r: np.ndarray, point) -> list[float]:
    """Part-frame point -> coordinates of the oriented part shown on the bed (print rotation ``r``)."""
    pts = mesh.triangles.reshape(-1, 3) @ r.T
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return (r @ np.asarray(point) + shift).tolist()


# Support is printed sparse (~15 %) with denser interface layers under the part: about a quarter of
# the volume under the overhangs ends up as plastic (rough, like the support volume itself).
SUPPORT_FILL = 0.25
SPOOL_MARGIN = (1.1, 5.0)  # a print needs × this + g on the spool: estimate error, purge line and skirt


def _cost(oriented: Mesh, p: Profile, part_g: float) -> dict:
    """Support grams for this orientation and, with the user's price per kg / grams left on the spool
    (My materials), the cost and whether the spool will last."""
    from .refine import support_stats

    support_g = support_stats(oriented, p)["support_volume"] * SUPPORT_FILL / 1000 * p.material["density_g_cm3"]
    total = part_g + support_g
    out = {"support_g": round(support_g, 1), "total_g": round(total, 1)}
    if p.material.get("price_per_kg"):
        out["cost"] = round(total / 1000 * p.material["price_per_kg"], 2)
    left = p.material.get("spool_left_g")
    if left is not None:
        need = total * SPOOL_MARGIN[0] + SPOOL_MARGIN[1]
        out["spool_left_g"], out["spool_short"] = float(left), bool(need > left)
    return out


def check(up, profile: dict | None = None, mode: str = "print", faces: dict | None = None, detailed: bool = False) -> dict:
    """Run all printability checks for one orientation, explain them and recommend settings.
    ``faces`` as in ``orientations``: support on a face marked clean becomes a problem.
    ``detailed``: the heat check uses the real layer times of the loaded G-code (if it is for this orientation)."""
    s = _require()
    p = Profile.from_dict(profile)
    rotation, fit = print_rotation(s.mesh, up, p.printer["bed"], _moving(p))
    oriented = orient(s.mesh, rotation)
    sliced = None
    if detailed and s.gcode is not None and np.dot(s.gcode["up"], np.asarray(up, float) / np.linalg.norm(up)) >= 0.999:
        from .gcode import layer_times

        sliced = layer_times(s.gcode["toolpath"])
    result = run_checks(oriented, p, sliced=sliced)
    result["stats"]["heat_from_gcode"] = sliced is not None
    result["stats"].update(_cost(oriented, p, result["stats"]["filament_g"]))
    spots = _spot_heat(rotation, p) if sliced is not None else None
    if spots is not None:
        result["findings"] += _spot_findings(spots, p, result["findings"])
    result["findings"] = _drop_rejected(result["findings"], s.mesh, rotation)
    result["findings"] += _clean_face_findings(s.mesh, up, p, _marked_faces(faces), rotation)
    result["findings"] += _thread_findings(oriented, rotation, p)
    result["findings"] += _grip_findings(s.name)
    if not fit["fits"]:
        result["findings"].insert(0, {"rule": "exceeds_build_volume", "severity": "problem", "bbox": None, "metrics": fit,
                                      "detail": f"Doesn't fit the {p.printer['name']}: {fit['reason']}"})
    elif fit["turned_deg"]:
        result["findings"].append({"rule": "turned_to_fit", "severity": "info", "bbox": None, "metrics": fit,
                                   "detail": f"Turned {fit['turned_deg']:.0f}° on the bed so it fits the {p.printer['name']}"})
    for f in result["findings"]:  # findings built here, outside run_checks, need one too
        f.setdefault("impact", RULE_IMPACT.get(f["rule"], "info"))
    findings = [explain_finding(f, mode) for f in result["findings"]]
    findings = emphasize_for_goal(findings, p.goal)
    return {
        "up": list(map(float, up)),
        "rotation": rotation.tolist(),
        "mode": mode,
        "findings": findings,
        "stats": result["stats"],
        "settings": recommend_settings(p, result["findings"], result["stats"]) if mode != "design" else [],
        "z_strength_factor": result["z_strength_factor"],
        "face_temp": _face_temp(oriented, result["stats"]["layer_heat"], spots),
    }


def _grip_findings(name: str) -> list[dict]:
    """grip_texture when the file name says the part is held (a knob, handle, …: ``grip`` words in
    thresholds.yaml, at the start of a word so "radial" isn't a dial)."""
    import re

    words = load_data("thresholds")["grip"]["words"]
    hit = re.search(r"(?<![a-z])(" + "|".join(map(re.escape, words)) + ")", (name or "").lower())
    if not hit:
        return []
    return [{"rule": "grip_texture", "severity": "info", "bbox": None, "metrics": {"word": hit.group(1)},
             "detail": f"A {hit.group(1)} is held by hand: fuzzy skin on its outer walls gives it a grippy, matte surface "
                       "that also hides the layer lines"}]


def _gcode_to_oriented(rotation: np.ndarray):
    """Printer coordinates of the loaded G-code -> the oriented part (``orient``: centred, on z = 0)."""
    s = _require()
    g = s.gcode
    turned = s.mesh.triangles.reshape(-1, 3) @ rotation.T
    lo, hi = turned.min(axis=0), turned.max(axis=0)
    base = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
    return lambda x: x @ g["q"].T + g["shift"] - base


def _spot_heat(rotation: np.ndarray, p: Profile) -> dict:
    from .gcode import spot_heat

    return spot_heat(_require().gcode["toolpath"], _gcode_to_oriented(rotation), p)


def _spot_findings(spots: dict, p: Profile, findings: list[dict]) -> list[dict]:
    """Spots that get the next layer while still soft, line by line from the G-code: only when the
    layer heat check (whole layers) didn't already say so for that height."""
    C = load_data("thresholds")["cooling"]
    soft = float(p.material["softening_c"])
    hot = (spots["temp_c"] >= soft + C["soft_margin_c"]) & (spots["z"] > C["above_bed_mm"])
    if not hot.any():
        return []
    zs = np.unique(spots["z"][hot])
    if zs[-1] - zs[0] < C["min_run_mm"]:
        return []
    known = [f["bbox"] for f in findings if f["rule"] == "small_layers_cooling" and f.get("bbox")]
    i = int(np.argmax(np.where(hot, spots["temp_c"], -np.inf)))
    if any(b[0][2] - 0.5 <= spots["z"][i] <= b[1][2] + 0.5 for b in known):
        return []
    near = hot & (np.linalg.norm(spots["xy"] - spots["xy"][i], axis=1) <= 10)
    xy, z = spots["xy"][near], spots["z"][near]
    half = spots["cell_mm"] * 1.5
    t, wait = float(spots["temp_c"][i]), float(spots["seconds"][i])
    sev = "warn" if t >= soft + C["hot_margin_c"] else "info"
    return [{"rule": "small_layers_cooling", "severity": sev, "bbox": [[*(xy.min(axis=0) - half).tolist(), float(z.min())], [*(xy.max(axis=0) + half).tolist(), float(z.max())]],
             "metrics": {"temp_c": round(t, 1), "softening_c": soft, "seconds": round(wait, 1)},
             "detail": f"Line by line from your G-code: a spot at {z.min():.0f}–{z.max():.0f} mm gets the next layer after only {wait:.0f} s and is "
                       f"still about {t:.0f} °C ({p.material['name']} softens from about {soft:.0f} °C), so it can droop or blob. "
                       f"A longer minimum layer time, or a second copy on the plate, gives it time to cool"}]


HEAT_COLOUR_C = (10.0, 55.0)  # °C above softening: no colour … full red


def _face_temp(oriented: Mesh, heat: dict, spots: dict | None = None) -> bytes:
    """Heat build-up per triangle (uint8, 255 = far above softening while printing), for the viewer.
    With ``spots`` (line by line from the G-code) each triangle takes the warmest spot within a cell of it
    on its layer; triangles without one (the top, the first layer) keep the whole layer's."""
    z = oriented.triangles[..., 2].mean(axis=1)
    temp = np.interp(z, heat["z"], heat["temp_c"])
    if spots is not None and len(spots["z"]):
        c = oriented.triangles.mean(axis=1)
        zs = np.unique(spots["z"])
        level = np.clip(np.searchsorted(zs, c[:, 2]), 0, len(zs) - 1)
        cell = spots["cell_mm"]
        key = lambda lv, xy: (lv * 100_003 + np.floor(xy[:, 0] / cell).astype(np.int64)) * 100_003 + np.floor(xy[:, 1] / cell).astype(np.int64)
        sk = key(np.searchsorted(zs, spots["z"]), spots["xy"])
        order = np.argsort(sk)
        sk, st = sk[order], spots["temp_c"][order]
        best = np.full(len(c), -np.inf)
        for dx in (-cell, 0, cell):
            for dy in (-cell, 0, cell):
                k = key(level, c[:, :2] + [dx, dy])
                j = np.clip(np.searchsorted(sk, k), 0, len(sk) - 1)
                best = np.where(sk[j] == k, np.maximum(best, st[j]), best)
        temp = np.where(np.isfinite(best), best, temp)
    over = temp - heat["softening_c"]
    lo, hi = HEAT_COLOUR_C
    return (np.clip((over - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8).tobytes()


def _thread_findings(oriented: Mesh, rotation: np.ndarray, p: Profile) -> list[dict]:
    """Threads modelled into the part (features.find_threads, along the model's own axes), with what
    this orientation and layer height do to them (modelled_thread)."""
    from .features import find_threads

    T = load_data("thresholds")["threads"]
    lh = p.layer_height
    out = []
    for t in find_threads(oriented, axes=np.asarray(rotation).T):  # the model's x, y, z in the print frame (R's columns)
        what = (f"{t['size']} " if t["size"] else f"Ø{t['major_mm']:.1f} mm ") + ("bolt thread" if t["external"] else "threaded hole")
        upright = abs(t["axis"][2]) > 0.9
        lying = abs(t["axis"][2]) < 0.3
        layers = t["pitch_mm"] / lh
        tips = []
        bad = False
        if t["major_mm"] < T["min_major_mm"]:
            bad = True
            tips.append(f"threads smaller than M{T['min_major_mm']:g} print poorly: a heat-set insert, a nut, or tapping a plain "
                        "hole holds better")
        if upright and layers < T["min_layers_per_pitch"]:
            bad = True
            tips.append(f"standing up it gets only {layers:.1f} layers per turn of the thread ({t['pitch_mm']:g} mm pitch, "
                        f"{lh:g} mm layers): print it with layers of {t['pitch_mm'] / T['min_layers_per_pitch']:.2f} mm or "
                        "finer there (variable layer height)")
        if lying:
            bad = bad or t["major_mm"] < T["lying_ok_major_mm"]
            tips.append("it lies on its side, so its top and bottom come out as layer steps and the thread binds: stand it "
                        "up if you can")
        tips.append(f"leave about {T['clearance_mm']:g} mm between the printed bolt and nut, and screw it in and out a few "
                    "times to clean the thread before use")
        lo, hi = np.asarray(t["lo"]), np.asarray(t["hi"])
        out.append({"rule": "modelled_thread", "severity": "warn" if bad else "info", "bbox": [lo.tolist(), hi.tolist()],
                    "metrics": {"size": t["size"], "pitch_mm": t["pitch_mm"], "major_mm": t["major_mm"], "external": t["external"],
                                "layers_per_pitch": round(layers, 1), "upright": upright},
                    "detail": f"Modelled {what} ({t['pitch_mm']:g} mm pitch, {t['length_mm']:.0f} mm long): " + "; ".join(tips)})
    return out


def _clean_face_findings(mesh: Mesh, up, p: Profile, marked: dict | None, rotation: np.ndarray) -> list[dict]:
    """Support marks on a face the user wants clean: the one place they matter most."""
    if not marked or not marked["clean"]:
        return []
    from .orientation import NEEDS_SUPPORT

    _, areas = mesh.normals_and_areas()
    classes = classify_faces(mesh, up, orientation_settings(p))
    placed = mesh.triangles @ rotation.T  # same placement as orient(): centred on x/y, lowest point at z = 0
    lo, hi = placed.reshape(-1, 3).min(axis=0), placed.reshape(-1, 3).max(axis=0)
    placed = placed + [-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]]
    out = []
    for m in marked["clean"]:
        hit = m & (classes == NEEDS_SUPPORT)
        if areas[hit].sum() < 0.05 * areas[m].sum():
            continue
        pts = placed[hit].reshape(-1, 3)
        out.append({"rule": "support_marks", "severity": "problem", "impact": "surface",
                    "bbox": [pts.min(axis=0).tolist(), pts.max(axis=0).tolist()], "metrics": {"area": float(areas[hit].sum())},
                    "detail": f"Supports would touch the face you want clean (~{areas[hit].sum():.0f} mm²) — pick another orientation or accept marks there"})
    return out


def diagnose(symptom: str | None = None, up=None, profile: dict | None = None, findings: list[dict] | None = None) -> dict:
    """"My print failed": for what the user saw (``symptom``, a key of symptoms.yaml; None lists them), its usual
    causes and fixes, and ``on_your_part``: the findings that predicted it on this part - from ``findings``
    (the last check's, as the app has them) or a check in direction ``up``. Sorted most serious first."""
    data = load_data("symptoms")["symptoms"]
    if symptom is None:
        return {"symptoms": [{"id": k, "label": v["label"]} for k, v in data.items()]}
    if symptom not in data:
        raise ValueError(f"symptom must be one of {', '.join(data)}")
    entry = data[symptom]
    if findings is None and up is not None:
        findings = check(up, profile)["findings"]
    rank = {"problem": 0, "warn": 1, "info": 2}
    mine = sorted((f for f in findings or [] if f.get("rule") in entry["rules"]), key=lambda f: rank.get(f.get("severity"), 3))
    return {"symptom": symptom, "label": entry["label"], "why": entry["why"], "fixes": entry["fixes"], "sources": entry["sources"],
            "on_your_part": [{"rule": f["rule"], "title": f.get("title") or load_data("rules")["rules"][f["rule"]]["title"],
                              "detail": f.get("detail", ""), "severity": f.get("severity")} for f in mine]}


FIT_WORDS = {"press": "press together tightly", "snug": "push together snugly", "sliding": "slide and turn",
             "loose": "move freely with some play"}


def clearance(data: bytes, profile: dict | None = None) -> dict:
    """The gap between the loaded part and a mating part (``data``: STL / 3MF / OBJ exported from the same CAD
    assembly, so both keep their places): the closest place (``gap_mm``, negative when they overlap; ``at``, part
    coordinates), the fit it gives (``verdict``: overlap, too tight, press, snug, sliding, loose, free) against the
    fit gaps (your fit test print if measured) and a plain ``explanation``."""
    from .clearance import clearance as measure
    from .formats import read_bodies

    s = _require()
    p = Profile.from_dict(profile)
    mate = Mesh(np.concatenate([m.triangles for _, m in read_bodies(data)]))
    gaps = {**load_data("thresholds")["fits"]["gap_mm"], **(p.material.get("fit_gap_mm") or {})}
    r = measure(s.mesh, mate, gaps)
    side = r["per_side_mm"]
    g = r["gap_mm"]
    if r["verdict"] == "overlap":
        text = f"The parts overlap by {-g:.2f} mm: they won't go together. Leave about {side['sliding']:.2f} mm per side for a sliding fit."
    elif r["verdict"] == "free":
        text = "The parts don't come closer than 1 mm anywhere: no fit between them to check."
    elif r["verdict"] == "too tight":
        text = (f"The closest gap is {g:.2f} mm per side: printed, that fuses or needs force. Leave {side['snug']:.2f} mm to push "
                f"together snugly, {side['sliding']:.2f} mm to slide and turn.")
    else:
        nxt = {"press": "snug", "snug": "sliding", "sliding": "loose"}.get(r["verdict"])
        text = f"The closest gap is {g:.2f} mm per side: the parts will {FIT_WORDS[r['verdict']]}."
        if nxt:
            text += f" For them to {FIT_WORDS[nxt]}, leave {side[nxt]:.2f} mm."
    measured = bool(p.material.get("fit_gap_mm"))
    text += " (Gaps from your fit test print.)" if measured else " (Typical gaps: the fit test print in My materials gives your printer's.)"
    return {**r, "explanation": text}


def setup_check(profile: dict | None = None, settings: dict | None = None) -> dict:
    """Mistakes in the chosen printer / material / nozzle (presets the user made) and in slicer values
    they set (``settings``, slicer keys such as layer_height, support_top_z_distance, outer_wall_speed):
    explained findings, no part needed."""
    from .checks import setup_findings

    return {"findings": [explain_finding(f) for f in setup_findings(Profile.from_dict(profile), settings)]}


def explain_finding(finding: dict, mode: str = "both") -> dict:
    rule = load_data("rules")["rules"].get(finding["rule"], {})
    domains = MODES[mode]
    all_fixes = rule.get("fixes", [])
    return {
        **finding,
        "title": rule.get("title", finding["rule"]),
        "why": rule.get("why", ""),
        "fixes": [f for f in all_fixes if f["domain"] in domains],
        # Fix kinds left out by the mode, so the UI can say "more fixes if you can change the model".
        "hidden_fixes": sorted({f["domain"] for f in all_fixes if f["domain"] not in domains}),
    }


# How the goal shifts severity, per impact group: +1 = one step more serious, -1 = less.
# A strong part can't afford weak spots; a fast or cheap print accepts rougher surfaces
# and weak spots the user has been told about.
GOAL_EMPHASIS = {
    "strength": {"weak": +1},
    "speed": {"weak": -1, "surface": -1},
    "material": {"weak": -1, "surface": -1},
}
_SEVERITIES = ["info", "warn", "problem"]
_GOAL_NAME = {"strength": "Strength", "speed": "Speed", "material": "Material"}


def emphasize_for_goal(findings: list[dict], goal: str) -> list[dict]:
    """Raise or lower severities to match what the user optimises for, then re-sort."""
    from .checks import IMPACT_ORDER, SEVERITY_ORDER

    shift = GOAL_EMPHASIS.get(goal, {})
    out = []
    for f in findings:
        step = shift.get(f.get("impact"), 0)
        k = _SEVERITIES.index(f["severity"])
        new = _SEVERITIES[min(max(k + step, 0), len(_SEVERITIES) - 1)]
        if new != f["severity"]:
            word = "More important" if step > 0 else "Less important"
            f = {**f, "severity": new, "goal_note": f"{word} because you optimise for {_GOAL_NAME[goal]}"}
        out.append(f)
    out.sort(key=lambda f: (IMPACT_ORDER.index(f.get("impact", "info")), SEVERITY_ORDER[f["severity"]]))
    return out


def export(up, profile: dict | None = None, settings: dict | None = None, zones: list[dict] | None = None, slicer: str = "orca",
           pauses: list[float] | None = None, fine_layers: list[dict] | None = None, faces: dict | None = None,
           grip: bool = False) -> bytes:
    """Oriented part as 3MF, with slicer settings for OrcaSlicer / Bambu Studio / PrusaSlicer.
    ``fine_layers``: {"z0", "z1", "layer_height"} each (the metrics of shallow_slope_stairs findings):
    thinner layers over just that height. ``faces`` as in ``orientations``: no seam on the faces marked
    "clean" (the ones people see; painted as seam blockers). ``grip``: fuzzy skin on the outer walls
    (recommend.grip_settings; the slicer settings given win).
    ``pauses``: pocket tops (mm, ``top_z`` of a pause_to_insert finding): the print pauses before the first
    layer above each, to put something into the pocket (OrcaSlicer and PrusaSlicer; Bambu Studio ignores it).
    ``zones`` (from optimise_strength's ``zoned``: lo, hi, walls, infill) become modifier volumes, and
    the advanced design boxes (volume_at) support enforcers, blockers and negative volumes; then the
    file is for one slicer: ``slicer`` "orca" (also Bambu Studio) or "prusa"."""
    s = _require()
    p = Profile.from_dict(profile)
    if settings is None:
        settings = {r["key"]: r["value"] for r in check(up, profile)["settings"]}
    if grip:
        settings = {**settings, **grip_settings(p), **{k: v for k, v in settings.items() if k.startswith("fuzzy_skin")}}
    bed = p.printer["bed"]
    rotation, _ = print_rotation(s.mesh, up, bed, _moving(p))
    modifiers = []
    for z in zones or []:
        kind = z.get("kind", "stronger")
        if kind in DESIGN_ONLY:  # design aids and model changes: nothing for the slicer
            continue
        conf = {"wall_loops": str(int(z["walls"])), "sparse_infill_density": f"{float(z['infill']):.0%}"} if kind == "stronger" else {}
        modifiers.append((_zone_box(s.mesh, up, rotation, z), conf, kind))
    if any(m[2] == "support" for m in modifiers) and str(settings.get("enable_support", "0")) == "0":
        # Supports only where the user put them: the slicer's own placement stays off.
        settings = {**settings, "enable_support": "1", "support_type": "tree(manual)"}
    return export_3mf(orient(s.mesh, rotation), s.name, settings, bed_center=(bed[0] / 2, bed[1] / 2), modifiers=modifiers, layout=slicer,
                      pauses=[{"top": float(z), "layer_height": float(settings.get("layer_height") or p.layer_height),
                               "note": "Put the part into the pocket"} for z in pauses or []],
                      ranges=[{k: float(r[k]) for k in ("z0", "z1", "layer_height")} for r in fine_layers or []],
                      seam_block=_seam_block(faces))


def _seam_block(faces: dict | None) -> np.ndarray | None:
    """Triangles of the faces marked "clean" (seen): the seam stays off them. None when none is marked."""
    marked = _marked_faces(faces)
    if not marked or not marked["clean"]:
        return None
    return np.logical_or.reduce(marked["clean"])


def _zone_box(mesh: Mesh, up, rotation: np.ndarray, zone: dict) -> np.ndarray:
    """A zone's box as triangles, in the frame of the oriented part (see orient()), kept between the
    bed and the top of the part: a modifier reaching below the bed lifts the part in PrusaSlicer."""
    r0 = rotation_to_z(up)
    zs = (mesh.triangles.reshape(-1, 3) @ r0.T)[:, 2]
    lo, hi = np.array(zone["lo"], float), np.array(zone["hi"], float)
    lo[2], hi[2] = max(lo[2], zs.min()), min(hi[2], zs.max())
    part = (_unit_shape(zone.get("shape", "box")) * (hi - lo) / 2 + (lo + hi) / 2) @ r0  # build frame -> part
    turned = mesh.triangles.reshape(-1, 3) @ rotation.T
    lo_t, hi_t = turned.min(axis=0), turned.max(axis=0)
    return part @ rotation.T - [(lo_t[0] + hi_t[0]) / 2, (lo_t[1] + hi_t[1]) / 2, lo_t[2]]


def _unit_shape(shape: str, n: int = 32) -> np.ndarray:
    """Closed, outward-wound triangles of a shape filling the cube [-1, 1]³: box, upright cylinder or ball."""
    if shape == "box":
        v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * 2 - 1
        f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
        return v[np.array(f)]
    a = np.linspace(0, 2 * np.pi, n + 1)
    ring = np.stack([np.cos(a), np.sin(a)], axis=1)
    if shape == "cylinder":
        p0, p1 = ring[:-1], ring[1:]
        lo = lambda p: np.c_[p, -np.ones(len(p))]
        hi = lambda p: np.c_[p, np.ones(len(p))]
        zero = np.zeros((n, 2))
        return np.concatenate([np.stack([lo(p0), lo(p1), hi(p1)], 1), np.stack([lo(p0), hi(p1), hi(p0)], 1),
                               np.stack([hi(zero), hi(p0), hi(p1)], 1), np.stack([lo(zero), lo(p1), lo(p0)], 1)])
    rows = n // 2
    lat = np.linspace(-np.pi / 2, np.pi / 2, rows + 1)
    grid = np.stack([np.cos(lat)[:, None] * ring[None, :, 0], np.cos(lat)[:, None] * ring[None, :, 1],
                     np.repeat(np.sin(lat)[:, None], n + 1, axis=1)], axis=-1)
    a, b, c, d = grid[:-1, :-1], grid[:-1, 1:], grid[1:, 1:], grid[1:, :-1]
    tris = np.concatenate([np.stack([a, b, c], 2).reshape(-1, 3, 3), np.stack([a, c, d], 2).reshape(-1, 3, 3)])
    area = np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1)
    return tris[area > 1e-12]  # the poles' collapsed triangles


def compare_nozzles(up, profile: dict | None = None) -> list[dict]:
    """The same part with every nozzle: print time, filament, the nozzle-dependent problems (``new``:
    not there with the smallest nozzle) and the detail it can't trace (checks.lost_detail)."""
    from .checks import NOZZLE_RULES, lost_detail

    s = _require()
    base = Profile.from_dict(profile)
    rotation, _ = print_rotation(s.mesh, up, base.printer["bed"], _moving(base))
    oriented = orient(s.mesh, rotation)
    detail_layers = slice_mesh(oriented, 0.2, 80)  # one set of layers, so only the nozzle differs
    out = []
    nozzles = sorted(presets()["nozzles"], key=float)
    for i, nozzle in enumerate(nozzles):
        p = Profile.from_dict({**(profile or {}), "nozzle": nozzle})
        with stage(i / len(nozzles), (i + 1) / len(nozzles)):
            r = run_checks(oriented, p, max_layers=80)
        issues = [explain_finding(f) for f in r["findings"] if f["rule"] in NOZZLE_RULES]
        out.append({
            "nozzle": nozzle,
            "print_time_min": r["stats"]["print_time_min"],
            "filament_g": r["stats"]["filament_g"],
            "issues": [{"rule": f["rule"], "title": f["title"], "detail": f["detail"]} for f in issues],
            "current": nozzle == base.nozzle,
            "walls": goal_plan(p)["walls"],
            **lost_detail(detail_layers, float(nozzle)),
        })
    first = {f["title"] for f in out[0]["issues"]}
    for row in out:
        for f in row["issues"]:
            f["new"] = f["title"] not in first
    # Each nozzle with its own usual settings. Bigger nozzles' profiles print slower, and a wider line
    # holds more plastic per mm, so once the hotend melts as fast as it can the nozzle moves slower:
    # on small parts a bigger nozzle often saves little time (OrcaSlicer's own profiles: L-bracket
    # 27 / 22 / 23 min with 0.4 / 0.6 / 0.8 mm on a P1S).
    for small, big in zip(out, out[1:]):
        if big["print_time_min"] >= 0.95 * small["print_time_min"]:
            big["note"] = (f"Not faster than {small['nozzle']} mm on this part: slicer profiles run bigger nozzles slower, "
                           f"and your hotend melts only about {base.material['max_flow_mm3s']:g} mm³/s of {base.material['name']}, "
                           "so wide lines can't move fast. Bigger nozzles save time on big, chunky parts")
    return out


def md_cell(value) -> str:
    """Text for one Markdown table cell: a ``|`` or a line break in it would split the row."""
    return " ".join(str(value).splitlines()).replace("|", "\\|")


def report(up, profile: dict | None = None, mode: str = "print") -> str:
    """Markdown report for sharing or for pasting into an AI chat."""
    s = _require()
    p = Profile.from_dict(profile)
    r = check(up, profile, mode)
    st = r["stats"]
    lines = [
        f"# Print check: {s.name}",
        "",
        f"- Printer: {p.printer['name']} · Material: {p.material['name']} · Nozzle: {p.nozzle} mm",
        f"- Build direction (part axis pointing up): {np.round(r['up'], 3).tolist()}",
        f"- Height {st['height_mm']:.1f} mm · ≈{st['print_time_min']:.0f} min · ≈{st['filament_g']:.0f} g (rough estimates)",
        "",
        "## Findings",
        "",
    ]
    if not r["findings"]:
        lines.append("No issues found.")
    for f in r["findings"]:
        lines += [f"### [{f['severity'].upper()}] {f['title']}", "", f["detail"], "", f["why"], ""]
        lines += [f"- *{fx['domain']}*: {fx['text']}" for fx in f["fixes"]] + [""]
    if r["settings"]:
        lines += ["## Recommended slicer settings", "", "| Setting | Value | Why |", "|---|---|---|"]
        lines += [f"| {md_cell(x['label'])} (`{x['key']}`) | {md_cell(x['value'])} | {md_cell(x['reason'])} |" for x in r["settings"]]
    return "\n".join(lines) + "\n"


METHODS = {
    "presets": get_presets,
    "custom_presets": custom_presets,
    "calibration_part": calibration_part,
    "load": load,
    "load_gcode": load_gcode,
    "pause_gcode": pause_gcode,
    "setup_check": setup_check,
    "clearance": clearance,
    "diagnose": diagnose,
    "warping": warping,
    "set_fit": set_fit,
    "set_fastener": set_fastener,
    "split": split,
    "set_gap": set_gap,
    "volume_solid": volume_solid,
    "snap_model": snap_model,
    "material_advice": material_advice,
    "heat_curve": heat_curve,
    "dynamics": dynamics,
    "select_body": select_body,
    "scale": scale,
    "orientations": orientations,
    "orientation": orientation,
    "check": check,
    "export": export,
    "report": report,
    "regions": regions,
    "optimise_strength": optimise_strength,
    "expert_settings": expert_settings,
    "set_expert": set_expert,
    "box_heat": box_heat,
    "zone_at": zone_at,
    "volume_at": volume_at,
    "tag_feature": tag_feature,
    "session_state": session_state,
    "holes_state": holes_state,
    "restore_holes": restore_holes,
    "restore_state": restore_state,
    "strength": strength,
    "strength_variant": strength_variant,
    "strength_options": strength_options,
    "design": design,
    "compare_nozzles": compare_nozzles,
    "topology": topology,
}


def dispatch(method: str, args_json: str = "{}", data: bytes | None = None):
    """Single entry point for the web worker (and later MCP): JSON args in, result out.
    ``data`` carries file bytes for ``load``."""
    args = json.loads(args_json)
    if data is not None:
        # A JS Uint8Array from the worker: bytes(proxy) would copy element by element (~10 s for 30 MB).
        args["data"] = data.to_bytes() if hasattr(data, "to_bytes") else data
    return _plain(METHODS[method](**args))


def _plain(value):
    """Convert numpy scalars/arrays to plain Python so every result converts cleanly to JS/JSON."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value
