"""Tests for T-435: exact-number CAD advice on the part for every design fix."""
from functools import lru_cache
import json
import re
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api, features, reports
from fdmcore.profile import Profile, load_data

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"
REQUIRED_KEYS = {"id", "rule", "text", "now", "target", "unit", "point", "points", "count", "fixes", "edit_id", "edit_ids"}


def _load(name):
    api.load((SAMPLES / f"{name}.stl").read_bytes(), name)
    return api._session.mesh


@lru_cache(maxsize=None)
def _sample_data(name):
    mesh = _load(name)
    lo, hi = mesh.bounds()
    checked = api.check([0, 0, 1], {})
    designed = api.design([0, 0, 1], {}, findings=checked["findings"])
    return lo, hi, checked, designed


def _numbers_in(text):
    return [float(m) for m in re.findall(r"\d+(?:\.\d+)?", text)]


@pytest.mark.parametrize("name", ["test_block", "feature_test", "enclosure"])
def test_advice_has_number_unit_and_point_inside_bbox(name):
    lo, hi, _, r = _sample_data(name)
    advice = r["advice"]
    assert advice, f"expected advice for {name}"
    json.dumps(advice)  # must be JSON-friendly

    ids, texts = set(), set()
    for a in advice:
        assert set(a.keys()) == REQUIRED_KEYS
        assert a["id"] not in ids
        ids.add(a["id"])
        assert a["text"] not in texts, f"repeated advice line on {name}: {a['text']}"
        texts.add(a["text"])
        assert isinstance(a["target"], (int, float)) and a["target"] > 0
        assert a["now"] is None or isinstance(a["now"], (int, float))
        assert isinstance(a["unit"], str) and a["unit"]
        assert a["unit"] in a["text"]
        assert any(abs(v - a["target"]) < 0.06 for v in _numbers_in(a["text"])), a["text"]
        assert a["count"] == len(a["points"]) >= 1
        assert a["point"] == a["points"][0]

        for raw in a["points"]:
            pt = np.asarray(raw, dtype=float)
            assert pt.shape == (3,)
            assert np.all(pt >= lo - 1e-4) and np.all(pt <= hi + 1e-4), (name, a["id"], pt, lo, hi)

    # Internal _advice payload must never leak into returned edits.
    assert all("_advice" not in e for e in r["edits"])


@pytest.mark.parametrize("name", ["test_block", "feature_test", "enclosure"])
def test_edit_and_advice_agree_on_number(name):
    _, _, _, r = _sample_data(name)
    edits = {e["id"]: e for e in r["edits"]}
    by_edit = {eid: a for a in r["advice"] for eid in a["edit_ids"]}

    for eid, e in edits.items():
        if e["source"] == "other":
            assert eid not in by_edit
            continue
        assert eid in by_edit, f"active edit {eid} missing advice item"
        a = by_edit[eid]
        edit_nums = _numbers_in(f"{e['title']} {e['description']}")
        assert any(abs(v - a["target"]) < 0.06 for v in edit_nums), (eid, a["target"], e["title"], e["description"])
        if e["source"] == "finding":
            assert a["fixes"] == e["fixes"]


def test_enclosure_groups_corners_and_avoids_fastener_contradiction():
    _, _, _, r = _sample_data("enclosure")
    sugs = api.fastener_suggestions([0, 0, 1])["suggestions"]
    assert sugs and sugs[0]["size"] == "M3" and sugs[0]["choices"][0]["target_mm"] == 2.5

    advice = r["advice"]
    # Generic hole-size advice must not contradict the M3 screw suggestion on enclosure's four holes.
    assert not any(a["rule"] == "small_hole_undersized" for a in advice), advice

    # The four identical bed-corner roundings collapse into one counted line keeping all 4 positions.
    corners = [a for a in advice if a["rule"] == "sharp_bed_corners"]
    assert len(corners) == 1
    assert corners[0]["text"] == "4 bed corners: round with r 3.0 mm"
    assert corners[0]["count"] == 4 and len(corners[0]["points"]) == 4 and len(corners[0]["edit_ids"]) == 4

    # Non-hole findings still produce their exact-number advice.
    assert any(a["rule"] == "wall_line_mismatch" and "2.25 mm" in a["text"] for a in advice)


def test_no_invented_numbers_for_missing_metrics_or_unmeasured_rules():
    p = Profile.from_dict({})
    T = load_data("thresholds")
    for rule in ("thick_solid_block", "small_layers_cooling", "floor_shrink_line", "pause_to_insert",
                 "thin_neck", "living_hinge", "uneven_wall_thickness", "weak_upright_pin", "tall_thin_feature"):
        assert features._finding_advice({"rule": rule, "metrics": {}}, p, T, 50.0) is None

    for rule in ("wall_line_mismatch", "small_hole_undersized", "nut_trap_tight", "screw_wall_thin",
                 "snap_fit_upright", "rib_sink_line", "shrinkage_size_error", "side_detail_small"):
        assert features._finding_advice({"rule": rule, "metrics": {}}, p, T, 50.0) is None

    # snap_fit_upright must read the finding's real hook_max_mm metric instead of an invented fallback.
    snap = features._finding_advice({"rule": "snap_fit_upright", "metrics": {"hook_mm": 1.2, "hook_max_mm": 0.85}}, p, T, 50.0)
    assert snap is not None and snap[2] == 0.85 and "0.85 mm" in snap[0]


def test_fastener_request_edit_and_advice_agree():
    _load("enclosure")
    holes = [f for f in api.design([0, 0, 1], {})["features"] if f["type"] == "hole"]
    assert holes
    api.set_fastener(holes[0]["region"], [0, 0, 1], "M3", "bolt")
    r = api.design([0, 0, 1], {})
    fastener_edits = [e for e in r["edits"] if e["id"].startswith("fastener-")]
    assert fastener_edits
    by_edit = {eid: a for a in r["advice"] for eid in a["edit_ids"]}
    for e in fastener_edits:
        a = by_edit[e["id"]]
        edit_nums = _numbers_in(f"{e['title']} {e['description']}")
        assert any(abs(v - a["target"]) < 0.06 for v in edit_nums), (e["id"], a["target"], e["title"], e["description"])


def test_reports_include_advice():
    _load("enclosure")
    advice = api.design([0, 0, 1], {})["advice"]
    assert advice
    rep = reports.collect([0, 0, 1], {}, mode="both")
    html = reports.to_html(rep)
    md, _ = reports.to_markdown(rep, "enclosure")
    assert "Design advice for CAD" in html
    assert "Design advice for CAD" in md
    assert advice[0]["text"] in html
    assert advice[0]["text"] in md
