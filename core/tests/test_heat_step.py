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
    return api.optimize_cooling([0, 0, 1], detail=True)


def test_detail_has_every_spot_before_and_after(detail):
    """The same spots in both runs; the plan's run has none of the 40 too hot ones left, the lines it slowed or sped
    up are marked, and everything sits on the oriented part (the frame the stage turns the part into)."""
    sp = detail["spots"]
    n = len(sp["points"]) // 12
    assert n > 100 and all(len(sp[k][i]) == n * 4 for k in ("cover_c", "cover_s", "bond") for i in (0, 1))
    before, after = (np.frombuffer(w, np.uint8) for w in sp["window"])
    assert (before == 1).sum() == detail["before"]["too_hot"] == 40 and (after == 1).sum() == detail["gcode"]["too_hot"] == 0
    tl = detail["timeline"]
    layers = len(tl["z"]) // 4
    assert layers == 50 and all(len(tl[k][i]) == layers * 4 for k in ("seconds", "fan", "cover_c") for i in (0, 1))
    secs = [np.frombuffer(s, np.float32) for s in tl["seconds"]]
    assert np.nansum(secs[1]) > np.nansum(secs[0])  # the plan waits where the pin is too hot
    factor = np.frombuffer(detail["lines"]["factor"], np.float32)
    assert len(factor) == detail["lines"]["all"] and (np.abs(factor - 1) > 0.01).any()
    pts = np.frombuffer(sp["points"], np.float32).reshape(-1, 3)
    seg = np.frombuffer(detail["lines"]["segments"], np.float32).reshape(-1, 6)
    assert pts[:, 2].min() >= 0 and abs(pts[:, :2].mean(axis=0)).max() < 3 and abs(seg[:, 3:5].mean(axis=0)).max() < 3
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
