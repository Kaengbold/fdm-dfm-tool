"""Strength check for a printed part under user-defined loads.

Loads are given on regions (see regions.py), or around a point on the surface (an edge, a
corner, a spot on a big face — part coordinates, ``radius`` mm, default 3):
  {"region": 3, "kind": "fixed"}                      # clamped or glued: can't move at all
  {"region": 3, "kind": "fixed", "hold": "resting"}   # resting on a surface: only pushes back, can lift off
  {"region": 5, "kind": "fixed", "hold": "screw"}     # a screw or bolt in this hole: bears on the hole wall
  {"region": 7, "kind": "force", "kg": 5, "direction": "push" | "pull" | "down" | [x, y, z], "case": 2}
  {"point": [10, 0, 5], "radius": 2, "kind": "force", "kg": 1}
``down`` means -Z in the part's own (as-modelled) coordinates, like a hanging weight.
Forces with different ``case`` numbers are separate situations (e.g. "hanging" and "being
pulled sideways"); fixed faces hold in every case and the weakest case decides.

The model: voxels with an anisotropic material whose weak axis is the build
direction. Within a layer it is the same in every direction, except at the detailed level: there
the sliced G-code gives the plastic in each spot and which way its lines run (``Printed``). How the slicer fills the part (``Shells``) is modelled by
depth: walls on the sides (walls × line width), solid top and bottom layers on surfaces facing
up and down (layers × layer height), and the much softer infill inside. Cubes on the surface
count with the share of them that is part (``VoxelGrid.fill``), so thin walls and ribs keep their
real thickness instead of a whole cube or none.
Results are indications — "fine / borderline / likely to break" and why.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .fea import Material, Model, build_model, element_stress, lowest_mode, rotate_stiffness, solve
from .mesh import Mesh
from .orientation import rotation_to_z
from .profile import Profile
from .service import heat_factor, required_safety
from .voxel import VoxelGrid, depth, voxelize

G = 9.81
POINT_RADIUS = 3.0  # mm around a picked point that carries the load
# Element rings around supports and loads left out of the verdict: a rigid clamp or a load on a few
# voxels gives stress peaks that grow as the grid gets finer (a singularity), as in any FEA.
SINGULAR_RINGS = 2
NEAR_SUPPORT_VOXELS = 4  # weakest spot this close to a held face: say the clamp may spread the load
E_Z_RATIO = 0.85  # layer-direction stiffness vs. in-plane (stiffness drops far less than strength)
INFILL_EXPONENT = 1.5  # Gibson–Ashby-style scaling for stiffness and strength of sparse infill
# Resting faces and screw holes: springs along the surface normal that only push (released where the
# part pulls away, re-solved), much stiffer than the part next to them, plus a little friction sideways
# (which also keeps a part that only rests from sliding away in the solve).
CONTACT_STIFFNESS = 10.0  # × E·h per node
FRICTION = 0.02  # sideways spring vs. the normal one
CONTACT_ROUNDS = 8
HOLDS = ("clamped", "resting", "screw")
MIN_SCALE = 0.01
 # empty cubes inside a hollow print: soft, but not zero (the solver needs some stiffness)


@dataclass(frozen=True)
class Zone:
    """A box printed with more walls and infill than the rest (never fewer): a slicer modifier volume.
    Corners in the build frame (``rotation_to_z(up) @ part``), so it stands upright on the bed.
    ``void``: a hidden gap instead (a negative volume inside the part): empty, and the slicer prints
    walls all round it, so the plastic next to it is solid. ``shape``: "box", "cylinder" (upright, in
    the box) or "ball" (in the box): round zones fit round things like a boss or a hole's wall."""

    lo: tuple[float, float, float]
    hi: tuple[float, float, float]
    walls: int
    infill: float
    void: bool = False
    shape: str = "box"

    def gap(self, q: np.ndarray) -> np.ndarray:
        """How far (mm) each build-frame point is outside the zone (0 inside). Round shapes use the
        distance scaled by the shortest half-size: exact for circles and spheres, close for ellipses."""
        lo, hi = np.asarray(self.lo), np.asarray(self.hi)
        if self.shape == "box":
            return np.linalg.norm(np.maximum(np.maximum(lo - q, q - hi), 0.0), axis=1)
        c, r = (lo + hi) / 2, np.maximum((hi - lo) / 2, 1e-9)
        if self.shape == "ball":
            return np.maximum(np.linalg.norm((q - c) / r, axis=1) - 1, 0) * r.min()
        side = np.maximum(np.linalg.norm((q[:, :2] - c[:2]) / r[:2], axis=1) - 1, 0) * r[:2].min()
        return np.hypot(side, np.maximum(np.maximum(lo[2] - q[:, 2], q[:, 2] - hi[2]), 0.0))


ZONE_SHAPES = ("box", "cylinder", "ball")


@dataclass(frozen=True)
class Shells:
    """How the slicer fills the part: wall lines, solid top/bottom layers, infill fraction, and
    zones that are printed differently (``Zone``, later ones win where they overlap)."""

    walls: int = 2
    infill: float = 0.15
    top: int = 4
    bottom: int = 3
    zones: tuple = ()


@dataclass
class Printed:
    """What the sliced G-code really puts in each analysis cube (the detailed level, see gcode.py)."""

    scale: np.ndarray  # (E,) stiffness/strength fraction, like element_scale
    fill: np.ndarray  # (E,) share of the cube filled with plastic
    lines: np.ndarray  # (E, 3) main line direction, part frame
    aligned: np.ndarray  # (E,) 1 = all lines one way (walls), 0 = crossing (grid infill, alternating solid layers)
    first_s: np.ndarray  # (E,) when the cube was first printed (s since start)


def printed_from(dep: dict, model: Model, inside: np.ndarray) -> Printed:
    """``Printed`` from gcode.deposit and the share of each voxel inside the part (grid-shaped).

    Walls and solid layers count with their plastic share of the room in the cube (a surface cube is
    only partly part); sparse infill like the simple level (fraction^1.5), its density smoothed over
    3×3×3 cubes because an infill cell is larger than a cube (a cube either has a line in it or not)."""
    inside = inside[tuple(model.elems.T)]
    room = np.maximum(inside, 0.05) * model.grid.h**3
    dense = np.clip(dep["volume"][:, :2].sum(axis=1) / room, 0.0, 1.0)
    sparse = _smooth(model.elems, model.grid.solid.shape, dep["volume"][:, 2] / room, inside)
    rest = np.maximum(1 - dense, 1e-9)
    scale = (dense + rest * np.clip(sparse / rest, 0.0, 1.0) ** INFILL_EXPONENT) * _fill(model)  # of the room, then of the cube
    t = _tensor(dep["tensor"])
    w, v = np.linalg.eigh(t)  # ascending; lines are horizontal, so the smallest is ~0 (the build direction)
    lines = v[:, :, 2]
    aligned = np.where(w[:, 2] > 0, (w[:, 2] - w[:, 1]) / np.maximum(w[:, 2] + w[:, 1], 1e-12), 0.0)
    return Printed(scale=np.maximum(scale, MIN_SCALE), fill=np.clip(dense + sparse, 0, 1), lines=lines,
                   aligned=aligned, first_s=dep["first_s"])


def _smooth(elems: np.ndarray, shape, values: np.ndarray, weight: np.ndarray) -> np.ndarray:
    """Average over each cube and its neighbours (3×3×3), weighted by how much of each is part."""
    from scipy.ndimage import uniform_filter

    num, den = np.zeros(shape), np.zeros(shape)
    num[tuple(elems.T)] = values * weight
    den[tuple(elems.T)] = weight
    num, den = uniform_filter(num, 3, mode="constant"), uniform_filter(den, 3, mode="constant")
    return num[tuple(elems.T)] / np.maximum(den[tuple(elems.T)], 1e-12)


@dataclass
class LoadCase:
    model: Model
    forces: np.ndarray  # (n_nodes, 3) N
    fixed: np.ndarray  # (n_nodes,) bool
    ignore: np.ndarray  # (E,) bool — elements at load/support points (stress singularities)
    number: int = 1  # load case number (see module docstring)
    contact: tuple | None = None  # (nodes, outward normals): resting faces and screw holes, push-only

    def held(self) -> np.ndarray:
        """Nodes held in any way (clamped, resting, screw)."""
        out = self.fixed.copy()
        if self.contact is not None:
            out[self.contact[0]] = True
        return out


def prepare_cases(mesh: Mesh, region: np.ndarray, loads: list[dict], target_voxels: int = 12_000) -> list[LoadCase]:
    """One LoadCase per case number, all on the same voxel model."""
    fixed = [l for l in loads if l["kind"] == "fixed"]
    forces = [l for l in loads if l["kind"] != "fixed"]
    numbers = sorted({int(l.get("case", 1)) for l in forces}) or [1]
    model = build_model(voxelize(mesh, target_voxels, partial=True))
    out = []
    for n in numbers:
        case = prepare(mesh, region, fixed + [l for l in forces if int(l.get("case", 1)) == n], model=model)
        case.number = n
        out.append(case)
    return out


def prepare(mesh: Mesh, region: np.ndarray, loads: list[dict], target_voxels: int = 12_000, model: Model | None = None) -> LoadCase:
    model = model or build_model(voxelize(mesh, target_voxels, partial=True))
    grid = model.grid
    normals, _ = mesh.normals_and_areas()
    forces = np.zeros((model.n_nodes, 3))
    fixed = np.zeros(model.n_nodes, dtype=bool)
    elem_id = np.full(grid.solid.shape, -1)
    elem_id[tuple(model.elems.T)] = np.arange(len(model.elems))
    ignore = np.zeros(len(model.elems), dtype=bool)
    surface = None
    contact_nodes, contact_normals = [], []

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
            hold = load.get("hold", "clamped")
            if hold not in HOLDS:
                raise ValueError(f"hold must be one of {HOLDS}")
            if hold == "clamped":
                fixed[nodes] = True
            else:
                contact_nodes.append(nodes)
                contact_normals.append(_node_normals(model, nodes, mesh.triangles[sel], normals[sel]))
        else:
            n = (normals[sel] * mesh.normals_and_areas()[1][sel, None]).sum(axis=0)
            n /= np.linalg.norm(n)
            d = load.get("direction", "push")
            vec = {"push": -n, "pull": n, "down": np.array([0.0, 0.0, -1.0])}.get(d) if isinstance(d, str) else np.asarray(d, float)
            vec = vec / np.linalg.norm(vec)
            forces[nodes] += vec * float(load["kg"]) * G / len(nodes)
    contact = None
    if contact_nodes:
        nodes, first = np.unique(np.concatenate(contact_nodes), return_index=True)
        keep = ~fixed[nodes]  # clamped wins where both touch
        contact = (nodes[keep], np.concatenate(contact_normals)[first][keep])
    held = fixed.copy()
    if contact is not None:
        held[contact[0]] = True
    if not held.any():
        raise ValueError("Mark at least one face as fixed (where the part is held)")
    if not forces.any():
        raise ValueError("Add at least one load")
    loose = _loose_nodes(model, held)
    if loose.any():
        if not np.abs(forces[~loose]).sum():
            raise ValueError("The load isn't connected to a held face (at the analysis resolution, thin links are lost)")
        fixed |= loose
        forces[loose] = 0.0
    for _ in range(SINGULAR_RINGS):  # stresses right at supports and loads are an artefact of rigid clamps / point loads
        ignore = _grow(ignore, model, elem_id)
    if contact is not None:
        contact = (contact[0][~loose[contact[0]]], contact[1][~loose[contact[0]]])
    return LoadCase(model, forces, fixed, ignore, contact=contact)


def _node_normals(model: Model, nodes: np.ndarray, tris: np.ndarray, normals: np.ndarray) -> np.ndarray:
    """Outward surface normal at each node: that of the nearest point sampled on the faces (a screw
    hole's wall turns, so one normal per face isn't enough)."""
    from scipy.spatial import cKDTree

    fine, src = _refine_with_source(tris, model.grid.h / 2)
    pos = model.grid.origin + np.argwhere(model.node_index >= 0)[nodes] * model.grid.h  # node ids follow this order
    _, i = cKDTree(fine.mean(axis=1)).query(pos)
    return normals[src[i]]


def _solve(case: LoadCase, c: np.ndarray, scale: np.ndarray, e: float):
    """``solve`` with the case's supports. Resting faces and screw holes only push: springs where the
    part pulls away are released and the solve repeated until nothing changes."""
    if case.contact is None or not len(case.contact[0]):
        return solve(case.model, c, scale, case.forces, case.fixed)
    nodes, n = case.contact
    k = CONTACT_STIFFNESS * e * case.model.grid.h
    active = np.ones(len(nodes), dtype=bool)
    for _ in range(CONTACT_ROUNDS):
        m = int(active.sum())
        if m == 0:
            if case.fixed.any():  # lifts off the rest, the clamp alone holds it
                return solve(case.model, c, scale, case.forces, case.fixed)
            raise ValueError("The part lifts off everywhere it rests: hold it somewhere too (clamped or a screw)")
        sel = nodes[active]
        springs = (np.concatenate([sel] + [sel] * 3), np.concatenate([n[active]] + [np.tile(np.eye(3)[i], (m, 1)) for i in range(3)]),
                   np.concatenate([np.full(m, k * (1 - FRICTION))] + [np.full(m, k * FRICTION)] * 3))
        u, info = solve(case.model, c, scale, case.forces, case.fixed, springs=springs)
        pressed = np.einsum("ij,ij->i", u[nodes], n) >= 0  # moving into the support: it pushes back
        if np.array_equal(pressed, active):
            break
        active = pressed
    return u, info


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
    k = heat_factor(profile)
    return Material(
        e_xy=m["modulus_xy_mpa"] * k,
        e_z=m["modulus_xy_mpa"] * E_Z_RATIO * k,
        strength_xy=m["tensile_xy_mpa"] * k,
        strength_z=m["tensile_xy_mpa"] * profile.z_strength_factor * k,
    )



def solid_fraction(model: Model, up, shells: Shells, profile: Profile) -> np.ndarray:
    """Fraction of each element that is solid shell (walls, top or bottom layers) for build
    direction ``up``; ``up=None`` treats every surface as a wall (direction-free, for one shared solve).
    Shells from opposite sides add up: a plate thinner than a cube has its top and bottom layers in
    the same cube."""
    h = model.grid.h
    pts = model.grid.centers(model.elems)
    wall = shells.walls * profile.line_width
    span = h * np.maximum(_fill(model), 1e-3)  # depth range the plastic in each cube covers

    def frac(dirs, shell):  # (len(dirs), E)
        if shell <= 0:
            return np.zeros((len(dirs), len(pts)))
        d = _depth_along(model.grid, pts, dirs, shell + h)
        return np.clip((shell - d) / span, 0.0, 1.0)  # the cube's plastic lies at depths d … d + span

    def both_sides(f):  # directions i and i + n/2 are opposite
        n = len(f) // 2
        return np.minimum(f[:n] + f[n:], 1.0).max(axis=0)

    if up is None:
        return both_sides(frac(np.vstack([np.eye(3), -np.eye(3)]), wall))
    up = np.asarray(up, float) / np.linalg.norm(up)
    a = np.cross(up, [1.0, 0, 0] if abs(up[0]) < 0.9 else [0, 1.0, 0])
    a /= np.linalg.norm(a)
    b = np.cross(up, a)
    sideways = np.array([np.cos(t) * a + np.sin(t) * b for t in np.arange(8) * np.pi / 4])
    lh = profile.layer_height
    top_bottom = np.minimum(frac(up[None], shells.top * lh)[0] + frac(-up[None], shells.bottom * lh)[0], 1.0)
    return np.maximum(both_sides(frac(sideways, wall)), top_bottom)


def element_scale(model: Model, up, shells: Shells, profile: Profile) -> np.ndarray:
    """Effective stiffness/strength fraction per element: solid shells, sparse infill inside.
    At least ``MIN_SCALE``: a hollow part (0 % infill) would leave cubes without stiffness."""
    solid, infill = _zoned(model, up, shells, profile)
    return np.maximum((solid + (1 - solid) * infill**INFILL_EXPONENT) * _fill(model), MIN_SCALE)


def material_volume(model: Model, up, shells: Shells, profile: Profile) -> float:
    """Plastic in the part (mm³) with these shells: what more walls or infill cost."""
    solid, infill = _zoned(model, up, shells, profile)
    return float(((solid + (1 - solid) * infill) * _fill(model)).sum() * model.grid.h**3)


def _fill(model: Model) -> np.ndarray | float:
    """Share of each element inside the part (1 where the grid has no partial voxels)."""
    return 1.0 if model.grid.fill is None else model.grid.fill[tuple(model.elems.T)]


def _zoned(model: Model, up, shells: Shells, profile: Profile) -> tuple[np.ndarray, np.ndarray]:
    """Solid-shell fraction and infill per element, with each zone's own walls and infill inside it.
    Shell fractions are cached on the model: the optimiser asks for the same walls many times."""
    cache = model.__dict__.setdefault("_solid_cache", {})

    def solid(walls):
        key = (None if up is None else tuple(np.round(np.asarray(up, float), 6)), walls, shells.top, shells.bottom,
               profile.line_width, profile.layer_height)
        if key not in cache:
            cache[key] = solid_fraction(model, up, Shells(walls, shells.infill, shells.top, shells.bottom), profile)
        return cache[key]

    out = solid(shells.walls).copy()
    infill = np.full(len(out), shells.infill)
    if shells.zones and up is not None:
        q = model.grid.centers(model.elems) @ rotation_to_z(up).T
        for z in shells.zones:
            if z.void:
                continue
            inside = z.gap(q) == 0
            out[inside] = solid(max(z.walls, shells.walls))[inside]  # a zone only ever adds strength
            infill[inside] = max(z.infill, shells.infill)
        wall = shells.walls * profile.line_width
        for z in shells.zones:  # gaps last: their walls are printed whatever the zone says
            if not z.void:
                continue
            gap = z.gap(q)
            ring = gap <= wall + model.grid.h / 2  # the cube reaches into the gap's walls
            out[ring] = 1.0
            out[gap == 0] = infill[gap == 0] = 0.0
    return out, infill


ZONE_SHARE = 0.5  # cubes using at least this share of the worst cube's strength make a zone
ZONE_PAD_MM = 2.0  # zones reach this far past the stressed cubes
MAX_ZONES = 3


def find_zones(model: Model, fi: np.ndarray, ignore: np.ndarray, up, walls: int, infill: float) -> list[Zone]:
    """Boxes around the most stressed places (up to ``MAX_ZONES``, the most loaded first), upright in
    the build frame and inside the part's height, starting with the given walls and infill."""
    from scipy.ndimage import label

    worst = fi[~ignore].max(initial=0)
    if worst <= 0:
        return []
    hot = np.zeros(model.grid.solid.shape, dtype=bool)
    hot[tuple(model.elems.T)] = fi >= ZONE_SHARE * worst
    groups, n = label(hot, structure=np.ones((3, 3, 3)))
    if n == 0:
        return []
    ids = groups[tuple(model.elems.T)]
    load = np.bincount(ids, np.where(ids > 0, fi, 0), minlength=n + 1)[1:]
    r = rotation_to_z(up)
    q = model.grid.centers(model.elems) @ r.T
    part_lo, part_hi = q.min(axis=0) - model.grid.h / 2, q.max(axis=0) + model.grid.h / 2
    pad = model.grid.h / 2 + ZONE_PAD_MM
    out = []
    for g in np.argsort(-load)[:MAX_ZONES] + 1:
        pts = q[ids == g]
        lo = np.maximum(pts.min(axis=0) - pad, part_lo)
        hi = np.minimum(pts.max(axis=0) + pad, part_hi)
        out.append(Zone(tuple(np.round(lo, 2).tolist()), tuple(np.round(hi, 2).tolist()), walls, infill))
    return out


def _depth_along(grid: VoxelGrid, pts: np.ndarray, dirs: np.ndarray, cap: float) -> np.ndarray:
    """How deep (mm) the cube at each centre point starts below the surface along each of ``dirs``
    (len(dirs), len(pts)), up to ``cap``: the plastic passed on the way out, in steps of h/3 (three per cube when
    axis-aligned). Partly filled cubes count with their fill, so a mostly empty cube kept for a thin
    edge doesn't push the surface out and the walls into the cube behind it."""
    step = grid.h / 3
    shape = np.array(grid.solid.shape)
    fill = grid.solid.astype(float) if grid.fill is None else grid.fill
    own = grid.index_of(pts)
    out = np.full((len(dirs), len(pts)), cap)
    for i, d in enumerate(dirs):
        passed = np.zeros(len(pts))
        going = np.ones(len(pts), dtype=bool)
        for k in range(1, int(np.ceil(cap / step)) + 1):
            idx = grid.index_of(pts + d * k * step)
            ok = np.all((idx >= 0) & (idx < shape), axis=1)
            f = np.zeros(len(pts))
            f[ok] = fill[tuple(idx[ok].T)]
            out[i] = np.where(going & (f == 0), passed, out[i])  # reached the outside
            going &= f > 0
            passed += np.where(going & np.any(idx != own, axis=1), f * step, 0.0)
    return out


def evaluate(case: LoadCase, up, profile: Profile, shells: Shells = Shells(), printed: Printed | None = None) -> dict:
    """Solve with the anisotropic material for build direction ``up``. With ``printed`` (from the
    G-code) the plastic in each cube and the line directions replace the shells model."""
    mat = material_for(profile)
    r = rotation_to_z(up)
    c = rotate_stiffness(mat.stiffness_print_frame(), r)
    scale = element_scale(case.model, up, shells, profile) if printed is None else printed.scale
    u, info = _solve(case, c, scale, mat.e_xy)
    stress = element_stress(case.model, c, scale, u)
    fi_layer, fi_plane = failure_indices(stress, np.asarray(up, float), mat, scale)
    fi_lines = None if printed is None else lines_index(stress, np.asarray(up, float), mat, printed)
    out = _summary(case, fi_layer, fi_plane, u, info, fi_lines)
    out["verdict"] = verdict(out["safety_factor"], required_safety(profile))
    out["vm"] = von_mises(stress)
    out["u"] = u
    return out


def vibration(case: LoadCase, up, profile: Profile, shells: Shells = Shells(), extra_kg: float = 0.0) -> tuple[float, np.ndarray]:
    """Lowest natural frequency (Hz) of the part as it is held, and how it swings (mode shape).
    The plastic's own mass (walls, infill) plus ``extra_kg`` spread over the loaded nodes (something
    mounted on it). Resting faces and screw holes hold like springs here (they don't let go)."""
    mat = material_for(profile)
    c = rotate_stiffness(mat.stiffness_print_frame(), rotation_to_z(up))
    scale = element_scale(case.model, up, shells, profile)
    solid, infill = _zoned(case.model, up, shells, profile)
    kg = (solid + (1 - solid) * infill) * _fill(case.model) * case.model.grid.h**3 * 1e-6 * profile.material["density_g_cm3"]  # mm³ · g/cm³ -> kg
    nodes = (case.model.edof[:, 0::3] // 3).ravel()
    mass = np.bincount(nodes, np.repeat(kg / 8, 8), minlength=case.model.n_nodes)
    loaded = np.linalg.norm(case.forces, axis=1) > 0
    if extra_kg > 0 and loaded.any():
        mass[loaded] += extra_kg / loaded.sum()
    springs = None
    if case.contact is not None and len(case.contact[0]):
        nodes_c, n = case.contact
        k = CONTACT_STIFFNESS * mat.e_xy * case.model.grid.h
        springs = (nodes_c, n, np.full(len(nodes_c), k))
    return lowest_mode(case.model, c, scale, np.maximum(mass, 1e-12), case.fixed, springs)


def impact_factor(drop_mm: float, deflection_mm: float) -> float:
    """How many times its weight a load hits with when it falls ``drop_mm`` onto the part, which bends
    ``deflection_mm`` under the same load held still: 1 + √(1 + 2h/δ) (energy method, e.g. Hibbeler,
    Mechanics of Materials, impact loading). A load set down gently (h = 0) still counts double."""
    return 1 + float(np.sqrt(1 + 2 * max(drop_mm, 0.0) / max(deflection_mm, 1e-6)))


def evaluate_cases(cases: list[LoadCase], up, profile: Profile, shells: Shells = Shells(), printed: Printed | None = None) -> dict:
    """``evaluate`` for every case; the weakest one is returned, with all safety factors in ``cases``."""
    results = [evaluate(c, up, profile, shells, printed) for c in cases]
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
    u, _ = _solve(case, c, scale, mat.e_xy)
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


def lines_index(stress: np.ndarray, up: np.ndarray, mat: Material, printed: Printed) -> np.ndarray:
    """Failure index for printed lines pulled or slid apart sideways (a bolt tearing out through
    the walls). Neighbouring lines in a layer are welded like layers are, so their bond is taken as
    the layer bond (until calibrated). Counts only as far as the lines in a cube run one way: where
    they cross (grid infill, alternating solid layers) the other lines carry the load."""
    up = up / np.linalg.norm(up)
    across = np.cross(up, printed.lines)
    across /= np.maximum(np.linalg.norm(across, axis=1, keepdims=True), 1e-12)
    traction = np.einsum("eij,ej->ei", _tensor(stress), across)
    normal = np.einsum("ei,ei->e", traction, across)
    shear = np.linalg.norm(traction - normal[:, None] * across, axis=1)
    return printed.aligned * np.hypot(np.maximum(normal, 0), shear) / mat.strength_z / np.maximum(printed.scale, 1e-3)


def von_mises(stress: np.ndarray) -> np.ndarray:
    """Von Mises stress (MPa) per element, the usual colour of FEA results."""
    sx, sy, sz, syz, sxz, sxy = stress.T
    return np.sqrt(0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2) + 3 * (syz**2 + sxz**2 + sxy**2))


def refine(tris: np.ndarray, max_edge: float, max_tris: int = 120_000) -> np.ndarray:
    """Split triangles into four until their edges are at most ``max_edge``: a display mesh fine
    enough to show the stress field (CAD exports often have huge flat triangles)."""
    return _refine_with_source(tris, max_edge, max_tris)[0]


def surface_values(model: Model, values: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Element values at every triangle corner, from the element just inside the surface there
    (the true element results, so the colours match the reported peak; nodal averaging smooths it away).
    Corners whose inside point misses the voxels get the node-averaged value."""
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    corners = tris.reshape(-1, 3)
    inward = corners - np.repeat(n, 3, axis=0) * model.grid.h * 0.5
    elem_id = np.full(model.grid.solid.shape, -1)
    elem_id[tuple(model.elems.T)] = np.arange(len(model.elems))
    ids = _lookup(inward, model.grid, elem_id)
    smooth = sample_nodes(model, node_average(model, values), corners)
    return np.where(ids >= 0, values[np.maximum(ids, 0)], smooth)


def node_average(model: Model, values: np.ndarray) -> np.ndarray:
    """Element values averaged onto the nodes around them (smooth colours, like FEA post-processing)."""
    nodes = (model.edof[:, 0::3] // 3).ravel()
    total = np.bincount(nodes, np.repeat(values, 8), minlength=model.n_nodes)
    return total / np.maximum(np.bincount(nodes, minlength=model.n_nodes), 1)


def sample_nodes(model: Model, node_values: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Trilinear interpolation of node values (n,) or (n, k) at points; corners without a node are left out."""
    grid = model.grid
    shape = np.array(model.node_index.shape)
    g = (pts - grid.origin) / grid.h
    i0 = np.clip(np.floor(g).astype(int), 0, shape - 2)
    f = np.clip(g - i0, 0.0, 1.0)
    vals = node_values.reshape(len(node_values), -1)
    acc = np.zeros((len(pts), vals.shape[1]))
    wsum = np.zeros(len(pts))
    for corner in np.ndindex(2, 2, 2):
        d = np.array(corner)
        idx = model.node_index[tuple((i0 + d).T)]
        w = np.prod(np.where(d == 1, f, 1 - f), axis=1) * (idx >= 0)
        acc += w[:, None] * vals[np.maximum(idx, 0)]
        wsum += w
    out = acc / np.maximum(wsum, 1e-12)[:, None]
    return out[:, 0] if node_values.ndim == 1 else out


def verdict(sf: float, need: float = 2.0) -> str:
    return "fine" if sf >= need else "borderline" if sf >= 1 else "likely to break"


def _summary(case: LoadCase, fi_layer, fi_plane, u, info, fi_lines=None) -> dict:
    ok = ~case.ignore
    fl, fp = np.where(ok, fi_layer, 0), np.where(ok, fi_plane, 0)
    fs = np.zeros_like(fl) if fi_lines is None else np.where(ok, fi_lines, 0)
    worst_layer, worst_plane, worst_lines = fl.max(initial=0), fp.max(initial=0), fs.max(initial=0)
    mode = "layers" if worst_layer >= max(worst_plane, worst_lines) else "lines" if worst_lines > worst_plane else "material"
    worst = max(worst_layer, worst_plane, worst_lines)
    crit = int(np.argmax({"layers": fl, "material": fp, "lines": fs}[mode]))
    sf = float(1 / worst) if worst > 0 else float("inf")
    grid = case.model.grid
    held = grid.origin + np.argwhere(case.model.node_index >= 0)[case.held()] * grid.h  # node ids follow this order
    centre = grid.centers(case.model.elems[crit])
    near = bool(len(held)) and float(np.linalg.norm(held - centre, axis=1).min()) <= NEAR_SUPPORT_VOXELS * grid.h
    return {
        "fi_layers_max": float(worst_layer),  # what the layer bond uses of its strength (for material swaps)
        "fi_material_max": float(worst_plane),
        "near_support": near,
        "safety_factor": sf,
        "verdict": verdict(sf),
        # "layers": layers pull/shear apart; "material": the plastic itself yields;
        # "lines": neighbouring lines in a layer pull apart (detailed level only)
        "mode": mode,
        "critical_point": case.model.grid.centers(case.model.elems[crit]).tolist(),
        "max_displacement_mm": float(np.linalg.norm(u, axis=1).max()),
        "solver_converged": info == 0,
        "fi": np.maximum.reduce([fi_layer, fi_plane] + ([] if fi_lines is None else [fi_lines])),
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
    """Voxels just inside a face: sampled every h/2 over each triangle (CAD faces are often two
    huge triangles; sampling only their corners held or loaded the part at a few points)."""
    fine, src = _refine_with_source(tris, grid.h / 2)
    n = normals[src]
    pts = np.concatenate([fine.mean(axis=1), fine.reshape(-1, 3)])
    inward = np.concatenate([n, np.repeat(n, 3, axis=0)])
    ids = _lookup(pts - inward * grid.h * 0.5, grid, elem_id)
    return np.unique(ids[ids >= 0])


def _refine_with_source(tris: np.ndarray, max_edge: float, max_tris: int = 2_000_000) -> tuple[np.ndarray, np.ndarray]:
    """``refine`` that also says which input triangle each small one came from."""
    src = np.arange(len(tris))
    while True:
        edges = np.linalg.norm(tris - np.roll(tris, 1, axis=1), axis=2).max(axis=1)
        long = edges > max_edge
        if not long.any() or len(tris) + 3 * long.sum() > max_tris:
            return tris, src
        a, b, c = tris[long, 0], tris[long, 1], tris[long, 2]
        ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2
        split = np.stack([np.stack(t, axis=1) for t in ((a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca))], axis=1).reshape(-1, 3, 3)
        tris = np.concatenate([tris[~long], split])
        src = np.concatenate([src[~long], np.repeat(src[long], 4)])


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
