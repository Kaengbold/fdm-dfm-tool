"""Browser smoke test of the web app (headless Edge/Chrome via Playwright).

    pip install playwright
    python tools/e2e_smoke.py [--quick] [--headed] [--browser chrome|chromium] [--gemini-review]

Starts its own static server, drives the app (web/index.html) like a user, and prints one line per check plus every
console error. Screenshots go to ./e2e-output/ (the page as each check left it). Exit code 1 on failure.
Without --quick it also walks the whole app as one user journey at 1280×720 and 1440×900 (T-581).
With --gemini-review, the Gemini CLI looks at each screenshot for visual problems and writes its notes to
e2e-output/gemini-review.md (it only gets the image, no shell access).
"""

from __future__ import annotations

import argparse
import functools
import http.server
import re
import socketserver
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "e2e-output"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    # Windows' registry maps .mjs to text/plain, and browsers refuse to import a module served
    # that way: the browser slicer (engine-st.mjs) would then always fall back to our own slicer.
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".mjs": "text/javascript", ".js": "text/javascript", ".wasm": "application/wasm"}

    def log_message(self, *args):
        pass


def serve() -> int:
    handler = functools.partial(QuietHandler, directory=str(ROOT))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


class Run:
    """One line per check, and a screenshot of the page each check leaves (SharedUi takes it as the page closes)."""

    def __init__(self, ui):
        self.ui = ui
        self.failures: list[str] = []

    def step(self, name: str, fn):
        self.ui.shot = OUT / f"{len(self.failures):02d}_{re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:60]}.png"
        try:
            print(f"PASS {name}: {fn() or ''}")
        except Exception as e:  # noqa: BLE001 - report and continue
            self.failures.append(name)
            print(f"FAIL {name}: {e}")


REVIEW_PROMPT = (
    "Do not run commands or edit files. This is a screenshot of a browser app that analyses 3D-print parts "
    "(a step list on the left, a 3D view of the part on a print bed, a panel on the right). List concrete visual problems only: "
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


class SharedUi:
    """The new-UI checks share one browser context, so Pyodide and its packages (tens of MB from the CDN) come from
    the browser cache after the first check instead of downloading again per check, which made later checks time
    out on slow runs. Each page still starts clean: this site's storage is wiped before it is handed out."""

    def __init__(self, browser, port: int):
        self.port = port
        self.ctx = browser.new_context(viewport={"width": 1280, "height": 720}, accept_downloads=True)
        self.shot = None  # where the next closed page's screenshot goes (Run.step sets it)

    def _clean_page(self):
        page = self.ctx.new_page()
        page.goto(f"http://127.0.0.1:{self.port}/web/samples/loaded_hook.json")
        page.evaluate("""async () => {
          try { localStorage.clear(); sessionStorage.clear(); } catch {}
          for (const db of (await indexedDB.databases?.()) ?? []) await new Promise((r) => { const q = indexedDB.deleteDatabase(db.name); q.onsuccess = q.onerror = q.onblocked = r; });
        }""")
        return page

    def new_page(self, viewport=None, **_):
        page = self._clean_page()
        if viewport:
            page.set_viewport_size(viewport)
        close = page.close

        def shot_and_close(**kw):
            # The check's last view, also when it failed (its finally closes the page).
            if self.shot and not page.is_closed():
                try:
                    page.screenshot(path=str(self.shot), timeout=90_000)
                except Exception as e:  # noqa: BLE001 - a missing picture isn't a failed check
                    print(f"  (no screenshot: {e})")
            close(**kw)

        page.close = shot_and_close
        return page

    def new_context(self, **_):
        return _OneContext(self)

    def close(self):
        self.ctx.close()


class _OneContext:
    """What a check that asks for its own context gets: pages from the shared one; close() closes only its pages."""

    def __init__(self, shared):
        self.shared, self.pages = shared, []

    def new_page(self, **kw):
        page = self.shared.new_page(**kw)
        self.pages.append(page)
        return page

    def close(self):
        for page in self.pages:
            if not page.is_closed():
                page.close()


def _scaled_stl(path: Path, factor: float) -> bytes:
    """A binary STL scaled by ``factor`` (for the wrong-unit check)."""
    import struct

    data = bytearray(path.read_bytes())
    n = struct.unpack_from("<I", data, 80)[0]
    for i in range(n):
        off = 84 + i * 50 + 12
        coords = struct.unpack_from("<9f", data, off)
        struct.pack_into("<9f", data, off, *(c * factor for c in coords))
    return bytes(data)


def check_new_ui(browser, port: int) -> str:
    """The new UI (web/index.html, T-531): Start → Part → a sample lands on Setup with the part shown; a STEP file
    does too; nothing but the format line and Sample parts sits under the drop zone."""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        assert page.locator(".big-card.is-soon").get_attribute("aria-disabled") == "true"
        assert page.locator(".big-card.is-soon").evaluate("e => e.tabIndex") < 0, "Coming soon card is a tab stop"
        page.get_by_role("button", name="Optimize your print").click()
        assert page.locator("#step-title").inner_text() == "Part"
        texts = page.locator(".drop-wrap").inner_text().split("\n")
        assert [x for x in texts if x.strip()] == ["Drop your part here", "Choose file", "STL, 3MF, STEP or a saved project", "Sample parts"], texts
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="Wall hook").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup'", timeout=240_000)
        assert page.evaluate("() => fdmUI.store.get().part.name") == "hook.stl"
        assert page.evaluate("() => fdmUI.services.stage.part !== null"), "part not shown on the stage"
        assert page.locator(".stage-camera").count() == 1
        page.wait_for_function("() => Boolean(fdmUI.store.get().catalog?.printers)", timeout=60_000)
        # A STEP file through the part pill (replace part) → Part → file input.
        page.locator(".part-pill").click()
        page.set_input_files(".drop-wrap input[type=file]", str(ROOT / "tools" / "fixtures" / "l_bracket.step"))
        page.wait_for_function("() => fdmUI.store.get().part?.name === 'l_bracket.step' && document.getElementById('step-title')?.textContent === 'Setup'",
                               timeout=240_000)
        part = page.evaluate("() => fdmUI.store.get().part")
        assert part["kind"] == "step" and round(part["size"][0]) == 60, part
        # A broken file: a plain reason and "Try another file", the flow stays on Part.
        page.locator(".part-pill").click()
        page.set_input_files(".drop-wrap input[type=file]", files=[{"name": "broken.stl", "mimeType": "model/stl", "buffer": b"not a mesh at all"}])
        page.wait_for_selector(".drop-error", timeout=120_000)
        assert page.get_by_role("button", name="Try another file").is_visible()
        assert page.locator("#step-title").inner_text() == "Part"
        # A part drawn in inches (25.4× too small): the unit bar asks, scaling fixes the size, then on to Setup.
        tiny = _scaled_stl(ROOT / "web" / "samples" / "l_bracket.stl", 1 / 25.4)
        page.set_input_files(".drop-wrap input[type=file]", files=[{"name": "inch_bracket.stl", "mimeType": "model/stl", "buffer": tiny}])
        page.wait_for_selector(".unit-bar", timeout=120_000)
        page.locator(".unit-bar button").first.click()
        page.wait_for_function("() => fdmUI.store.get().part?.name === 'inch_bracket.stl' && document.getElementById('step-title')?.textContent === 'Setup'",
                               timeout=120_000)
        scaled = page.evaluate("() => fdmUI.store.get().part.size")
        assert round(scaled[0]) == 60, scaled
        assert not errors, errors
        return f"sample and STEP land on Setup; STEP size {[round(v) for v in part['size']]} mm; broken file stays on Part; inch part scaled to {round(scaled[0])} mm"
    finally:
        page.close()


def check_new_ui_picker(browser, port: int) -> str:
    """The new UI's hover picker (T-540): a face shows its badge, an inside corner wins near its line, the keyboard
    list finds and picks a hole, and the GPU pick stays fast on a 500k-triangle part."""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="L-bracket").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup'", timeout=240_000)
        page.evaluate("() => fdmUI.go('holds')")
        page.wait_for_function("() => fdmUI.services.picker.enabled && fdmUI.services.picker.data", timeout=120_000)
        box = page.locator(".stage-canvas").bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.55, box["y"] + box["height"] * 0.62)
        page.wait_for_function("() => fdmUI.services.picker.hit?.kind === 'face'")
        face = page.locator(".pick-badge").inner_text()
        assert face.startswith("Flat face"), face
        # The bracket's inside corner: put the cursor 3 px off its projected middle.
        x, y = page.evaluate("""async () => {
          const THREE = await import('three');
          const p = fdmUI.services.picker, st = fdmUI.services.stage;
          const line = p.data.edges.find((l) => l.kind === 'corner');
          const m = line.meta.midpoint, o = st.part.offset;
          const v = new THREE.Vector3(m[0] + o.x, m[1] + o.y, m[2] + o.z).project(st.camera);
          const r = st.canvas.getBoundingClientRect();
          return [(v.x + 1) / 2 * r.width + r.left + 3, (1 - v.y) / 2 * r.height + r.top];
        }""")
        page.mouse.move(x, y)
        page.wait_for_function("() => fdmUI.services.picker.hit?.kind === 'corner'", timeout=5_000)
        corner = page.locator(".pick-badge").inner_text()
        assert corner.startswith("Inside corner"), corner
        page.mouse.move(box["x"] + 5, box["y"] + 5)
        # Keyboard: Tab-able stage, Enter opens the feature list, typing filters, Enter picks.
        page.locator(".stage-canvas").focus()
        page.keyboard.press("Enter")
        page.keyboard.type("hole")
        assert page.locator(".pick-badge").inner_text().endswith("mm hole")
        picked = page.evaluate("""() => new Promise((resolve) => {
          const off = fdmUI.services.picker.on('pick', (h) => { off(); resolve(h.info.kind); });
          document.querySelector('.feature-search').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        })""")
        assert picked == "hole", picked
        # Big part: the pick reads one pixel from the GPU, whatever the triangle count.
        perf = page.evaluate("""async () => {
          const THREE = await import('three');
          const gl0 = fdmUI.services.stage.renderer.getContext();
          const dbg = gl0.getExtension('WEBGL_debug_renderer_info');
          const gpu0 = String(dbg ? gl0.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl0.getParameter(gl0.RENDERER)).toLowerCase();
          // 500k triangles only on a real GPU (that's what is timed); a software renderer gets ~50k, enough to check hits
          // without slowing the browser for the checks after this one.
          const soft = ['swiftshader', 'llvmpipe', 'software'].some((k) => gpu0.includes(k));
          const g = new THREE.IcosahedronGeometry(40, soft ? 50 : 160).toNonIndexed();
          const tris = new Float32Array(g.attributes.position.array), n = tris.length / 9;
          fdmUI.services.stage.showPart(tris);
          const fr = new Uint32Array(n).map((_, i) => i % 64);
          fdmUI.services.picker.setData({ face_regions: new Uint8Array(fr.buffer),
            regions: Array.from({ length: 64 }, (_, i) => ({ id: i, kind: 'curved', area_mm2: 100, centroid: [0, 0, 40] })) });
          const c = fdmUI.services.stage.canvas.getBoundingClientRect();
          const t0 = performance.now();
          let hits = 0;
          for (let i = 0; i < 30; i++) if (fdmUI.services.picker.pickAt(c.left + c.width * (0.4 + i / 150), c.top + c.height * 0.5)) hits++;
          const gl = fdmUI.services.stage.renderer.getContext();
          const info = gl.getExtension('WEBGL_debug_renderer_info');
          const gpu = info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
          return { n, ms: (performance.now() - t0) / 30, hits, gpu };
        }""")
        assert perf["hits"] == 30, perf
        # The pick costs one GPU draw: fast on a real graphics card. CI runners have none (SwiftShader / llvmpipe
        # draw in software, ~75 ms here), so the time limit applies only on real hardware.
        software = any(k in str(perf["gpu"]).lower() for k in ("swiftshader", "llvmpipe", "software"))
        assert software or perf["ms"] < 40, perf
        assert not errors, errors
        where = "software renderer, not timed" if software else "GPU"
        return f"{face}; {corner}; keyboard picked a hole; {perf['n']} triangles: {perf['ms']:.1f} ms per pick ({where})"
    finally:
        page.close()


def check_new_ui_settings(browser, port: int) -> str:
    """The new UI's Settings & calibration hub with the live Python engine (T-533): calibration STL download,
    calibration results pushed to custom_presets and used by api.tolerance, reload persistence into the engine,
    and Back returning to the part intact."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 720}, accept_downloads=True)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.wait_for_function("() => Boolean(fdmUI.services.settings?.isEngineSynced?.())", timeout=240_000)

        # Load a sample part so we can verify "Back to your part" preserves it.
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="Wall hook").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup'", timeout=240_000)

        # Open Settings -> Calibration tests -> Overhang: download STL + save 65 deg.
        page.locator(".app-header").get_by_role("button", name="Settings & calibration", exact=True).click()
        page.wait_for_selector(".settings-wrap")
        page.locator("#settings-tab-calibration").click()
        page.locator('[data-cal-card="overhang"]').click()
        with page.expect_download(timeout=60_000) as dl_info:
            page.get_by_role("button", name="Download model").click()
        stl_name = dl_info.value.suggested_filename
        stl_size = Path(dl_info.value.path()).stat().st_size
        assert stl_name.endswith(".stl") and stl_size > 1000, (stl_name, stl_size)
        page.locator('.cal-pick[data-pick-value="65"]').click()
        page.get_by_role("button", name="Save result").click()

        # Also enter Fits (snug +0.1 mm, sliding +0.3 mm).
        page.get_by_role("button", name="All calibration tests").click()
        page.locator('[data-cal-card="fit"]').click()
        groups = page.locator(".cal-picker")
        groups.nth(0).locator('[data-pick-value="0.1"]').click()
        groups.nth(1).locator('[data-pick-value="0.3"]').click()
        page.get_by_role("button", name="Save result").click()

        page.get_by_role("button", name="All calibration tests").click()
        page.locator('[data-cal-card="creep"]').click()
        assert "9.19 MPa" in page.locator(".cal-steps + .hint").inner_text()
        with page.expect_download(timeout=60_000) as creep_download:
            page.get_by_role("button", name="Download model").click()
        assert creep_download.value.suggested_filename == "creep_test.stl"
        assert Path(creep_download.value.path()).stat().st_size > 1000
        page.get_by_role("textbox", name="After 1 minute").fill("4")
        page.get_by_role("button", name="Save result").click()
        page.get_by_text("The ×3 target stays", exact=False).wait_for()
        revision = page.evaluate("() => fdmUI.store.revisions().profile")
        page.get_by_role("textbox", name="After 1 hour").fill("4.1")
        page.get_by_role("button", name="Save result").click()
        page.get_by_text("Early estimate.", exact=False).wait_for()
        assert page.evaluate("() => fdmUI.store.revisions().profile") > revision

        # Fit ABS grip from the warping bars on a known plate.
        page.evaluate("""async () => {
          const { settings } = fdmUI.services;
          const data = settings.getData();
          data.mine.printers.my_test_pei = { based_on: "prusa_mk4s", bed_surface: "textured_pei" };
          data.calibrationMaterial = "abs";
          settings.importJson(JSON.stringify(data));
          await settings.syncEngine();
          fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: "my_test_pei" } });
        }""")
        page.locator('[data-cal-card="warping"]').click()
        page.locator('.cal-pick[data-pick-value="40"]').click()
        page.get_by_role("button", name="Save result").click()
        page.get_by_text("Bed grip calibrated for Textured PEI at 100 °C.").wait_for(timeout=120_000)

        # Confirm the Python engine actually received the measured values and uses them in tolerance().
        page.wait_for_function(
            "() => fdmUI.store.get().catalog.materials.my_cal_pla?.max_overhang_deg === 65",
            timeout=30_000,
        )
        engine_check = page.evaluate("""async () => {
          await fdmUI.services.settings.syncEngine();
          const presets = await fdmUI.services.engine.call("presets");
          const tol = await fdmUI.services.engine.call("tolerance", {
            profile: { printer: "bambu_p1s", material: "my_cal_pla", nozzle: "0.4" },
          });
          const { adhesion } = await fdmUI.services.engine.call("warping", {
            up: [0, 0, 1], profile: { printer: "my_test_pei", material: "my_cal_abs" },
          });
          return { mat: presets.materials.my_cal_pla, grip: presets.materials.my_cal_abs.bed_grip_calibration.none, adhesion, tol };
        }""")
        assert engine_check["mat"]["max_overhang_deg"] == 65, engine_check
        assert engine_check["mat"]["creep_readings"] == [[1, 4], [60, 4.1]], engine_check
        assert engine_check["grip"]["longest_flat_mm"] == 40, engine_check
        assert engine_check["adhesion"]["shear_source"] == "calibrated", engine_check
        assert engine_check["tol"]["measured"] is True, engine_check
        assert abs(engine_check["tol"]["gaps"]["snug"] - 0.1) < 1e-6, engine_check

        # Back to your part returns with hook.stl intact on Setup.
        page.get_by_role("button", name="Back to your part").click()
        assert page.locator("#step-title").inner_text() == "Setup"
        assert page.evaluate("() => fdmUI.store.get().part?.name") == "hook.stl"

        # Reload: engine receives the persisted calibration automatically on boot.
        page.reload()
        page.wait_for_selector("body[data-ready]")
        page.wait_for_function("() => Boolean(fdmUI.services.settings?.isEngineSynced?.())", timeout=240_000)
        reloaded = page.evaluate("""async () => {
          const presets = await fdmUI.services.engine.call("presets");
          return { pla: presets.materials.my_cal_pla, abs: presets.materials.my_cal_abs };
        }""")
        assert reloaded["pla"]["max_overhang_deg"] == 65, reloaded
        assert reloaded["pla"]["creep_readings"] == [[1, 4], [60, 4.1]], reloaded
        assert reloaded["abs"]["bed_grip_calibration"]["none"] == engine_check["grip"], reloaded

        # More thresholds disclosure loads engine expert_settings cleanly.
        page.locator(".app-header").get_by_role("button", name="Settings & calibration", exact=True).click()
        page.wait_for_selector(".settings-wrap")
        page.locator("#settings-tab-active").click()
        page.get_by_role("button", name="More thresholds").click()
        page.wait_for_selector(".expert-row", timeout=30_000)
        assert not errors, errors
        return f"downloaded {stl_name} ({stl_size} B) and creep_test.stl; overhang, fit, creep and bed grip survived reload"
    finally:
        ctx.close()


def check_new_ui_gizmo(browser, port: int) -> str:
    """The new UI's direction gizmo (T-542): Push in / ±Z by one click (a second click flips the sign), a ring drag
    snaps to 15° with the angle shown, arrow keys nudge, Esc cancels."""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="L-bracket").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup'", timeout=240_000)
        # The upright's inner face of the bracket: x = -25, facing +x.
        page.evaluate("""() => { window.cancelled = false;
          window.g = fdmUI.services.openGizmo([-25, 0, 25], [1, 0, 0], { onCancel: () => { window.cancelled = true; } }); }""")
        dirs = lambda: page.evaluate("() => window.g.direction().map((x) => Math.round(x * 1000) / 1000)")
        assert dirs() == [-1, 0, 0], "Push in should point into the face"
        page.get_by_role("button", name="+Z").click()
        assert dirs() == [0, 0, 1]
        page.get_by_role("button", name="+Z").click()
        assert dirs() == [0, 0, -1], "second click should flip to -Z"
        page.get_by_role("button", name="Push in").click()
        pts = page.evaluate("""async () => {
          const THREE = await import('three');
          const ring = window.g._debug.rings[0].holder, st = fdmUI.services.stage;
          ring.updateMatrixWorld(true);
          const r = st.canvas.getBoundingClientRect();
          return [60, 70, 81].map((deg) => { const a = deg * Math.PI / 180;
            const v = new THREE.Vector3(Math.cos(a), Math.sin(a), 0).applyMatrix4(ring.matrixWorld).project(st.camera);
            return [(v.x + 1) / 2 * r.width + r.left, (1 - v.y) / 2 * r.height + r.top]; });
        }""")
        page.mouse.move(*pts[0])
        page.mouse.down()
        page.mouse.move(*pts[1], steps=3)
        page.mouse.move(*pts[2], steps=3)
        label = page.locator(".gizmo-angle").inner_text()
        tilts = page.evaluate("() => window.g._debug.tilts")
        page.mouse.up()
        assert label == "15°" and tilts == [15, 0], (label, tilts)
        page.keyboard.press("ArrowUp")
        assert page.evaluate("() => window.g._debug.tilts") == [15, 15]
        page.keyboard.press("Escape")
        assert page.locator(".gizmo-row").count() == 0 and page.evaluate("() => window.cancelled"), "Esc didn't cancel"
        assert not errors, errors
        return f"push into the face, ±Z flips, ring drag snapped to {label}, arrow key nudged 15°, Esc cancelled"
    finally:
        page.close()


def check_new_ui_holds(browser, port: int) -> str:
    """The new UI's Holds step (T-541): click a face → radial menu → hold; click the inside corner → hold the corner;
    the holds map to the strength engine's format; "No loads" warns for Balanced and switching to Fast jumps on."""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="L-bracket").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup'", timeout=240_000)
        page.evaluate("() => fdmUI.go('holds')")
        page.wait_for_function("() => fdmUI.services.picker.enabled && fdmUI.services.picker.data", timeout=120_000)
        assert page.get_by_role("button", name="Next").is_disabled(), "Next without a hold"
        box = page.locator(".stage-canvas").bounding_box()
        x, y = box["x"] + box["width"] * 0.55, box["y"] + box["height"] * 0.62
        page.mouse.move(x, y)
        page.wait_for_function("() => fdmUI.services.picker.hit?.kind === 'face'")
        page.mouse.click(x, y)
        page.wait_for_selector(".ui-radial", state="attached")
        page.get_by_role("menuitem", name="Hold this face").click()
        # The inside corner: 3 px off its projected middle, then click.
        cx, cy = page.evaluate("""() => {
          const p = fdmUI.services.picker, line = p.data.edges.find((l) => l.kind === 'corner');
          const c = p.toClient(line.meta.midpoint);
          return [c.x + 3, c.y];
        }""")
        page.mouse.move(cx, cy)
        page.wait_for_function("() => fdmUI.services.picker.hit?.kind === 'corner'", timeout=5_000)
        page.mouse.click(cx, cy)
        page.wait_for_selector(".ui-radial", state="attached")
        page.get_by_role("menuitem", name="Hold inside corner").click()
        holds = page.evaluate("() => fdmUI.store.get().holds.map((h) => [h.kind, h.type])")
        assert holds == [["face", "clamped"], ["corner", "clamped"]], holds
        assert page.locator(".hold-marker").count() == 2
        assert page.get_by_role("button", name="Next").is_enabled()
        loads = page.evaluate("""async () => { const m = await import('./steps/holds.js');
          return fdmUI.store.get().holds.map(m.holdLoad); }""")
        assert loads[0]["kind"] == "fixed" and "region" in loads[0] and loads[1]["edge_kind"] == "concave", loads
        # Undo removes the last hold.
        page.keyboard.press("Control+z")
        assert page.evaluate("() => fdmUI.store.get().holds.length") == 1
        # "No loads" with Balanced asks first; Switch to Fast skips to Analyse.
        page.get_by_role("button", name="No loads, skip strength").click()
        assert "Balanced needs loads" in page.locator("dialog.ui-modal").inner_text()
        page.get_by_role("button", name="Switch to Fast").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Analyse'")
        assert page.evaluate("() => [fdmUI.store.get().goal, fdmUI.store.get().noLoads]") == ["fast", True]
        assert not errors, errors
        return "face and inside-corner holds with markers; engine format ok; undo; Balanced warns, Fast skips to Analyse"
    finally:
        page.close()


def check_new_ui_loads(browser, port: int) -> str:
    """The new UI's Loads and Load details steps (T-543): a load in N (Enter), one in MPa (× face area), MPa off on an
    inside corner, then Load details' three questions and on to Analyse with the loads in the engine's format."""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="L-bracket").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup'", timeout=240_000)
        page.evaluate("() => { fdmUI.store.set({ holds: [{ id: 'h1', region: 0, type: 'screw', name: 'Screw hole', kind: 'hole' }] }); fdmUI.go('loads'); }")
        page.wait_for_function("() => fdmUI.services.picker.enabled && fdmUI.services.picker.data", timeout=120_000)
        assert page.get_by_role("button", name="Next").is_disabled(), "Next without a load"
        box = page.locator(".stage-canvas").bounding_box()

        def click_at(fx, fy):
            x, y = box["x"] + box["width"] * fx, box["y"] + box["height"] * fy
            page.mouse.move(x, y)
            page.wait_for_function("() => fdmUI.services.picker.hit?.kind === 'face'")
            page.mouse.click(x, y)
            page.wait_for_selector(".ui-pop")

        click_at(0.36, 0.42)  # the upright's face
        page.keyboard.type("50")
        assert "kg of weight" in page.locator(".ui-pop .ui-num__hint").inner_text()
        page.keyboard.press("Enter")
        page.wait_for_selector(".ui-pop", state="detached")
        click_at(0.55, 0.62)  # the base's top face
        page.locator(".ui-pop [role=radio]", has_text="MPa").click()
        page.locator(".ui-pop input").fill("0.5")
        assert "MPa ×" in page.locator(".ui-pop .ui-num__hint").inner_text()
        page.get_by_role("button", name="OK").click()
        page.wait_for_selector(".ui-pop", state="detached")
        loads = page.evaluate("() => fdmUI.store.get().loads.map((l) => [Math.round(l.newtons), l.unit])")
        assert loads[0] == [50, "N"] and loads[1][1] == "MPa" and loads[1][0] > 500, loads
        assert page.locator(".load-label").count() == 2
        # MPa needs a face: on the inside corner it says so.
        cx, cy = page.evaluate("""async () => { const THREE = await import('three'); const p = fdmUI.services.picker, st = fdmUI.services.stage;
          const m = p.data.edges.find((l) => l.kind === 'corner').meta.midpoint, o = st.part.offset;
          const v = new THREE.Vector3(m[0] + o.x, m[1] + o.y, m[2] + o.z).project(st.camera); const r = st.canvas.getBoundingClientRect();
          return [(v.x + 1) / 2 * r.width + r.left + 3, (1 - v.y) / 2 * r.height + r.top]; }""")
        page.mouse.move(cx, cy)
        page.wait_for_function("() => fdmUI.services.picker.hit?.kind === 'corner'", timeout=5_000)
        page.mouse.click(cx, cy)
        page.wait_for_selector(".ui-pop")
        page.locator(".ui-pop [role=radio]", has_text="MPa").click()
        assert "Pick a face" in page.locator(".ui-pop .ui-num__hint").inner_text()
        page.keyboard.press("Escape")
        page.wait_for_selector(".ui-pop", state="detached")
        assert page.locator(".gizmo-row").count() == 0
        page.get_by_role("button", name="Next").click()
        assert page.locator("#step-title").inner_text() == "Load details"
        page.get_by_role("radio", name="All the time").click()
        page.get_by_role("button", name="Yes").first.click()
        page.get_by_role("button", name="Next").click()
        assert page.locator("#step-title").inner_text() == "Analyse"
        args = page.evaluate("async () => { const m = await import('./steps/args.js'); const a = m.optimizeArgs(fdmUI.store.get()); return [a.profile.constant_load, a.loads.map((l) => l.kind)]; }")
        assert args == [True, ["fixed", "force", "force"]], args
        assert not errors, errors
        return "50 N by Enter, 0.5 MPa × face area, MPa off on a corner, Esc cancels; Load details → Analyse with engine loads"
    finally:
        page.close()


def check_new_ui_analyse(browser, port: int) -> str:
    """The new UI's Analyse step (T-550): depth cards with estimates, a Quality run lands on Options with a result,
    a changed input marks it stale (Next waits), and a long run shows its stages. T-552: Stop & show best so far comes
    on with the first partial result and lands on Options with those options ("Stopped early" + Continue); Continue
    runs again, which Cancel stops, leaving a working engine; a spent time budget says "Search ended early"."""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="Wall hook").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("() => { fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, goal: 'quality' }); fdmUI.go('analyse'); }")
        page.wait_for_function("() => [...document.querySelectorAll('.ui-cards__meta')].filter((e) => e.textContent.trim()).length >= 2", timeout=60_000)
        assert page.get_by_role("radio", name=re.compile("Complete")).count() == 0, "Complete shown for Quality"
        assert page.get_by_role("button", name="Next").is_disabled(), "Next before a run"
        page.get_by_role("button", name="Start optimization").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Options'", timeout=240_000)
        res = page.evaluate("() => { const r = fdmUI.store.result('optimize'); return [r.goal, r.options.length]; }")
        assert res[0] == "quality" and res[1] >= 1, res
        # A changed input: the result goes stale and Analyse waits for a new run.
        page.evaluate("""async () => {
          const reg = (await fdmUI.services.engine.call('regions')).regions;
          const back = reg.reduce((a, r) => (r.normal[1] < a.normal[1] ? r : a)).id;
          const top = reg.filter((r) => r.normal[2] > 0.99).reduce((a, r) => (r.area_mm2 > a.area_mm2 ? r : a)).id;
          await fdmUI.services.engine.call('set_expert', { values: { 'analysis.strength_voxels': 2500 } });
          fdmUI.store.set({ goal: 'balanced', depth: 'extended', holds: [{ region: back, type: 'clamped' }], loads: [{ region: top, newtons: 49, direction: [0, 0, -1] }] });
          fdmUI.go('analyse'); }""")
        assert page.evaluate("() => fdmUI.store.isStale('optimize')") and page.locator(".analyse-stale").count() == 1
        assert page.get_by_role("button", name="Next").is_disabled(), "Next with a stale result"
        page.get_by_role("button", name="Start optimization").click()
        page.wait_for_selector(".ui-progress")
        stages = page.locator(".ui-progress__stages li").count()
        stop = page.get_by_role("button", name="Stop & show best so far")
        assert stop.is_disabled(), "Stop before any option"
        page.wait_for_function("() => !document.querySelector('.ui-progress__foot .ui-btn--secondary')?.disabled", timeout=300_000)
        stop.click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Options'", timeout=30_000)
        got = page.evaluate("() => { const r = fdmUI.store.result('optimize'); return [r.stopped, r.partial, r.options.length, fdmUI.store.isStale('optimize')]; }")
        assert got[0] and got[1] and got[2] >= 1 and not got[3], got
        assert page.locator(".options-cols .ui-col").count() == got[2]
        assert "Stopped early" in page.locator(".options-banner").inner_text()
        page.get_by_role("button", name="Continue the search").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Analyse'")
        page.wait_for_selector(".ui-progress")
        page.get_by_role("button", name="Cancel").click()
        page.wait_for_selector(".ui-progress", state="detached")
        assert page.locator("#step-title").inner_text() == "Analyse"
        ok = page.evaluate("async () => (await fdmUI.services.engine.call('estimate_runtime', { depth: 'fast' })).seconds > 0")
        assert ok, "engine not usable after Cancel"
        assert page.evaluate("() => fdmUI.store.result('optimize').stopped"), "Cancel lost the kept result"
        # A spent time budget: the engine picks from what it checked, Options says so.
        page.evaluate("""async () => {
          const s = fdmUI.store.get();
          const r = await fdmUI.services.engine.call('optimize', { goal: 'balanced', depth: 'extended', profile: { printer: 'bambu_p1s' },
            loads: null, budget_s: 0 });
          fdmUI.store.setResult('optimize', r, fdmUI.store.deps('model', 'profile', 'goal', 'environment', 'loads', 'depth'));
          fdmUI.go('options'); }""")
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Options'")
        banner = page.locator(".options-banner").inner_text()
        assert "Search ended early" in banner and "Continue the search" in banner, banner
        assert not errors, errors
        return (f"Quality run → Options ({res[1]} option); stale after a change, Next waits; {stages} stages shown; "
                f"Stop showed {got[2]} best so far, Continue, Cancel left a working engine; budget ended early")
    finally:
        page.close()


def show_comparison_row(page, key: str):
    """Open the shared settings when a check needs a collapsed row."""
    row = page.locator(f'.ui-col__cell[data-row="{key}"]')
    if not row.count():
        page.locator(".options-shared").get_by_role("button", name="Show", exact=True).click()
    return row


def check_new_ui_options(browser, port: int) -> str:
    """The new UI's Options step (T-551): aligned columns from a real optimizer run; pin PLA, re-optimize, every new
    column uses PLA; an option below the target warns before it can be used. T-553: walls edited inside a column
    become a chip, that column needs a re-check (no Use), Esc cancels an edit, and re-optimize uses the value."""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="Wall hook").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("""async () => {
          const e = fdmUI.services.engine;
          await e.call('set_expert', { values: { 'analysis.strength_voxels': 2500 } });
          const reg = (await e.call('regions')).regions;
          const back = reg.reduce((a, r) => (r.normal[1] < a.normal[1] ? r : a)).id;
          const top = reg.filter((r) => r.normal[2] > 0.99).reduce((a, r) => (r.area_mm2 > a.area_mm2 ? r : a)).id;
          fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, goal: 'balanced', depth: 'fast',
            holds: [{ region: back, type: 'clamped' }], loads: [{ region: top, newtons: 49, direction: [0, 0, -1] }] });
          const m = await import('./steps/args.js');
          const deps = fdmUI.store.deps('model', 'profile', 'goal', 'environment', 'loads', 'depth');
          fdmUI.store.setResult('optimize', await e.call('optimize', m.optimizeArgs(fdmUI.store.get())), deps);
          fdmUI.go('options'); }""")
        page.wait_for_selector(".ui-col")
        n = page.locator(".ui-col").count()
        assert n >= 2, f"{n} columns"
        page.set_viewport_size({"width": 1920, "height": 1080})
        assert page.evaluate("""() => {
          const stage = document.querySelector('.stage').getBoundingClientRect();
          const panel = document.querySelector('.compare .panel-body');
          return Math.abs(stage.right - panel.getBoundingClientRect().left) < 1
            && panel.scrollHeight <= panel.clientHeight && panel.scrollWidth <= panel.clientWidth;
        }"""), "The Options comparison should sit beside the stage without scrolling at 1920×1080"
        assert page.get_by_role("button", name="Keep all", exact=True).count() == n
        assert page.get_by_role("button", name="Use this option", exact=True).count() == n
        page.screenshot(path=str(OUT / "options_1920x1080.png"))
        theme = page.locator(".app-header").get_by_role("button", name="Dark theme")
        theme.click()
        page.screenshot(path=str(OUT / "options_dark_1920x1080.png"), animations="disabled")
        theme.click()
        page.set_viewport_size({"width": 1280, "height": 720})
        shared = page.locator(".options-shared")
        assert "Same for all:" in shared.inner_text()
        before = page.locator(".ui-col").first.locator(".ui-col__cell").count()
        shared.get_by_role("button", name="Show", exact=True).click()
        assert page.locator(".ui-col").first.locator(".ui-col__cell").count() > before
        assert page.evaluate("""() => {
          const columns = [...document.querySelectorAll('.ui-col')];
          const rows = columns.map((col) => [...col.querySelectorAll('.ui-col__cell')]
            .map((cell) => cell.getBoundingClientRect().top).join(','));
          return rows.every((row) => row === rows[0]);
        }"""), "Comparison rows are misaligned"
        shared.get_by_role("button", name="Hide", exact=True).click()
        assert page.locator(".ui-col").first.locator(".ui-col__cell").count() == before
        mats = page.evaluate("() => fdmUI.store.result('optimize').options.map((o) => o.material)")
        i = mats.index("pla") if "pla" in mats else 0
        cell = show_comparison_row(page, "material").nth(i)
        cell.get_by_role("button", name=re.compile("^Change Material")).click()
        cell.get_by_role("button", name="Material", exact=True).click()
        assert cell.get_by_role("listbox", name="Material").is_visible()
        page.keyboard.press("Escape")
        cell.get_by_role("button", name="Cancel", exact=True).click()
        assert cell.get_by_role("button", name=re.compile("^Change Material")).is_visible()
        cell.hover()
        cell.locator(".ui-col__pin").click()
        assert "Material:" in page.locator(".ui-constraints").inner_text()
        pinned = page.evaluate("() => fdmUI.store.get().constraints.material")
        page.get_by_role("button", name="Re-optimize with my choices").click()
        page.wait_for_function("(m) => fdmUI.store.result('optimize').options.every((o) => o.material === m)", arg=pinned, timeout=180_000)
        after = page.evaluate("() => fdmUI.store.result('optimize').options.map((o) => o.material)")
        # Edit walls in the last column: a chip, the column needs a re-check; re-optimize uses the value.
        col = page.locator(".ui-col").last
        walls_now = page.evaluate("() => fdmUI.store.result('optimize').options.at(-1).walls")
        want = 6 if walls_now != 6 else 5
        show_comparison_row(page, "walls")
        col.get_by_role("button", name=re.compile("^Change Walls")).click()
        page.keyboard.press("Escape")
        assert col.locator(".ui-col__cell.is-editing").count() == 0, "Esc didn't cancel"
        col.get_by_role("button", name=re.compile("^Change Walls")).click()
        field = col.locator(".ui-col__cell.is-editing input")
        field.fill(str(want))
        field.press("Enter")
        assert page.evaluate("() => fdmUI.store.get().constraints.walls") == want
        assert f"Walls: {want}" in page.locator(".ui-constraints").inner_text()
        assert "Needs a re-check" in col.inner_text() and col.get_by_role("button", name="Use this option").count() == 0
        assert col.locator(".ui-col__value.is-edited").inner_text() == str(want)
        assert page.locator(".ui-col").first.get_by_role("button", name="Use this option").count() == 1, "other columns stay usable"
        page.get_by_role("button", name="Re-optimize with my choices").click()
        page.wait_for_function("(w) => fdmUI.store.result('optimize').options.every((o) => o.walls === w)", arg=want, timeout=180_000)
        assert page.locator(".ui-col__value.is-edited").count() == 0 and "Needs a re-check" not in page.locator(".options-cols").inner_text()
        # Below the target: a clear warning first, and the warning travels with the option.
        page.evaluate("() => { const r = fdmUI.store.result('optimize'); r.options[0] = { ...r.options[0], meets_target: false, safety: 1.2, target: 3 }; fdmUI.services.rerender(); }")
        page.get_by_role("button", name="Use this option").first.click()
        assert "Below the safety target" in page.locator("dialog.ui-modal").inner_text()
        page.get_by_role("button", name="Use it anyway").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Simulation'")
        assert page.evaluate("() => fdmUI.store.get().option.warning") == "below_target"
        assert not errors, errors
        return f"{n} aligned columns; pinned {pinned}, re-optimized: {after}; walls edited to {want} in a column, re-optimized with it; below-target warns and is carried"
    finally:
        page.close()


def check_new_ui_simulation(browser, port: int) -> str:
    """The new UI's Simulation step (T-560): the chosen option is sliced in the background (browser OrcaSlicer when
    it's there, else our own slicer, said so), Run simulations gives the status lines, the overlay switch shows heat,
    warping (true size and exaggerated), internal stress and overhangs, Next names the problems, leaving clears the view."""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="Wall hook").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("""() => {
          fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, goal: 'fast', optimized: 1,
            option: { up: [0, 0, 1], orientation: 'Flat', material: 'pla', material_name: 'PLA', nozzle: '0.4', layer_height: 0.2,
              walls: 3, top: 4, bottom: 3, infill: 0.15, safety: 2.4, target: 2,
              settings: { wall_loops: '3', top_shell_layers: '4', bottom_shell_layers: '3', sparse_infill_density: '15%', layer_height: '0.20' } } });
          fdmUI.go('sim'); }""")
        assert "Flat · PLA · 0.4 mm · 3 walls · 15 % infill" in page.locator(".sim-setup").inner_text()
        assert page.get_by_role("button", name="Next").count() == 1, "Next before a run (skipping is allowed)"
        page.wait_for_function("() => !/Slicing|Getting ready/.test(document.querySelector('.sim-slice__text')?.textContent ?? 'Slicing')", timeout=180_000)
        sliced = page.locator(".sim-slice__text").inner_text()
        page.get_by_role("button", name="Run simulations").click()
        page.wait_for_selector(".sim-status", timeout=300_000)
        status = page.locator(".sim-status").inner_text().replace("\n", " ")
        basis = page.locator(".sim-status + .ui-hint").inner_text()
        if "OrcaSlicer in" in sliced:
            assert "G-code from OrcaSlicer" in basis, basis
        seen = []
        for name in ("Heat", "Warping", "Internal stress", "Overhangs"):
            page.locator(".sim-views").get_by_role("radio", name=name).click()
            seen.append(page.evaluate("() => Boolean(fdmUI.services.stage.field)"))
            if name == "Warping":
                page.locator(".sim-views").get_by_role("radio", name="Exaggerated").click()
                assert "exaggerated" in page.locator(".sim-legend").inner_text()
        assert seen == [False, True, True, False], seen
        painted = page.evaluate("() => { const c = fdmUI.services.stage.part.geo.attributes.color.array; return new Set(Array.from(c.slice(0, 30000), (v) => v.toFixed(2))).size > 3; }")
        nxt = page.locator(".panel-foot .ui-btn--primary").inner_text()
        assert re.match(r"Review \d+ problems?|All clear: Done", nxt), nxt
        page.locator(".panel-foot .ui-btn--primary").click()
        assert page.evaluate("() => fdmUI.services.stage.field === null || fdmUI.services.stage.field === undefined")
        assert not errors, errors
        return f"{sliced} {status}; overlays shown (overhang paint: {painted}); footer '{nxt}', leaving cleared the view"
    finally:
        page.close()


def check_new_ui_heat(browser, port: int) -> str:
    """The heat model in the new UI's Simulation step (T-478): a real OrcaSlicer G-code of the test fixture, picked as
    "Have a sliced file?", goes through Run simulations. The status list has the cooling plan, its summary counts the
    spots fixed, the window map turns red spots green between As sliced and With the plan, a clicked spot says why,
    the layer slider cuts the view, "Optimize my G-code directly" downloads a checked copy, and another view clears it."""
    import gzip

    fixture = ROOT / "core" / "tests" / "gcode"
    OUT.mkdir(exist_ok=True)
    gcode = OUT / "heat_fixture.gcode"
    gcode.write_bytes(gzip.decompress((fixture / "orca.gcode.gz").read_bytes()))
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.evaluate("""async () => {
          const bytes = new Uint8Array(await (await fetch('../core/tests/gcode/part.stl')).arrayBuffer());
          await fdmUI.services.loadPart('part.stl', bytes); }""")
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("""() => {
          fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, noLoads: true,
            option: { up: [0, 0, 1], orientation: 'Flat', material: 'pla', material_name: 'PLA', nozzle: '0.4', layer_height: 0.2, walls: 2, infill: 0.15 } });
          fdmUI.go('sim'); }""")
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Simulation'")
        page.set_input_files(".sim-slice input[type=file]", str(gcode))
        page.wait_for_selector("text=Using your sliced file heat_fixture.gcode")
        page.get_by_role("button", name="Run simulations").click()
        page.wait_for_selector(".heat-summary", timeout=600_000)
        status = page.locator(".sim-status").inner_text()
        outside = re.search(r"(\d+) spots outside their window, (\d+) with the plan", status)
        assert "Cooling plan" in status and outside and int(outside[1]) > int(outside[2]), status
        summary = page.locator(".heat-summary").inner_text()
        hot_spots = re.search(r"Too hot: (\d+) spots before, (\d+) after", summary)
        assert hot_spots and int(hot_spots[1]) > int(hot_spots[2]) and "Rough" in summary, summary
        page.locator(".sim-views").get_by_role("radio", name="Heat").click()
        page.wait_for_selector(".heat-slider")
        # The sliced paths stand in for the part (T-587); the plan leaves fewer red beads (instance colours).
        assert page.evaluate("() => fdmUI.services.heatView.paths.mesh.count > 1000 && !fdmUI.services.stage.part.group.visible")
        # The replay comes first (T-588): what we found (the hot pin's layers) and what we change, then a jump to a layer.
        story = page.locator("aside.panel").inner_text()
        assert "1 · Watch it print" in story and "2 · What we found" in story and "3 · What we change" in story, story
        assert "spots still too hot" in story or "spot still too hot" in story, story
        page.locator(".heat-found").get_by_role("button", name="Show").first.click()
        jumped = page.locator(".heat-slider").input_value()
        assert int(jumped) > 0 and "into the print" in page.locator(".heat-play + .ui-hint").inner_text()
        colours = "() => { const m = fdmUI.services.heatView.paths.mesh; return Array.from(m.instanceColor.array.slice(0, m.count * 3), (v) => v.toFixed(3)).join(); }"
        sliced = page.evaluate(colours)
        page.get_by_role("radio", name="With the plan").click()
        assert page.evaluate(colours) != sliced, "the plan's replay is coloured by its own temperatures"
        page.screenshot(path=str(OUT / "new-ui-heat-replay.png"))
        page.locator(".heat-slider").evaluate("(e) => { e.value = e.max; e.dispatchEvent(new Event('input')); }")  # all layers again
        page.get_by_role("radio", name="Too hot / too cold").click()
        hot = """() => { const hv = fdmUI.services.heatView, m = hv.paths.mesh, c = m.instanceColor.array;
          let n = 0;  // red: much more red than green and blue (colours are linear here)
          for (let i = 0; i < m.count; i++) if (c[i * 3] > 0.4 && c[i * 3 + 1] < 0.1 && c[i * 3 + 2] < 0.1) n++; return n; }"""
        page.get_by_role("radio", name="As sliced").click()
        red_before = page.evaluate(hot)
        page.get_by_role("radio", name="With the plan").click()
        red_after = page.evaluate(hot)
        assert red_before > red_after, (red_before, red_after)
        # Click a spot that was too hot: why, and what the plan did.
        page.evaluate("""async () => { const m = await import('./steps/heatlogic.js');
          const d = m.decode(fdmUI.store.result('simulate').extra.find((x) => x.name === 'heat').value.result);
          fdmUI.services.heatView.onPick(d.before.window.findIndex((w) => w === 1)); }""")
        why = page.locator(".heat-why").inner_text()
        assert "As sliced, the next layer covered it" in why and "So the plan changes this layer" in why, why
        # The layer slider shows the print up to a layer.
        total = page.evaluate("() => fdmUI.services.heatView.paths.mesh.count")
        page.locator(".heat-slider").fill("10")
        cut = page.evaluate("() => fdmUI.services.heatView.paths.mesh.count")
        assert 0 < cut < total, (cut, total)
        page.screenshot(path=str(OUT / "new-ui-heat-heat.png"))
        page.get_by_role("radio", name="Speed & fan changes").click()
        assert page.evaluate("() => fdmUI.services.heatView.marks?.mesh.visible ?? true")
        page.screenshot(path=str(OUT / "new-ui-heat-gcode.png"))
        charts = page.locator(".heat-chart").count()
        with page.expect_download(timeout=600_000) as dl:
            page.get_by_role("button", name="Download cooling-optimized G-code").click()
        name = dl.value.suggested_filename
        assert name == "part_cooled.gcode", name
        page.wait_for_selector(".heat-ok")
        # Another view takes the heat overlay away; leaving the step too.
        page.locator(".sim-views").get_by_role("radio", name="Overhangs").click()
        assert page.evaluate("() => !fdmUI.services.heatView.paths && fdmUI.services.stage.part.group.visible")
        assert not errors, errors
        return f"{hot_spots[1]} hot spots → {hot_spots[2]}; red on the stage {red_before} → {red_after}; slider {total} → {cut} beads; {charts} charts; downloaded {name}"
    finally:
        page.close()


def check_new_ui_quality(browser, port: int) -> str:
    """The new UI's Quality path (T-563, plan §14): no Holds / Loads / Load details in the rail, Analyse offers Fast and
    Extended only and tints the faces people see (our guess); a click marks a show face, which the optimizer gets;
    Options shows no Strength row."""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="Phone stand").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("() => { fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, goal: 'quality' }); fdmUI.go('analyse'); }")
        rail = page.locator(".ui-rail").inner_text()
        assert not any(w in rail for w in ("Holds", "Loads", "Load details")), rail
        assert page.get_by_role("radio", name=re.compile("Complete")).count() == 0
        page.wait_for_selector(".showfaces")
        assert "We assume the tinted outer faces" in page.locator(".showfaces").inner_text()
        page.wait_for_function("() => fdmUI.services.picker.colored?.size > 0", timeout=120_000)
        guessed = page.evaluate("() => fdmUI.services.picker.colored.size")
        box = page.locator(".stage-canvas").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_function("() => fdmUI.services.picker.hit != null", timeout=10_000)
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_function("() => fdmUI.store.get().showFaces.length === 1")
        assert "1 show face marked" in page.locator(".showfaces").inner_text()
        marked = page.evaluate("() => fdmUI.store.get().showFaces")
        args = page.evaluate("async () => (await import('./steps/args.js')).optimizeArgs(fdmUI.store.get()).faces")
        assert args == {"clean": marked}, args
        page.get_by_role("radio", name=re.compile("Fast")).click()
        page.get_by_role("button", name="Start optimization").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Options'", timeout=240_000)
        page.wait_for_selector(".ui-col")
        show_comparison_row(page, "time")
        labels = page.locator(".options-labels").inner_text()
        assert "Safety" not in labels and "Print time" in labels, labels
        assert page.evaluate("() => fdmUI.services.picker.colored?.size ?? 0") == 0, "Analyse's tint stayed on the part"
        assert not errors, errors
        return f"rail without holds/loads; Fast/Extended; {guessed} faces guessed as seen, show face {marked} marked and sent; Options without Strength"
    finally:
        page.close()


def check_new_ui_problems(browser, port: int) -> str:
    """The new UI's Problems & fixes step (T-561): grouped rows; opening one frames it and shows its fixes; a teardrop
    (design, with the one-time "changes your model" confirm) and a slicer fix go into one pending batch with a ghost;
    Confirm all changes applies them, carries the hold and load over, re-checks (strength too) and shows the result;
    Undo batch puts the part back."""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="Hole block").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("""async () => {
          const e = fdmUI.services.engine;
          const reg = (await e.call('regions')).regions;
          const bottom = reg.reduce((a, r) => (r.normal[2] < a.normal[2] ? r : a)).id;
          const top = reg.reduce((a, r) => (r.normal[2] > a.normal[2] ? r : a)).id;
          fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, goal: 'balanced', optimized: 1,
            holds: [{ id: 'h1', region: bottom, type: 'clamped' }], loads: [{ id: 'l1', region: top, newtons: 100, direction: [0, 0, -1] }],
            option: { up: [0, 0, 1], orientation: 'Flat', material: 'pla', material_name: 'PLA', nozzle: '0.4', layer_height: 0.2,
              walls: 3, top: 4, bottom: 3, infill: 0.15, safety: 20, target: 3,
              settings: { wall_loops: '3', top_shell_layers: '4', bottom_shell_layers: '3', sparse_infill_density: '15%', layer_height: '0.20' } } });
          fdmUI.go('problems'); }""")
        page.wait_for_selector(".prob-list .ui-find", timeout=120_000)
        groups = page.locator(".prob-group h3").all_inner_texts()
        rows = page.locator(".ui-find").count()
        row = page.locator(".ui-find", has_text="Side hole prints oval").first
        cam = page.evaluate("() => fdmUI.services.stage.camera.position.toArray()")
        row.locator(".ui-find__head").click()
        moved = page.evaluate("(c) => fdmUI.services.stage.camera.position.toArray().some((v, i) => Math.abs(v - c[i]) > 0.5)", cam)
        assert moved, "opening the row didn't frame the hole"
        tags = row.locator(".ui-tag").all_inner_texts()
        assert tags[0] == "Slicer" and "Design" in tags, tags
        row.locator(".ui-fix", has_text="Teardrop").get_by_role("button", name="Add").click()
        assert "changes your model" in page.locator("dialog.ui-modal").inner_text().lower()
        page.locator("dialog.ui-modal").get_by_role("button", name="Add").click()
        row = page.locator(".ui-find", has_text="Side hole prints oval").first
        if row.locator(".ui-find__body").is_hidden():
            row.locator(".ui-find__head").click()
        row.locator(".ui-fix", has_text="bridge").get_by_role("button", name="Add").click()
        page.wait_for_selector(".ui-pending")
        bar = page.locator(".ui-pending").inner_text()
        assert "2 changes pending" in bar, bar
        assert page.evaluate("() => fdmUI.services.stage.ghosts.length") > 0, "no ghost for the teardrop"
        vol0 = page.evaluate("() => fdmUI.store.get().part.volume")
        page.get_by_role("button", name="Confirm all changes").click()
        page.wait_for_selector(".prob-batch", timeout=240_000)
        card = page.locator(".prob-batch").inner_text().replace("\n", " ")
        assert "Re-checked:" in card and "Strength: ×" in card, card
        s = page.evaluate("""() => { const s = fdmUI.store.get(); return { name: s.part.name, volume: s.part.volume, holds: s.holds.length,
          loads: s.loads.length, bridge: s.option.settings.bridge_speed, pending: s.pending.length, design: s.design.length }; }""")
        assert s["name"].endswith("_dfm.stl") and s["volume"] < vol0, s
        assert s["holds"] == 1 and s["loads"] == 1 and s["bridge"] and s["pending"] == 0 and s["design"] == 1, s
        assert page.evaluate("() => fdmUI.services.stage.ghosts.length") == 0
        page.get_by_role("button", name="Undo these changes").click()
        page.wait_for_function("(v) => Math.abs(fdmUI.store.get().part.volume - v) < 0.01", arg=vol0, timeout=60_000)
        assert page.evaluate("() => fdmUI.store.get().option.settings.bridge_speed") is None
        nxt = page.locator(".panel-foot .ui-btn--primary").inner_text()
        assert nxt == "Continue to Done", nxt
        assert not errors, errors
        return f"groups {groups}, {rows} rows, framed: {moved}; teardrop + bridge fix → {card[:120]}; undo restored the part"
    finally:
        page.close()


def check_new_ui_done(browser, port: int) -> str:
    """The new UI's Done step (T-562): summary of the chosen option, slicer changes with ticks (an unticked one stays out
    of the export), Export 3MF for the remembered slicer, Save project, Share report, Download G-code from the browser
    OrcaSlicer, and the header's Export 3MF saying what goes out."""
    import io
    import json
    import zipfile

    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="Hole block").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("""() => {
          fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, goal: 'fast', optimized: 1, noLoads: true,
            option: { up: [0, 0, 1], orientation: 'Flat', material: 'pla', material_name: 'PLA', nozzle: '0.4', layer_height: 0.2,
              walls: 3, top: 4, bottom: 3, infill: 0.15, minutes: 41, part_g: 12, total_g: 12, support_g: 0,
              settings: { wall_loops: '3', top_shell_layers: '4', bottom_shell_layers: '3', sparse_infill_density: '15%', layer_height: '0.20' } } });
          fdmUI.go('done'); }""")
        page.wait_for_selector(".done-grid")
        grid = " ".join(page.locator(".done-grid").inner_text().split())
        assert "PLA" in grid and "41 min" in grid, grid
        page.wait_for_selector(".done-rows .ui-disc__head", timeout=120_000)
        page.locator(".done-rows .ui-disc__head").click()
        labels = page.locator(".done-settings li").all_inner_texts()
        assert any("Thin layers" in x for x in labels), labels
        row = page.locator(".done-settings li", has_text="ironing")
        row.locator("input[type=checkbox]").uncheck()
        assert page.evaluate("() => fdmUI.store.get().exportOff") == ["ironing_type"]
        page.locator(".done-export-row").get_by_role("radio", name="PrusaSlicer").click()
        with page.expect_download(timeout=120_000) as dl:
            page.locator(".done-export-row").get_by_role("button", name="Export 3MF").click()
        mf = zipfile.ZipFile(io.BytesIO(open(dl.value.path(), "rb").read()))
        config = " ".join(mf.read(n).decode("utf-8", "replace") for n in mf.namelist() if n.lower().endswith((".config", ".ini")))
        assert dl.value.suggested_filename == "hole_block.3mf", dl.value.suggested_filename
        assert "elefant_foot_compensation" in config and "ironing_type" not in config, config[:400]
        assert page.evaluate("() => localStorage.getItem('fdm-target-slicer')") == "prusa"
        with page.expect_download(timeout=120_000) as dl:
            page.get_by_role("button", name="Save project").click()
        project = json.loads(open(dl.value.path(), encoding="utf-8").read())
        assert project["format"] == "fdm-check-project" and project["up"] == [0, 0, 1] and "ironing_type" in project["excluded_settings"], project.keys()
        with page.expect_download(timeout=120_000) as dl:
            page.get_by_role("button", name="Share report").click()
        report = open(dl.value.path(), encoding="utf-8").read()
        assert "Print check: hole_block.stl" in report and "fdm-check-project" in report
        # The browser OrcaSlicer is there only after tools/vendor.py (local runs, the Pages build), not in CI.
        if any((ROOT / "vendor").glob("muon3d-slicer-engine@*/engine-st.mjs")):
            with page.expect_download(timeout=300_000) as dl:
                page.get_by_role("button", name="Download G-code").click()
            gcode = open(dl.value.path(), "rb").read()
            assert dl.value.suggested_filename == "hole_block.gcode" and b"G1 " in gcode, len(gcode)
            got = f"G-code ({len(gcode) // 1024} kB)"
        else:
            page.get_by_role("button", name="Download G-code").click()
            page.wait_for_selector(".drop-error", timeout=120_000)
            got = page.locator(".drop-error").inner_text()
            assert "isn't available" in got, got
        page.locator(".app-header").get_by_role("button", name="Export 3MF").click()
        what = page.locator("dialog.ui-modal").inner_text()
        assert "Chosen option: Flat" in what, what
        with page.expect_download(timeout=120_000) as dl:
            page.locator("dialog.ui-modal").get_by_role("button", name="Export 3MF").click()
        assert dl.value.suggested_filename.endswith(".3mf")
        assert not errors, errors
        return f"{grid}; {len(labels)} slicer changes, unticked ironing stays out; 3MF for PrusaSlicer remembered; project, report, {got}; header export"
    finally:
        page.close()


def check_new_ui_tolerances(browser, port: int) -> str:
    """Tolerances & fits (T-570), from Done's card: the electronics box's round holes and pegs are listed; heat-set
    inserts in all four screw holes (a ghost goes in, the hole is resized) go into Problems' batch through the
    one-time "changes your model" confirm; a snug fit on a peg with a Ø7 hole jams (red), and the printed-size overlay
    shows; a peg clicked on the part selects it; Problems confirms the batch."""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="Electronics box").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("""() => { fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, goal: 'fast', optimized: 1, noLoads: true,
          option: { up: [0, 0, 1], orientation: 'As loaded', material: 'pla', material_name: 'PLA', nozzle: '0.4', layer_height: 0.2, walls: 2, top: 4, bottom: 3,
            infill: 0.15, settings: { wall_loops: '2' } } }); fdmUI.go('done'); }""")
        page.get_by_role("button", name="Check the fits").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Tolerances & fits'")
        page.wait_for_selector(".tol-row", timeout=120_000)
        rows = page.locator(".tol-row").all_inner_texts()
        assert any("4 × Ø2.8 mm holes" in r for r in rows) and any("4 × Ø7 mm pegs" in r for r in rows), rows
        assert "Tolerances & fits" in page.locator(".ui-rail").inner_text()
        # Inserts in the screw holes.
        page.locator(".tol-row", has_text="Ø2.8 mm holes").click()
        page.get_by_role("radio", name="Screw / insert").click()
        page.get_by_role("radio", name="Heat-set insert").click()
        page.wait_for_function("() => /Model this hole Ø4/.test(document.querySelector('.tol-verdict')?.textContent ?? '')", timeout=60_000)
        assert page.evaluate("() => fdmUI.services.stage.part.group.getObjectByName('tol-mate') != null"), "no ghost"
        page.get_by_role("button", name="Change the model").click()
        assert "changes your model" in page.locator("dialog.ui-modal").inner_text().lower()
        page.locator("dialog.ui-modal").get_by_role("button", name="Add").click()
        page.wait_for_function("() => fdmUI.store.get().pending.some((p) => p.id.startsWith('tol:'))", timeout=120_000)
        item = page.evaluate("() => { const p = fdmUI.store.get().pending.find((p) => p.id.startsWith('tol:')); return [p.title, p.edits.length]; }")
        assert "M3" in item[0] and "4 the same" in item[0] and item[1] >= 4, item
        # A snug sleeve on a Ø7 peg: no gap left, it jams.
        page.locator(".tol-row", has_text="Ø7 mm pegs").click()
        page.get_by_role("radio", name="Snug").click()
        verdict = page.locator(".tol-verdict").inner_text()
        assert verdict.startswith("Will jam") and "typical PLA" in verdict, verdict
        page.locator(".sim-views").get_by_role("radio", name="×10").click()
        circles = page.evaluate("() => fdmUI.services.stage.part.group.getObjectByName('tol-printed').children.length")
        assert circles == 2 * 8, circles  # a grey (modelled) and an orange (printed) circle per hole and peg
        # Clicking a peg on the part selects it (here: back to the holes first, then a peg's top).
        page.locator(".tol-row", has_text="Ø2.8 mm holes").click()
        # A cylinder's centroid is on its axis, inside the peg: aim at the middle of its triangle that faces the camera most.
        spots = page.evaluate("""() => { const p = fdmUI.services.picker, st = fdmUI.services.stage, pos = st.partTriangles();
          const cam = st.camera.position.clone().sub(st.controls.target).normalize();
          return [1, 9, 13, 17].map((r) => { let best = null, bestDot = 0.3;
            for (const t of p.trianglesOf(r)) { const v = pos.subarray(t * 9, t * 9 + 9);
              const a = [v[3] - v[0], v[4] - v[1], v[5] - v[2]], b = [v[6] - v[0], v[7] - v[1], v[8] - v[2]];
              const n = [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]], l = Math.hypot(...n) || 1;
              const d = (n[0] * cam.x + n[1] * cam.y + n[2] * cam.z) / l;
              if (d > bestDot) { bestDot = d; best = [(v[0] + v[3] + v[6]) / 3, (v[1] + v[4] + v[7]) / 3, (v[2] + v[5] + v[8]) / 3]; } }
            if (!best) return null; const c = p.toClient(best); return [c.x, c.y, r]; }).filter(Boolean); }""")
        for x, y, region in spots:
            page.mouse.move(x, y)
            try:
                page.wait_for_function("(r) => fdmUI.services.picker.hit?.region === r", arg=region, timeout=3_000)
                break
            except Exception:  # noqa: BLE001 - this peg is behind a wall from here: the next one
                continue
        else:
            raise AssertionError(f"no peg to click among {spots}")
        page.mouse.click(x, y)
        page.wait_for_function("() => /Ø7 mm pegs/.test(document.querySelector('.tol-row.is-selected')?.textContent ?? '')", timeout=10_000)
        hit = page.locator(".tol-row.is-selected b").inner_text()
        assert "Ø7 mm pegs" in hit, hit
        # Problems confirms the batch with the inserts.
        page.get_by_role("button", name="Review changes").first.click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Problems'", timeout=60_000)
        vol0 = page.evaluate("() => fdmUI.store.get().part.volume")
        page.get_by_role("button", name="Confirm all changes").click()
        page.wait_for_selector(".prob-batch", timeout=300_000)
        vol1 = page.evaluate("() => fdmUI.store.get().part.volume")
        assert vol1 != vol0 and page.evaluate("() => fdmUI.store.get().design.length") == 1, (vol0, vol1)
        assert page.evaluate("() => fdmUI.services.stage.part.group.getObjectByName('tol-mate')") is None
        assert not errors, errors
        return f"rows {rows}; M3 inserts ×4 added and confirmed (volume {vol0:.0f} → {vol1:.0f} mm³); snug Ø7 peg: {verdict[:40]}; ×10 overlay; clicked on the part: {hit}"
    finally:
        page.close()


def check_new_ui_lighter_tuned(browser, port: int) -> str:
    """Make it lighter and Tuned settings (T-571), from Done's cards. Lighter: settings that still reach the option's
    target sit beside it and are used after a confirm; the lighter-shape guide shows on the part and downloads.
    Tuned: from a sliced G-code (the test fixture, as "Have a sliced file?"), the cooling plan's slicer part goes into
    the option and the 3MF."""
    import gzip

    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    done = []
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="Hole block").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("""async () => {
          const reg = (await fdmUI.services.engine.call('regions')).regions;
          const bottom = reg.reduce((a, r) => (r.normal[2] < a.normal[2] ? r : a)).id;
          const top = reg.reduce((a, r) => (r.normal[2] > a.normal[2] ? r : a)).id;
          fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, goal: 'balanced', optimized: 1,
            holds: [{ id: 'h1', region: bottom, type: 'clamped' }], loads: [{ id: 'l1', region: top, newtons: 100, direction: [0, 0, -1] }],
            option: { up: [0, 0, 1], orientation: 'Flat', material: 'pla', material_name: 'PLA', nozzle: '0.4', layer_height: 0.2,
              walls: 4, top: 5, bottom: 4, infill: 0.4, safety: 30, target: 3, settings: { wall_loops: '4', sparse_infill_density: '40%' } } });
          fdmUI.go('done'); }""")
        page.get_by_role("button", name="Find lighter settings").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Make it lighter'")
        page.get_by_role("button", name="Find lighter settings").click()
        page.wait_for_selector(".light-col.is-way", timeout=300_000)
        cols = page.locator(".light-col").count()
        page.get_by_role("button", name="Use these").first.click()
        assert "Strength" in page.locator("dialog.ui-modal").inner_text()
        page.locator("dialog.ui-modal").get_by_role("button", name="Use these").click()
        o = page.evaluate("() => { const o = fdmUI.store.get().option; return [o.lighter, o.walls, o.infill, o.safety, o.target]; }")
        assert o[0] and (o[1] < 4 or o[2] < 0.4) and o[3] >= o[4], o
        done.append(f"lighter: walls {o[1]}, infill {round(o[2] * 100)} %, ×{o[3]} ({cols - 1} ways)")
        page.get_by_role("button", name="Show the lighter shape").click()
        page.get_by_role("button", name="Download the guide (STL)").wait_for(timeout=300_000)  # the shape takes a while late in a long run
        with page.expect_download(timeout=300_000) as dl:
            page.get_by_role("button", name="Download the guide (STL)").click()
        assert dl.value.suggested_filename == "hole_block_lighter_guide.stl"
        assert page.evaluate("() => fdmUI.services.stage.ghosts.length") == 1
        page.locator(".panel-foot").get_by_role("button", name="Back").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Done'")
        assert "Lighter settings in use" in page.locator(".done-card", has_text="Want it even lighter").inner_text()
        assert page.evaluate("() => fdmUI.services.stage.ghosts.length") == 0
        done.append("shape guide shown and downloaded")
        # Tuned settings need a G-code: without one they say so; with the fixture's, the cooling plan goes in.
        page.get_by_role("button", name="Slower hot layers").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Tuned settings'")
        assert "needs the G-code" in page.locator(".panel").inner_text()
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        part = ROOT / "core" / "tests" / "gcode"
        page.set_input_files(".drop-wrap input[type=file]", str(part / "part.stl"))
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("""() => { fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, goal: 'fast', optimized: 1, noLoads: true,
          option: { up: [0, 0, 1], orientation: 'Flat', material: 'pla', material_name: 'PLA', nozzle: '0.4', layer_height: 0.2, walls: 2, top: 4, bottom: 3,
            infill: 0.15, settings: { wall_loops: '2' } } }); fdmUI.go('sim'); }""")
        page.set_input_files(".sim-slice input[type=file]", files=[{"name": "part.gcode", "mimeType": "text/plain",
                                                                   "buffer": gzip.decompress((part / "orca.gcode.gz").read_bytes())}])
        page.get_by_role("button", name="Run simulations").click()
        page.wait_for_selector(".sim-status", timeout=300_000)
        page.evaluate("() => fdmUI.go('tuned')")
        page.get_by_role("button", name="Work out slower layers").click()
        page.wait_for_selector(".tuned-summary", timeout=300_000)
        lines = page.locator(".tuned-ranges li").all_inner_texts()
        assert any("cooling tower" in x for x in lines), lines
        page.get_by_role("button", name="Put these in the 3MF").click()
        assert page.evaluate("() => fdmUI.store.get().option.cooling?.tower?.height") > 0
        page.locator(".panel-foot").get_by_role("button", name="Back").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Done'")
        assert "cooling plan goes into the 3MF" in page.locator(".done-card", has_text="Tuned settings").inner_text()
        with page.expect_download(timeout=120_000) as dl:
            page.locator(".done-export-row").get_by_role("button", name="Export 3MF").click()
        assert dl.value.suggested_filename.endswith(".3mf")
        done.append(f"tuned: {lines[0][:40]} into the 3MF")
        assert not errors, errors
        return "; ".join(done)
    finally:
        page.close()


def _bar_stl(x: float, y: float, z: float) -> bytes:
    """A plain box as binary STL (for a part too big for the bed)."""
    import struct

    v = [(a * x, b * y, c * z) for a in (0, 1) for b in (0, 1) for c in (0, 1)]
    faces = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4), (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    out = bytearray(80) + struct.pack("<I", len(faces))
    for f in faces:
        out += struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *v[i]) for i in f) + b"\0\0"
    return bytes(out)


def check_new_ui_tools(browser, port: int) -> str:
    """Done's More tools (T-564). On the electronics box: a stronger zone and a hidden gap from clicks (ghosts, kept for
    the 3MF), copies on the plate (the bed view, Done says so, the 3MF has them), the box heat (verdict and the walls
    coloured) and two versions compared. Then a 300 mm bar: Problems' "Too big" offers Split it, which opens the split
    tool; the pieces are cut, shown apart and downloaded with the pins."""
    import io
    import zipfile

    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    done = []
    title = "() => document.getElementById('step-title')?.textContent"
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="Electronics box").click()
        page.wait_for_function(f"() => ({title})() === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("""() => { fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, goal: 'fast', optimized: 1, noLoads: true,
          option: { up: [0, 0, 1], orientation: 'As loaded', material: 'pla', material_name: 'PLA', nozzle: '0.4', layer_height: 0.2, walls: 2, top: 4, bottom: 3,
            infill: 0.15, minutes: 60, total_g: 20, settings: { wall_loops: '2' } } }); fdmUI.go('done'); }""")
        page.get_by_role("button", name="More tools").click()
        page.locator(".done-tools li", has_text="Zones and hidden gaps").get_by_role("button", name="Open").click()
        page.wait_for_function(f"() => ({title})() === 'More tools'")
        assert "More tools" in page.locator(".ui-rail").inner_text()
        # Zones: a click on the floor (stronger) and on a side wall (hidden gap). The pick is sent as the picker sends it.
        page.wait_for_function("() => fdmUI.services.picker.data", timeout=120_000)
        regions = page.evaluate("""() => { const r = fdmUI.services.picker.data.regions.filter((x) => x.kind === 'flat');
          const up = r.filter((x) => x.normal[2] > 0.9).sort((a, b) => b.area_mm2 - a.area_mm2)[0];
          const side = r.filter((x) => Math.abs(x.normal[0]) > 0.9).sort((a, b) => b.area_mm2 - a.area_mm2)[0];
          return [up.id, side.id]; }""")
        pick = "(r) => fdmUI.services.picker.listeners.pick.forEach((fn) => fn({ kind: 'face', region: r, point: null }, 0, 0))"
        page.evaluate(pick, regions[0])
        page.wait_for_function("() => fdmUI.store.get().boxes.length === 1", timeout=60_000)
        page.get_by_role("radio", name="Hidden gap").click()
        page.evaluate(pick, regions[1])
        page.wait_for_function("() => fdmUI.store.get().boxes.length === 2", timeout=60_000)
        kinds = page.evaluate("() => fdmUI.store.get().boxes.map((b) => b.kind)")
        assert kinds == ["stronger", "void"], kinds
        assert page.evaluate("() => fdmUI.services.stage.ghosts.length") == 2
        assert page.locator(".tools-list li").count() == 2
        assert "null" not in page.locator(".panel").inner_text()
        done.append("zones: stronger + hidden gap")
        # Copies.
        page.get_by_role("radio", name="Copies").click()
        page.wait_for_selector(".tools-count", timeout=60_000)
        assert page.evaluate("() => fdmUI.services.stage.ghosts.length") == 0
        page.get_by_role("button", name="One more").click()
        page.wait_for_function("() => fdmUI.store.get().copies === 2 && document.querySelectorAll('.tools-plate__part').length === 2", timeout=60_000)
        fill = page.get_by_role("button", name="Fill the plate")
        most = int(fill.inner_text().split("(")[1].rstrip(")"))
        fill.click()
        page.wait_for_function("(n) => document.querySelectorAll('.tools-plate__part').length === n", arg=most, timeout=60_000)
        done.append(f"copies: {most} on the plate")
        # Box heat.
        page.get_by_role("radio", name="Box heat").click()
        page.get_by_role("button", name="Work out the heat").click()
        page.wait_for_selector(".tools-verdict", timeout=180_000)
        verdict = page.locator(".tools-verdict").inner_text()
        assert "°C inside" in verdict, verdict
        assert page.evaluate("() => fdmUI.services.stage.field != null"), "walls not coloured"
        assert page.locator(".sim-legend").count() == 1
        done.append("box heat: " + verdict.split("\n")[0])
        # Versions: keep, change the walls, keep again; the second row shows the difference.
        page.get_by_role("radio", name="Versions").click()
        assert page.evaluate("() => fdmUI.services.stage.field == null"), "heat field left on"
        page.get_by_role("button", name="Keep this version").click()
        page.evaluate("() => fdmUI.store.set({ option: { ...fdmUI.store.get().option, walls: 4, minutes: 75, total_g: 26 } })")
        page.get_by_role("button", name="Keep this version").click()
        rows = page.locator(".tools-table tbody tr").all_inner_texts()
        assert len(rows) == 2 and "+15 min" in rows[1] and "+6 g" in rows[1], rows
        done.append("versions compared")
        # Back on Done: the extras are listed, and the 3MF carries the copies and the zones.
        page.locator(".panel-foot").get_by_role("button", name="Back").click()
        page.wait_for_function(f"() => ({title})() === 'Done'")
        summary = page.locator(".ui-disc__head", has_text="More tools").inner_text()
        assert "2 zones or gaps" in summary and f"{most} copies" in summary, summary
        with page.expect_download(timeout=120_000) as dl:
            page.locator(".done-export-row").get_by_role("button", name="Export 3MF").click()
        z = zipfile.ZipFile(io.BytesIO(open(dl.value.path(), "rb").read()))
        text = "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.endswith((".model", ".config")))
        assert text.count("modifier") + text.count("negative") >= 2 or text.count("<object ") > most, "no zones in the 3MF"
        done.append(f"3MF with extras ({len(z.namelist())} files)")
        assert not errors, errors

        # A part too big for the bed: Problems' Split it.
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.set_input_files(".drop-wrap input[type=file]", files=[{"name": "long_bar.stl", "mimeType": "model/stl", "buffer": _bar_stl(300, 30, 20)}])
        page.wait_for_function(f"() => ({title})() === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("""() => { fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, goal: 'fast', optimized: 1, noLoads: true,
          option: { up: [0, 0, 1], orientation: 'Flat', material: 'pla', material_name: 'PLA', nozzle: '0.4', layer_height: 0.2, walls: 2, top: 4, bottom: 3,
            infill: 0.15, settings: { wall_loops: '2' } } }); fdmUI.go('problems'); }""")
        row = page.locator(".ui-find", has_text="Too big")
        row.wait_for(timeout=180_000)
        row.locator(".ui-find__head").click()
        row.get_by_role("button", name="Split it").click()
        page.wait_for_function(f"() => ({title})() === 'More tools'")
        page.get_by_role("button", name="Find the cut").click()
        page.wait_for_selector(".tools-list li", timeout=180_000)
        pieces = page.locator(".tools-list li").count()
        assert pieces >= 2, pieces
        assert page.evaluate("() => fdmUI.services.stage.field != null"), "pieces not shown"
        names = []
        page.on("download", lambda d: names.append(d.suggested_filename))
        page.get_by_role("button", name="Download the").click()
        page.wait_for_function("() => true")
        page.wait_for_timeout(2_000)
        assert "long_bar_piece_1.stl" in names and "long_bar_pin.stl" in names, names
        done.append(f"split: {pieces} pieces + pins")
        assert not errors, errors
        return "; ".join(done)
    finally:
        page.close()


def _sliced_tower() -> bytes:
    """A stand-in for a sliced temperature tower: layer marks every 0.2 mm up to 49 mm, the slicer's own temperatures."""
    out = ["M104 S220", "M109 S220", "G28"]
    for k in range(1, 246):
        z = round(0.2 * k, 2)
        out += [";LAYER_CHANGE", f";Z:{z:g}", f"G1 Z{z:g} F600", "G1 X10 Y10 E0.5 F1800"]
    out += ["M104 S0"]
    return "\n".join(out).encode()


def check_new_ui_quality_calibration(browser, port: int) -> str:
    """Quality settings and tests (T-572): the temperature tower downloads, a sliced tower comes back with each block's
    temperature set, the best block is saved as the material's temperature; the ironing test downloads as a 3MF with
    the flows set, the smoothest is saved; the engine has both. Then a Quality part's Done list has the looks settings
    (slower outer wall, inner-outer-inner, aligned seam)."""
    import io
    import zipfile

    ctx = browser.new_context(viewport={"width": 1280, "height": 720}, accept_downloads=True)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    done = []
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.wait_for_function("() => Boolean(fdmUI.services.settings?.isEngineSynced?.())", timeout=240_000)
        page.locator(".app-header").get_by_role("button", name="Settings & calibration", exact=True).click()
        page.wait_for_selector(".settings-wrap")
        page.locator("#settings-tab-calibration").click()
        # Temperature tower.
        page.locator('[data-cal-card="temp"]').click()
        with page.expect_download(timeout=60_000) as dl:
            page.get_by_role("button", name="Download model").click()
        assert dl.value.suggested_filename == "temp_tower.stl", dl.value.suggested_filename
        with page.expect_download(timeout=60_000) as dl:
            page.set_input_files('.cal-picker-group input[type=file]', files=[{"name": "temp_tower.gcode", "mimeType": "text/plain", "buffer": _sliced_tower()}])
        text = open(dl.value.path(), encoding="utf-8").read()
        assert dl.value.suggested_filename == "temp_tower_temps.gcode" and "M104 S230" in text and "M104 S205" in text, dl.value.suggested_filename
        assert text.rstrip().endswith("M104 S0"), "heater left on"
        page.locator('.cal-pick[data-pick-value="215"]').click()
        page.get_by_role("button", name="Save result").click()
        done.append("tower: G-code with 230 → 205 °C, 215 saved")
        # Ironing.
        page.get_by_role("button", name="All calibration tests").click()
        page.locator('[data-cal-card="ironing"]').click()
        with page.expect_download(timeout=60_000) as dl:
            page.get_by_role("button", name="Download model").click()
        assert dl.value.suggested_filename == "ironing_test.3mf", dl.value.suggested_filename
        z = zipfile.ZipFile(io.BytesIO(open(dl.value.path(), "rb").read()))
        assert z.read("Metadata/layer_config_ranges.xml").decode().count('opt_key="ironing_flow"') == 5
        page.locator('.cal-pick[data-pick-value="12"]').click()
        page.get_by_role("button", name="Save result").click()
        page.get_by_role("button", name="All calibration tests").click()
        cards = page.locator(".cal-grid").inner_text()
        assert "Prints best at 215 °C" in cards and "Irons best at 12 % flow" in cards, cards
        got = page.evaluate("""async () => { await fdmUI.services.settings.syncEngine();
          const m = (await fdmUI.services.engine.call("presets")).materials.my_cal_pla; return [m.nozzle_temp_c, m.ironing_flow_pct]; }""")
        assert got == [215, 12], got
        done.append("ironing: 3MF with 5 flows, 12 % saved")
        # A Quality part: the looks settings on Done.
        page.get_by_role("button", name="Back to your part").click()
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="Hole block").click()
        page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("""() => { fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s', material: '' }, goal: 'quality', optimized: 1,
          option: { up: [0, 0, 1], orientation: 'Flat', material: 'my_cal_pla', material_name: 'My PLA', nozzle: '0.4', layer_height: 0.2, walls: 2, top: 4,
            bottom: 3, infill: 0.15, settings: { wall_loops: '2' } } }); fdmUI.go('done'); }""")
        page.wait_for_selector(".done-settings li", state="attached", timeout=180_000)
        rows = page.locator(".done-settings").text_content()
        for want in ("Outer wall speed", "Wall order", "inner-outer-inner wall", "Seam"):
            assert want in rows, (want, rows)
        done.append("Quality Done list: outer wall, wall order, seam")
        assert not errors, errors
        return "; ".join(done)
    finally:
        ctx.close()


def _extras_parts() -> tuple[bytes, bytes]:
    """A 40 × 30 × 10 mm plate with a Ø8.4 hole and a 3 mm slot, and a Ø8.2 peg standing in the hole (one assembly).
    Fixture files (made with fdmcore's extrude): the browser test runs without the engine's Python packages."""
    fixtures = ROOT / "tools" / "fixtures"
    return (fixtures / "slot_plate.stl").read_bytes(), (fixtures / "slot_plate_peg.stl").read_bytes()


def check_new_ui_later_extras(browser, port: int) -> str:
    """T-573: Tolerances' Flat gaps (two slot walls clicked: 3 mm measured, a blade slides in; a 3 mm part jams red, a
    2.5 mm one fits; the change goes into Problems' batch) and Mating part (a peg from the same assembly: the gap painted
    on the part, the tightest spot marked, snug); and the Holds brush (paint by dragging over the top: one painted hold,
    sent to the engine as points with a radius)."""
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    done = []
    title = "() => document.getElementById('step-title')?.textContent"
    plate, peg = _extras_parts()
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.set_input_files(".drop-wrap input[type=file]", files=[{"name": "slot_plate.stl", "mimeType": "model/stl", "buffer": plate}])
        page.wait_for_function(f"() => ({title})() === 'Setup' && fdmUI.store.get().catalog", timeout=240_000)
        page.evaluate("""() => { fdmUI.store.set({ profile: { ...fdmUI.store.get().profile, printer: 'bambu_p1s' }, goal: 'fast', optimized: 1, noLoads: true, change: 'both',
          option: { up: [0, 0, 1], orientation: 'Flat', material: 'pla', material_name: 'PLA', nozzle: '0.4', layer_height: 0.2, walls: 2, top: 4, bottom: 3,
            infill: 0.15, settings: { wall_loops: '2' } } }); fdmUI.go('tolerances'); }""")
        page.wait_for_selector(".tol-modes", timeout=120_000)
        page.wait_for_function("() => fdmUI.services.picker.data", timeout=120_000)
        # Flat gaps: the two slot walls (x = 28 facing +x, x = 31 facing -x).
        page.get_by_role("radio", name="Flat gaps").click()
        walls = page.evaluate("""() => { const r = fdmUI.services.picker.data.regions.filter((x) => x.kind === 'flat' && Math.abs(x.normal[0]) > 0.99);
          const inSlot = r.filter((x) => x.area_mm2 > 150 && x.area_mm2 < 250);
          return [inSlot.find((x) => x.normal[0] > 0).id, inSlot.find((x) => x.normal[0] < 0).id]; }""")
        pick = "(r) => fdmUI.services.picker.listeners.pick.forEach((fn) => fn({ kind: 'face', region: r, point: null }, 0, 0))"
        page.evaluate(pick, walls[0])
        assert "other side" in page.locator(".panel").inner_text()
        page.evaluate(pick, walls[1])
        page.wait_for_selector(".tol-card", timeout=60_000)
        head = page.locator(".tol-card b").first.inner_text()
        assert head.startswith("Slot: 3.00 mm"), head
        assert page.evaluate("() => fdmUI.services.stage.part.group.getObjectByName('tol-feeler')?.children.length") == 1
        field = page.locator(".tol-card input").first
        field.fill("3")
        field.press("Enter")
        page.wait_for_function("() => /Will jam/.test(document.querySelector('.tol-verdict')?.textContent ?? '')", timeout=60_000)
        field = page.locator(".tol-card input").first
        field.fill("2.5")
        field.press("Enter")
        page.wait_for_function("() => /^Fits/.test(document.querySelector('.tol-verdict')?.textContent ?? '')", timeout=60_000)
        done.append("slot 3.00 mm: 3 mm jams, 2.5 mm fits")
        page.locator(".tol-card").get_by_role("button", name="Add to my changes").click()
        page.wait_for_function("() => fdmUI.store.get().pending.some((p) => p.id.startsWith('tol:gap-'))", timeout=120_000)
        item = page.evaluate("() => fdmUI.store.get().pending.find((p) => p.id.startsWith('tol:gap-')).title")
        done.append(f"in the batch: {item}")
        # Mating part: the peg.
        page.get_by_role("radio", name="Mating part").click()
        assert page.evaluate("() => fdmUI.services.stage.part.group.getObjectByName('tol-feeler')") is None, "blade left behind"
        page.set_input_files('.panel input[type=file][accept=".stl,.3mf,.obj"]', files=[{"name": "peg.stl", "mimeType": "model/stl", "buffer": peg}])
        page.wait_for_selector(".sim-legend", timeout=180_000)
        verdict = page.locator(".tol-verdict").inner_text()
        assert verdict.startswith("Snug") and "0.10 mm" in verdict, verdict
        assert page.evaluate("() => fdmUI.services.stage.field != null && fdmUI.services.stage.ghosts.length === 1")
        assert page.evaluate("() => fdmUI.services.stage.part.group.getObjectByName('tol-tight') != null")
        done.append("peg: snug, gap painted, tightest marked")
        page.get_by_role("radio", name="Holes & pegs").click()
        assert page.evaluate("() => fdmUI.services.stage.field == null && fdmUI.services.stage.ghosts.length === 0"), "map left behind"
        # The brush on Holds: paint across the top.
        page.evaluate("() => fdmUI.go('holds')")
        page.wait_for_function("() => fdmUI.services.picker.enabled && fdmUI.services.picker.data", timeout=120_000)
        a, b = page.evaluate("() => [fdmUI.services.picker.toClient([4, 4, 10]), fdmUI.services.picker.toClient([22, 4, 10])].map((c) => [c.x, c.y])")
        page.mouse.move(*a)
        page.wait_for_function("() => fdmUI.services.picker.hit?.kind === 'face'")
        page.mouse.click(*a)
        page.wait_for_selector(".ui-radial", state="attached")
        page.get_by_role("menuitem", name="Paint part of this face").click()
        assert "Drag over the part" in page.locator(".panel").inner_text()
        page.mouse.move(*a)
        page.mouse.down()
        for k in range(1, 13):
            page.mouse.move(a[0] + (b[0] - a[0]) * k / 12, a[1] + (b[1] - a[1]) * k / 12)
        page.mouse.up()
        spots = int(page.locator(".hold-brush .ui-hint").last.inner_text().split()[0])
        assert spots >= 4, spots
        page.locator(".hold-brush").get_by_role("button", name="Done").click()
        hold = page.evaluate("""async () => { const m = await import('./steps/args.js'); const h = fdmUI.store.get().holds.at(-1);
          return [h.name, m.holdToEngine(h)]; }""")
        assert hold[0] == "Painted area" and len(hold[1]["points"]) == spots and hold[1]["radius"] == 4 and "region" not in hold[1], hold
        assert page.evaluate("() => fdmUI.services.stage.controls.enabled"), "view left frozen"
        assert page.evaluate("() => fdmUI.services.stage.part.group.getObjectByName('hold-brush')?.children.length") == spots
        done.append(f"brush: {spots} spots, one painted hold")
        assert not errors, errors
        return "; ".join(done)
    finally:
        page.close()


def check_old_project(browser, port: int) -> str:
    """Projects from before the cut-over (T-580) still open: an old-style project (goal "strength", a load in kg
    pointing "down") and a shared report with a project inside land on Setup with the part, its holds and loads."""
    sys.path.insert(0, str(ROOT / "core"))
    from fdmcore.project import dump

    stl = (ROOT / "web" / "samples" / "hook.stl").read_bytes()
    text = dump("hook.stl", stl, profile={"printer": "bambu_p1s", "material": "petg", "nozzle": "0.4", "goal": "strength"},
                loads=[{"region": 0, "kind": "fixed"}, {"region": 1, "kind": "force", "kg": 2.0, "direction": "down"}],
                faces={"clean": [3]})
    report = ('<!doctype html><title>Print check</title><p>shared</p><script type="application/json" id="fdm-check-project">'
              + text.replace("<", "\\u003c") + "</script>")
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        seen = []
        for name, body in (("hook_project.json", text), ("hook_report.html", report)):
            page.goto(f"http://127.0.0.1:{port}/web/")
            page.wait_for_selector("body[data-ready]")
            page.get_by_role("button", name="Optimize your print").click()
            page.set_input_files(".drop-wrap input[type=file]", files=[{"name": name, "mimeType": "text/plain", "buffer": body.encode()}])
            page.wait_for_function("() => document.getElementById('step-title')?.textContent === 'Setup'", timeout=240_000)
            s = page.evaluate("""() => { const s = fdmUI.store.get(); return { part: s.part.name, goal: s.goal, material: s.profile.material,
              holds: s.holds.map((h) => [h.region, h.type, Array.isArray(h.point)]), loads: s.loads.map((l) => [l.region, Math.round(l.newtons * 10) / 10, l.direction]),
              faces: s.showFaces }; }""")
            assert s == {"part": "hook.stl", "goal": "strong", "material": "petg", "holds": [[0, "clamped", True]],
                         "loads": [[1, 19.6, [0, 0, -1]]], "faces": [3]}, s
            seen.append(name)
        assert not errors, errors
        return f"{', '.join(seen)}: part, goal strength → strong, PETG, hold and 2 kg load (19.6 N down) back on their faces"
    finally:
        page.close()


def _face_on_screen(page, exclude=()) -> tuple[int, float, float]:
    """A big flat face turned towards the camera, and where its middle is on screen, confirmed by hovering it there
    (the picker must report that face). Works at any window size, unlike fixed fractions of the canvas."""
    cands = page.evaluate("""(exclude) => { const p = fdmUI.services.picker, st = fdmUI.services.stage;
      const cam = st.camera.position.clone().sub(st.controls.target).normalize();
      return p.data.regions.filter((r) => r.kind === 'flat' && !exclude.includes(r.id) && r.normal[0] * cam.x + r.normal[1] * cam.y + r.normal[2] * cam.z > 0.25)
        .sort((a, b) => b.area_mm2 - a.area_mm2).slice(0, 6).map((r) => { const c = p.toClient(r.centroid); return [r.id, c.x, c.y]; }); }""", list(exclude))
    for region, x, y in cands:
        page.mouse.move(x, y)
        try:
            page.wait_for_function("(r) => fdmUI.services.picker.hit?.kind === 'face' && fdmUI.services.picker.hit.region === r", arg=region, timeout=3_000)
            return region, x, y
        except Exception:  # noqa: BLE001 - hidden behind another face from here: try the next one
            continue
    raise AssertionError(f"no face to click among {cands}")


def check_journey(browser, port: int, viewport=(1280, 720)) -> str:
    """The whole app like a user (T-581): Start → sample → Setup (printer) → Environment → Holds (a face and the inside
    corner) → Loads (N, then MPa) → Load details → Analyse (Cancel, then Fast) → Options (pin PLA, re-optimize, use)
    → Simulation → Problems (a slicer fix, Confirm all changes) → Done (Export 3MF, Tolerances) → Settings and back →
    Undo / Redo → the Quality path → the dark theme (screenshot)."""
    w, h = viewport
    page = browser.new_page(viewport={"width": w, "height": h})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    title = lambda: page.locator("#step-title").inner_text()  # noqa: E731
    wait_step = lambda name, t=240_000: page.wait_for_function("(n) => document.getElementById('step-title')?.textContent === n", arg=name, timeout=t)  # noqa: E731
    nxt = lambda: page.locator(".panel-foot .ui-btn--primary").click()  # noqa: E731
    done = []
    try:
        page.goto(f"http://127.0.0.1:{port}/web/")
        page.wait_for_selector("body[data-ready]")
        page.get_by_role("button", name="Optimize your print").click()
        page.get_by_role("button", name="Sample parts").click()
        page.locator(".sample-card", has_text="L-bracket").click()
        wait_step("Setup")
        page.wait_for_function("() => fdmUI.store.get().catalog")
        page.get_by_role("button", name="Printer").click()
        page.locator(".ui-select__pop:not([hidden]) .ui-select__search").fill("P1S")
        page.locator(".ui-select__pop:not([hidden]) .ui-select__option", has_text="Bambu Lab P1S").click()
        nxt()
        if page.locator("dialog.ui-modal").count():  # T-610 asks to re-run after changing goal on Setup
            page.locator("dialog.ui-modal").get_by_role("button", name="Not now").click()
        wait_step("Environment")
        nxt()
        wait_step("Holds")
        page.wait_for_function("() => fdmUI.services.picker.enabled && fdmUI.services.picker.data", timeout=120_000)
        held, x, y = _face_on_screen(page)
        page.mouse.click(x, y)
        page.get_by_role("menuitem", name="Hold this face").click()
        cx, cy = page.evaluate("""() => { const p = fdmUI.services.picker, c = p.toClient(p.data.edges.find((l) => l.kind === 'corner').meta.midpoint);
          return [c.x + 3, c.y]; }""")
        page.mouse.move(cx, cy)
        page.wait_for_function("() => fdmUI.services.picker.hit?.kind === 'corner'", timeout=5_000)
        page.mouse.click(cx, cy)
        page.get_by_role("menuitem", name="Hold inside corner").click()
        assert page.evaluate("() => fdmUI.store.get().holds.map((x) => x.kind)") == ["face", "corner"]
        done.append("2 holds")
        nxt()
        wait_step("Loads")
        page.wait_for_function("() => fdmUI.services.picker.enabled", timeout=60_000)
        pushed, x, y = _face_on_screen(page, exclude=[held])
        page.mouse.click(x, y)
        page.wait_for_selector(".ui-pop")
        page.keyboard.type("50")
        page.keyboard.press("Enter")
        page.wait_for_selector(".ui-pop", state="detached")
        _, x, y = _face_on_screen(page, exclude=[held, pushed])
        page.mouse.click(x, y)
        page.wait_for_selector(".ui-pop")
        page.locator(".ui-pop [role=radio]", has_text="MPa").click()
        page.locator(".ui-pop input").fill("0.05")
        page.get_by_role("button", name="OK").click()
        page.wait_for_selector(".ui-pop", state="detached")
        units = page.evaluate("() => fdmUI.store.get().loads.map((l) => l.unit)")
        assert units == ["N", "MPa"], units
        # Undo takes the last load back, Redo puts it there again (the header buttons, §1.7).
        page.locator(".app-header button[title^='Undo']").click()
        assert page.evaluate("() => fdmUI.store.get().loads.length") == 1
        page.locator(".app-header button[title^='Redo']").click()
        assert page.evaluate("() => fdmUI.store.get().loads.length") == 2
        done.append("loads in N and MPa, undo/redo")
        nxt()
        wait_step("Load details")
        nxt()
        wait_step("Analyse")
        page.get_by_role("radio", name=re.compile("Extended")).click()
        page.get_by_role("button", name="Start optimization").click()
        page.wait_for_selector(".ui-progress")
        page.locator(".ui-progress").get_by_role("button", name="Cancel").click()
        page.wait_for_selector(".ui-progress", state="detached", timeout=120_000)
        assert title() == "Analyse"
        page.get_by_role("radio", name=re.compile("Fast")).click()
        page.get_by_role("button", name="Start optimization").click()
        wait_step("Options", 600_000)
        done.append("analysis cancelled, then Fast")
        page.wait_for_selector(".ui-col")
        page.screenshot(path=str(OUT / f"journey_options_{w}x{h}.png"))
        mats = page.evaluate("() => fdmUI.store.result('optimize').options.map((o) => o.material)")
        cell = show_comparison_row(page, "material").nth(mats.index("pla") if "pla" in mats else 0)
        cell.hover()
        cell.locator(".ui-col__pin").click()
        pinned = page.evaluate("() => fdmUI.store.get().constraints.material")
        page.get_by_role("button", name="Re-optimize with my choices").click()
        page.wait_for_function("(m) => !document.querySelector('.ui-progress') && fdmUI.store.result('optimize').options.every((o) => o.material === m)", arg=pinned, timeout=600_000)
        page.get_by_role("button", name="Use this option").first.click()
        if page.locator("dialog.ui-modal").count():
            page.get_by_role("button", name="Use it anyway").click()
        wait_step("Simulation")
        done.append(f"options pinned to {pinned}")
        # The part stands the way it prints once an option is chosen (T-585), and says so beside the camera bar.
        orientation = page.evaluate("() => fdmUI.store.get().option.orientation")
        assert orientation and orientation in page.locator(".stage-turned").inner_text(), "the part shows how it prints"
        done.append(f"turned to print ({orientation})")
        page.get_by_role("button", name="Run simulations").click()
        page.wait_for_selector(".sim-status", timeout=300_000)
        nxt()
        page.wait_for_function("() => ['Problems', 'Done'].includes(document.getElementById('step-title')?.textContent)", timeout=60_000)
        if title() == "Problems":
            page.wait_for_selector(".prob-list .ui-find, .prob-clear", timeout=180_000)
            for i in range(page.locator(".ui-find").count()):
                row = page.locator(".ui-find").nth(i)
                row.locator(".ui-find__head").click()
                add = row.locator(".ui-fix", has=page.locator(".ui-tag.is-slicer")).get_by_role("button", name="Add")
                if add.count():
                    add.first.click()
                    break
            if page.locator(".ui-pending").count():
                page.get_by_role("button", name="Confirm all changes").click()
                page.wait_for_selector(".prob-batch", timeout=300_000)
                done.append("a slicer fix confirmed and re-checked")
            nxt()
            wait_step("Done")
        page.wait_for_selector(".done-grid")
        with page.expect_download(timeout=120_000) as dl:
            page.locator(".done-export-row").get_by_role("button", name="Export 3MF").click()
        assert dl.value.suggested_filename.endswith(".3mf")
        page.get_by_role("button", name="Check the fits").click()
        wait_step("Tolerances & fits")
        page.wait_for_selector(".tol-row, .tol-list:empty", state="attached", timeout=120_000)
        page.locator(".panel-foot").get_by_role("button", name="Back").click()
        wait_step("Done")
        done.append("3MF exported, Tolerances opened")
        page.locator(".app-header").get_by_role("button", name="Settings & calibration", exact=True).click()
        page.get_by_role("button", name="Back to your part").click()
        wait_step("Done")
        # The Quality path: no holds, loads or load details; Analyse offers Fast and Extended and shows the faces people see.
        page.locator(".ui-rail").get_by_role("button", name="Setup").click()
        wait_step("Setup")
        page.get_by_role("radio", name=re.compile("Quality")).click()
        rail = page.locator(".ui-rail").inner_text()
        assert not any(x in rail for x in ("Holds", "Loads", "Load details")), rail
        nxt()
        if page.locator("dialog.ui-modal").count():  # T-610 asks to re-run after changing goal on Setup
            page.locator("dialog.ui-modal").get_by_role("button", name="Not now").click()
        wait_step("Environment")
        nxt()
        wait_step("Analyse")  # straight from Environment: Quality has no holds or loads
        assert page.get_by_role("radio", name=re.compile("Complete")).count() == 0, "Complete offered for Quality"
        page.wait_for_selector(".showfaces")  # the faces people see: guessed, tinted on the part
        done.append("Quality path")
        page.locator(".app-header").get_by_role("button", name="Dark theme").click()
        page.wait_for_timeout(500)  # colours fade over --t (200 ms)
        bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor.match(/\\d+/g).slice(0, 3).map(Number)")
        assert sum(bg) < 200, bg
        page.screenshot(path=str(OUT / f"journey_dark_{w}x{h}.png"))
        page.locator(".app-header").get_by_role("button", name="Dark theme").click()
        done.append("dark theme")
        assert not errors, errors
        return f"{w}×{h}: " + ", ".join(done)
    finally:
        page.close()


CHECKS = [
    ("sample and STEP land on Setup", "check_new_ui"),
    ("old projects and shared reports open", "check_old_project"),
    ("holds", "check_new_ui_holds"),
    ("direction gizmo", "check_new_ui_gizmo"),
    ("settings", "check_new_ui_settings"),
    ("hover picker", "check_new_ui_picker"),
    ("loads", "check_new_ui_loads"),
    ("analyse", "check_new_ui_analyse"),
    ("options", "check_new_ui_options"),
    ("simulation", "check_new_ui_simulation"),
    ("simulation, heat", "check_new_ui_heat"),
    ("quality", "check_new_ui_quality"),
    ("problems", "check_new_ui_problems"),
    ("done", "check_new_ui_done"),
    ("tolerances", "check_new_ui_tolerances"),
    ("tolerances extras and the hold brush", "check_new_ui_later_extras"),
    ("lighter and tuned settings", "check_new_ui_lighter_tuned"),
    ("more tools", "check_new_ui_tools"),
    ("quality settings and tests", "check_new_ui_quality_calibration"),
]


def _run_suite(port: int, args) -> Run:
    with sync_playwright() as p:
        channel = None if args.browser == "chromium" else args.browser
        browser = p.chromium.launch(channel=channel, headless=not args.headed)
        ui = SharedUi(browser, port)
        run = Run(ui)
        try:
            for name, fn in CHECKS:
                run.step(name, lambda fn=fn: globals()[fn](ui, port))
            if not args.quick:  # the whole journey, at both desktop sizes the plan designs for (§1.1)
                for size in ((1280, 720), (1440, 900)):
                    run.step(f"journey {size[0]}x{size[1]}", lambda size=size: check_journey(ui, port, size))
        finally:
            ui.close()
            browser.close()
    return run


def main():
    sys.stdout.reconfigure(errors="replace")  # results quote UI text (×, →); Windows consoles may not have them
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="the checks only, without the two full journeys (CI runs this)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--browser", default="msedge", choices=["msedge", "chrome", "chromium"],
                    help="chromium = Playwright's own build (CI: playwright install chromium)")
    ap.add_argument("--gemini-review", action="store_true", help="have the Gemini CLI review the screenshots")
    ap.add_argument("--gemini-bin", default=str(Path.home() / ".gemini" / "bin" / "agy.exe"))
    args = ap.parse_args()
    started = time.monotonic()
    OUT.mkdir(exist_ok=True)
    port = serve()
    run = _run_suite(port, args)
    if args.gemini_review:
        gemini_review(args.gemini_bin)
    print(f"\n{len(run.failures)} failure(s) in {time.monotonic() - started:.1f}s; screenshots in {OUT}")
    sys.exit(1 if run.failures else 0)


if __name__ == "__main__":
    main()
