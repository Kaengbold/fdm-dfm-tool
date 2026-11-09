"""Print profile = printer + material + nozzle presets, with derived values."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
from pathlib import Path

import yaml

DATA = Path(__file__).parent / "data"


@lru_cache(maxsize=None)
def _read_data(name: str) -> dict:
    return yaml.safe_load((DATA / f"{name}.yaml").read_text(encoding="utf-8"))


_expert: dict[str, dict] = {}  # file name -> {"a.b.c": value}: the Expert settings window's changes
_expert_data: dict[str, dict] = {}


def load_data(name: str) -> dict:
    """A data file, with the user's Expert changes (``set_expert``) on top."""
    over = _expert.get(name)
    if not over:
        return _read_data(name)
    if name not in _expert_data:
        import copy

        data = copy.deepcopy(_read_data(name))
        for path, value in over.items():
            *head, last = path.split(".")
            node = data
            for key in head:
                node = node[int(key)] if isinstance(node, list) else node[key]
            node[int(last) if isinstance(node, list) else last] = value
        _expert_data[name] = data
    return _expert_data[name]


def set_expert(changes: dict[str, dict[str, float]]) -> None:
    """Replace the Expert changes: ``{"thresholds": {"features.vanish_below_nozzle": 0.6}}``. Unknown
    paths raise KeyError, so a stale saved setting doesn't silently do nothing."""
    for name, over in changes.items():
        data = _read_data(name)
        for path in over:
            node = data
            try:
                for key in path.split("."):
                    node = node[int(key)] if isinstance(node, list) else node[key]
            except (KeyError, IndexError, ValueError, TypeError):
                raise KeyError(f"Unknown setting {name}.{path}") from None
            if not isinstance(node, (int, float)) or isinstance(node, bool):
                raise KeyError(f"{name}: {path} is not a number")
    _expert.clear()
    _expert.update({k: dict(v) for k, v in changes.items() if v})
    _expert_data.clear()


_custom: dict = {"printers": {}, "materials": {}}  # the user's own presets (set_custom_presets)
_merged: dict | None = None


def presets() -> dict:
    """Built-in presets plus the user's own (their ids start with "my_")."""
    global _merged
    if _merged is None:
        base = load_data("presets")
        _merged = {**base, "printers": {**base["printers"], **_custom["printers"]},
                   "materials": {**base["materials"], **_custom["materials"]}}
    return _merged


# What a user preset may set: key -> (type, min, max). Everything else comes from ``based_on``.
PRINTER_FIELDS = {
    "name": (str, None, None),
    "bed": (list, 20, 2000),
    "enclosure": (("none", "passive", "heated"), None, None),
    "hardened_nozzle": (bool, None, None),
    "speed": (("fast", "medium"), None, None),
    "multi_material": (("none", "optional", "builtin"), None, None),
    "bed_surface": (("unknown", "textured_pei", "smooth_pei", "glass", "cool_plate"), None, None),
}
MATERIAL_FIELDS = {
    "name": (str, None, None),
    "max_overhang_deg": (float, 20, 75),
    "max_bridge_mm": (float, 1, 150),
    "min_layer_time_s": (float, 0, 60),
    "warp_risk": (("low", "medium", "high"), None, None),
    "abrasive": (bool, None, None),
    "hygroscopic": (("low", "medium", "high"), None, None),
    "max_flow_mm3s": (float, 1, 100),
    "z_strength_factor": (dict, 0.05, 1.0),
    "tensile_xy_mpa": (float, 1, 300),
    "modulus_xy_mpa": (float, 10, 50000),
    "density_g_cm3": (float, 0.5, 3.0),
    "shrinkage_pct": (float, 0, 5),
    "warp_length_mm": (dict, 10, 1000),  # measured with the warping calibration print (overrides thresholds.yaml)
    "heat_limit_c": (float, 20, 400),  # where printed parts lose most stiffness (glass transition)
    "uv_resistance": (("good", "fair", "poor"), None, None),
    "fit_gap_mm": ("fits", 0.0, 2.0),  # measured with the fit test print: {"press", "snug", "sliding", "loose"}
    "fibre": (("none", "carbon", "glass"), None, None),  # chopped fibre filled (-CF / -GF)
    "hole_error_mm": (float, -2, 2),  # measured − modelled diameter of a printed hole (negative: holes come out small)
    "outer_error_mm": (float, -2, 2),  # measured − modelled outer size of a printed part
    "price_per_kg": (float, 0, 10000),  # what the user pays, any currency: the cost of a print
    "spool_left_g": (float, 0, 100000),  # filament left on the spool now: warns when a print won't fit on it
}
ENCLOSURES = ("none", "passive", "heated")

FIBRE_NAME = re.compile(r"(?<![a-z])[cg]f(?![a-z])|carbon.?fib|glass.?fib", re.I)  # not "polycarbonate"


def fibre(material: dict) -> str | None:
    """"carbon" or "glass" for chopped-fibre filled filament (PLA-CF, PETG-CF, PA-GF…), else None: from
    the preset's ``fibre`` value, or for a user's own material without one, from -CF / -GF in its name.
    Fibres make a print stiffer, not stronger (rules.yaml fibre_filled)."""
    f = material.get("fibre")
    if f:
        return None if f == "none" else f
    m = FIBRE_NAME.search(material.get("name", ""))
    return None if m is None else "glass" if m.group(0)[0].lower() == "g" else "carbon"
FITS = ("press", "snug", "sliding", "loose")


def _custom_entry(kind: str, key: str, spec: dict, fields: dict, base_table: dict) -> dict:
    base_id = spec.get("based_on")
    if base_id not in base_table:
        raise ValueError(f"{key}: based_on must be one of the built-in {kind}s ({', '.join(base_table)})")
    out = {k: v for k, v in base_table[base_id].items()}
    for field, value in spec.items():
        if field == "based_on":
            continue
        if field not in fields:
            raise ValueError(f"{key}: unknown field {field!r}")
        kind_, lo, hi = fields[field]
        if kind_ == "fits":
            value = {k: float(v) for k, v in dict(value).items() if k in FITS}
            if not all(lo <= v <= hi for v in value.values()):
                raise ValueError(f"{key}.{field} gaps must be between {lo} and {hi} mm")
        elif isinstance(kind_, tuple):
            if value not in kind_:
                raise ValueError(f"{key}.{field} must be one of {', '.join(kind_)}")
        elif kind_ is bool:
            value = bool(value)
        elif kind_ is str:
            value = str(value)[:60]
        elif kind_ is list:
            value = [float(v) for v in value]
            if len(value) != 3 or not all(lo <= v <= hi for v in value):
                raise ValueError(f"{key}.{field} must be three sizes in mm ({lo}–{hi})")
        elif kind_ is dict:
            value = {e: float(value.get(e, out.get(field, {}).get(e, hi))) for e in ENCLOSURES}
            if not all(lo <= v <= hi for v in value.values()):
                raise ValueError(f"{key}.{field} must be between {lo} and {hi}")
        else:
            value = float(value)
            if not lo <= value <= hi:
                raise ValueError(f"{key}.{field} must be between {lo} and {hi}")
        out[field] = value
    out["based_on"] = base_id
    return out


def set_custom_presets(printers: dict | None = None, materials: dict | None = None) -> None:
    """Replace the user's own presets. Each one names a built-in preset in ``based_on`` and overrides
    some fields (PRINTER_FIELDS / MATERIAL_FIELDS); ids get a "my_" prefix so they never clash."""
    global _custom, _merged
    base = load_data("presets")
    new = {"printers": {}, "materials": {}}
    for table, specs, fields in (("printers", printers, PRINTER_FIELDS), ("materials", materials, MATERIAL_FIELDS)):
        for key, spec in (specs or {}).items():
            pid = key if key.startswith("my_") else f"my_{key}"
            new[table][pid] = _custom_entry(table[:-1], pid, spec, fields, base[table])
    _custom, _merged = new, None


GOALS = ("balanced", "strength", "speed", "material")


@dataclass(frozen=True)
class Profile:
    printer_id: str = "bambu_p1s"
    material_id: str = "pla"
    nozzle: str = "0.4"
    multi_material: bool = False  # AMS / MMU / second nozzle available (support interfaces in another material)
    goal: str = "balanced"  # what orientation and settings optimise for: see GOALS
    use_temp_c: float | None = None  # hottest the part gets in use (sun, car, near a heater); None = room
    constant_load: bool = False  # the load stays on (hangs permanently): plastic slowly creeps

    @property
    def printer(self) -> dict:
        return presets()["printers"][self.printer_id]

    @property
    def material(self) -> dict:
        return presets()["materials"][self.material_id]

    @property
    def line_width(self) -> float:
        return presets()["nozzles"][self.nozzle]["line_width"]

    @property
    def layer_height(self) -> float:
        return presets()["nozzles"][self.nozzle]["layer_height"]

    @property
    def enclosure(self) -> str:
        return self.printer["enclosure"]

    @property
    def speeds(self) -> dict:
        factor = presets()["nozzles"][self.nozzle].get("speed", {})
        return {k: v * factor.get(k, 1.0) for k, v in presets()["speeds"][self.printer["speed"]].items()}

    @property
    def z_strength_factor(self) -> float:
        return self.material["z_strength_factor"][self.enclosure]

    @classmethod
    def from_dict(cls, d: dict | None) -> "Profile":
        d = d or {}
        p = cls(
            printer_id=d.get("printer", cls.printer_id),
            material_id=d.get("material", cls.material_id),
            nozzle=str(d.get("nozzle", cls.nozzle)),
            multi_material=bool(d.get("multi_material", False)),
            goal=d.get("goal", "balanced"),
            use_temp_c=None if d.get("use_temp_c") in (None, "") else float(d["use_temp_c"]),
            constant_load=bool(d.get("constant_load", False)),
        )
        if p.goal not in GOALS:
            raise ValueError(f"Unknown goal: {p.goal} (use one of {', '.join(GOALS)})")
        for table, key in (("printers", p.printer_id), ("materials", p.material_id), ("nozzles", p.nozzle)):
            if key not in presets()[table]:
                raise ValueError(f"Unknown {table[:-1]} preset: {key}")
        return p
