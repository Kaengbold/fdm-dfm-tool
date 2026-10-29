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


def _condition(expr: str, machine: dict) -> bool:
    """Evaluate a profile's ``compatible_printers_condition`` (``key=~/regex/``, ``==``, and/or)
    against a machine profile. Anything we can't read counts as not compatible."""
    def regex(value, pattern):
        return re.search(pattern, value if isinstance(value, str) else " ".join(map(str, value))) is not None

    py = re.sub(r"(\w+)\s*!~\s*/(.*?)/", lambda m: f"(not _re({m.group(1)}, {m.group(2)!r}))", expr)
    py = re.sub(r"(\w+)\s*=~\s*/(.*?)/", lambda m: f"_re({m.group(1)}, {m.group(2)!r})", py)
    env = {"_re": regex}
    for k, v in machine.items():
        if isinstance(v, list):
            env[k] = [float(x) if re.fullmatch(r"[\d.]+", str(x)) else x for x in v]
        elif isinstance(v, str) and re.fullmatch(r"[\d.]+", v):
            env[k] = float(v)
        else:
            env[k] = v
    try:
        return bool(eval(py, {"__builtins__": {}}, env))  # noqa: S307 - installed slicer profiles, no builtins
    except Exception:
        return False


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


def slice_3mf(model: bytes, profiles: dict[str, Path], orca: Path, timeout: int = 900) -> dict:
    """Slice with OrcaSlicer; print time, filament and layers from the G-code it writes."""
    work = Path(tempfile.mkdtemp(prefix="fdm-orca-"))
    try:
        src = work / "part.3mf"
        src.write_bytes(model)
        cmd = [str(orca), "--slice", "0", "--outputdir", str(work / "out"),
               "--load-settings", f"{profiles['machine']};{profiles['process']}", "--load-filaments", str(profiles["filament"]), str(src)]
        (work / "out").mkdir()
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        gcode = sorted((work / "out").glob("*.gcode"))
        if not gcode:
            code = run.returncode - 2**32 if run.returncode >= 2**31 else run.returncode
            if code == -17:  # CLI_PROCESS_NOT_COMPATIBLE
                raise RuntimeError(f"OrcaSlicer's command line won't combine {profiles['machine'].stem} with "
                                   f"{profiles['process'].stem} (it does in the app). Slice the exported 3MF in OrcaSlicer instead.")
            raise RuntimeError(f"OrcaSlicer did not slice the part (exit {code}): {(run.stdout + run.stderr)[-500:]}")
        return parse_gcode(gcode[0].read_text(errors="replace"))
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
    if m := re.search(r"total layer number:\s*(\d+)", head):
        out["layers"] = int(m.group(1))
    if m := re.search(r"filament used \[mm\]\s*=\s*([\d.]+)", tail):
        out["filament_mm"] = float(m.group(1))
    if m := re.search(r"filament used \[cm3\]\s*=\s*([\d.]+)", tail):
        out["filament_cm3"] = float(m.group(1))
    out["support"] = "; FEATURE: Support" in text
    return out
