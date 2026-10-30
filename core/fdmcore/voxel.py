"""Voxelisation of a closed triangle mesh (ray parity along Z)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .mesh import Mesh


@dataclass
class VoxelGrid:
    solid: np.ndarray  # (nx, ny, nz) bool
    origin: np.ndarray  # world position of voxel (0, 0, 0)'s minimum corner
    h: float  # voxel edge length (mm)

    def centers(self, idx: np.ndarray) -> np.ndarray:
        return self.origin + (idx + 0.5) * self.h

    def index_of(self, pts: np.ndarray) -> np.ndarray:
        return np.floor((pts - self.origin) / self.h).astype(int)


def voxelize(mesh: Mesh, target_voxels: int = 30_000, h: float | None = None) -> VoxelGrid:
    """Solid voxels of a closed mesh. Picks ``h`` so the part has roughly ``target_voxels``."""
    lo, hi = mesh.bounds()
    if h is None:
        h = (abs(mesh.volume()) / target_voxels) ** (1 / 3)
        h = max(h, float((hi - lo).max()) / 200)  # cap grid size for very thin parts
    shape = np.maximum(np.ceil((hi - lo) / h).astype(int), 1)
    origin = lo - (shape * h - (hi - lo)) / 2  # centre the part in the grid
    return VoxelGrid(_solid(mesh, origin, float(h), shape), origin, float(h))


def inside_fraction(mesh: Mesh, grid: VoxelGrid, k: int = 3) -> np.ndarray:
    """Share of each voxel inside the part (k³ sub-voxels each): how much plastic a cube on the
    surface can hold at most."""
    fine = _solid(mesh, grid.origin, grid.h / k, np.array(grid.solid.shape) * k)
    nx, ny, nz = grid.solid.shape
    return fine.reshape(nx, k, ny, k, nz, k).mean(axis=(1, 3, 5))


def _solid(mesh: Mesh, origin: np.ndarray, h: float, shape: np.ndarray) -> np.ndarray:
    """Voxels of the grid (origin, h, shape) whose centre is inside the mesh."""
    # Ray through each column centre, nudged off exact edges/vertices.
    t = mesh.triangles[..., :2] - origin[:2]
    jitter = np.array([1.13e-4, 2.71e-4]) * h
    tz = mesh.triangles[..., 2]
    i0 = np.ceil((t[..., 0].min(axis=1) - jitter[0]) / h - 0.5).astype(int)
    i1 = np.floor((t[..., 0].max(axis=1) - jitter[0]) / h - 0.5).astype(int)
    j0 = np.ceil((t[..., 1].min(axis=1) - jitter[1]) / h - 0.5).astype(int)
    j1 = np.floor((t[..., 1].max(axis=1) - jitter[1]) / h - 0.5).astype(int)
    ni, nj = np.maximum(i1 - i0 + 1, 0), np.maximum(j1 - j0 + 1, 0)
    counts = ni * nj
    tri = np.repeat(np.arange(len(t)), counts)
    local = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
    ci = i0[tri] + local % ni[tri]
    cj = j0[tri] + local // ni[tri]
    px = (ci + 0.5) * h + jitter[0]
    py = (cj + 0.5) * h + jitter[1]

    # Barycentric point-in-triangle test in XY, then interpolate the hit height.
    a, b, c = t[tri, 0], t[tri, 1], t[tri, 2]
    v0, v1 = b - a, c - a
    v2 = np.stack([px, py], axis=1) - a
    den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        u = (v2[:, 0] * v1[:, 1] - v1[:, 0] * v2[:, 1]) / den
        v = (v0[:, 0] * v2[:, 1] - v2[:, 0] * v0[:, 1]) / den
    inside = (den != 0) & (u >= 0) & (v >= 0) & (u + v <= 1)
    z = (1 - u - v) * tz[tri, 0] + u * tz[tri, 1] + v * tz[tri, 2]
    ci, cj, z = ci[inside], cj[inside], z[inside]
    ok = (ci >= 0) & (ci < shape[0]) & (cj >= 0) & (cj < shape[1])
    ci, cj, z = ci[ok], cj[ok], z[ok]

    # Count hits below each voxel centre; odd count = inside.
    k = np.clip(np.ceil((z - origin[2]) / h - 0.5).astype(int), 0, shape[2])
    hits = np.zeros((shape[0], shape[1], shape[2] + 1), dtype=np.int32)
    np.add.at(hits, (ci, cj, k), 1)
    return (np.cumsum(hits, axis=2)[..., :-1] % 2) == 1


def depth(solid: np.ndarray, max_depth: int = 4) -> np.ndarray:
    """Voxel distance to the outside (1 = surface voxel), capped at ``max_depth``."""
    d = np.zeros(solid.shape, dtype=np.int8)
    cur = solid.copy()
    for level in range(1, max_depth + 1):
        p = np.pad(cur, 1)
        interior = cur & p[:-2, 1:-1, 1:-1] & p[2:, 1:-1, 1:-1] & p[1:-1, :-2, 1:-1] & p[1:-1, 2:, 1:-1] & p[1:-1, 1:-1, :-2] & p[1:-1, 1:-1, 2:]
        d[cur & ~interior] = level
        cur = interior
    d[cur] = max_depth
    return d
