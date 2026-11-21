"""Short plain-language findings, message keys, stable finding_ids, and fix linking (T-524)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fdmcore import api
from fdmcore.messages import (
    MAX_ONE_LINER_CHARS,
    MAX_TITLE_WORDS,
    finding_id,
    format_message,
    load_messages,
    summarize_finding,
)
from fdmcore.profile import Profile, load_data

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "web" / "samples"
CORPUS = ROOT / "core" / "tests" / "corpus"

SAMPLE_STLS = sorted(SAMPLES.glob("*.stl"))
CORPUS_STLS = sorted(p for p in CORPUS.glob("*.stl") if not p.stem.endswith("_80k"))


def _word_count(text: str) -> int:
    return len(text.split())


def _assert_valid_finding(f: dict, *, expect_fix_ids: bool = True) -> None:
    assert isinstance(f.get("finding_id"), str) and f["finding_id"], f"missing finding_id: {f}"
    assert isinstance(f.get("short_title"), str) and f["short_title"].strip(), f"missing short_title: {f}"
    assert _word_count(f["short_title"]) <= MAX_TITLE_WORDS, (
        f"short_title > {MAX_TITLE_WORDS} words ({f['rule']}): {f['short_title']!r}"
    )
    assert isinstance(f.get("one_liner"), str) and f["one_liner"].strip(), f"missing one_liner: {f}"
    assert len(f["one_liner"]) <= MAX_ONE_LINER_CHARS, (
        f"one_liner > {MAX_ONE_LINER_CHARS} chars ({len(f['one_liner'])}, {f['rule']}): {f['one_liner']!r}"
    )
    assert isinstance(f.get("instance_label"), str) and f["instance_label"].strip(), (
        f"missing instance_label: {f}"
    )
    msg = f.get("message")
    assert isinstance(msg, dict), f"missing message dict: {f}"
    for slot, field in (("title", "short_title"), ("one_liner", "one_liner"), ("instance_label", "instance_label")):
        spec = msg.get(slot)
        assert isinstance(spec, dict) and isinstance(spec.get("key"), str) and isinstance(spec.get("params"), dict)
        assert format_message(spec["key"], spec["params"]) == f[field], (
            f"message.{slot} mismatch for {f['rule']}: {spec!r} != {f[field]!r}"
        )
    # Existing fields kept intact for old web UI and API callers.
    assert isinstance(f.get("detail"), str) and f["detail"]
    assert isinstance(f.get("title"), str) and f["title"]
    assert "why" in f and "fixes" in f
    if expect_fix_ids:
        for fx in f["fixes"]:
            assert fx.get("finding_id") == f["finding_id"], f"fix missing finding_id: {fx}"


def test_every_rule_has_message_entries_within_limits():
    rules = load_data("rules")["rules"]
    table = load_messages("en")
    for rule in rules:
        t_key = f"finding.{rule}.title"
        o_key = f"finding.{rule}.one_liner"
        assert t_key in table, f"missing message key {t_key}"
        assert o_key in table, f"missing message key {o_key}"
        assert _word_count(table[t_key]) <= MAX_TITLE_WORDS, f"{t_key} > {MAX_TITLE_WORDS} words: {table[t_key]!r}"
        assert len(table[o_key]) <= MAX_ONE_LINER_CHARS, f"{o_key} > {MAX_ONE_LINER_CHARS} chars: {table[o_key]!r}"
    # Every title key in en.yaml respects the 6-word rule.
    for key, text in table.items():
        if key.startswith("finding.") and (key.endswith(".title") or ".title_" in key):
            assert _word_count(text) <= MAX_TITLE_WORDS, f"{key} > {MAX_TITLE_WORDS} words: {text!r}"


def test_format_message_features():
    assert format_message("finding.sideways_hole_oval.title") == "Side hole prints oval"
    assert format_message(
        "finding.sideways_hole_oval.one_liner_fastener",
        {"sag": 0.3, "fastener": "M6"},
    ) == "Top sags ~0.3 mm; an M6 bolt won't fit."
    assert format_message(
        "finding.elephant_foot.one_liner_holes",
        {"count": 1},
    ) == "1 bed hole bulges 0.1\u20130.2 mm tighter at the bottom."
    assert format_message(
        "finding.elephant_foot.one_liner_holes",
        {"count": 4},
    ) == "4 bed holes bulge 0.1\u20130.2 mm tighter at the bottom."
    assert format_message(
        "finding.place.hole_side",
        {"d": 6.0},
    ) == "\u00d86 \u00b7 side"
    assert format_message("finding.nonexistent.key") == "finding.nonexistent.key"


@pytest.mark.parametrize("path", SAMPLE_STLS + CORPUS_STLS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_corpus_and_samples_short_findings_and_links(path: Path):
    api.load(path.read_bytes(), path.name)
    res = api.check([0, 0, 1])
    findings = res["findings"]
    ids = [f["finding_id"] for f in findings]
    assert len(ids) == len(set(ids)), f"duplicate finding_ids on {path.name}: {ids}"
    id_set = set(ids)
    for f in findings:
        _assert_valid_finding(f)
    for s in res["settings"]:
        assert "finding_id" in s and "finding_ids" in s
        if s["finding_id"] is not None:
            assert s["finding_id"] in id_set, f"setting {s['key']} points to unknown finding_id {s['finding_id']}"
            assert s["finding_id"] == s["finding_ids"][0]
        assert all(fid in id_set for fid in s["finding_ids"])
    # JSON-friendly (everything except face_temp bytes).
    payload = {k: v for k, v in res.items() if k != "face_temp"}
    json.dumps(payload)


def test_finding_ids_stable_and_order_independent():
    path = SAMPLES / "feature_test.stl"
    data = path.read_bytes()
    api.load(data, path.name)
    first = api.check([0, 0, 1])["findings"]
    api.load(data, path.name)
    second = api.check([0, 0, 1])["findings"]
    assert [f["finding_id"] for f in first] == [f["finding_id"] for f in second]

    # Reversing input order or shifting severity must not change any finding's ID.
    reversed_ids = [finding_id(f) for f in reversed(first)]
    assert list(reversed(reversed_ids)) == [f["finding_id"] for f in first]

    shifted = api.emphasize_for_goal(first, "strength")
    assert {f["finding_id"] for f in first} == {f["finding_id"] for f in shifted}


@pytest.mark.parametrize(
    ("rel", "up", "rule", "expected_min"),
    [
        ("web/samples/enclosure.stl", [0, 0, 1], "small_hole_undersized", 4),
        ("web/samples/hole_block.stl", [0, 0, 1], "sideways_hole_oval", 2),
        ("web/samples/detail_plate.stl", [0, 0, 1], "small_hole_undersized", 2),
        ("web/samples/feature_test.stl", [0, 0, 1], "small_hole_undersized", 2),
        ("core/tests/corpus/ribbed_housing.stl", [0, 1, 0], "sideways_hole_oval", 4),
    ],
)
def test_one_finding_per_hole_on_multi_hole_parts(rel: str, up: list[int], rule: str, expected_min: int):
    path = ROOT / rel
    api.load(path.read_bytes(), path.name)
    findings = api.check(up)["findings"]
    matched = [f for f in findings if f["rule"] == rule]
    assert len(matched) >= expected_min, f"expected >= {expected_min} {rule} findings on {rel}, got {len(matched)}"
    ids = [f["finding_id"] for f in matched]
    assert len(ids) == len(set(ids)), f"hole findings share finding_id on {rel}: {ids}"


def test_hole_block_crossing_holes_and_teardrops_link_distinct_ids():
    path = SAMPLES / "hole_block.stl"
    api.load(path.read_bytes(), path.name)
    checked = api.check([0, 0, 1])
    side_holes = [f for f in checked["findings"] if f["rule"] == "sideways_hole_oval"]
    assert len(side_holes) == 2
    hole_ids = {f["finding_id"] for f in side_holes}
    assert len(hole_ids) == 2

    edit_res = api.design([0, 0, 1], findings=checked["findings"])
    teardrops = [e for e in edit_res["edits"] if e["rule"] == "short_bridge" and e["source"] == "finding"]
    assert len(teardrops) >= 2
    linked_ids = {e["finding_id"] for e in teardrops}
    assert linked_ids == hole_ids, f"crossing teardrops should link to distinct hole ids: {linked_ids} != {hole_ids}"
    for e in teardrops:
        assert e["fixes"]["finding_id"] == e["finding_id"]
    # Grouped teardrop advice keeps both finding_ids in fixes["finding_ids"].
    td_advice = [a for a in edit_res["advice"] if a["rule"] == "short_bridge"]
    assert len(td_advice) == 1
    assert set(td_advice[0]["fixes"].get("finding_ids", [])) == hole_ids


@pytest.mark.parametrize(
    "rel",
    [
        "web/samples/enclosure.stl",
        "web/samples/feature_test.stl",
        "web/samples/l_bracket.stl",
        "core/tests/corpus/drawer_handle.stl",
    ],
)
def test_design_edits_and_advice_carry_valid_finding_ids(rel: str):
    path = ROOT / rel
    api.load(path.read_bytes(), path.name)
    checked = api.check([0, 0, 1])
    valid_ids = {f["finding_id"] for f in checked["findings"]}
    edit_res = api.design([0, 0, 1], findings=checked["findings"])
    for e in edit_res["edits"]:
        assert "finding_id" in e
        if e["source"] == "finding":
            assert e["finding_id"] in valid_ids
            assert e["fixes"]["finding_id"] == e["finding_id"]
    for a in edit_res["advice"]:
        if a["fixes"] is not None:
            assert a["fixes"]["finding_id"] in valid_ids
            if "finding_ids" in a["fixes"]:
                assert all(fid in valid_ids for fid in a["fixes"]["finding_ids"])


def test_setup_check_findings_have_short_fields_and_distinct_ids():
    # Trigger both setup_flow_too_high variants (speed + hotend) plus thick layers, zero gap, and sticky plate.
    try:
        api.custom_presets(
            printers={"mine": {"based_on": "bambu_a1", "name": "My A1", "bed_surface": "smooth_pei"}},
            materials={"fast": {"based_on": "petg", "name": "Fast PETG", "max_flow_mm3s": 30}},
        )
        res = api.setup_check(
            {"printer": "my_mine", "material": "my_fast"},
            {"layer_height": "0.36", "support_top_z_distance": "0", "outer_wall_speed": "300"},
        )
        findings = res["findings"]
        assert [f["rule"] for f in findings].count("setup_flow_too_high") == 2
        ids = [f["finding_id"] for f in findings]
        assert len(ids) == len(set(ids)), f"setup_check emitted duplicate ids: {ids}"
        for f in findings:
            _assert_valid_finding(f)
    finally:
        api.custom_presets()


def test_long_custom_material_name_stays_within_80_chars():
    try:
        api.custom_presets(
            materials={
                "long": {
                    "based_on": "pla",
                    "name": "Super Ultra Hyper Engineering Carbon-Reinforced Aerospace Grade Filament v9",
                }
            }
        )
        p = Profile.from_dict({"material": "my_long"})
        raw = {
            "rule": "hardened_nozzle_required",
            "severity": "problem",
            "impact": "fails",
            "detail": "Wears out brass nozzle",
            "bbox": None,
            "metrics": {},
        }
        s = summarize_finding(raw, p)
        assert len(s["one_liner"]) <= MAX_ONE_LINER_CHARS
        assert format_message(s["message"]["one_liner"]["key"], s["message"]["one_liner"]["params"]) == s["one_liner"]
    finally:
        api.custom_presets()


def test_compare_nozzles_issues_include_short_fields():
    path = SAMPLES / "detail_plate.stl"
    api.load(path.read_bytes(), path.name)
    rows = api.compare_nozzles([0, 0, 1])
    issues = [iss for row in rows for iss in row["issues"]]
    assert issues
    for iss in issues:
        assert iss["finding_id"]
        assert _word_count(iss["short_title"]) <= MAX_TITLE_WORDS
        assert len(iss["one_liner"]) <= MAX_ONE_LINER_CHARS
        assert iss["instance_label"]
        assert isinstance(iss["message"], dict)
