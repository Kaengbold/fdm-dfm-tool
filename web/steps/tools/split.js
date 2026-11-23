// Split the part (T-564, More tools; also the fix under "Too big for your printer" in Problems). The engine
// (api.split, split.py) picks flat cuts: into pieces that fit the bed, or, for a part that fits, so each piece needs much
// less support; joined with alignment pins or a sliding dovetail, the cut kept off the weak spot. Each piece is the
// part with the rest cut away (web/geometry.js, no safety checks: cutting it apart is the point), shown pulled apart
// in its own colour, then downloaded as STLs with the pin. Your slicer's cut tool can do this too.
import { h, download } from "../../ui/dom.js";
import { Button } from "../../ui/button.js";
import { SegmentedControl } from "../../ui/choice.js";
import { t } from "../../i18n.js";
import { simProfile } from "../simulate.js";
import { baseName, busyLine, f32, run } from "./common.js";

const ui = { purpose: "fit", joint: "pins", busy: null, error: null };
let plan = null; // {key, plan, soups}

const keyOf = (store) => JSON.stringify([store.deps("model", "option"), ui.purpose, ui.joint]);

async function cut(store, services) {
  const s = store.get();
  const key = keyOf(store);
  const p = await services.engine.call("split", { profile: simProfile(s, s.option), joint: ui.joint, purpose: ui.purpose });
  const { applyEdits } = await import("../../geometry.js");
  const whole = services.stage.partTriangles();
  const soups = [];
  for (const piece of p.pieces) {
    const edits = piece.edits.map((e) => ({ ...e, mesh: f32(e.mesh) }));
    soups.push((await applyEdits(whole, edits, { check: false })).soup);
  }
  plan = { key, plan: p, soups };
}

/** The pieces pulled apart along the cut axis, each in its own colour band (Stage.showField). */
export function exploded(soups, axis, gap) {
  const total = soups.reduce((n, s) => n + s.length, 0);
  const triangles = new Float32Array(total);
  const values = new Float32Array(total / 3);
  let at = 0;
  soups.forEach((soup, i) => {
    triangles.set(soup, at);
    for (let k = at + axis; k < at + soup.length; k += 3) triangles[k] += (i - (soups.length - 1) / 2) * gap;
    values.fill(soups.length > 1 ? i / (soups.length - 1) : 0, at / 3, (at + soup.length) / 3);
    at += soup.length;
  });
  return { triangles, values, max: 1 };
}

export function show(store, services) {
  if (plan?.key !== keyOf(store)) { services.stage.hideField(); return; }
  const size = store.get().part?.size ?? [100, 100, 100];
  services.stage.showField(exploded(plan.soups, plan.plan.axis, Math.max(...size) * 0.15));
}

export function hide(services) { services.stage.hideField(); }

export function render({ body, store, services }) {
  const s = store.get();
  const fresh = plan?.key === keyOf(store) ? plan : null;
  body.append(h("p", { class: "ui-hint" }, t("tools.split.intro")),
    SegmentedControl({ label: t("tools.split.purpose"), size: "small", value: ui.purpose,
      options: ["fit", "supports"].map((v) => ({ value: v, label: t(`tools.split.purpose.${v}`) })), onChange: (v) => { ui.purpose = v; services.rerender(); } }),
    SegmentedControl({ label: t("tools.split.joint"), size: "small", value: ui.joint,
      options: ["pins", "dovetail"].map((v) => ({ value: v, label: t(`tools.split.joint.${v}`) })), onChange: (v) => { ui.joint = v; services.rerender(); } }),
    ...busyLine(ui, services));
  if (!fresh && !ui.busy) body.append(Button({ label: t("tools.split.run"), variant: "primary", onClick: () => run(ui, t("tools.split.running"), () => cut(store, services), services) }));
  if (fresh) {
    const p = fresh.plan;
    body.append(h("p", {}, p.explanation),
      h("ul", { class: "tools-list" }, p.pieces.map((piece, i) => h("li", {}, h("b", {}, t("tools.split.piece", { n: i + 1 })),
        h("span", { class: "num tools-list__text" }, piece.size_mm.map((v) => Math.round(v)).join(" × ") + " mm")))),
      Button({ label: t("tools.split.download", { count: fresh.soups.length }), variant: "primary", onClick: async () => {
        const { stlBytes } = await import("../../geometry.js");
        fresh.soups.forEach((soup, i) => download(stlBytes(soup), `${baseName(s)}_piece_${i + 1}.stl`, "model/stl"));
        if (p.pin.count) download(stlBytes(f32(p.pin.stl_triangles)), `${baseName(s)}_pin.stl`, "model/stl");
      } }));
  }
  body.append(h("p", { class: "ui-hint" }, t("tools.slicerToo")));
  show(store, services);
}
