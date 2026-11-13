"""Browser smoke test of the web app (headless Edge/Chrome via Playwright).

    pip install playwright
    python tools/e2e_smoke.py [extra parts .stl/.step ...] [--headed] [--browser chrome|chromium] [--gemini-review]

Starts its own static server, drives the UI like a user, and prints one line per
step plus every console error. Screenshots go to ./e2e-output/. Exit code 1 on failure.
With --gemini-review, the Gemini CLI looks at each screenshot for visual problems and
writes its notes to e2e-output/gemini-review.md (it only gets the image, no shell access).
"""

from __future__ import annotations

import argparse
import functools
import http.server
import re
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
        # Only letters, digits, - and _: CI's artifact upload refuses names with ':' and the like.
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name)[:60]
        self.page.screenshot(path=str(OUT / f"{len(self.failures):02d}_{safe}.png"))


def _box_stl(boxes) -> bytes:
    """Binary STL of axis-aligned boxes [(lo, size or (x, y, z)), ...] — enough for input tests."""
    import struct

    v = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    dims = lambda size: size if isinstance(size, (tuple, list)) else (size, size, size)  # noqa: E731
    tris = [[[lo[k] + dims(size)[k] * v[i][k] for k in range(3)] for i in face] for lo, size in boxes for face in f]
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
    sys.stdout.reconfigure(errors="replace")  # results quote UI text (🔒, ×); Windows consoles may not have them
    ap = argparse.ArgumentParser()
    ap.add_argument("parts", nargs="*", type=Path)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--browser", default="msedge", choices=["msedge", "chrome", "chromium"],
                    help="chromium = Playwright's own build (CI: playwright install chromium)")
    ap.add_argument("--gemini-review", action="store_true", help="have the Gemini CLI review the screenshots")
    ap.add_argument("--gemini-bin", default=str(Path.home() / ".gemini" / "bin" / "agy.exe"))
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    port = serve()

    with sync_playwright() as p:
        channel = None if args.browser == "chromium" else args.browser
        browser = p.chromium.launch(channel=channel, headless=not args.headed)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        # Expert level and no start guide, so every step finds its controls (the guide has its own step).
        page.add_init_script("localStorage.setItem('fdm-guided', '1'); if (!localStorage.getItem('fdm-level')) localStorage.setItem('fdm-level', 'expert');")
        # Long simulations ask first; the test says "Run it" unless a step wants to see the question.
        page.add_init_script("""addEventListener('DOMContentLoaded', () => new MutationObserver(() => {
            const d = document.getElementById('long-run');
            if (d?.open && !window.keepAsking) d.querySelector('#long-yes').click();
        }).observe(document.body, { attributes: true, subtree: true, attributeFilter: ['open'] }));""")
        run = Run(page)
        page.goto(f"http://127.0.0.1:{port}/web/")
        run.step("engine boots", run.booted)

        def blocked_storage():
            """Private mode / strict cookie settings: every storage call throws. The app must still start."""
            other = browser.new_page()
            errors = []
            other.on("pageerror", lambda e: errors.append(str(e)))
            other.add_init_script("Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('blocked', 'SecurityError'); } });")
            other.goto(f"http://127.0.0.1:{port}/web/")
            other.wait_for_function("() => document.getElementById('status').textContent.startsWith('Ready')", timeout=180_000)
            other.close()
            assert not errors, errors
            return "starts with storage blocked"

        run.step("starts when browser storage is blocked", blocked_storage)

        def levels_and_guide():
            page.click("#show-guide")
            page.click("[data-guide=settings]")
            simple = page.evaluate("() => [document.body.dataset.level, document.getElementById('design-block').checkVisibility(), document.getElementById('my-presets').checkVisibility()]")
            assert simple == ["simple", False, False], simple
            assert page.evaluate("() => !document.getElementById('load-optional').hidden && document.getElementById('load-optional').querySelector('#blk-strength') !== null"), "Simple: load fold empty or hidden"
            page.click("input[name=level][value=expert] >> xpath=..")
            assert page.evaluate("() => localStorage.getItem('fdm-level')") == "expert"
            # T-352: the optional load fold is only there in Simple (where the strength block moves into it).
            assert page.evaluate("() => document.getElementById('load-optional').hidden"), "empty load fold shown in Expert"
            return f"guide -> {simple[0]}; back to expert"
        run.step("levels and start guide", levels_and_guide)

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

        def option_costs():
            costs = page.locator("#options .option-cost").all_inner_texts()
            n = page.locator("#options > li").count()
            assert len(costs) == n and all("support" in c for c in costs), f"cards without support grams: {costs}"
            active = page.evaluate("() => [...document.querySelectorAll('#options > li')].findIndex(li => li.classList.contains('active'))")
            assert active == 1, f"option 2 no longer marked after the check redrew the cards ({active})"
            return " | ".join(costs)
        run.step("support grams on each orientation card", option_costs)

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
            why = page.locator("#options .why-lower").all_inner_texts()
            assert why and all(w.startswith(("Ranked lower", "Almost")) for w in why), f"no score explanation: {why}"
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
            edits = page.locator("#edits label").all_inner_texts()
            assert any("Ø8.0" in e for e in edits), f"expected teardrop for the Ø8 hole, got {edits}"
            run.act(lambda: page.fill("#param-teardrop_angle_deg", "30") or page.dispatch_event("#param-teardrop_angle_deg", "change"))
            assert any("30° point" in e for e in page.locator("#edits label").all_inner_texts()), "teardrop angle input had no effect"
            run.act(lambda: page.click("#apply-edits"))
            again = page.locator("#edits label").all_inner_texts()
            state = page.evaluate("() => ({active: fdm.state.active, up: fdm.state.options[fdm.state.active].up, name: fdm.state.name})")
            assert not any("Ø8.0" in e for e in again), f"teardrop still suggested after applying it: {again} state={state}"
            return f"applied {edits} -> now suggests {again or 'nothing'}"
        run.step("design edits apply + re-check", design)

        def versions_and_undo():
            versions = page.evaluate("() => fdm.state.versions.map((v) => v.label)")
            assert len(versions) == 2 and page.locator("#version-pick").is_visible(), versions
            table = page.locator("#versions").inner_text()
            run.act(lambda: page.keyboard.press("Control+z"), 300_000)
            back = page.evaluate("() => ({ version: fdm.state.version, name: fdm.state.name })")
            assert back["version"] == 0, back
            assert any("Ø8.0" in e for e in page.locator("#edits label").all_inner_texts()), "teardrop not suggested again on the original"
            run.act(lambda: page.keyboard.press("Control+y"), 300_000)  # Redo: the changed model again
            assert page.evaluate("() => fdm.state.version") == 1, "Ctrl+Y did not redo"
            run.act(lambda: page.keyboard.press("Control+z"), 300_000)
            run.act(lambda: page.select_option("#version", "1"), 300_000)
            assert page.evaluate("() => fdm.state.version") == 1
            return f"{versions} | {table.splitlines()[1:3]} | undo -> {back['name']}"
        run.step("model versions + Ctrl+Z", versions_and_undo)

        def added_material():
            run.act(lambda: page.click("[data-sample=test_block]"))
            edits = page.locator("#edits label").all_inner_texts()
            assert sum("Thicken" in e for e in edits) == 2, f"expected two thin walls to thicken, got {edits}"
            ops = page.evaluate("() => fdm.state.edits.map((e) => e.op)")
            page.evaluate("() => fdm.steps.go('design')")  # previews show while Design changes is in view
            page.wait_for_timeout(800)
            assert "add" in ops and len(page.evaluate("() => fdm.viewer.ghosts.children.map((c) => c.material.color.getHexString())")) == len(ops)
            run.act(lambda: page.click("#apply-edits"))
            again = page.locator("#edits label").all_inner_texts()
            assert not any("Thicken" in e for e in again), f"walls still thin after thickening: {again}"
            return f"{edits} -> {again or 'nothing'} | findings: {findings(page)}"
        run.step("design edits that add material", added_material)

        def design_keeps_orientation():
            """T-353: a design change made with another orientation picked keeps that orientation."""
            run.act(lambda: page.click("[data-sample=test_block]"))
            run.act(lambda: page.locator("#options > li").nth(1).click())
            up = page.evaluate("() => fdm.state.options[fdm.state.active].up")
            assert page.locator("#edits input[type=checkbox]:checked").count(), "no design change ticked"
            run.act(lambda: page.click("#apply-edits"))
            now = page.evaluate("() => ({ up: fdm.state.options[fdm.state.active].up, version: fdm.state.version })")
            assert now["version"] == 1 and sum(a * b for a, b in zip(up, now["up"])) > 0.999, f"orientation {up} -> {now}"
            run.act(lambda: page.select_option("#version", "0"), 300_000)
            back = page.evaluate("() => fdm.state.options[fdm.state.active].up")
            assert sum(a * b for a, b in zip(up, back)) > 0.999, f"switching back to the original: {up} -> {back}"
            return f"stays on {[round(v, 2) for v in up]} after the change and back on the original"
        run.step("design change keeps the orientation", design_keeps_orientation)

        def feature_confirm():
            run.act(lambda: page.click("[data-sample=l_bracket]"))
            row = page.locator("#features li", has_text="Ø8.0 mm hole")
            assert row.count() == 1, page.locator("#features").inner_text()
            run.act(lambda: row.get_by_role("button", name="Not a hole").click())
            edits, found = page.locator("#edits label").all_inner_texts(), findings(page)
            assert not any("Ø8.0" in e for e in edits), f"teardrop still offered for a rejected hole: {edits}"
            assert not any("Sideways Hole" in f for f in found), f"rejected hole still reported: {found}"
            run.act(lambda: page.locator("#features li", has_text="Ø8.0 mm hole").get_by_role("button", name="Undo").click())
            assert any("Ø8.0" in e for e in page.locator("#edits label").all_inner_texts()), "undo did not bring the teardrop back"
            return f"{page.locator('#features').inner_text()!r} | findings after reject: {found}"
        run.step("feature confirm - not a hole", feature_confirm)

        def fit_a_hole():
            row = page.locator("#fits li", has_text="Ø8.0 mm hole")
            row.locator(".fit:not(.fastener) input").fill("8")
            row.locator(".fit:not(.fastener) select").select_option("sliding")
            run.act(lambda: row.get_by_role("button", name="Size it").click())
            note = page.locator("#fits li", has_text="Ø8.0 mm hole").locator(".fit:not(.fastener) p").inner_text()
            edits = page.locator("#edits label").all_inner_texts()
            assert any("sliding fit" in e for e in edits), edits
            # The sized hole is the selection: its row marked, a line saying so, its faces coloured.
            assert "picked" in (page.locator("#fits li", has_text="Ø8.0 mm hole").get_attribute("class") or ""), "sized hole not marked"
            selected = page.locator("#fit-selected").inner_text()
            assert "Selected:" in selected and "Ø8.0 mm hole, sliding / turning fit for Ø8 mm" in selected, selected
            page.locator("#fit-selected").get_by_role("button", name="Clear").click()
            assert "Nothing selected" in page.locator("#fit-selected").inner_text()
            assert page.locator("#fits li.picked").count() == 0, "row still marked after Clear"
            return f"{note[:90]} | {selected.splitlines()[-1][:60]}"

        run.step("what fits in a hole", fit_a_hole)

        def screw_in_a_hole():
            row = page.locator("#fits li", has_text="Ø6.0 mm hole")
            row.locator(".fastener select").first.select_option("M3")
            row.locator(".fastener select").nth(1).select_option("nut_top")
            run.act(lambda: row.get_by_role("button", name="Size for it").click())
            note = page.locator("#fits li", has_text="Ø6.0 mm hole").locator(".fastener p").inner_text()
            edits = page.locator("#edits label").all_inner_texts()
            # Lying in this orientation: the M3 clearance hole (3.4 mm) plus the sideways allowance.
            assert any("Nut pocket for the M3 nut" in e for e in edits) and any("→ Ø3.50 mm hole" in e for e in edits), edits
            assert not any("Teardrop for the horizontal Ø6.0" in e for e in edits), "teardrop at the old size kept"
            return note[:120]

        run.step("screw and nut in a hole", screw_in_a_hole)

        def flat_gap():
            # Click a visible flat face that has a parallel face on the other side; the other side is
            # usually hidden from the camera, so it goes in as the first pick directly.
            page.click("#gap-pick")
            page.wait_for_selector("#gap-hint:not([hidden])", timeout=180_000)  # after the faces are grouped
            x, y = page.evaluate(
                "() => { const v = fdm.viewer, s = fdm.state; const n = v.part.geometry.getAttribute('position').count / 3;"
                " const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];"
                " for (let f = 0; f < n; f++) { const r = s.regionInfo[s.faceRegions[f]]; if (!r || r.kind !== 'flat' || r.area_mm2 < 20) continue;"
                "  const other = s.regionInfo.find((q) => q.kind === 'flat' && dot(q.normal, r.normal) < -0.999 && q.area_mm2 > 20);"
                "  if (!other) continue; const [x, y] = v.screenPointOfFace(f); const el = document.elementFromPoint(x, y);"
                "  if (el && el.tagName === 'CANVAS' && s.faceRegions[v.pick({clientX: x, clientY: y})] === r.id) { s.gapPick = [other.id]; return [x, y]; } }"
                " return [0, 0]; }"
            )
            if not (x or y):
                page.evaluate("() => { fdm.state.gapPick = null; }")  # don't leave the next steps' clicks picking gaps
                raise AssertionError("no pair of parallel flat faces found")
            page.mouse.click(x, y)  # measuring isn't a busy job: wait for the list instead
            page.wait_for_selector("#gaps li", timeout=60_000)
            row = page.locator("#gaps li").first
            label = row.locator("button.link").first.inner_text()
            assert "picked" in (row.get_attribute("class") or ""), "picked gap not selected"
            assert page.evaluate("() => fdm.state.fitSel?.regions.length") == 2, "both faces of the gap should light up"
            value = float(row.locator("input").input_value())
            row.locator("input").fill(f"{value - 0.5:.1f}" if label.startswith("Slot") else f"{value + 0.5:.1f}")
            run.act(lambda: row.get_by_role("button", name="Size it").click())
            note = page.locator("#gaps li").first.locator("p.hint").inner_text()
            edits = page.locator("#edits label").all_inner_texts()
            assert any(("slot" in e or "tab" in e) and "fit" in e for e in edits), edits
            page.locator("#gaps li").first.get_by_role("button", name="Remove").click()
            page.wait_for_function("() => !document.querySelector('#gaps li')", timeout=120_000)
            return f"{label}: {note[:90]}"
        run.step("slots and flat gaps", flat_gap)

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

        def load_cases():
            # Another visible face (different region) gets a load in case 1 and a heavier one in case 2.
            x, y = page.evaluate(
                "() => { const v = fdm.viewer, s = fdm.state, held = s.loads[0].region;"
                " const n = v.part.geometry.getAttribute('position').count / 3;"
                " for (let f = 0; f < n; f++) { if (s.faceRegions[f] === held) continue; const [x, y] = v.screenPointOfFace(f);"
                " const el = document.elementFromPoint(x, y); const hit = v.pick({clientX: x, clientY: y}); if (el && el.tagName === 'CANVAS' && hit >= 0 && s.faceRegions[hit] !== held) return [x, y]; }"
                " return [0, 0]; }"
            )
            for case, kg in (("1", "1"), ("2", "4")):
                page.mouse.click(x, y)
                page.wait_for_selector("#pick-panel:not([hidden])")
                page.fill("#kg", kg)
                page.select_option("#load-case", case)
                page.click("#as-load")
            run.act(lambda: page.click("#run-strength"), 300_000)
            text = page.locator("#strength-result").inner_text()
            assert "Weakest is case 2" in text, text
            return f"loads: {page.locator('#loads li').all_inner_texts()} | {text.splitlines()[0]}"
        run.step("strength with two load cases", load_cases)

        def spot_load():
            # Replace the case-2 face load by the same weight on a spot of that face.
            x, y = page.evaluate(
                "() => { const v = fdm.viewer, s = fdm.state, held = s.loads[0].region;"
                " const n = v.part.geometry.getAttribute('position').count / 3;"
                " for (let f = 0; f < n; f++) { if (s.faceRegions[f] === held) continue; const [x, y] = v.screenPointOfFace(f);"
                " const el = document.elementFromPoint(x, y); const hit = v.pick({clientX: x, clientY: y}); if (el && el.tagName === 'CANVAS' && hit >= 0 && s.faceRegions[hit] !== held) return [x, y]; }"
                " return [0, 0]; }"
            )
            page.evaluate("() => { fdm.state.loads = fdm.state.loads.filter((l) => l.kind === 'fixed'); }")
            page.mouse.click(x, y)
            page.wait_for_selector("#pick-panel:not([hidden])")
            page.check("#at-spot")
            assert page.evaluate("() => fdm.viewer.spots.children.length") == 1, "no preview ball for the spot"
            page.fill("#kg", "4")
            page.click("#as-load")
            page.evaluate("() => { document.getElementById('at-spot').checked = false; }")  # back to whole faces
            loads = page.locator("#loads li").all_inner_texts()
            assert any("(at a spot)" in l for l in loads), loads
            run.act(lambda: page.click("#run-strength"), 300_000)
            text = page.locator("#strength-result").inner_text()
            assert "safety" in text.lower(), text
            return f"loads: {loads} | {text.splitlines()[0]}"
        run.step("strength with a load on a spot", spot_load)

        def shaking():
            page.check("#dyn-shake")
            run.act(lambda: page.click("#run-dynamics"), 300_000)
            text = page.locator("#dynamics-result").inner_text()
            assert "Hz" in text, text
            swings = page.evaluate("() => !!fdm.viewer.field && fdm.state.showSwing")
            assert swings, "swing shape not shown on the part"
            page.uncheck("#dynamics-result input[type=checkbox]")  # back to the strength colours for the next steps
            return text.splitlines()[0][:120]
        run.step("shaking: natural frequency and swing view", shaking)

        def fea_view():
            page.evaluate("() => fdm.steps.go('strength')")  # the stress colours show in Will it hold?
            assert page.locator("#legend").is_visible(), "no legend"
            colours = page.evaluate("() => fdm.viewer.field.mesh.geometry.getAttribute('color').count")
            page.locator(".fea-view select").first.select_option("vm")
            title = page.locator("#legend strong").inner_text()
            page.locator(".fea-view select").nth(1).select_option("z")
            cubes = page.evaluate("() => fdm.viewer.section?.count ?? 0")
            assert cubes > 0, "section shows no cubes"
            assert not page.evaluate("() => fdm.viewer.field.mesh.visible"), "cut shows the hollow surface, not solid cubes"
            page.screenshot(path=OUT / "section-cut.png")
            page.locator(".fea-view select").nth(1).select_option("")
            run.act(lambda: page.get_by_text("Check accuracy with a finer grid").click(), 600_000)
            note = page.locator(".fea-view").inner_text()
            assert "Finer grid" in note, note
            return f"{colours} coloured corners; {title}; section {cubes} cubes; {note.splitlines()[-1][:90]}"
        run.step("FEA view: legend, section, finer grid", fea_view)

        def shells():
            page.locator(".shells:not(#detailed) summary").click()
            page.fill("#sh-walls", "6")
            run.act(lambda: page.dispatch_event("#sh-walls", "change"), 300_000)
            page.wait_for_function("() => document.querySelector('#strength-result')?.innerText.includes('6 walls')", timeout=300_000)
            text = page.locator("#strength-result").inner_text()
            settings = page.locator("#settings").inner_text()
            assert "Walls changed by you" in settings, settings
            return text.splitlines()[1][:120]
        run.step("walls set by hand go into strength and the 3MF", shells)

        def hot_car():
            run.act(lambda: page.select_option("#use-temp", "70"), 300_000)
            page.wait_for_function("() => document.querySelector('#strength-result')?.innerText.includes('°C')", timeout=300_000)
            text = page.locator("#strength-result").inner_text()
            found = findings(page)
            run.act(lambda: page.select_option("#use-temp", ""), 300_000)
            assert any("too hot" in f.lower() for f in found) or "ASA" in page.input_value("#material").upper(), found
            return text.splitlines()[1][:140]
        run.step("used in a hot car", hot_car)

        def temperature_chart():
            page.evaluate("() => fdm.steps.go('sims')")
            chart = page.locator("#temp-chart svg")
            assert chart.locator("polyline").count() == 1, "no strength vs. temperature curve"
            assert "at 23 °C" in chart.text_content(), chart.text_content()
            page.fill("#sim-temp", "50")
            run.act(lambda: page.click("#sim-temp-use"), 300_000)
            assert page.input_value("#use-temp") == "custom" and page.input_value("#use-temp-c") == "50"
            page.wait_for_function("() => document.querySelector('#temp-chart svg')?.textContent.includes('at 50 °C')")
            text = page.locator("#strength-result").inner_text()
            assert "50 °C" in text, text[:300]
            story = page.locator("#temp-story").inner_text()
            run.act(lambda: page.select_option("#use-temp", ""), 300_000)
            assert page.locator("#use-temp-own").is_hidden()
            return story
        run.step("strength vs. temperature", temperature_chart)

        def material_advisor():
            page.locator("#advice > summary").click()
            page.check("#adv-outdoors")
            run.act(lambda: page.click("#run-advice"), 300_000)
            items = page.locator("#advice-list > li").all_inner_texts()
            assert len(items) >= 6 and any("holds your load" in i for i in items), items
            assert any(i.startswith("ASA") for i in items[:3]), items[:3]  # made for sunlight
            page.uncheck("#adv-outdoors")
            page.locator("#advice > summary").click()
            return " | ".join(i.splitlines()[0] for i in items[:4])
        run.step("material advisor", material_advisor)

        def detailed():
            # Slice the part shown with the local OrcaSlicer (like a user would), then load the G-code.
            sys.path.insert(0, str(ROOT / "core"))
            from fdmcore.slicer_cli import find_orca  # standard library only: CI has no engine packages

            if find_orca() is None:
                return "skipped: OrcaSlicer not installed"
            from fdmcore import api, mcp_server
            name, data, up = page.evaluate("() => [fdm.state.source.name, Array.from(fdm.state.source.bytes), fdm.state.options[fdm.state.active].up]")
            api.load(bytes(data), name)
            sliced = mcp_server._slice(up, page.input_value("#printer"), page.input_value("#material"), page.input_value("#nozzle"), None, keep_gcode=True)
            gcode = OUT / "detailed.gcode"
            gcode.write_text(sliced["orcaslicer"]["gcode"])
            page.locator("#detailed summary").click()
            run.act(lambda: page.set_input_files("#gcode-file", str(gcode)), 600_000)
            assert page.is_checked("#use-gcode"), page.locator("#status").inner_text()
            page.wait_for_function("() => document.querySelector('#strength-result')?.innerText.includes('as sliced in')", timeout=300_000)
            text = page.locator("#strength-result").inner_text()
            # The result also shows in the G-code's own card, where the user loaded it.
            assert "With your sliced print: safety" in page.locator("#gcode-result").inner_text(), page.locator("#gcode-result").inner_text()
            page.uncheck("#use-gcode")
            page.wait_for_function("() => document.querySelector('#strength-result')?.innerText.includes('walls')", timeout=300_000)
            return f"{page.locator('#gcode-summary').inner_text()} | {text.splitlines()[0]}"
        run.step("detailed check from a sliced G-code", detailed)

        def optimise():
            run.act(lambda: page.click("#run-optimise"), 900_000)
            text = page.locator("#optimise-result").inner_text()
            assert "g of plastic" in text or "aren't enough" in text, text
            assert "aren't enough" not in text or "What else gets it to" in text, text
            zones = page.get_by_role("button", name="Use the zones")
            if zones.count():
                run.act(lambda: zones.click(), 300_000)
                assert page.locator("#zones-for").is_visible(), "no slicer choice for the zones"
                boxes = page.evaluate("() => fdm.viewer.zones?.children.length ?? 0")
                assert boxes >= 2, "zones not shown"
                with page.expect_download() as dl:
                    run.act(lambda: page.click("#dl-3mf"), 300_000)
                path = dl.value.path()
                import zipfile

                conf = zipfile.ZipFile(path).read("Metadata/model_settings.config").decode()
                assert "modifier_part" in conf, "no modifier volume in the 3MF"
                note = f" | zones used: {boxes // 2} box(es) in the 3MF"
            else:
                note = " | no zones offered"
            run.act(lambda: page.get_by_role("button", name="Use these settings").click(), 300_000)
            assert page.locator("#zones-for").is_hidden(), "zones kept after choosing plain settings"
            return text.splitlines()[1][:160] + note
        run.step("find the lightest settings that hold", optimise)

        def picked_zone():
            page.evaluate("() => fdm.steps.go('settings')")
            page.click("#zone-pick")
            x, y = page.evaluate(
                "() => { const v = fdm.viewer; const n = v.part.geometry.getAttribute('position').count / 3;"
                " for (let f = 0; f < n; f += Math.max(1, Math.floor(n / 50))) { const [x, y] = v.screenPointOfFace(f);"
                " const el = document.elementFromPoint(x, y); if (el && el.tagName === 'CANVAS' && v.pick({clientX: x, clientY: y}) >= 0) return [x, y]; } return [0, 0]; }"
            )
            run.act(lambda: page.mouse.click(x, y))
            item = page.locator("#zone-list li").first
            item.locator("input").nth(1).fill("80")
            item.locator("input").nth(1).press("Tab")  # change fires when the field is left
            assert page.evaluate("() => fdm.state.zones.length === 1 && fdm.state.zones[0].infill === 0.8"), "zone infill not set"
            assert page.evaluate("() => fdm.viewer.zones?.children.length ?? 0") >= 2, "zone not shown"
            run.act(lambda: page.locator("#zone-list li").first.locator("select.shape-pick").select_option("cylinder"))
            page.wait_for_function("() => fdm.state.zones[0]?.shape === 'cylinder' && fdm.state.zones[0].infill === 0.8")
            assert page.evaluate("() => fdm.viewer.zones?.children.length ?? 0") >= 3, "round zone not drawn"
            with page.expect_download() as dl:
                run.act(lambda: page.click("#dl-3mf"), 300_000)
            import zipfile

            conf = zipfile.ZipFile(dl.value.path()).read("Metadata/model_settings.config").decode()
            assert "modifier_part" in conf and 'value="80%"' in conf, "picked zone not in the 3MF"
            label = item.locator("strong").inner_text()
            page.click("#zone-card h3")  # Ctrl+Z isn't undo while typing in a field
            page.keyboard.press("Control+z")  # back to a box
            page.wait_for_function("() => fdm.state.zones[0]?.shape !== 'cylinder'")
            page.keyboard.press("Control+z")  # back to the default infill
            page.wait_for_function("() => fdm.state.zones[0]?.infill !== 0.8")
            page.locator("#zone-list li").first.get_by_role("button", name="Remove").click()
            assert page.evaluate("() => fdm.state.zones.length") == 0
            found = page.evaluate("() => fdm.state.strength?.stress_zones?.length ?? 0")
            assert found, "the strength check proposed no stressed places"
            page.locator("#zone-suggest button").click()  # starts no engine work
            assert page.evaluate("() => fdm.state.zones.length") == found, "stressed places not added"
            first = page.locator("#zone-list li strong").first.inner_text()
            assert first.startswith("Stressed place 1"), first
            assert page.locator("#zone-suggest button").count() == 0, "suggestion still offered after adding"
            page.click("#zone-card h3")
            page.keyboard.press("Control+z")
            page.wait_for_function("() => fdm.state.zones.length === 0")
            return f"{label}; cylinder, 80 % infill in the 3MF, undo and remove work | {first}"
        run.step("stronger area picked on the part", picked_zone)

        def advanced_boxes():
            page.evaluate("() => fdm.steps.go('extra')")
            spot = (
                "() => { const v = fdm.viewer; const n = v.part.geometry.getAttribute('position').count / 3;"
                " for (let f = 0; f < n; f += Math.max(1, Math.floor(n / 50))) { const [x, y] = v.screenPointOfFace(f);"
                " const el = document.elementFromPoint(x, y); if (el && el.tagName === 'CANVAS' && v.pick({clientX: x, clientY: y}) >= 0) return [x, y]; } return [0, 0]; }"
            )
            x, y = page.evaluate(spot)
            for kind in ("support", "blocker"):
                page.select_option("#vol-kind", kind)
                page.click("#vol-pick")
                run.act(lambda: page.mouse.click(x, y))
            assert page.evaluate("() => fdm.state.volumes.map((v) => v.kind).join()") == "support,blocker", "boxes not placed"
            assert page.locator("#vol-list li .hint").count() >= 2, "no notes on the boxes"
            assert page.evaluate("() => fdm.viewer.zones?.children.length ?? 0") >= 4, "boxes not shown"
            with page.expect_download() as dl:
                run.act(lambda: page.click("#dl-3mf"), 300_000)
            import zipfile

            conf = zipfile.ZipFile(dl.value.path()).read("Metadata/model_settings.config").decode()
            assert "support_enforcer" in conf and "support_blocker" in conf, "boxes not in the 3MF"
            page.click("#vol-kind-hint")  # Ctrl+Z isn't undo while in a field
            page.keyboard.press("Control+z")
            page.wait_for_function("() => fdm.state.volumes.length === 1")
            page.locator("#vol-list li").first.get_by_role("button", name="Remove").click()
            assert page.evaluate("() => fdm.state.volumes.length") == 0
            return "support and no-support boxes in the 3MF; undo and remove work"
        run.step("advanced design changes: support boxes", advanced_boxes)


        def rank_with_strength():
            run.act(lambda: page.get_by_role("button", name="Find the best orientation including strength").click(), 600_000)
            text = page.locator("#strength-result").inner_text()
            assert "safety" in text.lower(), "no strength result for the new best orientation"
            return page.locator("#status").inner_text()
        run.step("best orientation with strength shows its result", rank_with_strength)


        def heat_map():
            n = page.evaluate("() => [fdm.state.check.face_temp.length, fdm.viewer.part.geometry.getAttribute('position').count / 3]")
            assert n[0] == n[1], n
            page.check("#show-temp")
            page.uncheck("#show-temp")
            return f"{n[0]} faces coloured by heat"
        run.step("heat while printing colours", heat_map)

        def warping():
            run.act(lambda: page.click("#run-warp"), 600_000)
            text = page.locator("#warp-result").inner_text()
            assert "bar" in text, text
            assert page.locator("#legend").is_visible() and "Curls" in page.locator("#legend").inner_text()
            # T-362: true size by default (so PLA and ABS look different), exaggerated on request.
            assert "true size" in page.locator("#legend").inner_text(), page.locator("#legend").inner_text()
            page.select_option("#warp-show", "exaggerated")
            assert "exaggerated" in page.locator("#legend").inner_text()
            page.select_option("#warp-show", "stress")
            assert "Stress left inside" in page.locator("#legend").inner_text()
            page.select_option("#warp-show", "shape")
            page.locator("#warp-result input[type=checkbox]").uncheck()
            assert page.locator("#legend").is_hidden()
            return text.splitlines()[0] + " | " + text.splitlines()[1][:120]
        run.step("warping simulation", warping)

        def all_settings():
            page.check("input[name=level][value=expert]")
            page.click("#show-expert")
            try:
                page.wait_for_selector("#expert[open] .expert-values", state="attached")  # sections start folded
                sections = page.locator("#expert-sections > details").count()
                page.fill("#expert-filter", "vanish_below")
                shown = [d for d in page.locator("#expert-sections > details").all() if d.is_visible()]
                assert len(shown) == 1, f"filter shows {len(shown)} sections"
                field = shown[0].locator("input:visible").first
                field.fill("0.5")
                field.press("Tab")
                page.wait_for_function("() => JSON.parse(localStorage.getItem('fdm-expert') ?? '{}')['thresholds.features.vanish_below_nozzle'] === 0.5")
                assert "changed" in page.locator("#expert-sections .name.changed").first.get_attribute("class")
                run.act(lambda: page.click("#expert-close"), 300_000)  # checks again with the new value
                page.click("#show-expert")
                page.wait_for_selector("#expert[open] .expert-values", state="attached")
                page.click("#expert-reset")
                page.wait_for_function("() => !localStorage.getItem('fdm-expert')")
            finally:
                page.evaluate("() => document.getElementById('expert').open && document.getElementById('expert').close()")
            return f"{sections} sections; changed a limit, checked again, reset"
        run.step("all settings (Expert)", all_settings)
        def box_heat():
            page.evaluate("() => fdm.steps.go('sims')")
            page.fill("#heat-w", "6")
            page.select_option("#heat-cooling", "closed")
            run.act(lambda: page.click("#run-box-heat"))
            text = page.locator("#box-heat-result").inner_text()
            assert "°C" in text and "above the room" in text, text
            legend = page.locator("#legend")
            assert legend.is_visible() and "Wall temperature" in legend.inner_text(), "walls not coloured by temperature"
            # Put the electronics somewhere on the part: worked out again there, the dot shows where.
            page.click("#heat-place")
            x, y = page.evaluate(
                "() => { const v = fdm.viewer; const n = v.part.geometry.getAttribute('position').count / 3;"
                " for (let f = 0; f < n; f += Math.max(1, Math.floor(n / 50))) { const [x, y] = v.screenPointOfFace(f);"
                " const el = document.elementFromPoint(x, y); if (el && el.tagName === 'CANVAS' && v.pick({clientX: x, clientY: y}) >= 0) return [x, y]; } return [0, 0]; }"
            )
            run.act(lambda: page.mouse.click(x, y))
            assert page.evaluate("() => Boolean(fdm.state.heatSource && fdm.state.boxHeat.wall_max_c >= fdm.state.boxHeat.inside_c)")
            assert "where you put the electronics" in page.locator("#box-heat-result").inner_text()
            text += f" | walls up to {page.evaluate('() => fdm.state.boxHeat.wall_max_c')} °C"
            use = page.locator("#box-heat-result button")
            if use.count():
                run.act(lambda: use.click(), 300_000)
                assert "Inside the electronics box" in page.locator("#use-temp option:checked").inner_text()
                page.select_option("#use-temp", "")  # back to room temperature for the next steps
                run.act(lambda: page.dispatch_event("#use-temp", "change"), 300_000)
            return text[:120] + " …" + text[text.index(" | walls"):]
        run.step("heat inside an electronics box", box_heat)

        def report_with_pictures():
            with page.expect_download(timeout=180_000) as dl:
                run.act(lambda: page.click("#dl-report"))
            out = OUT / "report.html"
            dl.value.save_as(out)
            html = out.read_text(encoding="utf-8")
            pictures = html.count('<img src="data:image/jpeg')
            assert pictures >= 3 and "Will it hold?" in html and "Warping" in html and "Slicer settings" in html, (pictures, html[:300])
            return f"{len(html) // 1024} kB, {pictures} pictures"
        run.step("report with pictures", report_with_pictures)

        def asks_before_long_runs():
            page.evaluate("() => { window.keepAsking = true; fdm.state.runTimes = { warping: 75 }; }")
            page.click("#run-warp")
            page.wait_for_selector("#long-run[open]")
            title = page.locator("#long-title").inner_text()
            assert "1 min 15 s" in title, title
            page.click("#long-no")
            assert not page.locator("#long-run[open]").count(), "dialog stayed open"
            page.evaluate("() => { window.keepAsking = false; fdm.state.runTimes = {}; }")
            return title
        run.step("asks before a long simulation", asks_before_long_runs)

        def marked_faces():
            run.act(lambda: page.click("[data-sample=mushroom]"))
            before = page.evaluate("() => fdm.state.options[0].up.map(Math.round)")
            # Mark the stem end (the flat face pointing -Z as modelled) "on the bed".
            stem = page.evaluate("async () => (await fdm.call('regions')).regions.filter((r) => r.kind === 'flat')"
                                 ".sort((a, b) => a.normal[2] - b.normal[2])[0].id")
            run.act(lambda: page.evaluate(f"() => {{ fdm.state.selectedRegion = {stem}; document.getElementById('mark-bed').click(); }}"))
            after = page.evaluate("() => fdm.state.options[0].up.map(Math.round)")
            assert after == [0, 0, 1] and after != before, f"marking the stem end for the bed: {before} -> {after}"
            return f"best up {before} -> {after}; marks: {page.locator('#marks li').all_inner_texts()}"
        run.step("marked face steers orientation", marked_faces)

        def cancel():
            run.act(lambda: page.click("[data-sample=enclosure]"))
            before = page.evaluate("() => fdm.state.options.length")
            # A hole answer and a fit: the restarted engine must still know them (T-300).
            told = page.evaluate("""async () => {
                const { features } = await fdm.call('tag_feature', { region: 0, tag: 'none' });
                const round = features.find((f) => f.type === 'hole' || f.type === 'boss');
                if (round) await fdm.call('set_fit', { region: round.region, up: fdm.state.options[0].up, partner_mm: round.diameter, fit: 'sliding' });
                return fdm.call('session_state');
            }""")
            assert told["tags"] and told["fits"], f"enclosure sample: no hole or post to fit ({told})"
            page.click("input[name=goal][value=strength] >> xpath=..")  # slow: checks several orientations
            page.wait_for_selector("#progress:not([hidden])", timeout=30_000)
            page.wait_for_function("() => document.getElementById('progress-bar').value > 0", timeout=30_000)
            run.act(lambda: page.click("#working-cancel"), 60_000)
            status = page.locator("#status").inner_text()
            assert status.startswith("Cancelled"), status
            # The restarted engine must have the part again: re-orienting works.
            run.act(lambda: page.click("input[name=goal][value=balanced] >> xpath=.."))
            after = page.evaluate("() => fdm.state.options.length")
            assert after == before, f"{before} options before, {after} after restart"
            kept = page.evaluate("() => fdm.call('session_state')")
            assert kept["tags"] == told["tags"] and kept["fits"] == told["fits"], f"lost on cancel: {told} -> {kept}"
            return f"{status} | kept {len(kept['tags'])} answer(s), {len(kept['fits'])} fit(s) | after restart: {after} options, {len(findings(page))} findings"
        run.step("progress + cancel", cancel)

        def lighter_shape():
            run.act(lambda: page.click("[data-sample=hook]"))
            page.evaluate("async () => { const r = await fdm.call('regions'); const flat = r.regions.filter((x) => x.kind === 'flat');"
                          " fdm.state.loads = [{ region: flat[0].id, kind: 'fixed' }, { region: flat[1].id, kind: 'force', kg: 2 }]; }")
            page.evaluate("() => { const k = document.getElementById('topo-keep'); k.value = '50'; k.oninput(); }")
            # Loads set directly (not by clicking faces), so the button isn't refreshed: enable it as renderLoads would.
            run.act(lambda: page.evaluate("() => { const b = document.getElementById('run-topology'); b.disabled = false; b.click(); }"), 900_000)
            text = page.locator("#topology-result").inner_text()
            assert " g instead of " in text and "CAD" in text, text
            versions = page.evaluate("() => fdm.state.versions.length")
            run.act(lambda: page.evaluate("() => [...document.querySelectorAll('#topology-result button')].find((b) => b.textContent === 'Show it in 3D').click()"), 300_000)
            assert page.evaluate("() => fdm.state.versions.length") == versions + 1 and "lighter_50" in page.evaluate("() => fdm.state.name")
            return text.splitlines()[0]
        run.step("suggest a lighter shape", lighter_shape)

        def copy_report():
            run.act(lambda: page.click("[data-sample=hook]"))
            page.context.grant_permissions(["clipboard-read", "clipboard-write"])
            page.click("#copy-report")
            page.wait_for_function("() => document.getElementById('status').textContent.startsWith('Copied')")
            report = page.evaluate("() => navigator.clipboard.readText()")
            assert report.startswith("# Print check:") and "## Findings" in report, report[:600]
            assert "For the AI assistant" not in report, report[-600:]
            assert not page.locator("#ai-block, #paste-block").count()
            return f"{len(report)} characters"
        run.step("copy the report", copy_report)

        def project_round_trip():
            # Hook is loaded (copy_report): second orientation, a load, a slicer setting -> Save project -> reopen.
            run.act(lambda: page.locator("#options > li").nth(1).click())
            up = page.evaluate("() => fdm.state.options[fdm.state.active].up")
            page.evaluate("async () => { const r = await fdm.call('regions'); const flat = r.regions.filter((x) => x.kind === 'flat');"
                          " fdm.state.loads = [{ region: flat[0].id, kind: 'fixed' }, { region: flat[1].id, kind: 'force', kg: 2 }];"
                          " fdm.state.overrides = { wall_loops: '5' }; }")
            with page.expect_download(timeout=120_000) as dl:
                page.evaluate("() => document.getElementById('dl-project').click()")
            out = OUT / "project.json"
            dl.value.save_as(out)
            run.act(lambda: page.click("[data-sample=mushroom]"))
            run.act(lambda: page.set_input_files("#file", str(out)), 300_000)
            # Several engine calls in a row (load, orient, check, the project's orientation): wait for the last step.
            page.wait_for_function("() => document.getElementById('status').textContent.includes('opened the project')", timeout=300_000)
            got = page.evaluate("() => ({ label: fdm.state.options[0].label, up: fdm.state.options[0].up, loads: fdm.state.loads.length,"
                                " walls: fdm.state.overrides.wall_loops, name: fdm.state.name })")
            assert got["label"] == "From the project" and got["loads"] == 2 and got["walls"] == "5", got
            assert all(abs(a - b) < 1e-6 for a, b in zip(got["up"], up)) and got["name"].startswith("hook"), (got, up)
            return f"{out.stat().st_size // 1024} kB, {got}"
        run.step("save and reopen a project", project_round_trip)

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

        def step_round_trip():
            fixture = ROOT / "tools" / "fixtures" / "l_bracket.step"
            run.act(lambda: page.set_input_files("#file", str(fixture)), 300_000)
            if "Sideways hole prints oval" not in " ".join(findings(page)):
                run.act(lambda: page.locator("#options > li").nth(1).click())
            if not page.locator("input[name=mode][value=both]").is_checked():
                run.act(lambda: page.click("input[name=mode][value=both] >> xpath=.."))
            boxes = page.locator("#edits label")
            for i in range(boxes.count()):  # keep only the teardrop
                if "Teardrop" not in boxes.nth(i).inner_text():
                    boxes.nth(i).locator("input").uncheck()
            run.act(lambda: page.click("#apply-edits"))
            assert page.locator("#dl-step").is_visible(), "no STEP download after editing a STEP part"
            with page.expect_download(timeout=300_000) as dl:
                page.click("#dl-step")
            out = OUT / "l_bracket_dfm.step"
            dl.value.save_as(out)
            text = out.read_text(encoding="utf-8", errors="replace")
            assert text.startswith("ISO-10303-21") and "ADVANCED_FACE" in text, text[:200]
            up = page.evaluate("() => fdm.state.options[fdm.state.active].up.map(Math.round)")
            run.act(lambda: page.set_input_files("#file", str(out)), 300_000)
            ups = page.evaluate("() => fdm.state.options.map((o) => o.up.map(Math.round))")
            assert up in ups, f"edited orientation {up} not offered for the written STEP: {ups}"
            if ups.index(up) != page.evaluate("() => fdm.state.active"):
                run.act(lambda: page.locator("#options > li").nth(ups.index(up)).click())
            seen = findings(page)
            assert not any("Sideways hole prints oval" in f for f in seen), f"teardrop missing in the written STEP: {seen}"
            return f"{out.stat().st_size // 1024} kB STEP, {text.count('ADVANCED_FACE')} faces; reloaded: {page.locator('#status').inner_text()}"
        run.step("STEP in, design change, STEP out", step_round_trip)


        def test_design_change():
            # Mushroom held by its cap and pulled on the stem: the weak spot is the inside corner at the stem.
            run.act(lambda: page.click("[data-sample=mushroom]"))
            page.evaluate("""async () => {
              const r = await fdm.call("regions");
              fdm.state.faceRegions = new Uint32Array(r.face_regions.slice().buffer);
              fdm.state.regionInfo = r.regions;
              const flat = r.regions.map((x, i) => ({ ...x, i })).filter((x) => x.kind === "flat").sort((a, b) => b.area_mm2 - a.area_mm2);
              const cap = flat[0];
              const end = flat.find((x) => x.normal[0] * cap.normal[0] + x.normal[1] * cap.normal[1] + x.normal[2] * cap.normal[2] < -0.9 && x.area_mm2 < cap.area_mm2 / 2);
              fdm.state.loads = [{ region: cap.i, kind: "fixed" }, { region: end.i, kind: "force", kg: 20, direction: "pull", case: 1 }];
              document.getElementById("run-strength").disabled = false;
            }""")
            page.evaluate("() => fdm.steps.go('strength')")
            run.act(lambda: page.click("#run-strength"), 300_000)
            run.act(lambda: page.get_by_role("button", name="How can I make it stronger?").click(), 300_000)
            groups = page.locator("#strength-options summary").all_inner_texts()
            assert any(g.startswith("Design changes") for g in groups), groups
            page.locator("#strength-options summary", has_text="Design changes").evaluate("s => s.parentElement.open = true")
            run.act(lambda: page.get_by_role("button", name="Test it").first.click(), 600_000)
            status = page.locator("#status").inner_text()
            assert "With the rounded corners" in status and page.evaluate("() => fdm.state.versions.length") == 2, status
            # The thicker weak spot on the same part: another version, strength checked again.
            run.act(lambda: page.get_by_role("button", name="How can I make it stronger?").click(), 300_000)
            page.locator("#strength-options summary", has_text="Design changes").evaluate("s => s.parentElement.open = true")
            thicker = page.locator("#strength-options li", has_text="thicker").get_by_role("button", name="Test it")
            run.act(lambda: thicker.click(), 600_000)
            status2 = page.locator("#status").inner_text()
            assert "With the thicker weak spot" in status2 and page.evaluate("() => fdm.state.versions.length") == 3, status2
            return status + " | " + status2
        run.step("strength suggestion: test a design change", test_design_change)

        def design_change_in_view():
            """T-360: a design change pointed at in the list is turned into view, not hidden behind the part
            (the mushroom on its side, rounds at the stem's foot behind the cap)."""
            run.act(lambda: page.click("[data-sample=mushroom]"))
            side = page.evaluate("() => fdm.state.options.findIndex((o) => Math.abs(o.up[2]) < 0.1)")
            assert side >= 0, page.evaluate("() => fdm.state.options.map((o) => o.up)")
            if side != page.evaluate("() => fdm.state.active"):
                run.act(lambda: page.locator("#options > li").nth(side).click())
            page.evaluate("""async () => {
              const r = await fdm.call("regions");
              fdm.state.faceRegions = new Uint32Array(r.face_regions.slice().buffer);
              fdm.state.regionInfo = r.regions;
              const flat = r.regions.map((x, i) => ({ ...x, i })).filter((x) => x.kind === "flat").sort((a, b) => b.area_mm2 - a.area_mm2);
              const cap = flat[0];
              const end = flat.find((x) => x.normal[0] * cap.normal[0] + x.normal[1] * cap.normal[1] + x.normal[2] * cap.normal[2] < -0.9 && x.area_mm2 < cap.area_mm2 / 2);
              fdm.state.loads = [{ region: cap.i, kind: "fixed" }, { region: end.i, kind: "force", kg: 20, direction: "pull", case: 1 }];
              document.getElementById("run-strength").disabled = false;
            }""")
            page.evaluate("() => fdm.steps.go('strength')")
            run.act(lambda: page.click("#run-strength"), 300_000)
            if page.evaluate("() => document.querySelector('input[name=mode][value=print]').checked"):
                run.act(lambda: page.click("input[name=mode][value=both] >> xpath=.."))
            page.evaluate("() => fdm.steps.go('design')")
            # Suggest again with the strength result (the rounds at the weak spot): a size change re-runs them.
            run.act(lambda: page.dispatch_event("#param-stress_chamfer_mm", "change"), 300_000)
            items = page.locator("#edits li")
            assert items.count(), "no design changes listed"
            seen = []
            for i in range(items.count()):
                items.nth(i).hover()
                page.wait_for_timeout(500)  # the camera moves in 350 ms
                share = page.evaluate(f"() => fdm.viewer.editVisible(fdm.state.edits[{i}])")
                seen.append(round(share, 2))
            assert min(seen) >= 0.5, f"design changes hidden behind the part from the view they turn to: {seen}"
            return f"visible share per change: {seen}"
        run.step("design change turned into view", design_change_in_view)

        def thorough_design_test():
            # Thorough: sizes tried on copies in the background; only the picked one becomes a version.
            def set_mode(value):  # a click on the mode already chosen starts no check
                if not page.is_checked(f"input[name=mode][value={value}]"):
                    run.act(lambda: page.click(f"input[name=mode][value={value}] >> xpath=.."))
            set_mode("print")
            assert page.locator("#try-designs").is_hidden(), "shown although the design can't change"
            set_mode("both")
            page.evaluate("() => fdm.steps.go('strength')")
            versions = page.evaluate("() => fdm.state.versions.length")
            run.act(lambda: page.click("#try-designs"), 1_200_000)
            text = page.locator("#design-trials").inner_text()
            assert "Tested design changes" in text and page.locator("#design-trials li").count() >= 3, text
            assert page.evaluate("() => fdm.state.versions.length") == versions, "tries became versions"
            run.act(lambda: page.locator("#design-trials").get_by_role("button", name="Use this").first.click(), 600_000)
            assert page.evaluate("() => fdm.state.versions.length") == versions + 1
            set_mode("print")
            return " | ".join(text.splitlines()[2:4]) + " | " + page.locator("#status").inner_text()[:90]
        run.step("thorough: test design changes in the background", thorough_design_test)

        def my_presets():
            page.click("#my-presets > summary")
            page.click("#new-material")
            page.fill("#preset-fields [name=name]", "My PETG")
            page.select_option("#preset-fields [name=based_on]", "petg")
            page.fill("#cal-flat", "10")
            page.fill("#cal-upright", "4")
            page.click("#cal-z")
            page.select_option("#cal-warp", "120")
            page.click("#cal-warp-use")
            page.select_option("#cal-fit-snug", "0.2")
            page.select_option("#cal-fit-slide", "0.4")
            page.click("#cal-fit-use")
            page.select_option("#cal-overhang", "60")
            page.click("#cal-overhang-use")
            page.select_option("#cal-bridge", "30")
            page.click("#cal-bridge-use")
            page.fill("#cal-hole-model", "8")
            page.fill("#cal-hole-meas", "7.7")
            page.click("#cal-size-use")
            run.act(lambda: page.click("#preset-form button.primary"))
            chosen = page.evaluate("() => document.getElementById('material').value")
            mat = page.evaluate(f"() => fdm.state.presets.materials['{chosen}']")
            assert chosen.startswith("my_") and mat["name"] == "My PETG" and mat["z_strength_factor"]["none"] == 0.4, mat
            assert mat["warp_length_mm"]["none"] == 185, mat
            assert mat["fit_gap_mm"] == {"snug": 0.2, "press": 0.1, "sliding": 0.4, "loose": 0.55}, mat["fit_gap_mm"]
            assert mat["max_overhang_deg"] == 60 and mat["max_bridge_mm"] == 30, mat
            assert mat["hole_error_mm"] == -0.3 and "outer_error_mm" not in mat, mat  # measured on a print (T-345)
            orca = OUT / "orca_filament.json"
            orca.write_text('{"name": "Test PETG @BBL X1C", "filament_type": ["PETG"], "filament_density": ["1.27"], "filament_max_volumetric_speed": ["11"]}')
            page.set_input_files("#import-orca", str(orca))
            assert page.input_value("#preset-fields [name=density_g_cm3]") == "1.27"
            run.act(lambda: page.click("#preset-form button.primary"))
            # A printer with a smooth PEI plate while PETG is chosen: saved, with a plain warning (T-333).
            page.click("#new-printer")
            page.fill("#preset-fields [name=name]", "Smooth A1")
            page.select_option("#preset-fields [name=bed_surface]", "smooth_pei")
            run.act(lambda: page.click("#preset-form button.primary"))
            # Said when saved (the status line; a loaded part then gets new orientations) and kept above the 3MF button.
            page.wait_for_function("() => [...document.querySelectorAll('#setup-warnings li')].some((li) => li.textContent.includes('glue stick'))",
                                   timeout=120_000)
            warned = page.locator("#setup-warnings li").all_inner_texts()
            assert any("glue stick" in w for w in warned), warned
            page.reload()
            run.booted()
            names = page.locator("#material option").all_inner_texts()
            assert "My PETG" in names and "Test PETG" in names, names
            page.select_option("#printer", "bambu_p1s")  # the later steps use the built-in printer
            return f"{warned[0][:60]} | materials now: {names[-3:]}; calibrated z factor {mat['z_strength_factor']['none']}, warp length {mat['warp_length_mm']['none']} mm"
        run.step("my presets + calibration + Orca import", my_presets)

        def offline():
            # The reload above ran under the service worker, so the engine and libraries are cached now.
            run.act(lambda: page.click("[data-sample=l_bracket]"))
            page.context.set_offline(True)
            try:
                page.reload()
                run.booted()
                status = run.act(lambda: page.click("[data-sample=mushroom]"))
                assert page.locator("#options > li").count() >= 2, "no orientations offline"
            finally:
                page.context.set_offline(False)
            return f"offline: {status}"
        run.step("works offline after a visit", offline)

        def solids_and_snap():
            run.act(lambda: page.click("[data-sample=l_bracket]"))  # last: it changes the model
            page.evaluate("() => fdm.steps.go('extra')")
            spot = (
                "() => { const v = fdm.viewer; const n = v.part.geometry.getAttribute('position').count / 3;"
                " for (let f = 0; f < n; f += Math.max(1, Math.floor(n / 50))) { const [x, y] = v.screenPointOfFace(f);"
                " const el = document.elementFromPoint(x, y); if (el && el.tagName === 'CANVAS' && v.pick({clientX: x, clientY: y}) >= 0) return [x, y]; } return [0, 0]; }"
            )
            before = page.evaluate("() => fdm.state.versions.length")
            x, y = page.evaluate(spot)
            page.select_option("#vol-kind", "add_solid")
            page.click("#vol-pick")
            run.act(lambda: page.mouse.click(x, y))
            assert page.evaluate("() => fdm.state.volumes.at(-1)?.kind") == "add_solid", "block not placed"
            run.act(lambda: page.locator("#vol-list li", has_text="Add plastic").get_by_role("button", name="Apply to the model").click(), 300_000)
            after = page.evaluate("() => fdm.state.versions.length")
            assert after > before, "block not applied as a version"
            assert not page.evaluate("() => fdm.state.volumes.some((v) => v.kind === 'add_solid')")
            page.evaluate("() => fdm.steps.go('extra')")
            cube = OUT / "snap_cube.stl"
            cube.write_bytes(_box_stl([((0, 0, 0), 8)]))
            page.set_input_files("#snap-file", str(cube))
            page.wait_for_selector("#snap-hint:not([hidden])", timeout=180_000)
            x, y = page.evaluate(spot)
            run.act(lambda: page.mouse.click(x, y), 300_000)
            assert page.evaluate("() => fdm.state.versions.length") == after + 1, page.locator("#status").inner_text()
            return page.locator("#status").inner_text()[:120]
        run.step("advanced design changes: add plastic, snap a model on", solids_and_snap)

        def autosave_restore():
            name = page.evaluate("() => fdm.state.name")
            page.evaluate("() => fdm.autosave()")  # also every minute and when the tab is hidden
            page.reload()
            run.booted()
            page.wait_for_selector("#restore:not([hidden])", timeout=60_000)
            run.act(lambda: page.locator("#restore").get_by_role("button", name="Open it").click(), 300_000)
            page.wait_for_function("() => document.getElementById('status').textContent.includes('opened the project')", timeout=300_000)
            got = page.evaluate("() => fdm.state.name")
            assert got == name, (got, name)
            return f"restored {got}"
        run.step("autosave: continue where you left off", autosave_restore)

        def split_too_big():
            bar = OUT / "long_bar.stl"
            bar.write_bytes(_box_stl([((0, 0, 0), (400, 40, 20))]))
            run.act(lambda: page.set_input_files("#file", str(bar)), 300_000)
            assert page.evaluate("() => !document.getElementById('dl-split').hidden"), "no split offered for a 400 mm part"
            got = []
            page.on("download", lambda d: got.append(d.suggested_filename))
            run.act(lambda: page.evaluate("() => document.getElementById('dl-split').click()"), 300_000)
            page.wait_for_function("() => document.getElementById('status').textContent.includes('pins')", timeout=300_000)
            # Shown on the bed first, one plate per piece (T-321): the part is hidden meanwhile.
            shown = page.evaluate("() => ({ panel: !document.getElementById('split-panel').hidden, pieces: fdm.viewer.pieces?.children.length,"
                                  " part: fdm.viewer.part.visible })")
            assert shown == {"panel": True, "pieces": 4, "part": False}, shown
            def downloads(n):  # the files arrive one by one, slowly on a busy machine
                page.evaluate("() => document.getElementById('split-download').click()")
                for _ in range(60):
                    if len(got) >= n:
                        break
                    page.wait_for_timeout(500)

            downloads(3)
            assert sorted(got) == ["long_bar_piece_1.stl", "long_bar_piece_2.stl", "long_bar_pin.stl"], got
            pins = page.locator("#status").inner_text()[:100]
            got.clear()
            run.act(lambda: page.select_option("#split-joint", "dovetail"), 300_000)
            page.wait_for_function("() => document.getElementById('status').textContent.includes('dovetail')", timeout=300_000)
            downloads(2)
            page.wait_for_timeout(1000)  # no pin file after the two pieces
            assert sorted(got) == ["long_bar_piece_1.stl", "long_bar_piece_2.stl"], got  # no pins to print
            page.evaluate("() => document.getElementById('split-back').click()")
            back = page.evaluate("() => [document.getElementById('split-panel').hidden, fdm.viewer.pieces, fdm.viewer.part.visible]")
            assert back == [True, None, True], back
            return pins + " | " + page.locator("#status").inner_text()[:140]
        run.step("too big: split into pieces with pins or a dovetail, shown on the bed", split_too_big)

        def screws_in_project():
            # A screw choice goes into the project file and is sized again when it opens (T-320 follow-up).
            run.act(lambda: page.click("[data-sample=l_bracket]"))
            page.evaluate("async () => { const up = fdm.state.options[fdm.state.active].up;"
                          " const hole = (await fdm.call('design', { up })).features.find((f) => f.type === 'hole');"
                          " const r = await fdm.call('set_fastener', { region: hole.region, up, size: 'M3', use: 'insert' });"
                          " fdm.state.fastenerNotes = { [hole.region]: { size: 'M3', use: 'insert', text: r.explanation } }; }")
            with page.expect_download(timeout=120_000) as dl:
                page.evaluate("() => document.getElementById('dl-project').click()")
            out = OUT / "screws_project.json"
            dl.value.save_as(out)
            assert '"use": "insert"' in out.read_text(encoding="utf-8").replace('":"', '": "'), "no screw in the project file"
            run.act(lambda: page.click("[data-sample=mushroom]"))
            run.act(lambda: page.set_input_files("#file", str(out)), 300_000)
            page.wait_for_function("() => document.getElementById('status').textContent.includes('opened the project')", timeout=300_000)
            notes = page.evaluate("() => Object.values(fdm.state.fastenerNotes)")
            assert len(notes) == 1 and notes[0]["use"] == "insert" and "insert" in notes[0]["text"], notes
            return notes[0]["text"][:120]
        run.step("screws kept in a project", screws_in_project)

        def bambu_pause():
            # Closed pocket: Bambu Studio ignores the 3MF's pause, so it goes into the sliced file (T-322).
            block = OUT / "pocket_block.stl"
            block.write_bytes(_box_stl([((0, 0, 0), 20), ((14, 6, 6), (-8, 8, 3))]))  # mirrored box: an inner shell
            run.act(lambda: page.set_input_files("#file", str(block)), 300_000)
            assert page.evaluate("() => !document.getElementById('pause-sliced').hidden"), "no pause button for a pocket"
            layers = "".join(f"; CHANGE_LAYER\n; Z_HEIGHT: {z / 5:.1f}\nG1 E-.8\nG1 X10 Y10 Z{z / 5:.1f}\nG1 E.8\nG1 X20 Y10 E1\n" for z in range(1, 101))
            sliced = OUT / "pocket_block.gcode"
            sliced.write_text("; generated by BambuStudio\n" + layers, encoding="utf-8")
            with page.expect_download(timeout=120_000) as dl:
                page.set_input_files("#pause-file", str(sliced))
            out = OUT / dl.value.suggested_filename
            dl.value.save_as(out)
            text = out.read_text(encoding="utf-8")
            assert out.name == "pocket_block_pause.gcode" and text.count("M400 U1") == 1, (out.name, text.count("M400 U1"))
            page.wait_for_function("() => document.getElementById('status').textContent.includes('pauses before')", timeout=60_000)
            status = page.evaluate("() => document.getElementById('status').textContent")[:120]
            # Or no pause at all: a side slot the magnet slides into after printing (T-336), a design change.
            run.act(lambda: page.click("input[name=mode][value=both] >> xpath=.."))
            assert any("Side slot" in e for e in page.locator("#edits label").all_inner_texts()), "no side slot offered"
            page.evaluate("() => { for (const e of fdm.state.edits) e.selected = e.id.startsWith('magnet-slot'); fdm.steps.go('design'); }")
            run.act(lambda: page.click("#apply-edits"), 300_000)
            assert page.evaluate("() => document.getElementById('pause-sliced').hidden"), "pause still offered after the side slot"
            run.act(lambda: page.click("input[name=mode][value=print] >> xpath=.."))
            return status + " | side slot: no pause needed"
        run.step("pause added to a file sliced in Bambu Studio", bambu_pause)

        def fine_slopes():
            # A 10° roof prints as steps: thinner layers over just its height, in the 3MF (T-330).
            import io
            import struct
            import zipfile

            v = [(0, 0, 0), (40, 0, 0), (40, 40, 0), (0, 40, 0), (0, 0, 10), (40, 0, 17.05), (40, 40, 17.05), (0, 40, 10)]
            f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
            wedge = OUT / "sloped_roof.stl"
            wedge.write_bytes(bytes(80) + struct.pack("<I", len(f)) + b"".join(struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0) for a, b, c in f))
            run.act(lambda: page.set_input_files("#file", str(wedge)), 300_000)
            up = page.evaluate("() => fdm.state.options[fdm.state.active].up")
            if up[2] < 0.99:  # the roof up, as modelled
                i = page.evaluate("() => fdm.state.options.findIndex((o) => o.up[2] > 0.99)")
                run.act(lambda: page.locator("#options > li").nth(i).click(), 300_000)
            assert page.evaluate("() => !document.getElementById('fine-for').hidden"), "no thinner-layers option for a 10° roof"
            text = page.locator("#fine-text").inner_text()
            page.evaluate("() => fdm.steps.go('download')")
            with page.expect_download(timeout=300_000) as dl:
                page.evaluate("() => document.getElementById('dl-3mf').click()")
            z = zipfile.ZipFile(io.BytesIO(Path(dl.value.path()).read_bytes()))
            ranges = z.read("Metadata/layer_config_ranges.xml").decode()
            assert 'opt_key="layer_height">0.080' in ranges, ranges
            return text
        run.step("thinner layers on a shallow slope", fine_slopes)

        def print_failed():
            # "My print failed" on the sloped roof: rough top -> the shallow slope the check flagged comes first (T-344).
            page.evaluate("() => { const d = document.getElementById('failed-block'); d.open = true; d.dispatchEvent(new Event('toggle')); }")
            page.wait_for_function("() => document.getElementById('failed-symptom').options.length > 5", timeout=60_000)
            page.select_option("#failed-symptom", "rough_overhang")
            page.wait_for_function("() => document.querySelector('#failed-result .mine li')", timeout=60_000)
            mine = page.locator("#failed-result .mine li").all_inner_texts()
            assert any("Shallow slope" in m for m in mine), mine
            return mine[0][:100]
        run.step("my print failed: matched to the part's findings", print_failed)

        def mating_part():
            # Two parts of one assembly: a copy of the bracket 0.3 mm above it -> 0.30 mm per side, loose (T-348).
            import struct

            raw = (ROOT / "web/samples/l_bracket.stl").read_bytes()
            n = struct.unpack_from("<I", raw, 80)[0]
            body = bytearray(raw[84:84 + 50 * n])
            for t in range(n):
                for v in range(3):
                    at = t * 50 + 12 + v * 12 + 8
                    struct.pack_into("<f", body, at, struct.unpack_from("<f", body, at)[0] + 40.3)
            mate = OUT / "bracket_on_top.stl"
            mate.write_bytes(raw[:84] + bytes(body))
            run.act(lambda: page.click("[data-sample=l_bracket]"))
            page.evaluate("() => fdm.steps.go('fits')")
            run.act(lambda: page.set_input_files("#mate-file", str(mate)), 300_000)
            # Going to the step starts jobs of its own, so "idle" can come before the result: wait for it.
            page.wait_for_function("() => document.getElementById('mate-result').textContent.trim()", timeout=300_000)
            text = page.locator("#mate-result").inner_text()
            assert "0.30 mm per side" in text and "move freely" in text, text
            return text[:120]
        run.step("gap to a mating part", mating_part)

        def split_for_support():
            # A part that fits but needs a lot of support: two plates on a 50 mm post (longer than a bridge spans), so
            # one plate overhangs every way up. Cut so each piece prints its own way without (T-347).
            spool = ROOT / "tools" / "fixtures" / "spool.stl"
            run.act(lambda: page.set_input_files("#file", str(spool)), 300_000)
            # The cut is worked out after the check: wait for it (on a slow runner "idle" came first).
            page.wait_for_function("() => !document.getElementById('dl-split-support').hidden", timeout=300_000)
            run.act(lambda: page.evaluate("() => document.getElementById('dl-split-support').click()"), 300_000)
            page.wait_for_function("() => !document.getElementById('split-panel').hidden", timeout=60_000)
            text = page.locator("#split-text").inner_text()
            assert "much less support" in text and page.evaluate("() => fdm.viewer.pieces?.children.length") == 4, text
            page.evaluate("() => document.getElementById('split-back').click()")
            return text[:140]
        run.step("less support: split into two pieces", split_for_support)

        def grip_texture():
            # A part named "knob": the grip suggestion, the box ticked, fuzzy skin in the 3MF (T-340).
            import io
            import zipfile

            run.act(lambda: page.set_input_files("#file", str(ROOT / "core" / "tests" / "corpus" / "knob.stl")), 300_000)
            assert any("Grip texture" in t for t in findings(page)), findings(page)
            assert page.evaluate("() => document.getElementById('dl-grip').checked"), "grip box not ticked for a knob"
            page.evaluate("() => fdm.steps.go('download')")
            with page.expect_download(timeout=300_000) as dl:
                page.evaluate("() => document.getElementById('dl-3mf').click()")
            conf = zipfile.ZipFile(io.BytesIO(Path(dl.value.path()).read_bytes())).read("Metadata/model_settings.config").decode()
            assert 'key="fuzzy_skin" value="external"' in conf, "no fuzzy skin in the 3MF"
            run.act(lambda: page.click("[data-sample=test_block]"))
            assert not page.evaluate("() => document.getElementById('dl-grip').checked"), "grip box kept for another part"
            return "suggested for knob.stl, fuzzy skin in the 3MF, off for the next part"
        run.step("grip texture on a knob", grip_texture)

        def plate_copies():
            # Four copies of the test block: how many fit, the plate's time, the notes, four items in the 3MF (T-349).
            import io
            import zipfile

            run.act(lambda: page.click("[data-sample=test_block]"))
            page.evaluate("() => fdm.steps.go('download')")
            page.fill("#dl-copies", "4")
            page.dispatch_event("#dl-copies", "change")
            page.wait_for_function("() => document.getElementById('copies-text').textContent.includes('4 copies')", timeout=60_000)
            text = page.locator("#copies-text").inner_text()
            assert "fit on this printer's bed" in text and page.locator("#copies-notes li").count() == 2, text
            with page.expect_download(timeout=300_000) as dl:
                page.evaluate("() => document.getElementById('dl-3mf').click()")
            model = zipfile.ZipFile(io.BytesIO(Path(dl.value.path()).read_bytes())).read("3D/3dmodel.model").decode()
            assert model.count("<item ") == 4, model.count("<item ")
            run.act(lambda: page.click("[data-sample=l_bracket]"))
            assert page.evaluate("() => document.getElementById('dl-copies').value") == "1", "copies kept for another part"
            return text
        run.step("copies on one plate", plate_copies)

        def grip_fins():
            # A Ø6 pin pressed into a plate: grip fins offered, shown in both colours, applied (T-335).
            run.act(lambda: page.set_input_files("#file", str(ROOT / "tools" / "fixtures" / "press_plate.stl")), 300_000)
            if not page.locator("input[name=mode][value=both]").is_checked():  # fits and design changes show for the design
                run.act(lambda: page.click("input[name=mode][value=both] >> xpath=.."))
            page.evaluate("() => fdm.steps.go('fits')")
            row = page.locator("#fits li", has_text="Ø6.0 mm hole")
            row.locator(".fit:not(.fastener) input").fill("6")
            row.locator(".fit:not(.fastener) select").select_option("press")
            run.act(lambda: row.get_by_role("button", name="Size it").click())
            edits = page.locator("#edits label").all_inner_texts()
            assert any("grip fins" in e for e in edits), edits
            page.evaluate("() => { for (const e of fdm.state.edits) e.selected = e.id.startsWith('grip-fins'); fdm.steps.go('design'); }")
            page.wait_for_timeout(800)
            colours = set(page.evaluate("() => fdm.viewer.ghosts.children.map((c) => c.material.color.getHexString())"))
            assert len(colours) == 2, f"fins and pocket not shown in two colours: {colours}"
            before = page.evaluate("() => fdm.state.triangles.length")
            run.act(lambda: page.click("#apply-edits"), 300_000)
            after = page.evaluate("() => ({ n: fdm.state.triangles.length, v: fdm.state.version })")
            assert after["v"] == 1 and after["n"] != before, after
            run.act(lambda: page.click("input[name=mode][value=print] >> xpath=.."))
            return next(e for e in edits if "grip fins" in e)[:80]
        run.step("grip fins for a press fit", grip_fins)
        def share_file():
            # Share: the report with the part inside; opened here again it brings the part and its settings back (T-350).
            run.act(lambda: page.click("[data-sample=l_bracket]"))
            run.act(lambda: page.select_option("#material", "petg"), 300_000)
            page.evaluate("() => fdm.steps.go('download')")
            with page.expect_download(timeout=300_000) as dl:
                page.evaluate("() => document.getElementById('dl-share').click()")
            shared = OUT / dl.value.suggested_filename
            dl.value.save_as(shared)
            html = shared.read_text(encoding="utf-8")
            assert shared.name == "l_bracket_share.html" and "The part is inside this file" in html and 'id="fdm-check-project"' in html
            run.act(lambda: page.click("[data-sample=test_block]"))
            run.act(lambda: page.select_option("#material", "pla"), 300_000)
            run.act(lambda: page.set_input_files("#file", str(shared)), 300_000)
            page.wait_for_function("() => document.getElementById('status').textContent.includes('opened the project')", timeout=300_000)
            got = page.evaluate("() => ({ name: fdm.state.name, material: document.getElementById('material').value })")
            assert got["name"].startswith("l_bracket") and got["material"] == "petg", got
            return f"{shared.stat().st_size // 1024} kB, opened again: {got}"
        run.step("share: report with the part inside", share_file)

        def load_at_an_angle():
            """T-372: no "hanging down (−Z as modelled)"; "at an angle" is chosen before placing the load, relative
            to the tapped face, with the arrow shown on the part while it's set."""
            run.act(lambda: page.click("[data-sample=mushroom]"))
            page.evaluate("() => fdm.steps.go('strength')")
            assert "down" not in page.eval_on_selector_all("#dir option", "os => os.map((o) => o.value)")
            xy = page.evaluate("""() => { const v = fdm.viewer; const n = v.part.geometry.getAttribute('position').count / 3;
              for (let f = 0; f < n; f++) { const [x, y] = v.screenPointOfFace(f); const el = document.elementFromPoint(x, y);
                if (el && el.tagName === 'CANVAS' && v.pick({ clientX: x, clientY: y }) === f) return [x, y]; } return null; }""")
            page.mouse.click(*xy)
            page.wait_for_selector("#pick-panel:not([hidden])", timeout=300_000)  # the first click loads the face tools
            arrows = page.evaluate("() => fdm.viewer.symbols?.children.length ?? 0")
            page.select_option("#dir", "angle")
            page.dispatch_event("#dir", "input")
            page.fill("#dir-lean", "90")
            page.dispatch_event("#dir-lean", "input")
            assert page.locator("#dir-angle").is_visible()
            assert page.evaluate("() => fdm.viewer.symbols.children.length") > arrows, "no preview arrow"
            normal = page.evaluate("() => fdm.state.regionInfo[fdm.state.selectedRegion].normal")
            page.click("#as-load")
            d = page.evaluate("() => fdm.state.loads.at(-1).direction")
            assert isinstance(d, list) and abs(sum(a * b for a, b in zip(d, normal))) < 0.01, (d, normal)  # along the face
            assert page.evaluate("() => fdm.state.previewLoad") is None
            assert "90° lean" in page.locator("#loads").inner_text(), page.locator("#loads").inner_text()
            return f"lean 90° on a face with normal {[round(v, 2) for v in normal]}: {d}"
        run.step("load at an angle to the face", load_at_an_angle)

        def hidden_gap_from_side():
            """T-363: a hidden gap clicked on a side face goes into the wall (it was placed along the surface or
            refused), and its outline is drawn over the part so it can be seen inside it."""
            run.act(lambda: page.click("[data-sample=mushroom]"))
            page.evaluate("() => fdm.steps.go('extra')")
            xy = page.evaluate("""() => {
              const v = fdm.viewer, p = v.part.geometry.getAttribute('position'), n = p.count / 3;
              for (let f = 0; f < n; f++) {
                const a = [0, 1, 2].map((k) => [p.getX(3 * f + k), p.getY(3 * f + k), p.getZ(3 * f + k)]);
                const u = a[1].map((x, i) => x - a[0][i]), w = a[2].map((x, i) => x - a[0][i]);
                const c = [u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]];
                if (Math.abs(c[2]) > 0.1 * Math.hypot(...c)) continue; // side faces only
                const [x, y] = v.screenPointOfFace(f);
                const el = document.elementFromPoint(x, y);
                if (el && el.tagName === 'CANVAS' && v.pick({ clientX: x, clientY: y }) === f) return [x, y];
              }
              return null;
            }""")
            assert xy, "no side face in view"
            page.select_option("#vol-kind", "void")
            page.click("#vol-pick")
            run.act(lambda: page.mouse.click(*xy))
            got = page.evaluate("() => fdm.state.volumes.map((v) => ({ kind: v.kind, notes: v.notes }))")
            assert len(got) == 1 and got[0]["kind"] == "void", f"gap not placed: {got} | {page.locator('#status').inner_text()}"
            assert not any("shows as a hole" in n for n in got[0]["notes"]), got
            over = page.evaluate("() => fdm.viewer.zones.children.some((c) => c.isLineSegments && c.material.depthTest === false)")
            assert over, "the gap's outline is hidden by the part"
            page.locator("#vol-list li").first.get_by_role("button", name="Remove").click()
            return f"placed inside the wall: {got[0]['notes'][:1]}"
        run.step("hidden gap from a side face", hidden_gap_from_side)

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
