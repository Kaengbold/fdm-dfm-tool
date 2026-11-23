"""MCP server: lets an AI agent (Claude Code, Codex, Claude Desktop, …) operate the tool.

Runs locally over stdio with the user's own agent subscription — the tool itself
never talks to an AI provider. Every tool maps onto fdmcore.api, the same API the
web app uses, and every API method has a tool (``API_TOOLS``), so an agent can do what the app does.

    pip install -e "core[local]"
    claude mcp add fdm-dfm -- python -m fdmcore.mcp_server

Files: by default the agent may read and write anywhere the user can. Start the server with
``--folder DIR`` (or set ``FDM_DFM_FOLDER``) to keep it inside one folder. It never replaces an
existing file it didn't write itself in this session (set ``FDM_DFM_OVERWRITE=1`` to allow that).

Typical agent flow: load_part -> find_orientations -> check_print -> (list_faces ->
check_strength -> strength_options) -> suggest_design_changes -> apply_design_changes
-> export_3mf (-> slice_with_orcaslicer to confirm with the real slicer).
"""

from __future__ import annotations

import os
from pathlib import Path

# Simulations must leave the computer usable (video, browser) while they run: cap the maths
# libraries at half the cores. Set before numpy loads; the user's own settings win.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, str(max(1, (os.cpu_count() or 2) // 2)))

from mcp.server.mcpserver import Image, MCPServer  # noqa: E402  (after the thread cap)

from . import api
from .mesh import to_binary_stl

server = MCPServer(
    name="fdm-dfm",
    instructions=(
        "Design-for-FDM assistant. Load one part (STL), then analyse printability, orientation, "
        "strength and design changes. Directions ('up') are unit vectors in the part's own coordinates "
        "that will point up on the printer. Faces for loads come from list_faces. Strength results are "
        "rough indications: aim for a safety factor of 2 or more. Explain findings to the user in plain language."
    ),
)


def _profile(printer: str, material: str, nozzle: str, goal: str = "balanced", use_temp_c: float | None = None,
             constant_load: bool = False) -> dict:
    return {"printer": printer, "material": material, "nozzle": nozzle, "goal": goal, "use_temp_c": use_temp_c, "constant_load": constant_load}


# ---------- file access ----------
# The agent passes paths; these keep it to part / G-code / project files and, when the user asks
# for it, to one folder, and stop it from silently replacing a file the user already had.
PARTS = (".stl", ".3mf")
GCODE = (".gcode", ".gco", ".g", ".3mf")
PROJECT = (".json",)
_written: set[Path] = set()  # files this server wrote: replacing those again is the normal loop


def _folder() -> Path | None:
    value = os.environ.get("FDM_DFM_FOLDER")
    return Path(value).expanduser().resolve() if value else None


def _path(path: str, kinds: tuple[str, ...], what: str) -> Path:
    p = Path(path).expanduser().resolve()
    if p.suffix.lower() not in kinds:
        raise ValueError(f"The {what} must end in {' or '.join(kinds)}: {p.name}")
    root = _folder()
    if root and not p.is_relative_to(root):
        raise ValueError(f"This server may only use files inside {root} (set with --folder / FDM_DFM_FOLDER)")
    return p


def _in(path: str, kinds: tuple[str, ...], what: str = "file") -> Path:
    """A file to read: right kind, inside the allowed folder."""
    return _path(path, kinds, what)


def _out(path: str, kinds: tuple[str, ...], what: str = "output file") -> Path:
    """A file to write: right kind, inside the allowed folder, and not someone's existing file."""
    p = _path(path, kinds, what)
    if p.exists() and p not in _written and os.environ.get("FDM_DFM_OVERWRITE") != "1":
        raise ValueError(f"{p} already exists: pick another name (or ask the user, who can allow "
                         "replacing files with FDM_DFM_OVERWRITE=1)")
    _written.add(p)
    return p


def _clean(value):
    """Drop binary payloads (meant for the 3D viewer) from results."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if not isinstance(v, (bytes, bytearray))}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


@server.tool()
def list_presets() -> dict:
    """Available printers, materials and nozzles (ids to pass to other tools)."""
    p = api.get_presets()
    return {
        "printers": {k: v["name"] for k, v in p["printers"].items()},
        "materials": {k: v["name"] for k, v in p["materials"].items()},
        "nozzles": list(p["nozzles"]),
    }


@server.tool()
def load_part(path: str) -> dict:
    """Load an STL or 3MF file (millimetres). Files with several bodies load the largest; the result lists
    them all (``bodies``) and ``unit_hint`` warns about parts exported in inches or metres.
    ``kind``: "cad" or "organic" (a scan, sculpt or figure); organic parts start with the checks that
    assume CAD intent off (``cad_checks``, see set_cad_checks). Replaces any previously loaded part."""
    p = _in(path, PARTS, "part file")
    return _clean(api.load(p.read_bytes(), p.name))


@server.tool()
def set_cad_checks(on: bool) -> dict:
    """Turn the checks that assume CAD intent on or off: holes, gaps between moving parts, pins, raised or
    engraved lettering, detail in the first layer and detected features. Off by itself for organic parts
    (``kind`` from load_part); turn it on when a CAD part was taken for a sculpt."""
    return _clean(api.set_cad_checks(on))


@server.tool()
def select_body(index: int) -> dict:
    """Switch to another body of a multi-body file (index into ``bodies`` from load_part)."""
    return _clean(api.select_body(index))


@server.tool()
def scale_part(factor: float) -> dict:
    """Rescale the loaded part, e.g. 25.4 for a part exported in inches."""
    return _clean(api.scale(factor))


@server.tool()
def find_orientations(
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    loads: list[dict] | None = None,
    faces: dict[str, list[int] | str] | None = None,
) -> dict:
    """Rank print orientations. With loads (see check_strength), strength is included in the ranking.
    ``faces`` marks faces (ids from list_faces) the user cares about: {"clean": [...] no support marks,
    "bed": [...] on the bed, "vertical": [...] upright, e.g. a hole that must come out round}; "clean": "visible"
    stands for the faces people see (guess_visible_faces)."""
    return _clean(api.orientations(_profile(printer, material, nozzle), loads=loads or None, faces=faces))


@server.tool()
def check_print(
    up: list[float],
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    mode: str = "both",
    faces: dict[str, list[int] | str] | None = None,
    use_temp_c: float | None = None,
) -> dict:
    """Printability findings, rough print time/filament and recommended slicer settings for one orientation.
    mode: 'print' (can't change the model), 'design' (can't change slicer settings) or 'both'.
    ``faces`` as in find_orientations: support on a "clean" face is reported as a problem.
    use_temp_c: hottest the part gets in use (warns near the material's heat limit)."""
    return _clean(api.check(up, _profile(printer, material, nozzle, use_temp_c=use_temp_c), mode, faces))


@server.tool()
def list_faces() -> list[dict]:
    """Faces (regions) of the part with id, kind (flat / cylinder / hole / curved), area_mm2,
    normal, centroid and hole_diameter (on holes) — use ids for loads."""
    return api.regions()["regions"]


@server.tool()
def guess_visible_faces() -> dict:
    """The faces people will see, guessed: the outer ones (on the part's convex hull), so hole walls, pockets and
    slots are left out. ``regions``: face ids (as list_faces), ``share``: their part of the surface. For a part that
    should look good, keep support off them: ``faces={"clean": "visible"}`` in optimize_print / check_print, or
    mark the one face that matters as {"clean": [id]}."""
    return api.visible_faces()


@server.tool()
def list_round_features(up: list[float], printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4") -> dict:
    """Round holes and pegs the user can set a fit on: region id, kind (hole / peg), diameter, axis, centre, the
    regions of the same size (``same``), the fit or fastener already set, and ``printed_mm``: the size it comes
    out at on this printer (measured with the fit test, or typical). ``tolerance``: the fit gaps in use. Then
    ``set_fit`` / ``set_fastener`` on a region, and ``suggest_design_changes`` offers the resize."""
    return _clean(api.round_features(up, _profile(printer, material, nozzle)))


@server.tool()
def list_edges(radius_mm: float = 2.0) -> dict:
    """Sharp outer feature edges (``feature_edges``) and inside corners (``concave_edges``) of the part.
    Each entry has ``id``, ``closed``, ``length_mm``, ``midpoint``, ``normal``, ``regions``, ``points``
    (sampled along the polyline) and ``radius`` mm. Pass ``{"points": e["points"], "radius": e["radius"],
    "kind": "fixed" | "force", ...}`` (or ``{"edge": e["id"], "edge_kind": "feature" | "concave", ...}``)
    directly to check_strength."""
    import numpy as np

    from .regions import EDGE_BAND_RADIUS, sample_polyline, unpack_polylines

    r = api.regions()
    rad = float(radius_mm) if radius_mm > 0 else EDGE_BAND_RADIUS

    def pack(group: dict) -> list[dict]:
        lines = unpack_polylines(group)
        return [
            {**meta, "points": np.round(sample_polyline(pts, 0.75 * rad), 3).tolist(), "radius": rad}
            for meta, pts in zip(group["meta"], lines)
        ]

    return {
        "feature_edges": pack(r["feature_edges"]),
        "concave_edges": pack(r["concave_edges"]),
    }


@server.tool()
def check_strength(
    up: list[float],
    loads: list[dict],
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    walls: int | None = None,
    infill: float | None = None,
    top: int | None = None,
    bottom: int | None = None,
    fine: bool = False,
    use_temp_c: float | None = None,
    constant_load: bool = False,
    zones: list[dict] | None = None,
) -> dict:
    """Rough strength check. use_temp_c: hottest the part gets in use (weaker when warm, soft near the
    material's heat limit); constant_load: the load stays on (uses the room-temperature creep test or a ×3 target).
    fine=True: a finer grid (slower); compare with the normal one to see how
    far to trust the answer. walls (lines), infill (0–1), top/bottom (solid layers): how it is
    printed; omitted = the goal's usual settings. loads: [{"region": id, "kind": "fixed", "hold": "clamped"|"resting"|"screw"},
    {"region": id, "kind": "force", "kg": 5, "direction": "push"|"pull"|"down"|[x,y,z], "case": 1}].
    Forces with different ``case`` numbers are separate situations; the weakest decides (all listed in ``cases``).
    Instead of "region", a load can sit on a spot: "point": [x, y, z] (part coordinates) with "radius" mm
    (default 3) — for an edge, a corner or part of a big face — on an area/edge band: "points": [[x, y, z], …] with
    "radius" (everything within it of any point, like the web app's brush or list_edges), or directly on a
    list_edges entry: "edge": id, "edge_kind": "feature" | "concave" (with optional "radius").
    ``zones``: stronger areas (add_zone, or optimise_strength's ``zoned``) printed with their own walls / infill.
    Returns safety factor, verdict and failure mode ('layers' = layers separate, 'material' = plastic yields)."""
    return _clean(api.strength(up, loads, _profile(printer, material, nozzle, use_temp_c=use_temp_c, constant_load=constant_load),
                               walls, infill, top, bottom, fine, zones=zones))


@server.tool()
def check_dynamics(
    up: list[float],
    loads: list[dict],
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    drop_mm: float = 0.0,
    mounted_kg: float = 0.0,
    walls: int | None = None,
    infill: float | None = None,
    top: int | None = None,
    bottom: int | None = None,
    zones: list[dict] | None = None,
) -> dict:
    """Vibration and knocks (simplified). frequency_hz: how fast the part swings as held — 20–200 Hz is
    where motors, fans and printers shake, so it may resonate; drop_mm: the load falls this far onto the
    part (impact_factor / impact_safety: how many times harder than held still and the safety factor then, for a hard
    object on a part screwed into wood; *_soft: for something soft; thresholds.yaml "drop");
    mounted_kg: something fixed on the loaded face that swings with it. loads, walls, infill, top, bottom
    and zones as in check_strength."""
    r = api.dynamics(up, loads, _profile(printer, material, nozzle), drop_mm, mounted_kg, True, walls, infill, top, bottom, zones)
    return _clean({k: v for k, v in r.items() if not isinstance(v, bytes)})


@server.tool()
def detailed_strength(
    up: list[float],
    loads: list[dict],
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    gcode_path: str | None = None,
    settings: dict[str, str] | None = None,
    fine: bool = False,
    heat: bool = False,
) -> dict:
    """Strength from the real sliced print: the plastic really in each spot (walls, solid layers, infill)
    and which way the printed lines run — lines pulled apart sideways are a third way to break ('lines').
    Uses ``gcode_path`` (sliced from our 3MF in this orientation), or slices with the installed OrcaSlicer
    (our recommended settings, or ``settings`` overrides; tens of seconds). loads as in check_strength.
    ``heat``: the layers' strength follows how well they fused in the heat simulation (slower); ``heat`` in
    the result compares the safety with the same print bonded as usual."""
    prof = _profile(printer, material, nozzle)
    if gcode_path:
        text = _in(gcode_path, GCODE, "G-code file").read_text(errors="replace")
        sliced = None
    else:
        sliced = _slice(up, printer, material, nozzle, settings, keep_gcode=True)
        text = sliced["orcaslicer"].pop("gcode")
    out = {"gcode": api.load_gcode(text, up, prof), **_clean(api.strength(up, loads, prof, fine=fine, detailed=True, heat=heat))}
    if sliced:
        out["slicer_settings"] = sliced["settings"]
    return out


@server.tool()
def material_advice(
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    use_temp_c: float | None = None,
    outdoors: bool = False,
    flexible: bool = False,
    up: list[float] | None = None,
    loads: list[dict] | None = None,
) -> list[dict]:
    """Which material to print it in: each one rated good / with care / not suitable with reasons —
    heat where it's used, sunlight, flexible or not, what the printer can print, and with ``up`` and
    ``loads`` (as in check_strength) the safety factor for the load. Best (and easiest) first."""
    return api.material_advice(_profile(printer, material, nozzle, use_temp_c=use_temp_c), up, loads, outdoors, flexible)


@server.tool()
def set_fit(region: int, up: list[float], partner_mm: float | None = None, fit: str = "sliding", material: str = "pla") -> dict:
    """Say what goes into a round hole (or what a round post goes into): the mating part's diameter in mm
    and the fit: "press", "snug", "sliding" or "loose". Returns the diameter to model and why;
    suggest_design_changes then offers the resize. partner_mm None forgets it. Region ids from list_faces."""
    return api.set_fit(region, up, partner_mm, fit, {"material": material})


@server.tool()
def set_fastener(region: int, up: list[float], size: str | None = "M3", use: str = "screw", material: str = "pla",
                 nozzle: str = "0.4") -> dict:
    """Say what fastens into a round hole: size "M2"–"M6" and use: "screw" (cut straight into the plastic),
    "insert" (heat-set insert), "nut_top" (bolt, nut in a hex pocket at the upper end), "nut_side" (nut pushed
    in through a slot from the side) or "bolt" (clearance hole). Returns the hole size, depth, wall and pocket
    to model, and the walls to print if the design can't change; suggest_design_changes then offers the
    resize, a boss and the pocket or slot. size None forgets it. Region ids from list_faces."""
    return api.set_fastener(region, up, size, use, {"material": material, "nozzle": nozzle})


@server.tool()
def fastener_suggestions(up: list[float], material: str = "pla", nozzle: str = "0.4") -> dict:
    """Round holes that look like standard screw holes (within 0.3 mm of an M2–M6 pilot, clearance or insert
    size), grouped so equal holes get one question ("All four Ø2.8 mm holes: is an M3 screw going in?"),
    with the likely uses and the hole size each needs. Answer with set_fastener for each region of a group."""
    return api.fastener_suggestions(up, {"material": material, "nozzle": nozzle})

@server.tool()
def set_gap(face_a: int, face_b: int, up: list[float], partner_mm: float | None = None, fit: str = "sliding", material: str = "pla") -> dict:
    """A slot or tab between two parallel flat faces (ids from list_faces): their distance and whether
    it is a slot (empty between) or a tab (plastic between). With partner_mm (thickness of what goes in,
    or of the slot the tab goes into) and fit ("press", "snug", "sliding", "loose"): the size to model
    and why; suggest_design_changes then offers moving both faces. partner_mm None only measures."""
    return api.set_gap(face_a, face_b, up, partner_mm, fit, {"material": material})


@server.tool()
def strength_options(
    up: list[float],
    loads: list[dict],
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    walls: int | None = None,
    infill: float | None = None,
    top: int | None = None,
    bottom: int | None = None,
) -> list[dict]:
    """Safety factor with more walls, more infill, more top/bottom layers and the strongest orientation."""
    return api.strength_options(up, loads, _profile(printer, material, nozzle), walls, infill, top, bottom)


@server.tool()
def stronger_orientation(
    up: list[float],
    loads: list[dict],
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    walls: int | None = None,
    infill: float | None = None,
    top: int | None = None,
    bottom: int | None = None,
) -> dict:
    """Quick check whether the part holds clearly more (1.3x) lying another way: the strongest of the six square
    build directions (``up``, None if none is clearly better), its safety factor and ``current`` for ``up``."""
    return api.stronger_orientation(up, loads, _profile(printer, material, nozzle), walls, infill, top, bottom)


@server.tool()
def optimise_strength(
    up: list[float],
    loads: list[dict],
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    goal: str = "balanced",
    target: float | None = None,
    zones: bool = False,
    use_temp_c: float | None = None,
    constant_load: bool = False,
) -> dict:
    """Lightest walls / top-bottom layers / infill that reach ``target`` safety (default ×2, ×3 for
    goal "strength"), for this orientation and the strongest one. Returns slicer ``settings``,
    grams, ``explanation`` and the steps tried. Takes up to a minute. ``zones=True`` also tries
    stronger zones only around the most stressed places (returned as ``zoned``, with ``zones`` to
    pass to export_3mf)."""
    return _clean(api.optimise_strength(up, loads, _profile(printer, material, nozzle, goal, use_temp_c, constant_load), target, zones))


@server.tool()
def suggest_design_changes(
    up: list[float], printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4", params: dict | None = None,
    keep_clear: list[dict] | None = None,
) -> dict:
    """Detected holes/bosses (``rounded_edge``: a fillet in an inside corner) and suggested geometry edits
    for this orientation: teardrop holes and corner
    chamfers (cut away, ``op: cut``), 45° fills under ledges, sacrificial layers over holes that start in
    mid-air and thicker walls (added, ``op: add``).
    ``params`` sets edit sizes, e.g. {"chamfer_mm": 2, "teardrop_angle_deg": 40, "ledge_max_mm": 20,
    "wall_lines": 3}; ranges come back in the result. ``keep_clear``: boxes from place_box(kind="keep_clear");
    edits that would add plastic into one come back with ``clash``. Each edit says what it is for in ``source``:
    "finding" (``fixes``: the check finding it fixes), "request" (a size the user asked for), "strength" (the weak
    spot of the last strength check) or "other" (no finding asks for it: offer it only as an idea)."""
    return _clean(api.design(up, _profile(printer, material, nozzle), params, keep_clear))


@server.tool()
def tag_feature(region: int, tag: str | None) -> dict:
    """Confirm or correct a detected feature (region id from suggest_design_changes): "hole", "boss",
    or "none" when it isn't one (e.g. a rounded edge taken for a hole). None forgets the answer.
    Ask the user about features with confidence "low". Rejected holes get no edits and no hole findings."""
    return _clean(api.tag_feature(region, tag))


@server.tool()
def apply_design_changes(
    up: list[float],
    edit_ids: list[str],
    output_path: str,
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    params: dict | None = None,
    keep_clear: list[dict] | None = None,
) -> dict:
    """Apply suggested edits (ids from suggest_design_changes, same params and keep_clear), save the modified STL and load it."""
    edits = {e["id"]: e for e in api.design(up, _profile(printer, material, nozzle), params, keep_clear)["edits"]}
    unknown = [i for i in edit_ids if i not in edits]
    if unknown:
        raise ValueError(f"Unknown edit ids: {unknown}")
    return _save_and_load_edits(api._require().mesh, [edits[i] for i in edit_ids], output_path)


@server.tool()
def export_3mf(
    up: list[float],
    output_path: str,
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    settings: dict[str, str] | None = None,
    zones: list[dict] | None = None,
    slicer: str = "orca",
    fine_layers: list[dict] | None = None,
    faces: dict | None = None,
    grip: bool = False,
    copies: int = 1,
    cooling: dict | None = None,
) -> str:
    """Write the oriented part as 3MF with slicer settings (recommended ones unless overridden).
    ``grip``: fuzzy skin on the outer walls, a grippy surface for knobs and handles (grip_texture finding).
    ``copies``: that many on the plate (plan_plate says how many fit).
    ``fine_layers``: the metrics (z0, z1, layer_height) of shallow_slope_stairs findings: thinner layers there.
    ``faces`` {"clean": [region ids from list_faces]}: faces people see; the seam is kept off them.
    ``zones`` (add_zone, or optimise_strength's ``zoned``) become modifier volumes, and boxes from place_box
    (support, blocker, void) support enforcers, blockers and negative volumes; then the file is for one
    slicer: ``slicer`` "orca" (OrcaSlicer and Bambu Studio) or "prusa". ``cooling``: slower speeds per height
    and a cooling tower, {"ranges": [{"z0", "z1", "speeds": {"outer_wall" | "inner_wall" | "sparse_infill" |
    "internal_solid_infill" | "top_surface": mm/s}}], "tower": {"height", "diameter"}} (mm), for spots that stay
    too hot (heat_model); a fan change per height can't go into a slicer project."""
    prof = _profile(printer, material, nozzle)
    merged = {r["key"]: r["value"] for r in api.check(up, prof)["settings"]}
    merged.update(settings or {})
    out = _out(output_path, (".3mf",), "3MF path")
    out.write_bytes(api.export(up, prof, merged, zones, slicer, fine_layers=fine_layers, faces=faces, grip=grip, copies=copies,
                                cooling=cooling))
    return str(out)


@server.tool()
def slice_with_orcaslicer(
    up: list[float],
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    settings: dict[str, str] | None = None,
) -> dict:
    """Slice for real with the installed OrcaSlicer (its own printer/process/filament profiles plus our
    recommended settings, or ``settings`` overrides). Returns OrcaSlicer's print time, filament use and
    layer count next to our quick estimate. Slow (tens of seconds) — use it to confirm a final choice."""
    return _slice(up, printer, material, nozzle, settings)


def _slice(up, printer: str, material: str, nozzle: str, settings: dict | None, keep_gcode: bool = False,
           cooling_slowdown: bool = True, goal: str = "balanced") -> dict:
    from .profile import Profile, presets
    from .slicer_cli import find_orca, pick_profiles, slice_3mf, without_cooling_slowdown

    orca = find_orca()
    if orca is None:
        raise RuntimeError("OrcaSlicer not found. Install it, or set ORCA_SLICER to its executable.")
    prof = _profile(printer, material, nozzle, goal)
    result = api.check(up, prof)
    merged = {r["key"]: r["value"] for r in result["settings"]}
    merged.update(settings or {})
    p = Profile.from_dict(prof)
    base_material = presets()["materials"][material].get("based_on", material)  # the user's own presets name one
    matched = pick_profiles(orca, presets()["printers"][printer]["name"], nozzle, base_material, float(merged.get("layer_height", p.layer_height)))
    used = matched if cooling_slowdown else without_cooling_slowdown(matched)
    sliced = slice_3mf(api.export(up, prof, merged), used, orca, keep_gcode=keep_gcode)
    if "filament_cm3" in sliced:
        sliced["filament_g"] = round(sliced["filament_cm3"] * p.material["density_g_cm3"], 1)
    return {
        "orcaslicer": sliced,
        "estimate": {k: round(result["stats"][k], 1) for k in ("print_time_min", "filament_g")},
        "profiles": {k: v["name"] for k, v in matched.items()},
        "settings": merged,
    }


@server.tool()
def optimize_print_cooling(up: list[float], output_path: str, printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4",
                           goal: str = "balanced", settings: dict[str, str] | None = None, loads: list[dict] | None = None,
                           intents: list[dict] | None = None, fine: bool = True) -> dict:
    """Slice the part with the installed OrcaSlicer and write G-code with our cooling plan instead of the slicer's
    own slowdown for layer cooling: sliced with that slowdown switched off (everything else as recommended, or
    ``settings``), so its speeds are clean; then the cooling plan (fan ahead of time, slower layers or park moves
    only where spots stay too hot) on the fine heat model, written and checked like optimize_gcode_file. If the
    plan doesn't pass its checks, Orca's own G-code (with its slowdown) is written instead, and ``why`` says so.
    ``minutes`` compares Orca's own G-code, the one without the slowdown and ours by the same timing model
    (printing only; ``orca_estimate`` is the slicer's own, with its start routine). Takes a minute or more."""
    import numpy as np

    from .gcode import parse
    from .kinematics import timing

    out = _out(output_path, (".gcode",), "G-code path")  # before the slow part: a bad path fails at once
    prof = _profile(printer, material, nozzle, goal)
    own = _slice(up, printer, material, nozzle, settings, keep_gcode=True, goal=goal)["orcaslicer"]
    clean = _slice(up, printer, material, nozzle, settings, keep_gcode=True, cooling_slowdown=False, goal=goal)["orcaslicer"]
    text = clean.pop("gcode")
    api.load_gcode(text, up, prof)
    r = api.optimize_gcode(up, text.encode(), prof, loads, intents, fine)
    moving = lambda g: round(timing(parse(g)).total_s / 60, 1)
    minutes = {"orca_with_its_slowdown": moving(own["gcode"]), "without_slowdown": moving(text),
               "orca_estimate": {"with_its_slowdown": own.get("print_time_min"), "without_slowdown": clean.get("print_time_min")}}
    if r["ok"]:
        out.write_bytes(r["data"])
        api.load_gcode(own["gcode"], up, prof)  # how Orca's own slowdown does, by the same heat model
        orca_hot = int((np.frombuffer(api.heat_model(up, prof, fine, intents, loads)["window"], np.uint8) == 1).sum())
        hot = {"orca_with_its_slowdown": orca_hot, "without_slowdown": r["too_hot"][0], "with_cooling_plan": r["too_hot"][1]}
        ours, theirs = r["minutes"][1], minutes["orca_with_its_slowdown"]
        summary = (f"{ours:.1f} min against {theirs:.1f} with OrcaSlicer's own slowdown, "
                   + (f"and {hot['with_cooling_plan']} spots too hot instead of {orca_hot}." if hot["with_cooling_plan"] != orca_hot
                      else f"with the same {orca_hot} spots too hot.")
                   + (" Slower, because cooling those spots takes time." if ours > theirs and hot["with_cooling_plan"] < orca_hot else ""))
        return {"path": str(out), "source": "cooling plan", "minutes": {**minutes, "with_cooling_plan": ours},
                "spots_too_hot": hot, "changes": r["changes"], "summary": summary}
    heat = api.heat_model(up, prof, fine, intents, loads)
    if not any(f["rule"] in ("heat_too_hot", "heat_weak_bond") for f in heat["findings"]):
        out.write_text(text, encoding="utf-8")  # nothing to fix: the clean slice is fine as it is
        return {"path": str(out), "source": "no slowdown needed", "minutes": minutes,
                "why": "Every spot stays in its temperature window without the slicer's slowdown."}
    out.write_text(own["gcode"], encoding="utf-8")
    return {"path": str(out), "source": "OrcaSlicer's own", "minutes": minutes,
            "why": "The cooling plan didn't pass its checks: " + "; ".join(r["problems"])}


@server.tool()
def get_report(up: list[float], printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4", mode: str = "both") -> str:
    """Markdown report of findings, fixes and settings."""
    return api.report(up, _profile(printer, material, nozzle), mode)


@server.tool()
def write_report(output_path: str, up: list[float], loads: list[dict] | None = None, printer: str = "bambu_p1s",
                 material: str = "pla", nozzle: str = "0.4", mode: str = "both") -> dict:
    """The report a person reads, with pictures and charts: ``output_path`` ending in .html (one file that
    opens in any browser and prints to PDF) or .md (pictures saved next to it). With ``loads`` (like
    check_strength) it says whether the part holds. Give the user the path. Takes 30–60 s."""
    from . import reports

    return reports.write(str(_out(output_path, (".html", ".htm", ".md"), "report path")), up, _profile(printer, material, nozzle), loads, mode)


@server.tool()
def make_chart(kind: str, output_path: str, up: list[float], loads: list[dict] | None = None, printer: str = "bambu_p1s",
               material: str = "pla", nozzle: str = "0.4", use_temp_c: float | None = None) -> dict:
    """Chart as an SVG file for the user: "orientations" (safety factor per orientation with ``loads``, else
    the support each needs), "nozzles" (print time and filament per nozzle: two files, ``_filament`` added
    to the second name), "warping" (how hard the base curls against the longest bar that stays flat),
    "heat" (strength left vs. temperature, ``use_temp_c`` marked)."""
    from . import charts

    return {"saved": charts.write(kind, str(_out(output_path, (".svg",), "chart path")), up, _profile(printer, material, nozzle, use_temp_c=use_temp_c), loads)}


@server.tool()
def set_custom_presets(printers: dict | None = None, materials: dict | None = None) -> dict:
    """Add the user's own printers and materials (same fields as presets.yaml; a material can say
    ``based_on`` a built-in one and change only some values). Replaces earlier custom ones; their ids
    then work in every tool. Returns the full list like list_presets."""
    api.custom_presets(printers, materials)
    return list_presets()


@server.tool()
def calibration_part(kind: str, output_path: str, material: str = "pla") -> dict:
    """Save a test print for measuring the user's own material on their printer. ``kind``:
    "layer_strength", "warping", "fit", "overhang", "bridge", "sag", "snap", "temp" (a temperature tower for
    ``material``: then heat_tuning_gcode "temp" on its sliced G-code), "ironing" (save it as .3mf: each step's
    ironing flow is set in it), "creep" (a hanging weight test) or "all" (the starter test plate).
    ``how`` says what to print and measure. The measured numbers go into a custom material (set_custom_presets:
    nozzle_temp_c from the tower, ironing_flow_pct from the ironing test, or creep_readings as
    [minutes since loading, drop in mm from the unloaded tip] pairs with a first reading at one minute)."""
    r = api.calibration_part(kind, {"material": material})
    if kind == "ironing" and str(output_path).lower().endswith(".3mf"):
        out = _out(output_path, (".3mf",), "3MF path")
        out.write_bytes(r["3mf"])
    else:
        out = _out(output_path, (".stl",), "STL path")
        out.write_bytes(r["stl"])
    extra = {k: r[k] for k in ("temps", "flows", "creep_test") if k in r}
    return {"saved": str(out), "how": r["how"], **extra}


@server.tool()
def calibrate_bed_grip(longest_flat_mm: int, printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4") -> dict:
    """Fit bed grip from the 40, 80, 120 and 160 mm warping bars. Use 0 if every bar lifted.
    Set the printer's build plate and material's bed temperature in the presets first.
    Save the returned record under its enclosure in the material's bed_grip_calibration field."""
    return api.calibrate_bed_grip(longest_flat_mm, _profile(printer, material, nozzle))


@server.tool()
def check_orientation(up: list[float], printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4") -> dict:
    """Score one build direction of your choice (``up``: the part axis that points up on the printer),
    packed like the entries of find_orientations: score, support area, bed contact, height, reasons."""
    return _clean(api.orientation(up, _profile(printer, material, nozzle)))


@server.tool()
def optimize_print(
    goal: str = "balanced",
    loads: list[dict] | None = None,
    depth: str = "extended",
    printer: str = "bambu_p1s",
    nozzle: str = "0.4",
    use_temp_c: float | None = None,
    constant_load: bool = False,
    owned: list[str] | None = None,
    materials: list[str] | None = None,
    nozzles: list[str] | None = None,
    touches: list[str] | None = None,
    constraints: dict | None = None,
    change: str = "slicer",
    faces: dict | None = None,
    budget_seconds: float | None = None,
) -> dict:
    """The best 2–4 ways to print the loaded part for a goal (strong / balanced / fast / light / quality):
    orientation, material, nozzle, layer height, walls, top/bottom and infill, each checked for strength at its
    own pose. Safety targets: strong ×5, balanced ×3, fast ×2, light ×1.5 (×3 at least for a load that stays on);
    quality and no loads skip strength. ``loads`` as for check_strength. ``depth`` fast / extended / complete
    (extended can take a few minutes). ``owned``: materials the user has; ``materials`` / ``nozzles``: the only
    ones to consider; ``touches``: water, food, outdoors, abrasion, oil_fuel, solvent, skin. ``constraints``:
    hard limits (material, nozzle, layer_height, walls, top, bottom, infill, up). ``change``: slicer / design /
    both. ``faces`` as in find_orientations, or ``{"clean": "visible"}`` for the faces people see as guessed by
    guess_visible_faces (what the app does for quality): orientations with support on them rank lower. Returns
    ``options`` (with ``settings`` to pass to export_3mf), ``feasible`` and, if nothing reaches the target, ``ways_out``.
    ``budget_seconds``: stop searching after about that long and pick from what was checked (``ended_early``; run it
    again without a budget to search the rest)."""
    profile = {"printer": printer, "nozzle": nozzle, "use_temp_c": use_temp_c, "constant_load": constant_load}
    return _clean(api.optimize(goal, loads, depth, profile, owned, materials, nozzles, {"temp_c": use_temp_c, "touches": touches or []},
                               constraints, change, faces, budget_seconds))


@server.tool()
def estimate_optimize_runtime(
    depth: str = "extended",
    loads: list[dict] | None = None,
    goal: str = "balanced",
    printer: str = "bambu_p1s",
    nozzle: str = "0.4",
    use_temp_c: float | None = None,
    constant_load: bool = False,
    owned: list[str] | None = None,
    materials: list[str] | None = None,
    nozzles: list[str] | None = None,
    touches: list[str] | None = None,
    constraints: dict | None = None,
    change: str = "slicer",
) -> dict:
    """Calibrated runtime estimate for optimize_print on the loaded part: ``seconds`` on this machine and
    ``browser_seconds`` (~3× slower in the browser), from triangle count, size, voxel count and candidate
    count. Runs instantaneously without solving FEA."""
    profile = {"printer": printer, "nozzle": nozzle, "use_temp_c": use_temp_c, "constant_load": constant_load}
    return _clean(api.estimate_runtime(depth, loads, profile, goal, owned, materials, nozzles,
                                       {"temp_c": use_temp_c, "touches": touches or []}, constraints, change))


@server.tool()
def compare_nozzles(up: list[float], printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4") -> list[dict]:
    """The same part with every nozzle size: print time, filament, problems that come with the bigger
    nozzles (``new``) and the detail each one can't print."""
    return _clean(api.compare_nozzles(up, _profile(printer, material, nozzle)))


@server.tool()
def heat_model(up: list[float], printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4", fine: bool = False,
               goal: str = "balanced", intents: list[dict] | None = None, loads: list[dict] | None = None, focus: bool = False) -> dict:
    """Heat simulation over the G-code from load_gcode: how hot each spot still is when the next layer covers it
    and how well each joint between layers bonds. ``findings``: spots that stay too hot (sag, blobs), joints too
    cold to bond well, and slowdowns for cooling that aren't needed. ``goal`` (balanced / strength / speed /
    material) moves the limits; ``intents`` = [{"kind": "cosmetic" | "dimension_critical" | "load_bearing",
    "point": [x, y, z], "radius": mm}] tightens them where they are. ``loads`` (as for check_strength) say
    where the layers must bond well; without them that is guessed from the shape (``guessed``, see
    spot_importance). ``hottest`` lists the worst spots; ``fine`` is slower and more exact; ``focus`` runs fine
    detail only around the spots that matter (a big part: nearly the fine answer there, at about the quick cost)."""
    return _clean(api.heat_model(up, _profile(printer, material, nozzle, goal), fine, intents, loads, focus))


@server.tool()
def optimize_cooling(up: list[float], printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4", goal: str = "balanced",
                     loads: list[dict] | None = None, intents: list[dict] | None = None, fine: bool = False) -> dict:
    """A cooling plan for the G-code from load_gcode: where spots stay too hot (sag, blobs) it turns the fan up
    ahead of time, slows layers or adds short park moves; where the slicer slowed layers for cooling they didn't
    need, it undoes that (faster, better bonded). Two plans, both checked with the heat simulation: ``gcode``
    (fan and speeds per layer) and ``slicer`` (slower heights and a cooling tower: pass its ``cooling`` to
    export_3mf). ``summary`` says what changes, in plain words. loads / intents as for heat_model."""
    return _clean(api.optimize_cooling(up, _profile(printer, material, nozzle, goal), loads, intents, fine))


@server.tool()
def optimize_gcode_file(up: list[float], gcode_path: str, output_path: str, printer: str = "bambu_p1s", material: str = "pla",
                        nozzle: str = "0.4", goal: str = "balanced", loads: list[dict] | None = None,
                        intents: list[dict] | None = None) -> dict:
    """Write the cooling plan (optimize_cooling's ``gcode`` plan) into a copy of a plain .gcode file: only speeds,
    part fan commands and short park moves change; the original stays as it is. The copy is read back and
    simulated first and only written when nothing got worse; else ``problems`` says why. Load the same file
    with load_gcode first."""
    src = _in(gcode_path, (".gcode", ".gco", ".g"), "G-code file")
    r = api.optimize_gcode(up, src.read_bytes(), _profile(printer, material, nozzle, goal), loads, intents)
    if not r["ok"]:
        return {"ok": False, "problems": r["problems"]}
    out = _out(output_path, (".gcode",), "G-code path")
    out.write_bytes(r.pop("data"))
    return {**_clean(r), "path": str(out)}


@server.tool()
def heat_tuning_gcode(kind: str, gcode_path: str, output_path: str, material: str = "pla") -> dict:
    """The G-code for a tuning print (calibration_part "sag", "snap" or "temp"), from the user's sliced test part: sag
    switches the slicer's slowdown for cooling off, snap steps the fan down every 8 mm, temp sets each block of the
    temperature tower to its temperature (``material``'s, as on the tower). Keep the written file: tune_heat_window
    reads it with the result (sag, snap)."""
    src = _in(gcode_path, (".gcode", ".gco", ".g"), "G-code file")
    r = api.tuning_gcode(kind, src.read_bytes(), {"material": material})
    out = _out(output_path, (".gcode",), "G-code path")
    out.write_bytes(r.pop("data"))
    return {**r, "path": str(out)}


@server.tool()
def tune_heat_window(kind: str, gcode_path: str, at_mm: float | None = None, printer: str = "bambu_p1s", material: str = "pla",
                     nozzle: str = "0.4") -> dict:
    """What a heat tuning print says, from its test G-code (heat_tuning_gcode's file): sag ``at_mm`` where the tallest
    pin first drooped (None: none did), snap ``at_mm`` below which the wall broke easily (None: it held everywhere).
    Returns the value (``sag_limit_c`` or ``min_bond``) to save with the material or pass as the profile's heat_tuning."""
    src = _in(gcode_path, (".gcode", ".gco", ".g"), "G-code file")
    return api.tune_heat(kind, src.read_bytes(), at_mm, _profile(printer, material, nozzle))


@server.tool()
def spot_importance(up: list[float], loads: list[dict] | None = None, printer: str = "bambu_p1s", material: str = "pla",
                    nozzle: str = "0.4", goal: str = "balanced", intents: list[dict] | None = None) -> dict:
    """How much each spot of the part matters for bonding between layers (weight 0.2–1), which heat_model uses
    to be strict where it counts. From ``loads`` (as for check_strength) when given, else guessed from the shape
    (``guessed``: pins, ribs and their roots, sharp inside corners, screw holes, outer walls). ``areas`` lists the
    important spots with the reason in plain words; per-spot arrays come as bytes."""
    return _clean(api.importance(up, loads, _profile(printer, material, nozzle, goal), intents))


@server.tool()
def check_warping(up: list[float], printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4",
                  detailed: bool = False) -> dict:
    """Warping simulation: how hard the base wants to curl off the bed (``lift_ratio``: 1 = like the longest
    bar that stays flat for this material and printer, above 1 it likely lifts), where it lifts first
    (``lift_points_print``) and the stress left inside after cooling. ``detailed`` uses the G-code from
    load_gcode (its real layer times)."""
    return _clean(api.warping(up, _profile(printer, material, nozzle), detailed))


@server.tool()
def load_gcode(path: str, up: list[float], printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4") -> dict:
    """Read the G-code the part was sliced to (from our 3MF, in orientation ``up``; plain G-code or a
    ``.gcode.3mf``) and check that it is this part. Afterwards check_warping(detailed=True) uses its real
    layer times; detailed_strength does the same for strength (and can slice by itself). Also reports the
    firmware and ``slowdown_min``: minutes added by each reason the slicer printed lines slower than set
    (overhang, bridge, small_perimeter, volumetric_cap, layer_time; only layer_time is for cooling)."""
    return _clean(api.load_gcode(_in(path, GCODE, "G-code file").read_bytes(), up, _profile(printer, material, nozzle)))


@server.tool()
def add_pause_to_gcode(path: str, output_path: str, pause_heights: list[float]) -> dict:
    """Put a pause into a G-code the user sliced (plain or Bambu Studio's ``.gcode.3mf``), before the first
    layer above each height in ``pause_heights`` (``top_z`` of the pause_to_insert findings), so they can drop
    a magnet or nut in. For Bambu Studio, which ignores the pause in export_3mf's file; OrcaSlicer and
    PrusaSlicer take it from there. Saves the file to ``output_path``."""
    src = _in(path, GCODE, "G-code file")
    out = _out(output_path, GCODE, "G-code file")
    r = api.pause_gcode(src.read_bytes(), pause_heights)
    out.write_bytes(r["data"])
    return {"saved": str(out), "pauses": r["pauses"]}


@server.tool()
def diagnose_failed_print(symptom: str | None = None, up: list[float] | None = None, printer: str = "bambu_p1s",
                          material: str = "pla", nozzle: str = "0.4") -> dict:
    """"My print failed": what went wrong (``symptom``: warped_corner, layer_split, stringing, sagging_hole,
    elephant_foot, rough_overhang, came_loose, holes_wrong_size, missing_detail, seam_blobs, broke_in_use; None
    lists them with labels). Returns the usual causes and fixes, and with ``up`` (the orientation it was printed
    in) ``on_your_part``: the findings on the loaded part that predicted it - start the answer with those."""
    return _clean(api.diagnose(symptom, up, _profile(printer, material, nozzle) if up is not None else None))


@server.tool()
def check_clearance(path: str, printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4") -> dict:
    """The gap between the loaded part and a mating part (STL / 3MF at ``path``, exported from the same CAD
    assembly so both keep their places): the closest gap per side (negative = overlap), where, and the fit it
    gives (overlap, too tight, press, snug, sliding, loose, free) with what to leave for the next looser fit."""
    return _clean(api.clearance(_in(path, PARTS, "part file").read_bytes(), _profile(printer, material, nozzle)))


@server.tool()
def check_setup(printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4", settings: dict[str, str] | None = None) -> dict:
    """Mistakes in a printer / material / nozzle choice and slicer values (``settings``, OrcaSlicer keys such as
    layer_height, support_top_z_distance, outer_wall_speed): layers too thick for the nozzle, support touching the
    part, faster than the hotend melts, PETG on smooth PEI, abrasive filament in a brass nozzle. No part needed."""
    return _clean(api.setup_check(_profile(printer, material, nozzle), settings))


@server.tool()
def printer_tolerance(printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4") -> dict:
    """The fit gaps in use (typical, or measured with the fit test print in My materials) and the slicer hole
    compensation that follows from them (None until the fit test is measured)."""
    return _clean(api.tolerance(_profile(printer, material, nozzle)))


@server.tool()
def heat_curve(material: str = "pla", use_temp_c: float | None = None) -> dict:
    """How much of its strength a material keeps from −20 °C to past its heat limit (``temps_c`` with
    ``share_pct``), where it gets brittle (``cold_c``) or soft (``soft_c``), and a plain sentence for
    ``use_temp_c``. No part needed."""
    return _clean(api.heat_curve({"material": material}, use_temp_c))


@server.tool()
def box_heat(power_w: float, ambient_c: float = 25.0, cooling: str = "closed", point: list[float] | None = None,
             face: int | None = None, printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4") -> dict:
    """The loaded part as a box for electronics using ``power_w`` watts in a room at ``ambient_c``: the air
    temperature inside and whether the plastic stays below where it goes soft. ``cooling``: "closed" or
    "fan" (vents land in between). Where the electronics sit: ``point`` (part coordinates, mm) or
    ``face`` (a triangle index; they sit just inside it); default the middle. ``wall_max_c`` and
    ``hottest_point`` say which wall gets warmest."""
    return _clean(api.box_heat(power_w, ambient_c, cooling, _profile(printer, material, nozzle), point, face))


@server.tool()
def add_zone(up: list[float], region: int | None = None, point: list[float] | None = None, margin: float = 3.0,
             walls: int | None = None, infill: float | None = None, shape: str | None = None,
             printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4") -> dict:
    """A "strong in one area" zone: an upright box (or "cylinder" / "ball" ``shape``) around a face
    (``region`` from list_faces) or a spot (``point``, part coordinates), ``margin`` mm bigger, printed
    with more walls and denser infill (default two more walls and 40 %). Pass returned zones as
    ``zones`` to strength_variant or export_3mf."""
    return _clean(api.zone_at(up, region, point, margin, walls, infill, _profile(printer, material, nozzle), shape))


@server.tool()
def place_box(up: list[float], kind: str, region: int | None = None, point: list[float] | None = None,
              point2: list[float] | None = None, margin: float | None = None, shape: str = "box",
              printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4") -> dict:
    """An advanced design box around a face (``region``), a spot (``point``) or between two spots
    (``point`` and ``point2``, part coordinates). ``kind``: "support" / "blocker" (force or forbid
    supports there), "void" (a hidden gap the slicer prints walls round), "keep_clear" (space that must
    stay empty: pass it in ``keep_clear`` to suggest_design_changes), "add_solid" / "cut_solid" (plastic
    to add or cut: apply_blocks). Returns the box and plain notes on what it does here."""
    return _clean(api.volume_at(up, kind, region, point, point2, margin, _profile(printer, material, nozzle), shape))


@server.tool()
def move_box(up: list[float], box: dict, by: list[float], printer: str = "bambu_p1s", material: str = "pla",
             nozzle: str = "0.4") -> dict:
    """A box from place_box moved by ``by`` mm (part coordinates), same size, with new notes."""
    return _clean(api.volume_move(up, box, by, _profile(printer, material, nozzle)))


@server.tool()
def apply_blocks(up: list[float], boxes: list[dict], output_path: str) -> dict:
    """Join the "add_solid" boxes onto the part and cut the "cut_solid" ones out of it (boxes from
    place_box with the same ``up``), save the changed STL and load it as the part."""
    edits = api.volume_solid(up, boxes)
    if not edits:
        raise ValueError('No "add_solid" or "cut_solid" boxes given')
    return _save_and_load_edits(api._require().mesh, edits, output_path)


@server.tool()
def snap_model(path: str, point: list[float], normal: list[float], output_path: str, spin_deg: float = 0.0) -> dict:
    """Put another model (STL / 3MF at ``path``) onto the part: its biggest flat face goes flat onto the
    part's face at ``point`` (part coordinates; ``normal`` points out of the part, e.g. that face's normal
    from list_faces), centred there and turned ``spin_deg``. Joined into one part, saved as STL and loaded."""
    src = _in(path, PARTS, "model file")
    edit = api.snap_model(src.read_bytes(), point, normal, spin_deg, src.stem)
    return {"placed": edit["description"], **_save_and_load_edits(api._require().mesh, [edit], output_path)}


@server.tool()
def strength_variant(
    up: list[float],
    loads: list[dict],
    variant_path: str,
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    walls: int | None = None,
    infill: float | None = None,
    top: int | None = None,
    bottom: int | None = None,
    zones: list[dict] | None = None,
) -> dict:
    """Strength of a changed copy of the part (STL / 3MF at ``variant_path``) without replacing the loaded
    one: for comparing design ideas. Loads move to the matching faces of the copy. Returns the safety
    factor, verdict and how much plastic the change adds (``added_g``). loads as in check_strength."""
    data = _in(variant_path, PARTS, "part file").read_bytes()
    return _clean(api.strength_variant(up, loads, data, _profile(printer, material, nozzle), walls, infill, top, bottom, zones))


@server.tool()
def expert_settings() -> dict:
    """Every number the checks use (analysis grids and all thresholds), grouped, each with its default,
    the value in use, its range and the note on what it does and where it comes from."""
    return api.expert_settings()


@server.tool()
def set_expert_settings(values: dict[str, float]) -> dict:
    """Change numbers from expert_settings by ``path``, e.g. {"analysis.strength_voxels": 20000}.
    Values not given go back to their defaults. Only for users who know why they change them."""
    return api.set_expert(values)


@server.tool()
def render_image(view: str, up: list[float], output_path: str, loads: list[dict] | None = None, finding: int = 0,
                 other_path: str | None = None, width: int = 800, height: int = 600, printer: str = "bambu_p1s",
                 material: str = "pla", nozzle: str = "0.4") -> list:
    """PNG picture of the loaded part printed with ``up`` as the build direction, saved to ``output_path`` and
    returned so you can see it. ``view``: "bed" (on the bed), "overhangs" (seen from below; red needs
    support, amber borderline, green on the bed), "problem" (finding number ``finding`` from check_print,
    its area in red), "stress" (needs ``loads`` like check_strength; blue little stress, red close to
    breaking, blue faces held, purple arrows the forces), "warp" (curled shape after cooling,
    exaggerated), "compare" (``other_path``, an earlier STL / 3MF, on the left, the loaded part on the
    right). Takes 1–8 s."""
    from . import render

    other = _in(other_path, PARTS, "part file").read_bytes() if other_path else None
    data = render.picture(view, up, _profile(printer, material, nozzle), loads, finding, other, width, height)
    out = _out(output_path, (".png",), "picture path")
    out.write_bytes(data)
    return [f"saved {out}", Image(data=data, format="png")]


@server.tool()
def save_project(output_path: str, up: list[float], loads: list[dict] | None = None, faces: dict[str, list[int]] | None = None,
                 zones: list[dict] | None = None, boxes: list[dict] | None = None, settings: dict[str, str] | None = None,
                 printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4", goal: str = "balanced",
                 use_temp_c: float | None = None, mode: str = "both") -> dict:
    """Save the loaded part (as it is now, after scaling and design changes) with the build direction,
    loads, marked ``faces``, ``zones`` (add_zone / optimise_strength), ``boxes`` (place_box) and slicer
    ``settings`` as a project file (.json). The user opens it in the web app (drop it in, or "Open")
    and sees in 3D what you did; projects saved there open with open_project. Holes, screws and slots sized
    with set_fit / set_fastener / set_gap are saved with it."""
    import numpy as np

    from . import project

    s = api._require()
    regions = np.frombuffer(api.regions()["face_regions"], "<u4")  # same face ids after reloading
    text = project.dump(s.name, to_binary_stl(s.mesh), regions.tolist(), profile=_profile(printer, material, nozzle, goal, use_temp_c),
                        mode=mode, up=[float(v) for v in up], loads=loads, faces=faces, zones=zones, volumes=boxes, settings=settings,
                        holes=api.holes_state())
    out = _out(output_path, PROJECT, "project path")
    out.write_text(text, encoding="utf-8")
    return {"saved": str(out)}


@server.tool()
def open_project(path: str) -> dict:
    """Open a project file (from the web app's "Save project" or save_project): loads the part and returns
    what was set: ``profile`` (printer, material, nozzle, goal), ``up``, ``loads``, ``faces``, ``zones``,
    ``volumes`` (pass as boxes / keep_clear), ``settings``. Use them in the next calls. Holes, screws and
    slots sized in it are sized again (``holes_sized``: how many; design_changes then offers them)."""
    from . import project

    p = project.read(_in(path, PROJECT, "project file").read_bytes())
    info = _clean(api.load(p.pop("stl"), p.pop("name"), p.pop("face_ids")))
    holes = p.pop("holes", None)
    if holes and p.get("up"):
        r = api.restore_holes(holes, p["up"], p.get("profile"))
        p["holes_sized"] = len(r["fits"]) + len(r["fasteners"]) + len(r["gaps"])
    return {**p, "part": info}


@server.tool()
def suggest_lighter_shape(up: list[float], loads: list[dict], output_path: str, keep: float = 0.4, fine: bool = False,
                          printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4") -> dict:
    """Topology optimisation: a lighter shape that keeps only ``keep`` (0.15–0.9, default 0.4) of the plastic,
    where ``loads`` (as in check_strength) run through the part. Saves it as a blocky STL at ``output_path``
    for the user to redesign the part from in CAD (not a finished part; it only removes plastic). Compares
    the plastic and the safety factor (both printed solid) with the whole part. ~10–30 s, ``fine`` ~4× longer."""
    r = api.topology(up, loads, _profile(printer, material, nozzle), keep, fine)
    out = _out(output_path, (".stl",), "STL path")
    out.write_bytes(r["stl"])
    return {"saved": str(out), **_clean(r)}


@server.tool()
def plan_plate(up: list[float], copies: int | None = None, printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4") -> dict:
    """Several copies of the loaded part on one plate, printed ``up``: how many fit (``most``), where ``copies`` of
    them go (offsets from the bed's centre; all that fit when None), the plate's print time and plastic (one
    part's from the check, times the copies, rough), and what printing them all at once or one after another
    means. export_3mf(copies=...) writes them into the 3MF."""
    prof = _profile(printer, material, nozzle)
    plan = api.plate(up, prof, copies)
    stats = api.check(up, prof)["stats"]
    one_min, one_g = stats.get("print_time_min"), stats.get("total_g", stats.get("filament_g"))
    if one_min is not None:
        plan["plate_time_min"] = round(one_min * plan["copies"])
    if one_g is not None:
        plan["plate_grams"] = round(one_g * plan["copies"], 1)
    return plan


@server.tool()
def split_part(output_folder: str, joint: str = "pins", purpose: str = "fit", printer: str = "bambu_p1s", material: str = "pla",
               nozzle: str = "0.4", loads: list[dict] | None = None) -> dict:
    """Cut a part that is too big for the printer flat into pieces that fit, with holes for alignment pins
    (``joint`` "pins") or a sliding dovetail ("dovetail"). Saves piece_1.stl, piece_2.stl, … (and pin.stl:
    print that many pins, standing up) into ``output_folder`` and says where it cut and how to put the pieces
    together. ``purpose`` "supports": cut a part that fits so each piece needs much less support (errors when no
    cut helps); "strength" (with ``loads`` as for check_strength): cut so each piece prints with its layers along its own
    load and together they hold clearly more (errors when no cut does). ``rotations``: how to turn each piece to print
    it. The loaded part stays as it was."""
    from .booleans import apply_edits
    from .mesh import Mesh
    import numpy as np

    plan = api.split(_profile(printer, material, nozzle), joint, purpose, loads)
    folder = Path(output_folder).expanduser().resolve()
    paths = [_out(str(folder / f"piece_{i}.stl"), (".stl",), "piece") for i in range(1, len(plan["pieces"]) + 1)]
    pin = _out(str(folder / "pin.stl"), (".stl",), "pin")
    folder.mkdir(parents=True, exist_ok=True)
    part = api._require().mesh
    saved = []
    for out, piece in zip(paths, plan["pieces"]):
        edits = [{**e, "mesh": np.frombuffer(e["mesh"], "<f4").reshape(-1, 3, 3)} for e in piece["edits"]]
        out.write_bytes(to_binary_stl(apply_edits(part, edits, check=False)))  # cutting it apart is the point
        saved.append(str(out))
    if plan["pin"]["count"]:
        pin.write_bytes(to_binary_stl(Mesh(np.frombuffer(plan["pin"]["stl_triangles"], "<f4").reshape(-1, 3, 3))))
        saved.append(str(pin))
    return {"saved": saved, "cuts_mm": plan["cuts"], "joint": plan["joint"], "pins": plan["pin"]["count"],
            "rotations": [p["rotation"] for p in plan["pieces"]], "explanation": plan["explanation"]}


def _save_and_load(mesh, output_path: str) -> dict:
    out = _out(output_path, (".stl",), "STL path")
    out.write_bytes(to_binary_stl(mesh))
    return {"saved": str(out), **_clean(api.load(out.read_bytes(), out.name))}


def _save_and_load_edits(part, edits: list[dict], output_path: str) -> dict:
    """Apply ``edits`` with the after-change checks (T-434), save and load the result; ``left_out``
    says which edits were refused and why, in words to pass on to the user."""
    from .booleans import apply_edits

    refused: list[dict] = []
    result = _save_and_load(apply_edits(part, edits, refused), output_path)
    if refused:
        result["left_out"] = [f"Left out \"{r['title']}\": {r['reason']}." for r in refused]
    return result


# The tool for every api.METHODS entry (test_mcp fails when a new API method has none).
API_TOOLS = {
    "presets": "list_presets",
    "custom_presets": "set_custom_presets",
    "calibration_part": "calibration_part",
    "calibrate_bed_grip": "calibrate_bed_grip",
    "load": "load_part",
    "set_cad_checks": "set_cad_checks",
    "load_gcode": "load_gcode",
    "heat_model": "heat_model",
    "importance": "spot_importance",
    "optimize_cooling": "optimize_cooling",
    "optimize_gcode": "optimize_gcode_file",
    "tuning_gcode": "heat_tuning_gcode",
    "tune_heat": "tune_heat_window",
    "pause_gcode": "add_pause_to_gcode",
    "setup_check": "check_setup",
    "tolerance": "printer_tolerance",
    "clearance": "check_clearance",
    "diagnose": "diagnose_failed_print",
    "warping": "check_warping",
    "set_fit": "set_fit",
    "set_fastener": "set_fastener",
    "round_features": "list_round_features",
    "fastener_suggestions": "fastener_suggestions",
    "split": "split_part",
    "plate": "plan_plate",
    "set_gap": "set_gap",
    "volume_solid": "apply_blocks",
    "snap_model": "snap_model",
    "material_advice": "material_advice",
    "heat_curve": "heat_curve",
    "dynamics": "check_dynamics",
    "select_body": "select_body",
    "scale": "scale_part",
    "orientations": "find_orientations",
    "orientation": "check_orientation",
    "check": "check_print",
    "export": "export_3mf",
    "report": "get_report",
    "regions": "list_faces",
    "visible_faces": "guess_visible_faces",
    "optimise_strength": "optimise_strength",
    "expert_settings": "expert_settings",
    "set_expert": "set_expert_settings",
    "box_heat": "box_heat",
    "zone_at": "add_zone",
    "volume_at": "place_box",
    "volume_move": "move_box",
    "tag_feature": "tag_feature",
    "strength": "check_strength",
    "strength_variant": "strength_variant",
    "strength_options": "strength_options",
    "stronger_orientation": "stronger_orientation",
    "design": "suggest_design_changes",
    "compare_nozzles": "compare_nozzles",
    "optimize": "optimize_print",
    "estimate_runtime": "estimate_optimize_runtime",
    "topology": "suggest_lighter_shape",
}
# Only for the web app: it keeps the engine's state while restarting its worker (Cancel). The MCP
# server's engine never restarts under the agent, so there is nothing to hand back.
APP_ONLY = {"session_state", "restore_state", "holes_state", "restore_holes",  # agents: save_project / open_project
            "print_mesh",  # the browser slicer's input; agents slice with export + optimize_print_cooling / local OrcaSlicer
            "clearance_map"}  # the gap painted on the part, for the 3D view; agents use check_clearance's numbers


def main():
    import argparse

    ap = argparse.ArgumentParser(description="FDM DFM tool for AI agents (MCP over stdio)")
    ap.add_argument("--folder", help="only read and write files inside this folder")
    a = ap.parse_args()
    if a.folder:
        os.environ["FDM_DFM_FOLDER"] = a.folder
    server.run("stdio")


if __name__ == "__main__":
    main()
