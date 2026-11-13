from fdmcore import api

from test_strength import hook_loads


def ratings(**kw):
    return {r["material"]: r for r in api.material_advice(**kw)}


def test_hot_and_outdoors_rules_out_pla():
    r = ratings(profile={"printer": "prusa_mk4s", "use_temp_c": 70}, outdoors=True)
    assert r["pla"]["rating"] == "not suitable" and any("soft" in x for x in r["pla"]["reasons"])
    assert r["asa"]["rating"] != "not suitable" and any("sunlight" in x for x in r["asa"]["reasons"])
    assert r["abs"]["rating"] == "not suitable"  # brittle in sunlight


def test_printer_limits_and_flexible():
    r = ratings(profile={"printer": "bambu_p1s"})
    assert r["pa_cf"]["rating"] == "not suitable" and any("hardened nozzle" in x for x in r["pa_cf"]["reasons"])
    assert r["tpu_95a"]["rating"] == "not suitable"
    first = api.material_advice(profile={}, flexible=True)[0]
    assert first["material"] == "tpu_95a" and first["rating"] == "good"


def test_load_and_heat_together():
    loads = hook_loads()
    hot = ratings(profile={"printer": "bambu_x1c", "use_temp_c": 55}, up=[1, 0, 0], loads=loads)
    assert hot["pla"]["safety_factor"] < 0.5 * hot["pc"]["safety_factor"]
    assert api.material_advice(profile={"printer": "bambu_p1s"}, up=[1, 0, 0], loads=loads)[0]["material"] == "pla"  # easiest that holds


def test_material_options_include_heat():
    loads = hook_loads()
    opts = api.strength_options([1, 0, 0], loads, {"material": "pla", "printer": "bambu_x1c", "use_temp_c": 55})
    mats = {o["material"]: o["safety_factor"] for o in opts if o["kind"] == "material"}
    assert "pc" in mats and "pla_plus" not in mats  # PLA+ goes soft just like PLA


def test_fibre_filament_is_never_stronger():
    """Carbon / glass fibre filament is stiffer, not stronger: never offered to make it stronger, even
    when its numbers say so (a user's PLA-CF copied from PLA with a datasheet's higher XY strength)."""
    from fdmcore.profile import fibre

    assert fibre({"name": "PLA-CF"}) == "carbon" and fibre({"name": "Bambu PETG GF"}) == "glass"
    assert fibre({"name": "PLA Basic"}) is None and fibre({"name": "PC (polycarbonate)"}) is None and fibre({"name": "My CF", "fibre": "none"}) is None
    api.custom_presets(materials={"my_pla_cf": {"based_on": "pla", "name": "PLA-CF", "tensile_xy_mpa": 90}})
    try:
        loads = hook_loads()
        opts = api.strength_options([1, 0, 0], loads, {"material": "pla", "printer": "bambu_x1c"})
        mats = {o["material"] for o in opts if o["kind"] == "material"}
        assert "my_pla_cf" not in mats and "pa_cf" not in mats
        advice = {r["material"]: r for r in api.material_advice(profile={"printer": "bambu_x1c"})}
        assert any("stiffer, not stronger" in why for why in advice["my_pla_cf"]["reasons"])
        assert advice["pa_cf"]["rating"] != "good"
    finally:
        api.custom_presets()


def test_settings_before_another_material():
    """T-366: a slicer setting that is enough comes first, and another material only after every setting and
    orientation, even when the material would be stronger (mushroom in ABS, 12 kg pulling on the stem)."""
    from pathlib import Path

    api.load((Path(__file__).resolve().parents[2] / "web" / "samples" / "mushroom.stl").read_bytes(), "mushroom")
    api.regions()
    loads = [{"region": 8, "kind": "fixed"}, {"region": 0, "kind": "force", "kg": 12, "direction": "pull"}]
    opts = api.strength_options([0, 0, 1], loads, {"material": "abs"})
    kinds = [o["kind"] for o in opts]
    assert kinds[0] == "slicer" and opts[0]["safety_factor"] >= 2
    last_setting = max(i for i, k in enumerate(kinds) if k in ("slicer", "orientation"))
    first_material = kinds.index("material")
    assert last_setting < first_material
    assert max(o["safety_factor"] for o in opts if o["kind"] == "material") > opts[last_setting]["safety_factor"]  # though stronger
    assert kinds[-1] == "load"
