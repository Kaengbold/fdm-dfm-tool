"""Plain-language finding summaries, message keys, and stable finding IDs (T-524).

Engine texts live in ``data/messages/<lang>.yaml`` and are returned alongside
English strings as ``{key, params}`` message objects (matching ``web/i18n.js``)
so translations are a single extra table later.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .profile import load_data

MAX_TITLE_WORDS = 6
MAX_ONE_LINER_CHARS = 80

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_PLATE_NAMES = {"smooth_pei": "smooth PEI", "glass": "bare glass"}


def load_messages(lang: str = "en") -> dict[str, Any]:
    """Message table for ``lang`` (falls back to English)."""
    try:
        return load_data(f"messages/{lang}")
    except FileNotFoundError:
        return load_data("messages/en")


def _is_message(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("key"), str)


def _fmt_num(value: int | float) -> str:
    """Format a number like ``web/i18n.js`` (up to 2 decimals, grouping commas)."""
    if isinstance(value, bool):
        return str(value)
    r = round(float(value), 2)
    if r == 0:
        r = 0.0
    if r.is_integer():
        return f"{int(r):,}"
    return f"{r:,.2f}".rstrip("0").rstrip(".")


def _fmt_param(value: Any, lang: str) -> str:
    if _is_message(value):
        return format_message(value, lang=lang)
    if isinstance(value, (list, tuple)):
        items = [_fmt_param(v, lang) for v in value]
        if len(items) <= 1:
            return items[0] if items else ""
        if len(items) == 2:
            return f"{items[0]} and {items[1]}"
        return ", ".join(items[:-1]) + f", and {items[-1]}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _fmt_num(value)
    return str(value)


def format_message(key_or_msg: str | dict[str, Any], params: dict[str, Any] | None = None, lang: str = "en") -> str:
    """Render a message key or ``{key, params}`` dict to text. Missing keys return the key itself."""
    if _is_message(key_or_msg):
        assert isinstance(key_or_msg, dict)
        key = key_or_msg["key"]
        params = key_or_msg.get("params") or {}
    else:
        key = str(key_or_msg)
        params = params or {}
    table = load_messages(lang)
    entry = table.get(key)
    if entry is None and lang != "en":
        entry = load_messages("en").get(key)
    if entry is None:
        return key
    if isinstance(entry, dict):
        count = params.get("count", 0)
        entry = entry.get("one" if count == 1 else "other", entry.get("other", key))
    return _PLACEHOLDER_RE.sub(
        lambda m: _fmt_param(params[m.group(1)], lang) if m.group(1) in params else m.group(0),
        str(entry),
    )


def _msg(key: str, **params: Any) -> dict[str, Any]:
    return {"key": key, "params": params}


def _num(value: Any, digits: int = 1) -> int | float:
    r = round(float(value), digits)
    if r == 0:
        return 0
    return int(r) if r.is_integer() else r


def _n0(value: Any) -> int:
    return int(round(float(value)))


def _sag_mid(sag: Any) -> int | float | None:
    if isinstance(sag, (list, tuple)) and len(sag) == 2:
        return _num((float(sag[0]) + float(sag[1])) / 2, 1)
    if isinstance(sag, (int, float)):
        return _num(sag, 1)
    return None


def finding_id(finding: dict[str, Any]) -> str:
    """Stable, order-independent ID for a finding instance.

    Depends only on the finding's own rule, quantised 3D bounding box (0.1 mm),
    and geometric/variant discriminators (hole axis, clean-face flag, upright
    flag, setup variant) — never on list order or goal-shifted severity.
    """
    rule = str(finding["rule"])
    bbox = finding.get("bbox")
    m = finding.get("metrics") or {}
    payload: dict[str, Any] = {"rule": rule}
    if bbox is not None:
        payload["bbox"] = [[int(round(float(v) * 10)) for v in pt] for pt in bbox]
    else:
        payload["scope"] = "part"
        if rule == "setup_flow_too_high":
            payload["kind"] = "speed" if "speed" in m else "hotend"
    axis = m.get("axis")
    if isinstance(axis, (list, tuple)) and len(axis) == 3:
        ax = [float(v) for v in axis]
        for v in ax:
            if abs(v) > 1e-3:
                if v < 0:
                    ax = [-u for u in ax]
                break
        payload["axis"] = [int(round(v * 100)) for v in ax]
    if m.get("clean_face"):
        payload["clean_face"] = True
    if rule in ("living_hinge", "modelled_thread") and "upright" in m:
        payload["upright"] = bool(m["upright"])
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{rule}-{hashlib.sha256(raw).hexdigest()[:10]}"


def _z_place(bbox: Any) -> dict[str, Any]:
    if not bbox:
        return _msg("finding.place.whole_part")
    z0, z1 = float(bbox[0][2]), float(bbox[1][2])
    if z1 - z0 >= 0.5:
        return _msg("finding.place.z_range", z0=_num(z0, 1), z1=_num(z1, 1))
    return _msg("finding.place.z", z=_num(z0, 1))


def _place_message(finding: dict[str, Any]) -> dict[str, Any]:
    rule = finding["rule"]
    m = finding.get("metrics") or {}
    bbox = finding.get("bbox")
    is_side = "axis" in m

    if rule == "sideways_hole_oval":
        d = m.get("diameter")
        return _msg("finding.place.hole_side", d=_num(d, 1)) if d is not None else _msg("finding.place.side")
    if rule in ("hole_closes", "small_hole_undersized"):
        d = m.get("diameter") if is_side else m.get("min_d", m.get("diameter"))
        if d is not None:
            return _msg("finding.place.hole_side" if is_side else "finding.place.hole_upright", d=_num(d, 1))
        return _msg("finding.place.side" if is_side else "finding.place.whole_part")
    if rule == "hole_starts_midair":
        d = m.get("hole_d")
        if d is not None and bbox:
            return _msg("finding.place.hole_z", d=_num(d, 1), z=_num(bbox[0][2], 1))
        if d is not None:
            return _msg("finding.place.hole_midair", d=_num(d, 1))
        return _z_place(bbox)
    if rule == "trapped_support":
        if m.get("diameter") is not None:
            return _msg("finding.place.hole_side", d=_num(m["diameter"], 1))
        return _z_place(bbox)
    if rule == "nut_trap_tight":
        return _msg("finding.place.nut_trap", fastener=m["fastener"]) if m.get("fastener") else _z_place(bbox)
    if rule in ("insert_wall_thin", "screw_wall_thin"):
        w = m.get("wall_mm")
        return _msg("finding.place.wall", w=_num(w, 1)) if w is not None else _z_place(bbox)
    if rule in ("too_thin_to_print", "single_line_wall", "wall_line_mismatch", "load_bearing_wall_thin"):
        w = m.get("width", m.get("wall_mm"))
        return _msg("finding.place.wall", w=_num(w, 2)) if w is not None else _z_place(bbox)
    if rule == "uneven_wall_thickness":
        w = m.get("thin_mm")
        return _msg("finding.place.wall", w=_num(w, 1)) if w is not None else _z_place(bbox)
    if rule == "fine_detail_rough":
        w = m.get("width")
        return _msg("finding.place.detail", w=_num(w, 2)) if w is not None else _z_place(bbox)
    if rule in ("gap_fuses", "flat_gap_fuses"):
        w = m.get("width", m.get("gap_mm"))
        return _msg("finding.place.gap", w=_num(w, 2)) if w is not None else _z_place(bbox)
    if rule in ("tall_thin_feature", "weak_upright_pin"):
        w = m.get("width")
        return _msg("finding.place.pin", w=_num(w, 1)) if w is not None else _z_place(bbox)
    if rule == "living_hinge":
        t = m.get("thickness_mm")
        return _msg("finding.place.hinge", t=_num(t, 2)) if t is not None else _z_place(bbox)
    if rule == "snap_fit_upright":
        t = m.get("thickness_mm")
        return _msg("finding.place.snap_tab", t=_num(t, 2)) if t is not None else _z_place(bbox)
    if rule == "modelled_thread":
        size = m.get("size") or (f"Ø{_fmt_num(m['major_mm'])}" if m.get("major_mm") is not None else None)
        if size:
            key = "finding.place.thread_upright" if m.get("upright") else "finding.place.thread_side"
            return _msg(key, size=size)
        return _z_place(bbox)
    if rule == "shallow_relief":
        return _msg("finding.place.top")
    if rule == "side_detail_small":
        return _msg("finding.place.side")
    if rule in ("small_bed_contact", "print_may_fail", "tipping_risk", "warping", "elephant_foot", "first_layer_detail"):
        return _msg("finding.place.bed")
    if rule == "support_marks":
        return _msg("finding.place.clean_face") if m.get("clean_face") else _z_place(bbox)
    if rule in ("rib_sink_line", "grip_texture"):
        return _msg("finding.place.outer_wall")
    if rule == "coarse_facets":
        return _msg("finding.place.curved_face")
    if rule == "stress_at_inside_corner":
        return _msg("finding.place.inside_corner")
    if rule == "thick_solid_block":
        return _msg("finding.place.interior")
    if rule in ("exceeds_build_volume", "turned_to_fit", "shrinkage_size_error", "vase_mode_possible"):
        return _msg("finding.place.whole_part")
    if rule in ("enclosure_recommended", "hardened_nozzle_required", "setup_plate_too_sticky"):
        return _msg("finding.place.printer")
    if rule in ("dry_filament", "too_hot_in_use", "fibre_filled"):
        return _msg("finding.place.material")
    if rule in ("setup_layer_too_thick", "setup_support_gap_zero", "setup_flow_too_high"):
        return _msg("finding.place.settings")
    return _z_place(bbox)


def _one_liner_message(finding: dict[str, Any], profile: Any = None) -> dict[str, Any]:
    rule = finding["rule"]
    sev = finding.get("severity", "info")
    m = finding.get("metrics") or {}
    if rule == "bed_adhesion" and "start_height_mm" in m:
        return _msg("finding.bed_adhesion.start_height", height=m["start_height_mm"])
    mat_name = profile.material["name"] if profile is not None else None
    prn_name = profile.printer["name"] if profile is not None else None
    nozzle = m.get("nozzle") if m.get("nozzle") is not None else (float(profile.nozzle) if profile is not None else None)

    if rule == "sideways_hole_oval":
        sag = _sag_mid(m.get("sag_mm"))
        if sag is not None and m.get("fastener"):
            return _msg("finding.sideways_hole_oval.one_liner_fastener", sag=sag, fastener=m["fastener"])
        if sag is not None and m.get("diameter") is not None:
            return _msg("finding.sideways_hole_oval.one_liner_size", sag=sag, d=_num(m["diameter"], 1))

    elif rule == "small_hole_undersized":
        if m.get("fastener"):
            return _msg("finding.small_hole_undersized.one_liner_fastener", fastener=m["fastener"])
        d = m.get("min_d", m.get("diameter"))
        if d is not None:
            return _msg("finding.small_hole_undersized.one_liner_size", d=_num(d, 1))

    elif rule == "hole_closes":
        d = m.get("min_d", m.get("diameter"))
        if d is not None and nozzle is not None:
            return _msg("finding.hole_closes.one_liner_size", d=_num(d, 1), nozzle=_num(nozzle, 2))

    elif rule == "hole_starts_midair":
        if m.get("hole_d") is not None:
            return _msg("finding.hole_starts_midair.one_liner_size", d=_num(m["hole_d"], 1))

    elif rule == "long_bridge":
        max_b = m.get("max_bridge_mm") or (profile.material["max_bridge_mm"] if profile is not None else None)
        if m.get("span") is not None and max_b is not None and mat_name:
            return _msg("finding.long_bridge.one_liner_span", span=_n0(m["span"]), material=mat_name, max_bridge=_n0(max_b))

    elif rule == "bridge_sag":
        sag = _sag_mid(m.get("sag_mm"))
        if m.get("span") is not None and sag is not None:
            return _msg("finding.bridge_sag.one_liner_span", span=_n0(m["span"]), sag=sag)

    elif rule == "support_marks":
        if m.get("clean_face") and m.get("area") is not None:
            return _msg("finding.support_marks.one_liner_clean", area=_n0(m["area"]))
        if m.get("area") is not None:
            return _msg("finding.support_marks.one_liner_area", area=_n0(m["area"]))

    elif rule == "trapped_support":
        if m.get("diameter") is not None:
            return _msg("finding.trapped_support.one_liner_hole", d=_n0(m["diameter"]))
        if m.get("depth_mm") is not None and m.get("opening_mm") is not None:
            return _msg("finding.trapped_support.one_liner_pocket", depth=_n0(m["depth_mm"]), opening=_n0(m["opening_mm"]))

    elif rule == "too_thin_to_print":
        if m.get("width") is not None and nozzle is not None:
            return _msg("finding.too_thin_to_print.one_liner_size", width=_num(m["width"], 2), nozzle=_num(nozzle, 2))

    elif rule == "fine_detail_rough":
        if m.get("width") is not None and nozzle is not None:
            return _msg("finding.fine_detail_rough.one_liner_size", width=_num(m["width"], 2), nozzle=_num(nozzle, 2))

    elif rule == "single_line_wall":
        if m.get("width") is not None:
            return _msg("finding.single_line_wall.one_liner_size", width=_num(m["width"], 2))

    elif rule == "gap_fuses":
        if m.get("width") is not None:
            return _msg("finding.gap_fuses.one_liner_size", width=_num(m["width"], 2))

    elif rule == "flat_gap_fuses":
        if m.get("gap_mm") is not None:
            return _msg("finding.flat_gap_fuses.one_liner_size", gap=_num(m["gap_mm"], 2))

    elif rule == "small_bed_contact":
        if sev == "problem" and m.get("bed_area") is None:
            return _msg("finding.small_bed_contact.one_liner_none")
        if m.get("bed_area") is not None:
            return _msg("finding.small_bed_contact.one_liner_area", area=_n0(m["bed_area"]))

    elif rule == "tipping_risk":
        if m.get("height_mm") is not None and m.get("base_width_mm") is not None:
            return _msg("finding.tipping_risk.one_liner_size", height=_n0(m["height_mm"]), width=_n0(m["base_width_mm"]))

    elif rule == "tall_thin_feature":
        if m.get("width") is not None and m.get("height_mm") is not None:
            return _msg("finding.tall_thin_feature.one_liner_size", width=_num(m["width"], 1), height=_n0(m["height_mm"]))

    elif rule == "weak_upright_pin":
        if m.get("width") is not None and m.get("height_mm") is not None:
            return _msg("finding.weak_upright_pin.one_liner_size", width=_num(m["width"], 1), height=_n0(m["height_mm"]))

    elif rule == "thin_neck":
        if m.get("area_mm2") is not None:
            return _msg("finding.thin_neck.one_liner_area", area=_n0(m["area_mm2"]))

    elif rule == "warping":
        if m.get("length_mm") is not None and mat_name:
            return _msg("finding.warping.one_liner_len", length=_n0(m["length_mm"]), material=mat_name)

    elif rule == "elephant_foot":
        if m.get("holes"):
            return _msg("finding.elephant_foot.one_liner_holes", count=int(m["holes"]))

    elif rule == "nut_trap_tight":
        if m.get("across_flats") is not None and m.get("fastener"):
            return _msg("finding.nut_trap_tight.one_liner_size", af=_num(m["across_flats"], 2), fastener=m["fastener"])

    elif rule == "insert_wall_thin":
        if m.get("wall_mm") is not None and m.get("fastener"):
            return _msg("finding.insert_wall_thin.one_liner_size", wall=_num(m["wall_mm"], 1), fastener=m["fastener"])

    elif rule == "screw_wall_thin":
        if m.get("wall_mm") is not None and m.get("fastener"):
            return _msg("finding.screw_wall_thin.one_liner_size", wall=_num(m["wall_mm"], 1), fastener=m["fastener"])

    elif rule == "small_layers_cooling":
        if m.get("temp_c") is not None:
            return _msg("finding.small_layers_cooling.one_liner_temp", temp=_n0(m["temp_c"]))

    elif rule == "heat_too_hot":
        if m.get("temp_c") is not None and m.get("limit_c") is not None:
            return _msg("finding.heat_too_hot.one_liner_temp", temp=_n0(m["temp_c"]), limit=_n0(m["limit_c"]))

    elif rule == "heat_weak_bond":
        if m.get("bond") is not None:
            return _msg("finding.heat_weak_bond.one_liner_bond", bond=_n0(float(m["bond"]) * 100))

    elif rule == "heat_needless_slowdown":
        if m.get("minutes") is not None:
            return _msg("finding.heat_needless_slowdown.one_liner_time", minutes=_num(m["minutes"], 1))

    elif rule == "enclosure_recommended" and mat_name:
        return _msg("finding.enclosure_recommended.one_liner_mat", material=mat_name)

    elif rule == "hardened_nozzle_required" and mat_name:
        return _msg("finding.hardened_nozzle_required.one_liner_mat", material=mat_name)

    elif rule == "dry_filament" and mat_name:
        return _msg("finding.dry_filament.one_liner_mat", material=mat_name)

    elif rule == "too_hot_in_use":
        if mat_name and m.get("use_temp_c") is not None and m.get("heat_limit_c") is not None:
            key = "finding.too_hot_in_use.one_liner_soft" if sev == "problem" else "finding.too_hot_in_use.one_liner_near"
            return _msg(key, material=mat_name, limit=_n0(m["heat_limit_c"]), temp=_n0(m["use_temp_c"]))

    elif rule == "side_detail_small":
        if m.get("depth_mm") is not None and m["depth_mm"] > 0.8:
            return _msg("finding.side_detail_small.one_liner_deep", depth=_num(m["depth_mm"], 1))
        if m.get("height_mm") is not None:
            return _msg("finding.side_detail_small.one_liner_small", height=_num(m["height_mm"], 1))

    elif rule == "shallow_relief":
        if m.get("depth_mm") is not None and m.get("layers") is not None:
            key = "finding.shallow_relief.one_liner_vanish" if sev == "warn" else "finding.shallow_relief.one_liner_faint"
            return _msg(key, depth=_num(m["depth_mm"], 2), layers=_num(m["layers"], 1))

    elif rule == "exceeds_build_volume" and prn_name:
        return _msg("finding.exceeds_build_volume.one_liner_printer", printer=prn_name)

    elif rule == "turned_to_fit":
        if m.get("turned_deg") is not None and prn_name:
            return _msg("finding.turned_to_fit.one_liner_deg", deg=_n0(m["turned_deg"]), printer=prn_name)

    elif rule == "shrinkage_size_error":
        if m.get("length_mm") is not None and m.get("short_mm") is not None:
            return _msg("finding.shrinkage_size_error.one_liner_span", length=_n0(m["length_mm"]), short=_num(m["short_mm"], 1))

    elif rule == "floor_shrink_line":
        if m.get("z") is not None:
            return _msg("finding.floor_shrink_line.one_liner_z", z=_num(m["z"], 1))

    elif rule == "rib_sink_line":
        if m.get("rib_mm") is not None:
            return _msg("finding.rib_sink_line.one_liner_size", rib=_num(m["rib_mm"], 1))

    elif rule == "living_hinge":
        t = m.get("thickness_mm")
        if t is not None:
            if m.get("upright"):
                return _msg("finding.living_hinge.one_liner_upright", t=_num(t, 1))
            if mat_name:
                key = "finding.living_hinge.one_liner_brittle" if sev == "warn" else "finding.living_hinge.one_liner_flat"
                return _msg(key, t=_num(t, 1), material=mat_name)

    elif rule == "snap_fit_upright":
        if m.get("strain_pct") is not None:
            if m.get("strain_max_pct") is not None and mat_name:
                return _msg("finding.snap_fit_upright.one_liner_strain",
                            strain=_num(m["strain_pct"], 1), material=mat_name, max=_num(m["strain_max_pct"], 1))
            return _msg("finding.snap_fit_upright.one_liner_pct", strain=_num(m["strain_pct"], 1))

    elif rule == "thick_solid_block":
        if m.get("thick_cm3") is not None:
            return _msg("finding.thick_solid_block.one_liner_vol", cm3=_n0(m["thick_cm3"]))

    elif rule == "setup_layer_too_thick":
        if m.get("layer_height") is not None and nozzle is not None:
            return _msg("finding.setup_layer_too_thick.one_liner_size", lh=_num(m["layer_height"], 2), nozzle=_num(nozzle, 2))

    elif rule == "setup_support_gap_zero":
        if m.get("gap_mm") is not None:
            return _msg("finding.setup_support_gap_zero.one_liner_gap", gap=_num(m["gap_mm"], 2))

    elif rule == "setup_flow_too_high":
        if m.get("speed") is not None and m.get("flow_mm3s") is not None:
            return _msg("finding.setup_flow_too_high.one_liner_speed", speed=_n0(m["speed"]), flow=_n0(m["flow_mm3s"]))
        if m.get("flow_mm3s") is not None:
            return _msg("finding.setup_flow_too_high.one_liner_hotend", flow=_n0(m["flow_mm3s"]))

    elif rule == "setup_plate_too_sticky":
        if mat_name and m.get("surface"):
            plate = _PLATE_NAMES.get(m["surface"], str(m["surface"]))
            return _msg("finding.setup_plate_too_sticky.one_liner_mat", material=mat_name, plate=plate)

    elif rule == "vase_mode_possible":
        if m.get("vase_min") is not None and m.get("normal_min") is not None:
            return _msg("finding.vase_mode_possible.one_liner_time", vase=_n0(m["vase_min"]), normal=_n0(m["normal_min"]))

    elif rule == "coarse_facets":
        if m.get("width_mm") is not None and m.get("error_mm") is not None:
            return _msg("finding.coarse_facets.one_liner_err", width=_num(m["width_mm"], 1), err=_num(m["error_mm"], 2))

    elif rule == "shallow_slope_stairs":
        if m.get("slope_deg") is not None and m.get("step_mm") is not None:
            return _msg("finding.shallow_slope_stairs.one_liner_step", deg=_n0(m["slope_deg"]), step=_num(m["step_mm"], 1))

    elif rule == "pause_to_insert":
        if m.get("layer") is not None:
            if sev == "warn" and m.get("span_mm") is not None:
                return _msg("finding.pause_to_insert.one_liner_long", layer=int(m["layer"]), span=_n0(m["span_mm"]))
            if m.get("pause_z") is not None:
                return _msg("finding.pause_to_insert.one_liner_layer", layer=int(m["layer"]), z=_num(m["pause_z"], 1))

    elif rule == "modelled_thread":
        if m.get("major_mm") is not None:
            what = (f"{m['size']} " if m.get("size") else f"Ø{_fmt_num(m['major_mm'])} mm ") + (
                "bolt" if m.get("external") else "hole"
            )
            if not m.get("upright"):
                return _msg("finding.modelled_thread.one_liner_lying", what=what)
            if float(m["major_mm"]) < 5.0:
                return _msg("finding.modelled_thread.one_liner_small", what=what)
            if m.get("layers_per_pitch") is not None and float(m["layers_per_pitch"]) < 4.0:
                return _msg("finding.modelled_thread.one_liner_layers", what=what, layers=_num(m["layers_per_pitch"], 1))
            return _msg("finding.modelled_thread.one_liner_ok", what=what)

    elif rule == "wall_line_mismatch":
        if m.get("wall_mm") is not None and m.get("line_width_mm") is not None:
            return _msg("finding.wall_line_mismatch.one_liner_size", wall=_num(m["wall_mm"], 2), lw=_num(m["line_width_mm"], 2))

    elif rule == "load_bearing_wall_thin":
        if m.get("wall_mm") is not None and m.get("min_wall_mm") is not None:
            return _msg("finding.load_bearing_wall_thin.one_liner_size", wall=_num(m["wall_mm"], 2), min=_num(m["min_wall_mm"], 1))

    elif rule == "uneven_wall_thickness":
        if m.get("thin_mm") is not None and m.get("thick_mm") is not None:
            return _msg("finding.uneven_wall_thickness.one_liner_size", thin=_num(m["thin_mm"], 1), thick=_n0(m["thick_mm"]))

    return _msg(f"finding.{rule}.one_liner")


def summarize_finding(finding: dict[str, Any], profile: Any = None) -> dict[str, Any]:
    """Attach ``finding_id``, ``short_title``, ``one_liner``, ``instance_label``, and ``message``."""
    rule = finding["rule"]
    table = load_messages("en")
    fid = finding.get("finding_id") or finding_id(finding)

    title_key = f"finding.{rule}.title"
    title_msg = _msg(title_key)
    short_title = format_message(title_msg) if title_key in table else str(finding.get("title") or rule)

    one_liner_msg = _one_liner_message(finding, profile)
    fallback_key = f"finding.{rule}.one_liner"
    if one_liner_msg["key"] in table:
        one_liner = format_message(one_liner_msg)
        if len(one_liner) > MAX_ONE_LINER_CHARS and fallback_key in table and one_liner_msg["key"] != fallback_key:
            one_liner_msg = _msg(fallback_key)
            one_liner = format_message(one_liner_msg)
    elif fallback_key in table:
        one_liner_msg = _msg(fallback_key)
        one_liner = format_message(one_liner_msg)
    else:
        one_liner = str(finding.get("detail") or short_title)[:MAX_ONE_LINER_CHARS]

    place_msg = _place_message(finding)
    instance_label = format_message(place_msg)

    return {
        **finding,
        "finding_id": fid,
        "short_title": short_title,
        "one_liner": one_liner,
        "instance_label": instance_label,
        "message": {
            "title": title_msg,
            "one_liner": one_liner_msg,
            "instance_label": place_msg,
        },
    }
