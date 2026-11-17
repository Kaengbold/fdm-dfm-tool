"""Command line ``fdmdfm``: the engine for agents and scripts that don't speak MCP.

One part per call, JSON on stdout (files where a command makes one), so an agent can run e.g.

    fdmdfm check part.stl --material petg
    fdmdfm faces part.stl
    fdmdfm strength part.stl --loads '[{"region": 3, "kind": "fixed"}, {"region": 7, "kind": "force", "kg": 5}]'
    fdmdfm report part.stl -o report.html
    fdmdfm chart part.stl --kind nozzles -o time.svg
    fdmdfm export part.stl -o part.3mf
    fdmdfm render part.stl --view overhangs -o under.png

Without ``--up`` every command uses the best orientation (as ``orient`` ranks it first). Errors go to
stderr as one line with exit code 1, so a script can tell them apart from results.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Same thread cap as the MCP server: simulations must leave the computer usable while they run.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, str(max(1, (os.cpu_count() or 2) // 2)))

from . import api  # noqa: E402  (after the thread cap)


def _json_arg(text: str | None):
    """A JSON value given inline or as the path of a .json file."""
    if text is None:
        return None
    path = Path(text).expanduser()
    return json.loads(path.read_text(encoding="utf-8") if path.suffix == ".json" and path.exists() else text)


def _clean(value):
    """Drop binary payloads (meant for the 3D viewer) from results."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if not isinstance(v, (bytes, bytearray))}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


DEFAULTS = {"printer": "bambu_p1s", "material": "pla", "nozzle": "0.4", "goal": "balanced", "use_temp_c": None}


def _setup(a) -> tuple[dict, list[float]]:
    """Load the part and return the profile and the build direction (``--up`` or the best one)."""
    if a.presets:
        p = _json_arg(a.presets)
        api.custom_presets(p.get("printers"), p.get("materials"))
    path = Path(a.part).expanduser()
    saved = {}
    if path.suffix.lower() == ".json":  # a project file: its part and settings, flags given win
        from . import project

        saved = project.read(path.read_bytes())
        api.load(saved["stl"], saved["name"], saved["face_ids"])
        boxes = saved.get("zones", []) + saved.get("volumes", [])  # export takes zones and design boxes together
        for key, value in (("loads", saved.get("loads")), ("zones", boxes), ("faces", saved.get("faces"))):
            if getattr(a, key) is None and value:
                setattr(a, key, json.dumps(value))
    else:
        api.load(path.read_bytes(), path.name)
    a.saved_settings = saved.get("settings", {})
    a.saved_excluded_settings = saved.get("excluded_settings", [])
    if getattr(a, "mode", "") is None:  # not given: the project's, else both
        a.mode = saved.get("mode") or "both"
    if a.scale != 1:
        api.scale(a.scale)
    given = {"printer": a.printer, "material": a.material, "nozzle": a.nozzle, "goal": a.goal, "use_temp_c": a.use_temp}
    profile = {**DEFAULTS, **saved.get("profile", {}), **{k: v for k, v in given.items() if v is not None}}
    if a.up:
        up = [float(v) for v in a.up.split(",")]
    else:
        up = saved.get("up") or api.orientations(profile, top=1)["orientations"][0]["up"]
    return profile, up


def _need_loads(a) -> list[dict]:
    loads = _json_arg(a.loads)
    if not loads:
        raise ValueError("--loads is required: at least one fixed face and one force (face ids from `fdmdfm faces`)")
    return loads


def cmd_orient(a, profile, up):
    return api.orientations(profile, top=a.top, loads=_json_arg(a.loads), faces=_json_arg(a.faces))


def cmd_check(a, profile, up):
    return api.check(up, profile, a.mode, _json_arg(a.faces))


def cmd_faces(a, profile, up):
    return api.regions()["regions"]


def cmd_strength(a, profile, up):
    out = api.strength(up, _need_loads(a), profile, a.walls, a.infill, zones=_json_arg(a.zones))
    if a.options:
        out["options"] = api.strength_options(up, _need_loads(a), profile, a.walls, a.infill)
    return out


def cmd_optimise(a, profile, up):
    return api.optimise_strength(up, _need_loads(a), profile, a.target, a.zones_too)


def cmd_design(a, profile, up):
    keep_clear = [b for b in _json_arg(a.zones) or [] if b.get("kind") == "keep_clear"]  # from the project's boxes
    return api.design(up, profile, _json_arg(a.params), keep_clear)


def cmd_warping(a, profile, up):
    return api.warping(up, profile)


def cmd_report(a, profile, up):
    if not a.output:
        return {"up": up, "markdown": api.report(up, profile, a.mode)}
    from .reports import write

    return {"up": up, **write(a.output, up, profile, _json_arg(a.loads), a.mode)}


def cmd_chart(a, profile, up):
    from .charts import write

    return {"up": up, "saved": write(a.kind, a.output, up, profile, _json_arg(a.loads))}


def cmd_export(a, profile, up):
    settings = {r["key"]: r["value"] for r in api.check(up, profile)["settings"]}
    given = _json_arg(a.settings) or {}
    settings.update({**a.saved_settings, **given})
    excluded = set(a.saved_excluded_settings) - set(given)  # explicit command-line values win
    settings = {k: v for k, v in settings.items() if k not in excluded}
    out = Path(a.output).expanduser()
    out.write_bytes(api.export(up, profile, settings, _json_arg(a.zones), a.slicer, excluded_settings=list(excluded)))
    return {"up": up, "saved": str(out), "settings": settings}


def cmd_render(a, profile, up):
    from .render import picture

    other = Path(a.other).expanduser().read_bytes() if a.other else None
    loads = _json_arg(a.loads) if a.view == "stress" else None
    out = Path(a.output).expanduser()
    out.write_bytes(picture(a.view, up, profile, loads, a.finding, other, a.width, a.height))
    return {"up": up, "view": a.view, "saved": str(out)}


def cmd_topology(a, profile, up):
    out = Path(a.output).expanduser()
    r = api.topology(up, _need_loads(a), profile, a.keep, a.fine)
    out.write_bytes(r["stl"])
    return {"up": up, "saved": str(out), **_clean(r)}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fdmdfm", description="Check a part for FDM printing. Prints JSON.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("part", help="STL or 3MF file (millimetres), or a project file (.json) from the web app or save_project")
    common.add_argument("--printer", help="printer id (see `fdmdfm presets`; default bambu_p1s or the project's)")
    common.add_argument("--material", help="default pla or the project's")
    common.add_argument("--nozzle", help="default 0.4 or the project's")
    common.add_argument("--goal", choices=["balanced", "strength", "speed", "material"])
    common.add_argument("--up", help="build direction x,y,z in part coordinates (default: the project's, else the best orientation)")
    common.add_argument("--scale", type=float, default=1.0, help="e.g. 25.4 for a part exported in inches")
    common.add_argument("--use-temp", type=float, dest="use_temp", help="hottest the part gets in use, °C")
    common.add_argument("--presets", help="your own printers / materials: JSON or a .json file {printers, materials}")
    common.add_argument("--faces", help='marked faces, e.g. {"clean": [3], "bed": [0]}')
    common.add_argument("--loads", help="loads as JSON or a .json file (see the MCP tool check_strength)")
    common.add_argument("--zones", help="stronger zones (from optimise --zones-too) as JSON or a .json file")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("presets", help="printers, materials and nozzles")
    for name, fn, text in [
        ("orient", cmd_orient, "rank print orientations"),
        ("check", cmd_check, "printability findings, print time, recommended settings"),
        ("faces", cmd_faces, "face ids for --loads and --faces"),
        ("strength", cmd_strength, "rough strength check (needs --loads)"),
        ("optimise", cmd_optimise, "lightest settings that hold the loads"),
        ("design", cmd_design, "suggested design changes"),
        ("topology", cmd_topology, "suggested lighter shape as a blocky STL (needs --loads, -o file)"),
        ("warping", cmd_warping, "warping simulation"),
        ("report", cmd_report, "report: text as JSON, or -o report.html / report.md with pictures and charts"),
        ("chart", cmd_chart, "SVG chart (-o file, --kind)"),
        ("export", cmd_export, "3MF with the settings for your slicer (-o file)"),
        ("render", cmd_render, "PNG picture of the part (-o file, --view)"),
    ]:
        p = sub.add_parser(name, parents=[common], help=text)
        p.set_defaults(fn=fn)
        if name == "orient":
            p.add_argument("--top", type=int, default=5)
        if name in ("check", "report"):
            p.add_argument("--mode", choices=["print", "design", "both"], help="default: the project's, else both")
        if name == "strength":
            p.add_argument("--walls", type=int)
            p.add_argument("--infill", type=float, help="0–1")
            p.add_argument("--options", action="store_true", help="also try more walls / infill / other orientations")
        if name == "optimise":
            p.add_argument("--target", type=float, help="safety factor to reach (default ×2, ×3 for --goal strength)")
            p.add_argument("--zones-too", action="store_true", help="also try stronger zones only where needed")
        if name == "topology":
            p.add_argument("--keep", type=float, default=0.4, help="share of the plastic to keep, 0.15–0.9")
            p.add_argument("--fine", action="store_true", help="finer grid, about 4× slower")
            p.add_argument("-o", "--output", required=True)
        if name == "design":
            p.add_argument("--params", help='edit sizes, e.g. {"chamfer_mm": 2}')
        if name in ("report", "export", "render", "chart"):
            p.add_argument("-o", "--output", required=name != "report")
        if name == "chart":
            from .charts import CHARTS

            p.add_argument("--kind", required=True, choices=CHARTS)
        if name == "render":
            from .render import VIEWS

            p.add_argument("--view", default="bed", choices=VIEWS,
                           help="overhangs: from below; problem: --finding N from check; stress: needs --loads; compare: --other")
            p.add_argument("--finding", type=int, default=0)
            p.add_argument("--other", help="earlier version (STL / 3MF) for --view compare")
            p.add_argument("--width", type=int, default=800)
            p.add_argument("--height", type=int, default=600)
        if name == "export":
            p.add_argument("--settings", help='slicer setting overrides, e.g. {"wall_loops": "4"}')
            p.add_argument("--slicer", default="orca", choices=["orca", "prusa"])
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        if a.command == "presets":
            out = api.get_presets()
        else:
            profile, up = _setup(a)
            out = a.fn(a, profile, up)
    except Exception as err:  # one readable line for the agent instead of a traceback
        print(f"error: {err}", file=sys.stderr)
        return 1
    json.dump(api._plain(_clean(out)), sys.stdout, ensure_ascii=False, separators=(",", ":"))  # compact: agents pay per token
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
