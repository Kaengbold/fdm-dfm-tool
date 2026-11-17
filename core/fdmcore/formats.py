"""Reading part files (STL, 3MF), splitting them into bodies and spotting wrong units.

A file often holds more than one part (a 3MF from a slicer with several objects, an STL
exported with a whole print plate). Orientation and checks only make sense per part,
so ``read_bodies`` returns the separate bodies and the user picks one.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

import numpy as np

from .mesh import Mesh, load_stl, vertex_ids

# 3MF ``unit`` attribute -> millimetres.
UNITS_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
MAX_BODIES = 30
# A file that unpacks to more than this (a "zip bomb", or a 3MF that places the same object
# thousands of times) would freeze the tab or crash the local MCP server before any check runs.
MAX_UNPACKED_MB = 400
MAX_FACES = 4_000_000


def too_many_faces(n: int) -> ValueError:
    return ValueError(f"This file has {n:,} triangles, more than the {MAX_FACES:,} this tool can handle. "
                      "Export it with a coarser resolution (fewer triangles) and try again")


def read_zip(z: zipfile.ZipFile, name: str) -> bytes:
    """One file from a zip, refusing anything that unpacks to more than MAX_UNPACKED_MB. The size in
    the zip header can lie, so at most one byte past the limit is ever unpacked."""
    limit = MAX_UNPACKED_MB * 2**20
    info = z.getinfo(name)
    data = b""
    if info.file_size <= limit:
        with z.open(info) as fh:
            data = fh.read(limit + 1)
    if info.file_size > limit or len(data) > limit:
        raise ValueError(f"This file unpacks to more than {MAX_UNPACKED_MB} MB, too big to open here")
    return data


def read_bodies(data: bytes) -> list[tuple[str, Mesh]]:
    """(name, mesh) per part in the file; a single entry for an ordinary one-part file."""
    if data[:2] == b"PK":
        objects = read_3mf(data)
        if len(objects) > 1:
            return objects
        name, mesh = objects[0]
    else:
        name, mesh = "", load_stl(data)
    if mesh.face_count > MAX_FACES:
        raise too_many_faces(mesh.face_count)
    bodies = split_bodies(mesh)
    if len(bodies) == 1:
        return [(name, mesh)]
    return [(f"{name or 'Body'} {i + 1}", b) for i, b in enumerate(bodies)]


# ---------- 3MF ----------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(el, name: str):
    """Attribute by local name, ignoring namespaces (the production extension prefixes ``path``)."""
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return None


def _transform(text: str | None) -> np.ndarray:
    """3MF 3×4 matrix (row-vector convention) as a 4×3 array: p' = [p 1] @ M."""
    if not text:
        return np.vstack([np.eye(3), np.zeros(3)])
    return np.array(text.split(), float).reshape(4, 3)


def _apply(tri: np.ndarray, m: np.ndarray) -> np.ndarray:
    return tri @ m[:3] + m[3]


def read_3mf(data: bytes) -> list[tuple[str, Mesh]]:
    """Every build item of a 3MF as its own mesh (components and transforms applied, in mm)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("Not a valid 3MF file") from e
    root_path = _root_model(z)
    docs: dict[str, ET.Element] = {}

    def doc(path: str) -> ET.Element:
        path = path.lstrip("/")
        if path not in docs:
            try:
                docs[path] = ET.fromstring(read_zip(z, path))
            except KeyError as e:
                raise ValueError(f"3MF file is missing {path}") from e
        return docs[path]

    root = doc(root_path)
    scale = UNITS_MM.get(root.get("unit", "millimeter"), 1.0)
    objects: dict[tuple[str, str], ET.Element] = {}
    counts: dict[tuple[str, str], int] = {}

    def face_count(path: str, oid: str, depth: int = 0) -> int:
        """Triangles an object expands to, counted before any are built: components that place one
        object many times multiply it, which is how a small 3MF can unpack to billions."""
        key = (path, oid)
        if key not in counts:
            if depth > 16:
                raise ValueError("3MF components nest too deep")
            n = 0
            for child in find_object(path, oid):
                kind = _local(child.tag)
                if kind == "mesh":
                    n += sum(len(p) for p in child if _local(p.tag) == "triangles")
                elif kind == "components":
                    n += sum(face_count((_attr(c, "path") or path).lstrip("/"), c.get("objectid"), depth + 1) for c in child)
            counts[key] = n
        return counts[key]

    def find_object(path: str, oid: str) -> ET.Element:
        key = (path, oid)
        if key not in objects:
            for el in doc(path).iter():
                if _local(el.tag) == "object":
                    objects[(path, el.get("id"))] = el
        if key not in objects:
            raise ValueError(f"3MF object {oid} not found")
        return objects[key]

    def triangles(path: str, oid: str, depth: int = 0) -> list[np.ndarray]:
        if depth > 16:
            raise ValueError("3MF components nest too deep")
        obj = find_object(path, oid)
        out = []
        for child in obj:
            kind = _local(child.tag)
            if kind == "mesh":
                out.append(_mesh_triangles(child))
            elif kind == "components":
                for comp in child:
                    sub = (_attr(comp, "path") or path).lstrip("/")
                    m = _transform(comp.get("transform"))
                    out += [_apply(t, m) for t in triangles(sub, comp.get("objectid"), depth + 1)]
        return out

    bodies = []
    build = next((el for el in root if _local(el.tag) == "build"), None)
    items = list(build) if build is not None else []
    total = sum(face_count(root_path, item.get("objectid")) for item in items)
    if total > MAX_FACES:
        raise too_many_faces(total)
    for item in items:
        oid = item.get("objectid")
        parts = triangles(root_path, oid)
        if not parts:
            continue
        tri = _apply(np.concatenate(parts), _transform(item.get("transform"))) * scale
        name = find_object(root_path, oid).get("name") or f"Object {oid}"
        bodies.append((name, Mesh(tri)))
    if not bodies:
        raise ValueError("No printable objects in this 3MF file")
    return bodies[:MAX_BODIES]


def _root_model(z: zipfile.ZipFile) -> str:
    try:
        rels = read_zip(z, "_rels/.rels").decode("utf-8", "replace")
        m = re.search(r'Target="([^"]+)"[^>]*Type="[^"]*3dmodel"|Type="[^"]*3dmodel"[^>]*Target="([^"]+)"', rels)
        if m:
            return posixpath.normpath((m.group(1) or m.group(2)).lstrip("/"))
    except KeyError:
        pass
    return "3D/3dmodel.model"


def _mesh_triangles(mesh_el: ET.Element) -> np.ndarray:
    verts, tris = [], []
    for part in mesh_el:
        kind = _local(part.tag)
        if kind == "vertices":
            verts = [(v.get("x"), v.get("y"), v.get("z")) for v in part]
        elif kind == "triangles":
            tris = [(t.get("v1"), t.get("v2"), t.get("v3")) for t in part]
    if not tris:
        return np.zeros((0, 3, 3))
    return np.asarray(verts, float)[np.asarray(tris, int)]


# ---------- bodies ----------
def split_bodies(mesh: Mesh, min_fraction: float = 0.002) -> list[Mesh]:
    """Connected pieces of the mesh, largest first. Inner shells (cavities) stay with the
    body around them, and crumbs below ``min_fraction`` of the total volume are dropped."""
    labels = _components(mesh.triangles)
    n = labels.max() + 1
    if n == 1:
        return [mesh]
    t = mesh.triangles
    signed = np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])) / 6.0
    vol = np.bincount(labels, signed, n)
    if vol.sum() < 0:  # every shell wound inward (inverted-normal STL): flip, so bodies count as solid
        signed, vol = -signed, -vol
    pts = t.reshape(-1, 3)
    lab3 = np.repeat(labels, 3)
    lo = np.full((n, 3), np.inf)
    hi = np.full((n, 3), -np.inf)
    np.minimum.at(lo, lab3, pts)
    np.maximum.at(hi, lab3, pts)

    # Shells wound inward (negative volume) are cavities: give them to the smallest body containing them.
    solid = np.flatnonzero(vol > 0)
    owner = np.arange(n)
    for c in np.flatnonzero(vol <= 0):
        inside = [s for s in solid if np.all(lo[s] <= lo[c] + 1e-6) and np.all(hi[s] >= hi[c] - 1e-6)]
        if inside:
            owner[c] = min(inside, key=lambda s: vol[s])
    labels = owner[labels]
    total = np.bincount(labels, signed, n)
    keep = [c for c in np.argsort(-total) if total[c] > min_fraction * total.max()]
    if len(keep) == 1 and np.all(labels == keep[0]):
        return [mesh]
    return [Mesh(t[labels == c]) for c in keep[:MAX_BODIES]]


def _components(tri: np.ndarray) -> np.ndarray:
    """Component label per triangle; triangles sharing a corner (exact float32 match) connect."""
    vid = vertex_ids(tri)
    parent = np.arange(vid.max() + 1)
    a = np.concatenate([vid[:, 0], vid[:, 0]])
    b = np.concatenate([vid[:, 1], vid[:, 2]])
    # Hook each edge's larger root onto the smaller one, then compress paths; repeat until stable.
    while True:
        pa, pb = parent[a], parent[b]
        diff = pa != pb
        if not diff.any():
            break
        np.minimum.at(parent, np.maximum(pa, pb)[diff], np.minimum(pa, pb)[diff])
        while True:
            jumped = parent[parent]
            if np.array_equal(jumped, parent):
                break
            parent = jumped
    _, labels = np.unique(parent[vid[:, 0]], return_inverse=True)
    return labels.ravel()


# ---------- units ----------
def unit_hint(size_mm, largest_bed_mm: float = 400.0) -> dict | None:
    """Suggest a scale factor when the size looks like the wrong unit (inch, metre, micron).
    Only a hint: small parts are real too, so the user decides. Inches and centimetres (Blender,
    SketchUp) can't be told apart by size, so an inch hint also offers ×10 in ``also``."""
    biggest = float(max(size_mm))
    if biggest <= 0:
        return None
    for factor, unit, lo, hi in ((25.4, "inches", 0.5, np.inf), (1000.0, "metres", 0.0, 0.5), (0.001, "microns", 5000.0, np.inf)):
        if lo <= biggest < hi and 3.0 <= biggest * factor <= largest_bed_mm:
            return {
                "factor": factor,
                "unit": unit,
                "text": f"Only {biggest:.3g} mm big" if factor > 1 else f"{biggest:.0f} mm big",
                "scaled_mm": round(biggest * factor, 1),
                "also": {"factor": 10.0, "unit": "centimetres", "scaled_mm": round(biggest * 10, 1)}
                if factor == 25.4 and biggest * 10 >= 20 else None,
            }
    return None
