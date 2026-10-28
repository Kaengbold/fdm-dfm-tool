"""Strength check for a printed part under user-defined loads.

Loads are given on regions (see regions.py):
  {"region": 3, "kind": "fixed"}
  {"region": 7, "kind": "force", "kg": 5, "direction": "push" | "pull" | "down" | [x, y, z], "case": 2}
``down`` means -Z in the part's own (as-modelled) coordinates, like a hanging weight.
Forces with different ``case`` numbers are separate situations (e.g. "hanging" and "being
pulled sideways"); fixed faces hold in every case and the weakest case decides.

The model: voxels with an anisotropic material whose weak axis is the build
direction; surface voxels carry the walls, inner voxels the (much softer) infill.
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
E_Z_RATIO = 0.85  # layer-direction stiffness vs. in-plane (stiffness drops far less than strength)
INFILL_EXPONENT = 1.5  # Gibson–Ashby-style scaling for stiffness and strength of sparse infill


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

    for load in loads:
        sel = region == load["region"]
        if not sel.any():
            raise ValueError(f"Unknown region {load['region']}")
        elems = _surface_elements(mesh.triangles[sel], normals[sel], grid, elem_id)
        if len(elems) == 0:
            raise ValueError(f"Region {load['region']} is too small for the analysis grid")
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
    return LoadCase(model, forces, fixed, _grow(ignore, model, elem_id))


def material_for(profile: Profile) -> Material:
    m = profile.material
    return Material(
        e_xy=m["modulus_xy_mpa"],
        e_z=m["modulus_xy_mpa"] * E_Z_RATIO,
        strength_xy=m["tensile_xy_mpa"],
        strength_z=m["tensile_xy_mpa"] * profile.z_strength_factor,
    )


def element_scale(model: Model, walls: int, line_width: float, infill: float) -> np.ndarray:
    """Effective stiffness/strength fraction per element: walls on the surface, infill inside."""
    d = depth(model.grid.solid)[tuple(model.elems.T)]
    shell = walls * line_width
    h = model.grid.h
    fill = infill**INFILL_EXPONENT
    # Fraction of each voxel layer (by depth) that is wall material.
    wall_frac = np.clip((shell - (d - 1) * h) / h, 0.0, 1.0)
    return wall_frac + (1 - wall_frac) * fill


def evaluate(case: LoadCase, up, profile: Profile, walls: int = 2, infill: float = 0.15) -> dict:
    """Solve with the anisotropic material for build direction ``up``."""
    mat = material_for(profile)
    r = rotation_to_z(up)
    c = rotate_stiffness(mat.stiffness_print_frame(), r)
    scale = element_scale(case.model, walls, profile.line_width, infill)
    u, info = solve(case.model, c, scale, case.forces, case.fixed)
    stress = element_stress(case.model, c, scale, u)
    fi_layer, fi_plane = failure_indices(stress, np.asarray(up, float), mat, scale)
    return _summary(case, fi_layer, fi_plane, u, info)


def evaluate_cases(cases: list[LoadCase], up, profile: Profile, walls: int = 2, infill: float = 0.15) -> dict:
    """``evaluate`` for every case; the weakest one is returned, with all safety factors in ``cases``."""
    results = [evaluate(c, up, profile, walls, infill) for c in cases]
    k = int(np.argmin([r["safety_factor"] for r in results]))
    worst = results[k]
    worst["case"] = cases[k].number
    worst["cases"] = [{"case": c.number, "safety_factor": r["safety_factor"], "verdict": r["verdict"]} for c, r in zip(cases, results)]
    return worst


def cases_safety(cases: list[LoadCase], ups: np.ndarray, profile: Profile, walls: int = 2, infill: float = 0.15) -> np.ndarray:
    """Worst-case ``orientation_safety`` over all cases."""
    return np.min([orientation_safety(c, ups, profile, walls, infill) for c in cases], axis=0)


def orientation_safety(case: LoadCase, ups: np.ndarray, profile: Profile, walls: int = 2, infill: float = 0.15) -> np.ndarray:
    """Safety factor for many build directions from ONE isotropic solve.

    Rotating the stress field is much cheaper than re-solving; anisotropy shifts the
    load paths only a little, which is fine for ranking orientations.
    """
    mat = material_for(profile)
    iso = Material(mat.e_xy, mat.e_xy, mat.nu_xy, mat.nu_xy)
    c = iso.stiffness_print_frame()
    scale = element_scale(case.model, walls, profile.line_width, infill)
    u, _ = solve(case.model, c, scale, case.forces, case.fixed)
    stress = element_stress(case.model, c, scale, u)
    out = []
    for up in ups:
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
