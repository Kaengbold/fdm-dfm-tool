"""MCP server: lets an AI agent (Claude Code, Codex, Claude Desktop, …) operate the tool.

Runs locally over stdio with the user's own agent subscription — the tool itself
never talks to an AI provider. Every tool maps onto fdmcore.api, the same API the
web app uses.

    pip install -e "core[local]"
    claude mcp add fdm-dfm -- python -m fdmcore.mcp_server

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

from mcp.server.mcpserver import MCPServer  # noqa: E402  (after the thread cap)

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
    Replaces any previously loaded part."""
    p = Path(path).expanduser()
    return _clean(api.load(p.read_bytes(), p.name))


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
    faces: dict[str, list[int]] | None = None,
) -> dict:
    """Rank print orientations. With loads (see check_strength), strength is included in the ranking.
    ``faces`` marks faces (ids from list_faces) the user cares about: {"clean": [...] no support marks,
    "bed": [...] on the bed, "vertical": [...] upright, e.g. a hole that must come out round}."""
    return _clean(api.orientations(_profile(printer, material, nozzle), loads=loads or None, faces=faces))


@server.tool()
def check_print(
    up: list[float],
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    mode: str = "both",
    faces: dict[str, list[int]] | None = None,
    use_temp_c: float | None = None,
) -> dict:
    """Printability findings, rough print time/filament and recommended slicer settings for one orientation.
    mode: 'print' (can't change the model), 'design' (can't change slicer settings) or 'both'.
    ``faces`` as in find_orientations: support on a "clean" face is reported as a problem.
    use_temp_c: hottest the part gets in use (warns near the material's heat limit)."""
    return _clean(api.check(up, _profile(printer, material, nozzle, use_temp_c=use_temp_c), mode, faces))


@server.tool()
def list_faces() -> list[dict]:
    """Faces (regions) of the part with id, flat/curved, area, normal and centroid — use ids for loads."""
    return api.regions()["regions"]


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
) -> dict:
    """Rough strength check. use_temp_c: hottest the part gets in use (weaker when warm, soft near the
    material's heat limit); constant_load: the load stays on (creep: needs ×3 instead of ×2). fine=True: a finer grid (slower); compare with the normal one to see how
    far to trust the answer. walls (lines), infill (0–1), top/bottom (solid layers): how it is
    printed; omitted = the goal's usual settings. loads: [{"region": id, "kind": "fixed", "hold": "clamped"|"resting"|"screw"},
    {"region": id, "kind": "force", "kg": 5, "direction": "push"|"pull"|"down"|[x,y,z], "case": 1}].
    Forces with different ``case`` numbers are separate situations; the weakest decides (all listed in ``cases``).
    Instead of "region", a load can sit on a spot: "point": [x, y, z] (part coordinates) with "radius" mm
    (default 3) — for an edge, a corner or part of a big face.
    Returns safety factor, verdict and failure mode ('layers' = layers separate, 'material' = plastic yields)."""
    return _clean(api.strength(up, loads, _profile(printer, material, nozzle, use_temp_c=use_temp_c, constant_load=constant_load),
                               walls, infill, top, bottom, fine))


@server.tool()
def check_dynamics(
    up: list[float],
    loads: list[dict],
    printer: str = "bambu_p1s",
    material: str = "pla",
    nozzle: str = "0.4",
    drop_mm: float = 0.0,
    mounted_kg: float = 0.0,
) -> dict:
    """Vibration and knocks (simplified). frequency_hz: how fast the part swings as held — 20–200 Hz is
    where motors, fans and printers shake, so it may resonate; drop_mm: the load falls this far onto the
    part (impact_factor = how many times harder than held still, impact_safety = safety factor then);
    mounted_kg: something fixed on the loaded face that swings with it. loads as in check_strength."""
    r = api.dynamics(up, loads, _profile(printer, material, nozzle), drop_mm, mounted_kg)
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
) -> dict:
    """Strength from the real sliced print: the plastic really in each spot (walls, solid layers, infill)
    and which way the printed lines run — lines pulled apart sideways are a third way to break ('lines').
    Uses ``gcode_path`` (sliced from our 3MF in this orientation), or slices with the installed OrcaSlicer
    (our recommended settings, or ``settings`` overrides; tens of seconds). loads as in check_strength."""
    prof = _profile(printer, material, nozzle)
    if gcode_path:
        text = Path(gcode_path).expanduser().read_text(errors="replace")
        sliced = None
    else:
        sliced = _slice(up, printer, material, nozzle, settings, keep_gcode=True)
        text = sliced["orcaslicer"].pop("gcode")
    out = {"gcode": api.load_gcode(text, up, prof), **_clean(api.strength(up, loads, prof, fine=fine, detailed=True))}
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
    up: list[float], printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4", params: dict | None = None
) -> dict:
    """Detected holes/bosses and suggested geometry edits for this orientation: teardrop holes and corner
    chamfers (cut away, ``op: cut``), 45° fills under ledges, sacrificial layers over holes that start in
    mid-air and thicker walls (added, ``op: add``).
    ``params`` sets edit sizes, e.g. {"chamfer_mm": 2, "teardrop_angle_deg": 40, "ledge_max_mm": 20,
    "wall_lines": 3}; ranges come back in the result."""
    return _clean(api.design(up, _profile(printer, material, nozzle), params))


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
) -> dict:
    """Apply suggested edits (ids from suggest_design_changes, same params), save the modified STL and load it."""
    from .booleans import apply_edits

    edits = {e["id"]: e for e in api.design(up, _profile(printer, material, nozzle), params)["edits"]}
    unknown = [i for i in edit_ids if i not in edits]
    if unknown:
        raise ValueError(f"Unknown edit ids: {unknown}")
    mesh = apply_edits(api._require().mesh, [edits[i] for i in edit_ids])
    out = Path(output_path).expanduser()
    out.write_bytes(to_binary_stl(mesh))
    return {"saved": str(out), **_clean(api.load(out.read_bytes(), out.name))}


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
) -> str:
    """Write the oriented part as 3MF with slicer settings (recommended ones unless overridden).
    ``zones`` (from optimise_strength's ``zoned``) become modifier volumes; then the file is for one
    slicer: ``slicer`` "orca" (OrcaSlicer and Bambu Studio) or "prusa"."""
    prof = _profile(printer, material, nozzle)
    merged = {r["key"]: r["value"] for r in api.check(up, prof)["settings"]}
    merged.update(settings or {})
    out = Path(output_path).expanduser()
    out.write_bytes(api.export(up, prof, merged, zones, slicer))
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


def _slice(up, printer: str, material: str, nozzle: str, settings: dict | None, keep_gcode: bool = False) -> dict:
    from .profile import Profile, presets
    from .slicer_cli import find_orca, match_profiles, slice_3mf

    orca = find_orca()
    if orca is None:
        raise RuntimeError("OrcaSlicer not found. Install it, or set ORCA_SLICER to its executable.")
    prof = _profile(printer, material, nozzle)
    result = api.check(up, prof)
    merged = {r["key"]: r["value"] for r in result["settings"]}
    merged.update(settings or {})
    p = Profile.from_dict(prof)
    resources = orca.parent / "resources" / "profiles"
    if not resources.exists():  # macOS app bundle
        resources = orca.parents[1] / "Resources" / "profiles"
    base_material = presets()["materials"][material].get("based_on", material)  # the user's own presets name one
    matched = match_profiles(resources, presets()["printers"][printer]["name"], nozzle, base_material, float(merged.get("layer_height", p.layer_height)))
    sliced = slice_3mf(api.export(up, prof, merged), matched, orca, keep_gcode=keep_gcode)
    if "filament_cm3" in sliced:
        sliced["filament_g"] = round(sliced["filament_cm3"] * p.material["density_g_cm3"], 1)
    return {
        "orcaslicer": sliced,
        "estimate": {k: round(result["stats"][k], 1) for k in ("print_time_min", "filament_g")},
        "profiles": {k: v.stem for k, v in matched.items()},
        "settings": merged,
    }


@server.tool()
def get_report(up: list[float], printer: str = "bambu_p1s", material: str = "pla", nozzle: str = "0.4", mode: str = "both") -> str:
    """Markdown report of findings, fixes and settings."""
    return api.report(up, _profile(printer, material, nozzle), mode)


def main():
    server.run("stdio")


if __name__ == "__main__":
    main()
