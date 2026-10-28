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

from .checks import orient, run_checks
from .export3mf import export_3mf
from .formats import read_bodies, unit_hint
from .mesh import Mesh
from .orientation import OrientationSettings, classify_faces, evaluate, find_orientations, rotation_to_z
from .placement import print_rotation
from .profile import Profile, load_data, presets
from . import progress
from .progress import stage
from .recommend import recommend_settings

MODES = {"print": {"slicer", "process"}, "design": {"geometry", "process"}, "both": {"geometry", "slicer", "process"}}


@dataclass
class Session:
    mesh: Mesh
    name: str
    regions: np.ndarray | None = None  # region id per triangle, computed on demand
    load_case: tuple | None = None  # (loads json, prepared LoadCase) cache
    bodies: list = field(default_factory=list)  # (name, Mesh) per body in the file
    body: int = 0  # which body is loaded


_session: Session | None = None


def _require() -> Session:
    if _session is None:
        raise RuntimeError("No part loaded")
    return _session


def get_presets() -> dict:
    return presets()


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
    s.mesh, s.body, s.regions, s.load_case = s.bodies[index][1], index, None, None
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


def orientations(profile: dict | None = None, top: int = 5, loads: list[dict] | None = None) -> dict:
    """Best build directions for the loaded part, plus how it scores as loaded.

    With ``loads``, candidates are also ranked by strength: a weak orientation is
    penalised even when it prints more easily.
    """
    s = _require()
    p = Profile.from_dict(profile)
    settings = orientation_settings(p)
    progress.report(0, "Scoring directions")
    candidates = find_orientations(s.mesh, settings, top=3 * top)  # extra, some get filtered out below
    as_loaded = evaluate(s.mesh, [0, 0, 1], settings)
    progress.report(0.2, "Scoring directions")
    safety = {}
    if loads:
        from .strength import orientation_safety, verdict

        case = _load_case(loads)
        ups = np.array([r.up for r in candidates] + [as_loaded.up])
        with stage(0.2, 0.95):
            sfs = orientation_safety(case, ups, p)
        for r, sf in zip([*candidates, as_loaded], sfs):
            safety[id(r)] = float(sf)
            r.reasons.append(_strength_reason(sf, verdict(sf)))
            r.penalize("strength", STRENGTH_WEIGHT * max(0.0, 2.0 - float(sf)))
        candidates.sort(key=lambda r: r.score)
    elif p.goal == "strength":
        with stage(0.2, 0.95):
            _rank_by_weak_spots(s.mesh, candidates, p, extra=[as_loaded])

    # Hide options that don't sit on any flat face or are far worse than the best — unless
    # nothing better exists (e.g. a sphere). Users shouldn't have to wade through bad choices.
    flat = [r for r in candidates if r.bed_area > 0]
    if flat:
        best = min(r.score for r in flat)
        candidates = [r for r in flat if r.score <= best + MAX_SCORE_GAP] or flat[:1]

    # Mirror-image directions of symmetric parts score identically: show one of them.
    unique, seen = [], set()
    for r in candidates:
        key = (round(r.bed_area), round(r.support_area), round(r.height, 1))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    candidates = unique

    placed = {id(r): _placement(s.mesh, r, p) for r in [*candidates, as_loaded]}
    candidates.sort(key=lambda r: not placed[id(r)][1]["fits"])  # stable: keeps ranking among fitting ones
    candidates = candidates[:top]

    def pack(r, label):
        out = _pack(s.mesh, r, label, settings, placed[id(r)][0], safety.get(id(r)))
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
    "weak": "has more weak spots along the layers",
    "strength": "is weaker under your load",
}


def why_lower(terms: dict[str, float], best: dict[str, float], min_gap: float = 0.05) -> str:
    """Plain-language reason an option ranks below the recommended one, biggest difference first."""
    gaps = sorted(((terms.get(k, 0.0) - best.get(k, 0.0), k) for k in WORSE), reverse=True)
    worse = [WORSE[k] for gap, k in gaps if gap > min_gap][:2]
    if not worse:
        return "Almost as good as the recommended option"
    return "Ranked lower: " + " and ".join(worse)


def _placement(mesh: Mesh, r, p: Profile):
    """Print rotation for a candidate; adds a reason when it doesn't fit or had to be turned."""
    rotation, fit = print_rotation(mesh, r.up, p.printer["bed"])
    if not fit["fits"]:
        r.reasons.insert(0, f"Too big for the {p.printer['name']}: {fit['reason']}")
    elif fit["turned_deg"]:
        r.reasons.append(f"Turned {fit['turned_deg']:.0f}° on the bed so it fits")
    return rotation, fit


def orientation_settings(p: Profile) -> OrientationSettings:
    """Scoring weights for the user's goal. Multi-material makes supports cheaper (clean interfaces)."""
    s = OrientationSettings(max_overhang_deg=p.material["max_overhang_deg"])
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
        rotation, _ = print_rotation(mesh, r.up, p.printer["bed"])
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
    return _pack(s.mesh, r, label, settings, _placement(s.mesh, r, p)[0])


def _pack(mesh: Mesh, r, label: str, settings: OrientationSettings, rotation: np.ndarray, safety: float | None = None) -> dict:
    return {
        "label": label,
        "up": r.up.tolist(),
        "rotation": rotation.tolist(),
        "score": r.score,
        "support_area_mm2": r.support_area,
        "bed_area_mm2": r.bed_area,
        "height_mm": r.height,
        "safety_factor": safety,
        "reasons": r.reasons,
        "score_terms": {k: round(v, 3) for k, v in r.terms.items()},
        "face_classes": classify_faces(mesh, r.up, settings).tobytes(),
    }


STRENGTH_WEIGHT = 1.5  # score penalty per unit of safety factor below 2
MAX_SCORE_GAP = 3.0  # options scoring this much worse than the best are not shown


def _strength_reason(sf: float, verdict: str) -> str:
    if sf >= 2:
        return f"Strong enough for your load (safety ×{sf:.1f})"
    return f"{verdict.capitalize()} under your load (safety ×{sf:.1f})"


def regions() -> dict:
    """Clickable faces: region id per triangle (uint32 bytes) and region summaries."""
    from .regions import find_regions, region_info

    s = _require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    return {"face_regions": s.regions.astype("<u4").tobytes(), "regions": region_info(s.mesh, s.regions)}


def _load_case(loads: list[dict]):
    from .regions import find_regions
    from .strength import prepare

    s = _require()
    key = json.dumps(loads, sort_keys=True)
    if s.load_case is None or s.load_case[0] != key:
        if s.regions is None:
            s.regions = find_regions(s.mesh)
        s.load_case = (key, prepare(s.mesh, s.regions, loads))
    return s.load_case[1]


def strength(up, loads: list[dict], profile: dict | None = None, walls: int = 2, infill: float = 0.15) -> dict:
    """Will it hold? Safety factor, failure mode and a per-face heat map (uint8, 255 = breaks)."""
    from .strength import evaluate as evaluate_strength
    from .strength import face_values

    s = _require()
    p = Profile.from_dict(profile)
    case = _load_case(loads)
    r = evaluate_strength(case, up, p, walls, infill)
    heat = face_values(s.mesh, case.model.grid, case.model, r.pop("fi"))
    r["face_heat"] = (np.clip(heat / 1.25, 0, 1) * 255).astype(np.uint8).tobytes()
    r["critical_point_print"] = _to_print_frame(s.mesh, print_rotation(s.mesh, up, p.printer["bed"])[0], r["critical_point"])
    r.update(walls=walls, infill=infill, material=p.material["name"])
    return r


def strength_options(up, loads: list[dict], profile: dict | None = None, walls: int = 2, infill: float = 0.15) -> list[dict]:
    """What would make it stronger? Tries more walls, more infill and the strongest orientation."""
    from .strength import evaluate as evaluate_strength
    from .strength import orientation_safety, verdict

    p = Profile.from_dict(profile)
    case = _load_case(loads)
    out = []

    def add(change, kind, sf, **extra):
        out.append({"change": change, "kind": kind, "safety_factor": float(sf), "verdict": verdict(sf), **extra})

    more_walls = evaluate_strength(case, up, p, walls + 2, infill)["safety_factor"]
    add(f"{walls + 2} walls instead of {walls}", "slicer", more_walls, settings={"wall_loops": str(walls + 2)})
    dense = max(0.4, infill)
    if dense > infill:
        sf = evaluate_strength(case, up, p, walls, dense)["safety_factor"]
        add(f"{dense:.0%} infill instead of {infill:.0%}", "slicer", sf, settings={"sparse_infill_density": f"{dense:.0%}"})
    axes = np.vstack([np.eye(3), -np.eye(3)])
    sfs = orientation_safety(case, axes, p, walls, infill)
    best = int(np.argmax(sfs))
    if abs(float(np.dot(axes[best], np.asarray(up, float) / np.linalg.norm(up)))) < 0.99:
        add("Print in the strongest orientation", "orientation", sfs[best], up=axes[best].tolist())
    return out


def design(up, profile: dict | None = None) -> dict:
    """Detected features (holes, bosses) and suggested geometry edits for this orientation.
    Each edit has a ``cutter`` (float32 triangles, part coordinates) to subtract from the part."""
    from .features import design_edits, detect_features
    from .regions import find_regions

    s = _require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    p = Profile.from_dict(profile)
    return {"features": detect_features(s.mesh, s.regions), "edits": design_edits(s.mesh, s.regions, up, p)}


def _to_print_frame(mesh: Mesh, r: np.ndarray, point) -> list[float]:
    """Part-frame point -> coordinates of the oriented part shown on the bed (print rotation ``r``)."""
    pts = mesh.triangles.reshape(-1, 3) @ r.T
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return (r @ np.asarray(point) + shift).tolist()


def check(up, profile: dict | None = None, mode: str = "print") -> dict:
    """Run all printability checks for one orientation, explain them and recommend settings."""
    s = _require()
    p = Profile.from_dict(profile)
    rotation, fit = print_rotation(s.mesh, up, p.printer["bed"])
    oriented = orient(s.mesh, rotation)
    result = run_checks(oriented, p)
    if not fit["fits"]:
        result["findings"].insert(0, {"rule": "exceeds_build_volume", "severity": "problem", "bbox": None, "metrics": fit,
                                      "detail": f"Doesn't fit the {p.printer['name']}: {fit['reason']}"})
    elif fit["turned_deg"]:
        result["findings"].append({"rule": "turned_to_fit", "severity": "info", "bbox": None, "metrics": fit,
                                   "detail": f"Turned {fit['turned_deg']:.0f}° on the bed so it fits the {p.printer['name']}"})
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
    }


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


def export(up, profile: dict | None = None, settings: dict | None = None) -> bytes:
    """Oriented part as 3MF, with slicer settings for OrcaSlicer / Bambu Studio."""
    s = _require()
    p = Profile.from_dict(profile)
    if settings is None:
        settings = {r["key"]: r["value"] for r in check(up, profile)["settings"]}
    bed = p.printer["bed"]
    rotation, _ = print_rotation(s.mesh, up, bed)
    return export_3mf(orient(s.mesh, rotation), s.name, settings, bed_center=(bed[0] / 2, bed[1] / 2))


def compare_nozzles(up, profile: dict | None = None) -> list[dict]:
    """The same part with every nozzle: print time, filament and the nozzle-dependent problems."""
    from .checks import NOZZLE_RULES

    s = _require()
    base = Profile.from_dict(profile)
    rotation, _ = print_rotation(s.mesh, up, base.printer["bed"])
    oriented = orient(s.mesh, rotation)
    out = []
    nozzles = list(presets()["nozzles"])
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
        })
    return out


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
        lines += [f"| {x['label']} (`{x['key']}`) | {x['value']} | {x['reason']} |" for x in r["settings"]]
    return "\n".join(lines) + "\n"


METHODS = {
    "presets": get_presets,
    "load": load,
    "select_body": select_body,
    "scale": scale,
    "orientations": orientations,
    "orientation": orientation,
    "check": check,
    "export": export,
    "report": report,
    "regions": regions,
    "strength": strength,
    "strength_options": strength_options,
    "design": design,
    "compare_nozzles": compare_nozzles,
}


def dispatch(method: str, args_json: str = "{}", data: bytes | None = None):
    """Single entry point for the web worker (and later MCP): JSON args in, result out.
    ``data`` carries file bytes for ``load``."""
    args = json.loads(args_json)
    if data is not None:
        args["data"] = data
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
