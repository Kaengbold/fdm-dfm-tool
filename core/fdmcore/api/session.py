from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..mesh import Mesh


MODES = {"print": {"slicer", "process"}, "design": {"geometry", "process"}, "both": {"geometry", "slicer", "process"}}


@dataclass
class Session:
    mesh: Mesh
    name: str
    regions: np.ndarray | None = None  # region id per triangle, computed on demand
    edges: tuple[dict, dict] | None = None  # cached (feature_edges, concave_edges) polylines for 3D picking
    load_case: tuple | None = None  # (loads json, prepared LoadCase) cache
    solved: tuple | None = None  # (key, result): the last static strength solve, reused by the next check
    bodies: list = field(default_factory=list)  # (name, Mesh) per body in the file
    body: int = 0  # which body is loaded
    tags: dict = field(default_factory=dict)  # region id -> what the user says the feature is (features.TAGS)
    weak_spot: tuple | None = None  # (critical point in part coords, voxel size) from the last strength check
    hot_spots: tuple | None = None  # (points, share of the highest stress) where it is nearly as high (features.HOT_SHARE)
    strength_verdict: str | None = None  # verdict ("fine", "borderline", "likely to break") from the last strength check
    stressed: dict | None = None  # the last strength check's stress per cube (fi, ignore) with its model, up and material (T-290)
    gcode: dict | None = None  # the sliced part (load_gcode): toolpath, orientation and placement
    fits: dict = field(default_factory=dict)  # region id -> {"partner_mm", "fit"}: what goes into a hole / onto a post
    fasteners: dict = field(default_factory=dict)  # region id -> {"size", "use"}: screw / insert / nut holes (set_fastener)
    gaps: dict = field(default_factory=dict)  # (face, face) -> {"partner_mm", "fit", "warp_mm"}: slots and tabs (set_gap)
    warp: dict | None = None  # the last warping simulation: up, model, displacement (part frame)
    coarse: list | None = None  # curves exported with too few facets (checks.coarse_surfaces), computed on demand
    kind: str = "cad"  # "cad" or "organic" (scan, sculpt, figure): _model_kind, T-408
    cad_checks: bool = True  # hole / gap / pin / lettering checks and feature detection (off for organic parts)
    cad_faces: tuple | None = None  # STEP parts: (the regions loaded with it, B-rep face id per region), see _cad_face
    importance: tuple | None = None  # (key, importance.Map): how much each spot matters (T-475), cached


_session: Session | None = None


def _require() -> Session:
    if _session is None:
        raise RuntimeError("No part loaded")
    return _session


# Analysis grids (Expert settings): more cubes are more exact and slower (about linear in the browser).
ANALYSIS = {
    "strength_voxels": (12_000, 2_000, 200_000, "Cubes in the strength grid (strength analysis, shaking, the optimiser). Seconds in the browser at 12 000."),
    "fine_voxels": (40_000, 5_000, 400_000, "Cubes for \"Check accuracy with a finer grid\": about 1.5× finer than the normal grid."),
    "warp_voxels": (6_000, 1_000, 100_000, "Cubes in the warping simulation: it solves once per group of layers, so a coarser grid."),
}


_analysis: dict[str, float] = {}


def _grid(key: str) -> int:
    return int(_analysis.get(key, ANALYSIS[key][0]))
