// Options step (T-551, plan §11, §10.2, decision 2): the optimizer's 2–4 options as aligned columns under a 3D strip
// (the part turned the way the focused option prints it). Pros and cons at the top, then the same rows in every
// column. Hover a value for its pin: pinned values become chips in "Your choices" (two values for the same thing
// ask Replace or Cancel); "Keep all" pins a column's whole recipe; "Re-optimize with my choices" reruns with the
// chips as hard limits. Nothing reaches the target → a banner with the ways out; an option below the target can
// still be used after a clear warning, which travels with it. "Use this option" moves on to Simulation.
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { OptionColumn, ConstraintsBar } from "../ui/options.js";
import { openModal } from "../ui/overlay.js";
import { registerStep } from "../shell.js";
import { t } from "../i18n.js";
import { optimizeArgs } from "./args.js";

// The rows of §11 in order: [key, label key, pin key in the optimizer's constraints (null = a result, not pinnable)].
export const ROWS = [
  ["orientation", "options.row.orientation", "up"], ["material", "options.row.material", "material"], ["nozzle", "options.row.nozzle", "nozzle"],
  ["layer", "options.row.layer", "layer_height"], ["walls", "options.row.walls", "walls"], ["topBottom", "options.row.topBottom", null],
  ["infill", "options.row.infill", "infill"], ["support", "options.row.support", null], ["total", "options.row.total", null],
  ["time", "options.row.time", null], ["safety", "options.row.safety", null],
];

const minutes = (m) => (m >= 60 ? `${Math.floor(m / 60)} h ${String(Math.round(m % 60)).padStart(2, "0")}` : `${Math.round(m)} min`);
const grams = (g) => (g == null ? t("options.unknown") : `${Math.round(g)} g`);

/** One option's cells (§11 rows) and the value each pin would lock. */
export function optionRows(o, constraints = {}) {
  const pinned = (key, value) => key != null && JSON.stringify(constraints[key]) === JSON.stringify(value);
  const cell = (key, value, pinKey, pinValue, tone) => ({ key, value, pinnable: pinKey != null, pinKey, pinValue, pinned: pinned(pinKey, pinValue), tone });
  const safety = o.safety == null ? t("options.noStrength")
    : t(o.meets_target ? "options.safetyOk" : "options.safetyLow", { sf: o.safety.toFixed(1), target: o.target });
  return [
    cell("orientation", o.orientation, "up", o.up),
    cell("material", `${o.material_name ?? o.material}${o.owned ? ` · ${t("options.owned")}` : ""}`, "material", o.material),
    cell("nozzle", `${o.nozzle} mm`, "nozzle", o.nozzle),
    cell("layer", `${o.layer_height.toFixed(2)} mm`, "layer_height", o.layer_height),
    cell("walls", String(o.walls), "walls", o.walls),
    cell("topBottom", `${o.top} / ${o.bottom}`, null),
    cell("infill", `${Math.round(o.infill * 100)} %`, "infill", o.infill),
    cell("support", grams(o.support_g), null),
    cell("total", grams(o.total_g), null),
    cell("time", minutes(o.minutes), null),
    cell("safety", safety, null, null, o.safety == null ? null : o.meets_target ? "good" : "warn"),
  ];
}

/** A chip's words: "Material: PETG", "Nozzle: 0.6 mm", "Orientation: On its side". */
function chipLabel(key, value, options) {
  const label = t(`options.pin.${key}`);
  if (key === "up") return `${label}: ${options.find((o) => JSON.stringify(o.up) === JSON.stringify(value))?.orientation ?? "…"}`;
  if (key === "material") return `${label}: ${options.find((o) => o.material === value)?.material_name ?? value}`;
  if (key === "infill") return `${label}: ${Math.round(value * 100)} %`;
  if (key === "nozzle" || key === "layer_height") return `${label}: ${value} mm`;
  return `${label}: ${value}`;
}

let busy = false;
let focused = 0;

async function reoptimize(services, store) {
  busy = true;
  services.rerender();
  try {
    const s = store.get();
    const deps = store.deps("model", "profile", "goal", "environment", "loads", "depth");
    const result = await services.engine.call("optimize", optimizeArgs(s));
    store.setResult("optimize", result, deps);
    focused = 0;
  } catch (err) {
    showError = err?.message ?? String(err);
  } finally {
    busy = false;
    services.rerender();
  }
}
let showError = null;

function setPin(store, key, value, options) {
  const constraints = { ...(store.get().constraints ?? {}) };
  if (JSON.stringify(constraints[key]) === JSON.stringify(value)) {
    delete constraints[key];
    store.set({ constraints });
  } else if (constraints[key] != null) {
    // Two values for the same thing: Replace or Cancel (§11).
    openModal({ title: t("options.conflictTitle"), body: t("options.conflictBody", { old: chipLabel(key, constraints[key], options), now: chipLabel(key, value, options) }),
      actions: [{ label: t("ui.cancel"), variant: "ghost" }, { label: t("options.replace"), variant: "primary", onClick: () => store.set({ constraints: { ...constraints, [key]: value } }) }] });
  } else {
    store.set({ constraints: { ...constraints, [key]: value } });
  }
}

function use(store, go, o) {
  const pick = () => { store.set({ option: o }); go("sim"); };
  if (o.meets_target === false || o.below_target) {
    // Decision 2: usable after a clear warning, and the warning travels with the export.
    openModal({ title: t("options.lowTitle"), body: t("options.lowBody", { sf: o.safety?.toFixed(1), target: o.target }),
      actions: [{ label: t("ui.cancel"), variant: "ghost" }, { label: t("options.useAnyway"), variant: "primary", onClick: () => { store.set({ option: { ...o, warning: "below_target" } }); go("sim"); } }] });
  } else pick();
}

registerStep("options", {
  render({ body, store, go, services }) {
    const s = store.get();
    const result = store.result("optimize");
    if (!result) {
      body.append(h("p", { class: "ui-hint" }, t("options.none")), Button({ label: t("options.toAnalyse"), variant: "primary", onClick: () => go("analyse") }));
      return;
    }
    const options = result.options ?? [];
    focused = Math.min(focused, Math.max(options.length - 1, 0));
    services.stage?.orient(options[focused]?.up ?? null);
    const constraints = s.constraints ?? {};
    const stale = store.isStale("optimize");

    if (!result.feasible) {
      const ways = (result.ways_out ?? []).filter((w) => w.id !== "use_anyway");
      body.append(h("div", { class: "options-banner", role: "alert" }, h("b", {}, result.explanation ?? t("options.notReached")),
        h("div", { class: "row" }, ways.map((w) => Button({ label: w.label, variant: "secondary", size: "small", onClick: () => {
          if (w.id === "allow_materials") { const c = { ...constraints }; delete c.material; store.set({ constraints: c, profile: { ...s.profile, material: "" } }); }
          if (w.id === "allow_design") store.set({ change: "both" });
          go("analyse");
        } })))));
    }
    if (stale) body.append(h("p", { class: "analyse-stale" }, t("options.stale")));

    const labels = Object.fromEntries(ROWS.map(([k, label]) => [k, t(label)]));
    const cols = h("div", { class: "options-cols" },
      h("div", { class: "options-labels", "aria-hidden": "true" }, h("span", { class: "options-labels__head" }), ROWS.map(([, label]) => h("span", {}, t(label)))),
      options.map((o, i) => {
        const col = OptionColumn({ option: { ...o, rows: optionRows(o, constraints) }, labels, needsRecheck: stale,
          onPin: (row) => setPin(store, row.pinKey, row.pinValue, options),
          onKeepAll: () => store.set({ constraints: { ...constraints, ...Object.fromEntries(optionRows(o).filter((r) => r.pinKey).map((r) => [r.pinKey, r.pinValue])) } }),
          onUse: () => use(store, go, o) });
        col.addEventListener("mouseenter", () => services.stage?.orient(o.up));
        col.addEventListener("mouseleave", () => services.stage?.orient(options[focused]?.up ?? null));
        col.addEventListener("focusin", () => { focused = i; services.stage?.orient(o.up); });
        return col;
      }));
    cols.style.setProperty("--n", String(options.length)); // a custom property can't be set through style.x
    body.append(cols);
    const choices = Object.entries(constraints).filter(([, v]) => v != null).map(([key, value]) => ({ key, label: chipLabel(key, value, options) }));
    body.append(ConstraintsBar({ choices, busy,
      onRemove: (c) => { const next = { ...constraints }; delete next[c.key]; store.set({ constraints: next }); },
      onReoptimize: () => reoptimize(services, store) }));
    if (busy) body.append(h("p", { class: "ui-hint", role: "status" }, t("options.running")));
    if (showError) { body.append(h("p", { class: "drop-error", role: "alert" }, t("options.failed", { reason: showError }))); showError = null; }
    if (result.nozzles_skipped?.length) body.append(h("p", { class: "ui-hint" }, result.nozzles_skipped.map((n) => n.why).join(" · ")));
  },
});
