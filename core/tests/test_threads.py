"""Threads modelled into the part (T-323)."""

import numpy as np
import pytest
from shapely.geometry import box

from fdmcore import api
from fdmcore.additions import extrude
from fdmcore.features import find_threads
from fdmcore.mesh import Mesh, to_binary_stl


def thread_mesh(major=6.0, pitch=1.0, length=10.0, internal=False, n_theta=96, per_pitch=24) -> Mesh:
    """A closed threaded rod along z (60° flanks, flat crests and roots); ``internal``: a block with it as its hole."""
    depth = 0.6 * pitch
    th = np.linspace(0, 2 * np.pi, n_theta, endpoint=False)
    zs = np.linspace(0, length, int(length / pitch * per_pitch) + 1)
    T, Z = np.meshgrid(th, zs)
    s = ((Z - T / (2 * np.pi) * pitch) / pitch) % 1.0
    R = major / 2 - depth + depth * np.interp(s, [0, 0.15, 0.5, 0.65, 1.0], [0, 0, 1, 1, 0])
    P = np.stack([R * np.cos(T), R * np.sin(T), Z], axis=-1)
    nz, nt = P.shape[:2]
    i, j = np.meshgrid(np.arange(nz - 1), np.arange(nt), indexing="ij")
    a, b, c, d = P[i, j], P[i, (j + 1) % nt], P[i + 1, (j + 1) % nt], P[i + 1, j]
    side = np.concatenate([np.stack([a, b, c], 2).reshape(-1, 3, 3), np.stack([a, c, d], 2).reshape(-1, 3, 3)])
    caps = []
    for k, flip in ((0, True), (nz - 1, False)):
        ring = P[k]
        ctr = np.broadcast_to([0, 0, zs[k]], ring.shape)
        tri = np.stack([ctr, ring, np.roll(ring, -1, axis=0)], 1)
        caps.append(tri[:, ::-1] if flip else tri)
    t = np.concatenate([side, *caps])
    if internal:
        t = np.concatenate([extrude(box(-8, -8, 8, 8), -1, length + 1), t[:, ::-1]])
    return Mesh(t)


@pytest.mark.parametrize("kw, size, pitch, external", [({}, "M6", 1.0, True), ({"major": 3.0, "pitch": 0.5}, "M3", 0.5, True),
                                                         ({"major": 10.0, "pitch": 1.5, "internal": True}, "M10", 1.5, False)])
def test_finds_bolts_and_threaded_holes(kw, size, pitch, external):
    (t,) = find_threads(thread_mesh(**kw))
    assert (t["size"], t["pitch_mm"], t["external"], t["left_hand"]) == (size, pitch, external, False)


def test_plain_round_parts_have_no_thread():
    rod = extrude(box(0, 0, 1, 1).centroid.buffer(3, 64), 0, 10)
    assert find_threads(Mesh(rod)) == []


def test_finding_depends_on_orientation_and_size():
    api.load(to_binary_stl(thread_mesh(major=8.0, pitch=1.25)), "m8")
    upright = next(f for f in api.check([0, 0, 1], {"material": "pla"})["findings"] if f["rule"] == "modelled_thread")
    assert upright["metrics"]["size"] == "M8" and upright["metrics"]["upright"]
    assert upright["severity"] == "info"  # 1.25 mm pitch on 0.2 mm layers: 6 layers per turn, fine
    lying = next(f for f in api.check([1, 0, 0], {"material": "pla"})["findings"] if f["rule"] == "modelled_thread")
    assert "on its side" in lying["detail"] and not lying["metrics"]["upright"]
    api.load(to_binary_stl(thread_mesh(major=6.0, pitch=0.75)), "m6_fine")  # 0.75 / 0.2: under 4 layers per turn
    fine = next(f for f in api.check([0, 0, 1], {"material": "pla"})["findings"] if f["rule"] == "modelled_thread")
    assert fine["severity"] == "warn" and "layers per turn" in fine["detail"] and fine["metrics"]["size"] == "M6"
    api.load(to_binary_stl(thread_mesh(major=3.0, pitch=0.5)), "m3")
    small = next(f for f in api.check([0, 0, 1], {"material": "pla"})["findings"] if f["rule"] == "modelled_thread")
    assert small["severity"] == "warn" and "smaller than M5" in small["detail"]
