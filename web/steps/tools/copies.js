// Copies on the plate (T-564, More tools): how many fit printed this way up (api.plate), where they go, and what all
// at once vs one after another means. The number goes into the 3MF (export's ``copies``). Your slicer can do this too.
import { h } from "../../ui/dom.js";
import { Button } from "../../ui/button.js";
import { t } from "../../i18n.js";
import { simProfile } from "../simulate.js";
import { busyLine, run } from "./common.js";

const ui = { busy: null, error: null };
let layout = null; // {key, r}

const keyOf = (store) => JSON.stringify([store.deps("model", "option", "profile"), store.get().copies ?? 1]);

async function place(store, services, copies) {
  const s = store.get();
  const r = await services.engine.call("plate", { up: s.option.up, profile: simProfile(s, s.option), copies });
  if (copies !== (s.copies ?? 1)) store.set({ copies });
  layout = { key: keyOf(store), r };
}

/** The bed seen from above with each copy's footprint (offsets from the bed's centre, mm). */
export function plateView(bed, r) {
  const [bw, bd] = bed;
  const [fw, fd] = r.footprint_mm;
  const pct = (v, of) => `${(v / of) * 100}%`;
  return h("div", { class: "tools-plate", style: { aspectRatio: `${bw} / ${bd}` }, role: "img", "aria-label": t("tools.copies.plate", { count: r.copies }) },
    r.offsets.map(([x, y]) => h("span", { class: "tools-plate__part", style: {
      left: pct(bw / 2 + x - fw / 2, bw), top: pct(bd / 2 - y - fd / 2, bd), width: pct(fw, bw), height: pct(fd, bd) } })));
}

export function render({ body, store, services }) {
  const s = store.get();
  const copies = s.copies ?? 1;
  const r = layout?.key === keyOf(store) ? layout.r : null;
  if (!r && !ui.busy && !ui.error) Promise.resolve().then(() => run(ui, t("tools.copies.placing"), () => place(store, services, copies), services));
  body.append(h("p", { class: "ui-hint" }, t("tools.copies.intro")), ...busyLine(ui, services));
  if (!r) return;
  const set = (n) => run(ui, t("tools.copies.placing"), () => place(store, services, n), services);
  const named = (btn, name) => { btn.setAttribute("aria-label", name); return btn; };
  body.append(h("div", { class: "tools-count" },
    named(Button({ label: "−", title: t("tools.copies.fewer"), size: "small", variant: "secondary", disabled: copies <= 1 || Boolean(ui.busy), onClick: () => set(copies - 1) }), t("tools.copies.fewer")),
    h("b", { class: "num", "aria-live": "polite" }, t("tools.copies.count", { count: r.copies })),
    named(Button({ label: "+", title: t("tools.copies.more"), size: "small", variant: "secondary", disabled: copies >= r.most || Boolean(ui.busy), onClick: () => set(copies + 1) }), t("tools.copies.more")),
    r.most > 1 && copies < r.most ? Button({ label: t("tools.copies.fill", { count: r.most }), size: "small", variant: "ghost", onClick: () => set(r.most) }) : null),
    h("p", { class: "ui-hint" }, r.most > 1 ? t("tools.copies.most", { count: r.most }) : t("tools.copies.one")));
  const bed = s.catalog?.printers?.[s.profile.printer]?.bed;
  if (bed) body.append(plateView(bed, r));
  for (const note of r.notes) body.append(h("p", { class: "ui-hint" }, note));
  body.append(h("p", { class: "ui-hint" }, t("tools.slicerToo")));
}
