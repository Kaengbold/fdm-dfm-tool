"""Sanity checks on the advice over every sample and corpus part (T-464).

Each check guards a problem the 2026-11 audit found. Where that problem is still open, the check is
marked ``xfail(strict=True)`` with its task: it fails today as expected, and turns red as soon as
the fix lands, so the mark is removed in the fixing PR instead of being forgotten.
"""

import re
from pathlib import Path

import pytest
import yaml

from fdmcore import api

ROOT = Path(__file__).resolve().parents[2]
PARTS = sorted((ROOT / "web" / "samples").glob("*.stl")) + sorted((Path(__file__).parent / "corpus").glob("*.stl"))
# What the tool must never show a user: internal task numbers, and finding texts that guess at the design.
FORBIDDEN = re.compile(r"\bT-\d+|The CAD model shows")
# The shell settings T-424 wants from the part's shape (walls are already raised for weak spots).
SHELLS = ("wall_loops", "sparse_infill_density", "top_shell_layers")


@pytest.fixture(scope="module")
def advice():
    """Per part: the orientation options and the check of the recommended one, with the default profile."""
    out = {}
    for path in PARTS:
        api.load(path.read_bytes(), path.stem)
        options = api.orientations()["orientations"]
        out[path.stem] = (options, api.check(options[0]["up"]))
    return out


def test_parts_found():
    assert len(PARTS) >= 15, [p.name for p in PARTS]


def _support(o):
    return o["support_g"] if o["support_g"] is not None else o["support_area_mm2"]


def test_recommended_not_beaten_on_support_bed_and_height_at_once(advice):
    beaten = [f"{name}: {o['label']} has less support ({_support(o)} < {_support(best)}), more bed contact"
              f" ({o['bed_area_mm2']:.0f} > {best['bed_area_mm2']:.0f} mm²) and is lower ({o['height_mm']:.0f} < {best['height_mm']:.0f} mm)"
              for name, (options, _) in advice.items() for best in options[:1] for o in options[1:]
              if _support(o) < _support(best) and o["bed_area_mm2"] > best["bed_area_mm2"] and o["height_mm"] < best["height_mm"]]
    assert not beaten, "\n".join(beaten)


# A "ranked lower" claim and the number on the card it must agree with: (claim, option is really worse).
CLAIMS = {
    "needs more support": lambda o, best: _support(o) > _support(best),
    "prints taller": lambda o, best: o["height_mm"] > best["height_mm"],
    "has less contact with the bed": lambda o, best: o["bed_area_mm2"] < best["bed_area_mm2"],
}


def test_ranked_lower_reasons_match_the_numbers(advice):
    false = [f"{name}: {o['label']} says '{claim}' (support {_support(o)} vs {_support(best)}, height {o['height_mm']:.0f}"
             f" vs {best['height_mm']:.0f} mm, bed {o['bed_area_mm2']:.0f} vs {best['bed_area_mm2']:.0f} mm²)"
             for name, (options, _) in advice.items() for best in options[:1] for o in options[1:]
             for claim, worse in CLAIMS.items() if claim in o.get("why_lower", "") and not worse(o, best)]
    assert not false, "\n".join(false)


def test_clearly_different_parts_get_different_shells(advice):
    shells = {name: tuple({s["key"]: s["value"] for s in check["settings"]}[k] for k in SHELLS)
              for name, (_, check) in advice.items()}
    # Not 6 as the audit hoped: the samples are mostly small parts that rightly share the goal's
    # shells, and lowering infill on small interiors only to vary them would weaken hooks (T-424 review).
    assert len(set(shells.values())) > 1, f"every part gets {next(iter(shells.values()))} for {SHELLS}"


def test_changed_shape_settings_quote_a_measurement(advice):
    from fdmcore.profile import Profile
    from fdmcore.recommend import goal_plan

    plan = goal_plan(Profile.from_dict({}))
    baseline = (str(plan["walls"]), f"{plan['infill']}%", str(plan["top"]))
    for name, (_, check) in advice.items():
        settings = {s["key"]: s for s in check["settings"]}
        for key, default in zip(SHELLS, baseline):
            row = settings[key]
            if row["value"] != default:
                assert re.search(r"\d.*(?:mm|cm³|%)", row["reason"]), (name, row)


def _texts(value):
    """Every string inside a YAML value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _texts(v)
    elif isinstance(value, list):
        for v in value:
            yield from _texts(v)


def test_no_internal_numbers_or_design_guesses_in_knowledge_base():
    data = ROOT / "core" / "fdmcore" / "data"
    hits = [f"{f}: {t[:80]}" for f in ("rules.yaml", "symptoms.yaml")
            for t in _texts(yaml.safe_load((data / f).read_text(encoding="utf-8"))) if FORBIDDEN.search(t)]
    assert not hits, "\n".join(hits)


def test_no_internal_numbers_or_design_guesses_in_results(advice):
    hits = []
    for name, (options, check) in advice.items():
        texts = [t for o in options for t in [*o["reasons"], o.get("why_lower", ""), o["support_summary"] or ""]]
        texts += [t for f in check["findings"] for t in (f["detail"], f.get("why", ""), f.get("title", ""))]
        texts += [s["reason"] for s in check["settings"]]
        hits += [f"{name}: {t[:80]}" for t in texts if isinstance(t, str) and FORBIDDEN.search(t)]
    assert not hits, "\n".join(hits)


def test_default_profile_has_no_use_temperature():
    # The app's "Where is it used?" starts at Indoors (browser test "fresh visit uses indoors", T-395);
    # the engine's default profile must agree: no hot or cold use unless the user says so.
    from fdmcore.profile import Profile

    assert Profile.from_dict({}).use_temp_c is None


def test_design_changes_belong_to_a_finding(advice):
    """T-432: a change is offered as a fix only for a finding of the current check; a part with no findings gets
    none outside "Other ideas". Teardrops and corner roundings still come with their findings."""
    linked, wrong = {}, []
    for path in PARTS:
        _, check = advice[path.stem]
        api.load(path.read_bytes(), path.stem)
        edits = api.design(check["up"], findings=check["findings"])["edits"]
        rules = {f["rule"] for f in check["findings"]}
        for e in edits:
            if e["source"] == "finding" and e["fixes"]["rule"] not in rules:
                wrong.append(f"{path.stem}: {e['title']} fixes {e['fixes']['rule']}, not in the check")
            if not rules and e["source"] != "other":
                wrong.append(f"{path.stem} (no findings): {e['title']} offered as {e['source']}")
        linked[path.stem] = {e["rule"] for e in edits if e["source"] == "finding"}
    assert not wrong, "\n".join(wrong)
    assert "sharp_bed_corners" in linked["tray"], linked["tray"]  # warping: its corners
    assert "short_bridge" in linked["l_bracket"], linked["l_bracket"]  # oval sideways holes: teardrops
    assert not linked["hook"] and not linked["shelf_bracket"], (linked["hook"], linked["shelf_bracket"])
