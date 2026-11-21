"""Screenshot the component gallery (T-511): light and dark at 1280 x 720 and 1440 x 900, full page.

    python tools/screenshot_gallery.py      # -> design/gallery-shots/

Serves the repo on a free local port and opens design/gallery.html?demo=1 (radial menu, popover, open select and a
toast shown). Needs playwright and Edge (or --browser chromium).
"""

from __future__ import annotations

import argparse
import functools
import http.server
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "design" / "gallery-shots"
SIZES = ((1280, 720), (1440, 900))


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def serve() -> tuple[http.server.ThreadingHTTPServer, int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--browser", default="msedge", choices=["msedge", "chrome", "chromium"])
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    server, port = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**({} if args.browser == "chromium" else {"channel": args.browser}))
            errors: list[str] = []
            for w, hgt in SIZES:
                for theme in ("light", "dark"):
                    page = browser.new_page(viewport={"width": w, "height": hgt})
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(f"http://127.0.0.1:{port}/design/gallery.html?demo=1&theme={theme}")
                    page.wait_for_selector("body[data-ready]")
                    page.wait_for_timeout(300)
                    out = OUT / f"gallery-{theme}-{w}x{hgt}.png"
                    page.screenshot(path=str(out), full_page=True)
                    print(out.relative_to(ROOT))
                    page.close()
            browser.close()
            if errors:
                raise SystemExit(f"page errors: {errors}")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
