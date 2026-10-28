"""Overhang classification and print-orientation search.

An orientation is described by its build direction ``up``: the unit vector in
part coordinates that will point to +Z on the printer. Nothing is rotated while
scoring; heights are just projections onto ``up``.

All support numbers are rough proxies: support volume assumes every overhang
needs a column down to the bed, even when it would land on the part itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .mesh import Mesh

# Per-face classes, shared with the web viewer colour map.
OK, BORDERLINE, NEEDS_SUPPORT, BED = 0, 1, 2, 3


@dataclass
class OrientationSettings:
    max_overhang_deg: float = 50.0  # steeper than this (from vertical) needs support
    borderline_margin_deg: float = 10.0  # within this of the limit = "may droop"
    bed_angle_tol_deg: float = 2.0
    sphere_samples: int = 256
    flat_face_candidates: int = 32
    coarse_faces: int = 20_000  # above this, pre-rank candidates on a sample
    refine_candidates: int = 10
    # Score weights (dimensionless terms, lower score is better).
    w_support_area: float = 1.0
    w_support_volume: float = 1.0
    w_height: float = 0.15
    w_bed_contact: float = 0.5
    w_small_bed: float = 1.0
    min_bed_fraction: float = 0.02  # bed area / L² below this = tippy part


@dataclass
class OrientationResult:
    up: np.ndarray
    score: float
    support_area: float  # mm², projected area of faces needing support
    support_volume: float  # mm³, rough proxy
    bed_area: float  # mm²
    height: float  # mm
    reasons: list[str] = field(default_factory=list)


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
    cands = candidate_directions(mesh, s)
    if mesh.face_count > s.coarse_faces:
        # Rank all candidates on an area-weighted sample, then re-score the best on the full mesh.
        sample, area_scale = _area_sample(mesh, s.coarse_faces)
        coarse = _score(sample, cands, s, size=_size(mesh), area_scale=area_scale)
        cands = np.array([r.up for r in _best_distinct(coarse, s.refine_candidates)])
    picked = _best_distinct(_score(mesh, cands, s), top)
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


def evaluate(mesh: Mesh, up, settings: OrientationSettings | None = None) -> OrientationResult:
    r = _score(mesh, _unit(up)[None, :], settings or OrientationSettings())[0]
    r.reasons = explain(r, mesh)
    return r


def candidate_directions(mesh: Mesh, s: OrientationSettings) -> np.ndarray:
    """Fibonacci-sphere samples plus "lay this flat face on the bed" directions."""
    n = s.sphere_samples
    i = np.arange(n) + 0.5
    phi = np.arccos(1 - 2 * i / n)
    theta = np.pi * (1 + 5**0.5) * i
    sphere = np.stack([np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)], axis=1)

    normals, areas = mesh.normals_and_areas()
    keys = np.round(normals, 2)
    uniq, inverse = np.unique(keys, axis=0, return_inverse=True)
    region_area = np.bincount(inverse.ravel(), weights=areas, minlength=len(uniq))
    best = np.argsort(region_area)[::-1][: s.flat_face_candidates]
    flat = np.array([-_unit(normals[inverse.ravel() == k].mean(axis=0)) for k in best])

    cands = np.vstack([[[0.0, 0.0, 1.0]], flat, sphere])
    return cands / np.linalg.norm(cands, axis=1, keepdims=True)


def rotation_to_z(up) -> np.ndarray:
    """Rotation matrix R with R @ up == +Z (Rodrigues)."""
    a = _unit(up)
    b = np.array([0.0, 0.0, 1.0])
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if np.linalg.norm(v) < 1e-12:
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
    if r.support_area < 0.005 * areas.sum():
        reasons.append("Prints without supports")
    else:
        reasons.append(f"Needs supports under about {r.support_area:.0f} mm²")
    reasons.append(f"Print height {r.height:.1f} mm")
    return reasons


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
    results = []
    for start in range(0, len(ups), chunk):
        U = ups[start : start + chunk]  # (K, 3)
        nz = normals @ U.T  # (M, K)
        z = np.einsum("mcj,kj->mck", mesh.triangles, U)  # (M, 3, K)
        zmin = z.min(axis=(0, 1))  # (K,)
        height = z.max(axis=(0, 1)) - zmin
        tol = _bed_tol(mesh)
        bed = (nz < -np.cos(np.radians(s.bed_angle_tol_deg))) & (z.max(axis=1) - zmin < tol)
        support = (-nz > sin_lim) & ~bed
        proj = areas[:, None] * np.clip(-nz, 0, None)
        support_area = (proj * support).sum(axis=0)
        centroid_h = z.mean(axis=1) - zmin
        support_vol = (proj * centroid_h * support).sum(axis=0)
        bed_area = (areas[:, None] * bed).sum(axis=0)

        bed_frac = bed_area / L**2
        score = (
            s.w_support_area * support_area / L**2
            + s.w_support_volume * support_vol / L**3
            + s.w_height * height / L
            - s.w_bed_contact * np.minimum(bed_frac, 1.0)
            + s.w_small_bed * (bed_frac < s.min_bed_fraction)
        )
        for k in range(len(U)):
            results.append(
                OrientationResult(
                    up=U[k].copy(),
                    score=float(score[k]),
                    support_area=float(support_area[k]),
                    support_volume=float(support_vol[k]),
                    bed_area=float(bed_area[k]),
                    height=float(height[k]),
                )
            )
    return results


def _bed_mask(nz, z, mesh, s):
    tol = _bed_tol(mesh)
    return (nz < -np.cos(np.radians(s.bed_angle_tol_deg))) & (z.max(axis=1) - z.min() < tol)


def _bed_tol(mesh: Mesh) -> float:
    lo, hi = mesh.bounds()
    return 0.01 + 1e-4 * float(np.linalg.norm(hi - lo))


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)
