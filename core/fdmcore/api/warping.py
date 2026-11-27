from __future__ import annotations

import json
from typing import TYPE_CHECKING

import numpy as np

from ..checks import orient, slice_mesh
from ..messages import format_message
from ..placement import print_rotation
from ..profile import Profile
from ..progress import stage
from . import check as _check
from . import design as _design
from . import export as _export
from . import orientation as _orient
from . import session as _S
from . import strength as _strength

if TYPE_CHECKING:
    from ..strength import ReleasedStrain, Shells


WARP_VERDICT = ((0.7, "fine"), (1.3, "might lift"))  # lift ratio below these, like the warping check's length ratio


_warp_reference: dict = {}


def warping(up, profile: dict | None = None, detailed: bool = False, ranges: bool = True,
            settings: dict | None = None, slicer: str = "orca", excluded_settings: list[str] | None = None, *,
            walls: int | None = None, infill: float | None = None, top: int | None = None,
            bottom: int | None = None, zones: list[dict] | None = None) -> dict:
    """Warping simulation (warp.py): how hard the base wants to curl off the bed, as a ratio to the
    longest bar that stays flat (1 = like that bar), where it lifts first, and the curled shape
    (exaggerated) for the FEA view: ``field_triangles``, ``vertex_disp`` (part frame), ``vertex_value`` (mm);
    the stress left inside after it cools: ``stress_value`` (MPa per vertex), ``stress_max`` (MPa, 99th percentile
    of the elements: single voxel corners spike) and ``stress_share`` (of what the layers hold).
    ``detailed``: with the loaded G-code's real layer times (layers that come quickly are still warm
    when the next go on and shrink less); ``from_gcode`` says whether it was used. ``ranges`` includes
    a curl range in mm and, for quick runs where both peel trials settle, possible bed lift verdicts.
    Brim settings, if given, override the recommendations, just as on download. Walls, infill, top,
    bottom and zones use the same settings as ``strength``."""
    from .. import warp
    from ..gcode import layer_times
    from ..strength import ReleasedStrain, refine, sample_nodes, surface_values
    from ..thermal import chamber_conditions

    s = _S._require()
    p = Profile.from_dict(profile)
    sh = _strength._shells(p, walls, infill, top, bottom, zones)
    tp = None
    if s.gcode is not None and np.dot(s.gcode["up"], np.asarray(up, float) / np.linalg.norm(up)) >= 0.999:
        tp = s.gcode["toolpath"]
    climate = chamber_conditions(p, toolpath=tp)
    sliced = layer_times(tp) if tp is not None else None
    warm = _warm_layers(up, p, sliced, climate) if detailed and sliced is not None else None

    def simulate(brim: dict | None = None) -> dict:
        return warp.simulate(s.mesh, up, p, sh, _S._grid("warp_voxels"), warm=warm, climate=climate, sliced=sliced,
                             adhesion=True, peel_range=ranges and not detailed, brim=brim)

    setup = _warp_setup(p, sh)

    def summarize(r: dict) -> tuple[dict, float, float | None]:
        part = r.get("part_nodes", slice(None))
        model, turn = r.get("part_model", r["model"]), r["rotation"]
        h = model.grid.h
        key = (*setup, round(h, 3))
        if key not in _warp_reference:
            _warp_reference[key] = warp.reference(p, h, sh)
        ref = _warp_reference[key]
        curl = warp.curl(r)
        ratio = float(np.sqrt(curl / ref)) if ref else None
        verdict = next((v for limit, v in WARP_VERDICT if ratio is not None and ratio < limit), "likely to lift")
        adhesion = r.get("adhesion")
        peel_point = adhesion["point"] @ turn if adhesion and adhesion.get("may_lift") else None
        # set_fit and set_gap need displacement on the part alone.
        saved = {"up": np.asarray(up, float) / np.linalg.norm(up), "model": model, "u": r["u"][part] @ turn,
                 "turn": turn, "verdict": verdict, "setup": setup, "adhesion": adhesion, "peel_point": peel_point}
        # Strength reads the stress locked into the part; the brim is cut off after printing.
        elastic = warp.residual_strain(r)[r.get("part_elems", slice(None))]
        if r["solver_converged"] and np.isfinite(elastic).all():
            saved.update(released=ReleasedStrain(model, turn, elastic), from_gcode=warm is not None)
        return saved, curl, ratio

    settings = settings or {}
    excluded = set(excluded_settings or ())
    chosen = None
    initial = None
    if "brim_type" not in settings and not {"brim_type", "brim_width"} & excluded:
        # A recommendation based on the bare part stays in place after we test its effect.
        with stage(0.0, 0.45):
            r = simulate()
        initial, curl, ratio = summarize(r)
        previous = s.warp
        try:
            s.warp = initial
            with stage(0.45, 0.5):
                recommendation = _check.check(up, profile)
        finally:
            s.warp = previous
        chosen = recommendation["print_risk"]["hold"]
        settings = {**{row["key"]: row["value"] for row in recommendation["settings"]}, **settings}
    settings = _export._export_brim_settings(p, settings, excluded, slicer=slicer)
    has_brim = (settings.get("brim_type") in warp.BRIM_TYPES and 0 < float(settings.get("brim_width") or 0) < float("inf")
                and not float(settings.get("raft_layers") or 0))
    if initial is None or has_brim:
        with stage(0.0 if initial is None else 0.5, 1.0):
            r = simulate(settings if has_brim else None)
        s.warp, curl, ratio = summarize(r)
        if initial is not None:
            s.warp["recommended_hold"] = chosen
    else:
        s.warp = initial
    model, turn, u = (s.warp[key] for key in ("model", "turn", "u"))
    verdict, adhesion, peel_point = (s.warp[key] for key in ("verdict", "adhesion", "peel_point"))
    # Shape for the view: the part's own movement without the rigid shift and tilt it gets off the bed.
    pos = (model.grid.origin + np.argwhere(model.node_index >= 0) * model.grid.h) @ turn  # part frame
    u = _without_rigid_motion(pos, u)
    fine = refine(s.mesh.triangles, model.grid.h)
    disp = sample_nodes(model, u, fine.reshape(-1, 3) @ turn.T)
    size = np.linalg.norm(disp, axis=1)
    rot = print_rotation(s.mesh, up, p.printer["bed"], _orient._moving(p))[0]
    pull = r["pull"][r.get("part_nodes", slice(None))]
    order = np.argsort(-pull)
    spots = [peel_point] if peel_point is not None else []
    for i in order[: 40]:
        if pull[i] <= 0 or len(spots) == 3:
            break
        if all(np.linalg.norm(pos[i] - q) > 10 for q in spots):
            spots.append(pos[i])
    out = {"lift_ratio": ratio, "verdict": verdict, "curl_mm": round(curl, 3), "reference_mm": warp.warp_limit(p),
           "field_triangles": fine.astype(np.float32).tobytes(), "vertex_disp": disp.astype(np.float32).tobytes(),
           "vertex_value": size.astype(np.float32).tobytes(), "max_mm": float(size.max(initial=0)),
           "lift_points_print": [_design._to_print_frame(s.mesh, rot, q) for q in spots], "material": p.material["name"],
           "from_gcode": warm is not None}
    if has_brim:
        kind = settings["brim_type"]
        width = float(settings["brim_width"])
        modeled = r["brim_modeled"]
        key = ("warp.mouse_ears" if kind == "brim_ears" else "warp.outer_brim") if modeled else "warp.brim_unresolved"
        message = {"key": key, "params": {"width": width} if modeled else {}}
        out["brim"] = {"type": kind, "width_mm": width, "modeled": modeled,
                       "message": message, "detail": format_message(message)}
    if ranges:
        values = [out["curl_mm"], *(round(curl * factor, 3) for factor in warp.shrink_factors(p))]
        out["range"] = {"curl_mm": [min(values), max(values)]}
    if adhesion is not None:
        out["adhesion"] = {k: v for k, v in adhesion.items() if k != "point"}
        if peel_point is not None:
            out["lift_points_print"][0][2] = 0
            out["adhesion"]["point_print"] = out["lift_points_print"][0]
    vm = warp.residual_stress(r)[r.get("part_elems", slice(None))]
    out["stress_value"] = surface_values(model, vm, fine @ turn.T).astype(np.float32).tobytes()
    out["stress_max"] = round(float(np.percentile(vm, 99)), 2) if len(vm) else 0.0
    out["stress_share"] = round(out["stress_max"] / r["material"].strength_z, 2)
    out["explanation"] = _warp_story(out, p)
    return out


def _warm_layers(up, p: Profile, sliced: dict, climate: dict) -> tuple:
    """(heights, °C) from the layer heat model with the loaded G-code's layer times and chamber air."""
    from ..checks import _layer_heat
    from ..slicer import slice_mesh

    s = _S._require()
    rotation = print_rotation(s.mesh, up, p.printer["bed"], _orient._moving(p))[0]
    layers = slice_mesh(orient(s.mesh, rotation), p.layer_height)
    return np.array([l.z for l in layers]), _layer_heat(layers, p, sliced, climate=climate)


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


def _warp_setup(p: Profile, shells: Shells | None = None) -> tuple:
    return (p.material_id, json.dumps(p.material, sort_keys=True), p.enclosure,
            p.printer.get("chamber_temp_c"), p.printer["speed"], p.goal,
            p.printer.get("bed_surface", "unknown"), p.nozzle, p.layer_height, _strength._shells(p) if shells is None else shells)


def _matching_warp(s: _S.Session, up, p: Profile, shells: Shells | None = None) -> dict | None:
    """A change to the orientation or to inputs that drive shrinkage or grip needs another solve."""
    w = s.warp
    if not w or w.get("setup") != _warp_setup(p, shells):
        return None
    return w if np.dot(w["up"], np.asarray(up, float) / np.linalg.norm(up)) >= 0.999 else None


def _adhesion_finding(s: _S.Session, w: dict, rotation: np.ndarray) -> dict:
    point = _design._print_frame_map(s.mesh, rotation)(w["peel_point"])
    point[2] = 0  # The voxelised footprint can sit a fraction of a cube below the mesh.
    h = w["model"].grid.h
    bbox = [(point - [h, h, 0]).tolist(), (point + [h, h, h]).tolist()]
    adhesion = w["adhesion"]
    metrics = {k: v for k, v in adhesion.items() if k not in ("point", "curls", "message", "detail")}
    message = adhesion.get("message", {"key": "finding.bed_adhesion.one_liner", "params": {}})
    return {"rule": "bed_adhesion", "severity": "warn", "impact": "fails", "bbox": bbox,
            "metrics": metrics, "detail": format_message(message), "message": message}


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
    brim = r.get("brim")
    fix = ""
    if r["verdict"] != "fine":
        fix = (" A warmer bed or an enclosure, glue stick, or rounding the corners of the base can help."
               if brim and brim["modeled"] else " A brim or mouse ears at the marked spots, a warmer bed or an enclosure, glue stick,"
               " or rounding the corners of the base help.")
        fix += " Upper layers shrinking onto the warm bottom cause it."
    if brim:
        head += " " + brim["detail"]
        if r.get("adhesion", {}).get("brim_lifts"):
            head += " " + r["adhesion"]["detail"]
    return head + fix + f" Off the bed it would curl up to {r['max_mm']:.2f} mm." + _stress_story(r)


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
