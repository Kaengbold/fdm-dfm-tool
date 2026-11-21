"""Overhang classification and print-orientation search.

An orientation is described by its build direction ``up``: the unit vector in
part coordinates that will point to +Z on the printer. Nothing is rotated while
scoring; heights are just projections onto ``up``.

The initial search uses rough support proxies. Finalists are ranked again with
sliced support weight, bed contact, print time and the existing bed checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .mesh import Mesh
from .profile import Profile

# Per-face classes, shared with the web viewer colour map.
OK, BORDERLINE, NEEDS_SUPPORT, BED = 0, 1, 2, 3


@dataclass
class OrientationSettings:
    max_overhang_deg: float = 50.0  # steeper than this (from vertical) needs support
    borderline_margin_deg: float = 10.0  # within this of the limit = "may droop"
    bed_angle_tol_deg: float = 2.0
    sphere_samples: int = 256
    flat_face_candidates: int = 32
    resting_candidates: int = 8  # sides the part settles on when set down (round parts)
    coarse_faces: int = 20_000  # above this, pre-rank candidates on a sample
    fine_faces: int = 200_000  # finalists are re-scored on a sample this big (the sliced refinement checks them after)
    refine_candidates: int = 10
    # Search proxy weights (dimensionless terms, lower score is better).
    w_support_area: float = 1.0
    w_support_volume: float = 1.0
    w_height: float = 0.15
    w_bed_contact: float = 0.5  # search bonus; sliced finalists get at least 0.25 (see rank_finalists)
    # Hobbyist parts should sit on a real flat face: no/tiny bed contact means brims, rafts or
    # supports just to hold the part, poor first layers and a tilted part that's hard to slice.
    w_small_bed: float = 3.0
    good_bed_fraction: float = 0.15  # bed area / L² at which the small-contact penalty reaches zero
    # Tall on a narrow footprint tips over and wobbles (the tipping check's height / base width limit,
    # thresholds.yaml stability.tip_aspect). Full penalty at twice that: a lying pin then beats a standing one
    # even though it needs a little support.
    w_tipping: float = 1.0
    tip_aspect: float = 4.0
    contact_depth_mm: float = 0.2  # points this close to the bed count as touching it (about the first layer)
    # Risky first layer (the sliced refinement only): per small separate spot on the bed (up to 3),
    # and for a first layer made only of thin strips.
    w_small_spots: float = 0.5
    w_thin_first_layer: float = 1.0
    w_print_time: float = 0.25


@dataclass
class OrientationResult:
    up: np.ndarray
    score: float
    support_area: float  # mm², projected area of faces needing support
    support_volume: float  # mm³, rough proxy
    bed_area: float  # mm²
    height: float  # mm
    reasons: list[str] = field(default_factory=list)
    # Score split into parts (sum == score), so the UI can say why an option ranks lower.
    terms: dict[str, float] = field(default_factory=dict)
    support_grams: float | None = None  # only known after sliced refinement
    support_needed: bool | None = None
    print_time_min: float | None = None  # reuse the finalist estimate for its card and comparison
    bed_rules: tuple[str, ...] = ()  # retain the findings that can override the score
    resting: bool = False  # a side the part settles on (convex-hull face), kept even without a flat face on the bed
    face_penalties: dict[str, float] = field(default_factory=dict)

    def penalize(self, term: str, value: float) -> None:
        self.terms[term] = self.terms.get(term, 0.0) + value
        self.score += value


def classify_faces(mesh: Mesh, up, settings: OrientationSettings | None = None) -> np.ndarray:
    """Per-face class (OK / BORDERLINE / NEEDS_SUPPORT / BED) for one build direction."""
    s = settings or OrientationSettings()
    up = _unit(up)
    normals, _ = mesh.normals_and_areas()
    nz = normals @ up
    z = mesh.triangles @ up  # (M, 3) corner heights
    bed = _bed_mask(nz, z, mesh, s)

    overhang = np.degrees(np.arcsin(np.clip(-nz, 0.0, 1.0)))  # 0 = vertical wall, 90 = ceiling
    classes = np.full(mesh.face_count, OK, dtype=np.uint8)
    classes[overhang > s.max_overhang_deg - s.borderline_margin_deg] = BORDERLINE
    classes[overhang > s.max_overhang_deg] = NEEDS_SUPPORT
    classes[bed] = BED
    return classes


def find_orientations(
    mesh: Mesh, settings: OrientationSettings | None = None, top: int = 5
) -> list[OrientationResult]:
    """Score candidate build directions and return the best ``top`` (distinct) ones."""
    s = settings or OrientationSettings()
    resting = resting_directions(mesh, s.resting_candidates)
    cands = np.vstack([candidate_directions(mesh, s), resting])  # resting last: ties keep their old pick
    if mesh.face_count > s.coarse_faces:
        # Rank all candidates on an area-weighted sample, then re-score the best on the full mesh.
        sample, area_scale = _area_sample(mesh, s.coarse_faces)
        coarse = _score(sample, cands, s, size=_size(mesh), area_scale=area_scale)
        cands = np.array([r.up for r in _with_resting(_best_distinct(coarse, s.refine_candidates), coarse, resting, _size(mesh))])
    if mesh.face_count > s.fine_faces:
        fine, scale = _area_sample(mesh, s.fine_faces)
        scored = _score(fine, cands, s, size=_size(mesh), area_scale=scale)
    else:
        scored = _score(mesh, cands, s)
    picked = _with_resting(_best_distinct(scored, top), scored, resting, _size(mesh))
    for r in picked:
        r.reasons = explain(r, mesh)
    return picked


def _best_distinct(results: list[OrientationResult], n: int, min_angle_deg: float = 10.0) -> list[OrientationResult]:
    """Lowest-score results whose build directions are at least ``min_angle_deg`` apart."""
    cos_lim = np.cos(np.radians(min_angle_deg))
    picked: list[OrientationResult] = []
    for i in np.argsort([r.score for r in results]):
        if all(np.dot(results[i].up, p.up) < cos_lim for p in picked):
            picked.append(results[i])
            if len(picked) == n:
                break
    return picked


def _with_resting(picked: list[OrientationResult], scored: list[OrientationResult],
                  resting: np.ndarray, size: float, flat_share: float = 0.01) -> list[OrientationResult]:
    """For a part with no flat face to lie on (no pick has ``flat_share`` × size² on the bed): mark
    picks that are resting sides, and add the best-scoring resting side if none was picked.

    Such a part scores alike in every direction on bed contact, so less support alone picks it,
    often standing on one tip; the sliced check then decides with the real first layer, so a
    steady side has to reach it (T-486). Parts with a flat face keep the flat-face search as is."""
    if any(r.bed_area >= flat_share * size**2 for r in picked):
        return picked

    def is_resting(r):
        return len(resting) > 0 and bool((resting @ r.up > np.cos(np.radians(0.5))).any())

    for r in picked:
        r.resting = is_resting(r)
    if not any(r.resting for r in picked):
        rest = [r for r in scored if is_resting(r)]
        if rest:
            best = min(rest, key=lambda r: r.score)
            best.resting = True
            picked = [*picked, best]
    return picked


def evaluate(mesh: Mesh, up, settings: OrientationSettings | None = None) -> OrientationResult:
    r = _score(mesh, _unit(up)[None, :], settings or OrientationSettings())[0]
    r.reasons = explain(r, mesh)
    return r


def candidate_directions(mesh: Mesh, s: OrientationSettings) -> np.ndarray:
    """Fibonacci-sphere samples plus "lay this flat face on the bed" directions."""
    sphere = _sphere(s.sphere_samples)

    normals, areas = mesh.normals_and_areas()
    # Group faces by normal rounded to 0.01: one integer key per face sorts far faster than
    # np.unique over rows (1M faces: 1.7 s -> ~0.1 s).
    q = np.round(normals * 100).astype(np.int64) + 100
    uniq, inverse = np.unique((q[:, 0] * 201 + q[:, 1]) * 201 + q[:, 2], return_inverse=True)
    region_area = np.bincount(inverse, weights=areas, minlength=len(uniq))
    best = np.argsort(region_area)[::-1][: s.flat_face_candidates]
    sums = np.zeros((len(uniq), 3))
    np.add.at(sums, inverse, normals)
    flat = np.array([-_unit(sums[k]) for k in best])

    cands = np.vstack([[[0.0, 0.0, 1.0]], flat, sphere])
    return cands / np.linalg.norm(cands, axis=1, keepdims=True)


def _sphere(n: int) -> np.ndarray:
    """``n`` near-evenly spread unit vectors (Fibonacci sphere)."""
    i = np.arange(n) + 0.5
    phi = np.arccos(1 - 2 * i / n)
    theta = np.pi * (1 + 5**0.5) * i
    return np.stack([np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)], axis=1)


def resting_directions(mesh: Mesh, n: int, starts: int = 96, hull_dirs: int = 600,
                       max_points: int = 200_000) -> np.ndarray:
    """Build directions (up, part frame) of the ``n`` convex-hull faces the part most often comes to rest
    on when set down: round parts have no flat face but still sit steady on three bumps.

    The sphere samples almost never hit a hull face exactly, so on a part with no flat face the
    search stood it on one bump tip (T-486). Here the part is "dropped" from ``starts`` directions
    and rolled over its lowest point, then its lowest edge, until the centre of mass is above a
    triangle of three contact points; faces reached from more directions come first. Rolling runs
    on the extreme points in ``hull_dirs`` directions (a few hundred), so no hull library (scipy
    isn't loaded for orienting in the browser) and little time on big meshes.
    """
    points = mesh.triangles.reshape(-1, 3)
    if len(points) > max_points:
        points = points[np.random.default_rng(0).choice(len(points), max_points, replace=False)]
    points = points[_extreme_points(points, _sphere(hull_dirs))].astype(np.float64)
    centre = _centroid(mesh)
    found: list[np.ndarray] = []
    counts: list[int] = []
    for g in _sphere(starts):
        g = _roll(points, centre, g)
        if g is None:
            continue
        for k, known in enumerate(found):
            if np.dot(known, g) > np.cos(np.radians(2.0)):
                counts[k] += 1
                break
        else:
            found.append(g)
            counts.append(1)
    order = np.argsort(counts, kind="stable")[::-1][:n]
    return -np.array([found[k] for k in order]).reshape(-1, 3)


def _extreme_points(points: np.ndarray, dirs: np.ndarray, block: int = 16384) -> np.ndarray:
    """Indices of the points farthest along each of ``dirs`` (hull corners). Float32 in blocks of
    points, directions × points so the arg-max runs along memory: 240k points × 600 directions 0.3 s."""
    pts = np.ascontiguousarray(points.T, dtype=np.float32)
    d = dirs.astype(np.float32)
    best = np.full(len(d), -np.inf, np.float32)
    index = np.zeros(len(d), dtype=np.intp)
    rows = np.arange(len(d))
    for start in range(0, pts.shape[1], block):
        along = d @ pts[:, start:start + block]
        arg = along.argmax(axis=1)
        top = along[rows, arg]
        better = top > best
        best[better], index[better] = top[better], arg[better] + start
    return np.unique(index)


def _roll(points: np.ndarray, centre: np.ndarray, g: np.ndarray, steps: int = 40) -> np.ndarray | None:
    """Gravity direction ``g`` after the hull of ``points`` rolls to rest, or None if it doesn't settle."""
    contact = [int(np.argmax(points @ g))]
    for _ in range(steps):
        p1 = points[contact[0]]
        if len(contact) == 3:
            # Resting if the centre of mass is above the contact triangle, else roll over the edge it's beyond.
            above = centre - np.dot(centre - p1, g) * g
            beyond = None
            for i, j, k in ((0, 1, 2), (1, 2, 0), (2, 0, 1)):
                a, b, c = points[contact[i]], points[contact[j]], points[contact[k]]
                side = np.cross(g, b - a)
                if np.dot(above - a, side) * np.dot(c - a, side) < -1e-9:
                    beyond = [contact[i], contact[j]]
                    break
            if beyond is None:
                return g
            contact = beyond
            p1 = points[contact[0]]
        lever = (centre - p1) - np.dot(centre - p1, g) * g  # from the pivot towards the centre, on the bed
        if len(contact) == 2:
            edge = points[contact[1]] - p1
            length = np.linalg.norm(edge)
            along = np.dot(centre - p1, edge / length)
            if not 0 <= along <= length:  # the centre is past one end: pivot on that end alone
                contact = [contact[0] if along < 0 else contact[1]]
                continue
            lever -= along * edge / length
        size = np.linalg.norm(lever)
        if size < 1e-9:
            return g if len(contact) == 2 else None
        lever /= size
        # Tip g towards the lever until the next point touches the bed.
        rel = points - p1
        down, out = rel @ g, rel @ lever
        ok = out > 1e-9
        ok[contact] = False
        if not ok.any():
            return None
        angle = np.where(ok, np.arctan2(-down, np.where(ok, out, 1.0)), np.inf)
        nxt = int(np.argmin(angle))
        g = np.cos(angle[nxt]) * g + np.sin(angle[nxt]) * lever
        g /= np.linalg.norm(g)
        contact.append(nxt)
    return None


def _centroid(mesh: Mesh) -> np.ndarray:
    """Centre of mass of the solid (signed tetrahedra from the origin); the box centre for open meshes."""
    a, b, c = mesh.triangles[:, 0], mesh.triangles[:, 1], mesh.triangles[:, 2]
    vol = np.einsum("ij,ij->i", a, np.cross(b, c))
    total = vol.sum()
    lo, hi = mesh.bounds()
    if abs(total) < 1e-9 * float(np.prod(np.maximum(hi - lo, 1e-9))):
        return (lo + hi) / 2
    return (vol[:, None] * (a + b + c)).sum(axis=0) / (4 * total)


def rotation_to_z(up) -> np.ndarray:
    """Rotation matrix R with R @ up == +Z (Rodrigues)."""
    a = _unit(up)
    b = np.array([0.0, 0.0, 1.0])
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if np.linalg.norm(v) < 1e-12 or 1 + c < 1e-12:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx / (1 + c)


def explain(r: OrientationResult, mesh: Mesh) -> list[str]:
    """Plain-language reasons for the UI."""
    _, areas = mesh.normals_and_areas()
    reasons = []
    if r.bed_area > 0:
        reasons.append(f"Flat contact with the bed: {r.bed_area:.0f} mm²")
    else:
        reasons.append("No flat face on the bed — needs a brim or supports to stay put")
    if r.support_grams is not None:
        reasons.append(support_summary(r.support_grams, bool(r.support_needed)))
    elif r.support_area < 0.005 * areas.sum():
        reasons.append("Prints without supports")
    else:
        reasons.append(f"Needs supports under about {r.support_area:.0f} mm²")
    reasons.append(f"Print height {r.height:.1f} mm")
    return reasons


def pose_names(mesh: Mesh, results: list[OrientationResult]) -> list[str]:
    """Name poses from their contact and proportions, keeping the offered names distinct."""
    normals, areas = mesh.normals_and_areas()
    keys = np.round(normals * 100).astype(int)
    _, inverse = np.unique(keys, axis=0, return_inverse=True)
    largest = np.bincount(inverse, weights=areas).max()
    names, heights = [], []
    for r in results:
        sizes = np.ptp(mesh.triangles.reshape(-1, 3) @ rotation_to_z(r.up).T, axis=0)
        if np.dot(r.up, [0, 0, 1]) > 0.999999:
            name = "As in your file"
        elif np.dot(r.up, [0, 0, -1]) > 0.999999:
            name = "Upside down"
        elif r.bed_area >= 0.8 * largest and r.bed_area > 0:
            name = "On its big flat side"
        elif sizes[2] > max(sizes[:2]) * 1.1:
            name = "Standing on the short edge"
        else:
            name = "On its side"
        if name in names:  # a different height tells two such poses apart; the same height is the mirror pose
            other = heights[names.index(name)]
            name = f"{name}, {sizes[2]:.0f} mm tall" if abs(other - sizes[2]) >= 1 else f"{name}, turned over"
        heights.append(sizes[2])
        base, n = name, 1
        while name in names:
            n += 1
            name = f"{base} ({n})"
        names.append(name)
    return names


def support_summary(grams: float, needed: bool) -> str:
    """Use sliced support presence even when its weight rounds down to zero."""
    if not needed:
        return "No support"
    if grams < 1:
        return "A little support (under 1 g)"
    return f"About {grams:.0f} g support"


def _area_sample(mesh: Mesh, n: int) -> tuple[Mesh, float]:
    """Sample faces with probability ∝ area, so large flat (bed) faces survive.
    Returns the sample and the factor that scales its area sums back to the full mesh;
    the result is approximate and only used for ranking."""
    _, areas = mesh.normals_and_areas()
    rng = np.random.default_rng(0)
    idx = np.unique(rng.choice(mesh.face_count, size=n, p=areas / areas.sum()))
    return Mesh(mesh.triangles[idx]), float(areas.sum() / areas[idx].sum())


def _size(mesh: Mesh) -> float:
    """Characteristic part length used to make score terms dimensionless.
    Falls back to the bounding box for open or inside-out meshes with ~zero volume."""
    lo, hi = mesh.bounds()
    return max(abs(mesh.volume()) ** (1 / 3), 0.1 * float(np.linalg.norm(hi - lo)), 1e-6)


def _score(
    mesh: Mesh, ups: np.ndarray, s: OrientationSettings, size: float | None = None, area_scale: float = 1.0
) -> list[OrientationResult]:
    normals, areas = mesh.normals_and_areas()
    areas = areas * area_scale
    L = size or _size(mesh)
    chunk = max(1, min(16, int(2e8 / (mesh.face_count * 64))))  # keep temporaries ≲ 200 MB
    sin_lim = np.sin(np.radians(s.max_overhang_deg))
    tol = _bed_tol(mesh)
    results = []
    for start in range(0, len(ups), chunk):
        U = ups[start : start + chunk]  # (K, 3)
        nz = normals @ U.T  # (M, K)
        z = np.einsum("mcj,kj->mck", mesh.triangles, U)  # (M, 3, K)
        zmin = z.min(axis=(0, 1))  # (K,)
        height = z.max(axis=(0, 1)) - zmin
        bed = (nz < -np.cos(np.radians(s.bed_angle_tol_deg))) & (z.max(axis=1) - zmin < tol)
        support = (-nz > sin_lim) & ~bed
        proj = areas[:, None] * np.clip(-nz, 0, None)
        support_area = (proj * support).sum(axis=0)
        centroid_h = z.mean(axis=1) - zmin
        support_vol = (proj * centroid_h * support).sum(axis=0)
        bed_area = (areas[:, None] * bed).sum(axis=0)
        width = _contact_width(mesh.triangles, z, zmin, U, s.contact_depth_mm)

        bed_frac = bed_area / L**2
        terms = {
            "support": s.w_support_area * support_area / L**2 + s.w_support_volume * support_vol / L**3,
            "height": s.w_height * height / L,
            "bed": s.w_small_bed * np.clip(1.0 - bed_frac / s.good_bed_fraction, 0.0, 1.0)
            - s.w_bed_contact * np.minimum(bed_frac, 1.0),
            "stability": stability_term(height, bed_area, s, width),
        }
        score = sum(terms.values())
        for k in range(len(U)):
            results.append(
                OrientationResult(
                    up=U[k].copy(),
                    score=float(score[k]),
                    support_area=float(support_area[k]),
                    support_volume=float(support_vol[k]),
                    bed_area=float(bed_area[k]),
                    height=float(height[k]),
                    terms={name: float(v[k]) for name, v in terms.items()},
                )
            )
    return results


def stability_term(height, bed_area, s: OrientationSettings, width=0.0):
    """Penalty for standing tall on a narrow footprint. The base width is the larger of the contact
    area's (the width of a circle that size) and ``width``, how far apart the touching points are
    (a round part resting on three bumps stands as steady as on a disc that wide)."""
    width = np.maximum(2.0 * np.sqrt(np.maximum(bed_area, 1e-6) / np.pi), width)
    aspect = np.asarray(height) / width
    return s.w_tipping * np.clip((aspect - s.tip_aspect) / s.tip_aspect, 0.0, 1.0)


def _contact_width(tris: np.ndarray, z: np.ndarray, zmin: np.ndarray, ups: np.ndarray, depth: float,
                   angles: int = 12, cap: int = 2000) -> np.ndarray:
    """Narrowest width (mm) of the points within ``depth`` of the bed, per build direction: the
    width of the base the part stands on, as the tipping check measures it on the first layer.
    Taken over ``angles`` directions on the bed (up to 4 % too wide), on at most ``cap`` points."""
    points = tris.reshape(-1, 3)
    widths = np.zeros(len(ups))
    turn = np.linspace(0.0, np.pi, angles, endpoint=False)
    for k, up in enumerate(ups):
        near = np.flatnonzero((z[..., k] - zmin[k]).reshape(-1) < depth)
        if len(near) < 2:
            continue
        if len(near) > cap:
            near = near[:: len(near) // cap + 1]
        a = np.cross(up, [1.0, 0.0, 0.0] if abs(up[0]) < 0.9 else [0.0, 1.0, 0.0])
        a /= np.linalg.norm(a)
        b = np.cross(up, a)
        flat = points[near] @ np.stack([a, b], axis=1)  # (n, 2) on the bed
        along = flat @ np.stack([np.cos(turn), np.sin(turn)])  # (n, angles)
        widths[k] = float((along.max(axis=0) - along.min(axis=0)).min())
    return widths


def rank_finalists(mesh: Mesh, results: list[OrientationResult], placed: dict, settings: OrientationSettings,
                   profile: Profile, support_fill: float,
                   extra: tuple[OrientationResult, ...] = (), layer_budget: float = 3e7) -> list[OrientationResult]:
    """Replace search proxies with the numbers shown to the user, only for finalists.

    Lower is better. Support costs w_support_volume (1 balanced, 1.5 speed, 2.5
    material) times displayed support grams / the grams of a support-filled
    part-sized cube (L is the search's characteristic length; fill is 25%).
    Do not also charge projected overhang area or scars: neither measures support
    weight, and summing areas across sampled layers favoured tall edge poses.
    Height retains w_height (0.15 balanced, 0.4 speed, 0.1 material) times height/L.
    Contact costs w_small_bed=3 times max(1-fraction/0.15, 0), plus a bounded bonus of
    max(w_bed_contact, 0.25)*fraction/(1+fraction), fraction=bed_area/L squared.
    Even PLA benefits from contact; the smooth bonus still distinguishes large
    footprints without letting them overwhelm support, strength or marked faces.

    Time uses the app's existing _estimate, including walls, infill, cooling and
    layer-change overhead, on up to 160 sections like its bed checks; big meshes get fewer
    (faces x sections within layer_budget for all finalists, at least 40) so the browser
    doesn't spend more on ranking than on the search. The bed checks use only the first
    layer, the top and the widest section, and time is only compared between finalists.
    It costs w_print_time=0.25 times minutes / the fastest finalist's minutes.
    Thus a twice-as-long print costs 0.25 extra, not just a taller bounding box.
    Existing penalties stay: stability up to 1, small spots 0.5 each (up to 3),
    thin first-layer share times 1, plus strength and marked-face preferences.

    Fit comes first, then no tipping_risk/small_bed_contact, then the Pareto front
    of displayed support grams, contact and height, then score. Hard guards stop
    a proxy or another preference recommending an avoidably unstable/dominated
    pose. No candidate outside this short list is sliced or checked here.
    """
    from .checks import _bed_findings, _estimate, orient, slice_mesh
    from .progress import report
    from .profile import load_data

    size = _size(mesh)
    support_scale = size**3 * support_fill / 1000 * profile.material["density_g_cm3"]
    thresholds = load_data("thresholds")
    times, risky = {}, {}
    sections = int(np.clip(layer_budget / (mesh.face_count * (len(results) + len(extra))), 40, 160))
    for index, result in enumerate([*results, *extra]):
        report(index / (len(results) + len(extra)), "Checking bed contact and print time")
        layers = slice_mesh(orient(mesh, placed[id(result)][0]), profile.layer_height, sections)
        findings = _bed_findings(layers, profile, thresholds)
        result.bed_rules = tuple(finding["rule"] for finding in findings
                                 if finding["rule"] in {"tipping_risk", "small_bed_contact"})
        risky[id(result)] = bool(result.bed_rules)
        times[id(result)] = _estimate(layers, profile)["print_time_min"]
        result.print_time_min = times[id(result)]
    fastest = min(times[id(result)] for result in results)
    for result in [*results, *extra]:
        fraction = result.bed_area / size**2
        terms = {
            "support": settings.w_support_volume * round(result.support_grams, 1) / support_scale,
            "bed": settings.w_small_bed * float(np.clip(1 - fraction / settings.good_bed_fraction, 0, 1))
            - max(settings.w_bed_contact, 0.25) * fraction / (1 + fraction),
            "time": settings.w_print_time * times[id(result)] / fastest,
        }
        for name, value in terms.items():
            result.penalize(name, value - result.terms.get(name, 0.0))

    def eligibility(result):
        # Tipping over fails the print; a small contact patch only needs a brim. A part with no
        # flat face (a sculpt) always has the small patch, so standing steady decides (T-486).
        return not placed[id(result)][1]["fits"], "tipping_risk" in result.bed_rules, risky[id(result)]

    def dominated(result):
        values = (round(result.support_grams, 1), -result.bed_area, result.height)
        for other in results:
            if eligibility(other) != eligibility(result):
                continue
            alternative = (round(other.support_grams, 1), -other.bed_area, other.height)
            if all(left <= right for left, right in zip(alternative, values)) and any(
                left < right for left, right in zip(alternative, values)
            ):
                return True
        return False

    return sorted(results, key=lambda result: (*eligibility(result), dominated(result), result.score))


def _bed_mask(nz, z, mesh, s):
    tol = _bed_tol(mesh)
    return (nz < -np.cos(np.radians(s.bed_angle_tol_deg))) & (z.max(axis=1) - z.min() < tol)


def _bed_tol(mesh: Mesh) -> float:
    lo, hi = mesh.bounds()
    return 0.01 + 1e-4 * float(np.linalg.norm(hi - lo))


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)
