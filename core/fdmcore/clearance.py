"""Clearance between the loaded part and a mating part (T-348).

Two parts exported from the same CAD assembly keep their positions, so the gap between them can be
measured directly: points spread over the mating part's surface, each with its exact distance to the
nearest triangle of the part (and on which side of it, so overlap shows as a negative gap). The
closest place is compared with the fit gaps (thresholds ``fits``, or the user's fit test print): the
gaps there are for diameters, so each side gets half.
"""

from __future__ import annotations

import numpy as np

from .mesh import Mesh

SPACING_MM = 0.5  # points on the mating part's surface about this far apart
MAX_POINTS = 200_000
NEIGHBOURS = 40  # nearest surface samples of the part tried per point (their triangles)
FREE_MM = 1.0  # farther apart than this everywhere: the parts don't touch at all


def _surface_points(mesh: Mesh, spacing: float, with_triangle: bool = False):
    """Points on the surface on a grid about ``spacing`` apart along each triangle's two edges from its first
    corner (so a long thin strip gets points all along it, and no part of a triangle is left out); and the
    triangle each lies on (``with_triangle``). Coarser when it would be more than MAX_POINTS."""
    t = mesh.triangles
    if not len(t):
        return (np.zeros((0, 3)), np.zeros(0, int)) if with_triangle else np.zeros((0, 3))
    ab, ac = t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]
    la, lc = np.linalg.norm(ab, axis=1), np.linalg.norm(ac, axis=1)
    for _ in range(8):
        mu = np.maximum(np.ceil(la / spacing), 1).astype(int)
        mv = np.maximum(np.ceil(lc / spacing), 1).astype(int)
        if ((mu + 1) * (mv + 1) // 2 + 1).sum() <= MAX_POINTS:
            break
        spacing *= 1.5
    pts, tri = [], []
    for u, v in np.unique(np.stack([mu, mv], axis=1), axis=0):
        i, j = np.meshgrid(np.arange(u + 1) / u, np.arange(v + 1) / v, indexing="ij")
        keep = i + j <= 1 + 1e-9
        i, j = i[keep], j[keep]
        sel = np.flatnonzero((mu == u) & (mv == v))
        pts.append((t[sel, 0][:, None] + ab[sel][:, None] * i[None, :, None] + ac[sel][:, None] * j[None, :, None]).reshape(-1, 3))
        tri.append(np.repeat(sel, len(i)))
    pts, tri = np.concatenate(pts), np.concatenate(tri)
    return (pts, tri) if with_triangle else pts


def _closest_on_triangles(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Closest point to each ``p`` on triangle (a, b, c) (row by row; Ericson, Real-Time Collision Detection 5.1.5)."""
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = np.einsum("ij,ij->i", ab, ap), np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3, d4 = np.einsum("ij,ij->i", ab, bp), np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5, d6 = np.einsum("ij,ij->i", ab, cp), np.einsum("ij,ij->i", ac, cp)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    denom = np.where(np.abs(va + vb + vc) < 1e-20, 1e-20, va + vb + vc)
    v, w = vb / denom, vc / denom
    out = a + ab * v[:, None] + ac * w[:, None]  # inside the face
    with np.errstate(divide="ignore", invalid="ignore"):
        rules = [
            ((d1 <= 0) & (d2 <= 0), a),
            ((d3 >= 0) & (d4 <= d3), b),
            ((d6 >= 0) & (d5 <= d6), c),
            ((vc <= 0) & (d1 >= 0) & (d3 <= 0), a + ab * np.nan_to_num(d1 / (d1 - d3))[:, None]),
            ((vb <= 0) & (d2 >= 0) & (d6 <= 0), a + ac * np.nan_to_num(d2 / (d2 - d6))[:, None]),
            ((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0),
             b + (c - b) * np.nan_to_num((d4 - d3) / ((d4 - d3) + (d5 - d6)))[:, None]),
        ]
    for mask, q in reversed(rules):  # the first matching rule wins
        out = np.where(mask[:, None], q, out)
    return out


def gaps(part: Mesh, mate: Mesh, spacing: float = SPACING_MM) -> tuple[np.ndarray, np.ndarray]:
    """Points on ``mate`` and their signed distance to ``part`` (negative: inside it, the parts overlap)."""
    from scipy.spatial import cKDTree

    t = part.triangles
    normals, _ = part.normals_and_areas()
    reach = FREE_MM + spacing  # only where the parts come this close does the exact gap matter
    lo, hi = part.triangles.reshape(-1, 3).min(axis=0) - reach, part.triangles.reshape(-1, 3).max(axis=0) + reach
    mlo, mhi = mate.triangles.reshape(-1, 3).min(axis=0) - reach, mate.triangles.reshape(-1, 3).max(axis=0) + reach
    pts = _surface_points(_within(mate, lo, hi), spacing)
    # Candidates: the triangles under the part's own surface points nearest to each point (a triangle's centre
    # can be far from its nearest spot when it is big or long, a grid of points on it isn't).
    near, owner = _surface_points(_within(part, mlo, mhi), spacing / 2, with_triangle=True)
    if not len(pts) or not len(near):
        return np.zeros((0, 3)), np.zeros(0)
    owner = np.flatnonzero(_box_mask(part, mlo, mhi))[owner]
    tree = cKDTree(near)
    rough, _ = tree.query(pts, k=1)
    close = rough < reach
    far_pts, far_d = pts[~close], rough[~close]
    pts = pts[close]
    if not len(pts):
        return far_pts, far_d
    k = min(NEIGHBOURS, len(near))
    _, idx = tree.query(pts, k=k)
    idx = owner[idx.reshape(len(pts), k)]
    dist, side = np.empty((len(pts), k)), np.empty((len(pts), k))
    for j in range(k):
        tri = t[idx[:, j]]
        q = _closest_on_triangles(pts, tri[:, 0], tri[:, 1], tri[:, 2])
        dist[:, j] = np.linalg.norm(pts - q, axis=1)
        side[:, j] = np.einsum("ij,ij->i", pts - q, normals[idx[:, j]])
    best = dist.min(axis=1)
    # Nearest to an edge or corner, several faces are as close: the one the point is most squarely in front of
    # (or behind) tells the side; the others' planes run past it.
    tie = dist <= best[:, None] * 1.001 + 1e-4
    j = np.argmax(np.where(tie, np.abs(side), -1.0), axis=1)
    sign = np.where(side[np.arange(len(pts)), j] < -1e-9, -1.0, 1.0)
    return np.concatenate([pts, far_pts]), np.concatenate([best * sign, far_d])


def _box_mask(mesh: Mesh, lo, hi) -> np.ndarray:
    """Triangles that reach into the box lo..hi."""
    t = mesh.triangles
    return np.all(t.max(axis=1) >= lo, axis=1) & np.all(t.min(axis=1) <= hi, axis=1)


def _within(mesh: Mesh, lo, hi) -> Mesh:
    return Mesh(mesh.triangles[_box_mask(mesh, lo, hi)])


def clearance(part: Mesh, mate: Mesh, fit_gaps: dict) -> dict:
    """The closest place between the two parts and what fit it gives. ``fit_gaps``: diameter gaps per fit
    ({"press", "snug", "sliding", "loose"}); a side's clearance is half of it."""
    pts, d = gaps(part, mate)
    if not len(d):
        return {"gap_mm": None, "at": None, "verdict": "free", "close_area_mm2": 0.0, "per_side_mm": {k: round(v / 2, 3) for k, v in fit_gaps.items()}}
    i = int(np.argmin(d))
    low = float(d[i])
    per_side = {k: v / 2 for k, v in fit_gaps.items()}
    if low < -0.01:
        verdict = "overlap"
    elif low > FREE_MM:
        verdict = "free"
    else:
        verdict = "too tight"
        for name in ("press", "snug", "sliding", "loose"):
            if low >= per_side[name] - 0.01:
                verdict = name
    near = d < (max(low, 0.0) + 0.05)
    return {"gap_mm": round(low, 3), "at": pts[i].round(2).tolist(), "verdict": verdict,
            "close_area_mm2": round(float(near.sum() * SPACING_MM**2), 1), "per_side_mm": {k: round(v, 3) for k, v in per_side.items()}}
