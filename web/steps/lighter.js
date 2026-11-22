// Make it lighter (T-571, plan §15.1), opened from Done's card; not for Quality. The goal's safety target stays
// locked: the engine (api.optimise_strength) looks for the least plastic that still reaches it at the chosen
// orientation, walls / top / bottom / infill first, and stronger zones only where the stress is (slicer modifier
// volumes) as a second way. The result sits beside the chosen option (plastic, time, strength) and is used only after
// a confirm. Then the shape: a blocky guide of where the plastic carries the load (api.topology), shown on the part and
// downloadable for the user's CAD; it is a guide, not a part to print.
import { h, download } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { SegmentedControl } from "../ui/choice.js";
import { openModal } from "../ui/overlay.js";
import { registerStep } from "../shell.js";
import { Cancelled } from "../engine.js";
import { t } from "../i18n.js";
import { holdToEngine, loadToEngine } from "./args.js";
import { simProfile } from "./simulate.js";

const f32 = (b) => new Float32Array(new Uint8Array(b).slice().buffer);
let result = null; // {key, plan}
let shape = null; // {key, keep, r}
let keep = 0.4;
let busy = null;
let error = null;

const loadsOf = (s) => (s.noLoads || !s.loads.length ? null : [...s.holds.map(holdToEngine), ...s.loads.map(loadToEngine)]);
const keyOf = (store) => JSON.stringify(store.deps("model", "option", "loads"));
const minutes = (m) => (m >= 60 ? t("done.hours", { h: Math.floor(m / 60), m: Math.round(m % 60) }) : t("done.minutes", { m: Math.round(m) }));

async function run(what, text, fn, services) {
  busy = { what, text };
  error = null;
  services.rerender();
  try {
    await fn();
  } catch (e) {
    if (!(e instanceof Cancelled)) error = e.message;
  } finally {
    busy = null;
    services.rerender();
  }
}

async function findLighter(store, services) {
  const s = store.get();
  const o = s.option;
  const plan = await services.engine.call("optimise_strength", {
    up: o.up, loads: loadsOf(s), profile: simProfile(s, o), target: o.target, zones: true, this_orientation: true,
    baseline: { walls: o.walls, infill: o.infill, top: o.top, bottom: o.bottom },
  });
  result = { key: keyOf(store), plan };
}

async function findShape(store, services) {
  const s = store.get();
  const o = s.option;
  const r = await services.engine.call("topology", { up: o.up, loads: loadsOf(s), profile: simProfile(s, o), keep });
  shape = { key: keyOf(store), keep, r };
  services.stage.showGhosts([{ mesh: f32(r.triangles), op: "add" }]);
}

/** The ways to print it lighter that reach the target: the settings, and with zones when they save plastic. */
export function lighterWays(plan, minSaving = 0.5) {
  if (!plan?.met) return [];
  const ways = [{ id: "settings", ...plan, zones: [] }];
  const z = plan.zoned;
  if (z?.met && z.zones?.length && z.grams <= plan.grams - minSaving) ways.push({ id: "zones", ...z });
  return ways;
}

function use(store, way) {
  const s = store.get();
  const o = s.option;
  const body = h("div", { class: "light-confirm" },
    h("p", {}, t("light.confirmLine", { walls: `${o.walls} → ${way.walls}`, infill: `${Math.round(o.infill * 100)} → ${Math.round(way.infill * 100)} %` })),
    h("p", {}, t("light.confirmSafety", { was: (o.safety ?? 0).toFixed(1), now: way.safety_factor.toFixed(1), target: o.target })),
    way.zones?.length ? h("p", { class: "ui-hint" }, t("light.confirmZones", { count: way.zones.length })) : null);
  openModal({ title: t("light.confirmTitle"), body, actions: [
    { label: t("ui.cancel"), variant: "ghost" },
    { label: t("light.use"), variant: "primary", onClick: () => {
      store.set({ option: { ...o, walls: way.walls, top: way.top, bottom: way.bottom, infill: way.infill,
        settings: { ...(o.settings ?? {}), ...way.settings }, zones: way.zones ?? [], safety: Math.round(way.safety_factor * 10) / 10,
        part_g: way.grams, total_g: way.grams + (o.support_g ?? 0), minutes: way.minutes, meets_target: true, lighter: way.id } });
      result = null;
    } }] });
}

registerStep("lighter", {
  render({ body, store, go, services }) {
    const s = store.get();
    const o = s.option;
    if (!o) {
      body.append(h("p", { class: "ui-hint" }, t("sim.noOption")), Button({ label: t("sim.toOptions"), onClick: () => go("options") }));
      return;
    }
    if (!loadsOf(s)) {
      body.append(h("p", { class: "ui-hint" }, t("light.noLoads")), Button({ label: t("light.toHolds"), onClick: () => go("holds") }));
      return;
    }
    const target = o.target ?? 2;
    body.append(h("p", { class: "ui-hint" }, t("light.intro", { target })));
    if (busy) body.append(h("div", { class: "sim-running", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {}, busy.text),
      Button({ label: t("ui.cancel"), variant: "ghost", onClick: () => services.engine.cancel() })));
    if (error) body.append(h("p", { class: "drop-error", role: "alert" }, error));
    const fresh = result?.key === keyOf(store) ? result.plan : null;
    if (!fresh && !busy) body.append(Button({ label: t("light.find"), variant: "primary", onClick: () => run("find", t("light.finding"), () => findLighter(store, services), services) }));
    if (o.lighter) body.append(h("p", { class: "prob-clear" }, h("b", {}, t("light.inUse"))));
    if (fresh) {
      const ways = lighterWays(fresh);
      if (!ways.length) body.append(h("p", { class: "analyse-stale" }, fresh.explanation));
      else {
        const base = fresh.baseline ?? {};
        const col = (title, x, action) => h("div", { class: ["light-col", action && "is-way"] }, h("b", {}, title),
          h("span", {}, t("light.row.walls", { walls: x.walls, top: x.top, bottom: x.bottom })),
          h("span", {}, t("light.row.infill", { infill: Math.round(x.infill * 100) })),
          h("span", { class: "num" }, `${(x.grams ?? 0).toFixed(1)} g · ${minutes(x.minutes ?? 0)}`),
          h("span", { class: "num" }, t("light.row.safety", { sf: (x.safety_factor ?? 0).toFixed(1), target })),
          x.zones?.length ? h("span", { class: "ui-hint" }, t("light.row.zones", { count: x.zones.length })) : null,
          action);
        body.append(h("div", { class: "light-cols" },
          col(t("light.now"), { walls: o.walls, top: o.top, bottom: o.bottom, infill: o.infill, grams: base.grams, minutes: base.minutes, safety_factor: base.safety_factor }),
          ...ways.map((w) => col(t(`light.way.${w.id}`), w, Button({ label: t("light.use"), size: "small", variant: "primary", onClick: () => use(store, w) })))));
        body.append(h("p", { class: "ui-hint" }, fresh.explanation));
      }
    }
    // The shape: a guide for CAD.
    const sh = shape?.key === keyOf(store) && shape.keep === keep ? shape.r : null;
    body.append(h("h3", { class: "done-h" }, t("light.shapeTitle")), h("p", { class: "ui-hint" }, t("light.shapeIntro")),
      SegmentedControl({ label: t("light.keep"), size: "small", value: String(keep), options: [0.3, 0.4, 0.6].map((k) => ({ value: String(k), label: t("light.keepValue", { pct: Math.round(k * 100) }) })),
        onChange: (v) => { keep = Number(v); services.stage.clearGhosts(); services.rerender(); } }));
    if (!sh && !busy) body.append(Button({ label: t("light.shapeFind"), onClick: () => run("shape", t("light.shaping"), () => findShape(store, services), services) }));
    if (sh) {
      body.append(h("p", {}, sh.explanation),
        Button({ label: t("light.shapeDownload"), onClick: () => download(new Uint8Array(sh.stl), `${s.part.name.replace(/\.[^.]+$/, "")}_lighter_guide.stl`, "model/stl") }));
    }
  },
  leave({ services }) { services.stage.clearGhosts(); },
});
