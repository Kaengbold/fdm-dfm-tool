"""Fusion-independent part of the add-in: run the engine on an exported STL and turn its
results into what the add-in shows and builds. No ``adsk`` imports, so it is testable outside
Fusion (core/tests/test_fusion_bridge.py) and under Fusion's own Python.

Lengths here are millimetres (the add-in exports the STL in mm and converts to Fusion's cm).
"""

from __future__ import annotations

import html
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Dependencies and engine copy from install_deps.py (per Python version, as wheels are); in a repo checkout
# the engine in core/ is used instead of the copy.
LIB = os.path.join(HERE, "lib", f"py{sys.version_info[0]}.{sys.version_info[1]}")
for path in (LIB, os.path.normpath(os.path.join(HERE, "..", "..", "core"))):  # later wins: repo engine over the copy
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)


def presets() -> dict:
    from fdmcore import api

    p = api.get_presets()
    return {"printers": {k: v["name"] for k, v in p["printers"].items()},
            "materials": {k: v["name"] for k, v in p["materials"].items()}}


def analyse(stl: bytes, printer: str, material: str, goal: str = "balanced", name: str = "part") -> dict:
    """Best orientation, its report, and the sideways holes that get a teardrop in it."""
    from fdmcore import api

    profile = {"printer": printer, "material": material, "goal": goal}
    api.load(stl, name)
    best = api.orientations(profile)["orientations"][0]
    design = api.design(best["up"], profile)
    holes = {f["region"]: f for f in design["features"]}
    teardrops = [holes[int(e["id"].split("-")[1])] for e in design["edits"] if e["id"].startswith("teardrop-")]
    return {
        "up": best["up"],
        "reasons": best.get("reasons", []),
        "report": api.report(best["up"], profile, "both"),
        "teardrops": teardrops,
    }


def teardrop_boxes(hole: dict, up) -> tuple[dict, dict]:
    """Two oriented boxes whose intersection is the 45° roof that turns a sideways hole into a
    teardrop: a square turned 45° around the hole axis (its sides touch the circle at 45°, its top
    corner is the tip), cut to the part above the two touching points. Each box is
    {"center", "length_dir", "width_dir", "length", "width", "height"} (mm), as for
    adsk.core.OrientedBoundingBox3D.create (height runs along length_dir × width_dir)."""
    axis = _unit(hole["axis"])
    u = _unit(_sub(up, _scale(axis, _dot(up, axis))))  # "up" across the hole
    s = _cross(axis, u)
    r, c, length = hole["radius"], hole["center"], hole["length"] + 2.0  # a little past both ends
    diag = _unit(_add(u, s))
    diamond = {"center": list(c), "length_dir": axis, "width_dir": diag, "length": length, "width": 2 * r, "height": 2 * r}
    lo, hi = r / math.sqrt(2) - 0.01 * r, r * math.sqrt(2) + 0.05 * r  # from just below the touching points to past the tip
    upper = {"center": _add(c, _scale(u, (lo + hi) / 2)), "length_dir": axis, "width_dir": u,
             "length": length, "width": hi - lo, "height": 4 * r}
    return diamond, upper


def report_html(markdown: str, up) -> str:
    """The engine's Markdown report as a small HTML page for a Fusion palette."""
    out = [f"<p><b>Print with this direction up:</b> [{', '.join(f'{v:.2f}' for v in up)}] (in the part's own axes)</p>"]
    in_list = False
    for line in markdown.splitlines():
        text = _inline(html.escape(line.lstrip("#-| ").strip()))
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{text}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("### "):
            out.append(f"<h3>{text}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{text}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{text}</h1>")
        elif line.startswith("|") and not set(line) <= set("|-: "):
            cells = [_inline(html.escape(c.strip())) for c in line.strip().strip("|").split("|")]
            out.append(f"<p class='row'>{' · '.join(c for c in cells if c)}</p>")
        elif text:
            out.append(f"<p>{text}</p>")
    if in_list:
        out.append("</ul>")
    style = ("body{font-family:Segoe UI,sans-serif;font-size:13px;margin:8px}h1{font-size:16px}h2{font-size:14px}"
             "h3{font-size:13px;margin:10px 0 2px}p{margin:3px 0}ul{margin:2px 0 6px 16px;padding:0}")
    return f"<!doctype html><meta charset='utf-8'><style>{style}</style>" + "\n".join(out)


def _inline(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", text)


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _add(a, b):
    return [x + y for x, y in zip(a, b)]


def _sub(a, b):
    return [x - y for x, y in zip(a, b)]


def _scale(a, k):
    return [x * k for x in a]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _unit(a):
    n = math.sqrt(_dot(a, a))
    return [x / n for x in a]
