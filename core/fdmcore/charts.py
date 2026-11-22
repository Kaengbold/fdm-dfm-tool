"""Charts as SVG text, drawn from API results, for agents and the engine-built report.

Plain string building (no plotting library, so it runs in Pyodide too). Same colours as the web
app (web/tokens.css, light theme). Every chart has a title and plain labels, so it reads on its own in a report or a chat.
"""

from __future__ import annotations

from html import escape

# web/tokens.css :root (light): --text, --text-2, --line, --accent, --good, --warn, --bad
INK, MUTED, LINE, ACCENT = "#111827", "#4B5563", "#D9DEE5", "#2563EB"
GOOD, WARN, BAD = "#15803D", "#B45309", "#B91C1C"
FONT = "Atkinson Hyperlegible, system-ui, sans-serif"

W, ROW, LEFT, RIGHT, TOP = 560, 30, 150, 70, 46


def _svg(height: float, body: list[str], title: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height:.0f}" viewBox="0 0 {W} {height:.0f}" '
            f'font-family="{FONT}" font-size="13" fill="{INK}" role="img"><title>{escape(title)}</title>'
            f'<rect width="100%" height="100%" fill="#fdfdfc"/>'
            f'<text x="12" y="24" font-size="15" font-weight="700">{escape(title)}</text>' + "".join(body) + "</svg>")


def bar_chart(labels: list[str], values: list[float], title: str, unit: str = "", target: float | None = None,
              target_label: str = "", colors: list[str] | None = None, note: str = "") -> str:
    """Horizontal bars, one row per label; an optional ``target`` line (e.g. safety ×2)."""
    top = max([*values, target or 0, 1e-9]) * 1.1
    span = W - LEFT - RIGHT
    x = lambda v: LEFT + span * max(v, 0) / top  # noqa: E731
    body = []
    for i, (label, v) in enumerate(zip(labels, values)):
        y = TOP + i * ROW
        color = (colors or [ACCENT] * len(values))[i]
        body += [f'<text x="{LEFT - 8}" y="{y + 18}" text-anchor="end">{escape(label)}</text>',
                 f'<rect x="{LEFT}" y="{y + 5}" width="{x(v) - LEFT:.1f}" height="{ROW - 10}" rx="3" fill="{color}"/>',
                 f'<text x="{x(v) + 6:.1f}" y="{y + 18}">{_num(v)}{escape(unit)}</text>']
    bottom = TOP + len(values) * ROW
    if target is not None:
        tx = x(target)
        body += [f'<line x1="{tx:.1f}" x2="{tx:.1f}" y1="{TOP}" y2="{bottom}" stroke="{INK}" stroke-dasharray="4 3"/>',
                 f'<text x="{tx:.1f}" y="{bottom + 16}" text-anchor="middle" fill="{MUTED}">{escape(target_label)}</text>']
        bottom += 16
    if note:
        body.append(f'<text x="12" y="{bottom + 20}" fill="{MUTED}" font-size="12">{escape(note)}</text>')
        bottom += 20
    return _svg(bottom + 14, body, title)


def line_chart(xs: list[float], ys: list[float], title: str, x_label: str, y_label: str,
               marks: list[tuple[float, str]] = (), bands: list[tuple[float, float, str]] = (), note: str = "",
               target: tuple[float, str] | None = None) -> str:
    """One line over ``xs``; ``marks`` are labelled vertical lines, ``bands`` shaded x ranges (from, to, colour),
    ``target`` a labelled horizontal line (value, label)."""
    h, left, bottom_pad = 280, 56, 50
    x0, x1 = min(xs), max(xs)
    y1 = max(*ys, target[0] if target else 0, 1e-9) * 1.1
    px = lambda v: left + (W - left - 24) * (v - x0) / ((x1 - x0) or 1)  # noqa: E731
    py = lambda v: TOP + (h - TOP - bottom_pad) * (1 - v / y1)  # noqa: E731
    base = py(0)
    body = [f'<rect x="{px(a):.1f}" y="{TOP}" width="{px(b) - px(a):.1f}" height="{base - TOP:.1f}" fill="{c}" opacity="0.25"/>'
            for a, b, c in bands]
    body += [f'<line x1="{left}" x2="{W - 24}" y1="{base:.1f}" y2="{base:.1f}" stroke="{LINE}"/>',
             f'<line x1="{left}" x2="{left}" y1="{TOP}" y2="{base:.1f}" stroke="{LINE}"/>']
    for t in _ticks(x0, x1):
        body.append(f'<text x="{px(t):.1f}" y="{base + 16:.1f}" text-anchor="middle" fill="{MUTED}">{_num(t)}</text>')
    for t in _ticks(0, y1 / 1.1):
        body.append(f'<text x="{left - 6}" y="{py(t) + 4:.1f}" text-anchor="end" fill="{MUTED}">{_num(t)}</text>')
    pts = " ".join(f"{px(a):.1f},{py(b):.1f}" for a, b in zip(xs, ys))
    body.append(f'<polyline points="{pts}" fill="none" stroke="{ACCENT}" stroke-width="2.5"/>')
    if target:
        body += [f'<line x1="{left}" x2="{W - 24}" y1="{py(target[0]):.1f}" y2="{py(target[0]):.1f}" stroke="{INK}" stroke-dasharray="4 3"/>',
                 f'<text x="{W - 26}" y="{py(target[0]) - 5:.1f}" text-anchor="end" fill="{MUTED}">{escape(target[1])}</text>']
    for v, label in marks:
        body += [f'<line x1="{px(v):.1f}" x2="{px(v):.1f}" y1="{TOP}" y2="{base:.1f}" stroke="{INK}" stroke-dasharray="4 3"/>',
                 f'<text x="{px(v) + 4:.1f}" y="{TOP + 12}">{escape(label)}</text>']
    body += [f'<text x="{(left + W) / 2:.0f}" y="{base + 34:.1f}" text-anchor="middle" fill="{MUTED}">{escape(x_label)}</text>',
             f'<text x="12" y="{TOP - 6}" fill="{MUTED}">{escape(y_label)}</text>']
    if note:
        body.append(f'<text x="12" y="{h - 4}" fill="{MUTED}" font-size="12">{escape(note)}</text>')
    return _svg(h + (8 if note else 0), body, title)


def _num(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 10 or v == int(v) else f"{v:.1f}"


def _ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """About ``n`` round tick values between lo and hi."""
    raw = (hi - lo) / n or 1
    step = min((s * 10 ** e for e in range(-2, 5) for s in (1, 2, 5) if s * 10 ** e >= raw), default=raw)
    start = -(-lo // step) * step
    return [start + i * step for i in range(int((hi - start) // step) + 1)]


def _safety_color(sf: float, target: float) -> str:
    return GOOD if sf >= target else WARN if sf >= 1 else BAD


# ---------- charts for API results ----------

def orientations(result: dict, target: float = 2.0) -> str:
    """Safety factor per orientation (api.orientations with loads), else the support each one needs."""
    opts = result["orientations"]
    labels = [o["label"] for o in opts]
    if all(o.get("safety_factor") is not None for o in opts):
        sf = [o["safety_factor"] for o in opts]
        return bar_chart(labels, sf, "How strong in each orientation", "×", target, f"aim ×{target:g}",
                         [_safety_color(v, target) for v in sf], "Safety factor: how many times the load it holds (rough).")
    return bar_chart(labels, [o["support_area_mm2"] / 100 for o in opts], "Support needed in each orientation", " cm²",
                     note="Less support: less plastic, less cleanup, nicer surfaces. Support is one of several reasons in the ranking.")


def nozzles(rows: list[dict]) -> tuple[str, str]:
    """Print time and filament per nozzle (api.compare_nozzles): two charts."""
    labels = [f"{r['nozzle']} mm nozzle" for r in rows]
    time = bar_chart(labels, [r["print_time_min"] / 60 for r in rows], "Print time per nozzle", " h",
                     note="Bigger nozzles print faster but can't trace fine detail.")
    grams = bar_chart(labels, [r["filament_g"] for r in rows], "Filament per nozzle", " g")
    return time, grams


def warping(result: dict) -> str:
    """How hard the base wants to curl (api.warping), against the longest bar that stays flat."""
    ratio = result["lift_ratio"]
    color = GOOD if ratio < 0.7 else WARN if ratio < 1 else BAD
    return bar_chart(["Your part", "Longest flat bar"], [ratio, 1.0], f"Warping: {result['verdict']}", "×", colors=[color, MUTED],
                     note=f"1× = the longest bar ({result['reference_mm']:.0f} mm) that stays flat in {result['material']} on this printer.")


def heat(result: dict) -> str:
    """Strength left vs. temperature (api.heat_curve), brittle and soft ranges shaded."""
    t = result["temps_c"]
    return line_chart(t, result["share_pct"], f"{result['material']}: strength vs. temperature", "Temperature (°C)",
                      "Strength left (%)", marks=[(result["use_temp_c"], f"{result['use_temp_c']:.0f} °C")],
                      bands=[(t[0], result["cold_c"], "#0090ff"), (result["soft_c"], t[-1], BAD)],
                      note="Blue: brittle in the cold. Red: soft, it bends and creeps.")


CHARTS = ("orientations", "nozzles", "warping", "heat")


def for_part(kind: str, up, profile: dict | None = None, loads: list[dict] | None = None) -> list[str]:
    """Charts (SVG) for the loaded part (api session) printed with ``up``: "orientations" (safety per
    orientation with ``loads``, else support), "nozzles" (print time and filament, two charts),
    "warping", "heat" (strength vs. temperature of the material)."""
    from . import api
    from .profile import Profile
    from .service import required_safety

    if kind == "orientations":
        return [orientations(api.orientations(profile, top=5, loads=loads), required_safety(Profile.from_dict(profile)))]
    if kind == "nozzles":
        return list(nozzles(api.compare_nozzles(up, profile)))
    if kind == "warping":
        return [warping(api.warping(up, profile))]
    if kind == "heat":
        return [heat(api.heat_curve(profile))]
    raise ValueError(f"kind must be one of {', '.join(CHARTS)}")


def write(kind: str, path: str, up, profile: dict | None = None, loads: list[dict] | None = None) -> list[str]:
    """Save ``for_part`` charts as SVG at ``path`` (the second nozzle chart gets ``_filament`` added)."""
    from pathlib import Path

    svgs = for_part(kind, up, profile, loads)
    out = Path(path).expanduser()
    paths = [out, out.with_name(f"{out.stem}_filament{out.suffix}")][: len(svgs)]
    for p, svg in zip(paths, svgs):
        p.write_text(svg, encoding="utf-8")
    return [str(p) for p in paths]


def sweep(xs: list[float], safety: list[float], setting: str, target: float = 2.0) -> str:
    """Safety factor as one setting changes (e.g. infill %), with the target line."""
    return line_chart(xs, safety, f"Safety factor vs. {setting}", setting, "Safety factor (×)",
                      note="Rough estimate: compare the settings, not the exact numbers.", target=(target, f"aim ×{target:g}"))
