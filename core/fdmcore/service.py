"""How the part is used: how hot it gets and whether the load stays on (T-161).

Kept apart from strength.py so the web app can use it without loading scipy.
"""

from __future__ import annotations

from math import log

import numpy as np

from .profile import Profile

# Stiffness and strength left when warm, by how far the temperature is from room (23 °C) to the material's
# heat limit (0 = room, 1 = at the limit). Measured on printed PLA, Young's modulus at 30 / 40 / 50 / 60 °C
# = 1400 / 1143 / 961 / 350 MPa with Tg ~60 °C, strength falling the same way (Grasso et al. 2018, Rapid
# Prototyping Journal, doi:10.1108/RPJ-04-2017-0055); used for other materials relative to their own limit.
HEAT_RETENTION = ((0.0, 1.0), (0.19, 1.0), (0.46, 0.82), (0.73, 0.69), (1.0, 0.25), (1.15, 0.05))
ROOM_C = 23.0
# A load that stays on: plastics creep (slowly stretch) and can break under less than their short-term
# strength. Without a usable calibration, ask for ×3 instead of ×2 (rule of thumb).
CONSTANT_LOAD_SAFETY = 3.0
CREEP_LIFE_DAYS = 365
CREEP_TIMES_MIN = (1, 60, 1440, 10080, 40320)


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


def creep_estimate(profile: Profile) -> dict | None:
    """Estimate one-year sag relative to the one-minute reading, for room-temperature use."""
    if not profile.constant_load or not 18 <= (ROOM_C if profile.use_temp_c is None else profile.use_temp_c) <= 30:
        return None
    readings = profile.material.get("creep_readings", [])
    if len(readings) < 2 or readings[0][0] != 1 or readings[-1][0] < 60:
        return None
    initial = readings[0][1]
    # Compliance under the same force scales with drop. Fit drop against log(time),
    # anchored at the initial reading, and disallow a slope that predicts recovery under load.
    followups = [(log(t), d - initial) for t, d in readings[1:]]
    slope = max(0.0, sum(x * y for x, y in followups) / sum(x * x for x, _ in followups))
    factor = 1.0 + slope * log(CREEP_LIFE_DAYS * 1440) / initial
    return {"factor": factor, "days": CREEP_LIFE_DAYS, "points": len(readings),
            "provisional": len(readings) < 4 or readings[-1][0] < CREEP_TIMES_MIN[-1]}


def required_safety(profile: Profile) -> float:
    if not profile.constant_load:
        return 2.0
    creep = creep_estimate(profile)
    # Treat extra sag as a margin on the short-load check. This is not a fit of time to rupture.
    if creep is None:
        return CONSTANT_LOAD_SAFETY
    # A year extrapolated from hours or days can miss the late speed-up of creep: an early estimate may only
    # raise the margin; four weeks of readings may also lower it.
    return max(CONSTANT_LOAD_SAFETY, 2.0 * creep["factor"]) if creep["provisional"] else 2.0 * creep["factor"]
