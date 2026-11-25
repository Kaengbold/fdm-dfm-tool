// Compare the optimizer's options beside a 3D view of the focused option's orientation.
// Hover a value for its pin: pinned values become chips in "Your choices" (two values for the same thing
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
import { ProgressCard } from "../ui/progress.js";
import { openModal } from "../ui/overlay.js";
import { registerStep } from "../shell.js";
import { t } from "../i18n.js";
import { activeNozzles, optimizeArgs, orderedMaterials } from "./args.js";
import { STAGES, STAGE_OF, start as startAnalysis } from "./analyse.js";

// Comparison order: [row key, label key].
export const ROWS = [
  ["safety", "options.row.safety"], ["time", "options.row.time"], ["total", "options.row.total"],
  ["orientation", "options.row.orientation"], ["material", "options.row.material"], ["infill", "options.row.infill"],
  ["topBottom", "options.row.topBottom"], ["changes", "options.row.changes"], ["nozzle", "options.row.nozzle"],
  ["layer", "options.row.layer"], ["walls", "options.row.walls"], ["support", "options.row.support"],
];

const minutes = (m) => (m >= 60 ? `${Math.floor(m / 60)} h ${String(Math.round(m % 60)).padStart(2, "0")}` : `${Math.round(m)} min`);
const grams = (g) => (g == null ? t("options.unknown") : `${Math.round(g)} g`);

/** One option's cells (§11 rows) and the value each pin would lock. */
export function optionRows(o, constraints = {}) {
  const pinned = (key, value) => key != null && JSON.stringify(constraints[key]) === JSON.stringify(value);
  const cell = (key, value, pinKey, pinValue, tone) => ({ key, value, pinnable: pinKey != null, pinKey, pinValue, pinned: pinned(pinKey, pinValue), tone });
  const nominal = o.safety == null ? t("options.noStrength")
    : t(o.meets_target ? "options.safetyOk" : "options.safetyLow", { sf: o.safety.toFixed(1), target: o.target });
  const [low, high] = o.safety_range?.map((value) => Number(value.toFixed(1))) ?? [];
  const safety = low == null ? nominal : t("ranges.safety", { nominal, low, high });
  return [
    cell("orientation", o.orientation, "up", o.up),
    cell("material", `${o.material_name ?? o.material}${o.owned ? ` · ${t("options.owned")}` : ""}`, "material", o.material),
    cell("nozzle", `${o.nozzle} mm`, "nozzle", o.nozzle),
    cell("layer", `${o.layer_height.toFixed(2)} mm`, "layer_height", o.layer_height),
    cell("walls", String(o.walls), "walls", o.walls),
    cell("topBottom", `${o.top} / ${o.bottom}`, null),
    cell("changes", o.reinforcements?.map((r) => r.label).join(" · ") || t("options.noChanges"), null),
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
  if (Object.keys(edits).length) delete out.safety_range;
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
let rerun = null; // {started, done, share, partial, card, timer, deps} while re-optimizing with constraints (T-608)
let focused = 0;
let editing = null; // {col, key} while a value is being edited
let edits = { result: null, cols: {} }; // per column: constraint key → edited value, for the result they were made on
let showShared = false;
let framedPart = null;

function framePreview(stage) {
  const part = stage?.part;
  if (!part || part === framedPart) return;
  // The old camera target may fall outside the turned part in the taller Options stage.
  const { min, max } = part.geo.boundingBox;
  const { x, y, z } = part.offset;
  const corners = [min.x, max.x].flatMap((px) => [min.y, max.y].flatMap((py) =>
    [min.z, max.z].map((pz) => [px - x, py - y, pz - z])));
  stage.frameBox(corners);
  framedPart = part;
}

function sharedRows(keys, columns) {
  if (columns.length < 2) return [];
  return keys.filter((key) => {
    const first = columns[0].rows[key];
    const pinValue = JSON.stringify(first.pinValue);
    return editing?.key !== key && columns.every(({ rows }) => {
      const row = rows[key];
      return !row.edited && row.value === first.value && JSON.stringify(row.pinValue) === pinValue;
    });
  });
}

const rerunCardState = (r) => ({ done: r.done, share: r.share, elapsed: (performance.now() - r.started) / 1000, canStop: Boolean(r.partial) });

function finishRerun() {
  if (rerun?.timer) clearInterval(rerun.timer);
  rerun = null;
  busy = false;
}

function stopRerun(services, store) {
  const r = rerun;
  if (!r?.partial) return;
  finishRerun();
  store.setResult("optimize", { ...r.partial, stopped: true }, r.deps);
  services.engine.cancel();
  focused = 0;
  services.rerender();
}

async function reoptimize(services, store) {
  busy = true;
  const s = store.get();
  const deps = store.deps("model", "profile", "goal", "environment", "loads", "depth");
  const me = { started: performance.now(), done: 0, share: 0, partial: null, card: null, timer: null, deps };
  rerun = me;
  const update = () => rerun?.card?.set(rerunCardState(rerun));
  me.timer = setInterval(update, 500);
  services.onPartial = (result) => { if (rerun === me && result?.options?.length) { me.partial = result; update(); } };
  services.onProgress = (fraction, text) => {
    if (rerun !== me) return;
    const hit = STAGE_OF.find(([prefix]) => text?.startsWith(prefix));
    if (hit) me.done = Math.max(me.done, STAGES.indexOf(hit[1]));
    me.share = Math.max(me.share, fraction ?? 0);
    update();
  };
  services.rerender();
  try {
    const result = await services.engine.call("optimize", optimizeArgs(s));
    if (rerun !== me) return;
    store.setResult("optimize", result, deps);
    focused = 0;
  } catch (err) {
    if (rerun !== me) return;
    const cancelled = err?.name === "Cancelled" || err?.constructor?.name === "Cancelled";
    if (!cancelled) showError = err?.message ?? String(err);
  } finally {
    if (rerun === me) finishRerun();
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
  leave() { framedPart = null; },
  render({ body, stage, store, go, services }) {
    const s = store.get();
    const result = store.result("optimize");
    if (!result) {
      body.append(h("p", { class: "ui-hint" }, t("options.none")), Button({ label: t("options.toAnalyse"), variant: "primary", onClick: () => go("analyse") }));
      return;
    }
    if (rerun) {
      const card = ProgressCard({ title: t("options.reoptimizing"), stages: STAGES.map((k) => t(`analyse.stage.${k}`)),
        onCancel: () => services.engine.cancel(), onStop: () => stopRerun(services, store) });
      rerun.card = card;
      card.set(rerunCardState(rerun));
      stage.append(h("div", { class: "analyse-progress" }, card));
    }
    const options = result.options ?? [];
    if (edits.result !== result) {
      edits = { result, cols: {} }; // A new run incorporates the edits.
      editing = null;
      showShared = false;
    }
    focused = Math.min(focused, Math.max(options.length - 1, 0));
    services.stage?.orient(options[focused]?.up ?? null);
    framePreview(services.stage);
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
    if (s.goal !== "quality" && options.some((o) => o.safety_range)) body.append(h("p", { class: "ui-hint" }, t("ranges.hint")));

    const shown = ROWS.filter(([key]) => (key !== "safety" || s.goal !== "quality")
      && (key !== "changes" || options.some((o) => o.reinforcements?.length)));
    const labels = Object.fromEntries(shown.map(([key, label]) => [key, t(label)]));
    labels.why = t("options.row.why");
    const columns = options.map((original, i) => {
      const mine = standingEdits(edits.cols[i], constraints);
      const option = editedOption(original, mine, s.catalog);
      const rows = Object.fromEntries(optionRows(option, constraints).map((row) => [row.key, {
        ...row, editable: row.key in EDITABLE && !busy, edited: EDITABLE[row.key] in mine,
      }]));
      return { option, rows, needsRecheck: stale || Object.keys(mine).length > 0 };
    });
    const keys = shown.map(([key]) => key);
    const shared = sharedRows(keys, columns);
    const visible = keys.filter((key) => showShared || !shared.includes(key));
    if (options.some((o) => o.pros?.length || o.cons?.length)) visible.push("why");

    const cols = h("div", { class: "options-cols" },
      h("div", { class: "options-labels", "aria-hidden": "true" }, h("span"), visible.map((key) => h("span", {}, labels[key]))),
      columns.map(({ option, rows, needsRecheck }, i) => {
        const col = OptionColumn({ option: { ...option, rows: visible.map((key) => rows[key] ?? { key }) }, labels, needsRecheck,
          editing: editing?.col === i ? editing.key : null, editor: editing?.col === i ? valueEditor(store, services, options, i, editing.key) : null,
          onEdit: (row) => { editing = { col: i, key: row.key }; services.rerender(); },
          onPin: (row) => setPin(store, row.pinKey, row.pinValue, options),
          onKeepAll: () => store.set({ constraints: { ...constraints, ...Object.fromEntries(Object.values(rows).filter((r) => r.pinKey).map((r) => [r.pinKey, r.pinValue])) } }),
          onUse: () => use(store, go, options[i]) });
        col.addEventListener("mouseenter", () => services.stage?.orient(option.up));
        col.addEventListener("mouseleave", () => services.stage?.orient(options[focused]?.up ?? null));
        col.addEventListener("focusin", () => { focused = i; services.stage?.orient(option.up); });
        return col;
      }));
    if (columns.length) {
      cols.style.setProperty("--option-count", String(columns.length));
      cols.style.setProperty("--comparison-rows", String(visible.length + 1));
      body.append(cols);
    }
    if (shared.length) {
      const settings = shared.map((key) =>
        t("options.sameSetting", { name: labels[key], value: columns[0].rows[key].value }));
      body.append(h("div", { class: "options-shared" },
        h("span", {}, t("options.sameForAll", { settings })),
        h("button", { type: "button", class: "ui-link", "aria-expanded": String(showShared), dataset: { focusKey: "options-shared" },
          onclick: () => { showShared = !showShared; services.rerender(); } },
        t(showShared ? "options.hideShared" : "options.showShared"))));
    }
    const choices = Object.entries(constraints).filter(([, v]) => v != null).map(([key, value]) => ({ key, label: chipLabel(key, value, options) }));
    body.append(ConstraintsBar({ choices, busy,
      onRemove: (c) => { const next = { ...constraints }; delete next[c.key]; store.set({ constraints: next }); },
      onReoptimize: () => reoptimize(services, store) }));
    if (busy) body.append(h("p", { class: "ui-hint", role: "status" }, t("options.running")));
    if (showError) { body.append(h("p", { class: "drop-error", role: "alert" }, t("options.failed", { reason: showError }))); showError = null; }
    if (result.nozzles_skipped?.length) body.append(h("p", { class: "ui-hint" }, result.nozzles_skipped.map((n) => n.why).join(" · ")));
  },
});
