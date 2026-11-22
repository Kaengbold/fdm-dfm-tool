// Zones and hidden gaps (T-564, More tools): click a face and that spot prints differently. A stronger zone (more
// walls and infill there: api.zone_at), a hidden gap (a thin negative volume the slicer prints walls round, the
// "holes that force walls" trick: api.volume_at "void"), or support only here / no support here. All of them go into
// the 3MF as modifier volumes (export's ``zones``), for the orientation they were made for. Your slicer can do this
// too (modifiers, negative volumes, support painting); this puts them where you click.
import { h } from "../../ui/dom.js";
import { Button } from "../../ui/button.js";
import { SegmentedControl } from "../../ui/choice.js";
import { t } from "../../i18n.js";
import { simProfile } from "../simulate.js";
import { boxSoup, busyLine, run } from "./common.js";

const KINDS = ["stronger", "void", "support", "blocker"];
const ui = { kind: "stronger", busy: null, error: null };
const sameUp = (a, b) => Boolean(a && b) && a.every((v, i) => Math.abs(v - b[i]) < 1e-6);

/** The boxes that go into the 3MF for this option: only those made for its orientation. */
export function exportZones(s) {
  return (s.boxes ?? []).filter((b) => sameUp(b.up, s.option?.up)).map((b) => b.box);
}

async function add(store, services, hit) {
  const s = store.get();
  const o = s.option;
  const region = hit.region;
  const normal = services.picker?.data?.regions?.[region]?.normal ?? null;
  const box = ui.kind === "stronger"
    ? await services.engine.call("zone_at", { up: o.up, region, profile: simProfile(s, o) })
    : await services.engine.call("volume_at", { up: o.up, kind: ui.kind, region, normal, profile: simProfile(s, o) });
  const { corners_print: _c, notes = [], ...keep } = box;
  const kind = ui.kind === "stronger" ? "stronger" : ui.kind;
  const item = { id: `box-${Date.now().toString(36)}`, kind, up: o.up, region, box: { ...keep, kind }, notes };
  store.set({ boxes: [...(store.get().boxes ?? []), item] });
}

export function show(store, services) {
  const s = store.get();
  const mine = (s.boxes ?? []).filter((b) => sameUp(b.up, s.option?.up));
  services.stage.showGhosts(mine.map((b) => ({ mesh: boxSoup(b.box.lo, b.box.hi, b.up), op: b.kind === "void" || b.kind === "blocker" ? "cut" : "add" })));
}

export function onPick(hit, store, services) {
  if (hit.region == null || ui.busy) return;
  run(ui, t("tools.zones.adding"), () => add(store, services, hit), services);
}

export function render({ body, store, services }) {
  const s = store.get();
  body.append(h("p", { class: "ui-hint" }, t("tools.zones.intro")),
    h("div", { class: "tools-tabs" }, SegmentedControl({ label: t("tools.zones.kind"), size: "small", value: ui.kind, options: KINDS.map((k) => ({ value: k, label: t(`tools.zones.kind.${k}`) })),
      onChange: (v) => { ui.kind = v; services.rerender(); } })),
    h("p", { class: "ui-hint" }, t(`tools.zones.how.${ui.kind}`)),
    ...busyLine(ui, services));
  const boxes = s.boxes ?? [];
  const other = boxes.filter((b) => !sameUp(b.up, s.option?.up)).length;
  if (boxes.length) {
    body.append(h("ul", { class: "tools-list" }, boxes.map((b) => h("li", { class: sameUp(b.up, s.option?.up) ? null : "is-off" },
      h("span", { class: ["ui-tag", b.kind === "stronger" ? "is-slicer" : "is-process"] }, t(`tools.zones.kind.${b.kind}`)),
      h("span", { class: "tools-list__text" }, b.kind === "stronger" ? t("tools.zones.strongerNote", { walls: b.box.walls, infill: Math.round(b.box.infill * 100) }) : b.notes?.[0] ?? ""),
      Button({ label: t("problems.remove"), size: "small", variant: "ghost", onClick: () => store.set({ boxes: store.get().boxes.filter((x) => x.id !== b.id) }) })))));
  } else body.append(h("p", { class: "ui-hint" }, t("tools.zones.none")));
  if (other) body.append(h("p", { class: "analyse-stale" }, t("tools.zones.otherUp", { count: other })));
  body.append(h("p", { class: "ui-hint" }, t("tools.slicerToo")));
  show(store, services);
}
