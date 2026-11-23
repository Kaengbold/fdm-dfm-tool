// Tuned settings (T-571, plan §15.1), opened from Done's card. From the G-code the Simulation step sliced (or the
// user's own), the heat model's cooling plan (api.optimize_cooling, T-480) says which heights stay too hot; the part of
// it a slicer can take from a project file is slower heights and a cooling tower (fan changes per height can't go in a
// 3MF). Ticked, they go into the 3MF as height ranges (export's ``cooling``), kept on the option (``option.cooling``),
// which is where later plans (T-479, T-481) plug in too. The G-code download stays on Done.
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { ToggleChip } from "../ui/chip.js";
import { registerStep } from "../shell.js";
import { Cancelled } from "../engine.js";
import { t } from "../i18n.js";
import { holdToEngine, loadToEngine } from "./args.js";
import { simProfile } from "./simulate.js";

let plan = null; // {key, r}
let busy = false;
let error = null;

const keyOf = (store) => JSON.stringify(store.deps("model", "option"));
const SPEED_WORD = { outer_wall: "tuned.part.outer", inner_wall: "tuned.part.inner", sparse_infill: "tuned.part.infill",
  internal_solid_infill: "tuned.part.solid", top_surface: "tuned.part.top" };

/** One height range as a line: "12.4–15.0 mm: outer walls 40 mm/s, top 30 mm/s". */
export function rangeText(r, word = (k) => k) {
  const speeds = Object.entries(r.speeds ?? {}).map(([k, v]) => `${word(k)} ${Math.round(v)} mm/s`).join(", ");
  return `${r.z0.toFixed(1)}–${r.z1.toFixed(1)} mm: ${speeds}`;
}

async function work(store, services) {
  const s = store.get();
  const o = s.option;
  busy = true;
  error = null;
  services.rerender();
  try {
    const loads = s.noLoads || !s.loads.length ? null : [...s.holds.map(holdToEngine), ...s.loads.map(loadToEngine)];
    const r = await services.engine.call("optimize_cooling", { up: o.up, profile: simProfile(s, o), loads });
    plan = { key: keyOf(store), r };
  } catch (e) {
    if (!(e instanceof Cancelled)) error = e.message;
  } finally {
    busy = false;
    services.rerender();
  }
}

registerStep("tuned", {
  render({ body, store, go, services }) {
    const s = store.get();
    const o = s.option;
    if (!o) {
      body.append(h("p", { class: "ui-hint" }, t("sim.noOption")), Button({ label: t("sim.toOptions"), onClick: () => go("options") }));
      return;
    }
    const sim = store.isStale("simulate") ? null : store.result("simulate");
    if (!sim?.detailed) {
      body.append(h("p", { class: "ui-hint" }, t("tuned.needGcode")), Button({ label: t("tuned.toSim"), variant: "primary", onClick: () => go("sim") }));
      return;
    }
    body.append(h("p", { class: "ui-hint" }, t("tuned.intro")));
    if (busy) body.append(h("div", { class: "sim-running", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {}, t("tuned.working")),
      Button({ label: t("ui.cancel"), variant: "ghost", onClick: () => services.engine.cancel() })));
    if (error) body.append(h("p", { class: "drop-error", role: "alert" }, error));
    const r = plan?.key === keyOf(store) ? plan.r : null;
    if (!r && !busy) body.append(Button({ label: t("tuned.work"), variant: "primary", onClick: () => work(store, services) }));
    if (!r) return;
    body.append(h("div", { class: "tuned-summary" }, r.summary.map((line) => h("p", {}, line))));
    const cooling = r.slicer.cooling;
    const ranges = cooling.ranges ?? [];
    if (!ranges.length && !cooling.tower) {
      body.append(h("p", { class: "prob-clear" }, h("b", {}, t("tuned.nothing"))));
      return;
    }
    body.append(h("ul", { class: "tuned-ranges" }, ranges.map((x) => h("li", { class: "num" }, rangeText(x, (k) => t(SPEED_WORD[k] ?? k)))),
      cooling.tower ? h("li", {}, t("tuned.tower", { height: cooling.tower.height.toFixed(0), d: cooling.tower.diameter.toFixed(0) })) : null));
    const on = JSON.stringify(o.cooling ?? null) === JSON.stringify(cooling);
    body.append(ToggleChip({ label: t("tuned.into3mf"), pressed: on, onChange: (v) => store.set({ option: { ...o, cooling: v ? cooling : null } }) }),
      h("p", { class: "ui-hint" }, t("tuned.minutes", { before: r.before.print_min.toFixed(0), after: r.slicer.print_min.toFixed(0) })));
  },
});
