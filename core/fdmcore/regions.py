"""Group triangles into user-meaningful faces ("regions").

Two passes over the triangle adjacency graph:
1. exact planar patches: neighbours with (almost) identical normals;
2. small patches (strips of a tessellated cylinder, fillet, sphere) are merged
   with smooth small neighbours, so a hole wall becomes one region while large
   flat faces stay separate from the fillets around them.

A flat side of an extruded outline is often just two triangles, as "small" as a
fillet strip; without more care one click would select the whole wall around a
rounded outline. So strips only join neighbours of about their own width that
bend the same way (outward or inward): flat sides, round outside corners and
round inside corners each become their own face.
"""

from __future__ import annotations

import numpy as np

from .mesh import Mesh


EDGE_ANGLE_DEG = 30.0  # minimum dihedral angle (deg) for sharp feature edges and inside corners
CORNER_ANGLE_DEG = 45.0  # polyline turn angle (deg) above which a chain splits at the corner
EDGE_SIMPLIFY_MM = 1e-3  # drop interior polyline vertices deviating less than this from the chord
EDGE_BAND_RADIUS = 2.0  # default band half-width (mm) when mapping an edge/corner to strength.py
# Noisy or organic meshes have tens of thousands of tiny sharp edges that nobody picks; keeping them made the
# result huge and slow in Pyodide (300k polylines, ~8 s natively on an 80k-triangle bumpy sphere).
MIN_EDGE_MM = 1.0  # shorter chains aren't offered for picking
MAX_EDGES = 2000  # per kind (feature / inside corner): the longest ones
CYLINDER_MIN_COVERAGE = 0.4  # arc share above which a convex cylinder is "cylinder" rather than a corner fillet


def weld(mesh: Mesh, decimals: int = 5) -> np.ndarray:
    """Vertex index per triangle corner (M, 3)."""
    pts = np.round(mesh.triangles.reshape(-1, 3), decimals)
    if len(pts) == 0:
        return np.empty((0, 3), dtype=np.intp)
    # Sort coordinate columns directly. Sorting NumPy records (unique with
    # axis=0) is much slower in Pyodide for large triangle soups.
    x, y, z = pts.T
    order = np.lexsort((z, y, x))
    sorted_pts = pts[order]
    starts = np.r_[False, (sorted_pts[1:] != sorted_pts[:-1]).any(axis=1)]
    inverse = np.empty(len(pts), dtype=np.intp)
    inverse[order] = np.cumsum(starts)
    return inverse.reshape(-1, 3)


def face_adjacency(faces: np.ndarray) -> np.ndarray:
    """Pairs of triangles sharing an edge (K, 2)."""
    if len(faces) == 0:
        return np.zeros((0, 2), dtype=np.intp)
    edges = np.sort(np.stack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]], axis=1).reshape(-1, 2), axis=1)
    owner = np.repeat(np.arange(len(faces)), 3)
    n_v = np.int64(int(faces.max()) + 1)
    keys = edges[:, 0].astype(np.int64) * n_v + edges[:, 1]
    order = np.argsort(keys, kind="mergesort")
    k, o = keys[order], owner[order]
    same = k[1:] == k[:-1]
    return np.stack([o[:-1][same], o[1:][same]], axis=1)


def find_regions(mesh: Mesh, planar_deg: float = 0.5, smooth_deg: float = 20.0, small_faces: int = 4,
                 width_ratio: float = 3.0, wide_mm: float = 1.0) -> np.ndarray:
    """Region id per triangle. Two smooth strips stay apart when one is more than ``width_ratio``
    times wider and at least ``wide_mm`` wide (a flat side next to fillet strips; the floor keeps a
    thin seam strip in a hole wall), or when one curves outward and the other inward."""
    normals, areas = mesh.normals_and_areas()
    adj = face_adjacency(weld(mesh))
    cos = np.einsum("ij,ij->i", normals[adj[:, 0]], normals[adj[:, 1]])
    n = mesh.face_count

    planar = adj[cos > np.cos(np.radians(planar_deg))]
    _, patch = _components(planar, n)
    size = np.bincount(patch)
    small = size[patch] <= small_faces
    smooth = (cos > np.cos(np.radians(smooth_deg))) & small[adj[:, 0]] & small[adj[:, 1]]

    width = _patch_width(mesh, patch, areas)
    pa, pb = patch[adj[:, 0]], patch[adj[:, 1]]
    wa, wb = width[pa], width[pb]
    same_width = (np.maximum(wa, wb) <= width_ratio * np.minimum(wa, wb)) | (np.maximum(wa, wb) < wide_mm)
    bend = _bend(mesh, normals, adj, smooth & (pa != pb))
    label = np.zeros(len(size))
    np.add.at(label, pa, bend)
    np.add.at(label, pb, bend)
    same_bend = np.sign(label[pa]) == np.sign(label[pb])
    _, region = _components(np.vstack([planar, adj[smooth & same_width & same_bend]]), n)
    return region


def planar_share(mesh: Mesh, planar_deg: float = 0.05, max_faces: int = 150_000) -> float:
    """Share of the surface on triangles that lie in one plane with an edge neighbour. CAD exports are ~1
    (flat faces, cylinder strips of two triangles); scans and sculpts are ~0. The angle is far tighter than
    ``find_regions``' 0.5°: on a finely meshed smooth shape neighbours are within 0.5° of each other, while
    CAD neighbours in one plane differ only by float noise (~0.006° on a 0.1 mm strip). No scipy: it runs
    when a part loads, before the browser fetches scipy. Big meshes are measured on whole grid cells
    picked at random (fixed seed) up to ``max_faces``: neighbours stay together, and a share is a share."""
    if mesh.face_count > max_faces:
        c = mesh.triangles.mean(axis=1)
        lo, size = c.min(axis=0), max(float(np.ptp(c, axis=0).max()), 1e-9)
        cell = np.minimum((8 * (c - lo) / size).astype(np.intp), 7)
        key = (cell[:, 0] * 8 + cell[:, 1]) * 8 + cell[:, 2]
        counts = np.bincount(key, minlength=512)
        order = np.random.default_rng(0).permutation(np.flatnonzero(counts))
        upto = order[: int(np.searchsorted(np.cumsum(counts[order]), max_faces)) + 1]
        mesh = Mesh(mesh.triangles[np.isin(key, upto)])
    normals, areas = mesh.normals_and_areas()
    adj = face_adjacency(weld(mesh))
    cos = np.einsum("ij,ij->i", normals[adj[:, 0]], normals[adj[:, 1]])
    pair = adj[cos > np.cos(np.radians(planar_deg))]  # sharing an edge, so the same direction is the same plane
    shared = np.zeros(mesh.face_count, bool)
    shared[pair.ravel()] = True
    total = areas.sum()
    return float(areas[shared].sum() / total) if total > 0 else 1.0


def _patch_width(mesh: Mesh, patch: np.ndarray, areas: np.ndarray) -> np.ndarray:
    """Rough width of each planar patch: its area over its longest triangle edge (a strip of two
    triangles: area / diagonal, about its short side)."""
    t = mesh.triangles
    longest = np.linalg.norm(t - np.roll(t, 1, axis=1), axis=2).max(axis=1)
    k = int(patch.max()) + 1
    edge = np.zeros(k)
    np.maximum.at(edge, patch, longest)
    return np.bincount(patch, areas, minlength=k) / np.maximum(edge, 1e-12)


def _bend(mesh: Mesh, normals: np.ndarray, adj: np.ndarray, use: np.ndarray) -> np.ndarray:
    """+1 where the surface bends outward across a triangle pair (a round outside corner), -1
    inward (a fillet in an inside corner, a hole wall), 0 for pairs not in ``use``. Going from one
    triangle to the next, the normal turns towards the step on an outward bend."""
    c = mesh.triangles.mean(axis=1)
    a, b = adj[:, 0], adj[:, 1]
    s = np.einsum("ij,ij->i", normals[b] - normals[a], c[b] - c[a])
    return np.where(use, np.sign(s), 0.0)


OUTER_TOL_SHARE = 0.01  # a triangle within 1 % of the part's size (at least OUTER_TOL_MM) of the hull is on the outside
OUTER_TOL_MM = 0.3
OUTER_REGION_SHARE = 0.5  # a face counts as outer when at least half its area is


def outer_regions(mesh: Mesh, region: np.ndarray, chunk: int = 4096) -> np.ndarray:
    """Faces people see, as a first guess for the Quality goal (plan §14, decision 10): the ones lying on the part's
    convex hull. Hole walls, pockets and the insides of slots are off the hull, so support marks there are hidden.
    Returns the sorted region ids."""
    from scipy.spatial import ConvexHull, QhullError

    pts = mesh.triangles.reshape(-1, 3)
    try:
        hull = ConvexHull(np.unique(np.round(pts, 4), axis=0))
    except (QhullError, ValueError):  # flat or degenerate: every face is on the outside
        return np.unique(region)
    eq = hull.equations  # n·x + d <= 0 inside, n pointing out
    tol = max(OUTER_TOL_MM, OUTER_TOL_SHARE * float(np.linalg.norm(np.ptp(pts, axis=0))))
    centres = mesh.triangles.mean(axis=1)
    outer = np.empty(len(centres), bool)
    for i in range(0, len(centres), chunk):
        dist = (centres[i:i + chunk] @ eq[:, :3].T + eq[:, 3]).max(axis=1)  # ≈ minus the distance to the hull
        outer[i:i + chunk] = dist > -tol
    _, areas = mesh.normals_and_areas()
    n = int(region.max()) + 1 if len(region) else 0
    on = np.bincount(region, weights=areas * outer, minlength=n)
    total = np.bincount(region, weights=areas, minlength=n)
    return np.flatnonzero(on >= OUTER_REGION_SHARE * np.maximum(total, 1e-12))


def region_info(mesh: Mesh, region: np.ndarray, features: list[dict] | None = None) -> list[dict]:
    """Area, mean normal, centroid and kind per region (bincount sums: CAD exports have
    thousands of regions, and a mask per region would scan every triangle each time).

    Without ``features``, ``kind`` is ``"flat"`` or ``"curved"``. When detected ``features``
    (from ``features.detect_features``) are passed, curved regions are further classified into
    ``"hole"`` (with ``hole_diameter``), ``"cylinder"`` (with ``diameter``) or ``"curved"``."""
    normals, areas = mesh.normals_and_areas()
    centroids = mesh.triangles.mean(axis=1)
    k = int(region.max()) + 1 if len(region) else 0
    area = np.bincount(region, areas, minlength=k)
    n = np.stack([np.bincount(region, normals[:, d] * areas, minlength=k) for d in range(3)], axis=1) if k else np.zeros((0, 3))
    c = np.stack([np.bincount(region, centroids[:, d] * areas, minlength=k) for d in range(3)], axis=1) if k else np.zeros((0, 3))
    length = np.linalg.norm(n, axis=1)
    flat = length / np.maximum(area, 1e-12) > 0.999  # 1 = planar
    normal = n / np.maximum(length, 1e-12)[:, None]
    centroid = c / np.maximum(area, 1e-12)[:, None]
    info = [{"id": r, "area_mm2": float(area[r]), "normal": normal[r].tolist(), "centroid": centroid[r].tolist(),
             "kind": "flat" if flat[r] else "curved"} for r in range(k)]
    if features:
        for f in features:
            if f.get("tag") == "none":
                continue
            r = int(f["region"])
            if not (0 <= r < k) or info[r]["kind"] == "flat":
                continue
            if f["type"] == "hole":
                info[r]["kind"] = "hole"
                info[r]["hole_diameter"] = float(f["diameter"])
            elif f["type"] == "boss" and (
                f.get("tag") == "boss" or f.get("full_circle") or f.get("coverage", 1.0) > CYLINDER_MIN_COVERAGE
            ):
                info[r]["kind"] = "cylinder"
                info[r]["diameter"] = float(f["diameter"])
    return info


def sharp_edge_segments(mesh: Mesh, min_angle_deg: float = EDGE_ANGLE_DEG):
    """Vectorised sharp edge segments shared by two triangles meeting at >= ``min_angle_deg``.
    Returns ``(verts, va, vb, f1, f2, n1, n2, is_concave)`` where ``is_concave`` marks inside
    corners (second face centroid in front of the first face's plane, matching ``features.concave_edges``)."""
    tris = mesh.triangles
    if len(tris) == 0:
        z0 = np.zeros((0, 3), dtype=np.float64)
        zi = np.zeros(0, dtype=np.intp)
        return z0, zi, zi, zi, zi, z0, z0, np.zeros(0, dtype=bool)
    normals, areas = mesh.normals_and_areas()
    verts, inv = np.unique(np.round(tris.reshape(-1, 3), 4), axis=0, return_inverse=True)
    ids = inv.reshape(-1, 3)
    # Pair faces sharing an edge using a single int64 key (much faster than 2D lexsort on 100k+ tris).
    # Stable sort preserves face order so 1st/2nd pair up, then 3rd/4th on non-manifold edges.
    edges = np.sort(np.stack([ids[:, [0, 1]], ids[:, [1, 2]], ids[:, [2, 0]]], axis=1).reshape(-1, 2), axis=1)
    n_v = np.int64(len(verts))
    keys = edges[:, 0].astype(np.int64) * n_v + edges[:, 1]
    order = np.argsort(keys, kind="mergesort")
    ek = keys[order]
    pos = np.arange(len(ek))
    new = np.empty(len(ek), dtype=bool)
    new[0] = True
    new[1:] = ek[1:] != ek[:-1]
    run_start = np.maximum.accumulate(np.where(new, pos, 0))
    k = np.flatnonzero(~new & ((pos - run_start) % 2 == 1))
    second, first = order[k], order[k - 1]
    by_second = np.argsort(second)  # keep deterministic face-by-face completion order
    second, first = second[by_second], first[by_second]
    f1, f2 = first // 3, second // 3
    va, vb = edges[second, 0], edges[second, 1]

    centroids = tris.mean(axis=1)
    n1, n2 = normals[f1], normals[f2]
    pa, pb = verts[va], verts[vb]
    seg_len = np.linalg.norm(pb - pa, axis=1)
    sharp = (
        (va != vb)
        & (seg_len > 1e-4)
        & (areas[f1] > 1e-12)
        & (areas[f2] > 1e-12)
        & (np.einsum("ij,ij->i", n1, n2) <= np.cos(np.radians(min_angle_deg)))
    )
    va, vb, f1, f2 = va[sharp], vb[sharp], f1[sharp], f2[sharp]
    n1, n2, pa = n1[sharp], n2[sharp], pa[sharp]
    is_concave = np.einsum("ij,ij->i", centroids[f2] - pa, n1) > 1e-6
    return verts, va, vb, f1, f2, n1, n2, is_concave


def _rdp_open(pts: np.ndarray, eps: float) -> np.ndarray:
    """Iterative Ramer-Douglas-Peucker simplification for an open (N, 3) polyline."""
    n = len(pts)
    if n <= 2 or eps <= 0:
        return pts
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    eps2 = eps * eps
    while stack:
        lo, hi = stack.pop()
        if hi - lo <= 1:
            continue
        a = pts[lo]
        ab = pts[hi] - a
        ab2 = float(np.dot(ab, ab))
        sub = pts[lo + 1 : hi] - a
        if ab2 <= 1e-18:
            d2 = np.einsum("ij,ij->i", sub, sub)
        else:
            t = np.clip((sub @ ab) / ab2, 0.0, 1.0)
            proj = sub - t[:, None] * ab
            d2 = np.einsum("ij,ij->i", proj, proj)
        rel = int(np.argmax(d2))
        if d2[rel] > eps2:
            mid = lo + 1 + rel
            keep[mid] = True
            stack.append((lo, mid))
            stack.append((mid, hi))
    return pts[keep]


def _simplify_polyline(pts: np.ndarray, eps: float, closed: bool) -> np.ndarray:
    """Simplify collinear runs without collapsing closed loops."""
    if len(pts) <= 2 or eps <= 0:
        return pts
    if closed and len(pts) >= 4:
        # Split closed loop at its half-index so start != end in each RDP pass.
        mid = len(pts) // 2
        a = _rdp_open(pts[: mid + 1], eps)
        b = _rdp_open(pts[mid:], eps)
        out = np.vstack([a[:-1], b])
        return out if len(out) >= 4 else pts
    return _rdp_open(pts, eps)


def _empty_polylines() -> dict:
    return {"points": b"", "offsets": np.zeros(1, dtype="<u4").tobytes(), "meta": []}


def _chain_polylines(verts: np.ndarray, va: np.ndarray, vb: np.ndarray, f1: np.ndarray, f2: np.ndarray,
                     n1: np.ndarray, n2: np.ndarray, region: np.ndarray | None,
                     corner_deg: float, simplify_mm: float) -> dict:
    """Chain raw undirected sharp segments into smooth polylines (breaking at junctions and turns
    above ``corner_deg``) and pack them compactly as float32 ``points`` + uint32 ``offsets`` + ``meta``."""
    if len(va) == 0:
        return _empty_polylines()
    # Deduplicate undirected edges (non-manifold edges can appear twice).
    n_v = np.int64(len(verts))
    ekeys = np.minimum(va, vb).astype(np.int64) * n_v + np.maximum(va, vb)
    _, keep_idx = np.unique(ekeys, return_index=True)
    keep_idx.sort()
    va, vb = va[keep_idx], vb[keep_idx]
    f1, f2 = f1[keep_idx], f2[keep_idx]
    n1, n2 = n1[keep_idx], n2[keep_idx]

    n_seg = len(va)
    seg_vec = verts[vb] - verts[va]
    seg_len = np.linalg.norm(seg_vec, axis=1)
    seg_dir = seg_vec / np.maximum(seg_len, 1e-12)[:, None]

    # Build incident-segment lists only for vertices participating in sharp edges.
    adj: dict[int, list[int]] = {}
    va_list, vb_list = va.tolist(), vb.tolist()
    for s in range(n_seg):
        u, w = va_list[s], vb_list[s]
        adj.setdefault(u, []).append(s)
        adj.setdefault(w, []).append(s)

    cos_corner = float(np.cos(np.radians(corner_deg)))
    smooth_v: set[int] = set()
    for v, inc in adj.items():
        if len(inc) != 2:
            continue
        s0, s1 = inc
        # Direction into v along s0, and out of v along s1.
        d_in = seg_dir[s0] if vb_list[s0] == v else -seg_dir[s0]
        d_out = seg_dir[s1] if va_list[s1] == v else -seg_dir[s1]
        if float(np.dot(d_in, d_out)) >= cos_corner:
            smooth_v.add(v)

    used = [False] * n_seg
    chains: list[tuple[list[int], list[int], bool]] = []

    def walk(start_v: int, first_seg: int, is_loop: bool):
        v_seq = [start_v]
        s_seq = []
        curr_v = start_v
        curr_s = first_seg
        while True:
            used[curr_s] = True
            s_seq.append(curr_s)
            nxt = vb_list[curr_s] if va_list[curr_s] == curr_v else va_list[curr_s]
            v_seq.append(nxt)
            if nxt == start_v:
                return v_seq, s_seq, True
            if nxt not in smooth_v:
                return v_seq, s_seq, False
            inc = adj[nxt]
            other = inc[0] if inc[1] == curr_s else inc[1]
            if used[other]:
                return v_seq, s_seq, is_loop and (nxt == start_v)
            curr_v, curr_s = nxt, other

    # Pass 1: open chains starting at non-smooth endpoints (junctions, dead ends, sharp corners).
    for v in sorted(adj):
        if v in smooth_v:
            continue
        for s in adj[v]:
            if not used[s]:
                chains.append(walk(v, s, False))

    # Pass 2: closed smooth loops (all vertices have degree 2 and smooth turns, e.g. hole rims).
    for s in range(n_seg):
        if not used[s]:
            chains.append(walk(va_list[s], s, True))

    pts_chunks: list[np.ndarray] = []
    offsets = [0]
    meta: list[dict] = []
    reg_f1 = region[f1].tolist() if region is not None else None
    reg_f2 = region[f2].tolist() if region is not None else None
    n_sum = n1 + n2

    # Only chains worth picking, longest first (cheap: plain sums before any per-chain numpy work).
    seg_len_list = seg_len.tolist()
    totals = [sum(seg_len_list[i] for i in s_seq) for _, s_seq, _ in chains]
    keep = sorted((i for i, t in enumerate(totals) if t >= MIN_EDGE_MM), key=lambda i: -totals[i])[:MAX_EDGES]
    chains = [chains[i] for i in sorted(keep)]  # back in walk order, so ids stay stable for a given mesh

    for v_seq, s_seq, closed in chains:
        raw = verts[v_seq]
        lens = seg_len[s_seq]
        total = float(lens.sum())
        cum = np.concatenate([[0.0], np.cumsum(lens)])
        mid = [round(float(np.interp(0.5 * total, cum, raw[:, d])), 4) for d in range(3)]
        n_vec = (lens[:, None] * n_sum[s_seq]).sum(axis=0)
        n_norm = float(np.linalg.norm(n_vec))
        normal = (n_vec / n_norm).tolist() if n_norm > 1e-9 else [0.0, 0.0, 0.0]

        if reg_f1 is not None and reg_f2 is not None:
            pair_len: dict[tuple[int, int], float] = {}
            for s, L in zip(s_seq, lens.tolist()):
                r_a, r_b = reg_f1[s], reg_f2[s]
                pair = (r_a, r_b) if r_a <= r_b else (r_b, r_a)
                pair_len[pair] = pair_len.get(pair, 0.0) + L
            dom = max(pair_len.items(), key=lambda kv: kv[1])[0]
            reg_pair = [int(dom[0]), int(dom[1])]
        else:
            reg_pair = []

        simp = _simplify_polyline(raw, simplify_mm, closed)
        pts_chunks.append(simp.astype("<f4"))
        offsets.append(offsets[-1] + len(simp))
        meta.append({
            "id": len(meta),
            "closed": bool(closed),
            "length_mm": round(total, 3),
            "midpoint": mid,
            "normal": [round(float(x), 4) for x in normal],
            "regions": reg_pair,
        })

    if not pts_chunks:
        return _empty_polylines()
    return {
        "points": np.vstack(pts_chunks).tobytes(),
        "offsets": np.asarray(offsets, dtype="<u4").tobytes(),
        "meta": meta,
    }


def extract_edges(mesh: Mesh, region: np.ndarray | None = None, min_angle_deg: float = EDGE_ANGLE_DEG,
                  corner_deg: float = CORNER_ANGLE_DEG, simplify_mm: float = EDGE_SIMPLIFY_MM) -> tuple[dict, dict]:
    """Sharp outer feature-edge polylines and concave inside-corner polylines for 3D picking.
    Returns ``(feature_edges, concave_edges)``, each packed as ``{points, offsets, meta}``."""
    verts, va, vb, f1, f2, n1, n2, is_concave = sharp_edge_segments(mesh, min_angle_deg)
    if len(va) == 0:
        return _empty_polylines(), _empty_polylines()
    conv = ~is_concave
    feature = _chain_polylines(verts, va[conv], vb[conv], f1[conv], f2[conv], n1[conv], n2[conv],
                               region, corner_deg, simplify_mm)
    concave = _chain_polylines(verts, va[is_concave], vb[is_concave], f1[is_concave], f2[is_concave],
                               n1[is_concave], n2[is_concave], region, corner_deg, simplify_mm)
    return feature, concave


def unpack_polylines(packed: dict) -> list[np.ndarray]:
    """Unpack ``{points, offsets, meta}`` back into a list of ``(N_i, 3)`` float64 arrays."""
    offsets = np.frombuffer(packed["offsets"], dtype="<u4")
    if len(offsets) <= 1 or not packed["points"]:
        return []
    pts = np.frombuffer(packed["points"], dtype="<f4").reshape(-1, 3).astype(np.float64)
    return [pts[int(offsets[i]) : int(offsets[i + 1])] for i in range(len(offsets) - 1)]


def sample_polyline(pts: np.ndarray, spacing: float, max_points: int = 256) -> np.ndarray:
    """Resample an ``(N, 3)`` polyline along arc length so consecutive points are at most
    ``spacing`` mm apart (capped at ``max_points``). Avoids duplicating the seam on closed loops."""
    pts = np.asarray(pts, dtype=float).reshape(-1, 3)
    if len(pts) <= 1:
        return pts.copy()
    lens = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    total = float(lens.sum())
    if total <= 1e-9:
        return pts[:1].copy()
    cum = np.concatenate([[0.0], np.cumsum(lens)])
    closed = bool(len(pts) >= 3 and np.linalg.norm(pts[-1] - pts[0]) < 1e-5)
    step = max(float(spacing), total / max(max_points - 1, 1))
    if closed:
        n_pts = max(3, min(max_points, int(np.ceil(total / step))))
        s = np.linspace(0.0, total, n_pts, endpoint=False)
    else:
        n_pts = max(2, min(max_points, int(np.ceil(total / step)) + 1))
        s = np.linspace(0.0, total, n_pts)
    return np.column_stack([np.interp(s, cum, pts[:, d]) for d in range(3)])


def edge_load(polyline: np.ndarray, kind: str = "fixed", radius: float = EDGE_BAND_RADIUS, **extra) -> dict:
    """Map an edge or inside-corner polyline onto a ``strength.py`` load/hold dict using a
    continuous ``points`` + ``radius`` band (sample spacing <= 0.75 * radius)."""
    r = float(radius)
    if r <= 0:
        raise ValueError("radius must be positive")
    pts = sample_polyline(polyline, spacing=0.75 * r)
    return {"points": pts.tolist(), "radius": r, "kind": kind, **extra}


def _components(pairs: np.ndarray, n: int):
    from scipy.sparse import coo_matrix  # here: weld / face_adjacency are used where scipy isn't loaded (checks)
    from scipy.sparse.csgraph import connected_components

    g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    return connected_components(g, directed=False)
