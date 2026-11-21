"""Setup and Environment steps of the new UI (T-532, plan §5, §6, §16.4, §16.7).

args.js is pure logic, unit-tested under Node (and cross-checked against Python's optimizer.allowed_materials
and api.optimize's signature); setup.js and environment.js are driven in a headless browser against web/new.html."""

from __future__ import annotations

import functools
import http.server
import inspect
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from fdmcore import api, optimizer
from fdmcore.profile import Profile, presets

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


def run_js(body: str):
    """Run ``body`` with S = state.js and A = steps/args.js copied to .mjs (web/ has no package.json)."""
    if not NODE:
        pytest.skip("node not installed")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "state.mjs").write_text((ROOT / "web" / "state.js").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp / "args.mjs").write_text((ROOT / "web" / "steps" / "args.js").read_text(encoding="utf-8"), encoding="utf-8")
    script = (
        f'import * as S from "{(tmp / "state.mjs").as_uri()}";\n'
        f'import * as A from "{(tmp / "args.mjs").as_uri()}";\n'
        f"const out = await (async () => {{ {body} }})();\n"
        "console.log(JSON.stringify(out));"
    )
    proc = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True)
    shutil.rmtree(tmp, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def catalog_fixture(owned: list[str] | None = None) -> dict:
    """Small real catalog slice taken straight from presets.yaml (same shape as api.get_presets)."""
    full = presets()
    cat = {
        "printers": {k: full["printers"][k] for k in ("bambu_p1s", "bambu_x1c", "prusa_mk4s")},
        "materials": full["materials"],
        "nozzles": full["nozzles"],
    }
    if owned is not None:
        cat["owned"] = owned
    return cat


# ---------- pure JS logic under Node ----------

def test_optimize_args_matches_api_signature_and_maps_all_fields():
    cat = catalog_fixture(owned=["petg", "pla"])
    out = run_js(f"""
      const catalog = {json.dumps(cat)};
      const base = S.initialState();
      const locked = A.optimizeArgs({{
        ...base,
        catalog,
        profile: {{ printer: "bambu_p1s", material: "petg", nozzle: "0.6", nozzles: ["0.4", "0.6"] }},
        goal: "strong",
        change: "both",
        environment: {{ temp: "sun", tempC: 50, touches: ["water", "outdoors"] }},
        holds: [{{ kind: "fixed", region: 1 }}],
        loads: [{{ kind: "force", region: 2, n: 40 }}],
        details: {{ duration: "always", vibration: "no" }},
      }});
      const unlocked = A.optimizeArgs({{
        ...base,
        catalog,
        profile: {{ printer: "bambu_p1s", material: "", nozzle: "", nozzles: [] }},
        goal: "quality",
        change: "design",
        environment: {{ temp: "indoors", tempC: 20, touches: [] }},
        holds: [{{ kind: "fixed", region: 1 }}],
        loads: [{{ kind: "force", region: 2, n: 40 }}],
      }});
      return {{ locked, unlocked }};
    """)
    sig = set(inspect.signature(api.optimize).parameters)
    assert set(out["locked"]) <= sig
    assert set(out["unlocked"]) <= sig

    lk = out["locked"]
    assert lk["goal"] == "strong"
    assert lk["change"] == "both"
    assert lk["profile"] == {"printer": "bambu_p1s", "nozzle": "0.6", "use_temp_c": 50, "constant_load": True}
    assert lk["constraints"] == {"material": "petg", "nozzle": "0.6"}
    assert lk["nozzles"] == ["0.4", "0.6"]
    assert lk["owned"] == ["petg", "pla"]
    assert lk["environment"] == {"temp_c": 50, "touches": ["water", "outdoors"]}
    assert len(lk["loads"]) == 2

    ul = out["unlocked"]
    assert ul["goal"] == "quality"
    assert ul["change"] == "design"
    assert ul["constraints"] == {}
    assert ul["nozzles"] == ["0.2", "0.4", "0.6", "0.8", "1.0"]
    assert ul["loads"] is None  # quality skips strength


def test_evaluate_materials_matches_python_allowed_materials():
    cat = catalog_fixture()
    cases = [
        ("bambu_p1s", 20, [], ""),
        ("bambu_p1s", 50, ["water", "outdoors"], ""),
        ("bambu_x1c", 70, [], "pla"),
        ("bambu_p1s", 20, ["solvent"], ""),
        ("bambu_x1c", 20, ["skin"], ""),
    ]
    js_res = run_js(f"""
      const catalog = {json.dumps(cat)};
      const cases = {json.dumps(cases)};
      return cases.map(([printer, tempC, touches, material]) =>
        A.evaluateMaterials(catalog, {{ printer, material }}, {{ temp: "custom", tempC, touches }}));
    """)
    for (printer, temp_c, touches, locked), res in zip(cases, js_res):
        py_keep, _, _ = optimizer.allowed_materials(
            Profile.from_dict({"printer": printer}),
            {"temp_c": temp_c, "touches": touches},
            None,
            None,
        )
        assert {m["id"] for m in res["suitable"]} == set(py_keep), (printer, temp_c, touches)
    # Hot car + locked PLA triggers a conflict with heat reason.
    assert js_res[2]["lockedConflict"]["id"] == "pla"
    assert js_res[2]["lockedConflict"]["reasons"][0]["kind"] == "heat"
    # Skin contact leaves only unverified built-ins (PA-CF is poor and dropped).
    assert js_res[4]["hasVerified"] is False


# ---------- Setup and Environment in a headless browser ----------

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


def test_loading_state_before_catalog_arrives(page):
    page.evaluate("() => { fdmUI.store.set({ part: { name: 'l_bracket.stl', size: [60, 30, 40], kind: 'stl' } }); fdmUI.go('setup'); }")
    assert "Loading" in page.locator(".panel-body").inner_text()


def test_setup_controls_locks_nozzles_manage_and_step_warning(page):
    cat = catalog_fixture(owned=["petg", "asa"])
    page.evaluate(
        """(cat) => {
          fdmUI.store.set({
            catalog: cat,
            part: { name: "l_bracket.stl", size: [60, 30, 40], kind: "stl" },
            profile: { printer: null, material: "", nozzle: "", nozzles: [] },
            goal: "balanced",
            change: "slicer",
          });
          fdmUI.go("setup");
        }""",
        cat,
    )
    # No raw browser controls (§1.5).
    assert page.locator("select, details, input[type=checkbox], input[type=radio]").count() == 0
    assert page.get_by_role("button", name="Next").is_disabled()

    # Printer searchable Select -> Bambu Lab P1S.
    printer_btn = page.get_by_role("button", name="Printer")
    printer_btn.click()
    page.locator(".ui-select__pop:not([hidden]) .ui-select__search").fill("P1S")
    page.locator(".ui-select__pop:not([hidden]) .ui-select__option", has_text="Bambu Lab P1S").click()
    assert page.evaluate("() => fdmUI.store.get().profile.printer") == "bambu_p1s"
    assert "Bambu Lab P1S" in page.locator(".stage-note").inner_text()
    assert page.get_by_role("button", name="Next").is_enabled()

    # All printer nozzle ToggleChips ticked by default; unticking 1.0 removes it from profile.nozzles.
    nozzle_chips = page.locator(".step-nozzles [aria-pressed]")
    assert nozzle_chips.count() == 5
    for i in range(5):
        assert nozzle_chips.nth(i).get_attribute("aria-pressed") == "true"
    page.locator(".step-nozzles [data-nozzle='1.0']").click()
    assert page.evaluate("() => fdmUI.store.get().profile.nozzles") == ["0.2", "0.4", "0.6", "0.8"]

    # Material Select: defaults "Decide later", owned materials listed first with "Owned" badge, pick = clearable hard lock.
    mat_btn = page.get_by_role("button", name="Material")
    assert "Decide later" in mat_btn.inner_text()
    mat_btn.click()
    opts = page.locator(".ui-select__pop:not([hidden]) .ui-select__option")
    # Index 0 is "Decide later"; owned PETG and ASA come immediately next.
    assert "PETG" in opts.nth(1).inner_text() and "Owned" in opts.nth(1).inner_text()
    assert "ASA" in opts.nth(2).inner_text() and "Owned" in opts.nth(2).inner_text()
    opts.filter(has_text="PLA").first.click()
    assert page.evaluate("() => fdmUI.store.get().profile.material") == "pla"
    clear_mat = page.get_by_role("button", name="Clear Material")
    assert clear_mat.is_visible()
    clear_mat.click()
    assert page.evaluate("() => fdmUI.store.get().profile.material") == ""

    # Nozzle Select: pick = hard lock, clear ✕ returns to "Decide later".
    noz_btn = page.get_by_role("button", name="Nozzle")
    assert "Decide later" in noz_btn.inner_text()
    noz_btn.click()
    page.locator(".ui-select__pop:not([hidden]) .ui-select__option", has_text="0.6 mm").click()
    assert page.evaluate("() => fdmUI.store.get().profile.nozzle") == "0.6"
    page.get_by_role("button", name="Clear Nozzle").click()
    assert page.evaluate("() => fdmUI.store.get().profile.nozzle") == ""

    # Goal ChoiceCards: only selected card displays its one-line description.
    goal_group = page.locator("[role=radiogroup][aria-label='What matters most?']")
    assert goal_group.locator(".ui-cards__line:visible").count() == 1
    goal_group.get_by_role("radio", name="Strong", exact=True).click()
    assert page.evaluate("() => fdmUI.store.get().goal") == "strong"
    assert goal_group.locator(".ui-cards__line:visible").count() == 1

    # "What can you change?" + STEP warning on STL (warning only, never blocks).
    change_group = page.locator("[role=radiogroup][aria-label='What can you change?']")
    assert page.locator("[data-notice='step-warning']").count() == 0
    change_group.get_by_role("radio", name="The design").click()
    warn = page.locator("[data-notice='step-warning']")
    assert warn.is_visible()
    assert "Design changes work best with a STEP file." in warn.inner_text()
    assert warn.get_by_role("button", name="Load STEP").is_visible()
    assert page.get_by_role("button", name="Next").is_enabled()
    # STEP part hides the warning.
    page.evaluate("() => fdmUI.store.set({ part: { name: 'l_bracket.step', size: [60, 30, 40], kind: 'step' } })")
    assert page.locator("[data-notice='step-warning']").count() == 0
    # Restore STL + Both and check warning comes back.
    page.evaluate("() => fdmUI.store.set({ part: { name: 'l_bracket.stl', size: [60, 30, 40], kind: 'stl' } })")
    change_group.get_by_role("radio", name="Both").click()
    assert page.locator("[data-notice='step-warning']").is_visible()

    # Manage opens Settings and coming back preserves everything.
    page.get_by_role("button", name="Manage").click()
    assert page.evaluate("() => fdmUI.route") == "settings"
    page.get_by_role("button", name="Back to your part").click()
    assert page.evaluate("() => fdmUI.route") == "flow"
    assert page.locator("#step-title").inner_text() == "Setup"
    assert page.evaluate("() => [fdmUI.store.get().profile.printer, fdmUI.store.get().goal, fdmUI.store.get().change]") == [
        "bambu_p1s", "strong", "both",
    ]


def test_environment_temps_touches_suitable_and_lock_conflict(page):
    # Lock PLA first so we can verify the locked-material conflict card in Environment.
    page.evaluate(
        """() => {
          const s = fdmUI.store.get();
          fdmUI.store.set({
            profile: { ...s.profile, printer: "bambu_p1s", material: "pla" },
            environment: { temp: "indoors", tempC: 20, touches: [] },
          });
          fdmUI.go("env");
        }"""
    )
    assert page.locator("#step-title").inner_text() == "Environment"
    assert page.locator("select, details, input[type=checkbox], input[type=radio]").count() == 0
    assert page.locator("[data-notice='lock-conflict']").count() == 0

    # Hot car (70+ °C) conflicts with locked PLA -> conflict card offers "Let the tool choose".
    temp_group = page.locator("[role=radiogroup][aria-label='Temperature']")
    temp_group.get_by_role("radio", name="Hot car").click()
    assert page.evaluate("() => fdmUI.store.get().environment") == {"temp": "car", "tempC": 70, "touches": []}
    conflict = page.locator("[data-notice='lock-conflict']")
    assert conflict.is_visible()
    assert "PLA" in conflict.inner_text()
    conflict.get_by_role("button", name="Let the tool choose").click()
    assert page.evaluate("() => fdmUI.store.get().profile.material") == ""
    assert page.locator("[data-notice='lock-conflict']").count() == 0

    # Outdoors sun (~50 °C) + Water + Sun and rain -> Suitable shows PETG, ASA, PC +1 (matches mockup).
    temp_group.get_by_role("radio", name="Outdoors, sun").click()
    page.locator("[data-touch='water']").click()
    page.locator("[data-touch='outdoors']").click()
    assert page.evaluate("() => fdmUI.store.get().environment.touches") == ["water", "outdoors"]
    assert page.locator("[data-touch='none']").get_attribute("aria-pressed") == "false"
    suitable_text = page.locator("[data-role='suitable']").inner_text()
    assert "PETG" in suitable_text and "ASA" in suitable_text and "PC" in suitable_text and "+1" in suitable_text

    # Food note appears when Food or drinks is selected.
    page.locator("[data-touch='food']").click()
    assert page.locator(".step-food-note").is_visible()

    # None of these is exclusive and clears touches.
    page.locator("[data-touch='none']").click()
    assert page.evaluate("() => fdmUI.store.get().environment.touches") == []
    assert page.locator("[data-touch='none']").get_attribute("aria-pressed") == "true"

    # Skin leaves only unverified materials -> honest "Not verified" line.
    page.locator("[data-touch='skin']").click()
    assert "Not verified" in page.locator("[data-role='suitable']").inner_text()
    page.locator("[data-touch='none']").click()

    # Custom temperature reveals °C NumberField and updates tempC live.
    temp_group.get_by_role("radio", name="Custom").click()
    num_input = page.locator(".ui-num__input")
    assert num_input.is_visible()
    num_input.fill("110")
    assert page.evaluate("() => fdmUI.store.get().environment.tempC") == 110
    assert "PC" in page.locator("[data-role='suitable']").inner_text()
