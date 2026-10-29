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
            run.act(lambda: page.click("#suggest-design"))
            edits = page.locator("#edits label").all_inner_texts()
            assert any("Ø8.0" in e for e in edits), f"expected teardrop for the Ø8 hole, got {edits}"
            run.act(lambda: page.fill("#param-teardrop_angle_deg", "30") or page.dispatch_event("#param-teardrop_angle_deg", "change"))
            assert any("30° point" in e for e in page.locator("#edits label").all_inner_texts()), "teardrop angle input had no effect"
            run.act(lambda: page.click("#apply-edits"))
            run.act(lambda: page.click("#suggest-design"))
            again = page.locator("#edits label").all_inner_texts()
            state = page.evaluate("() => ({active: fdm.state.active, up: fdm.state.options[fdm.state.active].up, name: fdm.state.name})")
            assert not any("Ø8.0" in e for e in again), f"teardrop still suggested after applying it: {again} state={state}"
            return f"applied {edits} -> now suggests {again or 'nothing'}"
        run.step("design edits apply + re-check", design)

        def added_material():
            run.act(lambda: page.click("[data-sample=test_block]"))
            run.act(lambda: page.click("#suggest-design"))
            edits = page.locator("#edits label").all_inner_texts()
            assert sum("Thicken" in e for e in edits) == 2, f"expected two thin walls to thicken, got {edits}"
            ops = page.evaluate("() => fdm.state.edits.map((e) => e.op)")
            assert "add" in ops and len(page.evaluate("() => fdm.viewer.ghosts.children.map((c) => c.material.color.getHexString())")) == len(ops)
            run.act(lambda: page.click("#apply-edits"))
            run.act(lambda: page.click("#suggest-design"))
            again = page.locator("#edits label").all_inner_texts()
            assert not any("Thicken" in e for e in again), f"walls still thin after thickening: {again}"
            return f"{edits} -> {again or 'nothing'} | findings: {findings(page)}"
        run.step("design edits that add material", added_material)

        def feature_confirm():
            run.act(lambda: page.click("[data-sample=l_bracket]"))
            run.act(lambda: page.click("#suggest-design"))
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

        def shells():
            page.locator(".shells summary").click()
            page.fill("#sh-walls", "6")
            run.act(lambda: page.dispatch_event("#sh-walls", "change"), 300_000)
            page.wait_for_function("() => document.querySelector('#strength-result')?.innerText.includes('6 walls')", timeout=300_000)
            text = page.locator("#strength-result").inner_text()
            settings = page.locator("#settings").inner_text()
            assert "Walls ★" in settings, settings
            return text.splitlines()[1][:120]
        run.step("walls set by hand go into strength and the 3MF", shells)

        def optimise():
            run.act(lambda: page.click("#run-optimise"), 900_000)
            text = page.locator("#optimise-result").inner_text()
            assert "g of plastic" in text or "aren't enough" in text, text
            run.act(lambda: page.locator("#optimise-result button").click(), 300_000)
            return text.splitlines()[1][:160]
        run.step("find the lightest settings that hold", optimise)

        def heat_map():
            n = page.evaluate("() => [fdm.state.check.face_temp.length, fdm.viewer.part.geometry.getAttribute('position').count / 3]")
            assert n[0] == n[1], n
            page.check("#show-temp")
            page.uncheck("#show-temp")
            return f"{n[0]} faces coloured by heat"
        run.step("heat while printing colours", heat_map)

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
            page.click("input[name=goal][value=strength] >> xpath=..")  # slow: checks several orientations
            page.wait_for_selector("#progress:not([hidden])", timeout=30_000)
            page.wait_for_function("() => document.getElementById('progress-bar').value > 0", timeout=30_000)
            run.act(lambda: page.click("#cancel"), 60_000)
            status = page.locator("#status").inner_text()
            assert status.startswith("Cancelled"), status
            # The restarted engine must have the part again: re-orienting works.
            run.act(lambda: page.click("input[name=goal][value=balanced] >> xpath=.."))
            after = page.evaluate("() => fdm.state.options.length")
            assert after == before, f"{before} options before, {after} after restart"
            return f"{status} | after restart: {after} options, {len(findings(page))} findings"
        run.step("progress + cancel", cancel)

        def paste_back():
            run.act(lambda: page.click("[data-sample=hook]"))
            page.context.grant_permissions(["clipboard-read", "clipboard-write"])
            page.click("#copy-report")
            page.wait_for_function("() => document.getElementById('status').textContent.startsWith('Report copied')")
            report = page.evaluate("() => navigator.clipboard.readText()")
            assert "## For the AI assistant: how to answer" in report and "- 1: " in report, report[-600:]
            page.click("#paste-block > summary")
            reply = 'Lay it on its side.\n```json\n{"orientation": 1, "settings": {"wall_loops": 5}}\n```'
            page.fill("#paste-input", reply)
            run.act(lambda: page.click("#paste-apply"))
            result = page.locator("#paste-result li").all_inner_texts()
            walls = page.evaluate("() => document.querySelector('#settings').innerText")
            assert page.evaluate("() => fdm.state.active") == 1 and not page.locator("#paste-result li.error").count(), result
            assert "5" in [line.split("\t")[-1].strip() for line in walls.splitlines() if line.startswith("Walls")], walls
            return f"{result}"
        run.step("paste back an AI answer", paste_back)

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
            if "Sideways Hole Prints Oval" not in " ".join(findings(page)):
                run.act(lambda: page.locator("#options > li").nth(1).click())
            if not page.locator("input[name=mode][value=both]").is_checked():
                run.act(lambda: page.click("input[name=mode][value=both] >> xpath=.."))
            run.act(lambda: page.click("#suggest-design"))
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
            assert not any("Sideways Hole Prints Oval" in f for f in seen), f"teardrop missing in the written STEP: {seen}"
            return f"{out.stat().st_size // 1024} kB STEP, {text.count('ADVANCED_FACE')} faces; reloaded: {page.locator('#status').inner_text()}"
        run.step("STEP in, design change, STEP out", step_round_trip)

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
            run.act(lambda: page.click("#preset-form button.primary"))
            chosen = page.evaluate("() => document.getElementById('material').value")
            mat = page.evaluate(f"() => fdm.state.presets.materials['{chosen}']")
            assert chosen.startswith("my_") and mat["name"] == "My PETG" and mat["z_strength_factor"]["none"] == 0.4, mat
            assert mat["warp_length_mm"]["none"] == 185, mat
            orca = OUT / "orca_filament.json"
            orca.write_text('{"name": "Test PETG @BBL X1C", "filament_type": ["PETG"], "filament_density": ["1.27"], "filament_max_volumetric_speed": ["11"]}')
            page.set_input_files("#import-orca", str(orca))
            assert page.input_value("#preset-fields [name=density_g_cm3]") == "1.27"
            run.act(lambda: page.click("#preset-form button.primary"))
            page.reload()
            run.booted()
            names = page.locator("#material option").all_inner_texts()
            assert "My PETG" in names and "Test PETG" in names, names
            return f"materials now: {names[-3:]}; calibrated z factor {mat['z_strength_factor']['none']}, warp length {mat['warp_length_mm']['none']} mm"
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
