"""Render the T-512 style mockups to PNG for the owner to compare (plan research/redesign-2026-11/PLAN-COMBINED.md §16.6, §16.11).

    python tools/screenshot_mocks.py                  # every screen, light and dark -> design/mocks/shots/
    python tools/screenshot_mocks.py --screen holds   # one screen

Serves the repo on a local port, renders the real sample part per style and theme with design/mocks/render.html
(transparent PNGs in design/mocks/shots/parts/), then screenshots design/mocks/mock.html at 1440 x 900.
Needs playwright and Edge (or --browser chromium). The mockups are static pictures, not a working UI (decision 11).
"""

from __future__ import annotations

import argparse
import functools
import http.server
import threading
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "design" / "mocks" / "shots"
STYLES = ("a",)  # the owner picked A (Workbench) on 2026-11-21; B and C are in the git history (PR #211)
THEMES = ("light", "dark")
SCREENS = ("start", "part", "setup", "env", "holds", "loads", "details", "analyse", "options", "sim", "problems", "done", "tolerances", "settings")

# Part colours per look: body, edge lines, bed, bed grid. Holds stay blue in every look (overlay meaning, §1.5).
PART = {
    ("a", "light"): ("#C9CED6", "#4B5563", "#DCE1E7", "#9AA3AF"),
    ("a", "dark"): ("#8C95A3", "#0F1216", "#242A33", "#4B5563"),
}
# name -> render options. "On its back": the upright wall lies on the bed (the Options recommendation).
RENDERS = {
    "setup": dict(bed=1, w=1000, h=760, zoom=1.05),
    "holds": dict(hl="holds", w=1000, h=760, zoom=1.25),
    "opt-0": dict(bed=1, ry=-90, w=1200, h=300, zoom=0.8),
    "opt-1": dict(bed=1, w=1200, h=300, zoom=0.8),
    "held": dict(hl="held", w=1000, h=760, zoom=1.25),
    "heat": dict(hl="heat", bed=1, w=1000, h=760, zoom=1.15),
    "problem": dict(hl="problem", w=1000, h=760, zoom=1.25),
    "tol": dict(hl="tol", w=1000, h=760, zoom=1.25),
    "done": dict(bed=1, ry=-90, w=1000, h=760, zoom=1.1),
}


def serve() -> tuple[http.server.ThreadingHTTPServer, int]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

    handler = functools.partial(Quiet, directory=str(ROOT))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--style", choices=STYLES, action="append")
    ap.add_argument("--screen", choices=SCREENS, action="append")
    ap.add_argument("--browser", default="msedge", choices=["msedge", "chrome", "chromium"])
    args = ap.parse_args()
    styles = args.style or STYLES
    (SHOTS / "parts").mkdir(parents=True, exist_ok=True)
    server, port = serve()
    base = f"http://127.0.0.1:{port}/design/mocks"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**({} if args.browser == "chromium" else {"channel": args.browser}))
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            for style in styles:
                for theme in THEMES:
                    body, edge, bed, grid = PART[(style, theme)]
                    for name, opts in RENDERS.items():
                        q = {"part": "l_bracket", "color": body, "edge": edge, **opts}
                        if opts.get("bed"):
                            q.update(bed=bed, grid=grid)
                        page.goto(f"{base}/render.html?{urlencode(q)}")
                        page.wait_for_selector("body[data-ready]", timeout=60_000)
                        page.locator("canvas").screenshot(path=str(SHOTS / "parts" / f"{style}-{theme}-{name}.png"), omit_background=True)
                    for screen in args.screen or SCREENS:
                        page.goto(f"{base}/mock.html?{urlencode({'screen': screen, 'style': style, 'theme': theme})}")
                        page.wait_for_selector("body[data-ready]", timeout=60_000)
                        out = SHOTS / f"{style}-{theme}-{screen}.png"
                        page.screenshot(path=str(out))
                        print(out.relative_to(ROOT))
            browser.close()
            if errors:
                raise SystemExit(f"page errors: {errors}")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
