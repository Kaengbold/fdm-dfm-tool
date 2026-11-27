from __future__ import annotations

import numpy as np

from .. import progress
from ..checks import orient, run_checks, slice_mesh
from ..mesh import Mesh
from ..orientation import OrientationResult, OrientationSettings, classify_faces, evaluate, find_orientations, support_summary
from ..placement import print_rotation
from ..profile import Profile
from ..progress import stage
from ..service import required_safety
from . import check as _check
from . import session as _S
from . import strength as _strength


def orientations(profile: dict | None = None, top: int = 5, loads: list[dict] | None = None, faces: dict | None = None,
                 keep: list[float] | None = None) -> dict:
    """Best build directions for the loaded part, plus how it scores as loaded.

    With ``loads``, candidates are also ranked by strength: a weak orientation is
    penalised even when it prints more easily. ``faces`` = {"clean": [region ids],
    "bed": [...], "vertical": [...]}: faces that should stay free of support marks / sit on
    the bed / stand upright (a flat face upright, or a round hole or pin with its axis up).
    ``keep``: a build direction that is always among the options (``kept`` = its index), scored like
    the others: after a design change the part stays the way the user had it on the bed (T-353).
    """
    s = _S._require()
    p = Profile.from_dict(profile)
    settings = orientation_settings(p)
    dense_organic = s.kind == "organic" and s.mesh.face_count >= DENSE_ORGANIC_FACES
    if dense_organic:
        settings.coarse_faces = 10_000  # the shortlisted directions still get the fine mesh score
    progress.report(0, "Scoring directions")
    candidates = find_orientations(s.mesh, settings, top=3 * top)  # extra, some get filtered out below
    as_loaded = evaluate(s.mesh, [0, 0, 1], settings)
    kept = None
    if keep is not None:
        k = np.asarray(keep, float) / np.linalg.norm(keep)
        kept = next((r for r in candidates if np.dot(r.up, k) > 0.999), None)
        if kept is None:
            kept = evaluate(s.mesh, k, settings)
            candidates.append(kept)
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
        from ..strength import cases_safety, verdict

        case = _strength._load_case(loads)
        ups = np.array([r.up for r in candidates] + [as_loaded.up])
        with stage(0.2, 0.95):
            sfs = cases_safety(case, ups, p, _strength._shells(p))
        strongest = float(max(sfs[:-1].max(initial=0.0), 1e-9))
        relative = _strength.STRENGTH_RATIO_WEIGHT.get(p.goal, _strength.STRENGTH_RATIO_WEIGHT["balanced"])
        need = required_safety(p)
        for r, sf in zip([*candidates, as_loaded], sfs):
            sf = float(sf)
            safety[id(r)] = sf
            r.reasons.append(_strength._strength_reason(sf, verdict(sf, need), strongest))
            # Below ×2 it may break; above, a much stronger direction is still worth something
            # (how much depends on the goal), so "strong enough either way" doesn't hide a 2× difference.
            r.penalize("strength", _strength.STRENGTH_WEIGHT * max(0.0, need - sf) + relative * np.log2(strongest / max(sf, 1e-9)))
        candidates.sort(key=lambda r: r.score)
        strongest_option = max(candidates, key=lambda r: safety[id(r)])
    elif p.goal == "strength":
        with stage(0.2, 0.95):
            _rank_by_weak_spots(s.mesh, candidates, p, extra=[as_loaded])

    if marked:
        candidates.sort(key=lambda r: r.score)
    # Hide options that don't sit on any flat face or are far worse than the best — unless
    # nothing better exists (e.g. a sphere). Users shouldn't have to wade through bad choices.
    flat = [r for r in candidates if r.bed_area > 0 or r.resting]
    if flat:
        best = min(r.score for r in flat)
        keep = [r for r in flat if r.score <= best + _strength.MAX_SCORE_GAP] or flat[:1]
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
    if kept is not None and all(r is not kept for r in candidates):  # filtered out as a near-twin or as worse
        candidates.append(kept)

    candidates = candidates[: top + 2]
    if kept is not None and all(r is not kept for r in candidates):
        candidates.append(kept)
    placed = {id(r): _placement(s.mesh, r, p) for r in [*candidates, as_loaded]}
    from ..orientation import rank_finalists

    layer_budget = ORGANIC_REFINE_BUDGET if dense_organic else REFINE_BUDGET
    layer_samples = {} if dense_organic else None
    with stage(0.95 if loads or p.goal == "strength" else 0.2, 1.0):
        with stage(0.0, 0.6):
            _refine(s.mesh, [*candidates, as_loaded], placed, settings, p,
                    layer_budget=layer_budget, layer_samples=layer_samples)
        with stage(0.6, 1.0):
            candidates = rank_finalists(s.mesh, candidates, placed, settings, p, _check.SUPPORT_FILL, extra=(as_loaded,),
                                       layer_budget=layer_budget,
                                       layer_samples=layer_samples)
    candidates = candidates[:top]
    if kept is not None and all(r is not kept for r in candidates):
        candidates.append(kept)

    def pack(r, label):
        out = _pack(s.mesh, r, label, settings, placed[id(r)][0], p, safety.get(id(r)))
        if r.print_time_min is not None:
            out["reasons"] = [*out["reasons"], f"About {r.print_time_min:.0f} min to print"]
        if candidates and r is not candidates[0]:
            best = candidates[0]
            out["why_lower"] = why_lower(r, best, placed[id(r)][1]["fits"], placed[id(best)][1]["fits"])
        return out

    from ..orientation import pose_names

    names = pose_names(s.mesh, candidates)
    return {
        "as_loaded": pack(as_loaded, "As in your file"),
        "orientations": [pack(r, name) for r, name in zip(candidates, names)],
        "kept": next((i for i, r in enumerate(candidates) if r is kept), None),
    }


# Non-numeric penalties only: numeric claims must come from the card values.
WORSE = {
    "stability": "may tip or wobble",
    "first_layer": "has small spots or thin strips on the first layer",
    "weak": "has more weak spots along the layers",
    "strength": "is weaker under your load",
}


WORSE_FACE = {
    "clean": "puts support marks on a face you want clean",
    "bed": "leaves a face you marked off the bed",
    "vertical": "tilts a face you want upright",
}


def why_lower(option: OrientationResult, best: OrientationResult, fits: bool = True, best_fits: bool = True,
              min_gap: float = 0.05) -> str:
    """Compare the card values, never turn an internal score gap into a numeric claim.

    Require both 10% and an absolute gap: 0.5 g support, 1 mm² contact,
    0.5 mm height or 1 minute. Round as the cards do (whole grams/mm²/minutes,
    0.1 mm height); support must also be worse in the API's 0.1 g value.
    Under-1-g support shares one label, so cannot distinguish two such options.
    These small floors keep rounding and slice noise out of the short sentence.
    If no displayed number is meaningfully worse, explain a guard or preference
    penalty instead. Small score differences alone are an honest near tie.
    """
    def grams(result: OrientationResult) -> float:
        value = result.support_grams
        return float(f"{value:.0f}") if value is not None and value >= 1 else 0.0

    def shown(value: float | None, digits: int = 0) -> float | None:
        return None if value is None else float(f"{value:.{digits}f}")

    comparisons = [
        (grams(option), grams(best), 0.5, "needs more support"),
        (-shown(option.bed_area), -shown(best.bed_area), 1.0, "has less contact with the bed"),
        (shown(option.height, 1), shown(best.height, 1), 0.5, "prints taller"),
        (shown(option.print_time_min), shown(best.print_time_min), 1.0, "takes longer to print"),
    ]
    worse = []
    for value, reference, floor, phrase in comparisons:
        if value is None or reference is None:
            continue
        if phrase == "needs more support" and (
            option.support_grams is None or best.support_grams is None
            or round(option.support_grams, 1) - round(best.support_grams, 1)
            <= max(0.5, 0.1 * round(best.support_grams, 1))
        ):
            continue
        if value - reference > max(floor, 0.1 * abs(reference)):
            worse.append(phrase)
    if not worse:
        if not fits and best_fits:
            worse.append("doesn't fit the printer")
        bed_causes = {"tipping_risk": "may tip over", "small_bed_contact": "has too little contact to stay on the bed"}
        worse += [phrase for rule, phrase in bed_causes.items() if rule in option.bed_rules and rule not in best.bed_rules]
        gaps = [(option.terms.get(k, 0.0) - best.terms.get(k, 0.0), phrase) for k, phrase in WORSE.items()
                if k != "stability" or "may tip over" not in worse]
        gaps += [(option.face_penalties.get(k, 0.0) - best.face_penalties.get(k, 0.0), phrase)
                 for k, phrase in WORSE_FACE.items()]
        worse += [phrase for gap, phrase in sorted(gaps, reverse=True) if gap > min_gap]
    if not worse:
        return "About as good: pick whichever suits you"
    return "Ranked lower: " + " and ".join(worse[:2])


CLEAN_FACE_WEIGHT = 3.0  # full penalty when a "clean" face is all supported (a strong preference)


BED_FACE_WEIGHT = 10.0  # a face the user wants on the bed isn't on it (an instruction: beats everything else)


VERTICAL_FACE_WEIGHT = 10.0  # same for a face that should stand upright


VERTICAL_TOL = 0.05  # |normal · up| still counted as upright (~3°)


MARK_KINDS = ("clean", "bed", "vertical")


def _marked_faces(faces: dict | None) -> dict | None:
    """Triangle masks for the user's marked faces (regions), or None if nothing is marked. ``"clean": "visible"``
    stands for our guess of the faces people see (visible_faces), as one mask: ``guessed`` is then True."""
    if not faces or not any(faces.get(k) for k in MARK_KINDS):
        return None
    from ..regions import find_regions

    s = _S._require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    if faces.get("clean") == "visible":
        seen = np.isin(s.regions, visible_faces()["regions"])
        return {"clean": [seen] if seen.any() else [], "guessed": True,
                **{k: [s.regions == r for r in faces.get(k, [])] for k in ("bed", "vertical")}}
    return {kind: [s.regions == r for r in faces.get(kind, [])] for kind in MARK_KINDS}


def visible_faces() -> dict:
    """The faces people will see, guessed (plan §14, decision 10): the outer ones, on the part's convex hull. Hole
    walls, pockets and slots are left out, so support there doesn't count as a mark. The Quality goal starts from
    this guess (``faces={"clean": "visible"}`` in optimize, check and orientations) until the user marks show faces."""
    from ..regions import find_regions, outer_regions

    s = _S._require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    ids = outer_regions(s.mesh, s.regions)
    _, areas = s.mesh.normals_and_areas()
    share = float(areas[np.isin(s.regions, ids)].sum() / max(areas.sum(), 1e-12))
    return {"regions": [int(r) for r in ids], "share": round(share, 3)}


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
    from ..orientation import BED, NEEDS_SUPPORT

    _, areas = mesh.normals_and_areas()
    classes = classify_faces(mesh, r.up, settings)
    penalty, notes = 0.0, []
    for m in marked["clean"]:
        frac = areas[m & (classes == NEEDS_SUPPORT)].sum() / areas[m].sum()
        penalty += CLEAN_FACE_WEIGHT * frac
        r.face_penalties["clean"] = r.face_penalties.get("clean", 0.0) + CLEAN_FACE_WEIGHT * frac
        if frac > 0.05:
            notes.append(f"Supports would touch a face you want clean ({frac:.0%} of it)")
    for m in marked["bed"]:
        frac = areas[m & (classes == BED)].sum() / areas[m].sum()
        penalty += BED_FACE_WEIGHT * (1 - frac)
        r.face_penalties["bed"] = r.face_penalties.get("bed", 0.0) + BED_FACE_WEIGHT * (1 - frac)
        notes.append("The face you picked sits on the bed" if frac > 0.9 else "The face you want on the bed is not on the bed")
    for m in marked["vertical"]:
        tilt = _tilt(mesh, m, r.up)
        cost = VERTICAL_FACE_WEIGHT * min(1.0, max(0.0, tilt - VERTICAL_TOL) / 0.2)
        penalty += cost
        r.face_penalties["vertical"] = r.face_penalties.get("vertical", 0.0) + cost
        notes.append("The face you picked stands upright" if tilt <= VERTICAL_TOL else "The face you want upright is tilted or lying")
    r.penalize("faces", penalty)
    r.reasons += notes


SCAR_WEIGHT = 1.0  # support resting on the part: rough marks on a visible surface, hard to remove


REFINE_BUDGET = 3e7  # faces × layers for all finalists together


DENSE_ORGANIC_FACES = 50_000


# Curved supports are costly to overlay. Use a shorter shared stack for support,
# bed and time estimates on dense sculpts. The subsequent bed check slices afresh.
ORGANIC_REFINE_BUDGET = 6.4e6


def _refine(mesh: Mesh, results: list, placed: dict, settings: OrientationSettings, p: Profile,
            layer_budget: float = REFINE_BUDGET, layer_samples: dict | None = None) -> None:
    """Re-score the finalists with sliced layers: real bed contact, real support volume, and
    support that lands on the part instead of the bed (see refine.py)."""
    from ..orientation import _size, explain, stability_term
    from ..refine import support_stats
    L = _size(mesh)
    # Slicing cost ~ faces × layers: keep the whole refinement within a fixed budget on big parts.
    count = int(np.clip(layer_budget / (mesh.face_count * len(results)), 8, 40))
    for i, r in enumerate(results):
        progress.report(i / len(results), "Checking supports layer by layer")
        oriented = orient(mesh, placed[id(r)][0])
        layers = slice_mesh(oriented, p.layer_height, count)
        if layer_samples is not None:
            layer_samples[id(r)] = layers
        st = support_stats(oriented, p, layers=layers)
        frac = st["bed_area"] / L**2
        new = {
            "support": settings.w_support_area * st["support_area"] / L**2
            + settings.w_support_volume * st["support_volume"] / L**3
            + SCAR_WEIGHT * st["scar_area"] / L**2,
            "bed": settings.w_small_bed * float(np.clip(1.0 - frac / settings.good_bed_fraction, 0.0, 1.0))
            - settings.w_bed_contact * min(frac, 1.0),
            "stability": float(stability_term(r.height, st["bed_area"], settings, st.get("base_width", 0.0))),
            "first_layer": settings.w_small_spots * min(st["small_spots"], 3) + settings.w_thin_first_layer * st["thin_share"],
        }
        for k, v in new.items():
            r.penalize(k, v - r.terms.get(k, 0.0))
        old_bed_reason, old_support_reason = explain(r, mesh)[:2]
        r.support_grams = st["support_volume"] * _check.SUPPORT_FILL / 1000 * p.material["density_g_cm3"]
        r.support_needed = st["support_area"] > 0
        r.bed_area, r.support_volume = st["bed_area"], st["support_volume"]
        new_bed_reason, new_support_reason = explain(r, mesh)[:2]
        replacements = {old_bed_reason: new_bed_reason, old_support_reason: new_support_reason}
        r.reasons = [replacements.get(reason, reason) for reason in r.reasons]
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
    s = _S._require()
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
        "support_g": round(r.support_grams, 1) if r.support_grams is not None else None,
        "support_needed": r.support_needed,
        "print_time_min": r.print_time_min,
        "support_summary": support_summary(r.support_grams, bool(r.support_needed)) if r.support_grams is not None else None,
        "reasons": r.reasons,
        "score_terms": {k: round(v, 3) for k, v in r.terms.items()},
        "face_classes": classify_faces(mesh, r.up, settings).tobytes(),
    }


def _pose_marks(faces: dict | None):
    """For optimize: score candidate orientations against the marked faces, as orientations() does."""
    marked = _marked_faces(faces)
    if not marked:
        return None
    s = _S._require()
    return lambda results, settings: [_face_penalty(s.mesh, r, marked, settings) for r in results]
