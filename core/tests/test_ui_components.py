"""Keyboard use of the new UI components (T-511, plan §1.8) on design/gallery.html, in a headless browser.

Skipped when Playwright or a browser isn't installed (the engine CI only has Python)."""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sync_api = pytest.importorskip("playwright.sync_api")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def page():
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
        pg.goto(f"http://127.0.0.1:{server.server_address[1]}/design/gallery.html")
        pg.wait_for_selector("body[data-ready]")
        yield pg
        assert not errors, errors
        browser.close()
    server.shutdown()


def focused(page, attr: str = "textContent") -> str:
    return page.evaluate(f"() => document.activeElement?.{attr} ?? ''")


def test_choice_cards_move_with_arrows(page):
    group = page.locator("[role=radiogroup][aria-label='What matters most?']")
    group.locator("[aria-checked=true]").focus()
    page.keyboard.press("ArrowRight")
    assert group.locator("[aria-checked=true]").inner_text().startswith("Strong")
    assert "Strong" in focused(page)
    page.keyboard.press("End")  # the disabled card is skipped
    assert group.locator("[aria-checked=true]").inner_text().startswith("Quality")


def test_select_searches_and_picks_by_keyboard(page):
    trigger = page.locator(".ui-select__button").first
    trigger.focus()
    page.keyboard.press("Enter")
    assert trigger.get_attribute("aria-expanded") == "true"
    page.keyboard.type("as")
    page.keyboard.press("Enter")
    assert trigger.inner_text().strip() == "ASA"
    assert focused(page, "className") == "ui-select__button", "focus didn't return to the select"
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Escape")
    assert trigger.get_attribute("aria-expanded") == "false"


def test_radial_menu_arrows_and_escape(page):
    stage = page.locator(".stagebox").first
    stage.focus()
    page.keyboard.press("Enter")
    assert page.locator(".ui-radial [role=menuitem]").count() == 4
    assert focused(page) == "Hold this face"
    page.keyboard.press("ArrowDown")
    assert focused(page) == "Hold this edge"
    page.keyboard.press("Escape")
    assert page.locator(".ui-radial").count() == 0
    assert page.evaluate("() => document.activeElement.classList.contains('stagebox')"), "focus didn't return to the 3D stage"


def test_popover_focuses_the_number_and_escape_cancels(page):
    stage = page.locator(".stagebox").nth(1)
    stage.focus()
    page.keyboard.press("Enter")
    assert focused(page, "tagName") == "INPUT"
    page.keyboard.type("x")
    assert page.locator(".ui-pop .ui-num").get_attribute("class").endswith("is-error")
    page.keyboard.press("Escape")
    assert page.locator(".ui-pop").count() == 0


def test_disclosure_finding_and_modal(page):
    head = page.locator(".ui-disc__head").first
    head.focus()
    page.keyboard.press("Enter")
    assert head.get_attribute("aria-expanded") == "true"
    row = page.locator(".ui-find__head").nth(1)
    row.focus()
    page.keyboard.press("Enter")
    assert row.get_attribute("aria-expanded") == "true"
    opener = page.get_by_role("button", name="Open a modal")
    opener.focus()
    page.keyboard.press("Enter")
    assert page.locator("dialog.ui-modal").evaluate("d => d.open")
    page.keyboard.press("Escape")
    page.locator("dialog.ui-modal").wait_for(state="detached", timeout=2000)  # "close" fires a task later
    assert focused(page).strip() == "Open a modal"


def test_no_browser_default_controls(page):
    """§1.5: no raw <select>, <details> or unstyled checkbox/radio inputs in the components."""
    assert page.locator("select, details, input[type=checkbox], input[type=radio]").count() == 0
