"""Real slicing in local mode: run the installed OrcaSlicer on our 3MF export.

The pseudo-slicer (slicer.py) estimates; this gives the slicer's own numbers: print
time, filament, layer count. Our per-object settings ride along in the 3MF (checked by
tools/slicer_check.py); printer, process and filament come from OrcaSlicer's own system
profiles, matched to our presets by name. Local-only (subprocess), not in the web app.
"""

from __future__ import annotations

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
    ``inherits`` chain merged in (child keys win)."""
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
        parent = p["own"].get("inherits")
        return {**(full(parent, (*seen, name)) if parent else {}), **p["own"]}

    return {n: {"path": p["path"], "kind": p["kind"], "data": full(n), "own": p["own"]} for n, p in raw.items()}


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


def match_profiles(resources: Path, printer_name: str, nozzle: str, material: str, layer_height: float) -> dict[str, Path]:
    """OrcaSlicer machine, process and filament profiles for our printer / material, or KeyError."""
    profiles = _profiles(resources)
    usable = {n: p for n, p in profiles.items() if p["own"].get("instantiation") == "true"}
    base = _norm(re.sub(r"\(.*?\)", "", printer_name)).rstrip("+")
    want = f"{nozzle} nozzle"
    machines = [n for n, p in usable.items() if p["kind"] == "machine" and n.endswith(want) and _norm(n).startswith(base)]
    if not machines:
        raise KeyError(f"No OrcaSlicer profile for {printer_name} with a {nozzle} mm nozzle")
    machine = min(machines, key=len)
    mdata = profiles[machine]["data"]

    def compatible(n):
        d = profiles[n]["data"]
        if d.get("compatible_printers"):
            return machine in d["compatible_printers"]
        cond = d.get("compatible_printers_condition", "")
        return bool(cond) and _condition(cond, mdata)

    processes = [n for n, p in usable.items() if p["kind"] == "process" and compatible(n)]
    if not processes:
        raise KeyError(f"No OrcaSlicer process profile for {machine}")
    process = min(processes, key=lambda n: (abs(float(profiles[n]["data"].get("layer_height", 0.2)) - layer_height),
                                           "standard" not in n.lower(), len(n)))
    ftype = FILAMENT_TYPE.get(material, material.upper())
    filaments = [n for n, p in usable.items() if p["kind"] == "filament" and ftype in p["data"].get("filament_type", []) and compatible(n)]
    if not filaments:
        raise KeyError(f"No OrcaSlicer {ftype} filament profile for {machine}")
    filament = min(filaments, key=lambda n: ("Generic" not in n, len(n)))
    return {k: profiles[n]["path"] for k, n in (("machine", machine), ("process", process), ("filament", filament))}


def slice_3mf(model: bytes, profiles: dict[str, Path], orca: Path, timeout: int = 900, keep_gcode: bool = False) -> dict:
    """Slice with OrcaSlicer; print time, filament and layers from the G-code it writes
    (and the G-code itself as ``gcode`` with ``keep_gcode``, for the detailed simulation)."""
    work = Path(tempfile.mkdtemp(prefix="fdm-orca-"))
    try:
        src = work / "part.3mf"
        src.write_bytes(model)
        flat = {k: _flat_profile(Path(v), work) for k, v in profiles.items()}
        cmd = [str(orca), "--slice", "0", "--outputdir", str(work / "out"),
               "--load-settings", f"{flat['machine']};{flat['process']}", "--load-filaments", str(flat["filament"]), str(src)]
        (work / "out").mkdir()
        # cwd: OrcaSlicer writes a 00000.log ("Invalid T command") into the folder it runs in.
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=work)
        gcode = sorted((work / "out").glob("*.gcode"))
        if not gcode:
            code = run.returncode - 2**32 if run.returncode >= 2**31 else run.returncode
            if code == -17:  # CLI_PROCESS_NOT_COMPATIBLE
                raise RuntimeError(f"OrcaSlicer's command line won't combine {profiles['machine'].stem} with "
                                   f"{profiles['process'].stem} (it does in the app). Slice the exported 3MF in OrcaSlicer instead.")
            raise RuntimeError(f"OrcaSlicer did not slice the part (exit {code}): {(run.stdout + run.stderr)[-500:]}")
        text = gcode[0].read_text(errors="replace")
        return {**parse_gcode(text), **({"gcode": text} if keep_gcode else {})}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _flat_profile(path: Path, work: Path) -> Path:
    """A copy of a system profile with its ``inherits`` chain merged in. OrcaSlicer's command line
    does not follow ``inherits`` for profile files: every key the file doesn't set itself falls back
    to the built-in default (e.g. a 2 mm³/s melt rate instead of the profile's 12 for a P1S's PLA)."""
    resources = next((p for p in path.parents if p.name == "profiles"), None)
    data = json.loads(path.read_text(encoding="utf-8"))
    if resources is not None and data.get("name") in (index := _profiles(resources)):
        data = dict(index[data["name"]]["data"])
    data.pop("inherits", None)
    out = work / f"{path.parent.name}-{path.name}"
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


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
