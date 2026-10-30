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
