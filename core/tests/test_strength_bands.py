from unittest.mock import patch

import pytest

from fdmcore.api import _dynamics_story, _format_safety, _strength_band, _strength_story, _strength_story_parts


@pytest.mark.parametrize(("value", "expected"), [(1, "about ×1.0"), (9.9, "about ×9.9"),
                                                  (9.99, "about ×10"), (10, "about ×10"), (17.7, "about ×18")])
def test_safety_precision(value, expected):
    assert _format_safety(value) == expected


@pytest.mark.parametrize(("value", "need", "expected"), [(0.9, 2, "Likely to break"), (1, 2, "Marginal"),
    (2, 2, "Enough"), (2, 3, "Marginal"), (3, 3, "Enough"), (5, 3, "Plenty of reserve")])
def test_band_boundaries(value, need, expected):
    assert _strength_band(value, need) == expected


def test_strength_story_does_not_promise_decimal_precision():
    result = {"safety_factor": 17.7, "critical_point_print": [0, 0, 2], "verdict": "fine", "mode": "material"}
    with patch("fdmcore.api._use_story", return_value=""), patch("fdmcore.api.required_safety", return_value=2):
        story = _strength_story(None, None, result, None)
    assert story.startswith("Plenty of reserve. It should hold")
    assert "17.7" not in story


@pytest.mark.parametrize("mode", ["layers", "material", "lines"])
def test_strength_story_parts_match_full_explanation(mode):
    result = {"safety_factor": 1.5, "critical_point_print": [0, 0, 12], "verdict": "borderline", "mode": mode}
    with patch("fdmcore.api._use_story", return_value=""), patch("fdmcore.api.required_safety", return_value=2):
        story = _strength_story(None, None, result, None)
    parts = _strength_story_parts(result)
    assert parts["weakest"] == "The weakest spot is about 12 mm above the bed."
    assert parts["helps"] in story


def test_drop_safety_uses_estimate_precision():
    story = _dynamics_story(dict(frequency_hz=None, drop_mm=10, impact_safety=9.5, impact_safety_soft=10.2,
                                 static_safety=17.7, impact_factor_soft=2, impact_factor=4, sag_mm=0.1))
    assert "about ×18" in story
    assert "about ×9.5" in story
    assert "about ×10" in story


def test_strong_part_is_not_told_to_change():
    result = {"safety_factor": 12, "critical_point_print": [0, 0, 3], "verdict": "fine", "mode": "material"}
    assert _strength_story_parts(result)["helps"] == "Nothing needs to change for this load."
