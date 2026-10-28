"""Browser smoke test of the web app (headless Edge/Chrome via Playwright).

    pip install playwright
    python tools/e2e_smoke.py [extra parts .stl/.step ...] [--headed] [--browser chrome] [--gemini-review]

Starts its own static server, drives the UI like a user, and prints one line per
step plus every console error. Screenshots go to ./e2e-output/. Exit code 1 on failure.
With --gemini-review, the Gemini CLI looks at each screenshot for visual problems and
writes its notes to e2e-output/gemini-review.md (it only gets the image, no shell access).
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "e2e-output"
SAMPLES = ["mushroom", "l_bracket", "hook", "test_block", "detail_plate"]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve() -> int:
    handler = functools.partial(QuietHandler, directory=str(ROOT))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


class Run:
    def __init__(self, page):
        self.page = page
        self.failures: list[str] = []
        self.console: list[str] = []
        page.on("console", lambda m: m.type == "error" and self.console.append(m.text))
        page.on("pageerror", lambda e: self.console.append(f"pageerror: {e}"))

    def done(self) -> int:
        return int(self.page.evaluate("Number(document.body.dataset.done || 0)"))

    def act(self, action, timeout=180_000) -> str:
        """Run ``action`` and wait until the app finished the work it started (busy back to 0)."""
        before = self.done()
        action()
        self.page.wait_for_function(
            "n => Number(document.body.dataset.done || 0) > n && Number(document.body.dataset.busy || 0) === 0",
            arg=before,
            timeout=timeout,
        )
        return self.page.locator("#status").inner_text()

    def booted(self, timeout=240_000) -> str:
        self.page.wait_for_function(
            "() => document.getElementById('status').textContent.startsWith('Ready')", timeout=timeout
        )
        return self.page.locator("#status").inner_text()

    def step(self, name: str, fn):
        before = len(self.console)
        try:
            detail = fn()
            status = self.page.locator("#status")
            if "error" in (status.get_attribute("class") or ""):
                raise AssertionError(f"status shows error: {status.inner_text()}")
            new = self.console[before:]
            if new:
                raise AssertionError("console errors: " + " | ".join(new[:3]))
            print(f"PASS {name}: {detail or ''}")
        except Exception as e:  # noqa: BLE001 - report and continue
            self.failures.append(name)
            print(f"FAIL {name}: {e}")
        self.page.screenshot(path=str(OUT / f"{len(self.failures):02d}_{name.replace(' ', '_').replace('/', '_')[:60]}.png"))


def _box_stl(boxes) -> bytes:
    """Binary STL of axis-aligned boxes [(lo, size), ...] — enough for input tests."""
    import struct

    v = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    tris = [[[lo[k] + size * v[i][k] for k in range(3)] for i in face] for lo, size in boxes for face in f]
    out = bytes(80) + struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<12fH", 0, 0, 0, *t[0], *t[1], *t[2], 0)
    return out


def test_files() -> tuple[Path, Path]:
    plate, tiny = OUT / "two_bodies.stl", OUT / "inch_part.stl"
    plate.write_bytes(_box_stl([((0, 0, 0), 20), ((40, 0, 0), 10)]))
    tiny.write_bytes(_box_stl([((0, 0, 0), 2)]))
    return plate, tiny


def findings(page) -> list[str]:
    return page.locator("#findings .findings > li > details > summary").all_inner_texts()


REVIEW_PROMPT = (
    "Do not run commands or edit files. This is a screenshot of a browser app that analyses 3D-print parts "
    "(sidebar with settings/findings, 3D view of the part on a print bed). List concrete visual problems only: "
    "cut-off or overlapping elements, unreadable text, broken or misplaced 3D rendering, confusing layout. "
    "Max 5 bullets, or 'No problems.' if it looks fine: @{path}"
)


def gemini_review(binary: str):
    """Ask the Gemini CLI to review each screenshot; it reads the image via @file, nothing else."""
    import subprocess

    notes = []
    for shot in sorted(OUT.glob("*.png")):
        rel = shot.relative_to(ROOT).as_posix()
        try:
            out = subprocess.run(
                [binary, "--model", "gemini-3.8-flash-medium", "-p", REVIEW_PROMPT.format(path=rel)],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=240,
            ).stdout.strip()
        except Exception as e:  # noqa: BLE001
            out = f"(review failed: {e})"
        notes.append(f"## {shot.name}\n\n{out}\n")
        print(f"reviewed {shot.name}")
    (OUT / "gemini-review.md").write_text("# Gemini screenshot review\n\n" + "\n".join(notes), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("parts", nargs="*", type=Path)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--browser", default="msedge", choices=["msedge", "chrome"])
    ap.add_argument("--gemini-review", action="store_true", help="have the Gemini CLI review the screenshots")
    ap.add_argument("--gemini-bin", default=str(Path.home() / ".gemini" / "bin" / "agy.exe"))
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    port = serve()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.browser, headless=not args.headed)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        run = Run(page)
        page.goto(f"http://127.0.0.1:{port}/web/")
        run.step("engine boots", run.booted)

        for name in SAMPLES:
            def load(name=name):
                status = run.act(lambda: page.click(f"[data-sample={name}]"))
                assert page.locator("#options > li").count() >= 2, "no orientation options"
                return f"{status} | top: {page.locator('#options > li').first.inner_text().splitlines()[1:3]} | findings: {findings(page)}"
            run.step(f"sample {name}", load)

        def modes():
            out = []
            for mode in ("design", "both", "print"):
                run.act(lambda: page.click(f"input[name=mode][value={mode}] >> xpath=.."))
                out.append(f"{mode}: {len(findings(page))} findings, design panel {'shown' if page.locator('#design-block').is_visible() else 'hidden'}")
            return "; ".join(out)
        run.step("mode toggle", modes)

        def other_orientation():
            run.act(lambda: page.locator("#options > li").nth(1).click())
            return f"{len(findings(page))} findings"
        run.step("select option 2", other_orientation)

        def orientation_updates_findings():
            run.act(lambda: page.click("[data-sample=hook]"))
            seen = {}
            for i in range(page.locator("#options > li").count()):
                run.act(lambda i=i: page.locator("#options > li").nth(i).click())
                up = tuple(page.evaluate("() => fdm.state.check.up.map(v => Math.round(v))"))
                shown = tuple(page.evaluate("() => fdm.state.options[fdm.state.active].up.map(v => Math.round(v))"))
                assert up == shown, f"check is for {up} but option shows {shown}"
                seen[up] = tuple(findings(page))
            assert len(set(seen.values())) > 1, f"findings never changed between orientations: {seen}"
            return f"{len(seen)} orientations, {len(set(seen.values()))} different finding sets"
        run.step("orientation switch updates findings", orientation_updates_findings)

        def nozzles_and_goal():
            run.act(lambda: page.click("[data-sample=detail_plate]"))
            run.act(lambda: page.click("#nozzle-block > summary"))
            rows = page.locator("#nozzle-table tr").count() - 1
            assert rows == 4, f"expected 4 nozzle rows, got {rows}"
            run.act(lambda: page.click("input[name=goal][value=strength] >> xpath=.."))
            walls = page.evaluate("() => fdm.state.check.settings.find(s => s.key === 'wall_loops').value")
            run.act(lambda: page.click("input[name=goal][value=balanced] >> xpath=.."))
            return f"{rows} nozzle rows; strength goal -> {walls} walls"
        run.step("nozzle comparison + goal", nozzles_and_goal)

        def design():
            run.act(lambda: page.click("input[name=mode][value=both] >> xpath=.."))
            run.act(lambda: page.click("[data-sample=l_bracket]"))
            run.act(lambda: page.click("#suggest-design"))
            edits = page.locator("#edits label").all_inner_texts()
            assert any("Ø8.0" in e for e in edits), f"expected teardrop for the Ø8 hole, got {edits}"
            run.act(lambda: page.click("#apply-edits"))
            run.act(lambda: page.click("#suggest-design"))
            again = page.locator("#edits label").all_inner_texts()
            state = page.evaluate("() => ({active: fdm.state.active, up: fdm.state.options[fdm.state.active].up, name: fdm.state.name})")
            assert not any("Ø8.0" in e for e in again), f"teardrop still suggested after applying it: {again} state={state}"
            return f"applied {edits} -> now suggests {again or 'nothing'}"
        run.step("design edits apply + re-check", design)

        def pick_face():
            # Click the centre of the triangle nearest to the camera's view centre that is visible.
            x, y = page.evaluate(
                "() => { const v = fdm.viewer; const n = v.part.geometry.getAttribute('position').count / 3;"
                " for (let f = 0; f < n; f += Math.max(1, Math.floor(n / 50))) { const [x, y] = v.screenPointOfFace(f);"
                " const el = document.elementFromPoint(x, y); if (el && el.tagName === 'CANVAS') return [x, y]; } return [0, 0]; }"
            )
            page.mouse.click(x, y)  # first pick may download scipy for face grouping
            page.wait_for_selector("#pick-panel:not([hidden])", timeout=180_000)
            info = page.locator("#pick-info").inner_text()
            page.click("#as-fixed")
            return f"{info}; loads: {page.locator('#loads li').all_inner_texts()}"
        run.step("pick a face + mark fixed", pick_face)

        def bodies_and_units():
            plate, tiny = test_files()
            run.act(lambda: page.set_input_files("#file", str(plate)))
            assert page.locator("#body-pick").is_visible(), "no body picker for a two-body file"
            first = page.locator("#status").inner_text()
            run.act(lambda: page.select_option("#body", "1"))
            second = page.locator("#status").inner_text()
            assert first != second, f"switching body did not change the part: {first}"
            run.act(lambda: page.set_input_files("#file", str(tiny)))
            assert page.locator("#unit-hint").is_visible(), "no unit hint for a 2 mm part"
            run.act(lambda: page.click("#unit-scale"))
            assert "50.8" in page.locator("#status").inner_text(), page.locator("#status").inner_text()
            return f"{first} -> {second}; rescaled: {page.locator('#status').inner_text()}"
        run.step("multi-body file + unit hint", bodies_and_units)

        for part in args.parts:
            def load_part(part=part):
                status = run.act(lambda: page.set_input_files("#file", str(part)), 300_000)
                top = page.locator("#options > li").first.inner_text().splitlines()
                return f"{status} | top: {top[1:4]} | findings: {findings(page)}"
            run.step(f"file {part.name}", load_part)

        browser.close()

    if args.gemini_review:
        gemini_review(args.gemini_bin)
    print(f"\n{len(run.failures)} failure(s); screenshots in {OUT}")
    if run.console:
        print("console errors:", *run.console[:10], sep="\n  ")
    sys.exit(1 if run.failures else 0)


if __name__ == "__main__":
    main()
