from __future__ import annotations

import numpy as np

from ..formats import read_bodies, unit_hint
from ..mesh import Mesh, mesh_health
from ..profile import load_data, presets
from . import design as _design
from . import session as _S


def load(data: bytes, name: str = "part", face_ids: list[int] | None = None) -> dict:
    """Load an STL or 3MF; returns mesh info and the triangles for display.

    ``face_ids`` (one per triangle, e.g. CAD faces from a STEP file) replace the
    guessed face regions with exact ones. Files with several bodies load the largest;
    ``bodies`` lists them all so the user can switch with ``select_body``.
    """
    bodies = read_bodies(bytes(data))
    if face_ids is not None:
        if len(bodies) > 1 or len(face_ids) != bodies[0][1].face_count:
            raise ValueError("face_ids must have one entry per triangle")
        ids, regions = np.unique(np.asarray(face_ids), return_inverse=True)
        regions = regions.ravel()
    else:
        ids = regions = None
    _S._session = _S.Session(bodies[0][1], name, regions, bodies=bodies, cad_faces=None if ids is None else (regions, ids))
    _set_kind("cad" if face_ids is not None else None)
    return _info()


def select_body(index: int) -> dict:
    """Analyse another body of a multi-body file."""
    s = _S._require()
    if not 0 <= index < len(s.bodies):
        raise ValueError("No such body")
    s.mesh, s.body, s.regions, s.edges, s.load_case, s.tags = s.bodies[index][1], index, None, None, None, {}
    _set_kind()
    return _info()


def scale(factor: float) -> dict:
    """Rescale the loaded part (e.g. inch -> mm). Every body is scaled, so switching keeps the units."""
    s = _S._require()
    if not 1e-4 <= factor <= 1e4:
        raise ValueError("Scale factor out of range")
    s.bodies = [(n, Mesh(m.triangles * factor)) for n, m in s.bodies]
    s.mesh, s.edges, s.load_case = s.bodies[s.body][1], None, None
    return _info()


def _info() -> dict:
    s = _S._require()
    lo, hi = s.mesh.bounds()
    size = hi - lo
    biggest_bed = max(max(p["bed"]) for p in presets()["printers"].values())
    return {
        "name": s.name,
        "faces": s.mesh.face_count,
        "mesh_health": mesh_health(s.mesh),
        "volume_mm3": abs(s.mesh.volume()),
        "size_mm": size.tolist(),
        "triangles": s.mesh.triangles.astype("<f4").tobytes(),
        "body": s.body,
        "bodies": [{"name": n, "faces": m.face_count, "volume_mm3": abs(m.volume())} for n, m in s.bodies] if len(s.bodies) > 1 else [],
        "unit_hint": unit_hint(size, biggest_bed),
        "kind": s.kind,
        "cad_checks": s.cad_checks,
    }


def _set_kind(kind: str | None = None) -> None:
    """CAD part or organic shape (scan, sculpt, figure): organic parts start with the CAD checks off."""
    from ..regions import planar_share

    s = _S._require()
    if kind is None:
        below = load_data("thresholds")["organic"]["planar_share_below"]
        kind = "organic" if planar_share(s.mesh) < below else "cad"
    s.kind, s.cad_checks = kind, kind == "cad"


def set_cad_checks(on: bool) -> dict:
    """Turn the checks that assume CAD intent (holes, gaps, pins, lettering, detected features) on or off,
    e.g. back on for a CAD part that was taken for a sculpted one."""
    s = _S._require()
    s.cad_checks = bool(on)
    return {"kind": s.kind, "cad_checks": s.cad_checks}


def _features(mesh: Mesh | None = None) -> list[dict]:
    """Detected holes, bosses and rounded edges; none while the CAD checks are off (an organic shape is
    all curves, so every bump would be taken for a boss or a fillet)."""
    from ..features import detect_features

    s = _S._require()
    return detect_features(mesh or s.mesh, s.regions, s.tags) if s.cad_checks else []


def _edges() -> tuple[dict, dict]:
    """Cached compact (feature_edges, concave_edges) polylines for the loaded part. On a STEP part each also names
    the B-rep faces it lies between (``faces``, see ``_cad_face``): an edit there can work on the exact CAD edge."""
    from ..regions import extract_edges, find_regions

    s = _S._require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    if s.edges is None:
        s.edges = extract_edges(s.mesh, s.regions)
        for group in s.edges:
            for m in group["meta"]:
                faces = [_cad_face(r) for r in m["regions"]]
                if m["regions"] and None not in faces:
                    m["faces"] = faces
    return s.edges


def _cad_refs(intent: dict) -> dict:
    """On a STEP part, an edit intent's regions also by their B-rep face ids (``faces``), for the CAD engine."""
    refs = intent.get("refs", {})
    faces = [_cad_face(r) for r in refs.get("regions", [])]
    if refs.get("regions") and None not in faces:
        refs["faces"] = faces
    for e in refs.get("edges", []):
        pair = [_cad_face(r) for r in e.get("regions", [])]
        if e.get("regions") and None not in pair:
            e["faces"] = pair
    return intent


def _cad_face(region: int) -> int | None:
    """The STEP file's B-rep face id of a region (the importer's id for this body, which is OpenCascade's own face
    order, checked by tools/check_step_faces.mjs), or None when the part isn't from STEP or its regions changed."""
    s = _S._require()
    if s.cad_faces is None or s.cad_faces[0] is not s.regions or not 0 <= region < len(s.cad_faces[1]):
        return None
    return int(s.cad_faces[1][region])


def regions() -> dict:
    """Clickable faces and edges for 3D picking: region id per triangle (uint32 bytes), region
    summaries (area, normal, centroid, kind: flat/cylinder/hole/curved, hole_diameter on holes),
    plus compact feature-edge and inside-corner (concave_edges) polylines."""
    from ..features import accepted
    from ..regions import EDGE_BAND_RADIUS, find_regions, region_info

    s = _S._require()
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    info = region_info(s.mesh, s.regions, accepted(_features()))
    for r in info:
        face = _cad_face(r["id"])
        if face is not None:
            r["face"] = face
    feature_edges, concave_edges = _edges()
    return {
        "face_regions": s.regions.astype("<u4").tobytes(),
        "regions": info,
        "feature_edges": feature_edges,
        "concave_edges": concave_edges,
        "edge_radius_mm": EDGE_BAND_RADIUS,
    }


def session_state() -> dict:
    """What the user told the engine about this part and what the last strength check found, as JSON:
    the app keeps it and gives it back with ``restore_state`` when the engine restarts (Cancel), so
    hole / post answers, fits, gaps and the weak spot don't silently vanish. The warping result is
    too big to keep: it is simply run again."""
    s = _S._require()
    return {
        "tags": {str(k): v for k, v in s.tags.items()},
        "fits": {str(k): v for k, v in s.fits.items()},
        "gaps": [[a, b, v] for (a, b), v in s.gaps.items()],
        "fasteners": {str(k): v for k, v in s.fasteners.items()},
        "weak_spot": None if s.weak_spot is None else [np.asarray(s.weak_spot[0], float).tolist(), s.weak_spot[1]],
        "hot_spots": None if s.hot_spots is None else [np.asarray(x, float).tolist() for x in s.hot_spots],
        "strength_verdict": s.strength_verdict,
    }


def restore_state(state: dict) -> dict:
    """Put back what ``session_state`` gave (after the same part was loaded again)."""
    s = _S._require()
    s.tags = {int(k): v for k, v in state.get("tags", {}).items()}
    s.fits = {int(k): v for k, v in state.get("fits", {}).items()}
    s.gaps = {(int(a), int(b)): v for a, b, v in state.get("gaps", [])}
    s.fasteners = {int(k): v for k, v in state.get("fasteners", {}).items()}
    w = state.get("weak_spot")
    s.weak_spot = None if w is None else (np.asarray(w[0], float), float(w[1]))
    h = state.get("hot_spots")
    s.hot_spots = None if h is None else tuple(np.asarray(x, float) for x in h)
    s.strength_verdict = state.get("strength_verdict")
    return {"restored": True}


def holes_state() -> dict:
    """What the user sized, for a project file: fits (hole / post → what goes in, set_fit), screws,
    inserts and nuts (set_fastener) and slots (set_gap). restore_holes sizes them again."""
    s = _S._require()
    plain = lambda v: {"partner_mm": v["partner_mm"], "fit": v["fit"]}  # noqa: E731 - warp_mm is worked out again
    return {"fits": {str(k): plain(v) for k, v in s.fits.items()},
            "fasteners": {str(k): {"size": v["size"], "use": v["use"]} for k, v in s.fasteners.items()},
            "gaps": [[a, b, plain(v)] for (a, b), v in s.gaps.items()]}


def restore_holes(holes: dict, up, profile: dict | None = None) -> dict:
    """Size again what a project file's ``holes`` (holes_state) says, for build direction ``up``:
    the same results as set_fit / set_fastener / set_gap, by region (fits, fasteners) or in order (gaps).
    A hole that isn't found any more (the part changed) is left out, not an error."""
    out = {"fits": {}, "fasteners": {}, "gaps": [], "skipped": 0}

    def attempt(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except ValueError:
            out["skipped"] += 1
            return None

    for k, v in (holes.get("fits") or {}).items():
        if (r := attempt(_design.set_fit, int(k), up, v["partner_mm"], v["fit"], profile)) is not None:
            out["fits"][k] = r
    for k, v in (holes.get("fasteners") or {}).items():
        if (r := attempt(_design.set_fastener, int(k), up, v["size"], v["use"], profile)) is not None:
            out["fasteners"][k] = r
    for a, b, v in holes.get("gaps") or []:
        if (r := attempt(_design.set_gap, int(a), int(b), up, v["partner_mm"], v["fit"], profile)) is not None:
            out["gaps"].append({**r, "partner_mm": v["partner_mm"], "fit": v["fit"]})
    return out
