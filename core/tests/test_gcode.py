import numpy as np
import pytest

from fdmcore import api, gcode
from fdmcore.mesh import Mesh, to_binary_stl

LH, W = 0.2, 0.45
FIL_AREA = np.pi * 1.75**2 / 4


def box_mesh(sx, sy, sz) -> Mesh:
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * [sx, sy, sz]
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    return Mesh(v[np.array(f)])


def slice_box(sx, sy, sz, walls=2, infill=0.2, along=None, at=(100.0, 100.0)) -> str:
    """A tiny slicer for a box (OrcaSlicer-style comments): ``walls`` loops, then infill lines
    alternating along x / y each layer (``along`` "x" or "y": all layers the same way)."""
    out = ["; generated for tests", "M83", "G90"]
    x0, y0 = at[0] - sx / 2, at[1] - sy / 2

    def line(ax, ay, bx, by, z):
        e = np.hypot(bx - ax, by - ay) * W * LH / FIL_AREA
        out.append(f"G0 X{ax:.3f} Y{ay:.3f} Z{z:.3f}")
        out.append(f"G1 X{bx:.3f} Y{by:.3f} E{e:.5f} F3000")

    for k in range(int(round(sz / LH))):
        z = (k + 1) * LH
        out += [";LAYER_CHANGE", f";Z:{z:.3f}"]
        for i in range(walls):
            d = (i + 0.5) * W
            out.append(f"; FEATURE: {'Outer' if i == 0 else 'Inner'} wall")
            c = [(x0 + d, y0 + d), (x0 + sx - d, y0 + d), (x0 + sx - d, y0 + sy - d), (x0 + d, y0 + sy - d), (x0 + d, y0 + d)]
            for a, b in zip(c, c[1:]):
                line(*a, *b, z)
        if infill <= 0:
            continue
        out.append("; FEATURE: Sparse infill" if infill < 1 else "; FEATURE: Internal solid infill")
        d = walls * W
        way = along or ("x" if k % 2 == 0 else "y")
        span = (sy if way == "x" else sx) - 2 * d
        for t in np.arange(W / 2, span, W / infill):
            if way == "x":
                line(x0 + d, y0 + d + t, x0 + sx - d, y0 + d + t, z)
            else:
                line(x0 + d + t, y0 + d, x0 + d + t, y0 + sy - d, z)
    return "\n".join(out)


def test_parse_kinds_layers_and_plastic():
    tp = gcode.parse(slice_box(20, 10, 4, walls=2, infill=0.2))
    assert len(tp.layer_z) == 20
    assert np.allclose(tp.layer_height, LH)
    walls = tp.volume[tp.kind == gcode.WALL].sum()
    loops = sum(2 * ((20 - 2 * (i + 0.5) * W) + (10 - 2 * (i + 0.5) * W)) for i in range(2))
    assert walls == pytest.approx(loops * W * LH * 20, rel=1e-3)
    assert tp.volume[tp.kind == gcode.SPARSE].sum() > 0
    assert set(np.unique(tp.kind)) == {gcode.WALL, gcode.SPARSE}


def test_feature_names_of_all_slicers():
    assert gcode.kind_of("Outer wall") == gcode.kind_of("External perimeter") == gcode.kind_of("WALL-OUTER") == gcode.WALL
    assert gcode.kind_of("Overhang perimeter") == gcode.WALL
    assert gcode.kind_of("Sparse infill") == gcode.kind_of("Internal infill") == gcode.kind_of("FILL") == gcode.SPARSE
    assert gcode.kind_of("Internal solid infill") == gcode.kind_of("Top solid infill") == gcode.kind_of("Bridge") == gcode.SOLID
    assert gcode.kind_of("Support interface") == gcode.kind_of("Support material") == gcode.SUPPORT
    assert gcode.kind_of("Skirt/Brim") == gcode.kind_of("Prime tower") == gcode.OTHER


def test_arcs_and_absolute_extrusion():
    r = 5.0
    e = 2 * np.pi * r * W * LH / FIL_AREA
    text = "\n".join(["M82", "G92 E0", "; FEATURE: Outer wall", f"G0 X{10 + r} Y10 Z0.2", f"G2 X{10 + r} Y10 I{-r} J0 E{e:.5f} F1200"])
    tp = gcode.parse(text)
    assert tp.volume.sum() == pytest.approx(2 * np.pi * r * W * LH, rel=1e-6)
    assert np.linalg.norm(tp.p1[:, :2] - [10, 10], axis=1) == pytest.approx(r)
    assert tp.t1.max() - tp.t0.min() == pytest.approx(2 * np.pi * r / 20, rel=0.02)  # 1200 mm/min


def test_wrong_part_is_refused():
    api.load(to_binary_stl(box_mesh(20, 10, 4)), "box")
    with pytest.raises(ValueError, match="same part"):
        api.load_gcode(slice_box(30, 10, 4), [0, 0, 1])


def test_finds_part_turned_on_the_bed():
    api.load(to_binary_stl(box_mesh(20, 10, 4)), "box")
    turned = slice_box(10, 20, 4, at=(80, 120))  # the slicer turned it a quarter
    assert api.load_gcode(turned, [0, 0, 1])["turned_deg"] in (90, 270)


def test_detailed_strength_close_to_simple_for_the_same_print():
    api.load(to_binary_stl(box_mesh(40, 10, 6)), "bar")
    loads = [{"point": [0, 5, 3], "radius": 4, "kind": "fixed"}, {"point": [40, 5, 3], "radius": 4, "kind": "force", "kg": 10, "direction": "down"}]
    api.load_gcode(slice_box(40, 10, 6, walls=2, infill=0.2), [0, 0, 1])
    simple = api.strength([0, 0, 1], loads, walls=2, infill=0.2, top=0, bottom=0)["safety_factor"]
    detailed = api.strength([0, 0, 1], loads, detailed=True)
    assert detailed["detailed"]["missed_pct"] < 1
    assert detailed["safety_factor"] == pytest.approx(simple, rel=0.35)


def test_lines_pulled_apart_sideways_are_weak():
    """A solid plate pulled along y: lines along y carry it, lines along x get pulled apart."""
    api.load(to_binary_stl(box_mesh(10, 30, 4)), "plate")
    loads = [{"point": [5, 0, 2], "radius": 5, "kind": "fixed"}, {"point": [5, 30, 2], "radius": 5, "kind": "force", "kg": 50, "direction": "pull"}]
    out = {}
    for way in ("x", "y"):
        api.load_gcode(slice_box(10, 30, 4, walls=0, infill=1.0, along=way), [0, 0, 1])
        out[way] = api.strength([0, 0, 1], loads, detailed=True)
    assert out["x"]["mode"] == "lines"
    assert out["x"]["safety_factor"] < 0.8 * out["y"]["safety_factor"]
    assert "pulled apart" in out["x"]["explanation"]


def test_detailed_needs_the_same_orientation():
    api.load(to_binary_stl(box_mesh(20, 10, 4)), "box")
    api.load_gcode(slice_box(20, 10, 4), [0, 0, 1])
    loads = [{"point": [0, 5, 2], "radius": 3, "kind": "fixed"}, {"point": [20, 5, 2], "radius": 3, "kind": "force", "kg": 1}]
    with pytest.raises(ValueError, match="another orientation"):
        api.strength([1, 0, 0], loads, detailed=True)


def test_heat_uses_the_real_layer_times():
    """A thin pin printed alone gets a few seconds per layer and stays soft; with long layers
    (e.g. other parts on the plate printed in between) the G-code says it cools in time."""
    from shapely.geometry import Point

    from fdmcore.additions import extrude
    from fdmcore.checks import run_checks
    from fdmcore.profile import Profile

    prof = Profile.from_dict({"material": "pla", "printer": "bambu_p1s"})
    pin = Mesh(extrude(Point(0, 0).buffer(1.5, 32), 0, 20))
    z = np.arange(1, 101) * LH
    area = np.pi * 1.5**2

    def soft(seconds):
        sliced = {"z": z, "layer_height": np.full(100, LH), "volume": np.full(100, area * LH), "seconds": np.full(100, seconds)}
        return [f for f in run_checks(pin, prof, sliced=sliced)["findings"] if f["rule"] == "small_layers_cooling"]

    assert soft(2.0)
    assert not soft(40.0)


def test_layer_times_from_gcode():
    tp = gcode.parse(slice_box(20, 10, 1, walls=2, infill=0.2))
    lt = gcode.layer_times(tp)
    assert len(lt["z"]) == 5 and np.allclose(lt["layer_height"], LH)
    assert lt["seconds"].sum() == pytest.approx(tp.t1.max() - tp.t0.min(), rel=1e-6)
    assert lt["volume"].sum() == pytest.approx(tp.volume.sum())
