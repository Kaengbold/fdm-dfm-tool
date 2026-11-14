"""Real slicing in local mode: run the installed OrcaSlicer on our 3MF export.

The pseudo-slicer (slicer.py) estimates; this gives the slicer's own numbers: print
time, filament, layer count. Our per-object settings ride along in the 3MF (checked by
tools/slicer_check.py); printer, process and filament come from OrcaSlicer's system
profiles, matched to our presets by name. Local-only (subprocess), not in the web app.

Where the profiles come from: OrcaSlicer up to 2.4 keeps them as JSON files next to the program;
2.5 packs them into ``.opc`` files in its own format. So we ship the ones we need, flattened
(``data/orca_profiles.json.gz``, made by ``tools/orca_profiles.py`` from OrcaSlicer's source at a
fixed version), and slicing works with any OrcaSlicer version on any computer. The installed JSON
files still win when there are any, as they match that slicer exactly.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

ORCA_PATHS = [
    os.environ.get("ORCA_SLICER", ""),
    "C:/Program Files/OrcaSlicer/orca-slicer.exe",
    "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    "/usr/bin/orca-slicer",
    "orca-slicer",
]
# Our material id -> OrcaSlicer filament_type.
FILAMENT_TYPE = {"pla": "PLA", "pla_plus": "PLA", "petg": "PETG", "abs": "ABS", "asa": "ASA", "pc": "PC", "tpu_95a": "TPU", "pa_cf": "PA-CF"}
BUNDLED = Path(__file__).parent / "data" / "orca_profiles.json.gz"


def find_orca() -> Path | None:
    for p in ORCA_PATHS:
        if p and (Path(p).exists() or shutil.which(p)):
            return Path(shutil.which(p) or p)
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


@lru_cache(maxsize=2)
def _profiles(resources: Path) -> dict[str, dict]:
    """Every system profile by name: {"path", "kind" (machine/process/filament), "data"} with the
    ``inherits`` chain and the ``include``d templates merged in, like OrcaSlicer does: the parent, then
    each template in the order listed (2.5: start / end G-code …), then the profile's own keys."""
    raw = {}
    for f in resources.glob("*/*/**/*.json"):
        kind = f.relative_to(resources).parts[1]
        if kind not in ("machine", "process", "filament"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "name" in data:
            raw[data["name"]] = {"path": f, "kind": kind, "own": data}

    def full(name, seen=()):
        p = raw.get(name)
        if p is None or name in seen:
            return {}
        own, seen = p["own"], (*seen, name)
        out = full(own["inherits"], seen) if own.get("inherits") else {}
        includes = own.get("include") or []
        for template in [includes] if isinstance(includes, str) else includes:
            out.update(full(template, seen))
        out.update(own)
        out.pop("include", None)  # merged in here: the command line can't look templates up
        return out

    return {n: {"path": p["path"], "kind": p["kind"], "data": full(n), "own": p["own"]} for n, p in raw.items()}


@lru_cache(maxsize=1)
def bundled_profiles() -> dict[str, dict]:
    """The profiles we ship, in the same form as ``_profiles`` ({} if the file is missing)."""
    if not BUNDLED.exists():
        return {}
    snap = json.loads(gzip.decompress(BUNDLED.read_bytes()))
    return {n: {"kind": p["kind"], "data": p["data"], "own": p["data"]} for n, p in snap["profiles"].items()}


def installed_profiles(orca: Path) -> dict[str, dict]:
    """The installed OrcaSlicer's profiles when it keeps them as JSON files (up to 2.4; the macOS app
    has them under Resources), else {}."""
    for resources in (orca.parent / "resources" / "profiles", orca.parents[1] / "Resources" / "profiles"):
        if resources.is_dir():
            return _profiles(resources)
    return {}


_TOKEN = re.compile(r"""\s*(?:
    (?P<regex>/(?:\\.|[^/\\])*/) |
    (?P<str>"(?:\\.|[^"\\])*") |
    (?P<num>\d+(?:\.\d+)?) |
    (?P<word>[A-Za-z_]\w*) |
    (?P<op>==|!=|<=|>=|=~|!~|&&|\|\||[<>!()\[\]]))""", re.X)


def _condition(expr: str, machine: dict) -> bool:
    """Evaluate a profile's ``compatible_printers_condition`` against a machine profile: comparisons,
    ``key=~/regex/`` / ``!~``, ``and`` / ``or`` / ``not`` (or ``&&`` / ``||`` / ``!``), ``key[i]``.
    A small parser, not ``eval``: profile files are data. Anything we can't read counts as not compatible."""
    tokens, pos = [], 0
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            if expr[pos:].strip():
                return False
            break
        tokens.append((m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
    try:
        value, i = _or(tokens, 0, machine)
        return bool(value) if i == len(tokens) else False
    except (IndexError, ValueError, TypeError, re.error):
        return False


def _value(v):
    """Profile values are strings: numbers compare as numbers, lists stay lists."""
    if isinstance(v, list):
        return [_value(x) for x in v]
    if isinstance(v, str) and re.fullmatch(r"\d+(?:\.\d+)?", v):
        return float(v)
    return v


def _or(t, i, env):
    a, i = _and(t, i, env)
    while i < len(t) and t[i][1] in ("or", "||"):
        b, i = _and(t, i + 1, env)
        a = bool(a) or bool(b)
    return a, i


def _and(t, i, env):
    a, i = _not(t, i, env)
    while i < len(t) and t[i][1] in ("and", "&&"):
        b, i = _not(t, i + 1, env)
        a = bool(a) and bool(b)
    return a, i


def _not(t, i, env):
    if t[i][1] in ("not", "!"):
        a, i = _not(t, i + 1, env)
        return not a, i
    return _compare(t, i, env)


def _compare(t, i, env):
    a, i = _atom(t, i, env)
    if i < len(t) and t[i][1] in ("=~", "!~"):
        op, (kind, pattern) = t[i][1], t[i + 1]
        if kind != "regex":
            raise ValueError(pattern)
        text = a if isinstance(a, str) else " ".join(map(str, a))
        found = re.search(pattern[1:-1], text) is not None
        return found == (op == "=~"), i + 2
    if i < len(t) and t[i][1] in ("==", "!=", "<", ">", "<=", ">="):
        op = t[i][1]
        b, i = _atom(t, i + 1, env)
        if op in ("==", "!="):
            return (a == b) == (op == "=="), i
        return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op], i
    return a, i


def _atom(t, i, env):
    kind, text = t[i]
    if text == "(":
        a, i = _or(t, i + 1, env)
        if t[i][1] != ")":
            raise ValueError(text)
        return a, i + 1
    if kind == "num":
        return float(text), i + 1
    if kind == "str":
        return re.sub(r"\\(.)", r"\1", text[1:-1]), i + 1
    if kind == "word":
        if text in ("true", "false"):
            return text == "true", i + 1
        if text not in env:
            raise ValueError(text)
        a, i = _value(env[text]), i + 1
        if i < len(t) and t[i][1] == "[":
            index, i = _atom(t, i + 1, env)
            if t[i][1] != "]":
                raise ValueError(text)
            a, i = a[int(index)], i + 1
        return a, i
    raise ValueError(text)


def fits(data: dict, machine: str, mdata: dict) -> bool:
    """Does a process / filament profile name this machine (a list, or a condition on its settings)?"""
    if data.get("compatible_printers"):
        return machine in data["compatible_printers"]
    cond = data.get("compatible_printers_condition", "")
    return bool(cond) and _condition(cond, mdata)


def match_profiles(profiles: dict[str, dict], printer_name: str, nozzle: str, material: str, layer_height: float) -> dict[str, str]:
    """Names of the OrcaSlicer machine, process and filament profiles for our printer / material, or
    KeyError. ``profiles``: ``installed_profiles``, ``bundled_profiles`` or ``_profiles`` of a folder."""
    usable = {n: p for n, p in profiles.items() if p["own"].get("instantiation") == "true"}
    base = _norm(re.sub(r"\(.*?\)", "", printer_name)).rstrip("+")
    want = f"{nozzle} nozzle"
    machines = [n for n, p in usable.items() if p["kind"] == "machine" and n.endswith(want) and _norm(n).startswith(base)]
    if not machines:
        raise KeyError(f"No OrcaSlicer profile for {printer_name} with a {nozzle} mm nozzle")
    machine = min(machines, key=len)

    def compatible(n):
        return fits(profiles[n]["data"], machine, profiles[machine]["data"])

    processes = [n for n, p in usable.items() if p["kind"] == "process" and compatible(n)]
    if not processes:
        raise KeyError(f"No OrcaSlicer process profile for {machine}")
    process = min(processes, key=lambda n: (abs(float(profiles[n]["data"].get("layer_height", 0.2)) - layer_height),
                                           "standard" not in n.lower(), len(n)))
    ftype = FILAMENT_TYPE.get(material, material.upper())
    typed = [n for n, p in usable.items() if p["kind"] == "filament" and ftype in p["data"].get("filament_type", [])]

    def for_any(n):  # OrcaSlicer's own generic filaments ("Generic PC @System") name no printer
        d = profiles[n]["data"]
        return not d.get("compatible_printers") and not d.get("compatible_printers_condition")

    # The printer maker's filament first; when they have none of this type, the generic one.
    filaments = [n for n in typed if compatible(n)] or [n for n in typed if for_any(n)]
    if not filaments:
        raise KeyError(f"No OrcaSlicer {ftype} filament profile for {machine}")
    filament = min(filaments, key=lambda n: ("Generic" not in n, len(n)))
    return {"machine": machine, "process": process, "filament": filament}


def pick_profiles(orca: Path | None, printer_name: str, nozzle: str, material: str, layer_height: float) -> dict[str, dict]:
    """Machine, process and filament for OrcaSlicer, each ``{"name", "data"}`` with the ``inherits``
    chain merged in: from the installed OrcaSlicer's JSON files when it has them and knows this
    printer, else from the profiles we ship. KeyError when neither has it."""
    error = KeyError(f"No OrcaSlicer profiles for {printer_name}")
    for profiles in (installed_profiles(orca) if orca else {}, bundled_profiles()):
        if not profiles:
            continue
        try:
            names = match_profiles(profiles, printer_name, nozzle, material, layer_height)
        except KeyError as e:
            error = e
            continue
        return {k: {"name": n, "data": profiles[n]["data"]} for k, n in names.items()}
    raise error


# OrcaSlicer's bed plates and the filament setting with the bed temperature on each (0 = not allowed).
PLATES = (("Textured PEI Plate", "textured_plate_temp"), ("High Temp Plate", "hot_plate_temp"),
          ("Engineering Plate", "eng_plate_temp"), ("Cool Plate", "cool_plate_temp"))


def bed_type(filament: dict) -> str:
    """A plate the filament may be printed on. Without one OrcaSlicer takes the Cool Plate, and
    refuses PETG, ASA, PC … on it ("Cool Plate does not support filament 1")."""
    for plate, key in PLATES:
        temps = filament.get(key)
        if temps and float(temps[0] if isinstance(temps, list) else temps) > 0:
            return plate
    return PLATES[0][0]


def write_profiles(chosen: dict[str, dict], folder: Path) -> dict[str, Path]:
    """The chosen profiles as files for OrcaSlicer's command line. Flattened, because the command line
    does not follow ``inherits`` itself (up to 2.4: every key the file doesn't set falls back to the
    built-in default, e.g. a 2 mm³/s melt rate instead of 12 for a P1S's PLA; 2.5 only looks a parent
    up in the profiles the OrcaSlicer app has installed). ``from`` and ``type`` say what the file is."""
    out = {}
    for kind, p in chosen.items():
        data = {k: v for k, v in p["data"].items() if k != "inherits"}
        data.setdefault("type", kind)
        data.setdefault("from", "system")
        if kind == "process" and "filament" in chosen:
            data["curr_bed_type"] = bed_type(chosen["filament"]["data"])
        out[kind] = folder / f"{kind}.json"
        out[kind].write_text(json.dumps(data), encoding="utf-8")
    return out


def slice_3mf(model: bytes, chosen: dict[str, dict], orca: Path, timeout: int = 900, keep_gcode: bool = False) -> dict:
    """Slice with OrcaSlicer and ``pick_profiles``' profiles; print time, filament and layers from the
    G-code it writes (and the G-code itself as ``gcode`` with ``keep_gcode``, for the detailed simulation)."""
    work = Path(tempfile.mkdtemp(prefix="fdm-orca-"))
    try:
        src = work / "part.3mf"
        src.write_bytes(model)
        flat = write_profiles(chosen, work)
        cmd = [str(orca), "--slice", "0", "--outputdir", str(work / "out"),
               "--load-settings", f"{flat['machine']};{flat['process']}", "--load-filaments", str(flat["filament"]), str(src)]
        (work / "out").mkdir()
        # cwd: OrcaSlicer writes a 00000.log ("Invalid T command") into the folder it runs in.
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=work)
        gcode = sorted((work / "out").glob("*.gcode"))
        if not gcode:
            code = run.returncode - 2**32 if run.returncode >= 2**31 else run.returncode
            if code == -17:  # CLI_PROCESS_NOT_COMPATIBLE
                raise RuntimeError(f"OrcaSlicer's command line won't combine {chosen['machine']['name']} with "
                                   f"{chosen['process']['name']} (it does in the app). Slice the exported 3MF in OrcaSlicer instead.")
            raise RuntimeError(f"OrcaSlicer did not slice the part (exit {code}): {(run.stdout + run.stderr)[-500:]}")
        text = gcode[0].read_text(errors="replace")
        return {**parse_gcode(text), **({"gcode": text} if keep_gcode else {})}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _minutes(text: str) -> float:
    parts = dict((u, float(v)) for v, u in re.findall(r"([\d.]+)([dhms])", text))
    return parts.get("d", 0) * 1440 + parts.get("h", 0) * 60 + parts.get("m", 0) + parts.get("s", 0) / 60


def parse_gcode(text: str) -> dict:
    head = text[:4000]
    tail = text[-60000:]
    out: dict = {}
    if m := re.search(r"total estimated time:\s*([^\n;]+)", head) or re.search(r"estimated printing time \(normal mode\)\s*=\s*([^\n]+)", text):
        out["print_time_min"] = round(_minutes(m.group(1)), 1)
    if m := re.search(r"model printing time:\s*([^\n;]+)", head):
        out["model_time_min"] = round(_minutes(m.group(1)), 1)  # without the printer's start routine
    if m := re.search(r"total layer number:\s*(\d+)", head):
        out["layers"] = int(m.group(1))
    if m := re.search(r"filament used \[mm\]\s*=\s*([\d.]+)", tail):
        out["filament_mm"] = float(m.group(1))
    if m := re.search(r"filament used \[cm3\]\s*=\s*([\d.]+)", tail):
        out["filament_cm3"] = float(m.group(1))
    out["support"] = "; FEATURE: Support" in text
    return out
