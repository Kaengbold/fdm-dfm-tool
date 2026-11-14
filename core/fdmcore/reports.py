"""The report a person reads, built by the engine for agents: one HTML file (pictures and charts inside,
opens in any browser, prints to PDF) or Markdown with the pictures saved next to it. Same sections as
the web app's report (web/report.js): verdict, how it lies on the bed, problems with close-ups, strength,
warping, print time per nozzle, slicer settings. Pictures come from render.py, charts from charts.py.
"""

from __future__ import annotations

import base64
import html
from datetime import date
from pathlib import Path

from . import api, charts, render
from .profile import Profile

MAX_CLOSE_UPS = 3  # each close-up takes a second or more; the rest of the problems are listed without one
SEVERITY = {"problem": "Problem", "warn": "Warning", "info": "Tip"}  # labels and order as in the web app
DOMAIN = {"geometry": "Design", "slicer": "Slicer", "process": "Printer & material"}

STYLE = """
body { font: 16px/1.5 "Segoe UI", system-ui, sans-serif; color: #1f2328; max-width: 860px; margin: 32px auto; padding: 0 20px; }
h1 { font-size: 28px; margin: 0 0 4px; } h2 { font-size: 21px; margin: 36px 0 10px; padding-top: 18px; border-top: 1px solid #d5ddd7; }
h3 { font-size: 17px; margin: 0 0 6px; } p { margin: 6px 0; } .muted { color: #5b6560; font-size: 14px; }
img, svg { max-width: 100%; height: auto; display: block; margin: 8px 0; } img { border-radius: 8px; border: 1px solid #d5ddd7; }
.box { border: 1px solid #d5ddd7; border-radius: 8px; padding: 14px 16px; margin: 12px 0; break-inside: avoid; }
.verdict { font-size: 19px; font-weight: 700; } .ok { background: #e7f3ec; } .warn { background: #fbf0d9; } .bad { background: #fbe4e1; }
.sev { float: right; font-size: 13px; font-weight: 700; color: #5b6560; }
ul { margin: 6px 0; padding-left: 20px; } table { border-collapse: collapse; width: 100%; font-size: 15px; }
td, th { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e3e9e4; vertical-align: top; } td.why { color: #5b6560; font-size: 13px; }
@media print { body { margin: 0; } h2 { break-after: avoid; } }"""


def collect(up, profile: dict | None = None, loads: list[dict] | None = None, mode: str = "both") -> dict:
    """Facts, pictures (PNG bytes) and charts (SVG text) for the report of the loaded part printed with ``up``."""
    s = api._require()
    p = Profile.from_dict(profile)
    r = api.check(up, profile, mode)
    st = r["stats"]
    errors = sum(f["severity"] == "problem" for f in r["findings"])
    warnings = sum(f["severity"] == "warn" for f in r["findings"])
    kind = "bad" if errors else "warn" if warnings else "ok"
    head = {"bad": f"{errors} problem{'s' * (errors > 1)} to fix before printing",
            "warn": f"Printable, {warnings} thing{'s' * (warnings > 1)} to check",
            "ok": "Ready to print"}[kind]
    picked = api.orientation(up, profile)
    out = {
        "title": f"Print check: {s.name}",
        "date": date.today().isoformat(),
        "setup": f"{p.printer['name']} · {p.material['name']} · {p.nozzle} mm nozzle",
        "verdict": {"kind": kind, "head": head},
        "stats": f"{st['height_mm']:.0f} mm tall · about {st['print_time_min']:.0f} min · about {st['filament_g']:.0f} g of filament (rough estimates)",
        "reasons": picked["reasons"],
        "pictures": {"bed": render.picture("bed", up, profile), "overhangs": render.picture("overhangs", up, profile)},
        "charts": {"orientations": charts.orientations(api.orientations(profile, top=5, loads=loads), api.required_safety(p))},
        "problems": [], "settings": r["settings"],
    }
    placed = 0
    for i, f in enumerate(x for x in r["findings"] if x.get("bbox")):
        f["picture"] = f"problem_{i + 1}" if placed < MAX_CLOSE_UPS else None
        if f["picture"]:
            out["pictures"][f["picture"]] = render.picture("problem", up, profile, finding=i)
            placed += 1
    out["problems"] = sorted(r["findings"], key=lambda f: list(SEVERITY).index(f["severity"]))
    if loads:
        sr = api.strength(up, loads, profile)
        sf, target = sr["safety_factor"], api.required_safety(p)
        out["strength"] = {"kind": "ok" if sf >= target else "warn" if sf >= 1 else "bad",
                           "head": f"Holds about {sf:.1f}× the load (aim ×{target:g})", "text": sr.get("explanation", "")}
        out["pictures"]["stress"] = render.picture("stress", up, profile, loads)
    if any(f["rule"] == "warping" for f in r["findings"]):
        w = api.warping(up, profile)
        out["warp"] = {"kind": {"fine": "ok", "might lift": "warn"}.get(w["verdict"], "bad"), "head": f"Warping: {w['verdict']}",
                       "text": w["explanation"]}
        out["charts"]["warping"] = charts.warping(w)
        out["pictures"]["warp"] = render.picture("warp", up, profile)
    rows = api.compare_nozzles(up, profile)
    if len(rows) > 1:
        out["charts"]["nozzle_time"], out["charts"]["nozzle_filament"] = charts.nozzles(rows)
    return out


def to_html(rep: dict) -> str:
    """One self-contained HTML file: pictures as data URIs, charts as inline SVG."""
    e = html.escape

    def pic(key, alt):
        data = rep["pictures"].get(key)
        return f'<img src="data:image/png;base64,{base64.b64encode(data).decode()}" alt="{e(alt)}">' if data else ""

    def svg(key):
        return rep["charts"].get(key, "")

    def result(x, title, picture, chart=""):
        return (f'<h2>{e(title)}</h2><div class="box {x["kind"]}"><p class="verdict">{e(x["head"])}</p><p>{e(x["text"])}</p></div>'
                + svg(chart) + pic(picture, title)) if x else ""

    def fix(x):
        return f'<li><b>{e(DOMAIN.get(x["domain"], x["domain"]))}:</b> {e(x["text"])}</li>'

    problems = "".join(
        f'<div class="box"><span class="sev">{e(SEVERITY.get(f["severity"], f["severity"]))}</span><h3>{e(f["title"])}</h3>'
        f'{pic(f.get("picture"), f["title"])}<p><b>{e(f["detail"])}</b></p><p class="muted">{e(f["why"])}</p>'
        + (f'<ul>{"".join(fix(x) for x in f["fixes"])}</ul>' if f["fixes"] else "")
        + "</div>" for f in rep["problems"]) or "<p>No problems found in this orientation.</p>"
    settings = "".join(f'<tr><td>{e(x["label"])}</td><td><b>{e(str(x["value"]))}</b></td><td class="why">{e(x["reason"])}</td></tr>'
                       for x in rep["settings"])
    nozzles = (f'<h2>Print time per nozzle</h2>{svg("nozzle_time")}{svg("nozzle_filament")}'
               if "nozzle_time" in rep["charts"] else "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(rep["title"])}</title><style>{STYLE}</style></head><body>
<h1>{e(rep["title"])}</h1>
<p class="muted">{e(rep["setup"])} · {e(rep["date"])}</p>
<div class="box {rep["verdict"]["kind"]}"><p class="verdict">{e(rep["verdict"]["head"])}</p><p>{e(rep["stats"])}</p></div>
<h2>How it lies on the bed</h2>{pic("bed", "The part on the bed")}
<ul>{"".join(f"<li>{e(x)}</li>" for x in rep["reasons"])}</ul>{svg("orientations")}
<h2>What needs support</h2>{pic("overhangs", "Seen from below")}
<p class="muted">Seen from below. Red needs support, amber is borderline, green sits on the bed.</p>
<h2>Problems</h2>{problems}
{result(rep.get("strength"), "Strength analysis", "stress")}
{result(rep.get("warp"), "Warping", "warp", "warping")}
{nozzles}
{f'<h2>Slicer settings</h2><p class="muted">What to change from your slicer defaults.</p><table><tr><th>Setting</th><th>Value</th><th>Why</th></tr>{settings}</table>' if settings else ""}
<p class="muted" style="margin-top:36px">Made with FDM Check. Times, strength and warping are rough estimates: check the first print.</p>
</body></html>
"""


def to_markdown(rep: dict, stem: str) -> tuple[str, dict[str, bytes]]:
    """Markdown plus the files it links to (``<stem>_<name>.png`` / ``.svg``), to save next to it."""
    files: dict[str, bytes] = {}

    def pic(key, alt):
        if not rep["pictures"].get(key):
            return []
        name = f"{stem}_{key}.png"
        files[name] = rep["pictures"][key]
        return [f"![{alt}]({name})", ""]

    def svg(key, alt):
        if key not in rep["charts"]:
            return []
        name = f"{stem}_{key}.svg"
        files[name] = rep["charts"][key].encode("utf-8")
        return [f"![{alt}]({name})", ""]

    lines = [f"# {rep['title']}", "", f"{rep['setup']} · {rep['date']}", "",
             f"**{rep['verdict']['head']}.** {rep['stats']}", "", "## How it lies on the bed", "", *pic("bed", "The part on the bed"),
             *[f"- {x}" for x in rep["reasons"]], "", *svg("orientations", "Orientations compared"),
             "## What needs support", "", *pic("overhangs", "Seen from below"),
             "Seen from below. Red needs support, amber is borderline, green sits on the bed.", "", "## Problems", ""]
    if not rep["problems"]:
        lines += ["No problems found in this orientation.", ""]
    for f in rep["problems"]:
        lines += [f"### {f['title']} ({SEVERITY.get(f['severity'], f['severity'])})", "", *pic(f.get("picture"), f["title"]),
                  f["detail"], "", f["why"], ""]
        lines += [f"- *{DOMAIN.get(x['domain'], x['domain'])}*: {x['text']}" for x in f["fixes"]] + [""]
    for key, title, picture, chart in (("strength", "Strength analysis", "stress", None), ("warp", "Warping", "warp", "warping")):
        x = rep.get(key)
        if x:
            lines += [f"## {title}", "", f"**{x['head']}.** {x['text']}", "", *(svg(chart, title) if chart else []), *pic(picture, title)]
    if "nozzle_time" in rep["charts"]:
        lines += ["## Print time per nozzle", "", *svg("nozzle_time", "Print time per nozzle"), *svg("nozzle_filament", "Filament per nozzle")]
    if rep["settings"]:
        lines += ["## Slicer settings", "", "| Setting | Value | Why |", "|---|---|---|"]
        lines += [f"| {api.md_cell(x['label'])} | {api.md_cell(x['value'])} | {api.md_cell(x['reason'])} |" for x in rep["settings"]]
    return "\n".join(lines) + "\n", files


def write(path: str, up, profile: dict | None = None, loads: list[dict] | None = None, mode: str = "both") -> dict:
    """Write the report to ``path``: ``.html`` (one file) or ``.md`` (pictures and charts next to it)."""
    out = Path(path).expanduser()
    if out.suffix.lower() not in (".html", ".htm", ".md"):
        raise ValueError("The report path must end in .html or .md")
    rep = collect(up, profile, loads, mode)
    saved = [out]
    if out.suffix.lower() == ".md":
        text, files = to_markdown(rep, out.stem)
        for name, data in files.items():
            (out.parent / name).write_bytes(data)
            saved.append(out.parent / name)
    else:
        text = to_html(rep)
    out.write_text(text, encoding="utf-8")
    return {"saved": [str(x) for x in saved], "verdict": rep["verdict"]["head"], "problems": len(rep["problems"])}
