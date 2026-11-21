"""The new UI's store, flow and shell (T-530, plan research/redesign-2026-11/PLAN-COMBINED.md §1.2, §1.3, §1.7, §2).

state.js and flow.js are pure modules, tested under Node; the shell is walked in a headless browser (skipped when
Node or a browser isn't there)."""

from __future__ import annotations

import functools
import http.server
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


def run_js(body: str):
    """Run ``body`` with S = state.js and F = flow.js. They are copied to .mjs: web/ has no package.json, so Node
    would read .js as CommonJS (the browser loads them as modules)."""
    if not NODE:
        pytest.skip("node not installed")
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    for name in ("state", "flow"):
        (tmp / f"{name}.mjs").write_text((ROOT / "web" / f"{name}.js").read_text(encoding="utf-8"), encoding="utf-8")
    script = (f'import * as S from "{(tmp / "state.mjs").as_uri()}"; import * as F from "{(tmp / "flow.mjs").as_uri()}";\n'
              f"const out = await (async () => {{ {body} }})();\nconsole.log(JSON.stringify(out));")
    proc = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True)
    shutil.rmtree(tmp, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_results_go_stale_when_their_inputs_change():
    out = run_js("""
      const s = S.createStore();
      s.setResult("optimize", {n: 1}, s.deps("model", "profile", "goal", "environment", "loads", "depth"));
      const fresh = s.isStale("optimize");
      s.set({ step: "setup" }, { undoable: false });  // navigating isn't an input
      const afterNav = s.isStale("optimize");
      s.set({ goal: "fast" });
      const afterGoal = s.isStale("optimize");
      s.setResult("check", {}, s.deps("model", "option"));
      s.set({ environment: { temp: "car", tempC: 70, touches: [] } });
      return { fresh, afterNav, afterGoal, checkAfterEnv: s.isStale("check"), missing: s.isStale("nothing") };
    """)
    assert out == {"fresh": False, "afterNav": False, "afterGoal": True, "checkAfterEnv": False, "missing": False}


def test_undo_redo_covers_what_the_user_builds():
    out = run_js("""
      const s = S.createStore();
      s.set({ holds: [{ region: 3, kind: "screw" }] });
      s.set({ loads: [{ region: 5, kg: 2 }] });
      s.set({ goal: "strong" });  // a setting: not on the undo stack
      s.setResult("optimize", 1, s.deps("loads"));
      s.undo();
      const afterUndo = { loads: s.get().loads.length, holds: s.get().holds.length, goal: s.get().goal, stale: s.isStale("optimize") };
      s.undo();
      const second = s.get().holds.length;
      const none = s.undo();
      s.redo(); s.redo();
      return { afterUndo, second, none, redone: [s.get().holds.length, s.get().loads.length], canRedo: s.canRedo() };
    """)
    assert out["afterUndo"] == {"loads": 0, "holds": 1, "goal": "strong", "stale": True}
    assert out["second"] == 0 and out["none"] is False
    assert out["redone"] == [1, 1] and out["canRedo"] is False


def test_flow_skips_strength_steps_for_quality_and_no_loads():
    out = run_js("""
      const base = S.initialState();
      const ids = (st) => F.visibleSteps(st).map((x) => x.id);
      return {
        all: ids(base),
        quality: ids({ ...base, goal: "quality" }),
        noLoads: ids({ ...base, noLoads: true }),
        nextFromEnvQuality: F.nextStep("env", { ...base, goal: "quality" }),
        backFromAnalyseNoLoads: F.prevStep("analyse", { ...base, noLoads: true }),
        blocked: F.canLeave("part", base),
        rail: F.railSteps({ ...base, goal: "quality", visited: ["part", "setup", "env"] }, "env").map((r) => r.state),
      };
    """)
    assert out["all"] == ["part", "setup", "env", "holds", "loads", "details", "analyse", "options", "sim", "problems", "done"]
    assert out["quality"] == ["part", "setup", "env", "analyse", "options", "sim", "problems", "done"]
    assert out["noLoads"] == ["part", "setup", "env", "holds", "analyse", "options", "sim", "problems", "done"]
    assert out["nextFromEnvQuality"] == "analyse" and out["backFromAnalyseNoLoads"] == "holds"
    assert out["blocked"] == "step.part.needPart"
    assert out["rail"][:4] == ["done", "done", "current", "skipped"]


def test_rail_marks_a_done_step_stale():
    out = run_js("""
      const s = S.createStore();
      s.setResult("optimize", 1, s.deps("goal"));
      s.set({ goal: "light" });
      const st = { ...s.get(), visited: ["part", "setup", "env", "holds", "loads", "details", "analyse", "options", "sim"] };
      return F.railSteps(st, "sim", (r) => s.isStale(r)).filter((r) => r.stale).map((r) => r.id);
    """)
    assert out == ["analyse", "options"]


# ---------- the shell in a browser ----------

class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with sync_api.sync_playwright() as p:
        browser = None
        for kw in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                browser = p.chromium.launch(**kw)
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("no browser for Playwright")
        pg = browser.new_page(viewport={"width": 1280, "height": 720})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"http://127.0.0.1:{server.server_address[1]}/web/new.html")
        pg.wait_for_selector("body[data-ready]")
        yield pg
        assert not errors, errors
        browser.close()
    server.shutdown()


def test_walk_the_empty_flow(page):
    assert page.locator(".app-header").bounding_box()["y"] == 0
    page.get_by_role("button", name="Optimize your print").click()
    assert page.locator("#step-title").inner_text() == "Part"
    assert page.get_by_role("button", name="Next").is_disabled(), "Next works without a part"
    # What the later steps will set (T-531 …): a part, a printer, a hold, a load, a picked option.
    page.evaluate("""() => fdmUI.store.set({ part: { name: "hook.stl", size: [20, 34, 50] }, profile: { printer: "bambu_p1s", material: "", nozzle: "", nozzles: [] },
      holds: [{ region: 1 }], loads: [{ region: 2, kg: 5 }], optimized: 1, option: { id: 1 } })""")
    seen = []
    foot = []
    for _ in range(12):
        seen.append(page.locator("#step-title").inner_text())
        assert page.evaluate("() => document.activeElement.id") == "step-title", "focus didn't move to the step title"
        nxt = page.get_by_role("button", name="Next")
        if not nxt.count():
            break
        box = nxt.bounding_box()
        foot.append((round(box["x"] + box["width"]), round(box["y"])))
        nxt.click()
    assert seen == ["Part", "Setup", "Environment", "Holds", "Loads", "Load details", "Analyse", "Options", "Simulation", "Problems", "Done"]
    work = [f for s, f in zip(seen, foot) if s not in ("Part", "Options")]
    assert len(set(work)) == 1, f"Next moved between steps: {work}"
    page.get_by_role("button", name="Back").click()
    assert page.locator("#step-title").inner_text() == "Problems"
    assert page.locator(".app-header .part-pill").inner_text().startswith("hook.stl")


def test_quality_skips_and_undo_buttons(page):
    page.evaluate("""() => { fdmUI.store.set({ goal: "quality" }); fdmUI.go("env"); }""")
    page.get_by_role("button", name="Next").click()
    assert page.locator("#step-title").inner_text() == "Analyse"
    assert page.locator(".ui-rail li.is-skipped").count() == 0  # skipped steps are left out of the rail
    undo = page.get_by_role("button", name="Undo")
    page.evaluate("""() => fdmUI.store.set({ holds: [] })""")
    assert undo.is_enabled()
    page.keyboard.press("Control+z")
    assert page.evaluate("() => fdmUI.store.get().holds.length") == 1
