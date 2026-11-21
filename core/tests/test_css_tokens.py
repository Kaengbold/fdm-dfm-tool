"""Design-token lint for the new UI (T-510, plan research/redesign-2026-11/PLAN-COMBINED.md §1.5).

The owner's main complaint was inconsistent spacing, so spacing comes only from the token scale in web/tokens.css:
any raw px in margin/padding/gap elsewhere fails here. Every colour token also needs a dark value (light default,
dark switch, decision 6 in §16)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOKENS = ROOT / "web" / "tokens.css"
# The old stylesheet is replaced at cut-over (T-580), not fixed; everything else under web/ and design/ is linted.
EXEMPT = {TOKENS, ROOT / "web" / "style.css"}
SCALE = {4, 8, 12, 16, 24, 32, 48}

SPACING_PROPS = re.compile(r"^(margin|padding|gap|row-gap|column-gap|grid-gap|inset|scroll-margin|scroll-padding)(-[a-z-]+)?$")
DECL = re.compile(r"([a-z-]+)\s*:\s*([^;{}]+)")
RAW_PX = re.compile(r"(?<![\w.-])(\d*\.?\d+)px")


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def spacing_violations(css: str) -> list[str]:
    """Declarations that set a spacing property with a raw, non-zero px value (calc() included)."""
    bad = []
    for prop, value in DECL.findall(strip_comments(css)):
        if not SPACING_PROPS.match(prop):
            continue
        if any(float(n) != 0 for n in RAW_PX.findall(value)):
            bad.append(f"{prop}: {value.strip()}")
    return bad


def linted_files() -> list[Path]:
    files = [*(ROOT / "web").rglob("*.css"), *(ROOT / "design").rglob("*.css")]
    return sorted(f for f in files if f not in EXEMPT and "vendor" not in f.parts)


def block(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", strip_comments(css), flags=re.S)
    assert match, f"{selector} block missing in tokens.css"
    return match.group(1)


def custom_props(body: str) -> dict[str, str]:
    return {name: value.strip() for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


def test_lint_catches_raw_spacing():
    assert spacing_violations(".a { padding: 10px 0; }") == ["padding: 10px 0"]
    assert spacing_violations(".a { margin-top: calc(var(--s-2) + 3px); }")
    assert spacing_violations(".a { gap: var(--s-3); padding: 0 var(--s-4); margin: 0px; width: 13px; }") == []
    assert spacing_violations("/* padding: 5px */ .a { border: 1px solid; }") == []


def test_new_stylesheets_use_spacing_tokens():
    files = linted_files()
    assert ROOT / "web" / "ui" / "base.css" in files
    problems = [f"{f.relative_to(ROOT)}: {v}" for f in files for v in spacing_violations(f.read_text(encoding="utf-8"))]
    assert not problems, "raw px spacing, use the --s-* tokens from web/tokens.css:\n" + "\n".join(problems)


def test_spacing_scale_is_the_plan_scale():
    light = custom_props(block(TOKENS.read_text(encoding="utf-8"), ":root"))
    steps = {name: value for name, value in light.items() if re.fullmatch(r"--s-\d", name)}
    assert {int(v.removesuffix("px")) for v in steps.values()} == SCALE


@pytest.mark.parametrize("theme", [':root[data-theme="dark"]'])
def test_every_colour_token_has_a_dark_value(theme):
    css = TOKENS.read_text(encoding="utf-8")
    light, dark = custom_props(block(css, ":root")), custom_props(block(css, theme))
    colours = {n for n, v in light.items() if re.match(r"#|rgb|hsl|linear-gradient", v) or n.startswith("--shadow")}
    assert colours, "no colour tokens found"
    missing = sorted(colours - dark.keys())
    assert not missing, f"colour tokens without a dark value: {missing}"
    assert not set(dark) - set(light), "dark theme defines tokens the light theme doesn't have"
