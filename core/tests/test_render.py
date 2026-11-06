import struct
import zlib
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api, render

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


def decode(png: bytes) -> np.ndarray:
    """RGB pixels of a PNG written by render.png (one IDAT, no filter)."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    n = struct.unpack(">I", png[33:37])[0]
    raw = zlib.decompress(png[41:41 + n])
    return np.frombuffer(raw, np.uint8).reshape(h, 1 + w * 3)[:, 1:].reshape(h, w, 3)


def test_cube_fills_the_middle():
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * 10
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    img = render.draw(v[f], np.tile(render.rgb("#ff0000"), (12, 1)), 80, 60)
    assert img.shape == (60, 80, 3)
    assert img[30, 40, 0] > 100 and img[30, 40, 1] < 40  # the cube, shaded red
    assert tuple(img[1, 1]) == tuple((render.rgb(render.BACKGROUND) * 255 + 0.5).astype(int))


def test_every_view():
    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket.stl")
    up = [0.0, 0.0, 1.0]
    regs = sorted((r for r in api.regions()["regions"] if r["kind"] == "flat"), key=lambda r: -r["area_mm2"])
    loads = [{"region": regs[0]["id"], "kind": "fixed"}, {"region": next(r for r in regs[1:] if abs(r["normal"][2]) < 0.5)["id"], "kind": "force", "kg": 3}]
    args = {"stress": {"loads": loads}, "compare": {"other": (SAMPLES / "l_bracket.stl").read_bytes()}}
    for view in render.VIEWS:
        img = decode(render.picture(view, up, {"material": "abs"}, width=160, height=120, **args.get(view, {})))
        assert img.shape == (120, 160, 3) and len(np.unique(img.reshape(-1, 3), axis=0)) > 5, view
    with pytest.raises(ValueError, match="needs loads"):
        render.picture("stress", up)
