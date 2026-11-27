from __future__ import annotations

import numpy as np

from ..formats import read_bodies
from ..mesh import Mesh
from ..orientation import rotation_to_z
from ..placement import print_rotation
from ..profile import Profile
from ..recommend import goal_plan
from . import design as _design
from . import load as _load
from . import orientation as _orient
from . import session as _S


def _zone_info(z, mesh: Mesh, up, bed, moving: bool = False) -> dict:
    """A zone for the UI and the export: its box (build frame), settings and corners on the bed."""
    from itertools import product

    r0 = rotation_to_z(up)
    rot = print_rotation(mesh, up, bed, moving)[0]
    corners = np.array(list(product(*zip(z.lo, z.hi)))) @ r0  # build frame -> part
    return {"lo": list(z.lo), "hi": list(z.hi), "walls": z.walls, "infill": z.infill, "shape": z.shape,
            "corners_print": [_design._to_print_frame(mesh, rot, c) for c in corners]}


ZONE_MARGIN_MM = 3.0  # a zone reaches this far past the picked face, so the slicer's walls there are inside it


def zone_at(up, region: int | None = None, point=None, margin: float = ZONE_MARGIN_MM, walls: int | None = None,
            infill: float | None = None, profile: dict | None = None, shape: str | None = None) -> dict:
    """A zone the user picks: an upright box around a face (``region``) or a spot (``point``, part
    coordinates), ``margin`` mm bigger on every side, printed with more walls and infill (default
    two more walls and 40 %). Same form as the optimiser's zones, so strength and export take it.
    ``shape`` "box", "cylinder" or "ball" (strength.ZONE_SHAPES); default a cylinder around a round
    hole or post that stands upright, else a box."""
    from ..regions import find_regions
    from ..strength import ZONE_SHAPES, Zone

    if shape is not None and shape not in ZONE_SHAPES:
        raise ValueError(f"shape must be one of {ZONE_SHAPES}")

    s = _S._require()
    p = Profile.from_dict(profile)
    if region is not None:
        if s.regions is None:
            s.regions = find_regions(s.mesh)
        pts = s.mesh.triangles[np.asarray(s.regions) == int(region)].reshape(-1, 3)
        if not len(pts):
            raise ValueError(f"Unknown face {region}")
    elif point is not None:
        pts = np.asarray([point], float)
    else:
        raise ValueError("Give a face (region) or a point")
    built = pts @ rotation_to_z(up).T
    plan = goal_plan(p)
    if shape is None:
        shape = "cylinder" if region is not None and _upright_round(int(region), up) else "box"
    lo, hi = built.min(axis=0) - margin, built.max(axis=0) + margin
    if shape != "box":  # the round shape sits inside its box: grow the box so it still reaches past the face
        grow = (hi - lo) * (np.sqrt(2) - 1) / 2 if shape == "ball" else np.r_[(hi - lo)[:2] * (np.sqrt(2) - 1) / 2, 0.0]
        if region is not None and shape == "cylinder" and _upright_round(int(region), up):
            grow[:2] = 0.0  # a circle around a round face already fits it
        lo, hi = lo - grow, hi + grow
    z = Zone(tuple(lo.tolist()), tuple(hi.tolist()),
             int(walls if walls is not None else plan["walls"] + 2), float(infill if infill is not None else max(0.4, plan["infill"] / 100)),
             shape=shape)
    return _zone_info(z, s.mesh, up, p.printer["bed"], _orient._moving(p))


VOLUME_KINDS = {"support": "Support here", "blocker": "No support here", "void": "Hidden gap (extra walls)", "keep_clear": "Keep clear",
                "add_solid": "Add plastic", "cut_solid": "Cut away"}


SOLID_KINDS = ("add_solid", "cut_solid")  # change the model itself (volume_solid), not for the slicer


DESIGN_ONLY = ("keep_clear", *SOLID_KINDS)  # not in the 3MF


VOID_MM = 0.6  # a gap picked on a spot: a thin upright pin hole, the "holes that force walls" trick


VOID_COVER_MM = 1.0  # plastic left above and below such a pin hole, so it doesn't show


# Support / no support on a picked face: only that face, not 3 mm round it (T-363), reaching where the
# supports touching it are: down to the bed under a face that faces down, up to the top over one that
# faces up, ZONE_MARGIN_MM out from a side face.
FACE_SUPPORT_MARGIN_MM = 0.5


def volume_at(up, kind: str, region: int | None = None, point=None, point2=None, margin: float | None = None,
              profile: dict | None = None, shape: str = "box", normal=None) -> dict:
    """An advanced design box, upright in the build frame like a zone: around a face (``region``),
    a spot (``point``) or between two spots (``point`` and ``point2``, part coordinates), ``margin``
    mm bigger on every side. ``kind``: "support" / "blocker" (support enforcer / blocker in the 3MF),
    "void" (a hidden gap: a negative volume the slicer prints walls round; on one spot a thin pin
    hole up through the part that stays inside it), "keep_clear" (space that must stay empty; not
    exported), "add_solid" / "cut_solid" (a block of plastic added to or cut from the model itself: see
    volume_solid). Returns the box, its corners on the bed and plain notes on what it does here. ``shape``:
    "box", "cylinder" or "ball" inside that box (strength.ZONE_SHAPES). ``normal``: the clicked face's outward
    normal (part coordinates), so a pin hole picked on a side face goes into the wall, not along its surface."""
    from ..regions import find_regions

    if kind not in VOLUME_KINDS:
        raise ValueError(f"Unknown kind {kind!r}: use one of {', '.join(VOLUME_KINDS)}")
    s = _S._require()
    p = Profile.from_dict(profile)
    r0 = rotation_to_z(up)
    if region is not None:
        if s.regions is None:
            s.regions = find_regions(s.mesh)
        pts = s.mesh.triangles[np.asarray(s.regions) == int(region)].reshape(-1, 3)
        if not len(pts):
            raise ValueError(f"Unknown face {region}")
    elif point is not None:
        pts = np.asarray([point] + ([point2] if point2 is not None else []), float)
    else:
        raise ValueError("Give a face (region) or a point")
    built = pts @ r0.T
    zs = (s.mesh.triangles.reshape(-1, 3) @ r0.T)[:, 2]
    pin = kind == "void" and region is None and point2 is None
    on_face = kind in ("support", "blocker") and region is not None
    if margin is None:
        margin = VOID_MM / 2 if pin else FACE_SUPPORT_MARGIN_MM if on_face else ZONE_MARGIN_MM
    inside = _inside_test(s.mesh, r0)
    surface_n = _surface_normal(s.mesh, pts[0]) if pin else None
    if pin:  # into the wall: the vertical pin can't run along a side face
        face_n = surface_n if surface_n is not None else (np.asarray(normal, float) if normal is not None else None)
        if face_n is not None:
            n = r0 @ face_n
            side = np.hypot(n[0], n[1])
            if side > 0.3:
                built = built - [*(n[:2] / side * (VOID_COVER_MM + VOID_MM / 2)), 0.0]
    lo, hi = built.min(axis=0) - margin, built.max(axis=0) + margin
    if on_face:
        _reach_supports(s.mesh, int(region), r0, lo, hi, zs)
    if pin:
        lo[2], hi[2] = _solid_run(inside, built[0], zs.min(), zs.max(), on_surface=surface_n is not None)
    return {**_volume_box(up, kind, lo, hi, shape, p, inside), "margin": float(margin)}


def _volume_box(up, kind: str, lo, hi, shape: str, p: Profile, inside=None) -> dict:
    """An advanced design box (build frame ``lo`` / ``hi``) with its corners on the bed and its notes.
    Slicer boxes stay between the bed and the top of the part (see _zone_box); keep-clear space may be above it."""
    from ..strength import ZONE_SHAPES

    s = _S._require()
    r0 = rotation_to_z(up)
    zs = (s.mesh.triangles.reshape(-1, 3) @ r0.T)[:, 2]
    lo, hi = np.array(lo, float), np.array(hi, float)
    lo[2], hi[2] = max(lo[2], zs.min()), (hi[2] if kind in DESIGN_ONLY else min(hi[2], zs.max()))
    if np.any(hi - lo <= 0):
        raise ValueError("That box is outside the part's height")
    if shape not in ZONE_SHAPES:
        raise ValueError(f"shape must be one of {ZONE_SHAPES}")
    box = {"kind": kind, "lo": lo.tolist(), "hi": hi.tolist(), "walls": 0, "infill": 0.0, "shape": shape}
    corners = _zone_info(_box_zone(box), s.mesh, up, p.printer["bed"], _orient._moving(p))["corners_print"]
    notes = _volume_notes(s.mesh, r0, inside if inside is not None else _inside_test(s.mesh, r0), box, p)
    return {**box, "corners_print": corners, "notes": notes}


def volume_move(up, box: dict, by, profile: dict | None = None) -> dict:
    """An advanced design box (volume_at) moved ``by`` mm (part coordinates): the user dragged it in the
    3D view. Same size; its height still stops at the bed and the top of the part, and the notes say
    what it does in its new place."""
    if box.get("kind") not in VOLUME_KINDS:
        raise ValueError(f"Unknown kind {box.get('kind')!r}: use one of {', '.join(VOLUME_KINDS)}")
    shift = rotation_to_z(up) @ np.asarray(by, float)  # part -> build frame
    lo, hi = np.asarray(box["lo"], float) + shift, np.asarray(box["hi"], float) + shift
    moved = _volume_box(up, box["kind"], lo, hi, box.get("shape", "box"), Profile.from_dict(profile))
    return {**moved, **({"margin": box["margin"]} if "margin" in box else {})}


def volume_solid(up, boxes: list[dict]) -> list[dict]:
    """The "add plastic" / "cut away" boxes (volume_at) as design edits in part coordinates, for the
    web app to apply like any design change (a new version of the model)."""
    from ..features import _intent, _prism_shape

    r0 = rotation_to_z(up)
    unit = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)
    faces = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    out = []
    for i, b in enumerate(boxes):
        if b.get("kind") not in SOLID_KINDS:
            continue
        lo, hi = np.asarray(b["lo"], float), np.asarray(b["hi"], float)
        tris = ((unit * (hi - lo) + lo)[faces]) @ r0  # build frame -> part
        add = b["kind"] == "add_solid"
        size = " × ".join(f"{v:.0f}" for v in hi - lo)
        bottom = (unit[:4] * (hi - lo) + lo) @ r0
        out.append({"id": f"solid-{i}", "rule": "design_change", "title": f"{'Add' if add else 'Cut away'} a {size} mm block",
                    "description": "Placed under Advanced design changes.", "op": "add" if add else "cut", "param": None,
                    "mesh": tris.astype("<f4").tobytes(), "pieces": [len(tris)], "placed_by_user": True,
                    "intent": _load._cad_refs(_intent("slab" if add else "void", {"size_mm": (hi - lo).tolist()},
                                                [_prism_shape(bottom, np.array([0, 0, hi[2] - lo[2]]) @ r0, add=add)]))})
    return out


SNAP_SINK_MM = 0.05  # the snapped model reaches this far into the part, so the union joins them


def snap_model(data: bytes, point, normal, spin_deg: float = 0.0, name: str = "other part") -> dict:
    """Put another model (STL or 3MF bytes) onto the part: its biggest flat face goes flat onto
    the part's face at ``point`` (part coordinates, ``normal`` pointing out of the part), centred there
    and turned ``spin_deg`` about that normal. Returns a design edit (``op: "add"``) the web app joins
    to the part as a new version: an assembly made into one print, or a bracket stuck onto a box."""
    from ..regions import find_regions, region_info

    other = max(read_bodies(bytes(data)), key=lambda b: abs(b[1].volume()))[1]
    info = region_info(other, find_regions(other))
    face = max((r for r in info if r["kind"] == "flat"), key=lambda r: r["area_mm2"], default=None)
    if face is None:
        raise ValueError("The other model has no flat face to put down")
    n_to = -np.asarray(normal, float) / np.linalg.norm(normal)  # its face looks into the part
    turn = _rotation_between(np.asarray(face["normal"], float), n_to)
    spin = _axis_rotation(n_to, np.radians(spin_deg))
    tris = (other.triangles - np.asarray(face["centroid"], float)) @ (spin @ turn).T + np.asarray(point, float) + n_to * SNAP_SINK_MM
    lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
    return {"id": "snap", "rule": "design_change", "title": f"Add {name}",
            "description": f"Its biggest flat face ({face['area_mm2']:.0f} mm²) sits on the clicked face; it is {hi[0] - lo[0]:.0f} × {hi[1] - lo[1]:.0f} × {hi[2] - lo[2]:.0f} mm.",
            "op": "add", "param": None, "mesh": tris.astype("<f4").tobytes(), "pieces": [len(tris)], "placed_by_user": True}


def _rotation_between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Rotation matrix turning unit vector ``a`` onto ``b`` (a half turn about any perpendicular when opposite)."""
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v, c = np.cross(a, b), float(a @ b)
    if c < -0.999999:
        p = np.cross(a, [1.0, 0, 0] if abs(a[0]) < 0.9 else [0, 1.0, 0])
        return _axis_rotation(p / np.linalg.norm(p), np.pi)
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + k + k @ k / (1 + c)


def _axis_rotation(axis: np.ndarray, angle: float) -> np.ndarray:
    axis = axis / np.linalg.norm(axis)
    k = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + np.sin(angle) * k + (1 - np.cos(angle)) * k @ k


def _keep_clear_clash(edits: list[dict], up, keep_clear: list[dict] | None) -> list[dict]:
    """Design edits that add plastic into a keep-clear box get ``clash`` (plain words) so the app
    leaves them unticked: the user said that space has to stay empty."""
    if not keep_clear:
        return edits
    r0 = rotation_to_z(up)
    for e in edits:
        if e["op"] != "add" and "add" not in e.get("piece_ops", []):
            continue
        tris = np.frombuffer(e["mesh"], "<f4").reshape(-1, 3, 3).astype(float)
        pts = np.concatenate([tris.reshape(-1, 3), tris.mean(axis=1), (tris + np.roll(tris, 1, axis=1)).reshape(-1, 3) / 2]) @ r0.T
        for b in keep_clear:
            if np.all((pts >= np.asarray(b["lo"]) - 1e-6) & (pts <= np.asarray(b["hi"]) + 1e-6), axis=1).any():
                e["clash"] = "It reaches into a space you marked to keep clear, so it is not ticked."
                break
    return edits


def _box_zone(box: dict):
    """A zone or advanced design box (dict) as the strength model's Zone."""
    from ..strength import Zone

    return Zone(tuple(box["lo"]), tuple(box["hi"]), int(box.get("walls", 0)), float(box.get("infill", 0)), box.get("kind") == "void",
                box.get("shape", "box"))


def _upright_round(region: int, up) -> bool:
    """Is face ``region`` a round hole or post whose axis stands upright for this build direction?"""
    from ..features import accepted

    for f in accepted(_load._features()):
        if f["region"] == region:
            return abs(np.dot(f["axis"], np.asarray(up, float) / np.linalg.norm(up))) > 0.98
    return False


def _inside_test(mesh: Mesh, r0: np.ndarray):
    """Is a point (build frame) inside the part? A voxel test: fine enough for sizes and notes."""
    from ..voxel import voxelize

    grid = voxelize(mesh, 60_000)

    def inside(built):
        idx = grid.index_of(np.asarray(built, float) @ r0)
        ok = np.all((idx >= 0) & (idx < np.array(grid.solid.shape)), axis=1)
        out = np.zeros(len(idx), dtype=bool)
        out[ok] = grid.solid[tuple(idx[ok].T)]
        return out

    inside.h = grid.h
    return inside


def _reach_supports(mesh: Mesh, region: int, r0: np.ndarray, lo: np.ndarray, hi: np.ndarray, zs: np.ndarray) -> None:
    """Grow a support / no-support box on a face (in place) to where the supports touching that face are:
    down to the bed under a face that faces down, up to the top over one facing up, out from a side face."""
    s = _S._require()
    normals, areas = mesh.normals_and_areas()
    sel = np.asarray(s.regions) == region
    n = r0 @ ((normals[sel] * areas[sel, None]).sum(axis=0))
    n /= max(np.linalg.norm(n), 1e-12)
    if n[2] < -0.3:
        lo[2] = zs.min()
    elif n[2] > 0.3:
        hi[2] = zs.max()
    side = np.hypot(n[0], n[1])
    if side > 0.3:
        for k in (0, 1):
            if n[k] > 0.3 * side:
                hi[k] += ZONE_MARGIN_MM
            elif n[k] < -0.3 * side:
                lo[k] -= ZONE_MARGIN_MM


def _surface_normal(mesh: Mesh, pt: np.ndarray, tol: float = 0.1) -> np.ndarray | None:
    """Outward unit normal at a surface point ``pt`` (part coordinates), or None if farther than ``tol``
    from the surface. Triangles meeting at an edge or corner are averaged so clicks there step inward."""
    from ..strength import _distance_to_triangles

    t = mesh.triangles
    near = np.flatnonzero(np.all((pt >= t.min(axis=1) - tol) & (pt <= t.max(axis=1) + tol), axis=1))
    if not len(near):
        return None
    d = _distance_to_triangles(t[near], pt)
    best = float(d.min())
    if best > tol:
        return None
    normals, _ = mesh.normals_and_areas()
    n = normals[near[d <= best + 1e-4]].sum(axis=0)
    length = float(np.linalg.norm(n))
    return n / length if length > 1e-9 else None


def _solid_run(inside, at: np.ndarray, zmin: float, zmax: float, on_surface: bool = False) -> tuple[float, float]:
    """Heights of the solid run straight up and down through ``at`` (build frame), less
    ``VOID_COVER_MM`` at both ends: where a pin hole can go without showing."""
    zs = np.linspace(zmin, zmax, 400)
    solid = inside(np.column_stack([np.full_like(zs, at[0]), np.full_like(zs, at[1]), zs]))
    if not solid.any():
        raise ValueError("The part is too thin there for a hidden gap" if on_surface else "That spot isn't on the part")
    rows = np.flatnonzero(solid)
    k = int(rows[np.argmin(np.abs(zs[rows] - at[2]))])  # a click on the surface: the nearest solid point
    a = b = k
    while a > 0 and solid[a - 1]:
        a -= 1
    while b < len(zs) - 1 and solid[b + 1]:
        b += 1
    lo, hi = zs[a] + VOID_COVER_MM, zs[b] - VOID_COVER_MM
    if hi <= lo:
        raise ValueError("The part is too thin there for a hidden gap")
    return lo, hi


SHAPE_VOLUME = {"box": 1.0, "cylinder": np.pi / 4, "ball": np.pi / 6}  # share of the box it fills


def _volume_notes(mesh: Mesh, r0: np.ndarray, inside, box: dict, p: Profile) -> list[str]:
    """What an advanced design box does on this part, in plain words."""
    lo, hi = np.asarray(box["lo"]), np.asarray(box["hi"])
    kind, zone = box["kind"], _box_zone(box)
    if kind in ("void", "keep_clear"):
        n = np.clip(np.ceil((hi - lo) / (inside.h / 2)).astype(int) + 1, 2, 40)
        g = np.stack(np.meshgrid(*[np.linspace(lo[i], hi[i], n[i]) for i in range(3)], indexing="ij"), -1).reshape(-1, 3)
        g = g[zone.gap(g) <= 1e-9]
        filled = inside(g)
        if kind == "keep_clear":
            if filled.mean() < 0.01:
                return ["The part stays out of it."]
            volume = float(np.prod(hi - lo)) * SHAPE_VOLUME[zone.shape]
            return [f"The part fills about {filled.mean():.0%} of this space ({filled.mean() * volume / 1000:.1f} cm³): "
                    "change the design to clear it."]
        notes = ["The slicer prints walls all round it, so the plastic next to it is solid: stronger there, a little heavier. "
                 "The strength check counts it (roughly)."]
        c, r = (lo + hi) / 2, (hi - lo) / 2
        rim = _box_zone({**box, "lo": c - 0.9 * r, "hi": c + 0.9 * r}).gap(g) > 1e-9  # the outer tenth of the shape
        if not filled[rim].all():
            notes.append("It reaches the outside of the part, so it shows as a hole in the surface. Keep it inside if it should stay hidden.")
        return notes
    if kind in SOLID_KINDS:
        size = float(np.prod(hi - lo))
        n = np.clip(np.ceil((hi - lo) / (inside.h / 2)).astype(int) + 1, 2, 40)
        g = np.stack(np.meshgrid(*[np.linspace(lo[i], hi[i], n[i]) for i in range(3)], indexing="ij"), -1).reshape(-1, 3)
        filled = float(inside(g).mean())
        if kind == "add_solid":
            new = (1 - filled) * size / 1000
            if filled == 0:
                return [f"Adds about {new:.1f} cm³ of plastic, but it doesn't touch the part: move it so it overlaps."]
            return [f"Adds about {new:.1f} cm³ of plastic. Apply it to make a new version of the model (Undo goes back)."]
        if filled < 0.01:
            return ["The part isn't in this box: nothing to cut."]
        return [f"Cuts away about {filled * size / 1000:.1f} cm³ of the part. Apply it to make a new version of the model (Undo goes back)."]
    normals, areas = mesh.normals_and_areas()
    c = mesh.triangles.mean(axis=1) @ r0.T
    inbox = zone.gap(c) <= 1e-9
    steep = (normals @ r0.T)[:, 2] < -np.cos(np.radians(p.material["max_overhang_deg"]))
    above_bed = c[:, 2] > (mesh.triangles.reshape(-1, 3) @ r0.T)[:, 2].min() + 0.2
    hang = float(areas[inbox & steep & above_bed].sum())
    if kind == "support":
        return ["Supports are printed here, even with the slicer's automatic supports off."
                + ("" if hang > 1 else " Nothing here overhangs much: check it's the right place.")]
    if hang > 1:
        return [f"About {hang:.0f} mm² here hangs too far out to print in the air: it may sag or fail without support."]
    return ["No support is printed here."]


def _zone_box(mesh: Mesh, up, rotation: np.ndarray, zone: dict) -> np.ndarray:
    """A zone's box as triangles, in the frame of the oriented part (see orient()), kept between the
    bed and the top of the part: a modifier reaching below the bed lifts the part in PrusaSlicer."""
    r0 = rotation_to_z(up)
    zs = (mesh.triangles.reshape(-1, 3) @ r0.T)[:, 2]
    lo, hi = np.array(zone["lo"], float), np.array(zone["hi"], float)
    lo[2], hi[2] = max(lo[2], zs.min()), min(hi[2], zs.max())
    part = (_unit_shape(zone.get("shape", "box")) * (hi - lo) / 2 + (lo + hi) / 2) @ r0  # build frame -> part
    turned = mesh.triangles.reshape(-1, 3) @ rotation.T
    lo_t, hi_t = turned.min(axis=0), turned.max(axis=0)
    return part @ rotation.T - [(lo_t[0] + hi_t[0]) / 2, (lo_t[1] + hi_t[1]) / 2, lo_t[2]]


def _unit_shape(shape: str, n: int = 32) -> np.ndarray:
    """Closed, outward-wound triangles of a shape filling the cube [-1, 1]³: box, upright cylinder or ball."""
    if shape == "box":
        v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * 2 - 1
        f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
        return v[np.array(f)]
    a = np.linspace(0, 2 * np.pi, n + 1)
    ring = np.stack([np.cos(a), np.sin(a)], axis=1)
    if shape == "cylinder":
        p0, p1 = ring[:-1], ring[1:]
        lo = lambda p: np.c_[p, -np.ones(len(p))]
        hi = lambda p: np.c_[p, np.ones(len(p))]
        zero = np.zeros((n, 2))
        return np.concatenate([np.stack([lo(p0), lo(p1), hi(p1)], 1), np.stack([lo(p0), hi(p1), hi(p0)], 1),
                               np.stack([hi(zero), hi(p0), hi(p1)], 1), np.stack([lo(zero), lo(p1), lo(p0)], 1)])
    rows = n // 2
    lat = np.linspace(-np.pi / 2, np.pi / 2, rows + 1)
    grid = np.stack([np.cos(lat)[:, None] * ring[None, :, 0], np.cos(lat)[:, None] * ring[None, :, 1],
                     np.repeat(np.sin(lat)[:, None], n + 1, axis=1)], axis=-1)
    a, b, c, d = grid[:-1, :-1], grid[:-1, 1:], grid[1:, 1:], grid[1:, :-1]
    tris = np.concatenate([np.stack([a, b, c], 2).reshape(-1, 3, 3), np.stack([a, c, d], 2).reshape(-1, 3, 3)])
    area = np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1)
    return tris[area > 1e-12]  # the poles' collapsed triangles
