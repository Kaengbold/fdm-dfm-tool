"""How much each spot of the part matters, so the heat window can be strict where it counts (T-475).

A weight from ``floor`` to 1 per spot:

- with the user's loads: how much of the layer bond's strength the strength check says the spot uses
  (its "Layer" index: tension across layers, shear along them), scaled by the safety the profile asks
  for, so a spot right at that safety weighs 1;
- without loads, a guess from the shape (marked ``guessed``): roots of thin upright features (pins,
  pegs, ribs) weigh most, then the thin features themselves, sharp inside corners and screw or insert
  holes; outer walls are ordinary and the infill inside matters least.

The weight only tightens or relaxes the bonding goal (thermal.window): it never goes below ``floor``
and never touches the sag limit, so every spot is still kept from drooping.
"""

from dataclasses import dataclass

import numpy as np

from .mesh import Mesh
from .orientation import rotation_to_z
from .profile import load_data
from .voxel import VoxelGrid, voxelize

# Why a spot weighs what it does; ties go to the later entry.
REASONS = ("inside", "wall", "hole", "corner", "pin", "root", "load", "tag")


def settings() -> dict:
    return load_data("thermal")["importance"]


@dataclass
class Map:
    """Weights on a voxel grid in the frame ``part @ turn.T``."""

    grid: VoxelGrid
    turn: np.ndarray  # (3, 3): part frame -> grid frame
    weight: np.ndarray  # (nx, ny, nz), solid voxels only
    reason: np.ndarray  # (nx, ny, nz) int8 index into REASONS
    guessed: bool

    def at(self, pts: np.ndarray, offsets: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Weight and reason at part-frame points: those of the nearest solid voxel (a point on the
        surface often falls into an empty one). With ``offsets`` (part frame) the heaviest of the points
        moved by each: a heat cell matters as much as the most important plastic in it (the middle of a
        bent peg carries nothing, its sides the most)."""
        from scipy.ndimage import distance_transform_edt

        offsets = np.zeros((1, 3)) if offsets is None else np.asarray(offsets, float)
        shape = np.array(self.grid.solid.shape)
        near = distance_transform_edt(~self.grid.solid, return_distances=False, return_indices=True)
        idx = np.clip(self.grid.index_of((pts[None] + offsets[:, None]).reshape(-1, 3) @ self.turn.T), 0, shape - 1)
        i, j, k = (near[a][idx[:, 0], idx[:, 1], idx[:, 2]] for a in range(3))
        w, r = self.weight[i, j, k].reshape(len(offsets), -1), self.reason[i, j, k].reshape(len(offsets), -1)
        best = np.argmax(w, axis=0)
        cols = np.arange(w.shape[1])
        return w[best, cols], r[best, cols]

    def voxels(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Centres (part frame), weights and reasons of the solid voxels."""
        idx = np.argwhere(self.grid.solid)
        centres = self.grid.centers(idx) @ self.turn  # turn is a rotation: its inverse is its transpose
        return centres, self.weight[tuple(idx.T)], self.reason[tuple(idx.T)]


def from_strength(grid: VoxelGrid, elems: np.ndarray, layer_index: np.ndarray, required_safety: float) -> Map:
    """Weights from the strength check's layer index per analysis cube (part frame, strength grid)."""
    floor = float(settings()["floor"])
    weight = np.full(grid.solid.shape, floor)
    reason = np.zeros(grid.solid.shape, np.int8)
    w = np.clip(layer_index * required_safety, floor, 1.0)
    weight[tuple(elems.T)] = w
    reason[tuple(elems.T)] = np.where(w > floor, REASONS.index("load"), REASONS.index("inside"))
    return Map(grid, np.eye(3), weight, reason, guessed=False)


def guessed(mesh: Mesh, up, holes: list[dict] = ()) -> Map:
    """Weights guessed from the shape when no loads are entered. ``holes``: detected round holes
    (features.detect_features, part frame); screw-sized ones count."""
    from scipy import ndimage

    from .features import concave_edges

    cfg = settings()["guess"]
    weights = cfg["weights"]
    turn = rotation_to_z(np.asarray(up, float))
    turned = Mesh(mesh.triangles @ turn.T)
    grid = voxelize(turned, int(cfg["voxels"]))
    solid, h = grid.solid, grid.h
    nz = solid.shape[2]

    # Thin sections, slice by slice: narrow (thickness from the distance to the edge) and small.
    thick = np.full(solid.shape, np.inf)
    section = np.zeros(solid.shape, np.intp)  # an id per section (connected plastic in one slice)
    sections = 0
    for k in range(nz):
        lab, n = ndimage.label(solid[:, :, k])
        if not n:
            continue
        section[:, :, k] = np.where(lab > 0, lab + sections, 0)
        sections += n
        ids = np.arange(1, n + 1)
        edge = ndimage.distance_transform_edt(solid[:, :, k])
        width = (2 * np.asarray(ndimage.maximum(edge, lab, ids)) - 1) * h
        area = np.bincount(lab.ravel(), minlength=n + 1)[1:] * h * h
        narrow = np.where(area <= float(cfg["thin_area_mm2"]), width, np.inf)
        thick[:, :, k] = np.concatenate([[np.inf], narrow])[lab]
    thin = thick <= float(cfg["thin_mm"])

    # Sticking up (a pin, peg, rib or fin): a thin run that ends in the air and is at least about as tall
    # as it is thick. Plastic beside a sideways hole is thin too, but wider plastic carries on above it.
    # The end is judged per section, not per column: under a round hole single columns end in its air.
    # Root: the first few mm of such a run where it stands on wider plastic (not on the bed), plus the
    # plastic just under it: that's where an upright feature bends its layers apart.
    below = np.zeros(solid.shape, int)
    above = np.zeros(solid.shape, int)
    open_end = np.zeros(solid.shape, bool)
    for k in range(nz):
        below[:, :, k] = (below[:, :, k - 1] + 1 if k else 1) * thin[:, :, k]
    for k in range(nz - 1, -1, -1):
        if k == nz - 1:
            above[:, :, k], open_end[:, :, k] = thin[:, :, k], thin[:, :, k]
            continue
        above[:, :, k] = (above[:, :, k + 1] + 1) * thin[:, :, k]
        open_end[:, :, k] = thin[:, :, k] & np.where(thin[:, :, k + 1], open_end[:, :, k + 1], ~solid[:, :, k + 1])
    run = (below + above - 1) * h
    count = np.maximum(np.bincount(section.ravel(), minlength=sections + 1), 1)
    share = np.bincount(section[open_end], minlength=sections + 1) / count
    free = thin & (share >= float(cfg["free_share"]))[section]
    tall = free & (run >= np.maximum(float(cfg["min_tall_mm"]), float(cfg["tall_ratio"]) * np.where(thin, thick, 0)))
    rooted = np.zeros(solid.shape, bool)
    base = np.zeros(solid.shape, bool)
    root_layers = max(int(round(float(cfg["root_mm"]) / h)), 1)
    for k in range(1, nz):
        start = tall[:, :, k] & ~tall[:, :, k - 1] & solid[:, :, k - 1]
        rooted[:, :, k] = start | (rooted[:, :, k - 1] & tall[:, :, k])
        for d in range(1, min(root_layers, k) + 1):
            base[:, :, k - d] |= start & solid[:, :, k - d]
    root = ndimage.binary_dilation((rooted & (below * h <= float(cfg["root_mm"]))) | base) & solid

    corner = np.zeros(solid.shape, bool)
    for e in concave_edges(turned):
        a, b = np.asarray(e["a"], float), np.asarray(e["b"], float)
        t = np.linspace(0, 1, max(int(np.linalg.norm(b - a) / (h / 2)), 1) + 1)[:, None]
        idx = grid.index_of(a + t * (b - a))
        ok = ((idx >= 0) & (idx < solid.shape)).all(axis=1)
        corner[tuple(idx[ok].T)] = True
    corner = ndimage.binary_dilation(corner) & solid

    hole = np.zeros(solid.shape, bool)
    lo_r, hi_r = (float(v) for v in cfg["hole_radius_mm"])
    screws = [f for f in holes if f.get("type") == "hole" and lo_r <= f["radius"] <= hi_r]
    if screws:
        idx = np.argwhere(solid)
        centres = grid.centers(idx)
        for f in screws:  # the plastic around the hole that holds the screw or insert
            axis = np.asarray(f["axis"], float) @ turn.T
            rel = centres - np.asarray(f["center"], float) @ turn.T
            along = rel @ axis
            off = np.linalg.norm(rel - along[:, None] * axis, axis=1)
            near = (off <= f["radius"] + float(cfg["hole_ring_mm"])) & (np.abs(along) <= f["length"] / 2 + h)
            hole[tuple(idx[near].T)] = True

    depth = ndimage.distance_transform_edt(np.pad(solid, 1))[1:-1, 1:-1, 1:-1] * h
    wall = solid & (depth <= float(cfg["wall_mm"]) + h / 2)

    weight = np.full(solid.shape, float(weights["inside"]))
    reason = np.zeros(solid.shape, np.int8)
    for name, mask in (("wall", wall), ("hole", hole), ("corner", corner), ("pin", tall), ("root", root)):
        better = mask & (float(weights[name]) >= weight)
        weight[better], reason[better] = float(weights[name]), REASONS.index(name)
    return Map(grid, turn, np.maximum(weight, float(settings()["floor"])), reason, guessed=True)


def areas(m: Map, weight: np.ndarray, most: int = 8) -> list[np.ndarray]:
    """The important spots as groups of touching voxels (indices into ``m.voxels()`` order, ``weight`` per
    voxel after any marks): weighing more than an ordinary spot, heaviest first, then biggest; at most ``most``."""
    from scipy import ndimage

    idx = np.argwhere(m.grid.solid)
    dense = np.zeros(m.grid.solid.shape, bool)
    dense[tuple(idx.T)] = weight > float(settings()["ordinary"]) + 1e-6
    labels, n = ndimage.label(dense)
    if not n:
        return []
    lab = labels[tuple(idx.T)]
    order = np.argsort(lab, kind="stable")
    groups = np.split(order, np.searchsorted(lab[order], np.arange(1, n + 1)))[1:]
    groups.sort(key=lambda g: (-float(weight[g].max()), -len(g)))
    return groups[:most]


def bond_goal(goal_min_bond: float, weight: np.ndarray) -> np.ndarray:
    """The bonding goal at each weight: the goal's own at ``ordinary``, relaxed down to ``bond_relax`` ×
    it at ``floor``, and ``bond_strict_shift`` above it at 1 (what a spot marked load-bearing gets)."""
    cfg = settings()
    floor, ordinary = float(cfg["floor"]), float(cfg["ordinary"])
    low = np.clip((weight - floor) / (ordinary - floor), 0, 1)
    high = np.clip((weight - ordinary) / (1 - ordinary), 0, 1)
    relax = float(cfg["bond_relax"])
    return goal_min_bond * (relax + (1 - relax) * low) + float(cfg["bond_strict_shift"]) * high
