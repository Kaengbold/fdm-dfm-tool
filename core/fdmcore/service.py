"""How the part is used: how hot it gets and whether the load stays on (T-161).

Kept apart from strength.py so the web app can use it without loading scipy.
"""

from __future__ import annotations

import numpy as np

from .profile import Profile

# Stiffness and strength left when warm, by how far the temperature is from room (23 °C) to the material's
# heat limit (0 = room, 1 = at the limit). Measured on printed PLA, Young's modulus at 30 / 40 / 50 / 60 °C
# = 1400 / 1143 / 961 / 350 MPa with Tg ~60 °C, strength falling the same way (Grasso et al. 2018, Rapid
# Prototyping Journal, doi:10.1108/RPJ-04-2017-0055); used for other materials relative to their own limit.
HEAT_RETENTION = ((0.0, 1.0), (0.19, 1.0), (0.46, 0.82), (0.73, 0.69), (1.0, 0.25), (1.15, 0.05))
ROOM_C = 23.0
# A load that stays on: plastics creep (slowly stretch) and can break under less than their short-term
# strength. No printed-plastic data good enough for a number yet: ask for ×3 instead of ×2 (rule of thumb).
CONSTANT_LOAD_SAFETY = 3.0


def heat_limit(profile: Profile) -> float:
    m = profile.material
    return float(m.get("heat_limit_c", m["softening_c"] + 15))  # own materials may not have one


def heat_factor(profile: Profile) -> float:
    """Share of stiffness and strength left at the temperature it is used at (``HEAT_RETENTION``)."""
    t = profile.use_temp_c
    if t is None or t <= ROOM_C:
        return 1.0
    x = (t - ROOM_C) / max(heat_limit(profile) - ROOM_C, 1.0)
    xs, ks = zip(*HEAT_RETENTION)
    return float(np.interp(x, xs, ks))


def required_safety(profile: Profile) -> float:
    return CONSTANT_LOAD_SAFETY if profile.constant_load else 2.0
