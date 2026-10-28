"""Print profile = printer + material + nozzle presets, with derived values."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

DATA = Path(__file__).parent / "data"


@lru_cache(maxsize=None)
def load_data(name: str) -> dict:
    return yaml.safe_load((DATA / f"{name}.yaml").read_text(encoding="utf-8"))


def presets() -> dict:
    return load_data("presets")


@dataclass(frozen=True)
class Profile:
    printer_id: str = "bambu_p1s"
    material_id: str = "pla"
    nozzle: str = "0.4"

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
        return presets()["speeds"][self.printer["speed"]]

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
        )
        for table, key in (("printers", p.printer_id), ("materials", p.material_id), ("nozzles", p.nozzle)):
            if key not in presets()[table]:
                raise ValueError(f"Unknown {table[:-1]} preset: {key}")
        return p
