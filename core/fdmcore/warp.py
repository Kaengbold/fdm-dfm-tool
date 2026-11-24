"""Warping while printing: how each new layer's shrinking pulls on the part (T-162).

Plastic shrinks as it cools below the point where it sets (``softening_c``). Layers near the
heated bed stay warm and hardly shrink while printing; layers higher up cool to the air in the
printer and shrink fully (``shrinkage_pct``). Stuck on top of the warm bottom they can't, so the
part wants to curl: its ends and corners pull up on the bed. This is the "inherent strain" method
of print simulation: the analysis cubes are switched on layer by layer, each new layer gets its
shrinkage as a strain, and the part so far is solved with the bottom held by the bed.

The lift risk is how far the base would curl if the bed let go at the end of the print. That
grows with the square of the length and depends on the shape (a tall wall or a rib stiffens the
base). It is rated against a reference simulated on the same grid: a 10 × 3 mm bar as long as the
longest one that stays flat for this material and enclosure (thresholds.yaml, or the user's own
warping calibration print). ``lift_ratio`` is given like a length ratio (square root of the curl
ratio), so 1 means "like that bar" and the warping check's thresholds apply.

The bed's warmth fades with height like heat along a cooling fin: over sqrt(k · A/P / h), with
the cross-section's area A and perimeter P and the air's heat transfer h (more with the part fan).
So a thin wall cools a millimetre or two above the bed and a thick block stays warm higher up.

What stays behind after cooling is residual stress: each element's shrink minus what the part
let it do. ``residual_stress`` gives it per element (MPa, von Mises), the "stress left inside" view.

Linear and rough: real plastic creeps while warm (so real residual stress is lower), the fin
ignores the nozzle's heat, and the millimetres of the shape are exaggerated. Use it for where and how much relative to the
reference, not for exact numbers.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import product
from math import exp, isfinite, log, tanh

import numpy as np

from .fea import Model, b_matrix, build_model, cohesive_bed, element_stiffness, prolongation, solve, stiffness
from .heat import H_FAN, H_STILL, K_PLASTIC
from .mesh import Mesh
from .orientation import rotation_to_z
from .profile import WARPING_TEST_MM, Profile, load_data
from .progress import report
from .strength import Shells, element_scale, material_for, von_mises
from .voxel import VoxelGrid, voxelize

MAX_STEPS = 16  # solves while printing: voxel layers are grouped to at most this many
REF_BAR = (10.0, 3.0)  # width and height of the warping calibration bars (api.calibration_part)
RELEASE_SPRING = 1e-6  # × E·h: keeps the part from floating away once it is off the bed
THIN_WALL_MM = 1.0  # A/P of a thin wall: the fade used when no cross-sections are known
ROOM_C = 25.0


def warp_limit(profile: Profile) -> float | None:
    """Longest bar that stays flat (mm) for this material and enclosure."""
    return profile.warping_length(load_data("thresholds")["warping"]["risk_length_mm"])


def simulate(mesh: Mesh, up, profile: Profile, shells: Shells = Shells(), target_voxels: int = 6000, h: float | None = None,
             warm: tuple | None = None, *, adhesion: bool = False, grip_trial: bool = False) -> dict:
    """Print ``mesh`` (part coordinates) with build direction ``up``. Returns the model (voxelised in the
    print frame: ``rotation`` turns part coordinates into it, so a tilted part sits flat on the grid's
    bottom layer instead of touching the bed with one stair-step corner), displacement after coming off
    the bed (n_nodes, 3, print frame), the bed's pull at each bed node while printing
    (N, positive = the part pulls away from the bed) and the bed nodes. ``warm`` (heights, °C): how warm
    each height still is when more goes on top (the layer heat model with the G-code's real layer
    times); plastic still warm then shrinks less while printing. With ``adhesion``, test whether a
    bed edge peels in the final shrink state. With ``grip_trial``, compute the bar's grip threshold instead."""
    rotation = rotation_to_z(up)
    # Rounded: float noise from the turn (3.0000000001 mm) would add a whole voxel layer.
    model = build_model(voxelize(Mesh(np.round(mesh.triangles @ rotation.T, 9)), target_voxels, h))
    up = np.array([0.0, 0.0, 1.0])
    grid = model.grid
    mat = material_for(profile)
    c = mat.stiffness_print_frame()
    scale = element_scale(model, up, shells, profile)
    ke = element_stiffness(c, grid.h)
    bt = sum(b_matrix(*g, grid.h).T for g in product((-1 / np.sqrt(3), 1 / np.sqrt(3)), repeat=3)) * (grid.h / 2) ** 3
    f_unit = bt @ (c @ -np.array([1, 1, 1, 0, 0, 0.0]))  # (24,) force of a unit isotropic shrink, scale 1

    node_pos = grid.origin + np.argwhere(model.node_index >= 0) * grid.h  # node ids follow this order
    height = node_pos @ up
    bed = height <= height.min() + 1e-6
    zc = grid.centers(model.elems) @ up
    elem_level = np.round((zc - zc.min()) / grid.h).astype(int)
    shrink = shrink_while_printing(zc - zc.min() + grid.h / 2, profile, fade_depth(model, elem_level, profile))
    if warm is not None:
        shrink = np.minimum(shrink, shrink_below(np.interp(zc - zc.min() + grid.h / 2, *warm), profile))
    levels = np.unique(elem_level)
    groups = np.array_split(levels, min(MAX_STEPS, len(levels)))
    b_mid = b_matrix(0.0, 0.0, 0.0, grid.h)
    elem_id = np.full(grid.solid.shape, -1)
    elem_id[tuple(model.elems.T)] = np.arange(len(model.elems))

    u_total = np.zeros((model.n_nodes, 3))
    held_total = np.zeros((model.n_nodes, 3))  # force the bed puts on the part
    strain = np.zeros((len(model.elems), 6))  # each element's strain since it was printed
    done = np.zeros(len(model.elems), dtype=bool)
    for k, g in enumerate(groups):
        report(k / (len(groups) + 1), "Printing it layer by layer")
        active = elem_level <= g[-1]
        new = active & ~done
        solid = np.zeros_like(grid.solid)
        solid[tuple(model.elems[active].T)] = True
        sub = build_model(VoxelGrid(solid, grid.origin, grid.h))
        full_e = elem_id[tuple(sub.elems.T)]
        full_n = model.node_index[tuple(np.argwhere(sub.node_index >= 0).T)]
        forces = np.zeros(sub.n_nodes * 3)
        sub_new = new[full_e]
        np.add.at(forces, sub.edof[sub_new].ravel(), ((scale * shrink)[full_e[sub_new], None] * f_unit).ravel())
        fixed = bed[full_n]
        u, _ = solve(sub, c, scale[full_e], forces.reshape(-1, 3), fixed)
        # What the bed has to hold: internal forces of the elements minus the shrink forces, at the bed nodes.
        internal = np.zeros(sub.n_nodes * 3)
        ue = u.ravel()[sub.edof]
        np.add.at(internal, sub.edof.ravel(), (scale[full_e, None] * (ue @ ke.T)).ravel())
        held = (internal - forces).reshape(-1, 3)
        held_total[full_n[fixed]] += held[fixed]
        u_total[full_n] += u
        strain[full_e] += ue @ b_mid.T
        done = active

    # Off the bed: the bed's forces go away (as if reversed), the part springs into its final shape.
    report(len(groups) / (len(groups) + 1), "Taking it off the bed")
    n_bed = int(bed.sum())
    springs = (np.repeat(np.flatnonzero(bed), 3), np.tile(np.eye(3), (n_bed, 1)), np.full(3 * n_bed, RELEASE_SPRING * mat.e_xy * grid.h))
    u_free, _ = solve(model, c, scale, -held_total, np.zeros(model.n_nodes, dtype=bool), springs=springs)
    strain += u_free.ravel()[model.edof] @ b_mid.T
    pull = -(held_total @ up)  # the bed pulling the part down = the part pulling away from the bed
    out = {"model": model, "u": u_total + u_free, "pull": pull, "bed": bed, "up": up, "rotation": rotation,
           "strain": strain, "shrink": shrink, "scale": scale, "c": c, "material": mat}
    grip = bed_grip(profile) if adhesion else None
    if grip_trial or grip is not None:
        report(len(groups) / (len(groups) + 1), "Checking whether it lifts off the bed")
        peel = _edge_peel(model, c, scale, held_total, springs, None if grip_trial else grip["grip_n_mm"])
        if grip_trial:
            out["lift_grip_n_mm"] = peel.get("lift_grip_n_mm")
            out["peel_solved"] = not peel.get("solver_failed", False)
        else:
            out["adhesion"] = {**grip, "may_lift": False, **peel}
    return out


def bed_grip(profile: Profile, temp: float | None = None, calibrated: bool = True) -> dict | None:
    """Effective peel-work estimate from strand shear, with the measured inputs identified. ``temp``: bed
    temperature (default the material's); a matching warping-bar calibration comes first unless ``calibrated``
    is off."""
    if calibrated and (fit := _calibrated_grip(profile)) is not None:
        return fit
    settings = load_data("thresholds")["bed_adhesion"]
    mat = profile.material.get("based_on", profile.material_id)
    surface = profile.printer.get("bed_surface", "unknown")
    assumed_surface = surface == "unknown"
    if assumed_surface:
        surface = "textured_pei"
    temp = float(profile.material["bed_temp_c"] if temp is None else temp)
    pla_anchors = settings["pla_strand_shear_n_mm"].get(surface)
    if mat == "pla" and pla_anchors is not None:
        shear, source = _pla_shear(temp, surface, pla_anchors, settings["pla_hot_shear_share"])
    elif mat == "pla" and surface in settings["nominal_shear_n_mm"]["pla"]:
        # Other plates follow the measured glass curve's shape: the nominal PLA values were
        # set from the 60 °C glass measurement, so they scale it by plate.
        glass = settings["pla_strand_shear_n_mm"]["glass"]
        shear, _ = _pla_shear(temp, "glass", glass, settings["pla_hot_shear_share"])
        shear *= settings["nominal_shear_n_mm"]["pla"][surface] / glass["at_60_c"]
        source = "estimated"
    else:
        nominal = settings["nominal_shear_n_mm"].get(mat, {}).get(surface)
        if nominal is None:
            return None
        anchor_temp = load_data("presets")["materials"][mat]["bed_temp_c"]
        softening = profile.material["heat_limit_c"]
        # Spoerk saw ABS grip rise through the hottest bed tested. Hold the estimate
        # at the endpoints outside the experiment's 30–120 °C range.
        evaluated = min(120.0, max(30.0, temp)) if mat == "abs" else temp
        shear = nominal * (_shear_weight(evaluated - softening) / _shear_weight(anchor_temp - softening))
        source = "estimated" if evaluated == temp else "clamped extrapolation"
    # (peak N / strand length) / strand width gives apparent shear traction.
    # Integrate a triangular traction-slip law over an assumed effective slip
    # distance to estimate work per area, in N/mm. The slip and transfer to
    # mixed-mode opening are approximations even for measured shear inputs.
    strand_width_mm = 1.8  # Leoben's apparent strand width was 1.65–1.9 mm.
    grip = max(0.0, shear) * max(0.0, settings["effective_slip_mm"]) / (2 * strand_width_mm)
    return {"surface": surface, "assumed_surface": assumed_surface, "shear_source": source,
            "bed_temp_c": temp, "shear_n_mm": shear, "grip_n_mm": grip, "confidence": "low"}


def _pla_shear(temp: float, surface: str, anchors: dict, hot_share: float) -> tuple[float, str]:
    """Use geometric interpolation across the measured jump near PLA's glass transition."""
    measured = {t: max(0.0, anchors[f"at_{t}_c"]) for t in (50, 60, 70)}
    if temp in measured:
        return measured[temp], "measured shear anchor"
    if temp < 50:
        return measured[50], "clamped extrapolation"
    hot = measured[70] * min(1.0, max(0.0, hot_share))
    if temp >= 80:
        # Glass plateaus up to 120 °C. PI climbs again, but the available numbers
        # do not establish how much, so carry the estimated 80 °C value forward.
        extrapolated = temp > (120 if surface == "glass" else 80)
        return hot, "clamped extrapolation" if extrapolated else "estimated hot tail"
    points = {**measured, 80: hot}
    upper = next(t for t in (60, 70, 80) if t > temp)
    lower = upper - 10
    below, above = points[lower], points[upper]
    fraction = (temp - lower) / (upper - lower)
    # Expert can set an anchor to zero, where log interpolation is undefined.
    if below and above:
        shear = exp((1 - fraction) * log(below) + fraction * log(above))
    else:
        shear = (1 - fraction) * below + fraction * above
    return shear, "interpolated shear" if upper <= 70 else "estimated hot tail"


def _shear_weight(offset_c: float) -> float:
    """Estimated grip rises fastest near softening, with bounded cold and hot ends."""
    return 1.125 + 0.875 * tanh(offset_c / 24)


def _edge_peel(model: Model, c: np.ndarray, scale: np.ndarray, held: np.ndarray,
               springs: tuple, grip: float | None) -> dict:
    """Let the bed bond open under the final shrink load, with no pre-cut crack."""
    h = model.grid.h
    bottom = model.elems[:, 2].min()
    footprint = model.grid.solid[:, :, bottom]
    # Share a voxel's bed face among its four springs, including at edges and corners.
    area = np.zeros(np.array(footprint.shape) + 1)
    for x, y in product((0, 1), repeat=2):
        area[x:x + footprint.shape[0], y:y + footprint.shape[1]] += footprint * h**2 / 4
    xy = np.argwhere(area > 0)
    nodes = model.node_index[xy[:, 0], xy[:, 1], bottom]
    area = area[area > 0]
    settings = load_data("thresholds")["bed_adhesion"]
    failure = settings["cohesive_failure_mm"]
    peak = settings["cohesive_peak_share"] * failure
    if not 0 < peak < failure or not isfinite(failure):
        return {"solver_failed": True}
    contact_slope = 30.0  # N/mm³, keeps the bed effectively rigid in compression
    k = stiffness(model, c, scale, springs)
    coarse = prolongation(model) if k.shape[0] > 15_000 else None
    cache = {}
    loads = -held.ravel()  # Replace the clamped bed reactions with spring forces.
    dofs = 3 * nodes[:, None] + np.arange(3)

    def trial(work: float) -> dict:
        # The area of the traction triangle is work per area (N/mm).
        peak_traction = 2 * (work / failure)
        initial_slope = peak_traction / peak
        if initial_slope < 0 or not isfinite(initial_slope):
            return {"solver_failed": True}
        lift, largest, solved = cohesive_bed(k, loads, dofs, area, initial_slope, peak,
                                             failure, contact_slope, cache,
                                             stop_at_lift=grip is None, coarse_grid=coarse)
        detached = largest >= failure if work > 0 else lift > 0
        may_lift = bool(lift.any()) and solved
        result = {"may_lift": may_lift, "detached_area_mm2": float(area[detached].sum()),
                  "solver_failed": not solved}
        if may_lift:
            result["point"] = model.grid.origin + h * np.array([*xy[lift.argmax()], bottom])
        return result

    if grip is not None:
        return trial(grip)
    return _lift_threshold(trial)


def _lift_threshold(trial: Callable[[float], dict]) -> dict:
    """Find the strongest grip that starts to peel, using the normal run's spring law."""
    lower, upper = 0.0, 0.05
    for _ in range(12):
        result = trial(upper)
        if result["solver_failed"]:
            return {"solver_failed": True}
        if not result["may_lift"]:
            break
        lower, upper = upper, 2 * upper
    else:
        return {"solver_failed": True}
    for _ in range(7):
        middle = (lower + upper) / 2
        result = trial(middle)
        if result["solver_failed"]:
            return {"solver_failed": True}
        if result["may_lift"]:
            lower = middle
        else:
            upper = middle
    if lower == 0:
        free = trial(0.0)
        if free["solver_failed"] or not free["may_lift"]:
            return {"solver_failed": free["solver_failed"]}
    return {"lift_grip_n_mm": lower}


def _calibrated_grip(profile: Profile) -> dict | None:
    surface = profile.printer.get("bed_surface", "unknown")
    record = profile.material.get("bed_grip_calibration", {}).get(profile.enclosure, {})
    if surface == "unknown" or record.get("surface") != surface or record["status"] != "calibrated":
        return None
    temp, measured_at = float(profile.material["bed_temp_c"]), float(record["bed_temp_c"])
    grip = record["grip_n_mm"]
    if temp != measured_at:
        # The calibration fixes the level; the estimated curve (T-591) says how grip changes with bed temperature.
        now, then = (bed_grip(profile, t, calibrated=False) for t in (temp, measured_at))
        if now and then and then["grip_n_mm"] > 0:
            grip *= now["grip_n_mm"] / then["grip_n_mm"]
    return {"surface": surface, "assumed_surface": False, "shear_source": "calibrated", "bed_temp_c": temp,
            "grip_n_mm": grip, "confidence": "medium", "calibration_temp_c": measured_at,
            "calibration_temp_differs": temp != measured_at}


def calibrate_bed_grip(profile: Profile, longest_flat_mm: int, shells: Shells = Shells(),
                       lengths: tuple[int, ...] = WARPING_TEST_MM) -> dict:
    """Fit an effective grip between the observed flat bar and the first lifted bar.

    The lift threshold is a grip at which the bond starts to weaken and the
    bar opens. A missing threshold means the bar holds even at zero grip.
    """
    if longest_flat_mm not in (0, *lengths):
        raise ValueError(f"Choose 0 or a length from the warping test: {lengths}")
    surface = profile.printer.get("bed_surface", "unknown")
    record = {"surface": surface, "bed_temp_c": profile.material["bed_temp_c"],
              "longest_flat_mm": longest_flat_mm}
    if surface == "unknown":
        return {**record, "status": "unknown_plate"}

    next_length = next((n for n in lengths if n > longest_flat_mm), None)
    bounds = {}
    for key, length in (("lower_n_mm", longest_flat_mm), ("upper_n_mm", next_length)):
        if length:
            # Match the normal warping grid because rounding the bar's thin
            # cross-section to voxel layers changes the pull on the bond.
            result = simulate(_reference_bar(length), [0, 0, 1], profile, shells, grip_trial=True)
            if not result["peel_solved"]:
                return {**record, "status": "unresolved"}
            if result["lift_grip_n_mm"] is not None:
                bounds[key] = result["lift_grip_n_mm"]
    lower, upper = bounds.get("lower_n_mm"), bounds.get("upper_n_mm")
    if next_length is None:
        return {**record, "status": "lower_only", "lower_n_mm": lower if lower is not None else 0.0}
    if upper is None:
        return {**record, "status": "incompatible"}
    if not longest_flat_mm:
        return {**record, "status": "upper_only", "upper_n_mm": upper}
    if upper <= (lower if lower is not None else 0):
        return {**record, "status": "incompatible"}
    # When the short bar holds at any grip, use the midpoint between zero and the upper bound.
    grip = float(np.sqrt(lower * upper)) if lower else upper / 2
    return {**record, **bounds, "status": "calibrated", "grip_n_mm": grip}


def residual_stress(result: dict) -> np.ndarray:
    """Stress left in each element once the part is off the bed and cool (MPa, von Mises): the
    element's own strain minus the shrink it wanted (stress = C · (strain + shrink))."""
    free = result["strain"] + result["shrink"][:, None] * np.array([1, 1, 1, 0, 0, 0.0])
    return von_mises(result["scale"][:, None] * (free @ result["c"].T))


def fade_length(profile: Profile, area_per_perimeter_mm: float | np.ndarray) -> float | np.ndarray:
    """Height (mm) over which the bed's warmth fades in a section with this area / perimeter: a
    cooling fin, sqrt(k · A/P / h)."""
    h = H_STILL + H_FAN * profile.material.get("fan_pct", 100) / 100
    return np.sqrt(K_PLASTIC * np.asarray(area_per_perimeter_mm) * 1e-3 / h) * 1e3


def fade_depth(model, elem_level: np.ndarray, profile: Profile) -> np.ndarray:
    """How many fade lengths each element sits above the bed (the integral of dz / fade length up
    the part). A/P per voxel layer ≈ h × elements / elements on the surface."""
    grid = model.grid
    solid = np.pad(grid.solid, 1)
    i = model.elems + 1
    shut = np.ones(len(i), dtype=bool)
    for d in np.vstack([np.eye(3, dtype=int), -np.eye(3, dtype=int)]):
        shut &= solid[tuple((i + d).T)]
    n = np.bincount(elem_level)
    outside = np.bincount(elem_level, ~shut, minlength=len(n))
    per_layer = grid.h / np.maximum(fade_length(profile, grid.h * n / np.maximum(outside, 1)), 1e-6)
    below = np.concatenate([[0.0], np.cumsum(per_layer)[:-1]])
    return below[elem_level] + per_layer[elem_level] / 2


def shrink_while_printing(height: np.ndarray, profile: Profile, depth: np.ndarray | None = None) -> np.ndarray:
    """Linear shrinkage of plastic at ``height`` mm above the bed while the print goes on: from where
    it sets (``softening_c``) down to the temperature there (the bed's warmth fading into the air).
    ``depth``: fade lengths above the bed (``fade_depth``); default a thin wall's."""
    from .heat import AMBIENT_C

    mat = profile.material
    t_set, t_air = float(mat["softening_c"]), AMBIENT_C[profile.enclosure]
    if depth is None:
        depth = np.asarray(height) / fade_length(profile, THIN_WALL_MM)
    t_here = t_air + (float(mat["bed_temp_c"]) - t_air) * np.exp(-depth)
    return mat.get("shrinkage_pct", 0.3) / 100 * np.clip(t_set - t_here, 0, None) / max(t_set - ROOM_C, 1.0)


def shrink_below(temp: np.ndarray, profile: Profile) -> np.ndarray:
    """Linear shrinkage of plastic cooling from where it sets down to ``temp`` (°C)."""
    mat = profile.material
    t_set = float(mat["softening_c"])
    return mat.get("shrinkage_pct", 0.3) / 100 * np.clip(t_set - np.asarray(temp), 0, None) / max(t_set - ROOM_C, 1.0)


def curl(result: dict) -> float:
    """How far the base curls off flat (mm, exaggerated): up-movement of the bed nodes after release,
    with the part's tilt taken out."""
    model, bed, up = result["model"], result["bed"], result["up"]
    pos = (model.grid.origin + np.argwhere(model.node_index >= 0) * model.grid.h)[bed]
    w = result["u"][bed] @ up
    a, b = _plane_axes(up)
    fit = np.c_[pos @ a, pos @ b, np.ones(len(pos))]
    res = w - fit @ np.linalg.lstsq(fit, w, rcond=None)[0]
    return float(res.max() - res.min())


def _plane_axes(up: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a = np.cross(up, [1.0, 0, 0] if abs(up[0]) < 0.9 else [0, 1.0, 0])
    a /= np.linalg.norm(a)
    return a, np.cross(up, a)


def lift_stress(result: dict) -> np.ndarray:
    """Pull per bed node as a stress (N/mm²): positive where the part pulls away from the bed."""
    return np.maximum(result["pull"], 0) / result["model"].grid.h ** 2


def reference(profile: Profile, h: float, shells: Shells = Shells()) -> float | None:
    """Curl of the bar that just stays flat, on the same grid size ``h``."""
    length = warp_limit(profile)
    if not length:
        return None
    return curl(simulate(_reference_bar(length), [0, 0, 1], profile, shells, h=h))


def _reference_bar(length: float) -> Mesh:
    from shapely.geometry import box

    from .additions import extrude

    w, t = REF_BAR
    return Mesh(extrude(box(0, 0, length, w), 0, t))
