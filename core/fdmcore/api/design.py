from __future__ import annotations

import numpy as np

from ..mesh import Mesh
from ..orientation import rotation_to_z
from ..placement import print_rotation
from ..profile import Profile, load_data
from . import check as _check
from . import load as _load
from . import orientation as _orient
from . import session as _S
from . import strength as _strength
from . import zones as _zones


# Edits that reshape a designed part: on a sculpt, 45° fills under every bump would change how it looks
# (its overhangs are what supports are for), and dozens of wedges on a curved surface touch each other
# edge on edge (T-489).
CAD_ONLY_EDITS = ("ledge-",)


def design(up, profile: dict | None = None, params: dict | None = None, keep_clear: list[dict] | None = None,
           findings: list[dict] | None = None) -> dict:
    """Detected features (holes, bosses), suggested geometry edits and plain-language CAD ``advice`` for this
    orientation. Each edit has a ``mesh`` (float32 triangles, part coordinates) made of closed ``pieces``
    (triangle counts) to add to the part (``op: "add"``) or cut away (``op: "cut"``), and says what it is for
    (features.link_to_findings): a finding of the check (``fixes``), the user's request, the strength
    check, or none ("other": only an idea). ``advice`` lists every design fix with its exact number, unit and
    point (part coordinates), whether or not an auto-apply edit exists (``edit_id`` or None).
    ``params`` adjusts edit sizes (see features.PARAMS); the values used come back in ``params``.
    ``keep_clear``: boxes (volume_at, kind "keep_clear") that must stay empty: edits adding plastic
    there come back with ``clash``. ``findings``: the current check's findings for this orientation
    (``rule``, ``bbox``, ``title``, ``metrics``); left out, the check runs here."""
    from ..features import PARAMS, design_advice, design_edits, edit_params, link_to_findings
    from ..regions import find_regions

    s = _S._require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    p = Profile.from_dict(profile)
    coarse = s.kind == "organic" and s.mesh.face_count >= _orient.DENSE_ORGANIC_FACES and not s.cad_checks
    check_layers = check_rot = None
    if findings is None:
        findings, check_rot, check_layers = _check._check_for_design(up, profile)
    rotation, _ = print_rotation(s.mesh, up, p.printer["bed"], _orient._moving(p))
    to_print = _print_frame_map(s.mesh, rotation)
    from_print = _part_frame_map(s.mesh, rotation)
    feats = _load._features()
    design_layers = (check_layers if check_layers is not None and np.allclose(check_rot, rotation_to_z(up)) else None)
    edits = design_edits(s.mesh, s.regions, up, p, params, s.tags, *(s.weak_spot or (None, 1.0)), fits=s.fits,
                         gaps=s.gaps, hot_spots=s.hot_spots, fasteners=s.fasteners,
                         strength_verdict=s.strength_verdict, findings=findings, to_print=to_print,
                         features=feats, cad_checks=s.cad_checks, layers=design_layers, coarse=coarse)
    if not s.cad_checks:  # an organic shape (T-408), until the user turns the CAD checks back on
        edits = [e for e in edits if not e["id"].startswith(CAD_ONLY_EDITS)]
    for e in edits:
        if "intent" in e:
            _load._cad_refs(e["intent"])
    edits = link_to_findings(edits, findings, to_print)
    advice = design_advice(s.mesh, edits, findings, p, to_print, from_print, features=feats, up=up)
    return {
        "features": feats,
        "edits": _zones._keep_clear_clash(edits, up, keep_clear),
        "advice": advice,
        "params": edit_params(params),
        "param_ranges": {k: {"min": lo, "max": hi} for k, (_, lo, hi) in PARAMS.items()},
    }


def set_fit(region: int, up, partner_mm: float | None = None, fit: str = "sliding", profile: dict | None = None) -> dict:
    """Say what goes into a round hole (or what a round post goes into): the mating part's diameter
    and the fit ("press", "snug", "sliding", "loose"). Returns the diameter to model and why; design()
    then offers the resize as an edit. ``partner_mm`` None forgets it."""
    from ..features import accepted, fit_size
    from ..regions import find_regions

    s = _S._require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    feats = {f["region"]: f for f in accepted(_load._features())}
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
    from ..features import accepted, fastener_plan
    from ..regions import find_regions

    s = _S._require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    feats = {f["region"]: f for f in accepted(_load._features())}
    if region not in feats or feats[region]["type"] != "hole":
        raise ValueError(f"Face {region} is not a round hole")
    if size is None:
        s.fasteners.pop(region, None)
        return {"region": region}
    plan = fastener_plan(s.mesh, feats[region], size, use, up, Profile.from_dict(profile))
    s.fasteners[region] = {"size": size, "use": use}
    s.fits.pop(region, None)  # one size per hole: the fastener's
    return {"region": region, **plan, "explanation": plan["explanation"] + _coarse_note(region)}


def fastener_suggestions(up, profile: dict | None = None) -> dict:
    """Grouped screw / insert / nut sizing suggestions for round holes near standard metric sizes."""
    from ..features import suggest_fasteners
    from ..regions import find_regions

    s = _S._require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    return {"suggestions": suggest_fasteners(s.mesh, _load._features(), up, Profile.from_dict(profile))}


SAME_SIZE_MM = 0.05  # round features this close in diameter (and of one kind) count as the same, for "apply to all"


def round_features(up, profile: dict | None = None) -> dict:
    """Every round hole and peg the user can set a fit on (the Tolerances step, T-570): ``features`` with region,
    ``kind`` (hole / peg), diameter, axis, centre, length (part coordinates), ``same`` (regions of the same kind and
    size, for "apply to all"), what is set on it (``fit``: partner_mm + fit, or ``fastener``: size + use) and
    ``printed_mm``, the size it comes out at on this printer: the user's measured hole / outer error when they
    measured it (``printed_from``: "measured"), else the typical undersize of small holes ("typical"). Plus
    ``tolerance`` (the fit gaps in use, see tolerance())."""
    from ..features import accepted
    from ..regions import find_regions

    s = _S._require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    p = Profile.from_dict(profile)
    holes_t = load_data("thresholds")["holes"]
    typical = float(np.mean(holes_t["typical_undersize_mm"]))
    hole_err, outer_err = p.material.get("hole_error_mm"), p.material.get("outer_error_mm")
    feats = [f for f in accepted(_load._features()) if f["type"] in ("hole", "boss") and f.get("full_circle")]
    out = []
    for f in feats:
        hole = f["type"] == "hole"
        d = float(f["diameter"])
        if hole and hole_err is not None:
            printed, source = d + float(hole_err), "measured"
        elif not hole and outer_err is not None:
            printed, source = d + float(outer_err), "measured"
        elif hole and d <= holes_t["undersize_up_to_mm"]:
            printed, source = d - typical, "typical"
        else:
            printed, source = d, "typical"
        same = [g["region"] for g in feats if g["type"] == f["type"] and abs(float(g["diameter"]) - d) <= SAME_SIZE_MM]
        out.append({"region": int(f["region"]), "kind": "hole" if hole else "peg", "diameter": round(d, 2),
                    "axis": [float(x) for x in f["axis"]], "center": [float(x) for x in f["center"]], "length": round(float(f["length"]), 2),
                    "same": same, "fit": s.fits.get(f["region"]), "fastener": s.fasteners.get(f["region"]),
                    "printed_mm": round(printed, 2), "printed_from": source})
    return {"features": out, "tolerance": _check.tolerance(profile)}


def _coarse_note(region: int) -> str:
    """A warning to add when the hole / post ``region`` was exported with too few facets (coarse_facets):
    its measured size, and so the advice, is off by about the facets' error."""
    from ..checks import coarse_surfaces

    s = _S._require()
    if s.coarse is None:
        s.coarse = coarse_surfaces(s.mesh)
    pts = s.mesh.triangles[s.regions == region].reshape(-1, 3)
    for c in s.coarse:
        lo, hi = np.array(c["bbox"]) + [[-0.01], [0.01]]
        if len(pts) and np.all((pts >= lo) & (pts <= hi)):
            return (f" This curve was exported with few facets (up to {c['error_mm']:.2f} mm off round), so the size measured here "
                    "can be off by about that much: export a finer STL or use the STEP file.")
    return ""


def split(profile: dict | None = None, joint: str = "pins", purpose: str = "fit", loads: list[dict] | None = None) -> dict:
    """A part too big for the printer, cut flat into pieces that fit (split.py), joined with alignment
    pins or (``joint`` "dovetail") a sliding dovetail: each piece's ``edits`` (cut the rest away, then
    the pin holes) to apply to the part and its print ``rotation``, the pin as triangles
    (``pin.stl_triangles``, float32) and how many, and a plain explanation. Keeps the cut away from the
    weak spot of the last strength check, and uses the snug gap from the fit test print. ``purpose``
    "supports": cut a part that fits so each piece, turned its own best way, needs much less support
    (split.support_cut; each piece's ``rotation`` is then that way); raises when no cut helps enough.
    ``purpose`` "strength" (needs ``loads``, as for strength): cut so each piece prints its own strong way and
    together they hold clearly more (split.strength_cut), the glued face where little load crosses it."""
    from ..split import split_plan

    s = _S._require()
    p = Profile.from_dict(profile)
    gap = (p.material.get("fit_gap_mm") or {}).get("snug")
    if joint not in ("pins", "dovetail"):
        raise ValueError('joint is "pins" or "dovetail"')
    weak = s.weak_spot[0] if s.weak_spot else None
    if purpose == "fit":
        return split_plan(s.mesh, p.printer["bed"], weak, gap, joint)
    if purpose == "strength":
        return _strength_split(s, p, gap, joint, weak, loads)
    if purpose != "supports":
        raise ValueError('purpose is "fit", "supports" or "strength"')
    from ..split import _half, support_cut

    cut = support_cut(s.mesh, _orient.orientation_settings(p), p)
    if cut is None:
        raise ValueError("No straight cut makes the pieces need much less support: print it in one piece")
    plan = split_plan(s.mesh, p.printer["bed"], weak, gap, joint, force=(cut["axis"], [cut["cut"]]))
    for i, (piece, up) in enumerate(zip(plan["pieces"], cut["ups"])):
        piece["rotation"] = print_rotation(_half(s.mesh, cut["axis"], cut["cut"], i == 0), up, p.printer["bed"], _orient._moving(p))[0].tolist()
    note, rest = plan["explanation"].split("Cut ", 1)
    plan["explanation"] = (note + f"Cut in two so each piece prints its own best way with much less support: about "
                           f"{cut['pieces_mm3'] / 1000:.1f} cm³ of support instead of {cut['whole_mm3'] / 1000:.1f} cm³. "
                           + rest.split(" pieces that each fit the bed. ", 1)[-1])
    plan["support_mm3"] = {"whole": round(cut["whole_mm3"]), "pieces": round(cut["pieces_mm3"])}
    return plan


def _strength_split(s: _S.Session, p: Profile, gap, joint: str, weak, loads: list[dict] | None) -> dict:
    """split(purpose="strength"): the cut, each piece turned its strong way, and what it buys (T-347)."""
    from ..split import _half, split_plan, strength_cut

    if not loads:
        raise ValueError("Set where it is held and the load first (Strength analysis)")
    cut = strength_cut(s.mesh, _strength._load_case(loads), p, _strength._shells(p))
    if cut is None:
        raise ValueError("No straight cut makes it clearly stronger: print it in one piece")
    plan = split_plan(s.mesh, p.printer["bed"], weak, gap, joint, force=(cut["axis"], [cut["cut"]]))
    for i, (piece, up) in enumerate(zip(plan["pieces"], cut["ups"])):
        piece["rotation"] = print_rotation(_half(s.mesh, cut["axis"], cut["cut"], i == 0), up, p.printer["bed"], _orient._moving(p))[0].tolist()
    note, rest = plan["explanation"].split("Cut ", 1)
    plan["explanation"] = (note + f"Cut in two so each piece prints with its layers along its own load: together "
                           f"{_strength._format_safety(cut['safety'])} instead of {_strength._format_safety(cut['whole_safety'])} in one piece "
                           f"(rough, from one solve). The glued face sits where little load crosses it "
                           f"({_strength._format_safety(cut['joint_safety'])} there, taking glue as strong as the layers). "
                           + rest.split(" pieces that each fit the bed. ", 1)[-1])
    plan["safety"] = {k: round(float(cut[k]), 2) for k in ("whole_safety", "safety", "joint_safety")}
    return plan


def set_gap(face_a: int, face_b: int, up, partner_mm: float | None = None, fit: str = "sliding", profile: dict | None = None) -> dict:
    """A slot or tab between two flat faces the user picked: their distance and which it is (slot:
    empty between them, tab: plastic between). With ``partner_mm`` (thickness of what goes in, or of
    the slot the tab goes into) and ``fit``: the size to model and why; design() then offers moving both
    faces as an edit. If a warping simulation ran for this orientation, how much the gap closes as the
    part curls is added. ``partner_mm`` None only measures (and forgets a size given before)."""
    from ..features import flat_pair, gap_size
    from ..regions import find_regions

    s = _S._require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    a, b = sorted((int(face_a), int(face_b)))
    pair = flat_pair(s.mesh, s.regions, a, b)
    out = {"faces": [a, b], "kind": pair["kind"], "distance_mm": round(pair["distance_mm"], 3), "plane": pair["plane"]}
    if partner_mm is None:
        s.gaps.pop((a, b), None)
        return out
    closing = _warp_closing_pair(pair, a, b, up)
    size, why = gap_size(pair, float(partner_mm), fit, up, Profile.from_dict(profile), closing)
    s.gaps[(a, b)] = {"partner_mm": float(partner_mm), "fit": fit, "warp_mm": closing}
    return {**out, "suggested_mm": round(size, 2), "explanation": why, "warp_mm": round(closing, 3)}


def _warp_points(up):
    """(model, displacement at points) from the last warping run, if it was for ``up``."""
    s = _S._require()
    w = s.warp
    if w is None or np.dot(w["up"], np.asarray(up, float) / np.linalg.norm(up)) < 0.999:
        return None
    from ..strength import sample_nodes

    return lambda pts: sample_nodes(w["model"], w["u"], np.asarray(pts, float) @ w["turn"].T)


def _warp_closing_pair(pair: dict, a: int, b: int, up) -> float:
    """How much a slot narrows (or a tab thickens) as the part curls, by the warping simulation (mm, ≥ 0)."""
    at = _warp_points(up)
    if at is None:
        return 0.0
    s = _S._require()
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
    s = _S._require()
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
    from ..features import TAGS
    from ..regions import find_regions

    s = _S._require()
    if tag is not None and tag not in TAGS:
        raise ValueError(f"tag must be one of {TAGS}")
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    if tag is None:
        s.tags.pop(int(region), None)
    else:
        s.tags[int(region)] = tag
    return {"features": _load._features()}


HOLE_RULES = {"hole_closes", "small_hole_undersized", "insert_wall_thin", "screw_wall_thin", "sideways_hole_oval", "bridge_sag", "hole_starts_midair"}


def _drop_rejected(findings: list[dict], mesh: Mesh, r: np.ndarray) -> list[dict]:
    """Remove hole findings at features the user said aren't holes (by distance to the feature's axis)."""
    s = _S._require()
    if not any(t == "none" for t in s.tags.values()) or s.regions is None:
        return findings
    from ..features import detect_features

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
    return _print_frame_map(mesh, r)(np.asarray(point, float)).tolist()


def _print_frame_map(mesh: Mesh, r: np.ndarray):
    """Part-frame points (… × 3) -> the print frame of ``orient(mesh, r)``, where check's findings are."""
    pts = mesh.triangles.reshape(-1, 3) @ r.T
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return lambda p: np.asarray(p) @ r.T + shift


def _part_frame_map(mesh: Mesh, r: np.ndarray):
    """Print-frame points (… × 3) -> part coordinates (inverse of ``_print_frame_map``)."""
    pts = mesh.triangles.reshape(-1, 3) @ r.T
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return lambda p: (np.asarray(p) - shift) @ r
