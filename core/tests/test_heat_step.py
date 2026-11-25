"""The simulation step's heat views (T-478): what api.optimize_cooling(detail) sends, and the pure logic that turns it
into words (web/steps/heatlogic.js, run under Node). The page itself is driven in the browser test
(tools/e2e_smoke.py, check_new_ui_heat)."""

from __future__ import annotations

import gzip
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api
from fdmcore.gcode import CAUSE_NAMES
from fdmcore.importance import REASONS

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "gcode"
NODE = shutil.which("node")


@pytest.fixture(scope="module")
def detail():
    api.load((FIXTURES / "part.stl").read_bytes(), "fixture")
    api.load_gcode(gzip.decompress((FIXTURES / "orca.gcode.gz").read_bytes()), [0, 0, 1])
    return api.optimize_cooling([0, 0, 1], detail=True, ranges=True)


def test_detail_has_every_spot_before_and_after(detail):
    """The same spots in both runs; the plan's run has none of the 40 too hot ones left, the lines it slowed or sped
    up are marked, and everything sits on the oriented part (the frame the stage turns the part into)."""
    sp = detail["spots"]
    n = len(sp["points"]) // 12
    assert n > 100 and all(len(sp[k][i]) == n * 4 for k in ("cover_c", "cover_s", "bond") for i in (0, 1))
    before, after = (np.frombuffer(w, np.uint8) for w in sp["window"])
    assert (before == 1).sum() == detail["before"]["too_hot"] == 40 and (after == 1).sum() == detail["gcode"]["too_hot"] == 0
    baseline = detail["before"]
    low, high = baseline["range"]["cover_c"]["median"]
    assert low < baseline["cover_c"]["median"] < high
    low, high = baseline["range"]["bond_median"]
    assert low < baseline["bond_median"] < high
    tl = detail["timeline"]
    layers = len(tl["z"]) // 4
    assert layers == 50 and all(len(tl[k][i]) == layers * 4 for k in ("seconds", "fan", "cover_c") for i in (0, 1))
    secs = [np.frombuffer(s, np.float32) for s in tl["seconds"]]
    assert np.nansum(secs[1]) > np.nansum(secs[0])  # the plan waits where the pin is too hot
    factor = np.frombuffer(detail["lines"]["factor"], np.float32)
    assert len(factor) >= detail["lines"]["all"] and (np.abs(factor - 1) > 0.01).any()
    pts = np.frombuffer(sp["points"], np.float32).reshape(-1, 3)
    seg = np.frombuffer(detail["lines"]["segments"], np.float32).reshape(-1, 6)
    assert pts[:, 2].min() >= 0 and abs(pts[:, :2].mean(axis=0)).max() < 3 and abs(seg[:, 3:5].mean(axis=0)).max() < 3
    # The printed paths in short pieces, each in the heat spot it lies in (T-587): as wide and high as its line.
    spot = np.frombuffer(detail["lines"]["spot"], np.int32)
    width, height = (np.frombuffer(detail["lines"][k], np.float32) for k in ("width", "height"))
    assert len(spot) == len(seg) == len(width) == len(height) == len(factor) and (spot >= 0).mean() > 0.98
    assert 0.3 < np.median(width) < 0.6 and np.allclose(np.median(height), 0.2)
    near = spot >= 0
    gap = np.linalg.norm((seg[near, :2] + seg[near, 3:5]) / 2 - pts[spot[near], :2], axis=1)
    assert np.percentile(gap, 99) <= 1.6 * sp["cell_mm"]
    assert np.allclose(detail["rotation"], np.eye(3)) and detail["slowdown_min"]["layer_time"] > 0


def test_detail_is_left_out_unless_asked():
    api.load((FIXTURES / "part.stl").read_bytes(), "fixture")
    api.load_gcode(gzip.decompress((FIXTURES / "orca.gcode.gz").read_bytes()), [0, 0, 1])
    assert "spots" not in api.optimize_cooling([0, 0, 1])


def run_js(body: str):
    if not NODE:
        pytest.skip("node not installed")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "heat.mjs").write_text((ROOT / "web" / "steps" / "heatlogic.js").read_text(encoding="utf-8"), encoding="utf-8")
    script = f'import * as M from "{(tmp / "heat.mjs").as_uri()}";\nconst out = await (async () => {{ {body} }})();\nconsole.log(JSON.stringify(out));'
    proc = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True)
    shutil.rmtree(tmp, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# Three spots on layers 0, 4 and 8: a fine one, a too hot pin root the plan parks for, a too cold one that hardly
# matters (an accepted risk).
DATA = """const f = (a) => new Float32Array(a), d = {
  points: f([0, 0, 0.2, 1, 0, 1.0, 2, 0, 1.2]), layer: new Int32Array([0, 4, 8]), limit: f([90, 90, 90]),
  importance: f([0.5, 0.9, 0.2]), reason: new Uint8Array([1, 5, 0]), reasons: ["inside", "wall", "hole", "corner", "pin", "root", "load", "tag"],
  before: { cover: f([70, 129, 40]), coverS: f([5, 1.1, 9]), bond: f([0.8, 0.9, 0.2]), window: new Uint8Array([0, 1, 2]) },
  after: { cover: f([70, 85, 40]), coverS: f([5, 4.1, 9]), bond: f([0.8, 0.85, 0.2]), window: new Uint8Array([0, 0, 2]) } };
const plan = [{ layers: [5, 5], z_mm: [1.2, 1.2], fan: 1, slower: 1, faster: 1, park_s: 3 }];"""


def test_a_clicked_spot_says_why_it_changed():
    out = run_js(DATA + "return [M.explain(d, plan, 1), M.explain(d, plan, 2), M.explain(d, plan, 0)];")
    hot, cold, fine = ([m["key"] for m in lines] for lines in out)
    assert hot == ["heat.why.where", "heat.why.before", "heat.why.hot", "heat.why.change", "heat.why.after", "heat.why.ok", "heat.why.matters"]
    change = out[0][3]["params"]["actions"]
    assert [a["key"] for a in change] == ["heat.do.fan", "heat.do.park"] and change[1]["params"]["s"] == 3
    assert out[0][6]["params"]["what"]["key"] == "heat.reason.root"
    assert cold[-1] == "heat.why.accepted" and "heat.why.noFix" in cold  # no plan row for layers 8 and 9
    assert "heat.why.noChange" in fine


def test_summary_counts_and_the_slowest_cause():
    out = run_js(DATA + """const r = { before: { print_min: 4.7, bond_median: 0.66 }, gcode: { print_min: 5.7, bond_median: 0.67, plan },
      slowdown_min: { bridge: 0.1, layer_time: 1.4 } };
      return [M.summary(r, d), M.summary({ before: { print_min: 3 }, gcode: { print_min: 3, plan: [] } },
        { ...d, before: { ...d.before, window: new Uint8Array(3) }, after: { ...d.after, window: new Uint8Array(3) } }), M.byLayer(d.layer)];""")
    lines, nothing, layers = out
    keys = [m["key"] for m in lines]
    assert keys == ["heat.sum.slower", "heat.sum.hot", "heat.sum.cold", "heat.sum.bond", "heat.sum.accepted", "heat.sum.limit"]
    assert lines[1]["params"] == {"count": 1, "left": 0} and lines[5]["params"]["what"]["key"] == "heat.cause.layer_time"
    assert [m["key"] for m in nothing] == ["heat.sum.nothing"]
    assert layers["order"] == [0, 1, 2] and list(layers["upto"].values()) == [1, 1, 1, 1, 2, 2, 2, 2, 3]


def test_every_message_the_step_can_show_exists():
    """Keys built at run time (heat.reason.*, heat.cause.*, heat.mode.*, …) are in the English table too."""
    table = set(re.findall(r'^  "([\w.]+)":', (ROOT / "web" / "messages" / "en.js").read_text(encoding="utf-8"), re.M))
    src = (ROOT / "web" / "steps" / "heatlogic.js").read_text(encoding="utf-8") + (ROOT / "web" / "steps" / "heat.js").read_text(encoding="utf-8")
    used = set(re.findall(r'key: "(heat\.[\w.]+)"', src)) | set(re.findall(r't\("(heat\.[\w.]+)"', src))
    used |= {f"heat.reason.{r}" for r in REASONS} | {f"heat.cause.{c}" for c in CAUSE_NAMES[1:] if c != "unknown"}
    used |= {f"heat.mode.{v}" for v in ("window", "importance", "lines")} | {"sim.stage.heat"}
    used |= {f"heat.confidence.{c}" for c in ("rough", "partly", "tuned")}
    used |= {f"heat.legend.{k}" for k in ("cold", "ok", "hot", "slow", "faded", "low", "high", "slower", "faster", "same", "fan")}
    assert not used - table, used - table


@pytest.mark.parametrize("goal", ["balanced", "strong", "fast", "light", "quality"])
def test_the_heat_check_takes_the_apps_goal_names(detail, goal):
    """The app sends its own goal names (T-580); the heat check used to stop with "Unknown goal: strong" (T-584)."""
    r = api.optimize_cooling([0, 0, 1], profile={"goal": goal})
    assert r["before"]["too_hot"] + r["before"]["too_cold"] >= 0


def test_the_apps_goal_names_mean_the_engines_goals():
    from fdmcore.profile import Profile

    assert [Profile.from_dict({"goal": g}).goal for g in ("strong", "fast", "light", "quality", "speed")] == [
        "strength", "speed", "material", "balanced", "speed"]
    assert Profile.from_dict({"goal": "quality"}).looks and not Profile.from_dict({"goal": "strong"}).looks


def test_detail_has_a_replay_of_the_print(detail):
    """Every spot's temperature when each layer is done, as sliced and with the plan (T-588): blocks grow with the
    layers, the top layer is warm when it is done, and the plan's run is cooler where it fixed the hot pin."""
    rp = detail["replay"]
    layer, start = (np.frombuffer(rp[k], np.int32) for k in ("layer", "start"))
    before, after = (np.frombuffer(t, np.uint8) for t in rp["temp"])
    n = len(np.frombuffer(detail["spots"]["layer"], np.int32))
    assert list(layer) == list(range(50)) and len(before) == len(after) and len(before) - start[-1] == n
    assert (np.diff(start) > 0).all() and np.all(np.diff(np.frombuffer(rp["t_s"][0], np.float32)) > 0)
    temp = lambda q: rp["lo_c"] + q.astype(float) / 255 * (rp["hi_c"] - rp["lo_c"])
    spot_layer = np.frombuffer(detail["spots"]["layer"], np.int32)
    j = 25
    block = temp(before[start[j]:start[j + 1]])
    assert block[spot_layer[: len(block)] == 25].mean() > block[spot_layer[: len(block)] < 20].mean() + 10


def test_the_replay_tells_the_story():
    out = run_js(DATA + """const r = { replay: { layer: new Int32Array([0, 4, 8]).buffer, start: new Int32Array([0, 1, 2]).buffer,
        t_s: [new Float32Array([5, 20, 40]).buffer, new Float32Array([5, 26, 47]).buffer], fan: [new Float32Array([0, 1, 1]).buffer, new Float32Array([0, 1, 1]).buffer],
        temp: [new Uint8Array([200, 150, 120, 100, 80, 60]), new Uint8Array([200, 140, 110, 90, 70, 50])], lo_c: 20, hi_c: 220 } };
      const rp = M.decodeReplay(r);
      return [M.snapAt(rp, 6), M.tempAt(rp, 0, 1, 0), M.tempAt(rp, 0, 1, 2), M.problemLayers(d), M.foundText(M.problemLayers(d)[0]),
        M.planText(plan), M.clock(75), M.clock(3725), M.tempBand(50, 60, 90, 20, 220).band, M.tempBand(70, 60, 90, 20, 220).band,
        M.tempBand(120, 60, 90, 20, 220).band];""")
    snap, t1, missing, found, text, plan, c1, c2, *bands = out
    assert snap == 1 and abs(t1 - (20 + 150 / 255 * 200)) < 1e-3 and missing is None  # NaN: not printed yet
    assert found == [{"layer": 4, "z": 1.0, "hot": 1, "cold": 0}]  # the cold spot hardly matters
    assert text == {"key": "heat.story.foundHot", "params": {"layer": 5, "z": 1.0, "hot": 1, "cold": 0, "count": 1}}
    assert plan[0]["key"] == "heat.story.changeOne" and [a["key"] for a in plan[0]["params"]["actions"]] == ["heat.do.fan", "heat.do.park"]
    assert (c1, c2) == ("1:15", "1:02:05") and bands == ["set", "warm", "hot"]
