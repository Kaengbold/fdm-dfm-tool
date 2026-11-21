"""How hot every spot of the part is while it prints, from the sliced G-code (T-473, tier A).

The simple level (heat.py) treats each layer as one lump, and the line-by-line check
(gcode.spot_heat) lets each spot cool on its own. Here the plastic forms a heat network: each layer's
lines are drawn into small cells, every cell becomes a node the moment the nozzle puts it down
(at nozzle temperature, less at high flow), and heat flows sideways, down through the contact
between layers and into the bed, and out to the air: by convection that follows the part fan
(its speed, spin-up and distance below the nozzle), by radiation, and back in where the nozzle
passes over again. Heat capacity rises above the glass transition; semi-crystalline plastics
release their latent heat as they crystallise.

Solver: implicit (backward Euler), stepping with the print: steps are shorter when layers are short.
Only the top layers take part in full detail; older layers merge into one lump per column, which
keeps their heat and their contact to the bed, so tall parts cost no more per step than short ones.

For each joint between layers (a new node on top of the one below) it records the joint
temperature over time, T_int(t), and how long the joint stays above the glass transition: what
bonding between layers depends on (T-474). Constants and how sure they are: data/thermal.yaml.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from . import progress
from .gcode import LAYER_TIME, PART_KINDS, SOLID, SUPPORT, WALL, Toolpath, _role
from .kinematics import Timing, nozzle_at
from .profile import Profile, load_data

SIGMA = 5.670374e-8  # W/m²K⁴
DELAYS_S = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0)  # T_int is recorded this long after each joint forms
TIERS = {
    # cell size (mm), layers in full detail, step length (s): min, max and per layer
    "quick": {"cell_mm": 2.0, "layers": 25, "dt_min": 0.25, "dt_max": 5.0, "steps_per_layer": 20},
    "fine": {"cell_mm": 1.0, "layers": 60, "dt_min": 0.1, "dt_max": 2.0, "steps_per_layer": 40},
}
NOZZLE_SAMPLE_S = 0.05  # where the nozzle is, sampled this often for reheating
COOL_DOWN_S = DELAYS_S[-1]  # keep going after the last line, so the last joints get their T_int
MIN_FILL = 0.05  # a cell's plastic share never counts below this (sparse infill still conducts a little)
SLIVER_FILL = 0.25  # a cell with less plastic than this joins its fullest neighbour (a wall clipping a corner)


@dataclass
class Heat:
    """Result per node (a cell of one layer): where it is, when it was printed, and its joint to the
    node below. Coordinates are the G-code's (printer) frame."""

    layer: np.ndarray  # (M,) layer index
    xy: np.ndarray  # (M, 2) cell centre, mm
    z: np.ndarray  # (M,) mm, middle of the layer
    volume: np.ndarray  # (M,) mm³ of plastic
    born_s: np.ndarray  # (M,) when it was printed (s, print timing)
    birth_c: np.ndarray  # (M,) its temperature then
    cover_c: np.ndarray  # (M,) its temperature when the node above was printed (NaN: never covered)
    cover_s: np.ndarray  # (M,) seconds until then
    joint_c: np.ndarray  # (M, len(DELAYS_S)) joint to the node below, T_int at each delay (NaN: no node below)
    above_tg_s: np.ndarray  # (M,) seconds the joint stayed above the glass transition
    bond: np.ndarray  # (M,) how far the joint healed, 0..1 (welding integral, Wool: (∫dt / reptation time)^¼)
    overhang: np.ndarray  # (M,) share of its plastic printed as bridge or overhang
    cooled: np.ndarray  # (M,) share of its plastic the slicer slowed down for layer cooling
    shell: np.ndarray  # (M,) share of walls and solid layers (what holds layers together; sparse infill barely does)
    island: np.ndarray  # (M,) connected area of its layer
    column: np.ndarray  # (M,) islands joined from layer to layer
    cell_mm: float
    tier: str
    material: str
    tg_c: float
    seconds: float  # computing time
    steps: int


def _solve(A, b: np.ndarray, x: np.ndarray, tol: float = 1e-7, max_iter: int = 200) -> np.ndarray:
    """Conjugate gradients with a diagonal preconditioner, starting from the last step's temperatures.
    The matrix is symmetric and strongly diagonal (heat capacity / step), so a few iterations do what
    a direct solve does at a fraction of the cost."""
    inv = 1.0 / A.diagonal()
    r = b - A @ x
    z = inv * r
    p = z.copy()
    rz = r @ z
    stop = tol * np.linalg.norm(b)
    for _ in range(max_iter):
        if np.linalg.norm(r) <= stop:
            break
        Ap = A @ p
        alpha = rz / (p @ Ap)
        x = x + alpha * p
        r = r - alpha * Ap
        z = inv * r
        rz, rz_old = r @ z, rz
        p = z + (rz / rz_old) * p
    return x


def _value(entry) -> float:
    return float(entry["value"] if isinstance(entry, dict) else entry)


def _interp(points: dict, x):
    keys = np.array(sorted(float(k) for k in points))
    return np.interp(x, keys, [float(points[k if k in points else int(k)]) for k in keys])


def constants(profile: Profile, settings: dict | None = None) -> dict:
    """SI constants for this material and printer from data/thermal.yaml (and the slicer's
    temperatures when the G-code has them)."""
    data = load_data("thermal")
    mid = profile.material_id.split("_")[0]
    m = data["materials"].get(mid, data["materials"]["pla"])
    pr = data["process"]
    s = settings or {}

    def setting(*keys, default):
        for k in keys:
            try:
                v = float(str(s.get(k, "")).split(",")[0])
                if v > 0:
                    return v
            except ValueError:
                pass
        return default

    bed = setting("hot_plate_temp", "bed_temperature", "textured_plate_temp", default=float(profile.material["bed_temp_c"]))
    enclosure = profile.enclosure
    air = pr["chamber_air_c"]
    chamber = {"none": air["open"], "passive": air["closed_bed_100"] if bed >= 90 else air["closed_bed_60"],
               "heated": air["heated_chamber"]}[enclosure]
    tc = m.get("tc_c", {}).get("value")
    return {
        "material": mid, "rho": _value(m["density"]), "cp_glassy": _value(m["cp_glassy"]), "cp_melt": _value(m["cp_melt"]),
        "k": _value(m["k"]), "emissivity": _value(m["emissivity"]), "tg": _value(m["tg_c"]),
        "latent": _value(m["latent_heat"]), "tc": float(tc) if tc is not None else None,
        "nozzle_c": setting("nozzle_temperature", "temperature", default=float(profile.material["nozzle_temp_c"])),
        "bed_c": bed, "chamber_c": _value(chamber), "air_bed_mm": _value(pr["air_bed_layer_mm"]),
        "h_still": _value(pr["h_still_air"]), "h_fan": pr["h_fan"]["points"], "fan_falloff_mm": _value(pr["h_fan"]["falloff_mm"]),
        "fan_spin_up_s": _value(pr["fan_spin_up_s"]), "contact": _value(pr["bead_contact_conductance"]),
        "z_ratio": _value(pr["z_conductivity_ratio"]),
        "bed_contact": float(pr["bed_contact_conductance"]["smooth_pei"]),
        "nozzle_h": float(pr["nozzle_reheat"]["h_equiv"]), "melt_drop": pr["melt_temp_drop_c"]["points"],
        "wlf_c1": float(m["wlf"]["c1"]), "wlf_c2": float(m["wlf"]["c2_k"]), "wlf_ref": float(m["wlf"]["t_ref_c"]),
        "reptation_s": _value(m["reptation_s"]), "reptation_ref": float(m["reptation_s"]["t_ref_c"]),
        "no_sag_c": _value(m["no_sag_c"]),
    }


def _heat_capacity(T: np.ndarray, c: dict) -> np.ndarray:
    """J/(kg·K): glassy below Tg, rising to the melt value 20 K above; latent heat spread as a peak
    around the crystallisation temperature (released while cooling)."""
    ramp = np.clip((T - c["tg"]) / 20, 0, 1)
    cp = c["cp_glassy"] + (c["cp_melt"] - c["cp_glassy"]) * ramp
    if c["latent"] > 0 and c["tc"] is not None:
        width = 10.0
        cp = cp + c["latent"] * np.exp(-0.5 * ((T - c["tc"]) / width) ** 2) / (width * np.sqrt(2 * np.pi))
    return cp


def _reptation_s(T: np.ndarray, c: dict) -> np.ndarray:
    """How long the plastic's chains take to cross a joint at temperature T (s): the reptation time
    at its reference temperature, shifted by WLF. Below the glass transition they don't move at all."""
    def log_shift(t):
        dt = t - c["wlf_ref"]
        return -c["wlf_c1"] * dt / (c["wlf_c2"] + dt)

    above = T > c["wlf_ref"]
    shifted = log_shift(np.where(above, T, c["wlf_ref"] + 1)) - log_shift(c["reptation_ref"])
    return np.where(above, c["reptation_s"] * 10.0 ** np.minimum(shifted, 30), np.inf)


def _deposits(tp: Toolpath, tm: Timing, cell: float, c: dict) -> dict:
    """Plastic of the part and its supports drawn onto the cells of each layer: one node per filled
    cell, with its plastic, when it was printed and how hot it went down."""
    which = np.flatnonzero(np.isin(tp.kind, (*PART_KINDS, SUPPORT)))
    a, b = tp.p0[which].astype(float), tp.p1[which].astype(float)
    length = np.linalg.norm(b - a, axis=1)
    n = np.maximum(np.ceil(length / (cell / 2)).astype(int), 1)
    seg = np.repeat(np.arange(len(which)), n)
    k = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
    f = (k + 0.5) / n[seg]
    line = which[seg]
    pts = a[seg] + (b - a)[seg] * f[:, None]
    vol = tp.volume[line] / n[seg]
    t = tm.t0[line] + (tm.t1[line] - tm.t0[line]) * f
    duration = np.maximum(tm.t1[which] - tm.t0[which], 1e-6)
    flow = tp.volume[which] / duration  # mm³/s while printing the line
    melt = (c["nozzle_c"] - _interp(c["melt_drop"], flow))[seg]

    lo = np.floor(pts[:, :2].min(axis=0) / cell) - 1
    ij = (np.floor(pts[:, :2] / cell) - lo).astype(np.int64)
    nx, ny = (ij.max(axis=0) + 2).tolist()
    layer = tp.layer[line].astype(np.int64)
    key = (layer * nx + ij[:, 0]) * ny + ij[:, 1]
    keys, node = np.unique(key, return_inverse=True)
    # Slivers (a wall clipping a cell's corner) would cool on their own far too fast: join each to its
    # fullest neighbour in the layer, so a node is always a real lump of plastic.
    volume = np.bincount(node, vol)
    lh_node = np.bincount(node, vol * tp.layer_height[line]) / volume
    sliver = volume / (cell * cell * lh_node) < SLIVER_FILL
    near = _neighbours(keys, (ny, -ny, 1, -1))
    near_vol = np.where(near >= 0, volume[np.maximum(near, 0)], -1.0)
    best = near[np.arange(len(keys)), near_vol.argmax(axis=1)]
    move = sliver & (best >= 0) & (near_vol.max(axis=1) > volume)
    target = np.where(move, best, np.arange(len(keys)))
    keys, node = np.unique(keys[target][node], return_inverse=True)
    volume = np.bincount(node, vol)
    born = np.bincount(node, vol * t) / volume
    birth = np.bincount(node, vol * melt) / volume
    lay = keys // (nx * ny)
    rest = keys % (nx * ny)
    cx, cy = rest // ny, rest % ny
    lh = np.bincount(node, vol * tp.layer_height[line]) / volume
    # The part's skin: outer walls face the air, whatever the cell size (inner lines touch other plastic).
    outer = np.array([_role(f) in ("outer", "overhang") for f in tp.features])[tp.feature[line]]
    skin = np.bincount(node, outer * length[seg] / n[seg], minlength=len(keys))
    spans = np.array([_role(f) in ("bridge", "internal_bridge", "overhang") for f in tp.features])[tp.feature[line]]
    overhang = np.bincount(node, vol * spans, minlength=len(keys)) / volume
    cooled = np.bincount(node, vol * (tp.cause[line] == LAYER_TIME), minlength=len(keys)) / volume
    shell = np.bincount(node, vol * np.isin(tp.kind[line], (WALL, SOLID)), minlength=len(keys)) / volume
    return {"keys": keys, "nx": nx, "ny": ny, "layer": lay, "ix": cx, "iy": cy, "lo": lo, "volume": volume,
            "born": born, "birth": birth, "z": tp.layer_z[lay] - lh / 2, "lh": lh, "skin_mm": skin,
            "overhang": overhang, "cooled": cooled, "shell": shell}


def _neighbours(keys: np.ndarray, offsets) -> np.ndarray:
    """For each key, the node at key + offset (or -1), for each offset."""
    out = np.full((len(keys), len(offsets)), -1, np.int64)
    for j, off in enumerate(offsets):
        want = keys + off
        at = np.searchsorted(keys, want)
        ok = (at < len(keys)) & (keys[np.minimum(at, len(keys) - 1)] == want)
        out[ok, j] = at[ok]
    return out


def _islands(n: int, layer: np.ndarray, side: np.ndarray, up: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Connected areas of each layer (sideways neighbours) and columns (islands touching from layer to layer)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    a = np.repeat(np.arange(n), side.shape[1])[side.ravel() >= 0]
    b = side.ravel()[side.ravel() >= 0]
    _, island = connected_components(coo_matrix((np.ones(len(a)), (a, b)), shape=(n, n)), directed=False)
    has = up >= 0
    m = int(island.max()) + 1
    _, column_of = connected_components(coo_matrix((np.ones(has.sum()), (island[has], island[up[has]])), shape=(m, m)),
                                        directed=False)
    return island, column_of[island]


def simulate(tp: Toolpath, tm: Timing, profile: Profile, tier: str = "quick", overrides: dict | None = None) -> Heat:
    """Run the heat network over the whole print. ``overrides`` replaces constants (tests, tuning)."""
    from scipy.sparse import coo_matrix

    started = time.time()
    cfg = TIERS[tier]
    c = {**constants(profile, tp.settings), **(overrides or {})}
    cell = cfg["cell_mm"]
    d = _deposits(tp, tm, cell, c)
    n = len(d["keys"])
    nx, ny = d["nx"], d["ny"]
    layer, z, lh = d["layer"], d["z"], d["lh"]
    h, lh_m = cell * 1e-3, lh * 1e-3
    fill = np.clip(d["volume"] / (cell * cell * lh), MIN_FILL, 1.0)
    mass = c["rho"] * d["volume"] * 1e-9  # kg

    side = _neighbours(d["keys"], (ny, -ny, 1, -1))  # same layer: x+, x-, y+, y-
    up = _neighbours(d["keys"], (nx * ny,))[:, 0]
    down = _neighbours(d["keys"], (-nx * ny,))[:, 0]
    # Diagonal lines (45° infill) can join two cells only at a corner: link those directly, but only
    # where the two cells beside the corner are empty (else the plastic already conducts through them).
    corner = _neighbours(d["keys"], (ny + 1, ny - 1))  # x+y+, x+y-
    beside = _neighbours(d["keys"], (ny, 1, -1))  # x+, y+, y-
    corner[:, 0] = np.where((beside[:, 0] < 0) & (beside[:, 1] < 0), corner[:, 0], -1)
    corner[:, 1] = np.where((beside[:, 0] < 0) & (beside[:, 2] < 0), corner[:, 1], -1)
    island, column = _islands(n, layer, np.column_stack([side, corner]), up)

    # Conductances (W/K). Sideways through the shared face; down through the layers and their contact.
    ea, eb, eg = [], [], []
    for j in (0, 2):  # x+ and y+: each pair once
        has = side[:, j] >= 0
        i, o = np.flatnonzero(has), side[has, j]
        ea.append(i), eb.append(o)
        eg.append(c["k"] * (np.minimum(lh_m[i], lh_m[o]) * h) * np.minimum(fill[i], fill[o]) / h)
    for j in (0, 1):  # corner links: a bead about as wide as the cells' plastic, √2 cells long
        has = corner[:, j] >= 0
        i, o = np.flatnonzero(has), corner[has, j]
        ea.append(i), eb.append(o)
        eg.append(c["k"] * np.minimum(lh_m[i], lh_m[o]) * np.minimum(fill[i], fill[o]) / np.sqrt(2))
    has = up >= 0
    i, o = np.flatnonzero(has), up[has]
    area = h * h * np.minimum(fill[i], fill[o])
    bulk = c["k"] * c["z_ratio"] * area / ((lh_m[i] + lh_m[o]) / 2)
    ea.append(i), eb.append(o), eg.append(1 / (1 / bulk + 1 / (c["contact"] * area)))
    ea, eb, eg = np.concatenate(ea), np.concatenate(eb), np.concatenate(eg)
    g_bed = np.where(layer == 0, c["bed_contact"] * h * h * fill, 0.0)
    top_area = d["volume"] * 1e-9 / lh_m  # the plastic's own top (fill may pass 1 where lines sit on a cell edge)
    side_area = d["skin_mm"] * 1e-3 * lh_m  # the outer walls' faces

    # Nozzle reheating: how long the nozzle hovers over each cell of the layer below the one it prints.
    samples = np.arange(tm.moves["t0"][0], tm.total_s, NOZZLE_SAMPLE_S)
    at = nozzle_at(tp, tm, samples)
    noz_layer = np.searchsorted(tp.layer_z, at[:, 2] - 1e-3) - 1
    nij = (np.floor(at[:, :2] / cell) - d["lo"]).astype(np.int64)
    inside = (nij >= 0).all(axis=1) & (nij[:, 0] < nx) & (nij[:, 1] < ny) & (noz_layer >= 0)
    want = (noz_layer * nx + nij[:, 0]) * ny + nij[:, 1]
    hit = np.searchsorted(d["keys"], want)
    hit = np.where(inside & (hit < n) & (d["keys"][np.minimum(hit, n - 1)] == want), hit, -1)

    # Part fan over time, by print timing (fan commands sit between lines), with the blower's spin-up.
    part = [k for k, name in enumerate(tp.fan_names) if name == "part"]
    fan_mask = np.isin(tp.fans["fan"], part)
    fan_line = tp.fans["line"][fan_mask]
    fan_t = np.where(fan_line < len(tm.t0), tm.t0[np.minimum(fan_line, len(tm.t0) - 1)], tm.total_s)
    fan_v = tp.fans["value"][fan_mask].astype(float)

    # Steps: births quantised to step ends; steps per layer from each layer's print time.
    order = np.argsort(d["born"], kind="stable")
    n_layers = int(layer.max()) + 1
    first = np.full(n_layers, np.inf)
    np.minimum.at(first, layer, d["born"])
    first = np.minimum.accumulate(first[::-1])[::-1]
    layer_time = np.diff(np.append(first, d["born"].max() + COOL_DOWN_S))
    t_begin, t_stop = float(d["born"].min()) - 1e-9, float(d["born"].max()) + COOL_DOWN_S

    T = np.zeros(n + int(column.max()) + 1)  # nodes, then one lump per column for merged layers
    lump = n + column
    C_lump = np.zeros(len(T) - n)
    exposed_lump = np.zeros(len(T) - n)
    bed_lump = np.zeros(len(T) - n)
    rep = np.arange(len(T))  # each node's representative: itself, or its column's lump once merged
    born = np.zeros(len(T), bool)  # lumps count as born
    born[n:] = True
    merged_upto = -1
    cover_c = np.full(n, np.nan)
    cover_s = np.full(n, np.nan)
    joint = np.full((n, len(DELAYS_S)), np.nan)
    above_tg = np.zeros(n)
    weld = np.zeros(n)  # ∫ dt / reptation time at the joint
    # Nodes are sorted by layer (their keys start with it), so a layer is one slice; edges by their upper layer.
    lay_start = np.searchsorted(layer, np.arange(n_layers + 1))
    e_order = np.argsort(layer[eb], kind="stable")
    ea, eb, eg = ea[e_order], eb[e_order], eg[e_order]
    e_start = np.searchsorted(layer[eb], np.arange(n_layers + 1))
    local = np.full(len(T), -1)
    window = None
    nb = 0
    t, steps, fan_eff, cur = t_begin, 0, 0.0, 0
    air_of = lambda zz: c["chamber_c"] + (c["bed_c"] - c["chamber_c"]) * np.exp(-zz / c["air_bed_mm"])
    eps_sigma = c["emissivity"] * SIGMA
    report_at = 0.0

    while t < t_stop:
        dt = float(np.clip(layer_time[cur] / cfg["steps_per_layer"], cfg["dt_min"], cfg["dt_max"]))
        t_next = min(t + dt, t_stop)
        dt = t_next - t
        # New plastic: nodes printed by the end of this step.
        new_end = np.searchsorted(d["born"][order], t_next, side="right")
        new = order[nb:new_end]
        nb = new_end
        if len(new):
            below = down[new]
            ok = below >= 0
            cover_c[below[ok]] = T[rep[below[ok]]]
            cover_s[below[ok]] = d["born"][new[ok]] - d["born"][below[ok]]
            T[new] = d["birth"][new]
            # The moment the bead lands, before the cells even out: two bodies in contact meet at the mean of
            # their temperatures, until the heat has crossed half a layer (~0.1 s for PLA). Most healing happens here.
            joined = new[ok]
            contact_c = (d["birth"][joined] + T[rep[below[ok]]]) / 2
            t_contact = (lh_m[joined] / 2) ** 2 * c["rho"] * c["cp_melt"] / c["k"]
            weld[joined] += t_contact / _reptation_s(contact_c, c)
            born[new] = True
            cur = max(cur, int(layer[new].max()))
        # Merge old layers into their column's lump (heat kept, contact to the bed kept).
        while merged_upto < cur - cfg["layers"]:
            merged_upto += 1
            sink = np.arange(lay_start[merged_upto], lay_start[merged_upto + 1])
            sink = sink[born[sink]]
            if len(sink):
                Cn = mass[sink] * _heat_capacity(T[sink], c)
                L = lump[sink] - n
                energy = C_lump * T[n:]
                np.add.at(energy, L, Cn * T[sink])
                np.add.at(C_lump, L, Cn)
                T[n:] = np.where(C_lump > 0, energy / np.maximum(C_lump, 1e-12), T[n:])
                np.add.at(exposed_lump, L, top_area[sink] * (up[sink] < 0) + side_area[sink])
                np.add.at(bed_lump, L, g_bed[sink])
                rep[sink] = lump[sink]
            window = None
        if window is None or window[0] != cur:
            # In full detail: the unmerged layers up to the one printed next, and the lumps below them.
            if window is not None:
                local[window[1]] = -1
            nodes = np.arange(lay_start[merged_upto + 1], lay_start[min(cur + 2, n_layers)])
            lumps = np.flatnonzero(C_lump > 0) + n
            idx = np.concatenate([nodes, lumps])
            local[idx] = np.arange(len(idx))
            sl = slice(e_start[merged_upto + 1], e_start[min(cur + 2, n_layers)])
            wa, wb, wg = ea[sl], eb[sl], eg[sl]
            la, lb = local[rep[wa]], local[rep[wb]]
            keep = (la >= 0) & (lb >= 0) & (la != lb)
            wa, wb, wg, la, lb = wa[keep], wb[keep], wg[keep], la[keep], lb[keep]
            nl, m = len(nodes), len(idx)
            j_all = nodes[down[nodes] >= 0]  # nodes with a joint below
            window = (cur, idx)

        # Fan with spin-up (first order), at this step.
        k = np.searchsorted(fan_t, t_next, side="right") - 1
        fan_set = fan_v[k] if k >= 0 else 0.0
        fan_eff += (fan_set - fan_eff) * (1 - np.exp(-dt / c["fan_spin_up_s"]))
        z_print = float(tp.layer_z[cur])

        Tn, Tl = T[nodes], T[lumps]
        # Losses to the air: the top until the next layer covers it, and the outer walls.
        covered = (up[nodes] >= 0) & born[np.maximum(up[nodes], 0)]
        area_air = top_area[nodes] * ~covered + side_area[nodes]
        h_fan = _interp(c["h_fan"], fan_eff * 100) * np.exp(-np.maximum(z_print - z[nodes], 0) / c["fan_falloff_mm"])
        air = air_of(z[nodes])
        h_rad = eps_sigma * ((Tn + 273.15) ** 2 + (air + 273.15) ** 2) * (Tn + air + 546.3)
        air_l = air_of(0.0)
        h_rad_l = eps_sigma * ((Tl + 273.15) ** 2 + (air_l + 273.15) ** 2) * (Tl + air_l + 546.3)
        g_amb = np.concatenate([area_air * (c["h_still"] + h_fan + h_rad), exposed_lump[lumps - n] * (c["h_still"] + h_rad_l)])
        t_amb = np.concatenate([air, np.full(len(lumps), air_l)])
        # Nozzle passing over: share of the step it hovered above each cell.
        s0, s1 = np.searchsorted(samples, [t, t_next])
        hov = hit[s0:s1]
        hov = local[rep[hov[hov >= 0]]]
        dwell = np.bincount(hov[(hov >= 0) & (hov < nl)], minlength=m) * (NOZZLE_SAMPLE_S / dt)
        g_noz = c["nozzle_h"] * np.concatenate([top_area[nodes], np.zeros(len(lumps))]) * dwell
        g_b = np.concatenate([g_bed[nodes], bed_lump[lumps - n]])
        cap = np.concatenate([mass[nodes] * _heat_capacity(Tn, c), C_lump[lumps - n]]) / dt

        live = born[idx]  # not printed yet: kept out of this step
        g = wg * (born[wa] & born[wb])
        diag = np.where(live, cap + g_amb + g_b + g_noz, 1.0)
        rhs = np.where(live, cap * T[idx] + g_amb * t_amb + g_b * c["bed_c"] + g_noz * c["nozzle_c"], T[idx])
        diag += np.bincount(la, g, minlength=m) + np.bincount(lb, g, minlength=m)
        A = coo_matrix((np.concatenate([diag, -g, -g]), (np.concatenate([np.arange(m), la, lb]), np.concatenate([np.arange(m), lb, la]))),
                       shape=(m, m)).tocsr()
        T_old = T[idx]
        T[idx] = _solve(A, rhs, T_old)

        # Joints: T_int = mean of the two nodes (same plastic on both sides of the contact).
        j = j_all[born[j_all]]
        if len(j):
            lo_t = (T_old[local[j]] + T_old[local[rep[down[j]]]]) / 2
            hi_t = (T[j] + T[rep[down[j]]]) / 2
            age0, age1 = t - d["born"][j], t_next - d["born"][j]
            for q, delay in enumerate(DELAYS_S):
                hit_d = (age0 < delay) & (age1 >= delay)
                if hit_d.any():
                    w = (delay - np.maximum(age0[hit_d], 0)) / np.maximum(age1[hit_d] - np.maximum(age0[hit_d], 0), 1e-12)
                    joint[j[hit_d], q] = lo_t[hit_d] + (hi_t[hit_d] - lo_t[hit_d]) * w
            span = age1 - np.maximum(age0, 0)
            frac = np.where(hi_t > lo_t, (hi_t - c["tg"]) / np.maximum(hi_t - lo_t, 1e-9), (lo_t - c["tg"]) / np.maximum(lo_t - hi_t, 1e-9))
            above = np.where((lo_t > c["tg"]) & (hi_t > c["tg"]), 1.0,
                             np.where((lo_t > c["tg"]) | (hi_t > c["tg"]), np.clip(frac, 0, 1), 0.0))
            above_tg[j] += span * above
            weld[j] += span / _reptation_s((lo_t + hi_t) / 2, c)
        t = t_next
        steps += 1
        if (t - t_begin) / (t_stop - t_begin) >= report_at:
            progress.report(report_at, "Following the heat through the print")
            report_at += 0.02

    xy = (np.column_stack([d["ix"], d["iy"]]) + d["lo"] + 0.5) * cell
    has_below = down >= 0
    joint[~has_below] = np.nan
    above_tg[~has_below] = np.nan
    bond = np.where(has_below, np.minimum(weld, 1.0) ** 0.25, np.nan)
    return Heat(layer=layer, xy=xy, z=z, volume=d["volume"], born_s=d["born"], birth_c=d["birth"], cover_c=cover_c,
                cover_s=cover_s, joint_c=joint, above_tg_s=above_tg, bond=bond,
                overhang=d["overhang"], cooled=d["cooled"], shell=d["shell"], island=island, column=column, cell_mm=cell, tier=tier,
                material=c["material"], tg_c=c["tg"], seconds=time.time() - started, steps=steps)


INTENTS = ("cosmetic", "dimension_critical", "load_bearing")


def window(h: Heat, profile: Profile, intents: dict | None = None) -> dict:
    """The temperature window of every node, and which parts of the print leave it:

    - too hot: the node is above its sag limit when the next layer lands (stricter on bridges and
      overhangs, which nothing holds up);
    - too cold: its joint to the layer below healed too little (or was above Tg too briefly);
    - needless slowdown: too cold where the slicer slowed the layer down for cooling.

    The profile's goal and the user's design intents (``intents``: kind -> node mask) move the limits.
    Each column of the part is judged in bands of a few layers by the band's median, so a single cell
    doesn't raise an alarm; every node of a failing band is flagged."""
    data = load_data("thermal")
    win = data["window"]
    goal = win["goals"][profile.goal]
    mat = data["materials"].get(h.material, data["materials"]["pla"])
    supported = float(profile.material["softening_c"]) + float(load_data("thresholds")["cooling"]["hot_margin_c"])
    spans = h.overhang >= win["overhang_share"]
    limit = np.where(spans, _value(mat["no_sag_c"]), supported) + goal["sag_shift_c"]
    min_bond = np.full(len(h.layer), float(goal["min_bond"]))
    for kind, mask in (intents or {}).items():
        shift = win["intents"][kind]
        limit = limit + np.where(mask, shift["sag_shift_c"], 0.0)
        min_bond = min_bond + np.where(mask, shift["min_bond_shift"], 0.0)

    band = h.column.astype(np.int64) * (int(h.layer.max()) + 1) + h.layer // int(win["band_layers"])
    keys, inv = np.unique(band, return_inverse=True)

    def band_median(values: np.ndarray) -> np.ndarray:
        """Each node gets its band's median (NaN values left out; a band of only NaN gives NaN)."""
        out = np.full(len(keys), np.nan)
        ok = np.isfinite(values)
        order = np.lexsort((values[ok], inv[ok]))
        b, v = inv[ok][order], values[ok][order]
        start = np.searchsorted(b, np.arange(len(keys)))
        count = np.bincount(b, minlength=len(keys))
        has = count > 0
        lo = v[np.minimum(start + (count - 1) // 2, len(v) - 1)]
        hi = v[np.minimum(start + count // 2, len(v) - 1)]
        out[has] = ((lo + hi) / 2)[has]
        return out[inv]

    hot_margin = band_median(h.cover_c - limit)
    bond_short = band_median(h.bond - min_bond)
    brief = band_median(h.above_tg_s) < float(win["min_above_tg_s"])
    too_hot = np.nan_to_num(hot_margin, nan=-1.0) > 0
    too_cold = (np.nan_to_num(bond_short, nan=1.0) < 0) | brief
    needless = too_cold & (band_median(h.cooled) >= 0.5)
    return {"limit_c": limit, "min_bond": min_bond, "too_hot": too_hot, "too_cold": too_cold, "needless": needless}
