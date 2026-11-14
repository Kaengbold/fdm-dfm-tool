import json

from fdmcore.profile import presets
from fdmcore.slicer_cli import FILAMENT_TYPE, _condition, bundled_profiles, match_profiles, pick_profiles, write_profiles

MACHINE = {"printer_model": "Bambu Lab X1 Carbon", "nozzle_diameter": ["0.4"], "printer_notes": "PRINTER_VENDOR_BAMBU"}


def test_profile_conditions_without_eval():
    assert _condition('printer_model=~/.*X1.*/ and nozzle_diameter[0]==0.4', MACHINE)
    assert _condition('printer_model=~/.*X1.*/ && nozzle_diameter[0]==0.4', MACHINE)
    assert _condition('!(printer_model=~/.*P1P.*/) || nozzle_diameter[0]>0.6', MACHINE)
    assert _condition('printer_notes!~/PRUSA/ and not printer_model=="Bambu Lab A1"', MACHINE)
    assert not _condition('nozzle_diameter[0]==0.6', MACHINE)


def test_unreadable_conditions_count_as_not_compatible():
    for expr in ("", "unknown_key==1", "__import__('os')", "printer_model=~", "(nozzle_diameter[0]==0.4"):
        assert not _condition(expr, MACHINE)


def test_bundled_profiles_cover_every_printer():
    """Slicing must not depend on the OrcaSlicer version installed (2.5 packs its profiles where we
    can't read them): the profiles we ship have every printer at 0.4 mm with every material, flattened."""
    profiles = bundled_profiles()
    for printer in presets()["printers"].values():
        for material in FILAMENT_TYPE:
            names = match_profiles(profiles, printer["name"], "0.4", material, 0.2)
            assert all("inherits" not in profiles[n]["data"] for n in names.values()), names
    picked = pick_profiles(None, "Bambu Lab P1S", "0.4", "petg", 0.2)
    assert picked["machine"]["name"] == "Bambu Lab P1S 0.4 nozzle" and "PETG" in picked["filament"]["name"]
    assert float(picked["filament"]["data"]["filament_max_volumetric_speed"][0]) > 2  # not the built-in default


def test_generic_filament_when_the_printer_maker_has_none(tmp_path):
    machine = {"kind": "machine", "data": {"instantiation": "true", "printer_model": "X"}}
    machine["own"] = machine["data"]

    def entry(kind, **data):
        d = {"instantiation": "true", **data}
        return {"kind": kind, "data": d, "own": d}

    profiles = {
        "Maker X 0.4 nozzle": machine,
        "0.20mm Standard @X": entry("process", layer_height="0.2", compatible_printers=["Maker X 0.4 nozzle"]),
        "Maker PLA @X": entry("filament", filament_type=["PLA"], compatible_printers=["Maker X 0.4 nozzle"]),
        "Other PC @Y": entry("filament", filament_type=["PC"], compatible_printers=["Other Y 0.4 nozzle"]),
        "Generic PC @System": entry("filament", filament_type=["PC"], compatible_printers=[]),
    }
    assert match_profiles(profiles, "Maker X", "0.4", "pla", 0.2)["filament"] == "Maker PLA @X"
    assert match_profiles(profiles, "Maker X", "0.4", "pc", 0.2)["filament"] == "Generic PC @System"
    files = write_profiles({"machine": {"name": "m", "data": {"inherits": "base", "bed_temperature": ["60"]}}}, tmp_path)
    assert json.loads(files["machine"].read_text()) == {"bed_temperature": ["60"], "type": "machine", "from": "system"}
