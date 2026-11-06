"""Pictures of the part as PNG without a browser: for agents, the command line and the engine's report.

A small numpy software renderer: orthographic camera, flat shading, a z-buffer filled by
rasterising every triangle's bounding box at once (vectorised, in chunks), drawn at twice the
size and averaged down for smooth edges. PNG written with zlib. No new dependency, so it runs
in Pyodide too. Colours match the web app's 3D view (style.css / viewer.js).
"""

from __future__ import annotations

import struct
import zlib

import numpy as np

from .mesh import Mesh

# style.css: colours on the part
GREY, BORDERLINE, SUPPORT, BED_FACE = "#a3acb4", "#f0b429", "#e5484d", "#30a46c"
FIXED, FORCE, PROBLEM, PLATE, BACKGROUND = "#0090ff", "#8e4ec6", "#e5484d", "#d5dbd7", "#e9ecea"
CLASS_COLORS = [GREY, BORDERLINE, SUPPORT, BED_FACE]  # orientation.OK, BORDERLINE, NEEDS_SUPPORT, BED

VIEW_FROM = (1.0, -1.4, 1.0)  # front right, above: like the web app's start view
VIEW_BELOW = (1.0, -1.4, -0.7)  # overhangs face down: look at them from below
LIGHT = np.array([0.35, 0.55, 1.0]) / np.linalg.norm([0.35, 0.55, 1.0])  # camera frame
SUPERSAMPLE = 2
CHUNK = 3_000_000  # candidate pixels per rasterising step (memory)


def rgb(hex_color: str) -> np.ndarray:
    h = hex_color.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], float) / 255


def ramp(t: np.ndarray, bands: int = 10) -> np.ndarray:
    """The viewer's FEA colours in bands: blue (0) → cyan → green → yellow → red (1 and above)."""
    band = np.clip(np.floor(np.asarray(t, float) * bands), 0, bands - 1)
    hue = 0.66 * (bands - 1 - band) / (bands - 1)
    return _hsl(hue, 0.85, 0.5)


def _hsl(h: np.ndarray, s: float, l: float) -> np.ndarray:  # noqa: E741
    q = l + s - l * s if l >= 0.5 else l * (1 + s)
    p = 2 * l - q
    out = []
    for shift in (1 / 3, 0, -1 / 3):
        t = (h + shift) % 1.0
        out.append(np.select([t < 1 / 6, t < 1 / 2, t < 2 / 3], [p + (q - p) * 6 * t, q, p + (q - p) * (2 / 3 - t) * 6], p))
    return np.stack(out, axis=-1)


def camera(view_from=VIEW_FROM) -> np.ndarray:
    """Rotation world → camera (x right, y up, z towards the viewer), z up in the world stays up."""
    back = np.asarray(view_from, float) / np.linalg.norm(view_from)
    right = np.cross([0, 0, 1.0], back)
    right = right / np.linalg.norm(right) if np.linalg.norm(right) > 1e-9 else np.array([1.0, 0, 0])
    return np.stack([right, np.cross(back, right), back])


def draw(tris: np.ndarray, colors: np.ndarray, width: int = 800, height: int = 600, view_from=VIEW_FROM,
         focus: np.ndarray | None = None, margin: float = 0.06) -> np.ndarray:
    """RGB image (height, width, 3, uint8) of triangles (N, 3, 3) with one colour (N, 3) each. The view fits
    everything, or the ``focus`` box ([lo, hi] corners, world) when given."""
    cam = camera(view_from)
    pts = tris.reshape(-1, 3) @ cam.T
    fit = pts if focus is None else (np.array([[a, b, c] for a in focus[:, 0] for b in focus[:, 1] for c in focus[:, 2]]) @ cam.T)
    lo, hi = fit.min(axis=0), fit.max(axis=0)
    w, h = width * SUPERSAMPLE, height * SUPERSAMPLE
    scale = (1 - 2 * margin) * min(w / max(hi[0] - lo[0], 1e-9), h / max(hi[1] - lo[1], 1e-9))
    mid = (lo + hi) / 2
    sx = (pts[:, 0] - mid[0]) * scale + w / 2
    sy = h / 2 - (pts[:, 1] - mid[1]) * scale
    screen = np.stack([sx, sy, pts[:, 2]], axis=1).reshape(-1, 3, 3)

    # flat shading: light from the camera side, both sides of a face lit (open meshes, cut views)
    n = np.cross(screen[:, 1] - screen[:, 0], screen[:, 2] - screen[:, 0])
    cn = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]) @ cam.T
    cn /= np.maximum(np.linalg.norm(cn, axis=1, keepdims=True), 1e-12)
    shade = 0.5 + 0.5 * np.abs(cn @ LIGHT)
    face_rgb = np.clip(colors * shade[:, None], 0, 1)

    image = np.tile(rgb(BACKGROUND), (h * w, 1))
    depth = np.full(h * w, -np.inf)
    keep = np.abs(n[:, 2]) > 1e-9  # edge-on triangles cover no pixel
    screen, face_rgb = screen[keep], face_rgb[keep]
    x0 = np.clip(np.floor(screen[:, :, 0].min(axis=1)), 0, w - 1).astype(np.int64)
    x1 = np.clip(np.ceil(screen[:, :, 0].max(axis=1)), 0, w - 1).astype(np.int64)
    y0 = np.clip(np.floor(screen[:, :, 1].min(axis=1)), 0, h - 1).astype(np.int64)
    y1 = np.clip(np.ceil(screen[:, :, 1].max(axis=1)), 0, h - 1).astype(np.int64)
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    count = bw * bh
    start = 0
    while start < len(count):
        stop = start + max(1, int(np.searchsorted(np.cumsum(count[start:]), CHUNK)))
        _raster(screen[start:stop], face_rgb[start:stop], x0[start:stop], y0[start:stop], bw[start:stop], count[start:stop], w, image, depth)
        start = stop
    img = image.reshape(h, w, 3).reshape(height, SUPERSAMPLE, width, SUPERSAMPLE, 3).mean(axis=(1, 3))
    return (img * 255 + 0.5).astype(np.uint8)


def _raster(t, cols, x0, y0, bw, count, w, image, depth):
    """Fill the z-buffer with one chunk of triangles: every pixel centre in each bounding box, kept when
    the barycentric weights are all ≥ 0, the nearest one per pixel wins."""
    tri = np.repeat(np.arange(len(t)), count)
    local = np.arange(count.sum()) - np.repeat(np.cumsum(count) - count, count)
    px = x0[tri] + local % bw[tri] + 0.5
    py = y0[tri] + local // bw[tri] + 0.5
    a, b, c = t[tri, 0], t[tri, 1], t[tri, 2]
    area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    w0 = ((b[:, 0] - px) * (c[:, 1] - py) - (b[:, 1] - py) * (c[:, 0] - px)) / area
    w1 = ((c[:, 0] - px) * (a[:, 1] - py) - (c[:, 1] - py) * (a[:, 0] - px)) / area
    w2 = 1 - w0 - w1
    inside = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
    tri, px, py = tri[inside], px[inside], py[inside]
    z = (w0[inside] * a[inside, 2] + w1[inside] * b[inside, 2] + w2[inside] * c[inside, 2])
    pix = (py.astype(np.int64)) * w + px.astype(np.int64)
    order = np.lexsort((-z, pix))  # per pixel, nearest (largest z) first
    pix, z, tri = pix[order], z[order], tri[order]
    first = np.r_[True, pix[1:] != pix[:-1]]
    pix, z, tri = pix[first], z[first], tri[first]
    nearer = z > depth[pix]
    depth[pix[nearer]] = z[nearer]
    image[pix[nearer]] = cols[tri[nearer]]


def png(image: np.ndarray) -> bytes:
    """PNG bytes of an RGB uint8 image (no filter, zlib)."""
    h, w, _ = image.shape
    raw = b"".join(b"\x00" + image[y].tobytes() for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ---------- scenes ----------

def plate(tris: np.ndarray, margin: float = 0.15) -> np.ndarray:
    """A thin bed plate under the part (world z = 0), a little bigger than its footprint."""
    pts = tris.reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    pad = (hi[:2] - lo[:2]).max() * margin + 2
    (ax, ay), (bx, by) = lo[:2] - pad, hi[:2] + pad
    z = lo[2] - 0.05
    return np.array([[[ax, ay, z], [bx, ay, z], [bx, by, z]], [[ax, ay, z], [bx, by, z], [ax, by, z]]])


def arrow(start, direction, length: float) -> np.ndarray:
    """An arrow mesh (shaft and head) ending at ``start``, pointing along ``direction`` (a load pushing there)."""
    d = np.asarray(direction, float) / np.linalg.norm(direction)
    side = np.cross(d, [0, 0, 1.0] if abs(d[2]) < 0.9 else [1.0, 0, 0])
    side /= np.linalg.norm(side)
    up = np.cross(d, side)
    ring = lambda r, at: [at + r * (np.cos(a) * side + np.sin(a) * up) for a in np.linspace(0, 2 * np.pi, 13)[:-1]]  # noqa: E731
    tip = np.asarray(start, float)
    base = tip - d * length * 0.32
    tail = tip - d * length
    r_shaft, r_head = length * 0.09, length * 0.22
    head, s0, s1 = ring(r_head, base), ring(r_shaft, tail), ring(r_shaft, base)
    out = []
    for i in range(12):
        j = (i + 1) % 12
        out += [[tip, head[i], head[j]], [base, head[j], head[i]], [s0[i], s0[j], s1[j]], [s0[i], s1[j], s1[i]], [tail, s0[j], s0[i]]]
    return np.array(out)


def to_print(tris_part: np.ndarray, rotation: np.ndarray, part: Mesh) -> np.ndarray:
    """Part-frame triangles into the print frame the same way checks.orient moves the part."""
    t = part.triangles @ np.asarray(rotation).T
    lo, hi = t.reshape(-1, 3).min(axis=0), t.reshape(-1, 3).max(axis=0)
    return tris_part @ np.asarray(rotation).T - [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]]


VIEWS = ("bed", "overhangs", "problem", "stress", "warp", "compare")
WARP_SHOW = 0.05  # the curl is fractions of a mm: drawn so the biggest movement is this share of the part's size


def picture(view: str, up, profile: dict | None = None, loads: list[dict] | None = None, finding: int = 0,
            other: bytes | None = None, width: int = 800, height: int = 600) -> bytes:
    """PNG of the loaded part (api session) in print orientation ``up``. ``view``:
    "bed" the part on the bed; "overhangs" seen from below, faces coloured by what they need (red: support, amber:
    borderline, green: on the bed); "problem" a close-up of finding number ``finding`` from api.check
    (its area in red); "stress" the strength result for ``loads`` (blue: little stress, red: close to
    breaking; blue faces held, purple arrows the forces); "warp" the curled shape after cooling
    (exaggerated); "compare" before / after: ``other`` (STL / 3MF bytes of the earlier version) on the
    left, the loaded part on the right."""
    from . import api
    from .orientation import classify_faces
    from .placement import print_rotation
    from .profile import Profile

    if view not in VIEWS:
        raise ValueError(f"view must be one of {', '.join(VIEWS)}")
    s = api._require()
    p = Profile.from_dict(profile)
    rotation, _ = print_rotation(s.mesh, up, p.printer["bed"])
    part = to_print(s.mesh.triangles, rotation, s.mesh)
    colors = np.tile(rgb(GREY), (len(part), 1))
    extra: list[tuple[np.ndarray, str]] = []
    focus = None
    if view == "overhangs":
        classes = classify_faces(s.mesh, up, api.orientation_settings(p))
        colors = np.array([rgb(c) for c in CLASS_COLORS])[classes]
    elif view == "problem":
        findings = [f for f in api.check(up, profile)["findings"] if f.get("bbox")]
        if not findings:
            raise ValueError("No problem with a place on the part to show")
        lo, hi = (np.asarray(v, float) for v in findings[min(finding, len(findings) - 1)]["bbox"])
        c = part.mean(axis=1)
        pad = 0.5
        colors[np.all((c >= lo - pad) & (c <= hi + pad), axis=1)] = rgb(PROBLEM)
        size = max((hi - lo).max(), 10.0)
        focus = np.stack([(lo + hi) / 2 - size, (lo + hi) / 2 + size])
    elif view == "stress":
        if not loads:
            raise ValueError("The stress picture needs loads (a held face and a force)")
        r = api.strength(up, loads, profile)
        field = np.frombuffer(r["field_triangles"], np.float32).reshape(-1, 3, 3).astype(float)
        part = to_print(field, rotation, s.mesh)
        fi = np.frombuffer(r["vertex_fi"], np.float32).reshape(-1, 3).mean(axis=1)
        colors = ramp(fi / 100)
        extra = _load_marks(api, s, loads, rotation, part)
    elif view == "warp":
        r = api.warping(up, profile)
        field = np.frombuffer(r["field_triangles"], np.float32).reshape(-1, 3, 3).astype(float)
        disp = np.frombuffer(r["vertex_disp"], np.float32).reshape(-1, 3, 3).astype(float)
        size = np.ptp(field.reshape(-1, 3), axis=0).max()
        grow = WARP_SHOW * size / max(float(np.linalg.norm(disp, axis=-1).max()), 1e-9)
        part = to_print(field + disp * grow, rotation, s.mesh)
        value = np.frombuffer(r["vertex_value"], np.float32).reshape(-1, 3).mean(axis=1)
        colors = ramp(value / max(float(value.max()), 1e-9))
    elif view == "compare":
        if other is None:
            raise ValueError("The compare picture needs the earlier version (other)")
        from .formats import read_bodies

        before = max(read_bodies(bytes(other)), key=lambda b: abs(b[1].volume()))[1]
        old = to_print(before.triangles, rotation, before)
        gap = (np.ptp(part.reshape(-1, 3)[:, 0]) + np.ptp(old.reshape(-1, 3)[:, 0])) / 2 + 10
        extra = [(old - [gap, 0, 0], GREY)]
    scene = [part, *[t for t, _ in extra]]
    cols = [colors, *[np.tile(rgb(c), (len(t), 1)) for t, c in extra]]
    tris = np.concatenate(scene)
    if view in ("bed", "compare", "stress"):
        bed = plate(tris)
        tris, cols = np.concatenate([tris, bed]), [*cols, np.tile(rgb(PLATE), (2, 1))]
    return png(draw(tris, np.concatenate(cols), width, height, VIEW_BELOW if view == "overhangs" else VIEW_FROM, focus))


def _load_marks(api, s, loads, rotation, part) -> list[tuple[np.ndarray, str]]:
    """Held faces in blue and an arrow per force, in the print frame."""
    from .regions import find_regions, region_info

    if s.regions is None:
        s.regions = find_regions(s.mesh)
    info = {r["id"]: r for r in region_info(s.mesh, s.regions)}
    size = np.ptp(part.reshape(-1, 3), axis=0).max()
    out = []
    for load in loads:
        if "region" in load and load["region"] in info:
            at, normal = np.asarray(info[load["region"]]["centroid"]), np.asarray(info[load["region"]]["normal"])
        elif load.get("point") is not None:
            at, normal = np.asarray(load["point"], float), np.array([0, 0, 1.0])
        else:
            continue
        if load["kind"] == "fixed":
            if "region" in load:
                faces = s.mesh.triangles[np.asarray(s.regions) == load["region"]]
                out.append((to_print(faces, rotation, s.mesh) + 0.05 * (normal @ np.asarray(rotation).T), FIXED))
            continue
        d = load.get("direction", "push")
        vec = -normal if d == "push" else normal if d == "pull" else np.array([0, 0, -1.0]) if d == "down" else np.asarray(d, float)
        tip = to_print(at[None, None], rotation, s.mesh)[0, 0]
        start = tip if d != "pull" else tip + (vec @ np.asarray(rotation).T) * size * 0.4
        out.append((arrow(start, vec @ np.asarray(rotation).T, size * 0.4), FORCE))
    return out
