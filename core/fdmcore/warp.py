"""Warping while printing: how each new layer's shrinking pulls on the part (T-162).

Plastic shrinks as it cools below the point where it sets (``softening_c``). Layers near the
heated bed stay warm and hardly shrink while printing; layers higher up cool to the air in the
printer and shrink fully (``shrinkage_pct``). Stuck on top of the warm bottom they can't, so the
part wants to curl: its ends and corners pull up on the bed. This is the "inherent strain" method
of print simulation: the analysis cubes are switched on layer by layer, each new layer gets its
shrinkage as a strain, and the part so far is solved with the bottom held by the bed.

The lift risk is how far the base would curl if the bed let go at the end of the print. That
grows with the square of the length and depends on the shape (a tall wall or a rib stiffens the
base). It is rated against a reference simulated on the same grid: a 10 × 3 mm bar as long as the
longest one that stays flat for this material and enclosure (thresholds.yaml, or the user's own
warping calibration print). ``lift_ratio`` is given like a length ratio (square root of the curl
ratio), so 1 means "like that bar" and the warping check's thresholds apply.

The bed's warmth fades with height like heat along a cooling fin: over sqrt(k · A/P / h), with
the cross-section's area A and perimeter P and the air's heat transfer h (more with the part fan).
So a thin wall cools a millimetre or two above the bed and a thick block stays warm higher up.

What stays behind after cooling is residual stress: each element's shrink minus what the part
let it do. ``residual_stress`` gives it per element (MPa, von Mises), the "stress left inside" view.

Linear and rough: real plastic creeps while warm (so real residual stress is lower), the fin
ignores the nozzle's heat, and the millimetres of the shape are exaggerated. Use it for where and how much relative to the
reference, not for exact numbers.
"""

from __future__ import annotations

from itertools import product

import numpy as np

from .fea import b_matrix, build_model, element_stiffness, rotate_stiffness, solve
from .heat import H_FAN, H_STILL, K_PLASTIC
from .mesh import Mesh
from .orientation import rotation_to_z
from .profile import Profile, load_data
from .progress import report
from .strength import Shells, element_scale, material_for, von_mises
from .voxel import VoxelGrid, voxelize

MAX_STEPS = 16  # solves while printing: voxel layers are grouped to at most this many
REF_BAR = (10.0, 3.0)  # width and height of the warping calibration bars (api.calibration_part)
RELEASE_SPRING = 1e-6  # × E·h: keeps the part from floating away once it is off the bed
THIN_WALL_MM = 1.0  # A/P of a thin wall: the fade used when no cross-sections are known
ROOM_C = 25.0


def warp_limit(profile: Profile) -> float | None:
    """Longest bar that stays flat (mm) for this material and enclosure."""
    measured = profile.material.get("warp_length_mm")
    table = measured or load_data("thresholds")["warping"]["risk_length_mm"].get(profile.material.get("based_on", profile.material_id), {})
    return table.get(profile.enclosure)


def simulate(mesh: Mesh, up, profile: Profile, shells: Shells = Shells(), target_voxels: int = 6000, h: float | None = None) -> dict:
    """Print ``mesh`` (part coordinates) with build direction ``up``. Returns the model, displacement
    after coming off the bed (n_nodes, 3, part frame), the bed's pull at each bed node while printing
    (N, positive = the part pulls away from the bed) and the bed nodes."""
    model = build_model(voxelize(mesh, target_voxels, h))
    up = np.asarray(up, float) / np.linalg.norm(up)
    grid = model.grid
    mat = material_for(profile)
    c = rotate_stiffness(mat.stiffness_print_frame(), rotation_to_z(up))
    scale = element_scale(model, up, shells, profile)
    ke = element_stiffness(c, grid.h)
    bt = sum(b_matrix(*g, grid.h).T for g in product((-1 / np.sqrt(3), 1 / np.sqrt(3)), repeat=3)) * (grid.h / 2) ** 3
    f_unit = bt @ (c @ -np.array([1, 1, 1, 0, 0, 0.0]))  # (24,) force of a unit isotropic shrink, scale 1

    node_pos = grid.origin + np.argwhere(model.node_index >= 0) * grid.h  # node ids follow this order
    height = node_pos @ up
    bed = height <= height.min() + 1e-6
    zc = grid.centers(model.elems) @ up
    elem_level = np.round((zc - zc.min()) / grid.h).astype(int)
    shrink = shrink_while_printing(zc - zc.min() + grid.h / 2, profile, fade_depth(model, elem_level, profile))
    levels = np.unique(elem_level)
    groups = np.array_split(levels, min(MAX_STEPS, len(levels)))
    b_mid = b_matrix(0.0, 0.0, 0.0, grid.h)
    elem_id = np.full(grid.solid.shape, -1)
    elem_id[tuple(model.elems.T)] = np.arange(len(model.elems))

    u_total = np.zeros((model.n_nodes, 3))
    held_total = np.zeros((model.n_nodes, 3))  # force the bed puts on the part
    strain = np.zeros((len(model.elems), 6))  # each element's strain since it was printed
    done = np.zeros(len(model.elems), dtype=bool)
    for k, g in enumerate(groups):
        report(k / (len(groups) + 1), "Printing it layer by layer")
        active = elem_level <= g[-1]
        new = active & ~done
        solid = np.zeros_like(grid.solid)
        solid[tuple(model.elems[active].T)] = True
        sub = build_model(VoxelGrid(solid, grid.origin, grid.h))
        full_e = elem_id[tuple(sub.elems.T)]
        full_n = model.node_index[tuple(np.argwhere(sub.node_index >= 0).T)]
        forces = np.zeros(sub.n_nodes * 3)
        sub_new = new[full_e]
        np.add.at(forces, sub.edof[sub_new].ravel(), ((scale * shrink)[full_e[sub_new], None] * f_unit).ravel())
        fixed = bed[full_n]
        u, _ = solve(sub, c, scale[full_e], forces.reshape(-1, 3), fixed)
        # What the bed has to hold: internal forces of the elements minus the shrink forces, at the bed nodes.
        internal = np.zeros(sub.n_nodes * 3)
        ue = u.ravel()[sub.edof]
        np.add.at(internal, sub.edof.ravel(), (scale[full_e, None] * (ue @ ke.T)).ravel())
        held = (internal - forces).reshape(-1, 3)
        held_total[full_n[fixed]] += held[fixed]
        u_total[full_n] += u
        strain[full_e] += ue @ b_mid.T
        done = active

    # Off the bed: the bed's forces go away (as if reversed), the part springs into its final shape.
    report(len(groups) / (len(groups) + 1), "Taking it off the bed")
    n_bed = int(bed.sum())
    springs = (np.repeat(np.flatnonzero(bed), 3), np.tile(np.eye(3), (n_bed, 1)), np.full(3 * n_bed, RELEASE_SPRING * mat.e_xy * grid.h))
    u_free, _ = solve(model, c, scale, -held_total, np.zeros(model.n_nodes, dtype=bool), springs=springs)
    strain += u_free.ravel()[model.edof] @ b_mid.T
    pull = -(held_total @ up)  # the bed pulling the part down = the part pulling away from the bed
    return {"model": model, "u": u_total + u_free, "pull": pull, "bed": bed, "up": up,
            "strain": strain, "shrink": shrink, "scale": scale, "c": c, "material": mat}


def residual_stress(result: dict) -> np.ndarray:
    """Stress left in each element once the part is off the bed and cool (MPa, von Mises): the
    element's own strain minus the shrink it wanted (stress = C · (strain + shrink))."""
    free = result["strain"] + result["shrink"][:, None] * np.array([1, 1, 1, 0, 0, 0.0])
    return von_mises(result["scale"][:, None] * (free @ result["c"].T))


def fade_length(profile: Profile, area_per_perimeter_mm: float | np.ndarray) -> float | np.ndarray:
    """Height (mm) over which the bed's warmth fades in a section with this area / perimeter: a
    cooling fin, sqrt(k · A/P / h)."""
    h = H_STILL + H_FAN * profile.material.get("fan_pct", 100) / 100
    return np.sqrt(K_PLASTIC * np.asarray(area_per_perimeter_mm) * 1e-3 / h) * 1e3


def fade_depth(model, elem_level: np.ndarray, profile: Profile) -> np.ndarray:
    """How many fade lengths each element sits above the bed (the integral of dz / fade length up
    the part). A/P per voxel layer ≈ h × elements / elements on the surface."""
    grid = model.grid
    solid = np.pad(grid.solid, 1)
    i = model.elems + 1
    shut = np.ones(len(i), dtype=bool)
    for d in np.vstack([np.eye(3, dtype=int), -np.eye(3, dtype=int)]):
        shut &= solid[tuple((i + d).T)]
    n = np.bincount(elem_level)
    outside = np.bincount(elem_level, ~shut, minlength=len(n))
    per_layer = grid.h / np.maximum(fade_length(profile, grid.h * n / np.maximum(outside, 1)), 1e-6)
    below = np.concatenate([[0.0], np.cumsum(per_layer)[:-1]])
    return below[elem_level] + per_layer[elem_level] / 2


def shrink_while_printing(height: np.ndarray, profile: Profile, depth: np.ndarray | None = None) -> np.ndarray:
    """Linear shrinkage of plastic at ``height`` mm above the bed while the print goes on: from where
    it sets (``softening_c``) down to the temperature there (the bed's warmth fading into the air).
    ``depth``: fade lengths above the bed (``fade_depth``); default a thin wall's."""
    from .heat import AMBIENT_C

    mat = profile.material
    t_set, t_air = float(mat["softening_c"]), AMBIENT_C[profile.enclosure]
    if depth is None:
        depth = np.asarray(height) / fade_length(profile, THIN_WALL_MM)
    t_here = t_air + (float(mat["bed_temp_c"]) - t_air) * np.exp(-depth)
    return mat.get("shrinkage_pct", 0.3) / 100 * np.clip(t_set - t_here, 0, None) / max(t_set - ROOM_C, 1.0)


def curl(result: dict) -> float:
    """How far the base curls off flat (mm, exaggerated): up-movement of the bed nodes after release,
    with the part's tilt taken out."""
    model, bed, up = result["model"], result["bed"], result["up"]
    pos = (model.grid.origin + np.argwhere(model.node_index >= 0) * model.grid.h)[bed]
    w = result["u"][bed] @ up
    a, b = _plane_axes(up)
    fit = np.c_[pos @ a, pos @ b, np.ones(len(pos))]
    res = w - fit @ np.linalg.lstsq(fit, w, rcond=None)[0]
    return float(res.max() - res.min())


def _plane_axes(up: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a = np.cross(up, [1.0, 0, 0] if abs(up[0]) < 0.9 else [0, 1.0, 0])
    a /= np.linalg.norm(a)
    return a, np.cross(up, a)


def lift_stress(result: dict) -> np.ndarray:
    """Pull per bed node as a stress (N/mm²): positive where the part pulls away from the bed."""
    return np.maximum(result["pull"], 0) / result["model"].grid.h ** 2


def reference(profile: Profile, h: float, shells: Shells = Shells()) -> float | None:
    """Curl of the bar that just stays flat, on the same grid size ``h``."""
    length = warp_limit(profile)
    if not length:
        return None
    from shapely.geometry import box

    from .additions import extrude

    w, t = REF_BAR
    bar = Mesh(extrude(box(0, 0, length, w), 0, t))
    return curl(simulate(bar, [0, 0, 1], profile, shells, h=h))
