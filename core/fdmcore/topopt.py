"""Topology optimisation (T-291): where the plastic carries the load, for a suggested lighter shape.

SIMP on the strength check's voxel grid (research/topology_optimisation.md): each cube gets a density
between 0 and 1, its stiffness is ρ_min + (1 − ρ_min)·ρ³ of solid plastic, and each round moves plastic
from cubes that barely bend to the ones the load runs through (optimality criteria, Sigmund 2001 /
Andreassen et al. 2011), until only ``keep`` of the part's volume is left. The part's outline is the
design space: it only removes plastic, never adds any, so keep-clear boxes stay clear. Cubes at loads
and held faces always stay. The answer is blocky (one cube per voxel) and meant as a guide for a
redesign in CAD, not a finished part.

Speed: ρ_min 10⁻² (not 10⁻³) keeps the contrast low enough for CG, and every solve starts from the
last one's displacements, so later rounds take a fraction of the first.
"""

from __future__ import annotations

import numpy as np

from .fea import element_stiffness, rotate_stiffness
from .placement import rotation_to_z
from .profile import Profile
from .progress import report
from .strength import MIN_SCALE, LoadCase, _fill, _solve, failure_indices, material_for
from .fea import element_stress

PENALTY = 3.0
RHO_MIN = 1e-2
FILTER_RADIUS = 1.5  # voxels: no struts thinner than about a cube and a half
MOVE = 0.2
SOLID = 0.5  # cubes above this density make the suggested shape
STOP_CHANGE = 0.01
KEEP_RANGE = (0.15, 0.9)


def _filter(model) -> tuple:
    """Density filter weights (sparse, row-normalised) between cubes closer than FILTER_RADIUS."""
    import scipy.sparse as sp
    from scipy.spatial import cKDTree

    pts = model.elems.astype(float)
    pairs = cKDTree(pts).query_pairs(FILTER_RADIUS, output_type="ndarray")
    n = len(pts)
    d = np.linalg.norm(pts[pairs[:, 0]] - pts[pairs[:, 1]], axis=1)
    w = FILTER_RADIUS - d
    rows = np.concatenate([pairs[:, 0], pairs[:, 1], np.arange(n)])
    cols = np.concatenate([pairs[:, 1], pairs[:, 0], np.arange(n)])
    vals = np.concatenate([w, w, np.full(n, FILTER_RADIUS)])
    h = sp.csr_matrix((vals, (rows, cols)), shape=(n, n))
    return h, np.asarray(h.sum(axis=1)).ravel()


def contact_cubes(case: LoadCase) -> tuple[np.ndarray, np.ndarray]:
    """(cubes the load goes in at, cubes the part is held at): the one layer of cubes on those faces, all
    of whose corners carry the load or are held. Not ``case.ignore``: that reaches two more cubes in (for
    the stress check), and on a held plate it is most of the plate — kept whole, it used up the plastic
    allowed and the member between load and support was taken away instead (T-361)."""
    corners = case.model.edof[:, 0::3] // 3  # node of each corner
    loaded = np.abs(case.forces).sum(axis=1) > 0
    held = case.held()
    return loaded[corners].all(axis=1), held[corners].all(axis=1)


def optimise(cases: list[LoadCase], up, profile: Profile, keep: float = 0.4, iterations: int = 40) -> dict:
    """Densities (E,) per cube of ``cases[0].model`` keeping ``keep`` of the volume, stiffest for all
    load cases together (sum of their compliances). Also returns the rounds run, the compliance
    of the full part and the result (lower = stiffer), the cubes that always stay (``passive``), split
    into ``loaded`` and ``held``, and the share of the volume actually kept as the target (``keep_used``:
    more than ``keep`` when the faces that must stay already take most of it)."""
    if not KEEP_RANGE[0] <= keep <= KEEP_RANGE[1]:
        raise ValueError(f"keep must be between {KEEP_RANGE[0]:g} and {KEEP_RANGE[1]:g}")
    model = cases[0].model
    mat = material_for(profile)
    c = rotate_stiffness(mat.stiffness_print_frame(), rotation_to_z(up))
    ke = element_stiffness(c, model.grid.h)
    fill = np.broadcast_to(_fill(model), (len(model.elems),)).astype(float)
    loaded = np.zeros(len(model.elems), dtype=bool)
    held = np.zeros(len(model.elems), dtype=bool)
    for case in cases:  # cubes at loads and held faces carry the load in and out: they stay
        lo_, he_ = contact_cubes(case)
        loaded |= lo_
        held |= he_
    passive = loaded | held
    h, hs = _filter(model)
    # The faces that must stay can take much of the plastic allowed (a whole held plate): leave the
    # rest at least a fifth of what's free, so there is plastic to connect them.
    fixed_part = fill[passive].sum()
    target = max(keep * fill.sum(), fixed_part + 0.2 * (fill.sum() - fixed_part))
    x = np.full(len(fill), keep)
    x[passive] = 1.0
    starts = [None] * len(cases)
    history = []
    for it in range(iterations):
        rho = np.where(passive, 1.0, (h @ x) / hs)
        scale = (RHO_MIN + (1 - RHO_MIN) * rho**PENALTY) * fill
        energy = np.zeros(len(fill))
        compliance = 0.0
        for i, case in enumerate(cases):
            u, _ = _solve(case, c, scale, mat.e_xy, x0=starts[i], rtol=1e-3 if it < iterations // 2 else 1e-4)
            starts[i] = u
            ue = u.ravel()[model.edof]
            e = np.einsum("ij,jk,ik->i", ue, ke, ue)  # strain energy of each cube at full stiffness
            energy += e
            compliance += float((scale * e).sum())
        history.append(compliance)
        dc = -PENALTY * (1 - RHO_MIN) * rho ** (PENALTY - 1) * energy * fill
        dc = h.T @ (dc / hs)  # through the filter back to the design variables
        dv = h.T @ (fill / hs)
        lo, hi = 1e-12, 1e12
        while hi / lo > 1 + 1e-4:  # bisection on the volume's Lagrange multiplier
            mid = np.sqrt(lo * hi)
            new = np.clip(x * np.sqrt(np.maximum(-dc, 0) / (mid * dv)), np.maximum(x - MOVE, 0), np.minimum(x + MOVE, 1))
            new[passive] = 1.0
            if (np.where(passive, 1.0, (h @ new) / hs) * fill).sum() > target:
                lo = mid
            else:
                hi = mid
        change = float(np.abs(new - x).max())
        x = new
        report((it + 1) / iterations, "Finding where the load runs")
        if it >= 5 and change < STOP_CHANGE:
            break
    rho = np.where(passive, 1.0, (h @ x) / hs)
    return {"density": rho, "rounds": len(history), "compliance": history, "passive": passive, "loaded": loaded,
            "held": held, "keep_used": float(target / fill.sum())}


def keep_connected(model, solid: np.ndarray, loaded: np.ndarray, held: np.ndarray) -> np.ndarray:
    """Only pieces of the shape that join a load to a held face: a piece with only one of them carries
    nothing (it hangs free, or the load has nothing to push against). Empty when none does."""
    from scipy.ndimage import label

    grid = np.zeros(model.grid.solid.shape, dtype=bool)
    grid[tuple(model.elems[solid].T)] = True
    islands, _ = label(grid)
    ids = islands[tuple(model.elems.T)]
    wanted = np.intersect1d(ids[solid & loaded], ids[solid & held])
    return solid & np.isin(ids, wanted[wanted > 0])


FACES = np.array([  # per direction (−x, +x, −y, +y, −z, +z): two triangles over the cube's corners (0/1 offsets)
    [[[0, 0, 0], [0, 0, 1], [0, 1, 1]], [[0, 0, 0], [0, 1, 1], [0, 1, 0]]],
    [[[1, 0, 0], [1, 1, 0], [1, 1, 1]], [[1, 0, 0], [1, 1, 1], [1, 0, 1]]],
    [[[0, 0, 0], [1, 0, 0], [1, 0, 1]], [[0, 0, 0], [1, 0, 1], [0, 0, 1]]],
    [[[0, 1, 0], [0, 1, 1], [1, 1, 1]], [[0, 1, 0], [1, 1, 1], [1, 1, 0]]],
    [[[0, 0, 0], [0, 1, 0], [1, 1, 0]], [[0, 0, 0], [1, 1, 0], [1, 0, 0]]],
    [[[0, 0, 1], [1, 0, 1], [1, 1, 1]], [[0, 0, 1], [1, 1, 1], [0, 1, 1]]],
], float)
STEPS = np.array([[-1, 0, 0], [1, 0, 0], [0, -1, 0], [0, 1, 0], [0, 0, -1], [0, 0, 1]])


def voxel_surface(grid, cells: np.ndarray) -> np.ndarray:
    """Closed blocky surface (triangles, mm) around voxel indices ``cells`` (n, 3): the faces between a
    kept cube and an empty one, outward."""
    shape = np.array(grid.solid.shape)
    full = np.zeros(shape + 2, dtype=bool)
    full[tuple((cells + 1).T)] = True
    out = []
    for d in range(6):
        nb = cells + 1 + STEPS[d]
        open_ = ~full[tuple(nb.T)]
        if open_.any():
            out.append((cells[open_][:, None, None, :] + FACES[d][None]).reshape(-1, 3, 3))
    tris = np.concatenate(out) if out else np.zeros((0, 3, 3))
    return grid.origin + tris * grid.h


def safety_solid(case: LoadCase, up, profile: Profile, solid: np.ndarray) -> float:
    """Safety factor of the cubes in ``solid`` printed solid (100 % infill), the others empty: compares
    the suggested shape with the whole part on the same grid."""
    mat = material_for(profile)
    c = rotate_stiffness(mat.stiffness_print_frame(), rotation_to_z(up))
    scale = np.where(solid, 1.0, MIN_SCALE * RHO_MIN) * np.broadcast_to(_fill(case.model), (len(solid),))
    u, _ = _solve(case, c, scale, mat.e_xy)
    stress = element_stress(case.model, c, scale, u)
    fi_layer, fi_plane = failure_indices(stress, np.asarray(up, float), mat, scale)
    fi = np.maximum(fi_layer, fi_plane)
    # Away from the loads and supports, as the strength check does; a shape that is all contact band
    # is judged on what it has.
    worst = fi[solid & ~case.ignore] if (solid & ~case.ignore).any() else fi[solid]
    return float(1 / worst.max()) if len(worst) and worst.max() > 0 else 0.0
