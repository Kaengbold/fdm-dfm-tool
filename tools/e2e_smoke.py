"""Browser smoke test of the web app (headless Edge/Chrome via Playwright).

    pip install playwright
    python tools/e2e_smoke.py [extra parts .stl/.step ...] [--quick] [--headed] [--browser chrome|chromium] [--gemini-review]

Starts its own static server, drives the UI like a user, and prints one line per
step plus every console error. Screenshots go to ./e2e-output/. Exit code 1 on failure.
With --quick, runs a short subset (~2 min: boot, phone start screen, Simple flow to 3MF,
level switch, one finding). With --gemini-review, the Gemini CLI looks at each screenshot
for visual problems and writes its notes to e2e-output/gemini-review.md (it only gets the
image, no shell access).
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
        try:
            # 90 s: on a busy host (little free RAM) the 30 s default ran out and ended the whole run.
            self.page.screenshot(path=str(OUT / f"{len(self.failures):02d}_{safe}.png"), timeout=90_000)
        except Exception as e:  # noqa: BLE001 - a missing picture is a failure, not the end of the run
            self.failures.append(f"{name} (screenshot)")
            print(f"FAIL {name} (screenshot): {e}")


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


def click_sample(page, name):
    """Older fixtures live under More samples; open it like a visitor before clicking."""
    button = page.locator(f"[data-sample={name}]")
    fold = button.locator("xpath=ancestor::details[1]")
    if fold.count() and not fold.evaluate("d => d.open"):
        fold.locator("summary").click()
    button.click()

def check_thumbnails(page) -> str:
    """T-423: real uncached images, cache hits, and no changes to the live view."""
    # Let the lazy card renders finish before counting renders in this test.
    page.wait_for_function("() => [...document.querySelectorAll('#options > li img')].every(i => i.complete && i.naturalWidth > 0)")
    result = page.evaluate("""async () => {
      const v = fdm.viewer, s = fdm.state;
      const before = () => JSON.stringify([v.camera.position.toArray(), v.camera.quaternion.toArray(),
        v.controls.target.toArray(), v.renderer.domElement.width, v.renderer.domElement.height]);
      const saved = before(), target = v.renderer.getRenderTarget();
      const render = v.renderPicture;
      let renders = 0;
      v.renderPicture = function(...args) { renders++; return render.apply(this, args); };
      const times = [], pictures = [];
      let cached = true;
      try {
        const requests = s.options.map(o => ({up: o.up, size: [120, 90]}));
        for (const f of s.check.findings.filter(f => f.bbox).slice(0, 3)) {
          requests.push({up: s.options[s.active].up, focus: f, size: [120, 90]});
        }
        // Symmetric samples can offer the same build direction more than once.
        const unique = new Map(requests.map(r => [JSON.stringify([r.up, r.focus?.bbox, r.focus?.severity, r.size]), r]));
        for (const request of unique.values()) {
          const start = performance.now();
          const picture = v.thumbnail(request);
          times.push(performance.now() - start);
          pictures.push(picture);
          const count = renders;
          cached &&= v.thumbnail(request) === picture && renders === count;
        }
        // Reports use the same off-screen renderer for live heat/warp overlays.
        const snapshot = v.snapshot(null, 120);
        const unchanged = before() === saved && v.renderer.getRenderTarget() === target;
        const variety = [];
        for (const picture of [...pictures, snapshot]) {
          const img = new Image(); img.src = picture; await img.decode();
          const canvas = document.createElement('canvas'); canvas.width = img.width; canvas.height = img.height;
          const ctx = canvas.getContext('2d'); ctx.drawImage(img, 0, 0);
          const px = ctx.getImageData(0, 0, img.width, img.height).data, colors = new Set();
          for (let i = 0; i < px.length; i += 4) colors.add(`${px[i] >> 4},${px[i+1] >> 4},${px[i+2] >> 4}`);
          variety.push(colors.size);
        }
        return {times, cached, unchanged, renders, count: unique.size, variety};
      } finally { v.renderPicture = render; }
    }""")
    assert result["cached"], "thumbnail cache miss on an identical request"
    assert result["unchanged"], "taking pictures changed the camera, controls, canvas size or render target"
    assert result["renders"] == result["count"] + 1, result
    assert min(result["variety"]) > 8, f"blank or near-blank image: {result}"
    assert max(result["times"]) < 100, f"thumbnail took 100 ms or more: {result}"
    assert page.locator("#options .orientation-picture").count() == page.locator("#options > li").count()
    return f"{result['count']} images, slowest {max(result['times']):.1f} ms; cached, nonblank, live view unchanged"


def check_finding_views(page, limit: int | None = None) -> str:
    """Click every place (or the first ``limit``), checking live pixels and camera flights."""
    entries = page.evaluate("() => fdm.state.check.findings")
    if limit is not None:
        entries = ([f for f in entries if f.get("bbox")] or entries)[:limit]
        assert entries, "no findings to open"
    page.evaluate("() => document.querySelectorAll('#findings .impact > details').forEach(d => d.open = true)")
    focused, worst = 0, 0
    place_indices = {}
    for finding in entries:
        title = re.compile(r"^" + re.escape(finding["title"]) + r"(?: ×\d+)?$")
        card = page.locator("#findings .findings > li").filter(has=page.locator(".f-head b", has_text=title)).first
        if not card.locator(":scope > details").evaluate("d => d.open"):
            card.locator(":scope > details > summary").click()
        page.evaluate("""() => {
          const v = fdm.viewer;
          v.part.geometry.computeBoundingBox();
          window.findingFlight = {record: true, inside: 0};
          const sample = () => {
            if (!window.findingFlight.record) return;
            if (v.part.geometry.boundingBox.containsPoint(v.camera.position)) window.findingFlight.inside++;
            requestAnimationFrame(sample);
          };
          requestAnimationFrame(sample);
        }""")
        places = card.locator(".places .place")
        if places.count():
            index = place_indices.get(finding["rule"], 0)
            places.nth(index).click()
            place_indices[finding["rule"]] = index + 1
        else:
            card.locator(".detail.clickable").click()
        page.wait_for_function("() => fdm.viewer.cameraMove === null")
        pixels = page.evaluate("""bbox => {
          const v = fdm.viewer;
          window.findingFlight.record = false;
          v.renderer.render(v.scene, v.camera);
          const canvas = document.createElement('canvas'); canvas.width = 240; canvas.height = 180;
          const ctx = canvas.getContext('2d'); ctx.drawImage(v.renderer.domElement, 0, 0, 240, 180);
          const data = ctx.getImageData(0, 0, 240, 180).data, colors = new Map();
          for (let i = 0; i < data.length; i += 4) {
            const key = (data[i] << 16) | (data[i+1] << 8) | data[i+2];
            colors.set(key, (colors.get(key) ?? 0) + 1);
          }
          const point = bbox && v.camera.position.clone().fromArray(bbox[0]).add(
            v.camera.position.clone().fromArray(bbox[1])).multiplyScalar(0.5).project(v.camera);
          return {dominant: Math.max(...colors.values()) / (240 * 180),
            inside: window.findingFlight.inside || Number(v.part.geometry.boundingBox.containsPoint(v.camera.position)),
            marked: !point || (Math.abs(point.x) < 1 && Math.abs(point.y) < 1 && Math.abs(point.z) < 1)};
        }""", finding.get("bbox"))
        assert pixels["dominant"] <= 0.8, f"{finding['rule']}: over 80% one colour: {pixels}"
        assert not pixels["inside"], f"{finding['rule']}: camera entered the part's bounds: {pixels}"
        assert pixels["marked"], f"{finding['rule']}: marked place is outside the view"
        worst = max(worst, pixels["dominant"])
        if finding.get("bbox"):
            focused += 1
            assert page.is_visible("#whole-part"), "no way back from a finding"
    if focused:
        page.click("#whole-part")
        assert page.is_hidden("#whole-part")
        assert page.evaluate("() => fdm.viewer.controls.target.distanceTo(fdm.viewer.part.geometry.boundingSphere.center) < 1e-6")
    return f"{len(entries)} findings clicked, {focused} places framed; most common colour {worst:.1%}; flights outside, whole-part return works"


def check_fresh_start(page):
    """Shared fresh-visit assertions for the start screen."""
    assert page.evaluate("() => document.body.dataset.level") == "simple"
    assert page.evaluate("() => fdm.steps.current") == "part"
    assert page.locator("#drop").is_visible(), "fresh visit hides the drop zone"
    assert not page.locator("#guide").evaluate("d => d.open"), "guide opened by itself"
    assert page.locator("#more-tools").is_hidden(), "more tools offered before a result"
    assert page.evaluate("() => localStorage.getItem('fdm-guided')") == "1"


def check_phone_start_screen(page) -> str:
    """Verify the start screen fits cleanly at phone width without horizontal overflow."""
    check_fresh_start(page)
    assert page.locator(".samples").evaluate("e => e.scrollWidth <= e.clientWidth"), "sample list overflows phone width"
    assert page.locator("[data-pane=part]").evaluate("e => e.scrollWidth <= e.clientWidth"), "start pane overflows phone width"
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth"), "page overflows phone width"
    return "390x844 start screen: Simple drop zone and samples fit without overflow"


def check_levels_and_guide(page) -> str:
    """Open the start guide, verify Simple visibility, and switch to Expert."""
    assert not page.locator("#guide").evaluate("d => d.open")
    page.click("#show-guide")
    assert page.locator("#guide").evaluate("d => d.open")
    assert page.locator("[data-guide=settings]").get_attribute("class") == "current"
    page.click("[data-guide=settings]")
    simple = page.evaluate("() => [document.body.dataset.level, document.getElementById('design-block').checkVisibility(), document.getElementById('my-presets').checkVisibility()]")
    assert simple == ["simple", False, False], simple
    assert page.locator("#failed-guide").is_enabled(), "fix-my-print path unavailable in the guide"

    # T-374: strength is a step of its own in Simple; the optional fold stays hidden.
    assert page.evaluate("() => document.getElementById('load-optional').hidden && fdm.steps.steps.includes('strength')"), "Simple: strength not a step"
    page.click("input[name=level][value=expert] >> xpath=..")
    assert page.evaluate("() => localStorage.getItem('fdm-level')") == "expert"
    order = page.evaluate("() => fdm.steps.steps")  # T-376: simulations first, then the problems
    assert order.index("sims") < order.index("printing") < order.index("problems"), order

    # T-352: the optional load fold is only there in Simple (where the strength block moves into it).
    assert page.evaluate("() => document.getElementById('load-optional').hidden"), "empty load fold shown in Expert"
    return f"guide -> {simple[0]}; back to expert"


def assert_primary_at_foot(page, button: str):
    """Verify exactly one primary action is visible and it sits in the panel foot."""
    visible = page.locator(".panes button.primary:visible, .pane-foot button.primary:visible")
    assert visible.count() == 1, visible.all_inner_texts()
    assert visible.first.get_attribute("id") == button, visible.all_inner_texts()
    assert page.locator(f".pane-foot #{button}").is_visible(), f"{button} is not at the foot"


def simple_flow_to_3mf(run: Run, sample: str = "mushroom") -> str:
    """Walk one sample through the Simple steps (part -> have -> strength -> result) to the 3MF download."""
    page = run.page
    page.click("input[name=level][value=simple] >> xpath=..")
    page.evaluate("() => fdm.steps.go('part')")  # the samples are on the first step
    assert_primary_at_foot(page, "step-next")
    run.act(lambda: click_sample(page, sample))
    assert page.locator("input[name=goal][value=strength]").is_visible(), "goal choice hidden in Simple"
    assert page.locator("#compute").count() == 0, "redundant Compute button remains"
    assert_primary_at_foot(page, "step-next")
    before = run.done()
    page.click("#step-next")
    assert page.evaluate("() => fdm.steps.current") == "strength"
    assert_primary_at_foot(page, "step-next")
    # T-414: with no load set, the step says it is optional and the foot button says it skips it.
    assert page.locator("#step-next").inner_text() == "Skip: show my result"
    assert "Optional" in page.locator("[data-pane=strength]").inner_text()
    page.click("#step-next")
    assert run.done() == before, "completed checks were run again with no load set"
    items = page.locator("#compute-steps li")
    states = items.evaluate_all("lis => lis.map((li) => li.className)")
    assert states == ["done", "done", "skipped", "done"], states
    assert not page.locator("#result-progress").evaluate("d => d.open"), "finished progress should be folded"
    assert "no load set" in items.nth(2).text_content(), "missing optional-strength explanation"
    assert page.evaluate("() => fdm.steps.current") == "result"
    assert_primary_at_foot(page, "dl-3mf")
    with page.expect_download() as download:
        run.act(lambda: page.click("#dl-3mf"))
    assert download.value.suggested_filename.endswith(".3mf")
    assert page.locator("#after-dl").is_visible(), "next steps hidden inside More options"
    page.click("#after-dl-close")
    return f"{sample} -> {download.value.suggested_filename}"


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


def check_phone_content_first(run: Run) -> str:
    """T-456 at 390 x 844 with a part loaded: one header row with the levels behind "Menu"; on each Simple step
    the heading (Result: the verdict) and the first control show without scrolling; no control is covered by
    the sticky buttons or the sticky 3D view; the view folds to a strip and comes back."""
    page = run.page
    page.set_viewport_size({"width": 390, "height": 844})
    try:
        assert page.locator(".levels").is_hidden() and page.locator("#menu-button").is_visible(), "levels not behind Menu"
        header = page.locator(".top").bounding_box()["height"]
        assert header < 60, f"header is {header:.0f} px: more than one row"
        page.click("#menu-button")
        assert page.locator(".levels").is_visible(), "Menu does not open"
        page.keyboard.press("Escape")
        assert page.locator(".levels").is_hidden(), "Escape leaves the menu open"
        seen = []
        for step in ("have", "result"):
            page.evaluate(f"() => (fdm.steps.go('{step}'), window.scrollTo(0, 0))")
            page.wait_for_timeout(200)
            first = page.evaluate("""() => {
              const pane = [...document.querySelectorAll('.pane')].find((p) => p.getClientRects().length);
              const head = pane.querySelector('h2') ?? document.getElementById('verdict');
              const control = [...pane.querySelectorAll('button, select, input:not([type=hidden])')].find((c) => c.checkVisibility());
              const bar = document.querySelector('.foot-buttons').getBoundingClientRect().top;
              return { pane: pane.dataset.pane, head: head.getBoundingClientRect().bottom, control: control.getBoundingClientRect().bottom, bar };
            }""")
            assert first["head"] <= first["bar"] and first["control"] <= first["bar"], f"{step}: content starts below the fold {first}"
            covered = page.evaluate("""() => {
              const panel = document.getElementById('panel');
              const out = [];
              for (const c of panel.querySelectorAll('button, select, summary, input:not([type=hidden])')) {
                if (!c.checkVisibility() || c.closest('.foot-buttons')) continue; // closed <details> content still has boxes
                const target = c.type === 'checkbox' || c.type === 'radio' ? (c.closest('label') ?? c) : c;
                target.scrollIntoView({ block: 'center' });
                const r = target.getBoundingClientRect();
                if (!r.width || !r.height) continue;
                const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                if (hit && !target.contains(hit) && !hit.contains(target)) out.push(`${c.id || c.textContent.trim().slice(0, 30)} under ${hit.id || hit.className}`);
              }
              return out;
            }""")
            assert not covered, f"{step}: covered controls {covered}"
            seen.append(f"{step}: heading at {first['head']:.0f} px")
        page.evaluate("() => window.scrollTo(0, 0)")
        page.click("#view-hide")
        strip = page.locator("main#viewer").bounding_box()["height"]
        assert strip < 70, f"folded view is {strip:.0f} px"
        page.click("#view-hide")  # "Show 3D view"
        assert page.locator("main#viewer canvas").is_visible(), "the view does not come back"
        return f"header {header:.0f} px; {'; '.join(seen)}; folded view {strip:.0f} px; nothing covered"
    finally:
        page.set_viewport_size({"width": 1500, "height": 950})


def check_saved_projects(run: Run, other: str = "l_bracket") -> str:
    """T-453: two parts worked on in a row are both under "Your recent projects" after a reload and both reopen
    as the same entries (no copies); "Delete all" empties the list."""
    page = run.page
    count = "() => document.querySelectorAll('#saved-list .saved-item').length"
    first = page.evaluate("() => fdm.state.name")
    page.evaluate("() => fdm.autosave()")
    page.evaluate("() => fdm.steps.go('part')")
    run.act(lambda: click_sample(page, other), 300_000)
    second = page.evaluate("() => fdm.state.name")
    assert second != first, (first, second)
    page.evaluate("() => fdm.autosave()")
    page.reload()
    run.booted()
    page.wait_for_selector("#restore:not([hidden])", timeout=60_000)
    assert second in page.locator("#restore").inner_text(), "the newest project isn't the one offered"
    page.evaluate("() => fdm.steps.go('part')")
    names = page.locator("#saved-list .saved-item strong").all_inner_texts()
    assert second in names and first in names and names.index(second) < names.index(first), names
    saved = page.evaluate(count)
    for name in (first, second):
        run.act(lambda name=name: page.locator("#saved-list button.saved-open", has_text=name).first.click(), 300_000)
        page.wait_for_function("() => document.getElementById('status').textContent.includes('opened the project')", timeout=300_000)
        assert page.evaluate("() => fdm.state.name") == name
        assert page.locator("#restore").is_hidden(), "the offer stays up after opening a project"
        page.evaluate("() => fdm.autosave()")
        page.wait_for_function(f"() => document.querySelector('#saved-list .saved-item.current strong')?.textContent === {name!r}")
        assert page.evaluate(count) == saved, "reopening made a copy"
    page.evaluate("() => fdm.steps.go('part')")
    page.once("dialog", lambda d: d.accept())
    page.click("#saved-clear")
    page.wait_for_selector("#saved", state="hidden")
    page.evaluate("() => fdm.autosave()")  # deleted while open: not saved again until a new part
    assert page.locator("#saved").is_hidden(), "the open project came back after Delete all"
    return f"{saved} saved; reopened {first} and {second}; Delete all emptied the list"


def run_quick(run: Run) -> None:
    """Short smoke check (~2 min): phone start screen, Simple flow to 3MF, level switch, one finding."""
    page = run.page
    page.set_viewport_size({"width": 390, "height": 844})
    try:
        run.step("phone start screen", lambda: check_phone_start_screen(page))
    finally:
        page.set_viewport_size({"width": 1500, "height": 950})
    run.step("simple flow to 3MF download", lambda: simple_flow_to_3mf(run, "hole_block"))
    run.step("phone: content first", lambda: check_phone_content_first(run))
    run.step("levels and start guide", lambda: check_levels_and_guide(page))

    def open_one_finding():
        page.evaluate("() => fdm.steps.go('problems')")
        return check_finding_views(page, limit=1)

    run.step("open one finding", open_one_finding)
    run.step("saved projects: two parts in a row", lambda: check_saved_projects(run))


def run_extra_parts(run: Run, parts: list[Path]) -> None:
    page = run.page
    for part in parts:
        def load_part(part=part):
            status = run.act(lambda: page.set_input_files("#file", str(part)), 300_000)
            top = page.locator("#options > li").first.inner_text().splitlines()
            return f"{status} | top: {top[1:4]} | findings: {findings(page)}"
        run.step(f"file {part.name}", load_part)


def _run_suite(port: int, args) -> Run:
    with sync_playwright() as p:
        channel = None if args.browser == "chromium" else args.browser
        browser = p.chromium.launch(channel=channel, headless=not args.headed)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        # Start with a real fresh visit; the Guide step later opens Expert for the rest of the checks.
        page.add_init_script("localStorage.setItem('fdm-sa-demo', '1');")
        # Long simulations ask first; the test says "Run it" unless a step wants to see the question.
        page.add_init_script("""addEventListener('DOMContentLoaded', () => new MutationObserver(() => {
            const d = document.getElementById('long-run');
            if (d?.open && !window.keepAsking) d.querySelector('#long-yes').click();
        }).observe(document.body, { attributes: true, subtree: true, attributeFilter: ['open'] }));""")
        run = Run(page)
        page.goto(f"http://127.0.0.1:{port}/web/")
        run.step("engine boots", run.booted)
        if args.quick:
            run_quick(run)
            run_extra_parts(run, args.parts)
            browser.close()
            return run

        def failed_without_part():
            page.click("#failed-start")
            page.locator('[data-symptom="warped_corner"]').click(timeout=60_000)
            page.get_by_text("Usual causes", exact=True).wait_for(timeout=60_000)
            assert "layers cool" in page.locator("#failed-result").inner_text()
            assert not page.locator("#failed-result .mine").count()
            assert page.locator('[data-symptom="warped_corner"]').get_attribute("aria-pressed") == "true", "picked symptom not marked"
            page.click("#failed-close")
            return "warped corner: usual causes without a part"
        run.step("failed print without a part", failed_without_part)

        def part_first():
            check_fresh_start(page)
            page.click("input[name=level][value=advanced] >> xpath=..")
            page.reload()
            run.booted()
            assert page.evaluate("() => document.body.dataset.level") == "advanced", "returning visit lost its level"
            assert page.locator("#drop").is_visible()
            assert not page.locator("#guide").evaluate("d => d.open"), "returning visit opened the guide"
            page.click("input[name=level][value=simple] >> xpath=..")
            return "fresh: Simple drop zone; returning: saved Advanced; no automatic dialog"

        run.step("first and returning visits start with the part", part_first)

        def indoors_default():
            assert page.input_value("#use-temp") == ""
            assert page.locator("#use-temp option:checked").inner_text() == "Indoors (room temperature)"
            return "Indoors (room temperature)"
        run.step("fresh visit uses indoors", indoors_default)

        def no_internal_task_numbers():
            # innerText omits hidden text, so check the "Estimate" notes too,
            # including ones in steps the user has not opened yet.
            text = page.locator("body").inner_text()
            hints = page.locator(".estimate-note").evaluate_all("els => els.map(el => el.textContent)")
            hits = re.findall(r"T-\d+", "\n".join([text, *hints]))
            assert not hits, f"internal task numbers shown to users: {hits}"
            return "page text and estimate notes contain no internal task numbers"

        run.step("no internal task numbers", no_internal_task_numbers)

        def estimate_badges():
            """T-444: estimates carry a pill that explains itself on a click, not a dashed frame."""
            assert page.locator(".wip, [data-wip]").count() == 0, "a dashed 'Not finished yet' frame is left"
            assert page.locator(".estimate").count() == 3  # strength, design changes, shaking and knocks
            # The pane may not be open yet (no part loaded), so click through the DOM and read `hidden`.
            toggle = """() => { const p = document.querySelector('[aria-controls=est-strength]'); p.click();
              return [document.getElementById('est-strength').hidden, p.getAttribute('aria-expanded')]; }"""
            assert page.evaluate("() => document.getElementById('est-strength').hidden")
            assert page.evaluate(toggle) == [False, "true"], "the note did not open"
            assert page.evaluate(toggle) == [True, "false"], "the note did not close"
            return "three estimate pills, the note opens and closes"

        run.step("estimate badges", estimate_badges)

        def blocked_storage():
            """Private mode / strict cookie settings: every storage call throws. The app must still start."""
            other = browser.new_page()
            errors = []
            other.on("pageerror", lambda e: errors.append(str(e)))
            other.add_init_script("Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('blocked', 'SecurityError'); } });")
            other.goto(f"http://127.0.0.1:{port}/web/")
            other.wait_for_function("() => document.getElementById('status').textContent.startsWith('Ready')", timeout=180_000)
            assert other.evaluate("() => document.body.dataset.level") == "simple"
            assert other.locator("#drop").is_visible()
            assert not other.locator("#guide").evaluate("d => d.open")
            other.click("#show-guide")
            assert other.locator("#guide").evaluate("d => d.open"), "Guide button needs storage"
            other.click("[data-guide=settings]")
            assert not other.locator("#guide").evaluate("d => d.open")
            other.close()
            assert not errors, errors
            return "starts with storage blocked"

        run.step("starts when browser storage is blocked", blocked_storage)

        run.step("levels and start guide", lambda: check_levels_and_guide(page))

        def showcase_samples():
            assert page.locator(".more-samples").evaluate("d => !d.open"), "More samples starts open"
            names = page.locator(".sample-example [data-sample]").evaluate_all("bs => bs.map(b => b.dataset.sample)")
            assert names == ["hole_block", "loaded_hook", "enclosure"], names
            for width in (390, 1500):
                page.set_viewport_size({"width": width, "height": 950})
                assert page.locator(".samples").evaluate("e => e.scrollWidth <= e.clientWidth"), "sample list overflows"
            page.click("input[name=level][value=simple] >> xpath=..")
            run.act(lambda: click_sample(page, "hole_block"))
            page.evaluate("() => fdm.steps.go('result')")
            classes = page.evaluate("() => [...fdm.state.options[fdm.state.active].face_classes]")
            assert any(c in (1, 2) for c in classes), "first sample has no warning colours"
            assert page.locator("#view-key").is_visible()
            assert page.evaluate("() => fdm.state.check.findings.some(f => f.fixes.some(fx => fx.domain === 'slicer'))"), "no slicer fix"
            page.click("input[name=level][value=expert] >> xpath=..")
            page.evaluate("() => fdm.steps.go('part')")
            run.act(lambda: click_sample(page, "loaded_hook"))
            page.wait_for_function("() => fdm.state.loads.some(l => l.kind === 'force')")
            page.evaluate("() => fdm.steps.go('strength')")
            loads = page.evaluate("() => fdm.state.loads")
            assert any(l["kind"] == "fixed" for l in loads), loads
            force = next(l for l in loads if l["kind"] == "force")
            assert force["kg"] == 2 and force["direction"] == [0, 0, -1], force
            assert page.locator("#run-strength").is_enabled(), "example load cannot be checked"
            page.evaluate("() => fdm.steps.go('part')")
            return "three explained samples; sideways holes coloured with slicer fix; hook has held face and downward 2 kg example load"

        run.step("showcase samples", showcase_samples)

        for name in SAMPLES:
            def load(name=name):
                if name == SAMPLES[0]:
                    # Inspect the real check profile without loading an extra part before the guide.
                    page.evaluate("""() => {
                      window.checkProfile = null;
                      window.originalPostMessage = Worker.prototype.postMessage;
                      Worker.prototype.postMessage = function(message, ...args) {
                        if (message.method === 'check') window.checkProfile = message.args.profile;
                        return window.originalPostMessage.call(this, message, ...args);
                      };
                    }""")
                    try:
                        status = run.act(lambda: click_sample(page, name))
                        sent = page.evaluate("() => window.checkProfile")
                        assert sent is not None, "first sample finished without sending a check request"
                        assert sent["use_temp_c"] is None, sent
                    finally:
                        page.evaluate("""() => {
                          Worker.prototype.postMessage = window.originalPostMessage;
                          delete window.originalPostMessage;
                          delete window.checkProfile;
                        }""")
                else:
                    status = run.act(lambda: click_sample(page, name))
                assert page.locator("#options > li").count() >= 2, "no orientation options"
                return f"{status} | top: {page.locator('#options > li').first.inner_text().splitlines()[1:3]} | findings: {findings(page)}"
            run.step(f"sample {name}", load)
            run.step(f"thumbnails {name}", lambda: check_thumbnails(page))

        def broken_mesh_notice():
            import struct
            raw = _box_stl([((0, 0, 0), 20)])
            holed = OUT / "holed_cube.stl"
            holed.write_bytes(raw[:80] + struct.pack("<I", 10) + raw[84 + 2 * 50:])
            run.act(lambda: page.set_input_files("#file", str(holed)), 300_000)
            notice = page.locator("#mesh-health")
            assert notice.is_visible() and "1 hole" in notice.inner_text(), notice.inner_text()
            page.evaluate("() => fdm.steps.go('part')")
            run.act(lambda: click_sample(page, "l_bracket"), 300_000)
            assert notice.is_hidden(), "clean sample has a broken-file notice"
            return "holed cube warned; clean sample has no notice"
        run.step("broken mesh notice", broken_mesh_notice)

        def answer_first_result():
            page.click("input[name=level][value=simple] >> xpath=..")
            page.evaluate("() => fdm.steps.go('part')")
            sample = page.locator("[data-sample=feature_test]")
            fold = sample.locator("xpath=ancestor::details[1]")
            if fold.count() and not fold.evaluate("d => d.open"):
                fold.locator("summary").click()
            run.act(sample.click)
            page.evaluate("() => fdm.steps.go('result')")
            stats = page.locator("#stats")
            assert stats.is_visible(), "Simple Result hides time and filament"
            text = stats.inner_text()
            assert text.startswith("About ") and " g of filament" in text and " · " in text, text
            cost = page.evaluate("() => fdm.state.check.stats.cost")
            if cost is not None:
                assert f"about {cost:.2f} for the filament" in text, text
            assert page.evaluate("""() => {
              const before = (a, b) => !!(document.getElementById(a).compareDocumentPosition(document.getElementById(b)) & Node.DOCUMENT_POSITION_FOLLOWING);
              return before('verdict', 'stats') && before('stats', 'result-problems') && before('result-problems', 'result-all')
                && before('dl-3mf', 'result-next') && before('result-next', 'result-options');
            }"""), "answer, estimate and problems are out of order"
            cards = page.locator("#result-problems > li")
            assert cards.count() == 3, f"expected three problem kinds, got {cards.count()}"
            rules = cards.evaluate_all("lis => lis.map(li => li.dataset.rule)")
            assert len(set(rules)) == 3, rules
            assert page.locator("#result-problems .result-fix").count() == 3, "not one fix per problem"
            page.wait_for_function("() => [...document.querySelectorAll('#result-problems img')].every(img => img.complete && img.naturalWidth > 0)")
            assert page.locator("#result-problems img").count() == 3, "missing place pictures"
            for selector in ("#dl-copies", "#dl-grip", "#dl-report", "#dl-share", "#settings"):
                assert page.locator(selector).is_hidden(), f"optional control visible before download: {selector}"
            assert not page.locator("#result-options").evaluate("d => d.open")
            assert not page.locator("#result-all").evaluate("d => d.open")
            assert page.locator(".pane-foot #dl-3mf.primary").is_visible(), "primary download no longer at the foot"
            assert page.locator("#more-tools").is_visible(), "more tools invitation lost"
            page.locator("#result-problems .result-place").first.click()
            assert page.evaluate("() => fdm.viewer.markers.children.length > 0"), "problem click has no marker"
            page.click("#result-all > summary")
            assert page.locator("#findings").is_visible(), "Show all does not open the full findings"
            page.click("#result-all > summary")
            page.click("#result-options > summary")
            for selector in ("#dl-copies", "#dl-grip", "#dl-report", "#dl-share", "#settings-fold > summary"):
                assert page.locator(selector).is_visible(), f"More options lost {selector}"
            page.click("#result-options > summary")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.locator("[data-pane=result]").evaluate("p => p.scrollWidth <= p.clientWidth"), "phone Result overflows"
            assert stats.is_visible()
            assert page.locator(".pane-foot #dl-3mf.primary").is_visible()
            page.click("#result-options > summary")
            assert page.locator("#result-options").evaluate("p => p.scrollWidth <= p.clientWidth"), "phone options overflow"
            assert page.locator("#dl-copies").is_visible()
            page.click("#result-options > summary")
            page.set_viewport_size({"width": 1500, "height": 950})
            page.click("input[name=level][value=basic] >> xpath=..")
            page.evaluate("() => fdm.steps.go('problems')")
            assert stats.is_visible() and stats.evaluate("p => p.closest('[data-pane]').dataset.pane") == "problems"
            assert page.evaluate("() => !!(document.getElementById('stats').compareDocumentPosition(document.getElementById('blk-problems')) & Node.DOCUMENT_POSITION_FOLLOWING)"), "stats returned below the findings"
            assert page.locator("#blk-download #dl-copies").count() == 1
            page.click("input[name=level][value=expert] >> xpath=..")
            page.evaluate("() => fdm.steps.go('part')")
            return f"{text} | three pictured problem kinds with one fix each; optional controls folded; phone fits; stats restore in Basic"

        run.step("Simple Result puts the answer first", answer_first_result)

        def quiet_hook_key():
            page.click("input[name=level][value=simple] >> xpath=..")
            page.evaluate("() => fdm.steps.go('part')")
            run.act(lambda: click_sample(page, "hook"))
            assert page.is_hidden("#view-label"), "orientation ranking shown on the setup step"
            page.evaluate("() => fdm.steps.go('result')")
            assert page.is_hidden("#view-key"), "trouble-free Hook still shows a colour key"
            assert page.is_visible("#view-label"), "different build directions have no ranking label"
            # One option, or duplicate entries for the same pose, give no useful comparison.
            page.evaluate("""() => {
              window.savedOptions = fdm.state.options;
              fdm.state.options = [fdm.state.options[0]];
              fdm.steps.go('have'); fdm.steps.go('result');
            }""")
            try:
                assert page.is_hidden("#view-label"), "single orientation still labelled best"
                page.evaluate("""() => {
                  fdm.state.options.push(fdm.state.options[0]);
                  fdm.steps.go('have'); fdm.steps.go('result');
                }""")
                assert page.is_hidden("#view-label"), "duplicate pose still labelled best"
            finally:
                page.evaluate("() => { fdm.state.options = window.savedOptions; delete window.savedOptions; }")
                page.click("input[name=level][value=expert] >> xpath=..")
            return "Simple Hook: no key; ranking only with a different pose to compare"
        run.step("quiet colour key and orientation label on Hook", quiet_hook_key)

        def more_tools():
            assert page.locator("#more-tools").is_hidden(), "invitation visible outside Simple"
            page.click("input[name=level][value=simple] >> xpath=..")
            page.evaluate("() => fdm.steps.go('result')")
            invitation = page.locator("#more-tools")
            assert invitation.is_visible(), "first Simple result has no invitation"
            assert "Want to check strength, fits or design changes?" in invitation.inner_text()
            assert "link" in page.locator("#show-more-tools").get_attribute("class").split()
            page.set_viewport_size({"width": 375, "height": 812})
            try:
                assert invitation.evaluate("p => p.scrollWidth <= p.clientWidth"), "invitation overflows at phone width"
                for selector in ("#show-more-tools", "#dismiss-more-tools"):
                    assert page.locator(selector).evaluate("b => { const r = b.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }"), selector
            finally:
                page.set_viewport_size({"width": 1500, "height": 950})
            run.act(lambda: page.click("#show-more-tools"))
            assert page.evaluate("() => document.body.dataset.level") == "advanced"
            assert page.evaluate("() => fdm.steps.current") == "strength"
            assert page.evaluate("() => ['strength', 'fits', 'design'].every(s => fdm.steps.steps.includes(s))")
            assert page.is_checked("input[name=mode][value=both]"), "design changes not enabled"
            page.click("input[name=level][value=simple] >> xpath=..")
            page.evaluate("() => fdm.steps.go('result')")
            assert invitation.is_hidden(), "used invitation came back"
            page.reload()
            run.booted()
            assert page.locator("#more-tools").evaluate("p => p.hidden"), "used invitation forgot on return"
            page.click("input[name=level][value=expert] >> xpath=..")
            return "Simple result offers a wrapping secondary link; Advanced opens strength, fits and design; use remembered"

        run.step("Simple result offers more tools", more_tools)

        def dismiss_more_tools():
            other = browser.new_page()
            other_run = Run(other)
            try:
                other.goto(f"http://127.0.0.1:{port}/web/")
                other_run.booted()
                other_run.act(lambda: click_sample(other, "hook"))
                other.click("#step-next")
                other.click("#step-next")
                assert other.locator("#more-tools").is_visible()
                other.click("#dismiss-more-tools")
                assert other.locator("#more-tools").is_hidden()
                assert other.evaluate("() => document.body.dataset.level") == "simple", "dismissal changed level"
                other.reload()
                other_run.booted()
                assert other.locator("#more-tools").evaluate("p => p.hidden"), "dismissal forgot on return"
                assert not other.locator("#guide").evaluate("d => d.open")
                assert not other_run.console, other_run.console
                return "Not now keeps Simple and stays dismissed after reload"
            finally:
                other.close()

        run.step("more tools invitation can be dismissed", dismiss_more_tools)

        def feature_colour_key():
            page.evaluate("() => fdm.steps.go('part')")
            run.act(lambda: click_sample(page, "feature_test"))
            page.evaluate("() => fdm.steps.go('problems')")
            classes = page.evaluate("() => [...new Set(fdm.state.options[fdm.state.active].face_classes)].sort()")
            assert 1 in classes or 2 in classes, f"feature test has no warning colours: {classes}"
            assert page.is_visible("#view-key"), "feature test has warning colours but no key"
            entries = page.locator("#view-key > li:visible").evaluate_all("els => els.map(e => Number(e.dataset.faceClass)).sort()")
            assert entries == classes, f"key {entries} does not match face colours {classes}"
            page.wait_for_timeout(450)
            visible = "() => fdm.viewer.problemsVisible(fdm.viewer.problemSamples(fdm.state.options[fdm.state.active].face_classes))"
            assert page.evaluate(visible), "feature_test: no red/amber face is visible after the check"
            # Repainting the same check must respect the user's camera; explicit controls still work.
            page.evaluate("() => fdm.viewer.resetCamera()")
            before = page.evaluate("() => fdm.viewer.camera.position.toArray()")
            page.evaluate("() => { fdm.steps.go('orient'); fdm.steps.go('problems'); }")
            page.wait_for_timeout(450)
            after = page.evaluate("() => fdm.viewer.camera.position.toArray()")
            assert max(abs(a - b) for a, b in zip(before, after)) < 1e-6, "same check turned twice"
            page.click("#look-below")
            page.wait_for_timeout(450)
            assert page.evaluate("() => fdm.viewer.camera.position.z < fdm.viewer.controls.target.z")
            assert page.is_checked("#bed-see-through")
            page.uncheck("#bed-see-through")
            assert page.evaluate("() => fdm.viewer.bedGroup.children.every(o => o.material.opacity === 1 && o.material.depthWrite)")
            page.check("#bed-see-through")

            check_thumbnails(page)
            assert page.locator("#findings .place-picture").count() > 0, "finding cards have no place pictures"
            # Hook's second pose has support but no yellow: absent entries must disappear too.
            page.evaluate("() => fdm.steps.go('part')")
            run.act(lambda: click_sample(page, "hook"))
            run.act(lambda: page.locator("#options > li > strong").nth(1).click())
            page.evaluate("() => fdm.steps.go('problems')")
            assert page.is_visible("#view-key") and page.is_hidden('#view-key > li[data-face-class="1"]')
            run.act(lambda: page.locator("#options > li > strong").first.click())
            assert page.is_hidden("#view-key"), "key survived a switch back to the trouble-free pose"
            return f"feature test key matches {classes}; unused yellow hidden; key clears on pose switch"
        run.step("colour key contains only colours on the part", feature_colour_key)

        def organic_model():
            import sys

            sys.path.insert(0, str(ROOT / "core"))
            sys.path.insert(0, str(ROOT / "core" / "tests"))
            from fdmcore.mesh import to_binary_stl
            from test_organic import blob

            sculpt = OUT / "sculpt.stl"
            sculpt.write_bytes(to_binary_stl(blob()))
            cad_rules = "['first_layer_detail', 'shallow_relief', 'side_detail_small', 'fine_detail_rough', 'small_hole_undersized', 'gap_fuses', 'flat_gap_fuses', 'weak_upright_pin', 'insert_wall_thin', 'screw_wall_thin']"
            count = f"() => fdm.state.check.findings.filter(f => {cad_rules}.includes(f.rule)).length"
            if not page.is_checked("input[name=mode][value=both]"):
                run.act(lambda: page.click("input[name=mode][value=both] >> xpath=.."))
            page.evaluate("() => fdm.steps.go('part')")
            run.act(lambda: page.set_input_files("#file", str(sculpt)), 300_000)
            page.wait_for_function("() => fdm.state.check && fdm.state.name === 'sculpt.stl'", timeout=300_000)
            assert page.is_checked("input[name=mode][value=print]"), "sculpt did not default to print mode"
            assert page.is_visible("#kind-hint"), "no notice for a sculpted model"
            assert not page.is_checked("#cad-checks")
            assert page.evaluate(count) == 0, "CAD-only findings on a sculpted model"
            run.act(lambda: page.check("#cad-checks"))
            page.wait_for_function(f"() => ({count})() > 0", timeout=300_000)
            back = page.evaluate(count)
            page.evaluate("() => fdm.steps.go('part')")
            run.act(lambda: click_sample(page, "hook"))
            assert page.is_hidden("#kind-hint"), "notice stays on a CAD part"
            return f"sculpt: notice shown, 0 CAD-only findings; {back} with the switch on; hidden again on Hook"
        run.step("sculpted model turns CAD checks off", organic_model)

        def print_fix_chips():
            if not page.is_checked("input[name=mode][value=print]"):
                run.act(lambda: page.click("input[name=mode][value=print] >> xpath=.."))
            page.evaluate("() => fdm.steps.go('part')")
            run.act(lambda: click_sample(page, "test_block"))
            chips = page.locator("#findings .findings > li > details > summary .paths .chip").all_inner_texts()
            assert chips, "sample has no fix chips"
            assert all("change the design" not in chip.lower() for chip in chips), chips
            return f"{len(chips)} chips have no design fix"
        run.step("print mode fix chips", print_fix_chips)

        def modes():
            out = []
            for mode in ("design", "both", "print"):
                run.act(lambda: page.click(f"input[name=mode][value={mode}] >> xpath=.."))
                out.append(f"{mode}: {len(findings(page))} findings, design panel {'shown' if page.locator('#design-block').is_visible() else 'hidden'}")
            return "; ".join(out)
        run.step("mode toggle", modes)

        def other_orientation():
            page.evaluate("() => fdm.viewer.thumbnailCache.clear()")
            check_thumbnails(page)
            run.act(lambda: page.locator("#options > li > strong").nth(1).click())
            check_thumbnails(page)  # changing the print orientation invalidates the old images
            return f"{len(findings(page))} findings"
        run.step("select option 2", other_orientation)

        def option_costs():
            costs = page.locator("#options .option-cost").all_inner_texts()
            n = page.locator("#options > li").count()
            assert len(costs) == n and all("support" in c for c in costs), f"cards without support grams: {costs}"
            active = page.evaluate("() => [...document.querySelectorAll('#options > li')].findIndex(li => li.classList.contains('active'))")
            assert active == 1, f"option 2 no longer marked after the check redrew the cards ({active})"
            cards = page.locator("#options > li")
            page.wait_for_function("() => [...document.querySelectorAll('#options > li img')].every(i => i.complete && i.naturalWidth > 0)")
            for card in cards.all():
                title = card.locator("strong").first.inner_text()
                assert not re.match(r"^Option \d", title), title
                assert card.locator("img, canvas").count() > 0
                assert "mm²" not in card.inner_text(), card.inner_text()
                card.locator("summary").click()
                assert "mm²" in card.inner_text(), card.inner_text()
                card.locator("summary").click()
            page.screenshot(path=OUT / "orientation-cards.png")
            return " | ".join(costs)
        run.step("support grams on each orientation card", option_costs)

        def orientation_updates_findings():
            run.act(lambda: click_sample(page, "hook"))
            seen = {}
            for i in range(page.locator("#options > li").count()):
                run.act(lambda i=i: page.locator("#options > li > strong").nth(i).click())
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
            run.act(lambda: click_sample(page, "detail_plate"))
            run.act(lambda: page.click("#nozzle-block > summary"))
            rows = page.locator("#nozzle-table tr").count() - 1
            assert rows == 4, f"expected 4 nozzle rows, got {rows}"
            run.act(lambda: page.click("input[name=goal][value=strength] >> xpath=.."))
            walls = page.evaluate("() => fdm.state.check.settings.find(s => s.key === 'wall_loops').value")
            run.act(lambda: page.click("input[name=goal][value=balanced] >> xpath=.."))
            return f"{rows} nozzle rows; strength goal -> {walls} walls"
        run.step("nozzle comparison + goal", nozzles_and_goal)

        def suggestion_selection():
            run.act(lambda: page.click("input[name=mode][value=both] >> xpath=.."))
            run.act(lambda: click_sample(page, "l_bracket"))
            page.evaluate("() => fdm.steps.go('design')")
            boxes = page.locator("#edits input[type=checkbox]")
            count = boxes.count()
            assert count > 0, "no suggestions to choose"
            assert page.locator("#edits input:checked").count() == 0, "fresh suggestions ticked"
            assert page.locator("#apply-edits").is_disabled(), "empty selection can be applied"
            page.click("#edits-all")  # ticks the changes that fix a finding, not the folded other ideas
            assert page.locator("#edits input:checked").count() == page.locator("#edits > li:not(.other-ideas) input").count()
            page.click("#edits-none")
            assert page.locator("#edits input:checked").count() == 0
            fixes = page.locator("#edits > li:not(.other-ideas) .edit-for").all_inner_texts()
            assert fixes and all(t.startswith("Fixes: ") for t in fixes), f"changes without their finding: {fixes}"
            numbers = page.locator("#cad-advice > li").all_inner_texts()
            assert any("teardrop" in t for t in numbers), f"no CAD numbers for the sideways holes: {numbers}"
            page.locator("#cad-advice > li button").first.click()  # highlights it on the part
            assert page.locator("#cad-advice > li.active").count() == 1
            boxes.first.check()
            chosen = page.evaluate("() => fdm.state.edits.filter(e => e.selected).map(e => e.id)")
            run.act(lambda: page.dispatch_event("#param-teardrop_angle_deg", "change"))
            assert page.evaluate("() => fdm.state.edits.filter(e => e.selected).map(e => e.id)") == chosen, "re-suggest lost ticks"
            return (f"{count} unticked suggestions ({len(fixes)} fix a finding); all, none and re-suggest ticks kept; "
                    f"{len(numbers)} CAD numbers")
        run.step("suggestions start unticked and keep user choices", suggestion_selection)

        def project_suggestion_ticks():
            import base64
            import json

            # A 45-degree tilt gives several drooping faces: check both a tick and an untick.
            fixture = OUT / "suggestion_ticks_start.json"
            fixture.write_text(json.dumps({"format": "fdm-check-project", "version": 1, "mode": "both", "up": [0, 1, -1],
                                           "part": {"name": "l_bracket.stl", "stl": base64.b64encode((ROOT / "web/samples/l_bracket.stl").read_bytes()).decode("ascii")}}), encoding="utf-8")
            run.act(lambda: page.set_input_files("#file", str(fixture)), 300_000)
            page.wait_for_function("() => document.getElementById('status').textContent.includes('opened the project')", timeout=300_000)
            page.evaluate("() => fdm.steps.go('fits')")
            row = page.locator("#fits li", has_text="Ø6.0 mm hole")
            row.locator(".fit:not(.fastener) input").fill("6")
            run.act(lambda: row.get_by_role("button", name="Size it").click())
            requested = page.locator("#edits label", has_text="sliding fit").locator("input")
            assert requested.is_checked(), "direct request did not start ticked"
            page.click("#edits-none")  # In particular, the explicit untick of a direct request must survive.
            optional = page.evaluate("() => fdm.state.edits.findIndex(e => !e.id.startsWith('fit-'))")
            assert optional >= 0, "no optional suggestion"
            page.evaluate("() => document.querySelectorAll('#edits details').forEach((d) => (d.open = true))")  # Other ideas
            page.locator("#edits input").nth(optional).check()
            edits = page.evaluate("() => Object.fromEntries(fdm.state.edits.map(e => [e.id, e.selected]))")
            assert True in edits.values() and False in edits.values(), edits
            page.evaluate("""async () => {
              const r = await fdm.call('regions'), s = fdm.state, cls = s.options[s.active].face_classes;
              const ids = new Uint32Array(r.face_regions.slice().buffer), counts = new Map();
              for (let f = 0; f < cls.length; f++) if (cls[f] === 2) counts.set(ids[f], (counts.get(ids[f]) ?? 0) + 1);
              s.marks = r.regions.filter(r => r.area_mm2 >= 20 && counts.has(r.id)).map(r => ({ region: r.id, kind: 'clean' }));
            }""")
            page.evaluate("() => fdm.steps.go('extra')")
            run.act(lambda: page.click("#vol-suggest"))
            assert page.locator("#vol-suggest-list input").count() >= 2, "need two suggested boxes"
            page.locator("#vol-suggest-list input").first.check()
            box_choices = "() => Object.fromEntries(fdm.state.suggested.map(v => [JSON.stringify([v.kind, v.from.region ?? null, v.from.point ?? null, v.from.normal ?? null]), v.use]))"
            boxes = page.evaluate(box_choices)
            assert True in boxes.values() and False in boxes.values(), boxes
            with page.expect_download() as dl:
                page.evaluate("() => document.getElementById('dl-project').click()")
            out = OUT / "suggestion_ticks.json"
            dl.value.save_as(out)
            saved = json.loads(out.read_text(encoding="utf-8"))
            assert {k: saved["edit_selections"][k] for k in edits} == edits
            assert {k: v["use"] for k, v in saved["suggested_boxes"].items()} == boxes
            # Reorder saved boxes: their choices belong to their source, never to a list position.
            saved["suggested_boxes"] = dict(reversed(list(saved["suggested_boxes"].items())))
            out.write_text(json.dumps(saved), encoding="utf-8")
            run.act(lambda: click_sample(page, "mushroom"))
            run.act(lambda: page.set_input_files("#file", str(out)), 300_000)
            page.wait_for_function("() => document.getElementById('status').textContent.includes('opened the project')", timeout=300_000)
            assert page.evaluate("() => Object.fromEntries(fdm.state.edits.map(e => [e.id, e.selected]))") == edits
            assert page.evaluate(box_choices) == boxes
            assert page.locator("#edits input:checked").count() == sum(edits.values())
            assert page.locator("#vol-suggest-list input:checked").count() == sum(boxes.values())
            run.act(lambda: page.dispatch_event("#param-teardrop_angle_deg", "change"))
            run.act(lambda: page.click("#vol-suggest"))
            assert page.evaluate("() => Object.fromEntries(fdm.state.edits.map(e => [e.id, e.selected]))") == edits
            assert page.evaluate(box_choices) == boxes, "re-suggest lost box choices"
            # A pre-ticks file keeps today's defaults, including a selected direct size request.
            del saved["edit_selections"], saved["suggested_boxes"]
            old = OUT / "suggestion_ticks_old.json"
            old.write_text(json.dumps(saved), encoding="utf-8")
            run.act(lambda: page.set_input_files("#file", str(old)), 300_000)
            page.wait_for_function("() => document.getElementById('status').textContent.includes('opened the project')", timeout=300_000)
            defaults = page.evaluate("() => fdm.state.edits.filter(e => e.selected).map(e => e.id)")
            assert defaults and all(k.startswith("fit-") for k in defaults), defaults
            assert page.locator("#vol-suggest-list input").count() == 0, "old file inherited pending boxes"
            run.act(lambda: page.click("#vol-suggest"))
            assert page.locator("#vol-suggest-list input:checked").count() == 0
            return f"{len(edits)} design choices and {len(boxes)} box choices restored by key; old-file defaults kept"
        run.step("project keeps suggestion ticks and unticks", project_suggestion_ticks)

        def design():
            if not page.locator("input[name=mode][value=both]").is_checked():
                run.act(lambda: page.click("input[name=mode][value=both] >> xpath=.."))
            run.act(lambda: click_sample(page, "l_bracket"))
            options = page.evaluate("() => fdm.state.options")
            cards = page.locator("#options > li")
            for i, option in enumerate(options):
                if option.get("support_g") is None:
                    continue
                text = cards.nth(i).inner_text()
                assert option["support_summary"] in cards.nth(i).locator(".option-cost").inner_text(), text
                assert option["support_summary"] in option["reasons"], option
                assert "Needs supports" not in text, text
                assert ("No support" in text) == (not option["support_needed"]), text
            edits = page.locator("#edits label").all_inner_texts()
            assert any("Ø8.0" in e for e in edits), f"expected teardrop for the Ø8 hole, got {edits}"
            run.act(lambda: page.fill("#param-teardrop_angle_deg", "30") or page.dispatch_event("#param-teardrop_angle_deg", "change"))
            assert any("30° point" in e for e in page.locator("#edits label").all_inner_texts()), "teardrop angle input had no effect"
            page.click("#edits-all")
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
            run.act(lambda: click_sample(page, "test_block"))
            edits = page.evaluate("() => fdm.state.edits.map((e) => e.title)")
            assert sum("Thicken" in e for e in edits) == 2, f"expected two thin walls to thicken, got {edits}"
            # Tick all: the changes that fix a finding (the 0.30 mm wall too thin to print), not the other ideas.
            ops = page.evaluate("() => fdm.state.edits.filter((e) => e.source !== 'other').map((e) => e.op)")
            page.evaluate("() => fdm.steps.go('design')")  # previews show while Design changes is in view
            page.click("#edits-all")
            page.wait_for_timeout(800)
            assert "add" in ops and len(page.evaluate("() => fdm.viewer.ghosts.children.map((c) => c.material.color.getHexString())")) == len(ops)
            run.act(lambda: page.click("#apply-edits"))
            again = page.locator("#edits > li:not(.other-ideas) label").all_inner_texts()
            assert not any("Thicken" in e for e in again), f"walls still thin after thickening: {again}"
            return f"{edits} -> {again or 'nothing'} | findings: {findings(page)}"
        run.step("design edits that add material", added_material)

        def design_keeps_orientation():
            """T-353: a design change made with another orientation picked keeps that orientation."""
            run.act(lambda: click_sample(page, "test_block"))
            run.act(lambda: page.locator("#options > li > strong").nth(1).click())
            up = page.evaluate("() => fdm.state.options[fdm.state.active].up")
            page.click("#edits-all")
            assert page.locator("#edits input[type=checkbox]:checked").count(), "no design change ticked"
            run.act(lambda: page.click("#apply-edits"))
            now = page.evaluate("() => ({ up: fdm.state.options[fdm.state.active].up, version: fdm.state.version })")
            assert now["version"] == 1 and sum(a * b for a, b in zip(up, now["up"])) > 0.999, f"orientation {up} -> {now}"
            run.act(lambda: page.select_option("#version", "0"), 300_000)
            back = page.evaluate("() => fdm.state.options[fdm.state.active].up")
            assert sum(a * b for a, b in zip(up, back)) > 0.999, f"switching back to the original: {up} -> {back}"
            return f"stays on {[round(v, 2) for v in up]} after the change and back on the original"
        run.step("design change keeps the orientation", design_keeps_orientation)

        def versions_keep_loads():
            """T-355: loads, held faces, face marks and slicer settings stay on the part when a design change makes a
            new version and when switching back."""
            run.act(lambda: click_sample(page, "mushroom"))
            if page.locator("#design-on").is_visible():  # design changes off (run on its own)
                run.act(lambda: page.click("#design-on"))
            page.evaluate("""async () => {
              const r = await fdm.call("regions");
              fdm.state.faceRegions = new Uint32Array(r.face_regions.slice().buffer);
              fdm.state.regionInfo = r.regions;
              const flat = r.regions.map((x, i) => ({ ...x, i })).filter((x) => x.kind === "flat").sort((a, b) => b.area_mm2 - a.area_mm2);
              const cap = flat[0];
              const end = flat.find((x) => x.normal[0] * cap.normal[0] + x.normal[1] * cap.normal[1] + x.normal[2] * cap.normal[2] < -0.9 && x.area_mm2 < cap.area_mm2 / 2);
              const side = r.regions.findIndex((x, i) => i !== cap.i && i !== end.i && Math.abs(x.normal[2]) < 0.1);
              fdm.state.loads = [{ region: cap.i, kind: "fixed" }, { region: end.i, kind: "force", kg: 20, direction: "pull", case: 1 }];
              fdm.state.marks = [{ region: side, kind: "clean" }];
              fdm.state.overrides = { wall_loops: 5 };
            }""")
            where = lambda: page.evaluate("""() => ({ v: fdm.state.version, walls: fdm.state.overrides.wall_loops,
              loads: fdm.state.loads.map((l) => [l.kind, fdm.state.regionInfo[l.region].normal.map(Math.round)].join()),
              marks: fdm.state.marks.map((m) => [m.kind, fdm.state.regionInfo[m.region].normal.map(Math.round)].join()) })""")  # noqa: E731
            before = where()
            page.evaluate("() => fdm.steps.go('design')")
            page.evaluate("() => document.querySelectorAll('#edits details').forEach((d) => (d.open = true))")  # no findings: every change is an other idea
            for box in page.locator("#edits input[type=checkbox]").all():
                box.check()
            assert page.locator("#edits input[type=checkbox]:checked").count(), "no design change ticked"
            run.act(lambda: page.click("#apply-edits"), 300_000)
            changed = where()
            run.act(lambda: page.select_option("#version", "0"), 300_000)
            back = where()
            for got, v in ((changed, 1), (back, 0)):
                assert got == {**before, "v": v}, (before, got)
            return f"{len(before['loads'])} loads, {len(before['marks'])} mark, walls {before['walls']} kept on both versions"
        run.step("model versions keep loads, marks and settings", versions_keep_loads)

        def feature_confirm():
            run.act(lambda: click_sample(page, "l_bracket"))
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
            assert page.locator("#edits label", has_text="sliding fit").locator("input").is_checked(), "sized fit not ticked"
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
            assert page.locator("#edits label", has_text="Nut pocket for the M3 nut").locator("input").is_checked(), "requested nut pocket not ticked"
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
            shown = lambda: page.evaluate("() => fdm.viewer.fitView?.children.length ?? 0")  # noqa: E731
            assert shown() >= 2, "no measured width on the part"  # T-386: line + label
            value = float(row.locator("input").input_value())
            row.locator("input").fill(f"{value - 0.5:.1f}" if label.startswith("Slot") else f"{value + 0.5:.1f}")
            row.locator("input").dispatch_event("input")
            assert shown() >= 4, "no bars for the other part"  # + one bar (slot) or two walls (tab), with edges
            run.act(lambda: row.get_by_role("button", name="Size it").click())
            note = page.locator("#gaps li").first.locator("p.hint").inner_text()
            edits = page.locator("#edits label").all_inner_texts()
            assert any(("slot" in e or "tab" in e) and "fit" in e for e in edits), edits
            assert page.locator("#gaps li").first.get_by_role("button", name="Show in Design changes").count() == 1
            page.locator("#gaps li").first.get_by_role("button", name="Remove").click()
            page.wait_for_function("() => !document.querySelector('#gaps li')", timeout=120_000)
            return f"{label}: {note[:90]}"
        run.step("slots and flat gaps", flat_gap)

        why = lambda sel: page.evaluate(  # noqa: E731 - the visible reason line under a disabled button (T-402)
            """(sel) => { const b = document.querySelector(sel); const n = (b.closest('.row, .actions') ?? b).nextElementSibling;
              return { off: b.disabled, why: n && n.classList.contains('why-off') && !n.hidden ? n.textContent : '' }; }""", sel)

        def disabled_say_why():
            """T-402: every disabled button in the strength flow says why in text (phones show no tooltips)."""
            page.evaluate("() => fdm.steps.go('strength')")
            assert not page.evaluate("() => fdm.state.loads.length"), "test expects a part without loads here"
            for sel, words in (("#as-fixed", "Tap a face"), ("[data-next=load]", "held"), ("#run-strength", "where it's held and where the load goes"),
                               ("#run-dynamics", "Strength analysis"), ("#run-topology", "Strength analysis")):
                got = why(sel)
                assert got["off"] and words in got["why"], (sel, got)
            return why("#run-strength")["why"]
        run.step("disabled buttons say why", disabled_say_why)

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
            assert why("#as-fixed") == {"off": False, "why": ""}, why("#as-fixed")
            page.click("#as-fixed")
            assert why("[data-next=load]") == {"off": False, "why": ""}, why("[data-next=load]")
            assert "add a load" in why("#run-strength")["why"], why("#run-strength")
            return f"{info}; loads: {page.locator('#loads li').all_inner_texts()}"
        run.step("pick a face + mark fixed", pick_face)

        def load_cases():
            # Another visible face (different region) gets a load in case 1 and a heavier one in case 2.
            page.click("[data-next=load]")  # T-369: held is set, on to where the load goes
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
            # T-429: the lightest settings for the load run first (their own busy), then the strength check.
            page.wait_for_function("() => fdm.state.strength && fdm.state.loadSettings && Number(document.body.dataset.busy || 0) === 0", timeout=300_000)
            page.locator("#strength-result .strength-more").evaluate("e => e.open = true")
            text = page.locator("#strength-result").inner_text()
            assert "Weakest is case 2" in text, text
            headline = page.locator("#strength-result .verdict strong").first.inner_text()
            assert headline in ("Plenty of reserve", "Enough", "Marginal", "Likely to break"), headline
            return f"loads: {page.locator('#loads li').all_inner_texts()} | {text.splitlines()[0]}"
        run.step("strength with two load cases", load_cases)

        def settings_follow_load():
            """T-429: after a strength run the settings show the walls / infill the loads need, with a way back."""
            ls = page.evaluate("() => fdm.state.loadSettings")
            assert ls["met"] and ls["use"] and ls["values"] and ls["loadText"] == "For your loads" and ls["baseline"], ls
            page.evaluate("() => fdm.steps.go('settings')")
            rows = "() => Object.fromEntries([...document.querySelectorAll('#settings tr[data-setting]')].map(r => [r.dataset.setting, r.cells[2].innerText]))"
            note = page.locator("#load-settings-note")
            from_load = page.locator("#settings .setting-reason").filter(has_text="For your loads")
            if not from_load.count():  # the goal's values already hold it: nothing to change
                assert "already hold it" in note.inner_text(), note.inner_text()
                return note.inner_text()
            assert page.evaluate("() => fdm.state.strengthInputs === JSON.stringify(fdm.strengthInputs())"), "strength result is not for the settings shown"
            suggested = page.evaluate(rows)
            reason = from_load.first.inner_text()
            run.act(lambda: note.get_by_role("button", name="Keep the goal default").click(), 300_000)
            assert not page.evaluate("() => fdm.state.loadSettings.use")
            assert not page.locator("#settings .setting-reason").filter(has_text="For your loads").count()
            assert page.evaluate(rows) != suggested, "keeping the goal default did not change the settings"
            run.act(lambda: note.get_by_role("button", name="Use the values for your load").click(), 300_000)
            assert page.evaluate(rows) == suggested, "the load's settings did not come back"
            assert page.evaluate("() => fdm.state.strengthInputs === JSON.stringify(fdm.strengthInputs())"), "strength result is not for the settings shown"
            text = note.inner_text()
            assert "goal's settings" in text and " g" in text and " min" in text, text  # says what it costs or saves
            page.evaluate("() => fdm.steps.go('strength')")
            return f"{reason} | {text}"
        run.step("settings follow the load", settings_follow_load)

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
            # T-370: paint an area with the brush (a short drag over the face) instead of the whole face.
            page.click("label:has(#pick-paint)")
            camera = page.evaluate("() => fdm.viewer.camera.position.toArray()")
            page.mouse.move(x, y)
            page.mouse.down()
            for k in range(1, 9):
                page.mouse.move(x + 3 * k, y + k, steps=2)
            page.mouse.up()
            painted = page.evaluate("() => fdm.state.paint.length")
            assert painted > 1, f"the drag painted {painted} spot(s)"
            moved = max(abs(a - b) for a, b in zip(page.evaluate("() => fdm.viewer.camera.position.toArray()"), camera))
            assert moved < 0.5, "the drag turned the view instead of painting"
            assert page.evaluate("() => fdm.viewer.spots.children.at(-1).count") == painted, "painted spots not shown"
            page.fill("#kg", "4")
            page.click("#as-load")
            page.evaluate("() => { const r = document.querySelector('[name=pick-mode][value=face]'); r.checked = true; r.dispatchEvent(new Event('change')); }")  # back to whole faces (the panel is closed now)
            loads = page.locator("#loads li").all_inner_texts()
            assert any("(painted area)" in l for l in loads), loads
            assert page.evaluate("() => fdm.state.loads.at(-1).points.length") == painted
            run.act(lambda: page.click("#run-strength"), 300_000)
            text = page.locator("#strength-result").inner_text()
            headline = page.locator("#strength-result .verdict strong").first.inner_text()
            assert headline in ("Plenty of reserve", "Enough", "Marginal", "Likely to break"), text
            return f"loads: {loads} | {text.splitlines()[0]}"
        run.step("strength with a load on a painted area", spot_load)

        def edge_load():
            """T-371: the load snaps to the edge next to the tap and spreads along it; or sits at one point on it."""
            # A spot near a corner of a triangle on a loaded-able face: on flat faces those corners lie on its edges.
            x, y = page.evaluate("""() => {
              const v = fdm.viewer, s = fdm.state, held = s.loads[0].region, pos = v.part.geometry.getAttribute('position');
              const rect = v.renderer.domElement.getBoundingClientRect();
              for (let f = 0; f < pos.count / 3; f++) {
                if (s.faceRegions[f] === held) continue;
                const [a, b, c] = [0, 1, 2].map((k) => new v.camera.position.constructor().fromBufferAttribute(pos, f * 3 + k));
                const q = a.multiplyScalar(0.9).add(b.multiplyScalar(0.05)).add(c.multiplyScalar(0.05)).project(v.camera);
                const [x, y] = [rect.left + ((q.x + 1) / 2) * rect.width, rect.top + ((1 - q.y) / 2) * rect.height];
                const el = document.elementFromPoint(x, y), hit = v.pick({ clientX: x, clientY: y });
                if (el && el.tagName === 'CANVAS' && hit === f) return [x, y];
              }
              return [0, 0];
            }""")
            page.evaluate("() => { fdm.state.loads = fdm.state.loads.filter((l) => l.kind === 'fixed'); }")
            page.mouse.click(x, y)
            page.wait_for_selector("#pick-panel:not([hidden])")
            page.click("label:has(#pick-edge)")
            hint = page.locator("#edge-hint").inner_text()
            spots = page.evaluate("() => fdm.state.paint.length")
            assert hint.startswith("Along ") and spots > 1, (hint, spots)
            assert page.evaluate("() => fdm.viewer.spots.children.at(-1).count") == spots, "edge spots not shown"
            page.locator("#edge-point").check()
            assert page.evaluate("() => fdm.state.paint.length") == 1, "one point on the edge expected"
            page.locator("#edge-point").uncheck()
            page.fill("#kg", "3")
            page.click("#as-load")
            page.evaluate("() => { const r = document.querySelector('[name=pick-mode][value=face]'); r.checked = true; r.dispatchEvent(new Event('change')); }")
            loads = page.locator("#loads li").all_inner_texts()
            assert any("mm edge)" in l for l in loads), loads
            run.act(lambda: page.click("#run-strength"), 300_000)
            headline = page.locator("#strength-result .verdict strong").first.inner_text()
            assert headline in ("Plenty of reserve", "Enough", "Marginal", "Likely to break"), headline
            return f"{hint} | {headline}"
        run.step("strength with a load along an edge", edge_load)

        def compact_strength():
            page.set_viewport_size({"width": 1440, "height": 900})
            result = page.locator("#strength-result .verdict").last
            page.locator("#strength-result").scroll_into_view_if_needed()
            page.locator("#panel .panes").evaluate("e => e.scrollTop += 2")  # leave room for fractional pixel rounding
            for selector in ("strong", ".strength-band-bar", ".strength-weakest", ".strength-helps"):
                assert result.locator(selector).is_visible(), selector
            assert page.locator("#strength-result .strength-more > summary").is_visible()
            result.get_by_role("button", name="Show it").click()
            page.wait_for_function("() => !fdm.viewer.cameraMove", timeout=5000)
            assert page.evaluate("() => fdm.viewer.controls.target.distanceTo(fdm.viewer.camera.position.clone().set(...(fdm.state.strength.weak_marker?.point ?? fdm.state.strength.critical_point_print))) < 0.01"), "camera did not point at the weak spot"
            box = result.bounding_box()
            panel = page.locator("#panel .panes").bounding_box()
            assert box["height"] <= panel["height"] and box["y"] >= panel["y"] and box["y"] + box["height"] <= panel["y"] + panel["height"], (box, panel)
            page.screenshot(path=OUT / "strength-compact.png")
            page.set_viewport_size({"width": 1500, "height": 950})
            return f"result {box['height']:.0f}px, panel {panel['height']:.0f}px"
        run.step("strength result fits at 1440 x 900", compact_strength)

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
            page.locator("#strength-result .strength-more").evaluate("e => e.open = true")
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
            page.locator("#strength-result .strength-more").evaluate("e => e.open = true")
            note = page.locator(".fea-view").inner_text()
            assert "Finer grid" in note, note
            return f"{colours} coloured corners; {title}; section {cubes} cubes; {note.splitlines()[-1][:90]}"
        run.step("FEA view: legend, section, finer grid", fea_view)

        def shells():
            page.click("[data-stage=extra] .sa-head")
            page.locator(".shells:not(#detailed) summary").click()
            page.fill("#sh-walls", "6")
            run.act(lambda: page.dispatch_event("#sh-walls", "change"), 300_000)
            page.locator("#strength-result .strength-more").evaluate("e => e.open = true")
            page.wait_for_function("() => document.querySelector('#strength-result')?.innerText.includes('6 walls')", timeout=300_000)
            text = page.locator("#strength-result").inner_text()
            settings = page.locator("#settings").inner_text()
            assert "Walls changed by you" in settings, settings
            return text.splitlines()[1][:120]
        run.step("walls set by hand go into strength and the 3MF", shells)

        def hot_car():
            run.act(lambda: page.select_option("#use-temp", "70"), 300_000)
            page.locator("#strength-result .strength-more").evaluate("e => e.open = true")
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
            page.locator("#strength-result .strength-more").evaluate("e => e.open = true")
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
            page.locator("#strength-result .strength-more").evaluate("e => e.open = true")
            assert page.is_checked("#use-gcode"), page.locator("#status").inner_text()
            page.wait_for_function("() => document.querySelector('#strength-result')?.innerText.includes('as sliced in')", timeout=300_000)
            text = page.locator("#strength-result").inner_text()
            # The result also shows in the G-code's own card, where the user loaded it.
            headline = page.locator("#gcode-result strong").first.inner_text()
            assert headline in tuple(f"With your sliced print: {band}" for band in
                                     ("Plenty of reserve", "Enough", "Marginal", "Likely to break")), headline
            page.uncheck("#use-gcode")
            page.wait_for_function("() => !document.querySelector('#strength-result .strength-more')?.open", timeout=300_000)
            page.locator("#strength-result .strength-more").evaluate("e => e.open = true")
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

        def visible_setting_reasons():
            page.evaluate("() => fdm.steps.go('settings')")
            reasons = page.locator("#settings .setting-reason")
            assert reasons.count() > 0, "no setting reasons rendered"
            assert reasons.first.is_visible(), "setting reason needs hovering"
            assert all(text.strip() for text in reasons.all_inner_texts()), "empty reason line"
            assert "Hover a row" not in page.locator("#settings-block").text_content()
            assert page.locator("#settings tr[title]").count() == 0, "duplicate reason tooltip"
            viewport = page.viewport_size
            try:
                page.set_viewport_size({"width": 375, "height": 812})
                assert reasons.first.is_visible(), "setting reason hidden on phones"
                assert page.locator("#settings").evaluate(
                    "table => table.scrollWidth <= table.clientWidth && table.getBoundingClientRect().right <= innerWidth"
                ), "settings overflow phone width"
            finally:
                page.set_viewport_size(viewport)
            return f"{reasons.count()} visible reason lines; phone width fits"
        run.step("slicer setting reasons are visible", visible_setting_reasons)

        def settings_difference_export():
            import io
            import json
            import zipfile
            from xml.etree import ElementTree as ET

            had_strength = page.evaluate("() => !!fdm.state.strength")
            page.evaluate("() => fdm.steps.go('settings')")
            assert page.locator("#settings th").all_inner_texts() == ["Setting", "Usual (goal)", "For this part"]
            assert page.locator("#settings-unchanged").is_hidden(), "unchanged rows start open"
            page.get_by_role("button", name=re.compile(r"^Unchanged \(")).click()
            walls = page.locator('#settings tr[data-setting="wall_loops"] input')
            assert walls.is_checked(), "new part starts with a setting excluded"
            shell_walls = page.locator("#sh-walls").input_value()
            walls.uncheck()
            assert not walls.is_checked(), "render lost the unchecked choice"
            page.click("#undo")
            assert walls.is_checked(), "Undo did not restore the tick"
            page.click("#redo")
            assert not walls.is_checked(), "Redo did not restore the untick"
            assert page.locator("#sh-walls").input_value() == shell_walls, "unticking changed the estimate's wall assumption"
            assert page.locator("#settings-estimate-note").is_visible(), "unknown slicer settings need an estimate note"
            page.set_viewport_size({"width": 375, "height": 812})
            assert page.locator("#settings").evaluate("t => t.scrollWidth <= t.clientWidth"), "difference table overflows phone width"
            page.set_viewport_size({"width": 1500, "height": 950})
            page.evaluate("() => fdm.steps.go('download')")
            with page.expect_download(timeout=300_000) as dl:
                page.evaluate("() => document.getElementById('dl-3mf').click()")
            archive = zipfile.ZipFile(io.BytesIO(Path(dl.value.path()).read_bytes()))
            for name, omitted in [("Metadata/model_settings.config", "wall_loops"),
                                  ("Metadata/Slic3r_PE_model.config", "perimeters")]:
                keys = {m.attrib["key"] for m in ET.fromstring(archive.read(name)).iter("metadata")}
                assert omitted not in keys, (name, keys)
                assert "layer_height" in keys, "checked setting was dropped too"
            page.evaluate("() => fdm.steps.go('settings')")
            assert not walls.is_checked(), "changing steps lost the unchecked choice"
            page.evaluate("() => fdm.steps.go('download')")
            with page.expect_download() as saved:
                page.click("#dl-project")
            project_path = OUT / "settings_choices.json"
            saved.value.save_as(str(project_path))
            assert "wall_loops" in json.loads(project_path.read_text())["excluded_settings"]
            run.act(lambda: page.set_input_files("#file", str(project_path)), 300_000)
            page.evaluate("() => fdm.steps.go('settings')")
            if page.locator("#settings-unchanged").is_hidden():
                page.get_by_role("button", name=re.compile(r"^Unchanged \(")).click()
            assert not walls.is_checked(), "opening a project lost the unchecked choice"
            walls.check()
            assert page.locator("#settings-estimate-note").is_hidden()
            # Reopening a project drops cached analyses; leave the strength fixture ready for the next step.
            if had_strength:
                page.evaluate("() => fdm.steps.go('strength')")
                run.act(lambda: page.click("#run-strength"), 300_000)
                page.evaluate("() => fdm.steps.go('settings')")
            return "goal defaults shown; unchanged rows folded; unticked walls absent from both 3MF configs and kept in projects; phone fits"
        run.step("settings differences and per-row export choice", settings_difference_export)

        def import_print_settings():
            import gzip
            import json
            profiles = json.loads(gzip.decompress((ROOT / "core/fdmcore/data/orca_profiles.json.gz").read_bytes()))["profiles"]
            parent = next(name for name, p in profiles.items() if p["kind"] == "process" and "layer_height" in p["data"])
            orca = OUT / "print-profile.json"
            orca.write_text(json.dumps({"name": "My process", "inherits": parent, "wall_loops": "4"}))
            prusa = OUT / "print-profile.ini"
            prusa.write_text("perimeters = 4\nfill_density = 15%\nfill_pattern = gyroid\nlayer_height = 0.2\n")
            page.evaluate("() => fdm.steps.go('settings')")
            for path in (orca, prusa):
                page.set_input_files("#import-print-profile", str(path))
                page.wait_for_function("name => document.querySelector('#print-profile-status').textContent.includes(name)", arg="My process" if path == orca else "Your print settings")
                folded = page.locator("#settings-unchanged")
                if folded.is_hidden():
                    page.get_by_role("button", name=re.compile(r"^Unchanged \(")).click()
                walls = page.locator('#settings tr[data-setting="wall_loops"]')
                assert "yours: 4" in walls.inner_text(), walls.inner_text()
                goal = page.locator('#settings tr[data-setting="sparse_infill_pattern"]')
                assert goal.locator("input").is_disabled(), goal.inner_text()
                assert goal.locator("xpath=..").get_attribute("id") == "settings-unchanged"
                if path == orca:
                    assert "Can't find its parent" not in page.locator("#print-profile-status").inner_text()
            page.locator("#settings").screenshot(path=str(OUT / "imported-print-settings.png"))
            page.click("#remove-print-profile")
            return "Orca inherited profile and Prusa INI show yours: 4; goal-only pattern unchanged"
        run.step("import print settings", import_print_settings)

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
            # T-415: after a 3MF download the next steps show, and "Got it" closes them.
            assert page.evaluate("() => !document.getElementById('after-dl').hidden"), "no next steps after the download"
            page.click("#failed-after-dl")
            symptom = page.evaluate("""async () => {
              const { symptoms } = await fdm.call("diagnose", {});
              for (const s of symptoms) {
                const r = await fdm.call("diagnose", { symptom: s.id, findings: fdm.state.check.findings });
                if (r.on_your_part.length) return s.id;
              }
              return null;
            }""")
            assert symptom, "the checked sample has no matching symptom"
            page.locator(f'[data-symptom="{symptom}"]').click()
            page.locator("#failed-result .mine li").first.wait_for(timeout=60_000)
            assert page.get_by_text("Flagged on your part", exact=True).is_visible()
            page.click("#failed-close")
            page.evaluate("() => document.getElementById('after-dl-close').click()")
            assert page.evaluate("() => document.getElementById('after-dl').hidden"), "next steps did not close"
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
            assert any(band in text for band in ("Plenty of reserve", "Enough", "Marginal", "Likely to break")), "no strength result for the new best orientation"
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
        # Switched off in the app for now (T-379): its step comes back with the card.
        def box_heat_off():
            assert page.locator("#box-heat").is_hidden(), "electronics box card shown"
            return "hidden"
        run.step("electronics box heat is switched off", box_heat_off)

        def report_with_pictures():
            with page.expect_download(timeout=180_000) as dl:
                run.act(lambda: page.click("#dl-report"))
            out = OUT / "report.html"
            dl.value.save_as(out)
            html = out.read_text(encoding="utf-8")
            pictures = html.count('<img src="data:image/jpeg')
            assert pictures >= 3 and "Strength analysis" in html and "Warping" in html and "Slicer settings" in html, (pictures, html[:300])
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
            run.act(lambda: click_sample(page, "mushroom"))
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
            run.act(lambda: click_sample(page, "enclosure"))
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
            # Record before clicking: the restarting message may be gone by the time act returns.
            page.evaluate("""() => {
                window.cancelStatuses = [];
                const status = document.getElementById('status');
                const observer = new MutationObserver(() => {
                    cancelStatuses.push({ text: status.textContent,
                        working: !document.getElementById('working').hidden,
                        progress: !document.getElementById('progress').hidden,
                        busy: Number(document.body.dataset.busy || 0) });
                    if (status.textContent === 'Ready') observer.disconnect();
                });
                observer.observe(status, { childList: true });
            }""")
            run.act(lambda: page.click("#working-cancel"), 240_000)
            statuses = page.evaluate("() => cancelStatuses")
            assert [s["text"] for s in statuses] == ["Cancelled. Getting ready again…", "Ready"], statuses
            assert statuses[0]["working"] and statuses[0]["progress"] and statuses[0]["busy"] > 0, statuses
            assert not statuses[1]["working"] and not statuses[1]["progress"] and statuses[1]["busy"] == 0, statuses
            assert page.locator("#working-cancel").is_enabled(), "Cancel stayed disabled after restart"
            status = page.locator("#status").inner_text()
            assert status == "Ready", status
            # The restarted engine must have the part again: re-orienting works.
            run.act(lambda: page.click("input[name=goal][value=balanced] >> xpath=.."))
            after = page.evaluate("() => fdm.state.options.length")
            assert after == before, f"{before} options before, {after} after restart"
            kept = page.evaluate("() => fdm.call('session_state')")
            assert kept["tags"] == told["tags"] and kept["fits"] == told["fits"], f"lost on cancel: {told} -> {kept}"
            return f"{' -> '.join(s['text'] for s in statuses)} | kept {len(kept['tags'])} answer(s), {len(kept['fits'])} fit(s) | after restart: {after} options, {len(findings(page))} findings"
        run.step("progress + cancel", cancel)

        def lighter_shape():
            run.act(lambda: click_sample(page, "hook"))
            page.evaluate("async () => { const r = await fdm.call('regions'); const flat = r.regions.filter((x) => x.kind === 'flat');"
                          " fdm.state.loads = [{ region: flat[0].id, kind: 'fixed' }, { region: flat[1].id, kind: 'force', kg: 2 }]; }")
            # T-377: its own step in Advanced, after the design changes.
            level = page.evaluate("() => fdm.steps.level")
            page.click("input[name=level][value=advanced] >> xpath=..")
            order = page.evaluate("() => fdm.steps.steps")
            assert order.index("lighter") == order.index("extra") + 1, order
            assert not page.locator("[data-pane=design] #run-topology").count(), "still inside Design changes"
            page.evaluate("() => fdm.steps.go('lighter')")
            page.evaluate("() => { const k = document.getElementById('topo-keep'); k.value = '50'; k.oninput(); }")
            # Loads set directly (not by clicking faces), so the button isn't refreshed: enable it as renderLoads would.
            run.act(lambda: page.evaluate("() => { const b = document.getElementById('run-topology'); b.disabled = false; b.click(); }"), 900_000)
            text = page.locator("#topology-result").inner_text()
            assert " g instead of " in text and "CAD" in text, text
            # Shown by default on the faded part, without changing the part; gone on other steps.
            shown = "() => Boolean(fdm.viewer.lighter?.visible) && fdm.viewer.part.material.opacity < 1"
            assert page.evaluate(shown), "the lighter shape isn't shown after the run"
            assert "lighter" not in page.evaluate("() => fdm.state.name"), "the run replaced the part"
            page.locator("#topology-result input[type=checkbox]").uncheck()
            assert not page.evaluate(shown) and page.evaluate("() => fdm.viewer.part.material.opacity") == 1, "switch off keeps it"
            page.locator("#topology-result input[type=checkbox]").check()
            page.evaluate("() => fdm.steps.go('design')")
            assert not page.evaluate(shown), "the shape stays on another step"
            page.evaluate("() => fdm.steps.go('lighter')")
            assert page.evaluate(shown), "the shape doesn't come back on its step"
            versions = page.evaluate("() => fdm.state.versions.length")
            run.act(lambda: page.get_by_role("button", name="Use it as a new version").click(), 300_000)
            assert page.evaluate("() => fdm.state.versions.length") == versions + 1 and "lighter_50" in page.evaluate("() => fdm.state.name")
            assert not page.evaluate("() => Boolean(fdm.viewer.lighter)"), "the old shape stays on the new version"
            page.click(f"input[name=level][value={level}] >> xpath=..")
            return text.splitlines()[0]
        run.step("suggest a lighter shape", lighter_shape)

        def copy_report():
            run.act(lambda: click_sample(page, "hook"))
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
            run.act(lambda: page.locator("#options > li > strong").nth(1).click())
            up = page.evaluate("() => fdm.state.options[fdm.state.active].up")
            page.evaluate("async () => { const r = await fdm.call('regions'); const flat = r.regions.filter((x) => x.kind === 'flat');"
                          " fdm.state.loads = [{ region: flat[0].id, kind: 'fixed' }, { region: flat[1].id, kind: 'force', kg: 2 }];"
                          " fdm.state.overrides = { wall_loops: '5' }; }")
            with page.expect_download(timeout=120_000) as dl:
                page.evaluate("() => document.getElementById('dl-project').click()")
            out = OUT / "project.json"
            dl.value.save_as(out)
            run.act(lambda: page.select_option("#use-temp", "-20"))
            run.act(lambda: click_sample(page, "mushroom"))
            run.act(lambda: page.set_input_files("#file", str(out)), 300_000)
            # Several engine calls in a row (load, orient, check, the project's orientation): wait for the last step.
            page.wait_for_function("() => document.getElementById('status').textContent.includes('opened the project')", timeout=300_000)
            got = page.evaluate("() => ({ label: fdm.state.options[0].label, up: fdm.state.options[0].up, loads: fdm.state.loads.length,"
                                " walls: fdm.state.overrides.wall_loops, name: fdm.state.name })")
            assert got["label"] == "From the project" and got["loads"] == 2 and got["walls"] == "5", got
            assert all(abs(a - b) < 1e-6 for a, b in zip(got["up"], up)) and got["name"].startswith("hook"), (got, up)
            assert page.input_value("#use-temp") == "", "room-temperature project did not reset to Indoors"
            # The opposite direction must still restore a saved non-room temperature.
            import json

            project = json.loads(out.read_text(encoding="utf-8"))
            project["profile"]["use_temp_c"] = 35
            warm = OUT / "project_warm.json"
            warm.write_text(json.dumps(project), encoding="utf-8")
            run.act(lambda: page.set_input_files("#file", str(warm)), 300_000)
            page.wait_for_function("() => document.getElementById('status').textContent.includes('opened the project')", timeout=300_000)
            assert page.input_value("#use-temp") == "35", "project temperature was not restored"
            run.act(lambda: page.select_option("#use-temp", ""))
            return f"{out.stat().st_size // 1024} kB, {got}; restored Indoors and 35 C"
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
                run.act(lambda: page.locator("#options > li > strong").nth(1).click())
            if not page.locator("input[name=mode][value=both]").is_checked():
                run.act(lambda: page.click("input[name=mode][value=both] >> xpath=.."))
            boxes = page.locator("#edits label")
            for i in range(boxes.count()):  # keep only the teardrop
                boxes.nth(i).locator("input").set_checked("Teardrop" in boxes.nth(i).inner_text())
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
                run.act(lambda: page.locator("#options > li > strong").nth(ups.index(up)).click())
            seen = findings(page)
            assert not any("Sideways hole prints oval" in f for f in seen), f"teardrop missing in the written STEP: {seen}"
            return f"{out.stat().st_size // 1024} kB STEP, {text.count('ADVANCED_FACE')} faces; reloaded: {page.locator('#status').inner_text()}"
        run.step("STEP in, design change, STEP out", step_round_trip)


        def test_design_change():
            # Mushroom held by its cap and pulled on the stem: the weak spot is the inside corner at the stem.
            run.act(lambda: click_sample(page, "mushroom"))
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
            run.act(lambda: click_sample(page, "mushroom"))
            side = page.evaluate("() => fdm.state.options.findIndex((o) => Math.abs(o.up[2]) < 0.1)")
            assert side >= 0, page.evaluate("() => fdm.state.options.map((o) => o.up)")
            if side != page.evaluate("() => fdm.state.active"):
                run.act(lambda: page.locator("#options > li > strong").nth(side).click())
            page.evaluate("""async () => {
              const r = await fdm.call("regions");
              fdm.state.faceRegions = new Uint32Array(r.face_regions.slice().buffer);
              fdm.state.regionInfo = r.regions;
              const flat = r.regions.map((x, i) => ({ ...x, i })).filter((x) => x.kind === "flat").sort((a, b) => b.area_mm2 - a.area_mm2);
              const cap = flat[0];
              const end = flat.find((x) => x.normal[0] * cap.normal[0] + x.normal[1] * cap.normal[1] + x.normal[2] * cap.normal[2] < -0.9 && x.area_mm2 < cap.area_mm2 / 2);
              fdm.state.loads = [{ region: cap.i, kind: "fixed" }, { region: end.i, kind: "force", kg: 50, direction: "pull", case: 1 }];
              document.getElementById("run-strength").disabled = false;
            }""")
            page.evaluate("() => fdm.steps.go('strength')")
            run.act(lambda: page.click("#run-strength"), 300_000)
            if page.evaluate("() => document.querySelector('input[name=mode][value=print]').checked"):
                run.act(lambda: page.click("input[name=mode][value=both] >> xpath=.."))
            page.evaluate("() => fdm.steps.go('design')")
            # Suggest again with the strength result (the rounds at the weak spot): a size change re-runs them.
            run.act(lambda: page.dispatch_event("#param-stress_chamfer_mm", "change"), 300_000)
            page.evaluate("() => document.querySelectorAll('#edits details').forEach((d) => (d.open = true))")  # other ideas too
            items = page.locator("#edits li:has(> label)")  # in the order of fdm.state.edits
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
            run.act(lambda: click_sample(page, "l_bracket"))
            page.context.set_offline(True)
            try:
                page.reload()
                run.booted()
                status = run.act(lambda: click_sample(page, "mushroom"))
                assert page.locator("#options > li").count() >= 2, "no orientations offline"
            finally:
                page.context.set_offline(False)
            return f"offline: {status}"
        run.step("works offline after a visit", offline)

        def solids_and_snap():
            run.act(lambda: click_sample(page, "l_bracket"))  # last: it changes the model
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
            run.act(lambda: click_sample(page, "l_bracket"))
            page.evaluate("async () => { const up = fdm.state.options[fdm.state.active].up;"
                          " const hole = (await fdm.call('design', { up })).features.find((f) => f.type === 'hole');"
                          " const r = await fdm.call('set_fastener', { region: hole.region, up, size: 'M3', use: 'insert' });"
                          " fdm.state.fastenerNotes = { [hole.region]: { size: 'M3', use: 'insert', text: r.explanation } }; }")
            with page.expect_download(timeout=120_000) as dl:
                page.evaluate("() => document.getElementById('dl-project').click()")
            out = OUT / "screws_project.json"
            dl.value.save_as(out)
            assert '"use": "insert"' in out.read_text(encoding="utf-8").replace('":"', '": "'), "no screw in the project file"
            run.act(lambda: click_sample(page, "mushroom"))
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
            # Tick only the side slot through its checkbox, as a user does (setting state alone leaves Apply off).
            page.evaluate("() => fdm.steps.go('design')")
            ids = page.evaluate("() => fdm.state.edits.map((e) => e.id)")
            boxes = page.locator("#edits li input[type=checkbox]")
            for i, edit_id in enumerate(ids):
                boxes.nth(i).set_checked(edit_id.startswith("magnet-slot"))
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
                run.act(lambda: page.locator("#options > li > strong").nth(i).click(), 300_000)
            assert page.evaluate("() => !document.getElementById('fine-for').hidden"), "no thinner-layers option for a 10° roof"
            text = page.locator("#fine-text").inner_text()
            layer = page.evaluate("() => fdm.state.check.settings.find(s => s.key === 'layer_height')")
            assert float(layer["value"]) == 0.2, layer
            assert "thin layers only from 10 to 17.05 mm" in layer["reason"], layer
            assert page.evaluate("() => fdm.state.check.stats.print_time_min > 0")
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
            page.evaluate("() => document.getElementById('failed-start').click()")  # the start step may not be shown here
            page.locator('[data-symptom="rough_overhang"]').click(timeout=60_000)
            page.wait_for_function("() => document.querySelector('#failed-result .mine li')", timeout=60_000)
            mine = page.locator("#failed-result .mine li").all_inner_texts()
            assert any("Shallow slope" in m for m in mine), mine
            assert page.locator('[data-symptom="rough_overhang"] img').evaluate("i => i.complete && i.naturalWidth > 0"), "no symptom picture"
            page.click("#failed-close")
            return mine[0][:100]
        run.step("my print failed: the flagged slope comes first", print_failed)

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
            run.act(lambda: click_sample(page, "l_bracket"))
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
            run.act(lambda: click_sample(page, "test_block"))
            assert not page.evaluate("() => document.getElementById('dl-grip').checked"), "grip box kept for another part"
            return "suggested for knob.stl, fuzzy skin in the 3MF, off for the next part"
        run.step("grip texture on a knob", grip_texture)

        def printer_tolerance():
            """T-385: one card for the printer's tolerance: the gaps in use, the fit test, and the hole compensation
            (a slicer setting in the 3MF)."""
            run.act(lambda: click_sample(page, "l_bracket"))
            page.evaluate("() => fdm.steps.go('fits')")
            page.wait_for_function("() => document.getElementById('tol-text').textContent.length > 0", timeout=60_000)
            text = page.locator("#tol-text").inner_text()
            assert "Typical gaps" in text and "fit test" in text, text
            page.fill("#hole-comp", "0.05")
            page.dispatch_event("#hole-comp", "change")
            assert page.evaluate("() => fdm.state.overrides.xy_hole_compensation") == "0.05"
            return text
        run.step("printer tolerance card", printer_tolerance)

        def plate_copies():
            # Four copies of the test block: how many fit, the plate's time, the notes, four items in the 3MF (T-349).
            import io
            import zipfile

            run.act(lambda: click_sample(page, "test_block"))
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
            run.act(lambda: click_sample(page, "l_bracket"))
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
            run.act(lambda: click_sample(page, "l_bracket"))
            run.act(lambda: page.select_option("#material", "petg"), 300_000)
            page.evaluate("() => fdm.steps.go('download')")
            with page.expect_download(timeout=300_000) as dl:
                page.evaluate("() => document.getElementById('dl-share').click()")
            shared = OUT / dl.value.suggested_filename
            dl.value.save_as(shared)
            html = shared.read_text(encoding="utf-8")
            assert shared.name == "l_bracket_share.html" and "The part is inside this file" in html and 'id="fdm-check-project"' in html
            run.act(lambda: click_sample(page, "test_block"))
            run.act(lambda: page.select_option("#material", "pla"), 300_000)
            run.act(lambda: page.set_input_files("#file", str(shared)), 300_000)
            page.wait_for_function("() => document.getElementById('status').textContent.includes('opened the project')", timeout=300_000)
            got = page.evaluate("() => ({ name: fdm.state.name, material: document.getElementById('material').value })")
            assert got["name"].startswith("l_bracket") and got["material"] == "petg", got
            return f"{shared.stat().st_size // 1024} kB, opened again: {got}"
        run.step("share: report with the part inside", share_file)

        def strength_scale():
            # T-401: a lightly loaded bracket must not look dangerous just because it has a peak.
            run.act(lambda: click_sample(page, "l_bracket"))
            page.evaluate("""async () => {
              const r = await fdm.call("regions");
              fdm.state.faceRegions = new Uint32Array(r.face_regions.slice().buffer);
              fdm.state.regionInfo = r.regions;
              const held = r.regions.reduce((a, b) => a.normal[0] < b.normal[0] ? a : b);
              const top = r.regions.filter((r) => r.normal[2] > 0.99).sort((a, b) => b.area_mm2 - a.area_mm2)[0];
              fdm.state.loads = [{ region: held.id, kind: "fixed" }, { region: top.id, kind: "force", kg: 5, direction: [0, 0, -1] }];
              document.getElementById("run-strength").disabled = false;
            }""")
            page.evaluate("() => fdm.steps.go('strength')")
            run.act(lambda: page.click("#run-strength"), 300_000)
            page.locator("#strength-result .strength-more").evaluate("e => e.open = true")
            page.locator(".fea-view select").first.select_option("fi")
            stretch = page.get_by_label("Stretch the colours", exact=True)
            assert not stretch.is_checked(), "strength scale should start fixed"
            top_label = page.locator("#legend .band span").first
            assert top_label.inner_text() == "100 %+", top_label.inner_text()
            assert page.locator("#legend .band-0").inner_text() == "0 %"
            need = page.evaluate("() => fdm.state.strength.required_safety")
            assert f"×{need:g} safety" in page.locator("#legend small").inner_text()
            assert page.evaluate("""async () => {
              const { fieldColor } = await import('./viewer.js');
              const colour = (v) => fieldColor(v, undefined, true);
              return !colour(0.9999).equals(colour(1)) && colour(1).equals(colour(2));
            }"""), "fixed colours must turn red at 100%, with higher values clamped"

            def colour_counts(section=False):
                return page.evaluate("""async section => {
                  const { fieldColor } = await import('./viewer.js');
                  const v = fdm.viewer, result = fdm.state.strength;
                  const colours = section ? v.section.instanceColor : v.field.mesh.geometry.getAttribute('color');
                  const floats = (b) => new Float32Array(b.slice().buffer);
                  let values = floats(section ? result.elements.fi : result.vertex_fi);
                  if (section) {
                    const positions = v.field.mesh.geometry.getAttribute('position').array;
                    let low = Infinity, high = -Infinity;
                    for (let i = 2; i < positions.length; i += 3) {
                      low = Math.min(low, positions[i]); high = Math.max(high, positions[i]);
                    }
                    const cut = Number([...document.querySelectorAll('.fea-view input[type=range]')].at(-1).value);
                    const at = low + cut * (high - low), centers = floats(result.elements.centers);
                    values = values.filter((_, i) => v.toShown([...centers.subarray(3 * i, 3 * i + 3)])[2] <= at);
                  }
                  let red = 0, blue = 0, mismatched = Number(values.length !== colours.count);
                  for (let i = 0; i < colours.count; i++) {
                    const r = colours.getX(i), g = colours.getY(i), b = colours.getZ(i);
                    if (r > 0.9 && g < 0.2 && b < 0.2) red++;
                    if (b > r) blue++;
                    const expected = fieldColor(Math.fround(values[i] * result.required_safety) / 100, undefined, true);
                    if (Math.max(Math.abs(r - expected.r), Math.abs(g - expected.g), Math.abs(b - expected.b)) > 1e-6) mismatched++;
                  }
                  return { red, blue, total: colours.count, mismatched };
                }""", section)

            strong = colour_counts()
            assert strong["red"] == 0 and strong["blue"] > strong["total"] / 2 and strong["mismatched"] == 0, strong
            key = page.evaluate("() => fdm.viewer.fieldKey")
            surface = page.evaluate("() => fdm.viewer.field.mesh.uuid")
            stretch.check()
            stretched_label = top_label.inner_text()
            peak = page.evaluate("() => Math.round(fdm.state.strength.fi_max_pct * fdm.state.strength.required_safety)")
            assert stretched_label == f"{peak} %" and stretched_label != "100 %+", stretched_label
            assert page.evaluate("() => fdm.viewer.fieldKey") != key, "stretch did not refresh the surface"
            assert page.evaluate("() => fdm.viewer.field.mesh.uuid") != surface, "stretch did not rebuild the surface"
            stretch.uncheck()
            page.locator(".fea-view select").nth(1).select_option("z")
            cut = colour_counts(section=True)
            assert cut["total"] > 0 and cut["mismatched"] == 0, cut
            page.locator(".fea-view select").nth(1).select_option("")
            page.locator(".fea-view select").first.select_option("vm")
            assert not stretch.is_visible(), "stretch switch should only show for strength used"
            page.locator(".fea-view select").first.select_option("fi")
            page.evaluate("() => { fdm.state.loads.find((l) => l.kind === 'force').kg = 500; }")
            run.act(lambda: page.click("#run-strength"), 300_000)
            page.locator("#strength-result .strength-more").evaluate("e => e.open = true")
            assert top_label.inner_text() == "100 %+"
            overloaded = colour_counts()
            assert overloaded["red"] > 0 and overloaded["mismatched"] == 0, overloaded
            page.locator(".fea-view select").nth(1).select_option("z")
            overloaded_cut = colour_counts(section=True)
            assert overloaded_cut["red"] > 0 and overloaded_cut["mismatched"] == 0, overloaded_cut
            page.locator(".fea-view select").nth(1).select_option("")
            return f"5 kg: {strong}; stretched top {stretched_label}; 500 kg: red weak spot"
        run.step("strength colours: fixed allowed load and optional stretch", strength_scale)

        def load_at_an_angle():
            """T-372: no "hanging down (−Z as modelled)"; "at an angle" is chosen before placing the load, relative
            to the tapped face, with the arrow shown on the part while it's set."""
            run.act(lambda: click_sample(page, "mushroom"))
            page.evaluate("() => fdm.steps.go('strength')")
            assert "down" not in page.eval_on_selector_all("#dir option", "os => os.map((o) => o.value)")
            xy = page.evaluate("""() => { const v = fdm.viewer; const n = v.part.geometry.getAttribute('position').count / 3;
              for (let f = 0; f < n; f++) { const [x, y] = v.screenPointOfFace(f); const el = document.elementFromPoint(x, y);
                if (el && el.tagName === 'CANVAS' && v.pick({ clientX: x, clientY: y }) === f) return [x, y]; } return null; }""")
            page.click("[data-stage=load] .sa-head")
            page.mouse.click(*xy)
            page.wait_for_selector("#pick-panel:not([hidden])", timeout=300_000)  # the first click loads the face tools
            arrows = page.evaluate("() => fdm.viewer.symbols?.children.length ?? 0")
            assert arrows > 0, "no preview arrow on the tapped face"  # T-369: shown before it's added
            page.select_option("#dir", "angle")
            page.dispatch_event("#dir", "input")
            page.fill("#dir-lean", "90")
            page.dispatch_event("#dir-lean", "input")
            assert page.locator("#dir-angle").is_visible()
            assert page.evaluate("() => fdm.viewer.symbols.children.length") == arrows, "the preview arrow should turn, not be added"
            normal = page.evaluate("() => fdm.state.regionInfo[fdm.state.selectedRegion].normal")
            page.click("#as-load")
            d = page.evaluate("() => fdm.state.loads.at(-1).direction")
            assert isinstance(d, list) and abs(sum(a * b for a, b in zip(d, normal))) < 0.01, (d, normal)  # along the face
            assert page.evaluate("() => fdm.state.previewLoad") is None
            assert "90° lean" in page.locator("#loads").inner_text(), page.locator("#loads").inner_text()
            return f"lean 90° on a face with normal {[round(v, 2) for v in normal]}: {d}"
        run.step("load at an angle to the face", load_at_an_angle)

        def offer_stronger_orientation():
            """T-368: the first failing strength check on a part offers a stronger orientation, once."""
            run.act(lambda: click_sample(page, "mushroom"))
            standing = page.evaluate("() => fdm.state.options.findIndex((o) => Math.abs(o.up[2]) > 0.9)")
            if standing != page.evaluate("() => fdm.state.active"):
                run.act(lambda: page.locator("#options > li > strong").nth(standing).click())
            page.evaluate("""async () => {
              const r = await fdm.call("regions");
              fdm.state.faceRegions = new Uint32Array(r.face_regions.slice().buffer);
              fdm.state.regionInfo = r.regions;
              const flat = r.regions.map((x, i) => ({ ...x, i })).filter((x) => x.kind === "flat").sort((a, b) => b.area_mm2 - a.area_mm2);
              const cap = flat[0];
              const end = flat.find((x) => x.normal[0] * cap.normal[0] + x.normal[1] * cap.normal[1] + x.normal[2] * cap.normal[2] < -0.9 && x.area_mm2 < cap.area_mm2 / 2);
              fdm.state.loads = [{ region: cap.i, kind: "fixed" }, { region: end.i, kind: "force", kg: 30, direction: "pull", case: 1 }];
              document.getElementById("run-strength").disabled = false;
            }""")
            page.evaluate("() => fdm.steps.go('strength')")
            run.act(lambda: page.click("#run-strength"), 300_000)
            offer = page.locator("#strength-result .turn-offer")
            assert offer.count() == 1, page.locator("#strength-result").inner_text()[:200]
            text = offer.locator("strong").inner_text()
            run.act(lambda: offer.get_by_role("button", name="Turn it").click(), 300_000)
            up = page.evaluate("() => fdm.state.options[fdm.state.active].up")
            assert abs(up[2]) < 0.01, up  # lying on its side now
            assert page.locator("#strength-result .turn-offer").count() == 0  # once per part
            return text
        run.step("failing strength check offers a stronger orientation", offer_stronger_orientation)

        def turn_by_hand():
            """T-380: the user can turn the part themselves: 90° steps or any angle, as a "Your own" option."""
            run.act(lambda: click_sample(page, "l_bracket"))
            page.evaluate("() => fdm.steps.go('orient')")
            page.locator("#turn-own > summary").click()
            up = lambda: page.evaluate("() => fdm.state.options[fdm.state.active].up")  # noqa: E731
            before = up()
            run.act(lambda: page.click("[data-tip=flip]"))
            flipped = up()
            assert sum(a * b for a, b in zip(before, flipped)) < -0.99, (before, flipped)
            assert page.evaluate("() => fdm.state.options[fdm.state.active].label") == "Your own"
            page.fill("#tip-forward", "90")
            run.act(lambda: page.click("#tip-use"))
            tipped = up()
            assert abs(sum(a * b for a, b in zip(flipped, tipped))) < 0.01, (flipped, tipped)
            assert page.evaluate("() => fdm.state.options.filter((o) => o.label === 'Your own').length") == 1
            # T-354: another material keeps the orientation the user chose (it went back to the best one).
            run.act(lambda: page.select_option("#material", "petg"), 300_000)
            kept = up()
            label = page.evaluate("() => fdm.state.options[fdm.state.active].label")
            run.act(lambda: page.select_option("#material", "pla"), 300_000)
            assert label == "Your own" and sum(a * b for a, b in zip(tipped, kept)) > 0.99, (label, tipped, kept)
            return f"{[round(v) for v in before]} -> upside down {[round(v) for v in flipped]} -> tipped {[round(v) for v in tipped]}, kept after a material change"
        run.step("turn the part by hand", turn_by_hand)

        def settings_lead():
            """T-375: Simple and Basic put the settings first: steps across the top, the 3D view a card on the
            right that says what the step changes, with Before / After for how the part lies."""
            page.click("input[name=level][value=basic] >> xpath=..")
            page.evaluate("() => fdm.steps.go('orient')")
            box = lambda sel: page.locator(sel).bounding_box()  # noqa: E731
            nav, panel, view = box("#steps"), box("#panel"), box("#viewer")
            assert nav["width"] > 1000 and nav["y"] + nav["height"] <= panel["y"] + 1, (nav, panel)
            assert view["x"] >= panel["x"] + panel["width"] - 1 and panel["width"] > view["width"], (panel, view)
            assert "which face lies on the bed" in page.inner_text("#view-changes")
            page.click("[data-compare=before]")
            assert page.evaluate("() => !!fdm.viewer.before && !fdm.viewer.part.visible")
            assert not page.is_visible("#view-label") and page.is_visible(".view-before-label")
            page.click("[data-compare=after]")
            assert page.evaluate("() => !fdm.viewer.before && fdm.viewer.part.visible")
            page.click("[data-compare=before]")
            page.evaluate("() => fdm.steps.go('problems')")  # another step shows the part as it prints again
            assert page.evaluate("() => !fdm.viewer.before && fdm.viewer.part.visible")
            assert page.is_hidden("#view-compare")
            page.click("input[name=level][value=advanced] >> xpath=..")
            panel, view = box("#panel"), box("#viewer")
            assert view["x"] < panel["x"] and page.is_hidden("#view-changes"), (panel, view)
            page.click("input[name=level][value=expert] >> xpath=..")
            return "Basic: settings left, 3D card right, Before / After works; Advanced keeps the big 3D view"
        run.step("Simple and Basic: settings first, 3D beside them", settings_lead)

        def hidden_gap_from_side():
            """T-363: a hidden gap clicked on a side face goes into the wall (it was placed along the surface or
            refused), and its outline is drawn over the part so it can be seen inside it."""
            run.act(lambda: click_sample(page, "mushroom"))
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

        def move_and_suggest_boxes():
            """T-392: drag a box by its arrow in the 3D view, and let the app suggest boxes."""
            x, y = page.evaluate(
                "() => { const v = fdm.viewer; const n = v.part.geometry.getAttribute('position').count / 3;"
                " for (let f = 0; f < n; f += Math.max(1, Math.floor(n / 50))) { const [x, y] = v.screenPointOfFace(f);"
                " const el = document.elementFromPoint(x, y); if (el && el.tagName === 'CANVAS' && v.pick({clientX: x, clientY: y}) >= 0) return [x, y]; } return [0, 0]; }"
            )
            page.select_option("#vol-kind", "support")
            page.click("#vol-pick")
            run.act(lambda: page.mouse.click(x, y))
            before = page.evaluate("() => fdm.state.volumes[0]")
            page.locator("#vol-list li").first.get_by_role("button", name="Move", exact=True).click()
            assert page.evaluate("() => fdm.viewer.handles.children.length") == 3, "no move arrows"
            # The x arrow on screen: from the arrows' middle to its tip; drag the tip further out.
            mid, tip = page.evaluate("""() => {
              const v = fdm.viewer, a = v.handles.children[0], r = v.renderer.domElement.getBoundingClientRect();
              const screen = (p) => { const q = p.clone().project(v.camera); return [r.left + (q.x + 1) / 2 * r.width, r.top + (1 - q.y) / 2 * r.height]; };
              return [screen(v.handles.userData.mid), screen(a.localToWorld(a.children[1].position.clone()))];
            }""")
            to = [tip[0] + 0.6 * (tip[0] - mid[0]), tip[1] + 0.6 * (tip[1] - mid[1])]
            camera = page.evaluate("() => fdm.viewer.camera.position.toArray()")
            page.mouse.move(*tip)
            page.mouse.down()
            page.mouse.move(*to, steps=8)
            page.mouse.up()
            page.wait_for_function("() => fdm.state.volumes[0].offset", timeout=60_000)
            after = page.evaluate("() => fdm.state.volumes[0]")
            moved = [b - a for a, b in zip(before["lo"], after["lo"])]
            assert abs(moved[0]) > 0.4 or abs(moved[1]) > 0.4, f"box not moved: {moved}"
            size = lambda v: [h - l for l, h in zip(v["lo"], v["hi"])]
            assert all(abs(a - b) < 1e-6 for a, b in zip(size(before), size(after))), "the box changed size"
            turned = max(abs(a - b) for a, b in zip(page.evaluate("() => fdm.viewer.camera.position.toArray()"), camera))
            assert turned < 0.5, "the drag turned the view"
            page.locator("#vol-list li").first.get_by_role("button", name="Remove").click()
            # Suggestions: the face that needs the most support, marked "keep clean", gets a no-support box.
            # Lying its best way the mushroom needs none: take the orientation with the most support.
            most = page.evaluate("() => fdm.state.options.map((o) => o.face_classes.filter((c) => c === 2).length).reduce((b, n, i, a) => (n > a[b] ? i : b), 0)")
            page.evaluate("() => fdm.steps.go('orient')")
            run.act(lambda: page.locator("#options > li > strong").nth(most).click())
            page.evaluate("() => fdm.steps.go('extra')")
            page.evaluate("""() => {
              const s = fdm.state, cls = s.options[s.active].face_classes, n = new Map();
              for (let f = 0; f < cls.length; f++) if (cls[f] === 2) n.set(s.faceRegions[f], (n.get(s.faceRegions[f]) ?? 0) + 1);
              const region = [...n].sort((a, b) => b[1] - a[1])[0][0];
              s.marks = [{ region, kind: 'clean' }];
            }""")
            run.act(lambda: page.click("#vol-suggest"))
            kinds = page.evaluate("() => fdm.state.suggested.map((v) => v.kind)")
            assert "blocker" in kinds, f"no no-support box suggested: {kinds} | {page.locator('#status').inner_text()}"
            assert page.locator("#vol-suggest-list input:checked").count() == 0, "suggested boxes arrived ticked"
            assert page.locator("#vol-use").is_disabled()
            page.click("#vol-all")
            assert page.locator("#vol-suggest-list input:checked").count() == len(kinds)
            page.click("#vol-none")
            assert page.locator("#vol-suggest-list input:checked").count() == 0
            page.click("#vol-all")
            dashed = page.evaluate("() => fdm.viewer.zones.children.some((c) => c.isLineSegments && c.material.isLineDashedMaterial)")
            assert dashed, "suggestions not drawn dashed"
            page.click("#vol-use")
            assert "blocker" in page.evaluate("() => fdm.state.volumes.map((v) => v.kind)"), "suggestions not added"
            assert page.locator("#vol-suggested").is_hidden()
            page.evaluate("() => { fdm.state.marks = []; }")
            return f"moved by {[round(m, 1) for m in moved]} mm; suggested {kinds}"
        run.step("move boxes and suggested boxes", move_and_suggest_boxes)

        def less_text():
            """T-382: explanations fold behind an ⓘ next to their heading; "Explain" walks through what is shown."""
            page.evaluate("() => fdm.steps.go('printing')")
            folded = page.evaluate("() => [...document.querySelectorAll('.info-text')].filter((p) => p.hidden).length")
            assert folded >= 15, f"only {folded} explanations folded away"
            btn = page.locator("[data-pane=printing] .info-btn").first
            text = page.locator("#" + btn.get_attribute("aria-controls"))
            assert text.is_hidden()
            btn.click()
            assert text.is_visible() and btn.get_attribute("aria-expanded") == "true", "the ⓘ doesn't open its text"
            btn.click()
            assert text.is_hidden()
            page.click("#explain")
            assert page.locator("#tour").is_visible()
            count = page.locator("#tour-count").inner_text()
            stops = int(count.split(" of ")[1])
            assert stops >= 4, count
            page.click("#tour-next")
            page.click("#tour-next")
            assert page.locator("#tour-count").inner_text().startswith("3 of"), page.locator("#tour-count").inner_text()
            page.keyboard.press("Escape")
            assert page.locator("#tour").is_hidden(), "Esc doesn't end the walk-through"
            return f"{folded} explanations behind an ⓘ; walk-through with {stops} stops here"
        run.step("less text: ⓘ explanations and the Explain walk-through", less_text)

        def simple_result():
            """T-374 / T-397: one primary per step, with missing work started by Show my result."""
            simple_flow_to_3mf(run, "mushroom")
            page.evaluate("() => { window.simpleCompleted = { options: fdm.state.options, check: fdm.state.check }; }")
            items = page.locator("#compute-steps li")

            # Set loads without running Check strength. Show my result must supply that missing work.
            page.evaluate("""async () => {
              const r = await fdm.call('regions'), s = fdm.state;
              s.faceRegions = new Uint32Array(r.face_regions.slice().buffer);
              s.regionInfo = r.regions;
              const flat = r.regions.map((x, i) => ({ ...x, i })).filter((x) => x.kind === 'flat').sort((a, b) => b.area_mm2 - a.area_mm2);
              const cap = flat[0];
              const end = flat.find((x) => x.normal[0] * cap.normal[0] + x.normal[1] * cap.normal[1] + x.normal[2] * cap.normal[2] < -0.9 && x.area_mm2 < cap.area_mm2 / 2);
              s.loads = [{ region: cap.i, kind: 'fixed' }, { region: end.i, kind: 'force', kg: 0.01, direction: 'pull', case: 1 }];
            }""")
            page.click("#step-back")

            def show_missing_result():
                assert page.locator("#step-next").inner_text() == "Show my result", "a load is set: not a skip"
                page.click("#step-next")
                assert page.evaluate("() => fdm.steps.current") == "result"
                assert page.locator("#compute-steps li.working").is_visible(), "missing work has no progress list"
                assert_primary_at_foot(page, "dl-3mf")

            run.act(show_missing_result, 300_000)
            assert page.evaluate("() => !!fdm.state.strength"), "missing strength check was skipped"
            assert items.evaluate_all("lis => lis.every(li => li.className === 'done')")
            assert page.evaluate("() => fdm.state.options === simpleCompleted.options && fdm.state.check === simpleCompleted.check"), "orientation or printability was needlessly repeated"
            page.evaluate("() => { window.simpleStrength = fdm.state.strength; }")
            before = run.done()
            page.click("#step-back")
            page.click("#step-next")
            assert run.done() == before, "a current strength result was run again"
            assert page.evaluate("() => fdm.state.strength === simpleStrength")
            # An old strength result is no longer sufficient after the load changes.
            page.click("#step-back")
            page.evaluate("() => { fdm.state.loads[1].kg = 0.02; }")
            run.act(show_missing_result, 300_000)
            assert page.evaluate("() => fdm.state.strength !== simpleStrength"), "changed load reused stale strength"

            for level in ("basic", "advanced", "expert"):
                page.click(f"input[name=level][value={level}] >> xpath=..")
                assert page.locator("#blk-download #dl-3mf.primary.big").count() == 1, f"download not restored in {level}"
                assert page.locator("#run-strength.primary").count() == 1, f"strength action not restored in {level}"
                assert page.locator(".sa-next.primary").count() == 2, f"strength stages not restored in {level}"
            page.evaluate("() => { delete window.simpleCompleted; delete window.simpleStrength; }")
            return " | ".join(items.all_inner_texts())
        run.step("simple: one primary and missing work on Result", simple_result)
        run.step("phone: content first", lambda: check_phone_content_first(run))

        def tick_and_recheck():
            """T-364: ideas are ticked and applied together with one "Re-check", not one button each."""
            run.act(lambda: click_sample(page, "mushroom"))
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
            recheck = page.get_by_role("button", name="Re-check with the ticked changes")
            assert recheck.is_disabled(), "re-check enabled with nothing ticked"
            recheck_why = page.locator("#strength-options .why-off")
            assert recheck_why.is_visible() and "Tick" in recheck_why.inner_text(), "no reason under the re-check button"
            page.locator("#strength-options summary", has_text="Slicer settings").evaluate("s => s.parentElement.open = true")
            ticks = page.locator("#strength-options li.slicer input[type=checkbox]")
            assert ticks.count() >= 2, f"{ticks.count()} slicer ideas"
            ticks.nth(0).check()
            ticks.nth(1).check()
            assert page.locator("#strength-options li.slicer input:checked").count() == 2 and recheck.is_enabled()
            assert not recheck_why.is_visible(), "reason still shown with changes ticked"
            before = page.evaluate("() => ({ ...fdm.state.overrides })")
            run.act(lambda: recheck.click(), 300_000)
            after = page.evaluate("() => ({ ...fdm.state.overrides })")
            changed = {k: v for k, v in after.items() if before.get(k) != v}
            assert len(changed) >= 2, f"settings from two ideas not both applied: {changed}"
            assert page.evaluate("() => !!fdm.state.strength"), "strength not checked again"
            # T-365: the list is worked out again for the new settings, and after a material change.
            groups = page.locator("#strength-options .option-group")
            assert groups.count(), "no ideas listed after the re-check"
            base = page.evaluate("() => fdm.state.strength.safety_factor")
            run.act(lambda: page.select_option("#material", "petg"), 300_000)
            assert page.evaluate("() => fdm.state.strength?.material").lower().startswith("petg"), "strength not checked again for PETG"
            assert groups.count(), "no ideas listed after the material change"
            run.act(lambda: page.select_option("#material", "pla"), 300_000)
            return f"applied together: {changed}; list shown again after the re-check (×{base:.1f}) and for PETG"
        run.step("make it stronger: tick several, re-check once", tick_and_recheck)

        def design_ideas_tested():
            """T-367: design ideas under "How can I make it stronger?" are tested on copies straight away and shown
            with their result; nothing is applied."""
            run.act(lambda: click_sample(page, "mushroom"))
            page.evaluate("""async () => {
              const r = await fdm.call("regions");
              fdm.state.faceRegions = new Uint32Array(r.face_regions.slice().buffer);
              fdm.state.regionInfo = r.regions;
              const flat = r.regions.map((x, i) => ({ ...x, i })).filter((x) => x.kind === "flat").sort((a, b) => b.area_mm2 - a.area_mm2);
              const cap = flat[0];
              const end = flat.find((x) => x.normal[0] * cap.normal[0] + x.normal[1] * cap.normal[1] + x.normal[2] * cap.normal[2] < -0.9 && x.area_mm2 < cap.area_mm2 / 2);
              fdm.state.loads = [{ region: cap.i, kind: "fixed" }, { region: end.i, kind: "force", kg: 50, direction: "pull", case: 1 }];
              document.getElementById("run-strength").disabled = false;
            }""")
            page.evaluate("() => fdm.steps.go('strength')")
            run.act(lambda: page.click("#run-strength"), 300_000)
            versions = page.evaluate("() => fdm.state.versions.length")
            run.act(lambda: page.get_by_role("button", name="How can I make it stronger?").click(), 600_000)
            page.wait_for_function("() => document.querySelector('#design-trials li')", timeout=600_000)
            tested = page.locator("#design-trials li").all_inner_texts()
            assert tested and all("×" in t for t in tested), tested
            assert page.evaluate("() => fdm.state.versions.length") == versions, "a tested change was applied"
            return f"{len(tested)} tested: " + " | ".join(t.splitlines()[0] for t in tested)
        run.step("make it stronger: design ideas tested straight away", design_ideas_tested)

        # T-423's timing limit applies to every shipped sample, including those outside the main tour.
        for sample in sorted((ROOT / "web" / "samples").glob("*.stl")):
            if sample.stem in [*SAMPLES, "feature_test"]:
                continue

            def extra_thumbnails(sample=sample):
                page.evaluate("() => fdm.steps.go('part')")
                run.act(lambda: page.set_input_files("#file", str(sample)), 300_000)
                return check_thumbnails(page)

            run.step(f"thumbnails {sample.stem}", extra_thumbnails)
        # Every shipped part, including fixtures outside the sample buttons, must keep visual context.
        page.click("input[name=level][value=expert] >> xpath=..")
        for sample in sorted((ROOT / "web" / "samples").glob("*.stl")):
            def finding_views(sample=sample):
                page.evaluate("() => fdm.steps.go('part')")
                run.act(lambda: page.set_input_files("#file", str(sample)), 300_000)
                page.evaluate("() => fdm.steps.go('problems')")
                return check_finding_views(page)

            run.step(f"finding views {sample.stem}", finding_views)

        run.step("saved projects: two parts in a row", lambda: check_saved_projects(run))
        run_extra_parts(run, args.parts)
        browser.close()
    return run


def main():
    sys.stdout.reconfigure(errors="replace")  # results quote UI text (🔒, ×); Windows consoles may not have them
    ap = argparse.ArgumentParser()
    ap.add_argument("parts", nargs="*", type=Path)
    ap.add_argument("--quick", action="store_true",
                    help="short smoke check (~2 min): boot, phone start screen, Simple flow to 3MF, level switch, one finding")
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
    elapsed = time.monotonic() - started
    print(f"\n{len(run.failures)} failure(s) in {elapsed:.1f}s; screenshots in {OUT}")
    if run.console:
        print("console errors:", *run.console[:10], sep="\n  ")
    sys.exit(1 if run.failures else 0)


if __name__ == "__main__":
    main()
