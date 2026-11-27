from __future__ import annotations

import numpy as np

from ..checks import orient
from ..export3mf import export_3mf
from ..mesh import Mesh
from ..placement import plate_layout, print_rotation
from ..profile import Profile
from ..recommend import bambu, cooling_ranges, grip_settings
from . import check as _check
from . import orientation as _orient
from . import session as _S
from . import zones as _zones


def export(up, profile: dict | None = None, settings: dict | None = None, zones: list[dict] | None = None, slicer: str = "orca",
           pauses: list[float] | None = None, fine_layers: list[dict] | None = None, faces: dict | None = None,
           grip: bool = False, copies: int = 1, excluded_settings: list[str] | None = None, cooling: dict | None = None) -> bytes:
    """Oriented part as 3MF, with slicer settings for OrcaSlicer / Bambu Studio / PrusaSlicer.
    ``fine_layers``: {"z0", "z1", "layer_height"} each (the metrics of shallow_slope_stairs findings):
    thinner layers over just that height. ``faces`` as in ``orientations``: no seam on the faces marked
    "clean" (the ones people see; painted as seam blockers). ``grip``: fuzzy skin on the outer walls
    (recommend.grip_settings; the slicer settings given win). ``copies``: that many on the plate (see ``plate``).
    ``excluded_settings``: per-object keys left to the slicer's profile, even when grip or support boxes add them.
    ``pauses``: pocket tops (mm, ``top_z`` of a pause_to_insert finding): the print pauses before the first
    layer above each, to put something into the pocket (OrcaSlicer and PrusaSlicer; Bambu Studio ignores it).
    ``zones`` (from optimise_strength's ``zoned``: lo, hi, walls, infill) become modifier volumes, and
    the advanced design boxes (volume_at) support enforcers, blockers and negative volumes; then the
    file is for one slicer: ``slicer`` "orca" (also Bambu Studio) or "prusa". ``cooling``: a cooling plan
    (T-479): ``ranges`` = [{"z0", "z1", "speeds": {"outer_wall" | "inner_wall" | "sparse_infill" |
    "internal_solid_infill" | "top_surface": mm/s}}] slow those heights down (recommend.cooling_ranges),
    ``tower`` = {"height", "diameter"} (mm) adds a cooling tower beside the part, so each layer up to that
    height takes longer (not with several copies, which already do that). The slicer's own slowdown for
    layer cooling is never switched off."""
    s = _S._require()
    p = Profile.from_dict(profile)
    if settings is None:
        settings = {r["key"]: r["value"] for r in _check.check(up, profile)["settings"]}
    if grip:
        settings = {**settings, **grip_settings(p), **{k: v for k, v in settings.items() if k.startswith("fuzzy_skin")}}
    bed = p.printer["bed"]
    rotation, _ = print_rotation(s.mesh, up, bed, _orient._moving(p))
    modifiers = []
    for z in zones or []:
        kind = z.get("kind", "stronger")
        if kind in _zones.DESIGN_ONLY:  # design aids and model changes: nothing for the slicer
            continue
        conf = {"wall_loops": str(int(z["walls"])), "sparse_infill_density": f"{float(z['infill']):.0%}"} if kind == "stronger" else {}
        modifiers.append((_zones._zone_box(s.mesh, up, rotation, z), conf, kind))
    if any(m[2] == "support" for m in modifiers) and str(settings.get("enable_support", "0")) == "0":
        # Supports only where the user put them: the slicer's own placement stays off.
        settings = {**settings, "enable_support": "1", "support_type": "tree(manual)"}
    settings = _export_brim_settings(p, settings, set(excluded_settings or ()))
    layer = float(settings.get("layer_height") or p.layer_height)
    part = orient(s.mesh, rotation)
    seam = _seam_block(faces)
    if (cooling or {}).get("tower"):
        if copies > 1:
            raise ValueError("Print several copies or add a cooling tower, not both: the copies already give each layer time to cool")
        part, seam = _with_tower(part, seam, cooling["tower"], bed)
    return export_3mf(part, s.name, settings, layer_height=layer, bed_center=(bed[0] / 2, bed[1] / 2), modifiers=modifiers, layout=slicer,
                      pauses=[{"top": float(z), "layer_height": layer, "note": "Put the part into the pocket"} for z in pauses or []],
                      ranges=[{k: float(r[k]) for k in ("z0", "z1", "layer_height")} for r in fine_layers or []] + cooling_ranges(cooling or {}),
                      seam_block=seam, copies=plate(up, profile, copies)["offsets"] if copies > 1 else ((0.0, 0.0),))


def _export_brim_settings(p: Profile, settings: dict, excluded: set[str], *, slicer: str = "orca") -> dict:
    """Use the export's Bambu fallback, and mirror the Prusa metadata when simulating it."""
    out = {k: v for k, v in settings.items() if k not in excluded}
    if out.get("brim_type") == "brim_ears" and (bambu(p.printer) or slicer == "prusa"):
        out["brim_type"] = "outer_only"
    return out


TOWER_GAP_MM = 5.0  # between the part and a cooling tower


def _with_tower(part: Mesh, seam, tower: dict, bed) -> tuple[Mesh, np.ndarray | None]:
    """The oriented part with a cooling tower (a cylinder ``diameter`` wide, ``height`` tall) as a second
    shell beside it, on whichever side of the bed has room: the slicer prints it on every layer up to
    its top, so those layers take longer and cool more (a common trick for small parts)."""
    from shapely.geometry import Point

    from ..additions import extrude

    d, height = float(tower["diameter"]), float(tower["height"])
    lo, hi = part.bounds()
    if not (d > 0 and 0 < height <= hi[2] + 1e-6):
        raise ValueError(f"A cooling tower needs a diameter above 0 and a height up to the part's ({hi[2]:.1f} mm)")
    for x in (hi[0] + TOWER_GAP_MM + d / 2, lo[0] - TOWER_GAP_MM - d / 2):
        if abs(x) + d / 2 <= bed[0] / 2:
            shell = extrude(Point(x, 0.0).buffer(d / 2, 32), 0.0, height)
            out = Mesh(np.concatenate([part.triangles, shell]))
            return out, None if seam is None else np.concatenate([seam, np.zeros(len(shell), bool)])
    raise ValueError("There's no room on the bed for a cooling tower beside the part")


def plate(up, profile: dict | None = None, copies: int | None = None) -> dict:
    """Several copies of the part on one plate, printed this way up: how many fit (``most``), where ``copies`` of
    them go (offsets from the bed's centre, mm; all that fit when None), and what printing them all at once or
    one after another means, in plain words (Slant 3D: a full plate fails as one, and strings between parts).
    """
    s = _S._require()
    p = Profile.from_dict(profile)
    rotation, fit = print_rotation(s.mesh, up, p.printer["bed"], _orient._moving(p))
    size = fit["size_mm"]
    most, offsets = plate_layout(size[:2], p.printer["bed"], copies)
    n = len(offsets)
    notes = []
    if n > 1:
        notes.append(f"All {n} at once (the slicer's default): the plate takes about {n} times one part's time, and every "
                     "part waits for the last. If one comes loose, it can knock the others over or pull plastic strings "
                     "across them, and a stopped print loses them all. The nozzle also travels between parts on every "
                     "layer, which leaves strings and blobs.")
        notes.append("One after another (OrcaSlicer and Bambu Studio: plate settings, print sequence “By object”; PrusaSlicer: "
                     "Print settings, Output options, “Complete individual objects”): each part is finished before the next "
                     "starts, so a failure only costs that part and there are no strings between them. The slicer only allows "
                     "it for parts lower than the printhead's carriage, and wants more room between them: it tells you, then "
                     "arrange again (A).")
    return {"most": most, "copies": n, "offsets": offsets, "footprint_mm": [round(v, 1) for v in size[:2]],
            "height_mm": round(size[2], 1), "notes": notes}


def _seam_block(faces: dict | None) -> np.ndarray | None:
    """Triangles of the faces marked "clean" (seen): the seam stays off them. None when none is marked."""
    marked = _orient._marked_faces(faces)
    if not marked or not marked["clean"]:
        return None
    return np.logical_or.reduce(marked["clean"])


def md_cell(value) -> str:
    """Text for one Markdown table cell: a ``|`` or a line break in it would split the row."""
    return " ".join(str(value).splitlines()).replace("|", "\\|")


def report(up, profile: dict | None = None, mode: str = "print") -> str:
    """Markdown report for sharing or for pasting into an AI chat."""
    s = _S._require()
    p = Profile.from_dict(profile)
    r = _check.check(up, profile, mode)
    st = r["stats"]
    lines = [
        f"# Print check: {s.name}",
        "",
        f"- Printer: {p.printer['name']} · Material: {p.material['name']} · Nozzle: {p.nozzle} mm",
        f"- Build direction (part axis pointing up): {np.round(r['up'], 3).tolist()}",
        f"- Height {st['height_mm']:.1f} mm · ≈{st['print_time_min']:.0f} min · ≈{st['filament_g']:.0f} g (rough estimates)",
        "",
        "## Findings",
        "",
    ]
    if not r["findings"]:
        lines.append("No issues found.")
    for f in r["findings"]:
        lines += [f"### [{f['severity'].upper()}] {f['title']}", "", f["detail"], "", f["why"], ""]
        lines += [f"- *{fx['domain']}*: {fx['text']}" for fx in f["fixes"]] + [""]
    if r["settings"]:
        lines += ["## Recommended slicer settings", "", "| Setting | Value | Why |", "|---|---|---|"]
        lines += [f"| {md_cell(x['label'])} (`{x['key']}`) | {md_cell(x['value'])} | {md_cell(x['reason'])} |" for x in r["settings"]]
    return "\n".join(lines) + "\n"
