"""Settings & calibration hub of the new UI (T-533, plan research/redesign-2026-11/PLAN-COMBINED.md §15.3).

Engine-free browser tests: keyboard tab navigation, no raw browser form controls, printer nozzles + Orca list +
"My printer isn't listed", Orca/Prusa print-profile preview, owned materials + spool details + custom filament,
calibration results updating "What the checks use", reload persistence, export -> reset -> import round-trip,
and "Back to your part" returning with work intact. The engine-backed calibration sync and STL download check lives
in tools/e2e_smoke.py (`new UI: settings`)."""

from __future__ import annotations

import functools
import http.server
import json
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def server_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/web/"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        br = None
        for kw in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                br = p.chromium.launch(**kw)
                break
            except Exception:
                continue
        if br is None:
            pytest.skip("no browser for Playwright")
        yield br
        br.close()


@pytest.fixture()
def page(browser, server_url):
    ctx = browser.new_context(viewport={"width": 1280, "height": 720}, accept_downloads=True)
    pg = ctx.new_page()
    # Keep pytest strictly engine-free by aborting Pyodide CDN downloads (three.js still loads normally).
    pg.route("**/pyodide/**", lambda route: route.abort())
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server_url)
    pg.wait_for_selector("body[data-ready]")
    yield pg
    assert not errors, errors
    ctx.close()


def _open_settings(page):
    page.locator(".app-header").get_by_role("button", name="Settings & calibration", exact=True).click()
    page.wait_for_selector(".settings-wrap")


def _assert_no_raw_controls(page, where: str):
    bad = page.evaluate("""() => ({
      select: document.querySelectorAll('select').length,
      details: document.querySelectorAll('details').length,
      checkbox: document.querySelectorAll('input[type="checkbox"]').length,
      radio: document.querySelectorAll('input[type="radio"]').length,
    })""")
    assert all(v == 0 for v in bad.values()), f"raw controls in {where}: {bad}"


def test_tabs_keyboard_layout_and_no_raw_controls(page):
    _open_settings(page)
    nav_box = page.locator(".settings-nav").bounding_box()
    body_box = page.locator(".settings-body").bounding_box()
    assert 215 <= nav_box["width"] <= 225, nav_box
    assert body_box["width"] <= 885, body_box

    tabs = ["printers", "profiles", "materials", "calibration", "active"]
    first_tab = page.locator("#settings-tab-printers")
    first_tab.focus()
    assert first_tab.get_attribute("aria-selected") == "true"
    _assert_no_raw_controls(page, "printers")

    for tab_id in tabs[1:]:
        page.keyboard.press("ArrowDown")
        expected_id = f"settings-tab-{tab_id}"
        assert page.locator(f"#{expected_id}").get_attribute("aria-selected") == "true"
        assert page.evaluate("() => document.activeElement.id") == expected_id
        _assert_no_raw_controls(page, tab_id)

    # Wrap around with ArrowDown, then ArrowUp back to last tab, then Home to first.
    page.keyboard.press("ArrowDown")
    assert page.locator("#settings-tab-printers").get_attribute("aria-selected") == "true"
    page.keyboard.press("ArrowUp")
    assert page.locator("#settings-tab-active").get_attribute("aria-selected") == "true"
    page.keyboard.press("Home")
    assert page.locator("#settings-tab-printers").get_attribute("aria-selected") == "true"
    page.keyboard.press("End")
    assert page.locator("#settings-tab-active").get_attribute("aria-selected") == "true"


def test_printers_nozzles_helper_and_orca(page):
    _open_settings(page)
    # Toggle nozzle 0.2 off on Bambu P1S and confirm catalog reflects it for Setup.
    chip_02 = page.locator('[data-nozzle-chip="bambu_p1s:0.2"]')
    assert chip_02.get_attribute("aria-pressed") == "true"
    chip_02.click()
    assert page.locator('[data-nozzle-chip="bambu_p1s:0.2"]').get_attribute("aria-pressed") == "false"
    nozzles = page.evaluate("() => fdmUI.store.get().catalog.printers.bambu_p1s.nozzles")
    assert nozzles == ["0.4", "0.6", "0.8", "1.0"], nozzles

    # "My printer isn't listed" 3-question helper -> Start from closest -> Save printer.
    page.get_by_role("button", name="My printer isn't listed").click()
    page.locator(".set-card--editor input.set-input").fill("Shop CoreXY")
    page.get_by_role("button", name="Start from closest").click()
    page.get_by_role("button", name="Save printer").click()
    assert page.locator(".set-card-title", has_text="Shop CoreXY").is_visible()
    custom_ids = page.evaluate(
        "() => Object.keys(fdmUI.store.get().catalog.printers).filter(k => k.startsWith('my_'))"
    )
    assert len(custom_ids) == 1
    assert page.evaluate("(id) => fdmUI.store.get().catalog.printers[id].nozzles", custom_ids[0]) == [
        "0.2", "0.4", "0.6", "0.8", "1.0"
    ]

    # OrcaSlicer catalogue picker -> Continue with printer -> Save printer.
    page.get_by_role("button", name="Add from OrcaSlicer").click()
    page.wait_for_selector(".set-card--editor .ui-select", timeout=10_000)
    page.get_by_role("button", name="Continue with printer").click()
    page.get_by_role("button", name="Save printer").click()
    custom_after = page.evaluate(
        "() => Object.keys(fdmUI.store.get().catalog.printers).filter(k => k.startsWith('my_'))"
    )
    assert len(custom_after) == 2


def test_print_profile_preview_and_save(page):
    _open_settings(page)
    page.locator("#settings-tab-profiles").click()

    orca_json = json.dumps({
        "name": "0.20mm Strength @BBL",
        "wall_loops": "4",
        "sparse_infill_density": "25%",
        "layer_height": "0.38",
        "enable_support": "1",
    }).encode("utf-8")
    page.set_input_files(
        ".prof-drop input[type=file]",
        files=[{"name": "strength.json", "mimeType": "application/json", "buffer": orca_json}],
    )
    page.wait_for_selector(".set-card--editor")
    preview_text = page.locator(".set-card--editor").inner_text()
    assert "0.20mm Strength @BBL" in preview_text
    assert "Recognised values" in preview_text
    assert "Missing values" in preview_text
    assert "Potential conflicts" in preview_text
    assert "Layer height 0.38 mm" in preview_text

    page.get_by_role("button", name="Save as baseline").click()
    saved = page.evaluate("() => fdmUI.store.get().catalog.printProfile")
    assert saved["name"] == "0.20mm Strength @BBL"
    assert saved["values"]["wall_loops"] == "4"

    # Also test PrusaSlicer .ini preview.
    prusa_ini = b"perimeters = 3\nfill_density = 20%\nlayer_height = 0.2\n"
    page.set_input_files(
        ".prof-drop input[type=file]",
        files=[{"name": "prusa_draft.ini", "mimeType": "text/plain", "buffer": prusa_ini}],
    )
    page.wait_for_selector(".set-card--editor")
    ini_text = page.locator(".set-card--editor").inner_text()
    assert "Walls" in ini_text and "Infill" in ini_text
    page.get_by_role("button", name="Cancel").click()
    assert page.evaluate("() => fdmUI.store.get().catalog.printProfile.name") == "0.20mm Strength @BBL"


def test_materials_owned_spools_and_custom(page):
    _open_settings(page)
    page.locator("#settings-tab-materials").click()

    page.locator('[data-owned-chip="pla"]').click()
    page.locator('[data-owned-chip="petg"]').click()
    owned = page.evaluate("() => fdmUI.store.get().catalog.owned")
    assert set(owned) == {"pla", "petg"}, owned

    # Add price per kg and grams left on PLA.
    pla_row = page.locator('[data-material-id="pla"]')
    pla_row.get_by_role("button", name="Spool cost & stock").click()
    pla_row.get_by_label("Price per kg").fill("24")
    pla_row.get_by_label("Grams left").fill("650")
    pla_row.get_by_role("button", name="Save").click()
    assert "24/kg" in page.locator('[data-material-id="pla"]').inner_text()
    assert "650 g left" in page.locator('[data-material-id="pla"]').inner_text()
    pla_cat = page.evaluate("() => fdmUI.store.get().catalog.materials.pla")
    assert pla_cat["price_per_kg"] == 24 and pla_cat["spool_left_g"] == 650

    # Add custom filament.
    page.get_by_role("button", name="Add custom filament").click()
    page.locator(".set-card--editor input.set-input").fill("Sunlu Matte PLA")
    page.get_by_role("button", name="Save filament").click()
    assert page.locator(".mat-row-name", has_text="Sunlu Matte PLA").is_visible()
    owned_after = page.evaluate("() => fdmUI.store.get().catalog.owned")
    assert any(k.startswith("my_") for k in owned_after), owned_after


def test_calibration_updates_active_checks_and_persists_through_reload_and_export(page):
    _open_settings(page)

    # Set a nozzle preference and owned material so export/import covers every tab.
    page.locator('[data-nozzle-chip="bambu_p1s:1.0"]').click()
    page.locator("#settings-tab-materials").click()
    page.locator('[data-owned-chip="pla"]').click()

    # Calibration tests tab: enter Overhang = 65 deg and Fits = 6.2 / 6.4.
    page.locator("#settings-tab-calibration").click()
    assert page.locator(".cal-card").count() == 6

    page.locator('[data-cal-card="overhang"]').click()
    _assert_no_raw_controls(page, "calibration-overhang")
    page.locator('.cal-pick[data-pick-value="65"]').click()
    page.get_by_role("button", name="Save result").click()
    assert "Measured" in page.locator(".cal-detail-top").inner_text()
    page.get_by_role("button", name="All calibration tests").click()
    assert "Measured" in page.locator('[data-cal-card="overhang"]').inner_text()
    assert "65°" in page.locator('[data-cal-card="overhang"]').inner_text()

    page.locator('[data-cal-card="fit"]').click()
    groups = page.locator(".cal-picker")
    groups.nth(0).locator('[data-pick-value="0.2"]').click()
    groups.nth(1).locator('[data-pick-value="0.4"]').click()
    page.get_by_role("button", name="Save result").click()

    # What the checks use tab reflects measured values.
    page.locator("#settings-tab-active").click()
    overhang_row = page.locator('[data-active-row="overhang"]').inner_text()
    snug_row = page.locator('[data-active-row="fit_snug"]').inner_text()
    slide_row = page.locator('[data-active-row="fit_sliding"]').inner_text()
    assert "65°" in overhang_row and "Measured" in overhang_row
    assert "0.20 mm" in snug_row and "Measured" in snug_row
    assert "0.40 mm" in slide_row and "Measured" in slide_row

    # Reset one value (sliding fit) reverts only that row.
    page.locator('[data-active-row="fit_sliding"]').get_by_role("button", name="Reset").click()
    slide_after = page.locator('[data-active-row="fit_sliding"]').inner_text()
    assert "0.35 mm" in slide_after and "Typical" in slide_after
    assert "65°" in page.locator('[data-active-row="overhang"]').inner_text()

    # Reload the page: settings survive reload.
    page.reload()
    page.wait_for_selector("body[data-ready]")
    nozzles_reloaded = page.evaluate("() => fdmUI.store.get().catalog.printers.bambu_p1s.nozzles")
    assert "1.0" not in nozzles_reloaded
    assert "pla" in page.evaluate("() => fdmUI.store.get().catalog.owned")
    _open_settings(page)
    page.locator("#settings-tab-active").click()
    assert "65°" in page.locator('[data-active-row="overhang"]').inner_text()
    assert "Measured" in page.locator('[data-active-row="overhang"]').inner_text()

    # Export -> Reset all -> Import restores everything.
    with page.expect_download() as dl_info:
        page.get_by_role("button", name="Export all my settings").click()
    exported_path = dl_info.value.path()
    exported_bytes = Path(exported_path).read_bytes()
    exported_data = json.loads(exported_bytes.decode("utf-8"))
    assert exported_data["format"] == "fdm-check-settings"

    page.get_by_role("button", name="Reset all settings").click()
    page.locator("dialog.ui-modal").get_by_role("button", name="Reset everything").click()
    assert "55°" in page.locator('[data-active-row="overhang"]').inner_text()
    assert "Typical" in page.locator('[data-active-row="overhang"]').inner_text()
    assert page.evaluate("() => fdmUI.store.get().catalog.owned") == []
    assert "1.0" in page.evaluate("() => fdmUI.store.get().catalog.printers.bambu_p1s.nozzles")

    page.set_input_files(
        'input[data-settings-import="1"]',
        files=[{"name": "fdm-check-settings.json", "mimeType": "application/json", "buffer": exported_bytes}],
    )
    assert "65°" in page.locator('[data-active-row="overhang"]').inner_text()
    assert "Measured" in page.locator('[data-active-row="overhang"]').inner_text()
    assert "pla" in page.evaluate("() => fdmUI.store.get().catalog.owned")
    assert "1.0" not in page.evaluate("() => fdmUI.store.get().catalog.printers.bambu_p1s.nozzles")


def test_back_to_part_preserves_work(page):
    # Without a part, Back returns to Start.
    _open_settings(page)
    page.get_by_role("button", name="Back to your part").click()
    assert page.evaluate("() => fdmUI.route") == "start"

    # With a part and work in progress, Back returns to the flow with everything intact.
    page.evaluate("""() => {
      fdmUI.store.set({
        part: { name: "bracket.stl", size: [40, 30, 20] },
        profile: { printer: "bambu_p1s", material: "pla", nozzle: "0.4", nozzles: ["0.4"] },
        holds: [{ region: 2, kind: "screw" }],
        loads: [{ region: 5, kg: 3 }],
      });
      fdmUI.go("loads");
    }""")
    assert page.locator("#step-title").inner_text() == "Loads"
    _open_settings(page)
    page.locator("#settings-tab-calibration").click()
    page.get_by_role("button", name="Back to your part").click()
    assert page.locator("#step-title").inner_text() == "Loads"
    assert page.evaluate("() => fdmUI.route") == "flow"
    state = page.evaluate("() => fdmUI.store.get()")
    assert state["part"]["name"] == "bracket.stl"
    assert len(state["holds"]) == 1 and state["loads"][0]["kg"] == 3
    assert page.locator(".app-header .part-pill").inner_text().startswith("bracket.stl")
