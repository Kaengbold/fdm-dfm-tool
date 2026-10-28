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
        pts = self.triangles.reshape(-1, 3)
        return pts.min(axis=0), pts.max(axis=0)

    def volume(self) -> float:
        """Signed volume via divergence theorem; positive for outward-wound closed meshes."""
        t = self.triangles
        return float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)


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
