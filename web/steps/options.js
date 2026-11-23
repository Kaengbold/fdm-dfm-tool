// Options step (T-551, plan §11, §10.2, decision 2): the optimizer's 2–4 options as aligned columns under a 3D strip
// (the part turned the way the focused option prints it). Pros and cons at the top, then the same rows in every
// column. Hover a value for its pin: pinned values become chips in "Your choices" (two values for the same thing
// ask Replace or Cancel); "Keep all" pins a column's whole recipe; "Re-optimize with my choices" reruns with the
// chips as hard limits. Nothing reaches the target → a banner with the ways out; an option below the target can
// still be used after a clear warning, which travels with it. "Use this option" moves on to Simulation.
// Click a value (material, nozzle, layer, walls, infill) to edit it in the column (T-553): the value becomes a chip in
// Your choices, and that column says "Needs a re-check" (it can't be used) until Re-optimize, which uses the value.
// Removing the chip takes the edit back.
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { OptionColumn, ConstraintsBar } from "../ui/options.js";
import { NumberField } from "../ui/numberfield.js";
import { Select } from "../ui/select.js";
import { openModal } from "../ui/overlay.js";
import { registerStep } from "../shell.js";
import { t } from "../i18n.js";
import { activeNozzles, optimizeArgs, orderedMaterials } from "./args.js";
import { start as startAnalysis } from "./analyse.js";

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

/** What editing a value can change: row key → the constraint it sets. */
export const EDITABLE = { material: "material", nozzle: "nozzle", layer: "layer_height", walls: "walls", infill: "infill" };

/** The option as edited in its column: the edited values (``edits``: constraint key → value) in place, for display. */
export function editedOption(o, edits = {}, catalog = null) {
  const out = { ...o };
  for (const [key, value] of Object.entries(edits)) {
    if (key === "material") Object.assign(out, { material: value, material_name: catalog?.materials?.[value]?.name ?? value, owned: catalog?.owned?.includes(value) });
    else out[key] = value;
  }
  return out;
}

/** The edits that still stand: an edit lasts while its chip in Your choices holds the same value. */
export function standingEdits(edits = {}, constraints = {}) {
  return Object.fromEntries(Object.entries(edits).filter(([k, v]) => JSON.stringify(constraints[k]) === JSON.stringify(v)));
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
let editing = null; // {col, key} while a value is being edited
let edits = { result: null, cols: {} }; // per column: constraint key → edited value, for the result they were made on

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

function setPin(store, key, value, options, { toggle = true } = {}) {
  const constraints = { ...(store.get().constraints ?? {}) };
  if (JSON.stringify(constraints[key]) === JSON.stringify(value)) {
    if (!toggle) return;
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

/** The editor for one value in column ``i``: a list for material and nozzle, a number for the rest. */
function valueEditor(store, services, options, i, key) {
  const s = store.get();
  const o = editedOption(options[i], edits.cols[i], s.catalog);
  const cancel = () => { editing = null; services.rerender(); };
  const commit = (value) => {
    if (value == null) return;
    const pinKey = EDITABLE[key];
    edits.cols[i] = { ...(edits.cols[i] ?? {}), [pinKey]: value };
    editing = null;
    setPin(store, pinKey, value, options, { toggle: false });
    services.rerender();
  };
  const label = t(`options.row.${key}`);
  if (key === "material" || key === "nozzle") {
    const list = key === "material"
      ? orderedMaterials(s.catalog).map((m) => ({ value: m.id, label: m.name ?? m.id, badge: m.owned ? t("options.owned") : null }))
      : activeNozzles(s.catalog, s.profile).map((n) => ({ value: n, label: `${n} mm` }));
    const pick = Select({ label, options: list, value: key === "material" ? o.material : String(o.nozzle), onChange: commit });
    return h("div", { class: "options-edit" }, pick, Button({ label: t("ui.cancel"), size: "small", variant: "ghost", onClick: cancel }));
  }
  const spec = {
    layer: { value: o.layer_height, unit: "mm", min: 0.04, max: Math.round(Number(o.nozzle) * 0.8 * 100) / 100, to: (v) => Math.round(v * 100) / 100 },
    walls: { value: o.walls, unit: "", min: 1, max: 12, to: (v) => Math.round(v) },
    infill: { value: Math.round(o.infill * 100), unit: "%", min: 0, max: 100, to: (v) => Math.round(v) / 100 },
  }[key];
  const field = NumberField({ label, value: spec.value, units: spec.unit ? [{ value: spec.unit, label: spec.unit }] : [], min: spec.min, max: spec.max,
    autofocus: true, onCommit: (v) => commit(spec.to(v)), onCancel: cancel });
  return h("div", { class: "options-edit" }, field, h("div", { class: "row" },
    Button({ label: t("options.set"), size: "small", variant: "primary", onClick: () => {
      const v = field.value;
      if (v != null && v >= spec.min && v <= spec.max) commit(spec.to(v));
    } }),
    Button({ label: t("ui.cancel"), size: "small", variant: "ghost", onClick: cancel })));
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
    if (edits.result !== result) { edits = { result, cols: {} }; editing = null; } // a new run: the edits went into it
    focused = Math.min(focused, Math.max(options.length - 1, 0));
    services.stage?.orient(options[focused]?.up ?? null);
    const constraints = s.constraints ?? {};
    const stale = store.isStale("optimize");

    // Stopped or out of time (T-552): the best found so far, with Continue (a run without a time budget). Never "impossible".
    const early = result.stopped || result.ended_early;
    const goOn = () => { go("analyse"); startAnalysis(services, store, go, optimizeArgs(store.get()), { budget: false }); };
    if (!result.feasible) {
      const ways = (result.ways_out ?? []).filter((w) => w.id !== "use_anyway" && !(w.id === "continue" && !early));
      if (early && !ways.some((w) => w.id === "continue")) ways.unshift({ id: "continue", label: t("options.continue") });
      body.append(h("div", { class: "options-banner", role: "alert" },
        h("b", {}, early ? t(result.stopped ? "options.stoppedNone" : "options.endedNone") : result.explanation ?? t("options.notReached")),
        h("div", { class: "row" }, ways.map((w) => Button({ label: w.id === "continue" ? t("options.continue") : w.label, variant: w.id === "continue" ? "primary" : "secondary", size: "small", onClick: () => {
          if (w.id === "continue") { goOn(); return; }
          if (w.id === "allow_materials") { const c = { ...constraints }; delete c.material; store.set({ constraints: c, profile: { ...s.profile, material: "" } }); }
          if (w.id === "allow_design") store.set({ change: "both" });
          go("analyse");
        } })))));
    } else if (early) {
      body.append(h("div", { class: "options-banner is-early", role: "status" }, h("b", {}, t(result.stopped ? "options.stopped" : "options.endedEarly")),
        h("div", { class: "row" }, Button({ label: t("options.continue"), variant: "secondary", size: "small", onClick: goOn }))));
    }
    if (stale) body.append(h("p", { class: "analyse-stale" }, t("options.stale")));

    // Quality checks no strength at all (§14): no Strength row.
    const shown = s.goal === "quality" ? ROWS.filter(([k]) => k !== "safety") : ROWS;
    const rowsOf = (o) => optionRows(o, constraints).filter((r) => shown.some(([k]) => k === r.key));
    const labels = Object.fromEntries(shown.map(([k, label]) => [k, t(label)]));
    const cols = h("div", { class: "options-cols" },
      h("div", { class: "options-labels", "aria-hidden": "true" }, h("span", { class: "options-labels__head" }), shown.map(([, label]) => h("span", {}, t(label)))),
      options.map((o0, i) => {
        const mine = standingEdits(edits.cols[i], constraints);
        const edited = Object.keys(mine).length > 0;
        const o = editedOption(o0, mine, s.catalog);
        const rows = rowsOf(o).map((r) => ({ ...r, editable: r.key in EDITABLE && !busy, edited: EDITABLE[r.key] in mine }));
        const col = OptionColumn({ option: { ...o, rows }, labels, needsRecheck: stale || edited,
          editing: editing?.col === i ? editing.key : null, editor: editing?.col === i ? valueEditor(store, services, options, i, editing.key) : null,
          onEdit: (row) => { editing = { col: i, key: row.key }; services.rerender(); },
          onPin: (row) => setPin(store, row.pinKey, row.pinValue, options),
          onKeepAll: () => store.set({ constraints: { ...constraints, ...Object.fromEntries(optionRows(o).filter((r) => r.pinKey).map((r) => [r.pinKey, r.pinValue])) } }),
          onUse: () => use(store, go, o0) });
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
