"""Heat build-up while printing (simple level): how hot each layer still is when the next one
goes on. Small cross-sections — pins, tips, thin towers — get the next layer before they have
cooled below the plastic's softening point, and then droop, blob or lose their shape.

The model is a column of layers along the build direction (1-D heat flow): each new layer
arrives at nozzle temperature, the nozzle and heater block keep warming the top while it prints,
heat flows down into the part and the bed, and the sides and top lose heat to the air (more
with the part fan). Layer times come from the time estimate, slowed to the material's minimum
layer time like the slicer does. Rough: the constants below are typical values, not measured
for a given printer, and heat spreading sideways inside a layer is ignored. At the detailed
level the sliced G-code gives the real time between layers and the plastic in each.
"""

from __future__ import annotations

import numpy as np

from .profile import Profile
from .recommend import goal_plan

C_P = 1500.0  # J/(kg·K): PLA, PETG and ABS are 1200–1800
K_PLASTIC = 0.2  # W/(m·K) through printed plastic (0.13–0.25)
H_STILL = 10.0  # W/(m²·K): still air
H_FAN = 60.0  # W/(m²·K) added by the part cooling fan at 100 %
H_NOZZLE = 60.0  # W/(m²·K) from the nozzle and heater block hovering over the layer being printed
NOZZLE_REACH_MM2 = 50.0  # area the heater block warms (about Ø8 mm)
AMBIENT_C = {"none": 25.0, "passive": 35.0, "heated": 50.0}
DEPTH_MM = 1.0  # how deep below the top the plastic must be firm again (the fresh layer itself is always hot)
MAX_CELLS = 400  # layers simulated (taller parts merge layers)
WINDOW = 60  # only the top cells take part in each step: heat from new layers reaches only so far down
SUBSTEPS = 3
MIN_SPEED = 10.0  # mm/s: slicers slow small layers down to the minimum layer time, but not below this


def layer_heat(zs: np.ndarray, areas: np.ndarray, perimeters: np.ndarray, profile: Profile, sliced: dict | None = None,
               min_speed: float = MIN_SPEED, min_layer_time: float | None = None) -> np.ndarray:
    """Temperature (°C) of each sampled layer once ``DEPTH_MM`` more plastic has been printed on top:
    above the softening point, the top of the part is still soft and can't hold its shape.

    ``zs``: sampled layer heights (mm); ``areas`` (mm²) and ``perimeters`` (mm) of their cross-sections.
    ``sliced`` (detailed level, gcode.layer_times): the real time between layers and the plastic in
    each, instead of estimates from the goal's settings. ``min_speed``: the filament's minimum print speed
    (OrcaSlicer ``slow_down_min_speed``), the floor of the slicer's slow-down for small layers;
    ``min_layer_time`` (s) overrides the material's (``slow_down_layer_time``)."""
    mat = profile.material
    plan = goal_plan(profile)
    lh = plan["layer_height"] if sliced is None else float(np.median(sliced["layer_height"]))
    height = float(zs[-1] + lh / 2)
    n = int(min(MAX_CELLS, max(1, round(height / lh))))
    dz = height / n
    centers = (np.arange(n) + 0.5) * dz
    src = np.clip(np.searchsorted((zs[:-1] + zs[1:]) / 2, centers), 0, len(zs) - 1)  # nearest sampled layer
    a = np.maximum(areas[src], 1e-3) * 1e-6  # m²
    p = perimeters[src] * 1e-3  # m
    if sliced is None:
        wall = np.minimum(perimeters[src] * plan["walls"] * profile.line_width, areas[src])
        fill = np.where(areas[src] > 0, (wall + (areas[src] - wall) * plan["infill"] / 100) / np.maximum(areas[src], 1e-9), 1.0)
        t = layer_seconds(areas[src], perimeters[src], profile, min_speed, min_layer_time) * (dz / lh)
    else:
        mid = sliced["z"] - sliced["layer_height"] / 2
        per_mm = np.interp(centers, mid, sliced["seconds"] / sliced["layer_height"])
        plastic = np.interp(centers, mid, sliced["volume"] / sliced["layer_height"])  # mm³ per mm of height
        fill = np.clip(plastic / np.maximum(areas[src], 1e-9), 0.05, 1.0)
        t = per_mm * dz
    dzm = dz * 1e-3
    cap = mat["density_g_cm3"] * 1000 * fill * a * dzm * C_P  # J/K
    cond = K_PLASTIC * np.minimum(fill[:-1], fill[1:]) * np.minimum(a[:-1], a[1:]) / dzm  # between cell i and i+1
    h = H_STILL + H_FAN * mat["fan_pct"] / 100
    exposed = np.append(np.maximum(a[:-1] - a[1:], 0.0), a[-1])  # top faces not covered by the next layer
    side = h * p * dzm
    g_bed = K_PLASTIC * fill[0] * a[0] / (dzm / 2)
    t_amb, t_bed, t_noz = AMBIENT_C[profile.enclosure], float(mat["bed_temp_c"]), float(mat["nozzle_temp_c"])

    temp = np.full(n, t_amb)
    k = max(1, int(round(DEPTH_MM / dz)))
    soft = np.full(n, t_amb)
    for j in range(n):
        temp[j] = t_noz
        lo = max(0, j - WINDOW)
        dt = t[j] / SUBSTEPS
        g_noz = H_NOZZLE * min(a[j], NOZZLE_REACH_MM2 * 1e-6)
        for _ in range(SUBSTEPS):
            temp[lo:j + 1] = _step(temp[lo:j + 1], cap[lo:j + 1], cond[lo:j], side[lo:j + 1], h * exposed[lo:j], h * a[j],
                                   g_noz, g_bed if lo == 0 else 0.0, dt, t_amb, t_bed, t_noz)
        soft[max(j - k, 0)] = temp[max(j - k, 0)]
    if n > k:
        soft[n - k:] = soft[n - k - 1]  # nothing is printed on the last layers: they cool down instead
    out = np.full(len(zs), t_amb)
    np.maximum.at(out, src, soft)  # hottest cell within each sampled layer
    return out


def layer_seconds(area: np.ndarray, perimeter: np.ndarray, profile: Profile, min_speed: float = MIN_SPEED,
                  min_layer_time: float | None = None) -> np.ndarray:
    """Time the nozzle spends on one layer: the extrusion path at printing speed, slowed to the
    material's minimum layer time — but a tiny layer can't be slowed below MIN_SPEED, so a thin pin
    gets far less time to cool than the minimum layer time promises."""
    plan = goal_plan(profile)
    lw = profile.line_width
    wall = np.minimum(perimeter * plan["walls"] * lw, area)
    path = perimeter * plan["walls"] + (area - wall) * plan["infill"] / 100 / lw
    fast = path / profile.speeds["outer_wall"]
    target = profile.material["min_layer_time_s"] if min_layer_time is None else min_layer_time
    return np.maximum(fast, np.minimum(target, path / min_speed))


def _step(temp, cap, cond, side, exposed, top_air, g_noz, g_bed, dt, t_amb, t_bed, t_noz) -> np.ndarray:
    """One implicit Euler step of the active column (tridiagonal solve, Thomas algorithm)."""
    m = len(temp)
    air = side.copy()
    air[:-1] += exposed
    air[-1] += top_air
    diag = cap / dt + air
    diag[:-1] += cond
    diag[1:] += cond
    rhs = cap / dt * temp + air * t_amb
    diag[0] += g_bed
    rhs[0] += g_bed * t_bed
    diag[-1] += g_noz
    rhs[-1] += g_noz * t_noz
    # Forward sweep / back substitution; off-diagonals are -cond.
    c = np.zeros(m)
    d = np.zeros(m)
    c[0] = -cond[0] / diag[0] if m > 1 else 0.0
    d[0] = rhs[0] / diag[0]
    for i in range(1, m):
        den = diag[i] + cond[i - 1] * c[i - 1]
        c[i] = -cond[i] / den if i < m - 1 else 0.0
        d[i] = (rhs[i] + cond[i - 1] * d[i - 1]) / den
    out = np.empty(m)
    out[-1] = d[-1]
    for i in range(m - 2, -1, -1):
        out[i] = d[i] - c[i] * out[i + 1]
    return out
