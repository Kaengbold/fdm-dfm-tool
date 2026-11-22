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
    # The browser modules as data: URLs (Node reads .js here as CommonJS); i18n.js gets its English table the same way.
    script = """
      import fs from 'node:fs';
      const url = (src) => 'data:text/javascript;base64,' + Buffer.from(src).toString('base64');
      const web = process.argv[1];
      const m = await import(url(fs.readFileSync(web + '/mypresets.js', 'utf8')));
      const en = url(fs.readFileSync(web + '/messages/en.js', 'utf8'));
      const { t } = await import(url(fs.readFileSync(web + '/i18n.js', 'utf8').replace('"./messages/en.js"', JSON.stringify(en))));
      const [call, builtins, catalogue] = JSON.parse(fs.readFileSync(0, 'utf8'));
      const find = (name) => catalogue.find((p) => p.name === name);
      process.stdout.write(JSON.stringify(eval(call)));
    """
    catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))["printers"]
    out = subprocess.run([node, "--input-type=module", "-e", script, str(ROOT / "web")],
                         input=json.dumps([call, builtins, catalogue]), text=True, encoding="utf-8", capture_output=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_a_printer_from_the_catalogue_says_what_is_assumed_and_saves():
    builtins = presets()["printers"]
    r = _browser('(({values, note}) => ({values, note, text: t(note)}))(m.fromOrcaPrinter(find("Creality Ender-3 V2"), builtins, "none"))', builtins)
    v = r["values"]
    assert (v["name"], v["bed_x"], v["bed_y"], v["bed_z"], v["enclosure"], v["speed"]) == ("Creality Ender-3 V2", 220, 220, 250, "none", "medium")
    assert builtins[v["based_on"]]["enclosure"] == "none"
    # The note is a message (key + numbers, T-454) the app formats.
    assert r["note"]["key"] == "printer.noteFromProfile" and r["note"]["params"]["from"][0]["params"] == {"x": 220, "y": 220, "z": 250}
    assert r["text"].startswith("From the OrcaSlicer profile: bed 220 × 220 × 250 mm") and "Assumed, please check: no hardened nozzle" in r["text"]
    # The form's values go through toSpec into the engine like any printer the user makes.
    spec = _browser(f'm.toSpec("printers", {json.dumps(v)})', builtins)
    p = api.custom_presets(printers={"ender3v2": spec})["printers"]["my_ender3v2"]
    assert p["bed"] == [220, 220, 250] and p["speed"] == "medium"
    api.custom_presets()


def test_message_keys_format_numbers_plurals_and_lists():
    """T-454: the shape every new text follows (web/i18n.js)."""
    r = _browser('[t("printer.notListedMore", {count: 410}), t("printer.notListedMore", {count: 1}), t("no.such.key"),'
                 ' t({key: "printer.fact.bed", params: {x: 1000, y: 220.5, z: 3}})]', {})
    assert r == ["My printer isn't listed (+ about 410 more)…", "My printer isn't listed (+ 1 more)…", "no.such.key", "bed 1,000 × 220.5 × 3 mm"]


def test_every_key_used_by_the_page_exists():
    """data-i18n keys in index.html and t("…") keys in the scripts are all in the English table."""
    import re

    web = ROOT / "web"
    table = (web / "messages" / "en.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'^  "([\w.]+)":', table, re.M))
    used = set(re.findall(r'data-i18n(?:-placeholder|-title)?="([\w.]+)"', (web / "index.html").read_text(encoding="utf-8")))
    for script in [p for p in web.rglob("*.js") if p.name != "i18n.js"]:  # i18n.js: t() itself, with examples
        src = script.read_text(encoding="utf-8")
        used |= set(re.findall(r'\bt\("([\w.]+)"', src)) | set(re.findall(r'\bmsg\("([\w.]+)"', src))
    assert used and not used - keys, used - keys


def test_three_answers_start_from_the_closest_printer():
    builtins = presets()["printers"]
    small = _browser('m.fromAnswers(builtins, {enclosure: "none", side: 180, directDrive: true})', builtins)["values"]
    assert small["based_on"] == "bambu_a1_mini" and small["bed_x"] == small["bed_y"] == 180
    closed = _browser('m.fromAnswers(builtins, {enclosure: "heated", side: 300, directDrive: false})', builtins)["values"]
    assert builtins[closed["based_on"]]["enclosure"] == "heated" and closed["speed"] == "medium"
