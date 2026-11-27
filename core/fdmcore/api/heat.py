from __future__ import annotations

import json

import numpy as np

from .. import progress
from ..checks import RULE_IMPACT, orient
from ..mesh import Mesh
from ..messages import summarize_finding
from ..placement import print_rotation
from ..profile import Profile, load_data
from ..progress import stage
from ..recommend import goal_plan
from ..service import required_safety
from . import check as _check
from . import load as _load
from . import orientation as _orient
from . import session as _S
from . import strength as _strength


def pause_gcode(data: bytes | str, pauses: list[float]) -> dict:
    """A sliced file with a pause before the first layer above each pocket top (``pauses``: mm,
    ``top_z`` of pause_to_insert), for printers whose slicer doesn't take the pause from our 3MF
    (Bambu Studio). Plain G-code or a ``.gcode.3mf`` (each G-code inside, and its .md5 that Bambu
    printers check). Returns ``data`` (same kind of file) and ``pauses`` (where each went)."""
    from ..gcode import add_pauses

    if not pauses:
        raise ValueError("No pause to add: this part has no closed pocket")
    if isinstance(data, str):
        data = data.encode()
    if data[:4] == b"GCDE":
        raise ValueError("This is binary G-code (.bgcode). Turn off “binary G-code” in PrusaSlicer's printer settings and export again")
    if data[:2] != b"PK":
        text, where = add_pauses(data.decode("utf-8", "replace"), pauses)
        return {"data": text.encode(), "pauses": where}
    import hashlib
    import io
    import zipfile

    from ..formats import read_zip

    src = zipfile.ZipFile(io.BytesIO(data))
    names = [n for n in src.namelist() if n.lower().endswith(".gcode")]
    if not names:
        raise ValueError("This 3MF has no G-code inside: slice it and use “Export plate sliced file”")
    files, where = {}, []
    for n in names:
        text, w = add_pauses(read_zip(src, n).decode("utf-8", "replace"), pauses)
        files[n] = text.encode()
        where += w
        if n + ".md5" in src.namelist():  # the printer refuses a file whose checksum doesn't match
            old = read_zip(src, n + ".md5").decode().strip()
            md5 = hashlib.md5(files[n]).hexdigest()
            files[n + ".md5"] = (md5.upper() if old.isupper() else md5).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            z.writestr(info, files.get(info.filename) or read_zip(src, info.filename), compress_type=info.compress_type)
    return {"data": out.getvalue(), "pauses": where}


def print_mesh(up, profile: dict | None = None) -> dict:
    """The part turned and placed as it prints (the same turn on the bed as ``check`` and ``export``), centred on
    x = y = 0 and resting on z = 0: input for a slicer that takes a mesh, like the browser OrcaSlicer
    (web/slicer-worker.js, which moves it to the bed's centre). ``load_gcode`` then finds it again in that G-code."""
    s = _S._require()
    p = Profile.from_dict(profile)
    rotation, _ = print_rotation(s.mesh, up, p.printer["bed"], _orient._moving(p))
    tri = orient(s.mesh, rotation).triangles.reshape(-1, 3)
    return {"triangles": tri.astype(np.float32).tobytes(), "count": int(len(tri) // 3),
            "size_mm": [round(float(v), 2) for v in np.ptp(tri, axis=0)], "rotation": rotation.tolist()}


def load_gcode(data: bytes | str, up, profile: dict | None = None) -> dict:
    """Read the G-code the part was sliced to (from our 3MF, in this orientation ``up``) for the
    detailed level: strength with the real walls, infill and line directions. Checks that it is this
    part (sizes, and most of the plastic inside the model; a quarter turn on the bed is found).
    Plain G-code, Prusa binary G-code, or a Bambu Studio / OrcaSlicer ``.gcode.3mf``
    (the G-code is inside, and its settings next to it)."""
    from ..gcode import CAUSE_NAMES, KIND_NAMES, PART_KINDS, parse, placement, time_lost
    from ..voxel import voxelize

    s = _S._require()
    p = Profile.from_dict(profile)
    settings = None
    if isinstance(data, (bytes, bytearray)) and data[:2] == b"PK":
        import io
        import zipfile

        from ..formats import read_zip

        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = sorted(n for n in z.namelist() if n.lower().endswith(".gcode"))
            if not names:
                raise ValueError("This 3MF has no G-code inside: slice it and use “Export plate sliced file”")
            data = read_zip(z, names[0])
            if "Metadata/project_settings.config" in z.namelist():
                import json

                try:
                    settings = json.loads(read_zip(z, "Metadata/project_settings.config"))
                except ValueError:
                    pass
    tp = parse(data, settings)  # read plain or binary G-code a piece at a time
    rot = print_rotation(s.mesh, up, p.printer["bed"], _orient._moving(p))[0]
    turned = s.mesh.triangles.reshape(-1, 3) @ rot.T
    lo, hi = turned.min(axis=0), turned.max(axis=0)
    base = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])  # orient() moves the part by -base
    grid = voxelize(orient(s.mesh, rot), 30_000)

    def inside(pts):
        idx = grid.index_of(pts)
        ok = np.all((idx >= 0) & (idx < np.array(grid.solid.shape)), axis=1)
        out = np.zeros(len(pts), dtype=bool)
        out[ok] = grid.solid[tuple(idx[ok].T)]
        return out

    q, shift = placement(tp, lo - base, hi - base, inside)
    s.gcode = {"up": np.asarray(up, float) / np.linalg.norm(up), "toolpath": tp, "q": q, "shift": shift + base, "rot": rot, "printed": {}}
    s.warp = None
    vol = {KIND_NAMES[k]: round(float(tp.volume[tp.kind == k].sum()) / 1000, 2) for k in range(len(KIND_NAMES))}
    lost = time_lost(tp)
    return {"slicer": tp.slicer, "firmware": tp.firmware, "layers": int(len(tp.layer_z)), "lines": int(tp.part().sum()),
            "print_time_min": round(tp.total_s / 60, 1), "plastic_cm3": vol,
            "part_grams": round(sum(vol[KIND_NAMES[k]] for k in PART_KINDS) * p.material["density_g_cm3"], 1),
            "turned_deg": int(round(np.degrees(np.arctan2(q[1, 0], q[0, 0])))) % 360,
            # Minutes each cause of slower lines adds (only "layer_time" could be won back by better cooling).
            "slowdown_min": {CAUSE_NAMES[c]: round(float(lost[c]) / 60, 1) for c in range(1, len(CAUSE_NAMES)) if lost[c] >= 3}}


HOT_SPOTS = 20  # heat_model lists this many of the hottest spots


def heat_model(up, profile: dict | None = None, fine: bool = False, intents: list[dict] | None = None,
               loads: list[dict] | None = None, focus: bool = False, ranges: bool = True) -> dict:
    """Heat network over the loaded G-code (thermal.py): how hot every spot is when the next layer
    covers it, how well each joint between layers bonds, and where that leaves the temperature window
    (``findings``: too hot, too cold to bond, or slowed down for cooling it doesn't need). The profile's
    goal moves the window; ``intents`` = [{"kind": cosmetic / dimension_critical / load_bearing,
    "point", "radius"}] (oriented part's frame) tighten it where they are. How much each spot matters
    (``importance``, see the ``importance`` method) relaxes or tightens the bonding goal: from the
    strength check's ``loads`` when given, else guessed from the shape (``guessed``). ``fine``: smaller
    cells and more layers in full detail (slower). Per spot, as float32 bytes in the oriented part's
    frame: ``points`` (x, y, z), ``cover_c_all``, ``cover_s``, ``above_tg_s``, ``bond``, ``joint_c``
    (T_int at each of ``delays_s``), ``importance``; as uint8: ``window`` (0 fine, 1 too hot, 2 too
    cold, 3 needless slowdown) and ``importance_reason`` (index into ``reasons``). ``focus`` (with the quick
    tier): the spots that leave their window or matter most are run again in fine detail, each in a box with a
    margin (``focus``: where, and the hottest covering temperature both ways); the findings there come from the
    fine runs. Cheaper than a fine run of a big part; the per-spot arrays stay the quick tier's.
    ``ranges`` adds two quick heat runs to estimate ranges for the whole-print temperature and bonding summaries;
    skipped for fine or focused detail."""
    from ..importance import REASONS
    from ..thermal import DELAYS_S, confidence, window

    p = Profile.from_dict(profile)
    tp = _toolpath_for(up, "heat model")
    with_ranges = ranges and not (fine or focus)
    with stage(0, 1 / 3 if with_ranges else 1):
        h, tm = _heat(p, fine)
    pts, masks, weight, reason, imp = _spot_marks(h, up, p, intents, loads)
    w = window(h, p, masks, weight)
    state = np.select([w["too_hot"], w["needless"], w["too_cold"]], [1, 3, 2], 0)
    findings = _heat_findings(tp, h, w, pts, p, masks.get("load_bearing"), reason, imp.guessed)
    areas = None
    if focus and not fine:
        findings, areas = _focus(tp, tm, h, w, findings, up, p, intents, loads)
    covered = np.isfinite(h.cover_c)
    hottest = np.flatnonzero(covered)[np.argsort(-h.cover_c[covered])][:HOT_SPOTS]
    f32 = lambda a: np.asarray(a, np.float32).tobytes()
    summary = _heat_summary(h)
    extra = {}
    if with_ranges:
        with stage(1 / 3, 1):
            extra["range"] = _heat_ranges(tp, p, summary, tm)
    return {
        "tier": h.tier, "spots": int(len(h.layer)), "layers": int(h.layer.max()) + 1, "cell_mm": h.cell_mm, "confidence": confidence(p),
        "material": h.material, "tg_c": h.tg_c, "print_min": round(tm.total_s / 60, 1), "seconds": round(h.seconds, 1),
        **summary,
        **extra,
        "hottest": [{"point": np.round(pts[i], 2), "layer": int(h.layer[i]), "cover_c": round(float(h.cover_c[i]), 1),
                     "cover_s": round(float(h.cover_s[i]), 1)} for i in hottest],
        "delays_s": list(DELAYS_S),
        "points": f32(pts), "cover_c_all": f32(h.cover_c), "cover_s": f32(h.cover_s), "above_tg_s": f32(h.above_tg_s),
        "joint_c": f32(h.joint_c), "bond": f32(h.bond), "window": np.asarray(state, np.uint8).tobytes(),
        "importance": f32(w["weight"]), "importance_reason": np.asarray(reason, np.uint8).tobytes(),
        "reasons": list(REASONS), "guessed": imp.guessed,
        "findings": findings,
        **({"focus": areas} if areas is not None else {}),
    }


FOCUS_AREAS = 3  # at most this many fine detail runs


FOCUS_MARGIN_MM = 6.0  # around what is looked at: a cut side loses no heat sideways, so keep it away


FOCUS_MAX_SHARE = 0.5  # a box covering more of the part than this saves too little: run the fine tier instead


def _focus_boxes(h, w: dict) -> list[tuple]:
    """Printer-frame boxes (x0, y0, x1, y1) around what matters most: per column, the spots outside their
    window (and, less, the ones that weigh most), heaviest columns first; grown by the margin, joined where
    they overlap, none much bigger than ``FOCUS_MAX_SHARE`` of the part."""
    flagged = w["too_hot"] | w["too_cold"]
    score = np.where(flagged, 1.0 + w["weight"], np.where(w["weight"] >= 0.9, 0.5 * w["weight"], 0.0))
    if not (score > 0).any():
        return []
    per_column = np.bincount(h.column.astype(np.intp), score)
    lo_all, hi_all = h.xy.min(axis=0), h.xy.max(axis=0)
    part_area = float(np.prod(hi_all - lo_all + h.cell_mm))
    boxes = []
    for col in np.argsort(-per_column)[:FOCUS_AREAS]:
        sel = (h.column == col) & (score > 0)
        if not sel.any():
            continue
        lo = h.xy[sel].min(axis=0) - h.cell_mm / 2 - FOCUS_MARGIN_MM
        hi = h.xy[sel].max(axis=0) + h.cell_mm / 2 + FOCUS_MARGIN_MM
        boxes.append([*lo, *hi])
    merged = []
    for b in sorted(boxes):
        if merged and b[0] <= merged[-1][2] and b[1] <= merged[-1][3] and b[3] >= merged[-1][1]:
            m = merged[-1]
            merged[-1] = [min(m[0], b[0]), min(m[1], b[1]), max(m[2], b[2]), max(m[3], b[3])]
        else:
            merged.append(b)
    return [tuple(float(v) for v in b) for b in merged if (b[2] - b[0]) * (b[3] - b[1]) <= FOCUS_MAX_SHARE * part_area * 1.5]


def _focus(tp, tm, h, w: dict, findings: list[dict], up, p: Profile, intents, loads) -> tuple[list[dict], list[dict]]:
    """Fine detail runs around ``_focus_boxes``: their findings replace the quick ones inside each box (the
    margin itself is left to the quick run), and per box where it is and how hot it got both ways."""
    from ..thermal import simulate, window

    to_oriented = _gcode_to_oriented(_S._require().gcode["rot"])
    areas = []
    for box in _focus_boxes(h, w):
        hf = simulate(tp, tm, p, "fine", region=box)
        ptsf, masks, weight, reason, imp = _spot_marks(hf, up, p, intents, loads)
        wf = window(hf, p, masks, weight)
        inner = ((hf.xy[:, 0] >= box[0] + FOCUS_MARGIN_MM) & (hf.xy[:, 0] <= box[2] - FOCUS_MARGIN_MM)
                 & (hf.xy[:, 1] >= box[1] + FOCUS_MARGIN_MM) & (hf.xy[:, 1] <= box[3] - FOCUS_MARGIN_MM))
        for key in ("too_hot", "too_cold", "needless"):
            wf[key] = wf[key] & inner
        corners = to_oriented(np.array([[box[0] + FOCUS_MARGIN_MM, box[1] + FOCUS_MARGIN_MM, 0], [box[2] - FOCUS_MARGIN_MM, box[3] - FOCUS_MARGIN_MM, 0],
                                        [box[0] + FOCUS_MARGIN_MM, box[3] - FOCUS_MARGIN_MM, 0], [box[2] - FOCUS_MARGIN_MM, box[1] - FOCUS_MARGIN_MM, 0]]))
        lo, hi = corners[:, :2].min(axis=0), corners[:, :2].max(axis=0)

        def inside(f) -> bool:
            c = (np.asarray(f["bbox"][0][:2]) + np.asarray(f["bbox"][1][:2])) / 2
            return bool((c >= lo).all() and (c <= hi).all())

        findings = [f for f in findings if not inside(f)] + _heat_findings(tp, hf, wf, ptsf, p, masks.get("load_bearing"), reason, imp.guessed)
        quick_in = (h.xy[:, 0] >= box[0] + FOCUS_MARGIN_MM) & (h.xy[:, 0] <= box[2] - FOCUS_MARGIN_MM) & \
                   (h.xy[:, 1] >= box[1] + FOCUS_MARGIN_MM) & (h.xy[:, 1] <= box[3] - FOCUS_MARGIN_MM)
        hottest = lambda heat, sel: round(float(np.nanmax(heat.cover_c[sel])), 1) if np.isfinite(heat.cover_c[sel]).any() else None
        areas.append({"bbox": [lo.tolist(), hi.tolist()], "spots": int(inner.sum()), "seconds": round(hf.seconds, 1),
                      "cover_c_max": {"quick": hottest(h, quick_in), "fine": hottest(hf, inner)}})
    return findings, areas


def _toolpath_for(up, what: str):
    """The loaded G-code's toolpath, if it was sliced for build direction ``up``."""
    s = _S._require()
    if s.gcode is None:
        raise ValueError(f"Load the sliced G-code first ({what})")
    if np.dot(s.gcode["up"], np.asarray(up, float) / np.linalg.norm(up)) < 0.999:
        raise ValueError("The G-code was sliced for another orientation: slice this one, or switch back")
    return s.gcode["toolpath"]


def _spot_marks(h, up, p: Profile, intents: list[dict] | None, loads: list[dict] | None):
    """For each heat node: its point in the oriented part's frame, the design-intent masks, its importance
    weight and reason (a heat cell takes its most important plastic), and the importance map itself."""
    from ..importance import REASONS

    pts = _gcode_to_oriented(_S._require().gcode["rot"])(np.column_stack([h.xy, h.z]))
    masks = _intent_masks(pts, intents, h.cell_mm)
    imp = _importance_map(up, p, loads)
    rot, base = _orientation(up, p)
    spread = np.array([(dx, dy, 0.0) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]) * h.cell_mm / 3  # across each cell
    weight, reason = imp.at((pts + base) @ rot, spread @ rot)
    reason = np.where(masks.get("load_bearing", False), REASONS.index("tag"), reason)
    return pts, masks, weight, reason, imp


def optimize_cooling(up, profile: dict | None = None, loads: list[dict] | None = None, intents: list[dict] | None = None,
                     fine: bool = False, detail: bool = False, ranges: bool = False) -> dict:
    """A cooling plan for the loaded G-code (thermal_opt.py): fan ahead of time, slower layers or short park
    moves where spots stay too hot, and the slicer's layer-time slowdown undone where it only made the layers
    too cold to bond (at most ``max_speedup`` of the print time), each spot weighed by how much it matters
    (``loads`` / ``intents`` as for heat_model). Two plans, each checked with the heat model: ``gcode`` (fan
    and speeds per layer; for editing the G-code) and ``slicer`` (what the user's own slicer can take from
    the 3MF: slower heights and a cooling tower; pass its ``cooling`` to ``export``). ``before`` / ``gcode`` /
    ``slicer``: print minutes, spots too hot / too cold (weighted too), median bond. ``summary``: plain words.
    All limits are estimates (``confidence`` "rough") until real prints tune them. ``detail``: also what the
    screen shows before and after the G-code plan (``_cooling_detail``: every spot, a timeline per layer, the
    lines whose speed changes). ``ranges`` adds the quick heat bounds for the print as sliced, not
    a second optimization; ignored in the fine tier."""
    from ..thermal_opt import optimize

    p = Profile.from_dict(profile)
    tp = _toolpath_for(up, "cooling plan")
    marks = {}

    def mark(h):
        if id(h) not in marks:
            marks.clear()
            marks[id(h)] = _spot_marks(h, up, p, intents, loads)
        return marks[id(h)]

    with_ranges = ranges and not fine
    with stage(0, 0.75 if with_ranges else 1):
        r = optimize(tp, p, lambda h: mark(h)[2], lambda h: mark(h)[1], "fine" if fine else "quick")
    out = _cooling_view(r, _importance_map(up, p, loads), p)
    if with_ranges:
        summary = _heat_summary(r["heat"]["before"])
        with stage(0.75, 1):
            out["before"].update(summary, range=_heat_ranges(tp, p, summary))
    return {**out, **_cooling_detail(tp, r, up, p, mark)} if detail else out


DETAIL_MAX_LINES = 300_000  # the lines view sends at most this many (every changed one, then evenly spaced others)


def _match_nodes(a, b) -> np.ndarray:
    """For each heat node of ``a``, the node of ``b`` at the same layer and cell (-1: none). The plan only
    changes timing and fan, so the runs normally match one to one."""
    if len(a.layer) == len(b.layer) and np.array_equal(a.layer, b.layer) and np.allclose(a.xy, b.xy):
        return np.arange(len(a.layer))
    key = lambda x: (x.layer.astype(np.int64) << 32) + (np.round(x.xy[:, 0] / x.cell_mm).astype(np.int64) << 16) + np.round(x.xy[:, 1] / x.cell_mm).astype(np.int64)
    ka, kb = key(a), key(b)
    order = np.argsort(kb)
    at = np.minimum(np.searchsorted(kb[order], ka), len(kb) - 1)
    return np.where(kb[order][at] == ka, order[at], -1)


DETAIL_MAX_PIECES = 300_000  # the printed paths, cut into short pieces so each takes its own spot's colour


def _paths(tp, lines: np.ndarray, heat) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The G-code ``lines`` cut into pieces about two heat cells long (longer when there would be too many), so a
    long wall shows how its heat changes along it (T-587). Returns the pieces' ends (printer frame), the line
    each came from, and the heat node it lies in (same layer and cell as the heat model's; a sliver cell the
    model joined to a neighbour takes that neighbour; -1 when none)."""
    a, b = np.asarray(tp.p0[lines], float), np.asarray(tp.p1[lines], float)
    length = np.linalg.norm(b - a, axis=1)
    cell = heat.cell_mm
    piece = max(2 * cell, float(length.sum()) / DETAIL_MAX_PIECES)
    n = np.maximum(np.ceil(length / piece).astype(np.intp), 1)  # np.repeat needs intp (32 bits in Pyodide)
    if n.sum() > DETAIL_MAX_PIECES:
        n[:] = 1
    src = np.repeat(np.arange(len(lines)), n)
    k = np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)
    f0, f1 = (k / n[src])[:, None], ((k + 1) / n[src])[:, None]
    pa, pb = a[src] + (b - a)[src] * f0, a[src] + (b - a)[src] * f1
    mid = (pa + pb) / 2
    key = lambda layer, gx, gy: (layer.astype(np.int64) << 40) + ((gx.astype(np.int64) + (1 << 19)) << 20) + (gy.astype(np.int64) + (1 << 19))
    nodes = key(heat.layer, np.floor(heat.xy[:, 0] / cell), np.floor(heat.xy[:, 1] / cell))
    order = np.argsort(nodes)
    sorted_keys = nodes[order]
    layer = tp.layer[lines][src]
    gx, gy = np.floor(mid[:, 0] / cell), np.floor(mid[:, 1] / cell)
    spot = np.full(len(src), -1, np.int64)
    for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
        todo = spot < 0
        if not todo.any() or not len(sorted_keys):
            break
        want = key(layer[todo], gx[todo] + dx, gy[todo] + dy)
        at = np.minimum(np.searchsorted(sorted_keys, want), len(sorted_keys) - 1)
        spot[todo] = np.where(sorted_keys[at] == want, order[at], -1)
    return pa, pb, src, spot


def _replay(base, after, idx: np.ndarray) -> dict | None:
    """The print played back (T-588): every spot's temperature when each kept layer is done, as sliced and with
    the G-code plan, both on the spots of ``base`` (``idx``: base spot -> after node, ``_match_nodes``). ``temp``
    (before, after) are uint8 blocks from ``lo_c`` to ``hi_c``, one per entry of ``layer``, starting at ``start``
    (a block holds the spots up to that layer); ``t_s`` and ``fan`` (0–1) at that moment, for each run."""
    sb, sa = base.snaps, after.snaps
    if not sb or not sa or sb["layer"] != sa["layer"]:
        return None
    ends = np.append(sb["start"], len(sb["data"]))
    after_ends = np.append(sa["start"], len(sa["data"]))
    moved = np.zeros(len(sb["data"]), np.uint8)
    for j in range(len(sb["layer"])):
        block = sa["data"][after_ends[j]:after_ends[j + 1]]
        sel = idx[: ends[j + 1] - ends[j]]
        ok = (sel >= 0) & (sel < len(block))
        moved[ends[j]:ends[j + 1]][ok] = block[sel[ok]]
    f32 = lambda a: np.asarray(a, np.float32).tobytes()
    return {"layer": np.asarray(sb["layer"], np.int32).tobytes(), "start": np.asarray(sb["start"], np.int32).tobytes(),
            "t_s": [f32(sb["t_s"]), f32(sa["t_s"])], "fan": [f32(sb["fan"]), f32(sa["fan"])],
            "temp": [np.asarray(sb["data"], np.uint8).tobytes(), moved.tobytes()], "lo_c": sb["lo_c"], "hi_c": sb["hi_c"]}


def _cooling_detail(tp, r: dict, up, p: Profile, mark) -> dict:
    """What the simulation step shows (T-478), before and after the G-code plan. ``spots`` (heat nodes as
    sliced, in the oriented part's frame; the after values are the same spots in the planned run): ``points``,
    ``layer`` (int32), ``cover_c`` / ``cover_s`` / ``bond`` (before, after), ``window`` (uint8: 0 fine, 1 too
    hot, 2 too cold, 3 needless slowdown), ``limit_c`` (too hot above this), ``importance`` and its
    ``reason``. ``timeline`` per layer: height, seconds until the next layer, part fan (0–1), median covering
    temperature (before, after) and the sag limit; ``tg_c`` is the window's cold side. ``lines``: the part's
    lines cut into short pieces (``segments``: x0 y0 z0 x1 y1 z1) with ``factor`` (new speed / old), ``layer``,
    ``width`` / ``height`` (mm) and ``spot`` (the heat spot each lies in, -1: none; ``_paths``); ``fan_marks``
    where the plan changes the fan. ``slowdown_min``: minutes each slicer slowdown cause costs (what limits
    the print). ``rotation``: the oriented frame (part @ rotation.T, centred, on z = 0)."""
    from ..gcode import CAUSE_NAMES, fan_levels, time_lost
    from ..importance import REASONS
    from ..kinematics import timing
    from ..thermal_opt import apply

    base, after = r["heat"]["before"], r["heat"]["gcode"]
    plan = r["plans"]["gcode"]
    pts, _, weight, reason, _ = mark(base)
    idx = _match_nodes(base, after)
    hit = idx >= 0

    def moved(values: np.ndarray, fill=np.nan) -> np.ndarray:
        out = np.full(len(idx), fill, dtype=np.result_type(values, np.asarray(fill)))
        out[hit] = values[idx[hit]]
        return out

    def state(win: dict) -> np.ndarray:
        return np.select([win["too_hot"], win["needless"], win["too_cold"]], [1, 3, 2], 0).astype(np.uint8)

    f32 = lambda a: np.asarray(a, np.float32).tobytes()
    win0, win1 = r["windows"]["before"], r["windows"]["gcode"]
    spots = {"points": f32(pts), "layer": np.asarray(base.layer, np.int32).tobytes(),
             "cover_c": [f32(base.cover_c), f32(moved(after.cover_c))], "cover_s": [f32(base.cover_s), f32(moved(after.cover_s))],
             "bond": [f32(base.bond), f32(moved(after.bond))],
             "window": [state(win0).tobytes(), moved(state(win1), np.uint8(0)).tobytes()],
             "limit_c": f32(win0["limit_c"]), "importance": f32(weight), "reason": np.asarray(reason, np.uint8).tobytes(),
             "reasons": list(REASONS), "cell_mm": base.cell_mm}

    planned = apply(tp, plan)
    layers = int(tp.layer.max()) + 1

    def per_layer(path) -> tuple[np.ndarray, np.ndarray]:
        tm = timing(path)
        start = np.full(layers, np.inf)
        np.minimum.at(start, path.layer, tm.t0)
        start = np.minimum.accumulate(start[::-1])[::-1]  # a layer without lines starts with the next one
        seconds = np.diff(np.concatenate([start, [tm.total_s]]))
        part = path.part()
        fan = np.full(layers, np.nan)
        sums = np.bincount(path.layer[part].astype(np.intp), path.part_fan[part], minlength=layers)
        counts = np.bincount(path.layer[part].astype(np.intp), minlength=layers)
        fan[counts > 0] = sums[counts > 0] / counts[counts > 0]
        return seconds, fan

    def layer_median(values: np.ndarray, layer: np.ndarray) -> np.ndarray:
        out = np.full(layers, np.nan)
        ok = np.isfinite(values)
        for k in np.unique(layer[ok]):
            if k < layers:
                out[k] = np.median(values[ok & (layer == k)])
        return out

    s0, fan0 = per_layer(tp)
    s1, fan1 = per_layer(planned)
    z = np.asarray(tp.layer_z[:layers], float)
    timeline = {"z": f32(np.concatenate([z, np.full(layers - len(z), z[-1] if len(z) else 0.0)])),
                "seconds": [f32(s0), f32(s1)], "fan": [f32(fan0), f32(fan1)],
                "cover_c": [f32(layer_median(base.cover_c, base.layer)), f32(layer_median(after.cover_c, after.layer))],
                "limit_c": f32(layer_median(win0["limit_c"], base.layer)), "tg_c": base.tg_c}

    rot = _orientation(up, p)[0]
    to_oriented = _gcode_to_oriented(_S._require().gcode["rot"])
    part = np.flatnonzero(tp.part())
    factor = planned.speed[part] / np.maximum(tp.speed[part], 1e-6)
    keep = np.abs(factor - 1) > 0.01
    if len(part) > DETAIL_MAX_LINES:
        rest = np.flatnonzero(~keep)
        room = max(DETAIL_MAX_LINES - int(keep.sum()), 0)
        keep[rest[:: max(1, len(rest) // max(room, 1))][:room]] = True
    else:
        keep[:] = True
    chosen = part[keep]
    pa, pb, src, spot = _paths(tp, chosen, base)
    seg = np.column_stack([to_oriented(pa), to_oriented(pb)])
    a, b = np.asarray(tp.p0[chosen], float), np.asarray(tp.p1[chosen], float)
    width = tp.volume[chosen] / np.maximum(np.linalg.norm(b - a, axis=1) * tp.layer_height[chosen], 1e-6)
    # Where the plan sets a fan the slicer didn't: the first line of each stretch with a new value.
    fan_marks = []
    fans = [("part", tp.part_fan, planned.part_fan)]
    if plan.aux is not None and np.isfinite(plan.aux).any():
        fans.append(("aux", fan_levels(tp, "P2"), fan_levels(planned, "P2")))
    for channel, old, new in fans:
        changed = np.abs(new - old) > 0.01
        starts = np.flatnonzero(changed & (np.r_[True, np.abs(np.diff(new)) > 0.01] | ~np.r_[False, changed[:-1]]))
        fan_marks.extend({"layer": int(tp.layer[i]), "point": to_oriented(np.asarray(tp.p0[i], float)[None])[0].round(2).tolist(),
                          "fan": round(float(new[i]), 2), "channel": channel} for i in starts[tp.part()[starts]][:200])
    lost = time_lost(tp)
    return {"spots": spots, "timeline": timeline, "rotation": np.round(rot, 6).tolist(), "replay": _replay(base, after, idx),
            "lines": {"segments": f32(seg), "factor": f32(factor[keep][src]), "layer": np.asarray(tp.layer[chosen][src], np.int32).tobytes(),
                      "width": f32(np.clip(width, 0.1, 3.0)[src]), "height": f32(tp.layer_height[chosen][src]),
                      "spot": np.asarray(spot, np.int32).tobytes(), "all": int(len(part))},
            "fan_marks": fan_marks,
            "slowdown_min": {CAUSE_NAMES[c]: round(float(lost[c]) / 60, 1) for c in range(1, len(CAUSE_NAMES)) if lost[c] >= 3}}


def optimize_gcode(up, data: bytes, profile: dict | None = None, loads: list[dict] | None = None,
                   intents: list[dict] | None = None, fine: bool = False) -> dict:
    """Option B (opt-in): the cooling plan written into the user's own G-code (gcode_rewrite.py): only speeds,
    fan commands and cooling park moves change; the original file stays theirs. ``data``: the plain
    or binary G-code loaded with load_gcode. The output is plain G-code, read back and checked (same paths and plastic
    per layer, machine speed and flow limits) and simulated again: no spot may end up outside its window that
    wasn't before. ``ok`` False: no ``data``, and ``problems`` says why. Else ``data`` (the new G-code),
    ``minutes`` (before, after) and ``changes``."""
    from ..gcode import parse
    from ..gcode_rewrite import check, rewrite
    from ..kinematics import timing
    from ..thermal import simulate, window
    from ..thermal_opt import _same, optimize

    p = Profile.from_dict(profile)
    loaded = _toolpath_for(up, "G-code optimization")
    if isinstance(data, (bytes, bytearray)) and data[:2] == b"PK":
        raise ValueError("Plain .gcode only for now: export the G-code from the slicer (not the .gcode.3mf project)")
    tp = parse(data, loaded.settings)
    if len(tp.p0) != len(loaded.p0) or not np.allclose(tp.p0, loaded.p0, atol=1e-3):
        raise ValueError("This isn't the G-code that is loaded: load it first (load_gcode)")
    marks = {}

    def mark(h):
        if id(h) not in marks:
            marks.clear()
            marks[id(h)] = _spot_marks(h, up, p, intents, loads)
        return marks[id(h)]

    weight_at, intents_at = (lambda h: mark(h)[2]), (lambda h: mark(h)[1])
    tier = "fine" if fine else "quick"
    r = optimize(tp, p, weight_at, intents_at, tier)
    if not r["gcode"]["plan"]:
        return {"ok": False, "problems": ["Nothing to change. " + _cooling_view(r, _importance_map(up, p, loads), p)["summary"][0]]}
    new = rewrite(data, tp, r["plans"]["gcode"], [f"Spots too hot {r['before']['too_hot']} -> {r['gcode']['too_hot']}, "
                                                  f"too cold to bond {r['before']['too_cold']} -> {r['gcode']['too_cold']} (estimated limits)."])
    problems = check(tp, new.text, new.toolpath)
    if not problems:
        back = parse(new.text, loaded.settings)
        h = simulate(back, timing(back), p, tier)
        before, win0 = r["heat"]["before"], r["windows"]["before"]
        win = window(h, p, intents_at(h), weight_at(h))
        worse_hot = win["too_hot"] & ~_same(win0["too_hot"], h, before)
        worse_cold = win["too_cold"] & ~_same(win0["too_cold"], h, before)
        if worse_hot.any() or worse_cold.any():
            n_hot, n_cold = int(worse_hot.sum()), int(worse_cold.sum())
            problems.append(f"The heat check of the new file finds {n_hot} {'spot' if n_hot == 1 else 'spots'} newly too hot and "
                            f"{n_cold} newly too cold to bond")
    if problems:
        return {"ok": False, "problems": problems}
    return {"ok": True, "data": new.text.encode(), "minutes": [round(m, 1) for m in new.minutes],
            "too_hot": [r["before"]["too_hot"], r["gcode"]["too_hot"]], "too_cold": [r["before"]["too_cold"], r["gcode"]["too_cold"]],
            "changes": {"speed_lines": new.changed_lines, "fan_commands": new.fan_commands, "park_moves": new.parks,
                        "kept_near_slicer_speeds": new.clamped}}


def _cooling_view(r: dict, imp, p: Profile) -> dict:
    """thermal_opt.optimize's result as optimize_cooling shows it."""
    from ..thermal import confidence

    def view(part: dict) -> dict:
        return {"print_min": round(part["print_s"] / 60, 1), **{k: part[k] for k in ("too_hot", "too_cold", "needless", "bond_median")},
                "weighted_too_hot": round(part["weighted_hot"], 1), "weighted_too_cold": round(part["weighted_cold"], 1)}

    out = {"before": view(r["before"]), "gcode": {**view(r["gcode"]), "plan": r["gcode"]["plan"]},
           "slicer": {**view(r["slicer"]), "cooling": r["slicer"]["cooling"]}, "guessed": imp.guessed, "confidence": confidence(p),
           "aux_fan_estimated": r["aux_fan_estimated"]}
    out["summary"] = _cooling_summary(out)
    return out


CONFIDENCE_TEXT = {
    "rough": "Rough: the limits are estimates, not yet tuned to your printer.",
    "partly tuned": "Partly tuned: one heat limit is measured, the other is still an estimate.",
    "tuned": "Tuned to your printer.",
}


def _cooling_summary(r: dict) -> list[str]:
    """What the cooling plans change, in plain words; why nothing, when nothing can be gained."""
    b, g, sl = r["before"], r["gcode"], r["slicer"]

    def spots(x):
        n = x["too_hot"]
        return f"{n} {'spot' if n == 1 else 'spots'} too hot, {x['too_cold']} too cold to bond well"

    if not g["plan"] and b["too_hot"] == 0 and b["too_cold"] == 0:
        lines = ["Nothing to gain: every spot stays in its temperature window, and the slicer slowed no layer for cooling it didn't need."]
        if r["aux_fan_estimated"]:
            lines.append("The side fan's extra cooling is an estimate.")
        return lines
    lines = [f"As sliced: {b['print_min']:.1f} min, {spots(b)}."]
    if g["plan"]:
        lines.append(f"With the cooling plan in the G-code: {g['print_min']:.1f} min, {spots(g)}.")
        if g["too_hot"] and g["too_hot"] < b["too_hot"]:
            lines.append("The spots still too hot can't be cooled more without their layers bonding too weakly: a feature "
                         "that thin can't do both. Making it thicker helps most.")
    else:
        lines.append("The G-code plan finds nothing worth changing: the spots left outside their window can't be fixed with the fan or the speed.")
    extra = " and a cooling tower beside the part" if sl["cooling"].get("tower") else ""
    if sl["cooling"]["ranges"] or sl["cooling"].get("tower"):
        lines.append(f"With your slicer's settings only (slower heights{extra}): {sl['print_min']:.1f} min, {spots(sl)}. "
                     "Fan changes and undoing the slicer's slowdown need the G-code.")
    if r["aux_fan_estimated"]:
        lines.append("The side fan's extra cooling is an estimate.")
    lines.append(CONFIDENCE_TEXT[r["confidence"]] + (" Which spots matter is guessed from the shape: add the loads for a real answer." if r["guessed"] else ""))
    return lines


def _heat_key(p: Profile) -> str:
    return json.dumps([p.material_id, p.material, p.printer, p.enclosure], sort_keys=True, default=str)


def _heat_summary(heat) -> dict:
    """Report temperatures only where the next layer covers a spot, and bonding only at actual joints."""
    cover = heat.cover_c[np.isfinite(heat.cover_c)]
    bond = heat.bond[np.isfinite(heat.bond)]
    return {"cover_c": {name: round(float(np.percentile(cover, q)), 1) if len(cover) else None
                        for name, q in (("median", 50), ("p95", 95))},
            "bond_median": round(float(np.median(bond)), 3) if len(bond) else None}


def _heat_ranges(toolpath, profile: Profile, nominal: dict, timing_result=None) -> dict:
    """Contact conductance controls how much heat crosses a joint, and the fan controls how fast it
    leaves the part. Sample weaker contact/stronger fan and the reverse as contrasting conditions,
    not a guarantee of the extrema. The G-code and its timing do not change between the reruns."""
    from ..kinematics import timing
    from ..thermal import simulate

    gcode = _S._require().gcode
    key = _heat_key(profile)
    if gcode.get("heat_ranges", (None,))[0] != key:
        if timing_result is None:
            timing_result = timing(toolpath)
        process = load_data("thermal")["process"]
        fan = process["h_fan"]
        contacts = process["bead_contact_conductance"]["range"]
        points = fan["points"]
        off = points[0]
        samples = []
        for index, (contact, full_fan) in enumerate(zip(contacts, reversed(fan["range_at_100"]))):
            factor = (full_fan - off) / (points[100] - off)
            curve = {speed: off + (value - off) * factor for speed, value in points.items()}
            with progress.stage(index / 2, (index + 1) / 2):
                heat = simulate(toolpath, timing_result, profile, overrides={"contact": contact, "h_fan": curve})
                samples.append(_heat_summary(heat))
        gcode["heat_ranges"] = (key, samples)

    samples = [nominal, *gcode["heat_ranges"][1]]

    def bounds(values):
        return None if any(value is None for value in values) else [min(values), max(values)]

    return {"cover_c": {name: bounds([sample["cover_c"][name] for sample in samples]) for name in nominal["cover_c"]},
            "bond_median": bounds([sample["bond_median"] for sample in samples])}


def _heat(p: Profile, fine: bool = False):
    """The heat model over the loaded G-code (thermal.Heat, kinematics.Timing), cached per profile and tier:
    heat_model and the strength check's heat option share one run."""
    from ..kinematics import timing
    from ..thermal import simulate

    g = _S._require().gcode
    key = (_heat_key(p), bool(fine))
    if g.get("heat", (None,))[0] != key:
        with progress.stage(0, 0.05):
            tm = timing(g["toolpath"])
        with progress.stage(0.05, 1):
            g["heat"] = (key, (simulate(g["toolpath"], tm, p, "fine" if fine else "quick"), tm))
    return g["heat"][1]


def _bond_per_cube(model, g: dict, h, mat) -> tuple[np.ndarray, np.ndarray]:
    """Find the weakest joint crossing each cube: a crack can start at a single cold interface.
    Compare each joint's flat footprint with the cube's bounds in the printer frame.
    A cube without an overlapping joint keeps its usual strength and has source index -1."""
    from scipy.spatial import cKDTree

    from ..thermal import bond_strength

    joints = np.flatnonzero(np.isfinite(h.joint_z) & np.isfinite(h.bond))
    share = np.ones(len(model.elems))
    source = np.full(len(model.elems), -1, dtype=int)
    if not len(joints):
        return share, source

    # Rank by healing so a single indexed minimum keeps both the factor and its source.
    joints = joints[np.argsort(h.bond[joints], kind="stable")]
    points = np.column_stack([h.xy[joints], h.joint_z[joints]])
    rotation = g["q"].T @ g["rot"]
    centers = (model.grid.centers(model.elems) @ g["rot"].T - g["shift"]) @ g["q"]
    reach = np.abs(rotation).sum(axis=1) * model.grid.h / 2 + np.array([h.cell_mm / 2, h.cell_mm / 2, 0])
    # Keep a joint on a cube face despite roundoff in the coordinate transforms.
    tree = cKDTree((points - points[0]) / reach)
    scaled = (centers - points[0]) / reach
    # Bound the temporary pair list when a coarse strength cube spans many fine heat cells.
    for start in range(0, len(centers), 512):
        block = scaled[start:start + 512]
        pairs = cKDTree(block).sparse_distance_matrix(tree, 1 + 1e-8, p=np.inf, output_type="ndarray")
        best = np.full(len(block), len(joints), dtype=int)
        np.minimum.at(best, pairs["i"], pairs["j"])
        found = best < len(joints)
        source[start + np.flatnonzero(found)] = joints[best[found]]
    found = source >= 0
    share[found] = bond_strength(h.bond[source[found]], mat.strength_xy / mat.strength_z)
    return share, source


def _orientation(up, p: Profile) -> tuple[np.ndarray, np.ndarray]:
    """``rot`` and ``base`` of the oriented part (``orient``): oriented = part @ rot.T - base."""
    rot = print_rotation(_S._require().mesh, up, p.printer["bed"], _orient._moving(p))[0]
    turned = _S._require().mesh.triangles.reshape(-1, 3) @ rot.T
    lo, hi = turned.min(axis=0), turned.max(axis=0)
    return rot, np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])


def _intent_masks(pts: np.ndarray, intents: list[dict] | None, reach: float) -> dict:
    """Design-intent marks ({"kind", "point", "radius"}, oriented frame) -> kind: mask over ``pts``."""
    from ..thermal import INTENTS

    masks = {}
    for mark in intents or []:
        if mark["kind"] not in INTENTS:
            raise ValueError(f"Unknown design intent: {mark['kind']} (use one of {', '.join(INTENTS)})")
        near = np.linalg.norm(pts - np.asarray(mark["point"], float), axis=1) <= float(mark.get("radius", 5.0)) + reach
        masks[mark["kind"]] = masks.get(mark["kind"], np.zeros(len(pts), bool)) | near
    return masks


def _importance_map(up, p: Profile, loads: list[dict] | None):
    """importance.Map for build direction ``up``: from the strength check's layer index for ``loads``,
    else guessed from the shape. Cached per part, direction, loads, profile and feature answers."""
    from .. import importance
    from ..regions import find_regions
    from ..strength import layer_index

    s = _S._require()
    up = np.asarray(up, float) / np.linalg.norm(up)
    key = json.dumps([np.round(up, 6).tolist(), loads or None, p.material, p.goal, p.constant_load, p.use_temp_c, s.tags, s.cad_checks],
                     sort_keys=True, default=str)
    if s.importance is None or s.importance[0] != key:
        if loads:
            cases = _strength._load_case(loads)
            model = cases[0].model
            fi = layer_index(cases, up, p, _strength._shells(p))
            m = importance.from_strength(model.grid, model.elems, fi, required_safety(p))
        else:
            if s.regions is None:
                s.regions = find_regions(s.mesh)
            m = importance.guessed(s.mesh, up, _load._features())
        s.importance = (key, m)
    return s.importance[1]


IMPORTANCE_TEXT = {
    "load": "The layers here carry a big share of the load.",
    "tag": "You marked this spot as load-bearing.",
    "root": "Root of a thin upright feature: its layers bend apart here first.",
    "pin": "Thin upright feature: it snaps easily along its layers.",
    "corner": "Sharp inside corner: stress gathers here.",
    "hole": "Around a screw or insert hole: these layers hold the screw.",
}


def importance(up, loads: list[dict] | None = None, profile: dict | None = None, intents: list[dict] | None = None) -> dict:
    """How much each spot of the part matters (importance.py), which tells the heat window where to ask
    for a good bond between layers. From the strength check's ``loads`` when given; else guessed from
    the shape (``guessed``: thin upright pins and ribs, their roots, sharp inside corners, screw and
    insert holes, outer walls over infill), so the app can offer "Add loads" for a real answer.
    ``intents`` marked load_bearing weigh 1. Per analysis cube, in the oriented part's frame (as
    heat_model): ``points`` (float32 x, y, z), ``weight`` (float32, ``floor`` to 1) and ``reason``
    (uint8 index into ``reasons``). ``areas``: the important spots, most important first, each with
    its reason in plain words."""
    from .. import importance as imp_mod

    p = Profile.from_dict(profile)
    m = _importance_map(up, p, loads)
    centres, weight, reason = m.voxels()
    rot, base = _orientation(up, p)
    pts = centres @ rot.T - base
    tagged = _intent_masks(pts, intents, m.grid.h).get("load_bearing")
    if tagged is not None:
        weight, reason = np.where(tagged, 1.0, weight), np.where(tagged, imp_mod.REASONS.index("tag"), reason)
    areas = []
    for group in imp_mod.areas(m, weight):
        top = group[np.argmax(weight[group])]
        why = imp_mod.REASONS[reason[top]]
        lo, hi = pts[group].min(axis=0) - m.grid.h / 2, pts[group].max(axis=0) + m.grid.h / 2
        areas.append({"reason": why, "weight": round(float(weight[top]), 2), "text": IMPORTANCE_TEXT[why],
                      "point": np.round(pts[group].mean(axis=0), 1).tolist(), "bbox": [lo.tolist(), hi.tolist()]})
    f32 = lambda a: np.asarray(a, np.float32).tobytes()
    return {
        "guessed": m.guessed, "voxel_mm": float(m.grid.h), "floor": float(imp_mod.settings()["floor"]),
        "reasons": list(imp_mod.REASONS), "points": f32(pts), "weight": f32(weight),
        "reason": np.asarray(reason, np.uint8).tobytes(), "areas": areas,
        "note": "Guessed from the shape. Add the loads for a real answer." if m.guessed else None,
    }


def _heat_findings(tp, h, w: dict, pts: np.ndarray, p: Profile, load_bearing, reason: np.ndarray, guessed: bool) -> list[dict]:
    """One finding per stretch of a column (layers in a row) that leaves the window, in plain words."""
    from ..gcode import LAYER_TIME
    from ..importance import REASONS, settings

    name = p.material["name"]
    out = []
    for rule, flag in (("heat_too_hot", w["too_hot"]), ("heat_needless_slowdown", w["needless"]),
                       ("heat_weak_bond", w["too_cold"] & ~w["needless"])):
        for col in np.unique(h.column[flag]):
            nodes = np.flatnonzero(flag & (h.column == col))
            layers = np.unique(h.layer[nodes])
            for run in np.split(layers, np.flatnonzero(np.diff(layers) > 1) + 1):
                m = nodes[np.isin(h.layer[nodes], run)]
                lo, hi = pts[m].min(axis=0), pts[m].max(axis=0)
                half = h.cell_mm / 2
                bbox = [[float(lo[0] - half), float(lo[1] - half), float(lo[2])], [float(hi[0] + half), float(hi[1] + half), float(hi[2])]]
                where = f"At {lo[2]:.1f}–{hi[2]:.1f} mm" if hi[2] - lo[2] >= 0.05 else f"At {lo[2]:.1f} mm"
                if rule == "heat_too_hot":
                    t, limit = float(np.nanmax(h.cover_c[m])), float(np.median(w["limit_c"][m]))
                    out.append({"rule": rule, "severity": "warn", "bbox": bbox, "metrics": {"temp_c": round(t), "limit_c": round(limit)},
                                "detail": f"{where} this spot is still about {t:.0f} °C when the next layer goes on. {name} holds its "
                                          f"shape there only up to about {limit:.0f} °C, so it can sag or blob."})
                    continue
                bond = float(np.nanmedian(h.bond[m]))
                metrics = {"bond": round(bond, 2), "min_bond": round(float(np.median(w["min_bond"][m])), 2)}
                if rule == "heat_needless_slowdown":
                    lines = np.isin(tp.layer, run) & (tp.cause == LAYER_TIME)
                    length = np.linalg.norm(tp.p1[lines] - tp.p0[lines], axis=1)
                    minutes = float(np.sum(length / tp.speed[lines] - length / tp.set_speed[lines])) / 60
                    out.append({"rule": rule, "severity": "info", "bbox": bbox, "metrics": {**metrics, "minutes": round(minutes, 1)},
                                "detail": f"{where} the slicer slows down so the layers can cool (about {minutes:.1f} min), but they "
                                          f"already end up too cool to bond well. Print these layers faster, or with less fan."})
                    continue
                # Warn where the user said it carries load, or the strength check found it does; a guessed
                # important spot is worth knowing about, but only a hint until loads are entered.
                weight = float(np.median(w["weight"][m]))
                top = REASONS[int(np.bincount(reason[m]).argmax())]
                carrying = (load_bearing is not None and bool(load_bearing[m].any())) or (not guessed and weight > float(settings()["ordinary"]))
                why = f" {IMPORTANCE_TEXT[top]}" if top in IMPORTANCE_TEXT and top not in ("load", "tag") else ""
                guess = " (Guessed from the shape: add the loads for a real answer.)" if guessed and why else ""
                out.append({"rule": rule, "severity": "warn" if carrying else "info", "bbox": bbox,
                            "metrics": {**metrics, "importance": round(weight, 2), "reason": top},
                            "detail": f"{where} each new layer lands on plastic that has cooled a lot, so the layers fuse only "
                                      f"partly (a rough estimate: about {bond:.0%} of a full bond). The part is weaker along its "
                                      f"layers here" + (", where it carries load." if carrying else ".") + why + guess})
    for f in out:
        f.setdefault("impact", RULE_IMPACT.get(f["rule"], "info"))
    return [summarize_finding(f, p) for f in out]


# Heat inside a closed box: everything the electronics use ends up as heat that has to leave through
# the walls. Overall heat transfer of an enclosure wall to still room air, both sides: about 4.6 W/m²K
# (Elliott Electric's sealed-enclosure chart: 7.1 W/ft² gives a 16.7 °C rise; Firgelli: 3–6 W/m²K in
# still air); with a fan blowing through, 20 W/m²K (Firgelli: 20–40 forced). Printed plastic conducts
# about 0.2 W/m·K (PLA 0.13 … PETG 0.2): a 2 mm wall adds only ~7 %.
BOX_HEAT_U = {"closed": 4.6, "fan": 20.0}


PLASTIC_K = 0.2


# Walls next to the electronics get warmer than the air: part of the heat reaches them straight away
# (radiation, the warm air rising off a chip). Rough: a third of it, spread like light from a point
# (cos / 4πd²), the wall passing it on to the air on both sides; the plastic spreads a hot spot over
# about WALL_SPREAD_MM. Good enough to see which wall gets warmest, not for exact numbers.
RADIANT_SHARE = 0.3


WALL_SPREAD_MM = 15.0


SOURCE_LIFT_MM = 10.0  # a board sits on standoffs: its heat starts this far off the face it's placed on


def _outer_area_mm2(mesh: Mesh) -> float:
    """Outside surface of the part as a prism around its outline (hull × height + top and bottom),
    along whichever axis fits tightest: exact for boxes and cylinders, and it ignores the inside walls."""
    import shapely

    pts = mesh.triangles.reshape(-1, 3)
    best = np.inf
    for axis in range(3):
        a, b = [i for i in range(3) if i != axis]
        hull = shapely.MultiPoint(pts[:, [a, b]]).convex_hull
        height = float(np.ptp(pts[:, axis]))
        best = min(best, hull.length * height + 2 * hull.area)
    return float(best)


COLD_C = 0.0  # below this, printed plastic gets brittle: knocks break it more easily


def heat_curve(profile: dict | None = None, use_temp_c: float | None = None) -> dict:
    """How much of its strength the material keeps from a winter's day to past its heat limit
    (service.heat_factor), for the "strength vs. temperature" chart; ``use_temp_c`` (default the
    profile's) is marked on it. No part needed: it depends only on the material."""
    from dataclasses import replace

    from ..service import ROOM_C, heat_factor, heat_limit

    p = Profile.from_dict(profile)
    t = p.use_temp_c if use_temp_c is None else float(use_temp_c)
    limit = heat_limit(p)
    top = max(limit + 20.0, 100.0)
    temps = np.unique(np.r_[np.arange(-20.0, top + 0.1, 5.0), ROOM_C, limit, p.material["softening_c"]])
    shares = [heat_factor(replace(p, use_temp_c=float(c))) for c in temps]
    at = t if t is not None else ROOM_C
    k = heat_factor(replace(p, use_temp_c=at))
    name = p.material["name"]
    if at < COLD_C:
        story = (f"At {at:.0f} °C {name} is about as strong as at room temperature, but more brittle: a knock or a drop breaks it more easily.")
    elif k > 0.999:
        story = f"At {at:.0f} °C {name} keeps its full strength."
    elif at >= limit:
        story = f"At {at:.0f} °C {name} is past the point where it goes soft (about {limit:.0f} °C): only about {k:.0%} of its strength is left."
    else:
        story = f"At {at:.0f} °C {name} keeps about {k:.0%} of its strength. It goes soft at about {limit:.0f} °C."
    return {"material": name, "temps_c": temps.round(1).tolist(), "share_pct": [round(x * 100, 1) for x in shares],
            "room_c": ROOM_C, "softening_c": float(p.material["softening_c"]), "soft_c": float(limit), "cold_c": COLD_C,
            "use_temp_c": at, "share_at_pct": round(k * 100, 1), "explanation": story}


def _heat_source(mesh: Mesh, point=None, face: int | None = None) -> np.ndarray:
    """Where the electronics sit (part coordinates): ``SOURCE_LIFT_MM`` off the clicked ``point`` on
    ``face`` (into the box: a face of the inside wall points into the room inside), or the middle of the part."""
    tri = mesh.triangles
    if point is None:
        return (tri.reshape(-1, 3).min(axis=0) + tri.reshape(-1, 3).max(axis=0)) / 2
    p = np.asarray(point, float)
    if face is None:
        return p
    n = np.cross(tri[face, 1] - tri[face, 0], tri[face, 2] - tri[face, 0])
    return p + SOURCE_LIFT_MM * n / max(np.linalg.norm(n), 1e-12)


def _wall_temps(tris: np.ndarray, source: np.ndarray, power_w: float, u: float, air_c: float, corners: bool = False) -> np.ndarray:
    """Temperature (°C) of every triangle (or of its three ``corners``): the air inside, plus what
    reaches that wall straight from the source (RADIANT_SHARE). The wall is thin, so its outside face
    gets as warm as its inside one."""
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    c = tris.reshape(-1, 3) if corners else tris.mean(axis=1)
    if corners:
        n = np.repeat(n, 3, axis=0)
    d = source - c
    dist = np.linalg.norm(d, axis=1)
    cos = np.abs(np.einsum("ij,ij->i", n, d)) / np.maximum(dist, 1e-9)
    r2 = (dist**2 + WALL_SPREAD_MM**2) * 1e-6
    q = RADIANT_SHARE * float(power_w) * cos / (4 * np.pi * r2)  # W/m² reaching the wall
    return air_c + q / (2 * u)  # it leaves to the air on both sides


def box_heat(power_w: float, ambient_c: float = 25.0, cooling: str = "closed", profile: dict | None = None,
             point=None, face: int | None = None) -> dict:
    """Air temperature inside the part used as a box for electronics that use ``power_w`` watts, with
    the room at ``ambient_c``: the heat leaves through the outside surface (``BOX_HEAT_U`` for a
    closed box or one with a fan) and the printed wall. Compared with where the plastic goes soft.
    Vents help too, but how much depends on their size: between the two.
    ``point`` / ``face``: where the electronics sit (a click inside the box; default the middle).
    ``face_temp_c`` (float32 per face) shows which walls get warmest (``_wall_temps``); for the 3D view
    also on a finer copy of the surface (``field_triangles``, ``vertex_temp_c`` at every corner)."""
    from ..service import heat_limit
    from ..strength import refine

    if cooling not in BOX_HEAT_U:
        raise ValueError(f"cooling must be one of {', '.join(BOX_HEAT_U)}")
    s = _S._require()
    p = Profile.from_dict(profile)
    area = _outer_area_mm2(s.mesh)
    wall_m = goal_plan(p)["walls"] * p.line_width / 1000
    u = 1 / (1 / BOX_HEAT_U[cooling] + wall_m / PLASTIC_K)
    rise = float(power_w) / (u * area * 1e-6)
    inside = float(ambient_c) + rise
    # Same limits as "Where is it used?": soft past heat_limit, creeping from the softening point.
    limit = heat_limit(p)
    soft = min(float(p.material["softening_c"]), limit - 10)
    verdict = "too hot" if inside >= limit else "warm" if inside >= soft else "fine"
    name = p.material["name"]
    how = {"closed": "closed", "fan": "with a fan blowing through"}[cooling]
    text = (f"{power_w:g} W of heat inside this box ({how}, {area / 100:.0f} cm² outside) warms the air inside about {rise:.0f} °C above the room: "
            f"about {inside:.0f} °C at {ambient_c:g} °C room. ")
    text += {"fine": f"{name} stays below where it starts to soften ({soft:.0f} °C).",
             "warm": f"{name} starts to soften from about {soft:.0f} °C: screw bosses and clips may slowly give way. Add vents or a fan, or print it in PETG or ASA.",
             "too hot": f"{name} goes soft at about {limit:.0f} °C: the box will sag and clips loosen. Add a fan or vents, or print it in ASA, ABS or PC."}[verdict]
    if inside >= 70:  # commercial-grade chips are rated 0–70 °C (industrial 85 °C), whatever the box is printed in
        text += " That's too hot for the electronics too: this needs a fan or a much bigger box."
    source = _heat_source(s.mesh, point, face)
    walls = _wall_temps(s.mesh.triangles, source, power_w, u, inside)
    fine = refine(s.mesh.triangles, max(float(np.ptp(s.mesh.triangles.reshape(-1, 3), axis=0).max()) / 40, 1.0))
    hot = int(np.argmax(walls))
    wall_c = float(walls[hot])
    if wall_c >= max(soft, inside + 5):
        text += (f" The wall closest to the electronics gets warmer still, about {wall_c:.0f} °C (red on the part)"
                 + (": it can sag there first. Move the electronics away from the wall or add a vent there." if wall_c >= limit
                    else ": it may slowly give way there first." if wall_c >= soft else "."))
    return {"power_w": float(power_w), "ambient_c": float(ambient_c), "cooling": cooling, "area_cm2": round(area / 100, 1),
            "rise_c": round(rise, 1), "inside_c": round(inside, 1), "softening_c": soft, "heat_limit_c": limit,
            "verdict": verdict, "explanation": text, "source": source.tolist(), "wall_max_c": round(wall_c, 1),
            "hottest_point": s.mesh.triangles[hot].mean(axis=0).tolist(), "face_temp_c": walls.astype(np.float32).tobytes(),
            "field_triangles": fine.astype(np.float32).tobytes(),
            "vertex_temp_c": _wall_temps(fine, source, power_w, u, inside, corners=True).astype(np.float32).tobytes()}


def _gcode_to_oriented(rotation: np.ndarray):
    """Printer coordinates of the loaded G-code -> the oriented part (``orient``: centred, on z = 0)."""
    s = _S._require()
    g = s.gcode
    turned = s.mesh.triangles.reshape(-1, 3) @ rotation.T
    lo, hi = turned.min(axis=0), turned.max(axis=0)
    base = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
    return lambda x: x @ g["q"].T + g["shift"] - base


def _spot_heat(rotation: np.ndarray, p: Profile) -> dict:
    from ..gcode import spot_heat

    return spot_heat(_S._require().gcode["toolpath"], _gcode_to_oriented(rotation), p)


def _spot_findings(spots: dict, p: Profile, findings: list[dict]) -> list[dict]:
    """Spots that get the next layer while still soft, line by line from the G-code: only when the
    layer heat check (whole layers) didn't already say so for that height."""
    C = load_data("thresholds")["cooling"]
    soft = float(p.material["softening_c"])
    hot = (spots["temp_c"] >= soft + C["soft_margin_c"]) & (spots["z"] > C["above_bed_mm"])
    if not hot.any():
        return []
    zs = np.unique(spots["z"][hot])
    if zs[-1] - zs[0] < C["min_run_mm"]:
        return []
    known = [f["bbox"] for f in findings if f["rule"] == "small_layers_cooling" and f.get("bbox")]
    i = int(np.argmax(np.where(hot, spots["temp_c"], -np.inf)))
    if any(b[0][2] - 0.5 <= spots["z"][i] <= b[1][2] + 0.5 for b in known):
        return []
    near = hot & (np.linalg.norm(spots["xy"] - spots["xy"][i], axis=1) <= 10)
    xy, z = spots["xy"][near], spots["z"][near]
    half = spots["cell_mm"] * 1.5
    t, wait = float(spots["temp_c"][i]), float(spots["seconds"][i])
    sev = "warn" if t >= soft + C["hot_margin_c"] else "info"
    return [{"rule": "small_layers_cooling", "severity": sev, "bbox": [[*(xy.min(axis=0) - half).tolist(), float(z.min())], [*(xy.max(axis=0) + half).tolist(), float(z.max())]],
             "metrics": {"temp_c": round(t, 1), "softening_c": soft, "seconds": round(wait, 1)},
             "detail": f"Line by line from your G-code: a spot at {z.min():.0f}–{z.max():.0f} mm gets the next layer after only {wait:.0f} s and is "
                       f"still about {t:.0f} °C ({p.material['name']} softens from about {soft:.0f} °C), so it can droop or blob. "
                       f"A longer minimum layer time, or a second copy on the plate, gives it time to cool"}]


HEAT_COLOUR_C = (10.0, 55.0)  # °C above softening: no colour … full red


def _face_temp(oriented: Mesh, heat: dict, spots: dict | None = None) -> bytes:
    """Heat build-up per triangle (uint8, 255 = far above softening while printing), for the viewer.
    With ``spots`` (line by line from the G-code) each triangle takes the warmest spot within a cell of it
    on its layer; triangles without one (the top, the first layer) keep the whole layer's."""
    z = oriented.triangles[..., 2].mean(axis=1)
    temp = np.interp(z, heat["z"], heat["temp_c"])
    if spots is not None and len(spots["z"]):
        c = oriented.triangles.mean(axis=1)
        zs = np.unique(spots["z"])
        level = np.clip(np.searchsorted(zs, c[:, 2]), 0, len(zs) - 1)
        cell = spots["cell_mm"]
        key = lambda lv, xy: (lv * 100_003 + np.floor(xy[:, 0] / cell).astype(np.int64)) * 100_003 + np.floor(xy[:, 1] / cell).astype(np.int64)
        sk = key(np.searchsorted(zs, spots["z"]), spots["xy"])
        order = np.argsort(sk)
        sk, st = sk[order], spots["temp_c"][order]
        best = np.full(len(c), -np.inf)
        for dx in (-cell, 0, cell):
            for dy in (-cell, 0, cell):
                k = key(level, c[:, :2] + [dx, dy])
                j = np.clip(np.searchsorted(sk, k), 0, len(sk) - 1)
                best = np.where(sk[j] == k, np.maximum(best, st[j]), best)
        temp = np.where(np.isfinite(best), best, temp)
    over = temp - heat["softening_c"]
    lo, hi = HEAT_COLOUR_C
    return (np.clip((over - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8).tobytes()
