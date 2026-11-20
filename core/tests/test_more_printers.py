"""More printers than the built-in 15 (T-451): OrcaSlicer's printer catalogue and the browser's picker."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fdmcore import api
from fdmcore.profile import presets

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "core" / "fdmcore" / "data" / "orca_printers.json"


def test_catalogue_lists_the_other_printers():
    data = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    printers = data["printers"]
    assert data["orca_version"] and data["licence"] == "AGPL-3.0" and len(printers) > 300
    names = [p["name"] for p in printers]
    assert len(set(names)) == len(names)
    built_in = {p["name"].lower() for p in presets()["printers"].values()}
    assert not built_in & {n.lower() for n in names}  # those are in the list already
    for p in printers:
        assert len(p["bed"]) == 3 and all(v > 0 for v in p["bed"]), p
        assert p["moving_bed"] in (True, False, None) and p["speed"] in ("fast", "medium", None) and p["nozzles"], p
    v2 = next(p for p in printers if p["name"] == "Creality Ender-3 V2")
    assert v2["bed"][:2] == [220, 220] and v2["speed"] == "medium"


def _browser(call: str, builtins: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to run the browser's printer picker")
    script = """
      import fs from 'node:fs';
      const source = fs.readFileSync(process.argv[1]).toString('base64');
      const m = await import('data:text/javascript;base64,' + source);
      const [call, builtins, catalogue] = JSON.parse(fs.readFileSync(0, 'utf8'));
      const find = (name) => catalogue.find((p) => p.name === name);
      process.stdout.write(JSON.stringify(eval(call)));
    """
    catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))["printers"]
    out = subprocess.run([node, "--input-type=module", "-e", script, str(ROOT / "web" / "mypresets.js")],
                         input=json.dumps([call, builtins, catalogue]), text=True, encoding="utf-8", capture_output=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_a_printer_from_the_catalogue_says_what_is_assumed_and_saves():
    builtins = presets()["printers"]
    r = _browser('m.fromOrcaPrinter(find("Creality Ender-3 V2"), builtins, "none")', builtins)
    v = r["values"]
    assert (v["name"], v["bed_x"], v["bed_y"], v["bed_z"], v["enclosure"], v["speed"]) == ("Creality Ender-3 V2", 220, 220, 250, "none", "medium")
    assert builtins[v["based_on"]]["enclosure"] == "none"
    assert "From the OrcaSlicer profile: bed 220 × 220 × 250 mm" in r["note"] and "Assumed, please check: no hardened nozzle" in r["note"]
    # The form's values go through toSpec into the engine like any printer the user makes.
    spec = _browser(f'm.toSpec("printers", {json.dumps(v)})', builtins)
    p = api.custom_presets(printers={"ender3v2": spec})["printers"]["my_ender3v2"]
    assert p["bed"] == [220, 220, 250] and p["speed"] == "medium"
    api.custom_presets()


def test_three_answers_start_from_the_closest_printer():
    builtins = presets()["printers"]
    small = _browser('m.fromAnswers(builtins, {enclosure: "none", side: 180, directDrive: true})', builtins)["values"]
    assert small["based_on"] == "bambu_a1_mini" and small["bed_x"] == small["bed_y"] == 180
    closed = _browser('m.fromAnswers(builtins, {enclosure: "heated", side: 300, directDrive: false})', builtins)["values"]
    assert builtins[closed["based_on"]]["enclosure"] == "heated" and closed["speed"] == "medium"
