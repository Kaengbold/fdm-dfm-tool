"""Triangle mesh container and STL I/O.

Meshes are kept as a triangle soup: an (M, 3, 3) float array of triangle
corner positions. That matches STL directly and is all the orientation and
overhang checks need. Welding/topology comes later when feature detection needs it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Mesh:
    triangles: np.ndarray  # (M, 3, 3) float64
    _normals: np.ndarray | None = field(default=None, init=False, repr=False)
    _areas: np.ndarray | None = field(default=None, init=False, repr=False)
    _bounds: tuple | None = field(default=None, init=False, repr=False)
    _volume: float | None = field(default=None, init=False, repr=False)

    @property
    def face_count(self) -> int:
        return len(self.triangles)

    def normals_and_areas(self) -> tuple[np.ndarray, np.ndarray]:
        """Unit face normals (from winding, not the STL normal field) and areas. Cached;
        treat ``triangles`` as immutable."""
        if self._normals is None:
            t = self.triangles
            cross = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
            length = np.linalg.norm(cross, axis=1)
            self._normals = cross / np.where(length > 0, length, 1.0)[:, None]
            self._areas = 0.5 * length
        return self._normals, self._areas

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        """Cached (called many times per orientation search; 1M faces take ~0.1 s each)."""
        if self._bounds is None:
            pts = self.triangles.reshape(-1, 3)
            self._bounds = (pts.min(axis=0), pts.max(axis=0))
        lo, hi = self._bounds
        return lo.copy(), hi.copy()

    def volume(self) -> float:
        """Signed volume via divergence theorem; positive for outward-wound closed meshes. Cached."""
        if self._volume is None:
            t = self.triangles
            self._volume = float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)
        return self._volume


def load_stl(data: bytes) -> Mesh:
    """Parse binary or ASCII STL bytes."""
    count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else None
    if count and 84 + count * 50 == len(data):
        return _load_binary(data, count)
    if data.lstrip()[:5].lower() == b"solid" and b"facet" in data[:1000]:
        return _load_ascii(data)
    # Some exporters write binary STLs with trailing bytes or a header starting with "solid".
    if count and 84 + count * 50 <= len(data):
        return _load_binary(data, count)
    raise ValueError("Not a valid STL file")


def _load_binary(data: bytes, count: int) -> Mesh:
    record = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    arr = np.frombuffer(data, dtype=record, count=count, offset=84)
    return Mesh(arr["v"].astype(np.float64))


def _load_ascii(data: bytes) -> Mesh:
    verts = [
        line.split()[1:4]
        for line in data.decode("ascii", errors="replace").splitlines()
        if line.strip().startswith("vertex")
    ]
    if not verts or len(verts) % 3:
        raise ValueError("Malformed ASCII STL")
    return Mesh(np.asarray(verts, dtype=np.float64).reshape(-1, 3, 3))


def to_binary_stl(mesh: Mesh) -> bytes:
    normals, _ = mesh.normals_and_areas()
    record = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    arr = np.zeros(mesh.face_count, dtype=record)
    arr["normal"] = normals
    arr["v"] = mesh.triangles
    return b"\0" * 80 + struct.pack("<I", mesh.face_count) + arr.tobytes()


def _connected(n: int, edges: np.ndarray) -> np.ndarray:
    """Connected vertex labels, using vectorised hooking and path compression."""
    parent = np.arange(n)
    while len(edges):
        a, b = parent[edges[:, 0]], parent[edges[:, 1]]
        different = a != b
        if not different.any():
            break
        np.minimum.at(parent, np.maximum(a, b)[different], np.minimum(a, b)[different])
        while True:
            jumped = parent[parent]
            if np.array_equal(jumped, parent):
                break
            parent = jumped
    return parent


def mesh_health(mesh: Mesh) -> dict:
    """Inspect topology without changing the triangles. Points are in the loaded frame."""
    t = mesh.triangles
    _, areas = mesh.normals_and_areas()
    zero = areas == 0
    good = t[~zero]
    result = {"holes": 0, "boundary_edges": 0, "non_manifold_edges": 0,
              "flipped_faces": 0, "shells": 0, "zero_area_faces": int(zero.sum()),
              "points": {"holes": [], "non_manifold_edges": [], "flipped_faces": [],
                         "zero_area_faces": t[zero][:5].mean(axis=1).tolist()}}
    if not len(good):
        return result
    # Exact coordinate equality avoids silently welding gaps in the file.
    vertices, inverse = np.unique(good.reshape(-1, 3), axis=0, return_inverse=True)
    ids = inverse.reshape(-1, 3)
    directed = ids[:, [[0, 1], [1, 2], [2, 0]]].reshape(-1, 2)
    edges = np.sort(directed, axis=1)
    unique, inv, counts = np.unique(edges, axis=0, return_inverse=True, return_counts=True)
    boundary = unique[counts == 1]
    result["boundary_edges"] = int(len(boundary))
    result["non_manifold_edges"] = int((counts > 2).sum())
    result["points"]["non_manifold_edges"] = vertices[unique[counts > 2][:5]].mean(axis=1).tolist()
    result["shells"] = int(len(np.unique(_connected(len(vertices), unique))))
    if len(boundary):
        labels = _connected(len(vertices), boundary)[boundary[:, 0]]
        roots, inv_hole = np.unique(labels, return_inverse=True)
        result["holes"] = int(len(roots))
        centres = vertices[boundary].mean(axis=1)
        sums = np.zeros((len(roots), 3))
        np.add.at(sums, inv_hole, centres)
        result["points"]["holes"] = (sums / np.bincount(inv_hole)[:, None])[:5].tolist()
    # A reversed face usually disagrees with two or three neighbours. A single
    # disagreeing edge is ambiguous, so report both adjacent faces in that case.
    direction = np.where(directed[:, 0] < directed[:, 1], 1, -1)
    balance = np.bincount(inv, weights=direction, minlength=len(unique))
    bad = (counts == 2) & (np.abs(balance) == 2)
    votes = np.bincount(np.repeat(np.arange(len(good)), 3)[bad[inv]], minlength=len(good))
    flipped = votes >= 2
    if bad.any() and not flipped.any():
        flipped = votes > 0
    result["flipped_faces"] = int(flipped.sum())
    result["points"]["flipped_faces"] = good[flipped][:5].mean(axis=1).tolist()
    return result
