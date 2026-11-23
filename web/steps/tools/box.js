// Electronics box heat (T-564, More tools): the part as a box with electronics inside using some watts. How warm the
// air inside gets (api.box_heat: heat out through the outside surface and the printed wall), against where the
// plastic goes soft, and which walls get warmest, coloured on the part. Clicking an inside face says where the
// electronics sit (default the middle). No slicer does this.
import { h } from "../../ui/dom.js";
import { Button } from "../../ui/button.js";
import { SegmentedControl } from "../../ui/choice.js";
import { NumberField } from "../../ui/numberfield.js";
import { t } from "../../i18n.js";
import { simProfile } from "../simulate.js";
import { busyLine, f32, run } from "./common.js";

const ui = { watts: 5, room: null, cooling: "closed", point: null, busy: null, error: null };
let result = null; // {key, r}

const keyOf = (store) => JSON.stringify([store.deps("model", "option", "profile"), ui.watts, ui.room, ui.cooling, ui.point]);
const roomOf = (s) => ui.room ?? s.environment?.tempC ?? 25;

async function work(store, services) {
  const s = store.get();
  const r = await services.engine.call("box_heat", { power_w: ui.watts, ambient_c: roomOf(s), cooling: ui.cooling, profile: simProfile(s, s.option), point: ui.point });
  result = { key: keyOf(store), r };
}

export function show(store, services) {
  const r = result?.key === keyOf(store) ? result.r : null;
  if (!r) { services.stage.hideField(); return; }
  const temps = f32(r.vertex_temp_c);
  const values = temps.map((v) => Math.max(0, v - r.ambient_c));
  services.stage.showField({ triangles: f32(r.field_triangles), values, max: Math.max(1, r.heat_limit_c - r.ambient_c) });
}

export function hide(services) { services.stage.hideField(); }

export function onPick(hit, store, services) {
  if (!hit.point || ui.busy) return;
  ui.point = hit.point;
  services.rerender();
}

export function render({ body, stage, store, services }) {
  const s = store.get();
  const r = result?.key === keyOf(store) ? result.r : null;
  body.append(h("p", { class: "ui-hint" }, t("tools.box.intro")),
    h("div", { class: "tools-fields" },
      NumberField({ label: t("tools.box.watts"), value: ui.watts, min: 0.1, max: 500, units: [{ value: "w", label: "W" }], onChange: (n) => { if (n != null) ui.watts = n; } }),
      NumberField({ label: t("tools.box.room"), value: roomOf(s), min: -20, max: 80, units: [{ value: "c", label: "°C" }], onChange: (n) => { if (n != null) ui.room = n; } })),
    SegmentedControl({ label: t("tools.box.cooling"), size: "small", value: ui.cooling, options: ["closed", "fan"].map((v) => ({ value: v, label: t(`tools.box.cooling.${v}`) })),
      onChange: (v) => { ui.cooling = v; services.rerender(); } }),
    h("p", { class: "ui-hint" }, ui.point ? t("tools.box.at") : t("tools.box.pick"),
      ui.point ? h("button", { type: "button", class: "ui-link", onclick: () => { ui.point = null; services.rerender(); } }, t("tools.box.middle")) : null),
    ...busyLine(ui, services));
  if (!ui.busy) body.append(Button({ label: t("tools.box.run"), variant: r ? "secondary" : "primary", onClick: () => run(ui, t("tools.box.running"), () => work(store, services), services) }));
  if (r) {
    body.append(h("div", { class: ["tools-verdict", `is-${r.verdict.replace(" ", "-")}`] },
      h("b", { class: "num" }, t("tools.box.inside", { c: Math.round(r.inside_c) })),
      h("span", {}, r.explanation)));
    stage.append(h("div", { class: "sim-legend" }, h("b", {}, t("tools.box.legend")), h("span", {}, t("tools.box.legendRange", { from: Math.round(r.ambient_c), to: Math.round(r.heat_limit_c) }))));
  }
  show(store, services);
}
