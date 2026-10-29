"""Strength check for a printed part under user-defined loads.

Loads are given on regions (see regions.py), or around a point on the surface (an edge, a
corner, a spot on a big face — part coordinates, ``radius`` mm, default 3):
  {"region": 3, "kind": "fixed"}
  {"region": 7, "kind": "force", "kg": 5, "direction": "push" | "pull" | "down" | [x, y, z], "case": 2}
  {"point": [10, 0, 5], "radius": 2, "kind": "force", "kg": 1}
``down`` means -Z in the part's own (as-modelled) coordinates, like a hanging weight.
Forces with different ``case`` numbers are separate situations (e.g. "hanging" and "being
pulled sideways"); fixed faces hold in every case and the weakest case decides.

The model: voxels with an anisotropic material whose weak axis is the build
direction. Within a layer it is the same in every direction (line directions are left to
the detailed, G-code based level). How the slicer fills the part (``Shells``) is modelled by
depth: walls on the sides (walls × line width), solid top and bottom layers on surfaces facing
up and down (layers × layer height), and the much softer infill inside.
Results are indications — "fine / borderline / likely to break" and why.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .fea import Material, Model, build_model, element_stress, rotate_stiffness, solve
from .mesh import Mesh
from .orientation import rotation_to_z
from .profile import Profile
from .voxel import VoxelGrid, depth, voxelize

G = 9.81
POINT_RADIUS = 3.0  # mm around a picked point that carries the load
E_Z_RATIO = 0.85  # layer-direction stiffness vs. in-plane (stiffness drops far less than strength)
INFILL_EXPONENT = 1.5  # Gibson–Ashby-style scaling for stiffness and strength of sparse infill


@dataclass(frozen=True)
class Shells:
    """How the slicer fills the part: wall lines, solid top/bottom layers, infill fraction."""

    walls: int = 2
    infill: float = 0.15
    top: int = 4
    bottom: int = 3


@dataclass
class LoadCase:
    model: Model
    forces: np.ndarray  # (n_nodes, 3) N
    fixed: np.ndarray  # (n_nodes,) bool
    ignore: np.ndarray  # (E,) bool — elements at load/support points (stress singularities)
    number: int = 1  # load case number (see module docstring)


def prepare_cases(mesh: Mesh, region: np.ndarray, loads: list[dict], target_voxels: int = 12_000) -> list[LoadCase]:
    """One LoadCase per case number, all on the same voxel model."""
    fixed = [l for l in loads if l["kind"] == "fixed"]
    forces = [l for l in loads if l["kind"] != "fixed"]
    numbers = sorted({int(l.get("case", 1)) for l in forces}) or [1]
    model = build_model(voxelize(mesh, target_voxels))
    out = []
    for n in numbers:
        case = prepare(mesh, region, fixed + [l for l in forces if int(l.get("case", 1)) == n], model=model)
        case.number = n
        out.append(case)
    return out


def prepare(mesh: Mesh, region: np.ndarray, loads: list[dict], target_voxels: int = 12_000, model: Model | None = None) -> LoadCase:
    model = model or build_model(voxelize(mesh, target_voxels))
    grid = model.grid
    normals, _ = mesh.normals_and_areas()
    forces = np.zeros((model.n_nodes, 3))
    fixed = np.zeros(model.n_nodes, dtype=bool)
    elem_id = np.full(grid.solid.shape, -1)
    elem_id[tuple(model.elems.T)] = np.arange(len(model.elems))
    ignore = np.zeros(len(model.elems), dtype=bool)
    surface = None

    for load in loads:
        sel = _load_faces(mesh, region, load, grid.h)
        if "point" in load:  # outer voxels near the spot (a big triangle reaches far past it)
            if surface is None:
                surface = depth(grid.solid)[tuple(model.elems.T)] == 1
            near = np.linalg.norm(grid.centers(model.elems) - load["point"], axis=1) <= _radius(load, grid.h) + 0.5 * grid.h
            elems = np.flatnonzero(near & surface)
        else:
            elems = _surface_elements(mesh.triangles[sel], normals[sel], grid, elem_id)
        if len(elems) == 0:
            raise ValueError(f"{_where(load)} is too small for the analysis grid")
        ignore[elems] = True
        nodes = np.unique(_element_nodes(model, elems))
        if load["kind"] == "fixed":
            fixed[nodes] = True
        else:
            n = (normals[sel] * mesh.normals_and_areas()[1][sel, None]).sum(axis=0)
            n /= np.linalg.norm(n)
            d = load.get("direction", "push")
            vec = {"push": -n, "pull": n, "down": np.array([0.0, 0.0, -1.0])}.get(d) if isinstance(d, str) else np.asarray(d, float)
            vec = vec / np.linalg.norm(vec)
            forces[nodes] += vec * float(load["kg"]) * G / len(nodes)
    if not fixed.any():
        raise ValueError("Mark at least one face as fixed (where the part is held)")
    if not forces.any():
        raise ValueError("Add at least one load")
    loose = _loose_nodes(model, fixed)
    if loose.any():
        if not np.abs(forces[~loose]).sum():
            raise ValueError("The load isn't connected to a held face (at the analysis resolution, thin links are lost)")
        fixed |= loose
        forces[loose] = 0.0
    return LoadCase(model, forces, fixed, _grow(ignore, model, elem_id))


def _loose_nodes(model: Model, fixed: np.ndarray) -> np.ndarray:
    """Nodes of voxel islands with no held node: pieces that voxelising cut off (or a part made of
    several bodies). Left free they make the stiffness matrix singular; holding them changes nothing
    that is connected to the support."""
    from scipy.ndimage import label

    islands, _ = label(model.grid.solid)  # face-connected voxels
    elem_island = islands[tuple(model.elems.T)]
    nodes = _element_nodes(model, np.arange(len(model.elems)))  # (E, 8)
    held = np.unique(elem_island[fixed[nodes].any(axis=1)])
    anchored = np.isin(elem_island, held)
    loose = np.ones(model.n_nodes, dtype=bool)
    loose[nodes[anchored].ravel()] = False
    return loose


def _load_faces(mesh: Mesh, region: np.ndarray, load: dict, h: float) -> np.ndarray:
    """Triangles a load acts on: its region, or the surface within ``radius`` of its point
    (at least ~1.5 voxels, so a small spot still reaches the grid)."""
    if "point" in load:
        p = np.asarray(load["point"], float)
        r = _radius(load, h)
        sel = _distance_to_triangles(mesh.triangles, p) <= r
        if not sel.any():
            raise ValueError(f"No surface within {r:.1f} mm of point {p.round(1).tolist()}")
        return sel
    sel = region == load["region"]
    if not sel.any():
        raise ValueError(f"Unknown region {load['region']}")
    return sel


def _radius(load: dict, h: float) -> float:
    return max(float(load.get("radius", POINT_RADIUS)), 1.5 * h)


def _distance_to_triangles(tris: np.ndarray, p: np.ndarray) -> np.ndarray:
    """Distance from point p to each triangle: to its plane where p projects inside, else to its edges."""
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    n = np.cross(b - a, c - a)
    nn = np.einsum("ij,ij->i", n, n)
    ok = nn > 1e-18
    plane = np.where(ok, np.einsum("ij,ij->i", p - a, n) / np.sqrt(np.where(ok, nn, 1)), np.inf)
    q = p - plane[:, None] * n / np.sqrt(np.where(ok, nn, 1))[:, None]  # projection onto the plane
    inside = ok.copy()
    for u, v in ((a, b), (b, c), (c, a)):
        inside &= np.einsum("ij,ij->i", np.cross(v - u, q - u), n) >= 0
    edge = np.full(len(tris), np.inf)
    for u, v in ((a, b), (b, c), (c, a)):
        d = v - u
        t = np.clip(np.einsum("ij,ij->i", p - u, d) / np.maximum(np.einsum("ij,ij->i", d, d), 1e-18), 0, 1)
        edge = np.minimum(edge, np.linalg.norm(u + t[:, None] * d - p, axis=1))
    return np.where(inside, np.abs(plane), edge)


def _where(load: dict) -> str:
    return f"The spot at {np.round(load['point'], 1).tolist()}" if "point" in load else f"Region {load['region']}"


def material_for(profile: Profile) -> Material:
    m = profile.material
    return Material(
        e_xy=m["modulus_xy_mpa"],
        e_z=m["modulus_xy_mpa"] * E_Z_RATIO,
        strength_xy=m["tensile_xy_mpa"],
        strength_z=m["tensile_xy_mpa"] * profile.z_strength_factor,
    )


def solid_fraction(model: Model, up, shells: Shells, profile: Profile) -> np.ndarray:
    """Fraction of each element that is solid shell (walls, top or bottom layers) for build
    direction ``up``; ``up=None`` treats every surface as a wall (direction-free, for one shared solve)."""
    h = model.grid.h
    pts = model.grid.centers(model.elems)
    wall = shells.walls * profile.line_width

    def frac(dirs, shell):
        if shell <= 0:
            return np.zeros(len(pts))
        d = _depth_along(model.grid, pts, dirs, shell + h)
        return np.clip((shell - (d - h / 2)) / h, 0.0, 1.0)  # voxel spans depths d ± h/2

    if up is None:
        return frac(np.vstack([np.eye(3), -np.eye(3)]), wall)
    up = np.asarray(up, float) / np.linalg.norm(up)
    a = np.cross(up, [1.0, 0, 0] if abs(up[0]) < 0.9 else [0, 1.0, 0])
    a /= np.linalg.norm(a)
    b = np.cross(up, a)
    sideways = np.array([np.cos(t) * a + np.sin(t) * b for t in np.arange(8) * np.pi / 4])
    lh = profile.layer_height
    return np.maximum.reduce([frac(sideways, wall), frac(up[None], shells.top * lh), frac(-up[None], shells.bottom * lh)])


def element_scale(model: Model, up, shells: Shells, profile: Profile) -> np.ndarray:
    """Effective stiffness/strength fraction per element: solid shells, sparse infill inside."""
    solid = solid_fraction(model, up, shells, profile)
    return solid + (1 - solid) * shells.infill**INFILL_EXPONENT


def material_volume(model: Model, up, shells: Shells, profile: Profile) -> float:
    """Plastic in the part (mm³) with these shells: what more walls or infill cost."""
    solid = solid_fraction(model, up, shells, profile)
    return float((solid + (1 - solid) * shells.infill).sum() * model.grid.h**3)


def _depth_along(grid: VoxelGrid, pts: np.ndarray, dirs: np.ndarray, cap: float) -> np.ndarray:
    """Distance (mm) from each point to the outside, the shortest along ``dirs``, up to ``cap``.
    Steps of h/3 so an axis-aligned surface voxel comes out at exactly h/2."""
    step = grid.h / 3
    shape = np.array(grid.solid.shape)
    out = np.full(len(pts), cap)
    for k in range(1, int(np.ceil(cap / step)) + 1):
        for d in dirs:
            idx = grid.index_of(pts + d * k * step)
            ok = np.all((idx >= 0) & (idx < shape), axis=1)
            empty = ~ok
            empty[ok] = ~grid.solid[tuple(idx[ok].T)]
            out = np.where(empty, np.minimum(out, (k - 0.5) * step), out)
    return out


def evaluate(case: LoadCase, up, profile: Profile, shells: Shells = Shells()) -> dict:
    """Solve with the anisotropic material for build direction ``up``."""
    mat = material_for(profile)
    r = rotation_to_z(up)
    c = rotate_stiffness(mat.stiffness_print_frame(), r)
    scale = element_scale(case.model, up, shells, profile)
    u, info = solve(case.model, c, scale, case.forces, case.fixed)
    stress = element_stress(case.model, c, scale, u)
    fi_layer, fi_plane = failure_indices(stress, np.asarray(up, float), mat, scale)
    return _summary(case, fi_layer, fi_plane, u, info)


def evaluate_cases(cases: list[LoadCase], up, profile: Profile, shells: Shells = Shells()) -> dict:
    """``evaluate`` for every case; the weakest one is returned, with all safety factors in ``cases``."""
    results = [evaluate(c, up, profile, shells) for c in cases]
    k = int(np.argmin([r["safety_factor"] for r in results]))
    worst = results[k]
    worst["case"] = cases[k].number
    worst["cases"] = [{"case": c.number, "safety_factor": r["safety_factor"], "verdict": r["verdict"]} for c, r in zip(cases, results)]
    return worst


def cases_safety(cases: list[LoadCase], ups: np.ndarray, profile: Profile, shells: Shells = Shells()) -> np.ndarray:
    """Worst-case ``orientation_safety`` over all cases."""
    return np.min([orientation_safety(c, ups, profile, shells) for c in cases], axis=0)


def orientation_safety(case: LoadCase, ups: np.ndarray, profile: Profile, shells: Shells = Shells()) -> np.ndarray:
    """Safety factor for many build directions from ONE isotropic solve.

    Rotating the stress field is much cheaper than re-solving; anisotropy shifts the
    load paths only a little, which is fine for ranking orientations.
    """
    mat = material_for(profile)
    iso = Material(mat.e_xy, mat.e_xy, mat.nu_xy, mat.nu_xy)
    c = iso.stiffness_print_frame()
    scale = element_scale(case.model, None, shells, profile)
    u, _ = solve(case.model, c, scale, case.forces, case.fixed)
    stress = element_stress(case.model, c, scale, u)
    out = []
    for up in ups:
        # Shells as walls all round: where the top/bottom layers sit changes the load paths only a little.
        fl, fp = failure_indices(stress, np.asarray(up, float), mat, scale)
        worst = max(fl[~case.ignore].max(initial=0), fp[~case.ignore].max(initial=0))
        out.append(1 / worst if worst > 0 else np.inf)
    return np.array(out)


def failure_indices(stress: np.ndarray, up: np.ndarray, mat: Material, scale: np.ndarray):
    """(layer, in-plane) failure index per element; ≥ 1 means expected to break.

    Layer: tension across layers and shear along them vs. interlayer strength.
    In-plane: von Mises vs. in-plane strength. Sparse infill is weaker by ``scale``.
    """
    up = up / np.linalg.norm(up)
    s = _tensor(stress)  # (E, 3, 3)
    traction = s @ up
    normal = traction @ up
    shear = np.linalg.norm(traction - normal[:, None] * up, axis=1)
    allow = np.maximum(scale, 1e-3)
    fi_layer = np.hypot(np.maximum(normal, 0) / mat.strength_z, shear / mat.strength_z) / allow
    sx, sy, sz, syz, sxz, sxy = stress.T
    vm = np.sqrt(0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2) + 3 * (syz**2 + sxz**2 + sxy**2))
    fi_plane = vm / mat.strength_xy / allow
    return fi_layer, fi_plane


def verdict(sf: float) -> str:
    return "fine" if sf >= 2 else "borderline" if sf >= 1 else "likely to break"


def _summary(case: LoadCase, fi_layer, fi_plane, u, info) -> dict:
    ok = ~case.ignore
    fl, fp = np.where(ok, fi_layer, 0), np.where(ok, fi_plane, 0)
    worst_layer, worst_plane = fl.max(initial=0), fp.max(initial=0)
    mode = "layers" if worst_layer >= worst_plane else "material"
    worst = max(worst_layer, worst_plane)
    crit = int(np.argmax(fl if mode == "layers" else fp))
    sf = float(1 / worst) if worst > 0 else float("inf")
    return {
        "safety_factor": sf,
        "verdict": verdict(sf),
        "mode": mode,  # "layers": layers pull/shear apart; "material": the plastic itself yields
        "critical_point": case.model.grid.centers(case.model.elems[crit]).tolist(),
        "max_displacement_mm": float(np.linalg.norm(u, axis=1).max()),
        "solver_converged": info == 0,
        "fi": np.maximum(fi_layer, fi_plane),
    }


def face_values(mesh: Mesh, grid: VoxelGrid, model: Model, values: np.ndarray) -> np.ndarray:
    """Element values mapped to triangles (via the voxel just inside each triangle)."""
    normals, _ = mesh.normals_and_areas()
    elem_id = np.full(grid.solid.shape, -1)
    elem_id[tuple(model.elems.T)] = np.arange(len(model.elems))
    pts = mesh.triangles.mean(axis=1) - normals * grid.h * 0.5
    ids = _lookup(pts, grid, elem_id)
    return np.where(ids >= 0, values[np.maximum(ids, 0)], 0.0)


def _surface_elements(tris, normals, grid: VoxelGrid, elem_id) -> np.ndarray:
    pts = np.concatenate([tris.mean(axis=1), tris.reshape(-1, 3), (tris + tris.mean(axis=1, keepdims=True)).reshape(-1, 3) / 2])
    inward = np.concatenate([normals, np.repeat(normals, 3, axis=0), np.repeat(normals, 3, axis=0)])
    ids = _lookup(pts - inward * grid.h * 0.5, grid, elem_id)
    return np.unique(ids[ids >= 0])


def _lookup(pts, grid: VoxelGrid, elem_id) -> np.ndarray:
    idx = grid.index_of(pts)
    inside = np.all((idx >= 0) & (idx < np.array(grid.solid.shape)), axis=1)
    out = np.full(len(pts), -1)
    out[inside] = elem_id[tuple(idx[inside].T)]
    return out


def _element_nodes(model: Model, elems: np.ndarray) -> np.ndarray:
    return model.edof[elems][:, 0::3] // 3


def _grow(mask: np.ndarray, model: Model, elem_id) -> np.ndarray:
    """Add face neighbours of masked elements (stresses next to point loads are not meaningful)."""
    idx = model.elems[mask]
    out = mask.copy()
    shape = np.array(model.grid.solid.shape)
    for d in np.vstack([np.eye(3, dtype=int), -np.eye(3, dtype=int)]):
        n = idx + d
        ok = np.all((n >= 0) & (n < shape), axis=1)
        ids = elem_id[tuple(n[ok].T)]
        out[ids[ids >= 0]] = True
    return out


def _tensor(v: np.ndarray) -> np.ndarray:
    sx, sy, sz, syz, sxz, sxy = v.T
    return np.stack([np.stack([sx, sxy, sxz], -1), np.stack([sxy, sy, syz], -1), np.stack([sxz, syz, sz], -1)], -2)
