// Step 2: Setup ("What you have", T-532, plan §5, §16.4, §16.7).
// Printer (searchable + Manage -> Settings), Material & Nozzle ("Decide later" or clearable hard lock),
// printer nozzle ToggleChips (all ticked by default), goal ChoiceCards, "What can you change?" SegmentedControl
// with a non-blocking STEP warning when design changes are chosen on an STL or 3MF part.
import { registerStep } from "../shell.js";
import { t } from "../i18n.js";
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { Select } from "../ui/select.js";
import { ToggleChip } from "../ui/chip.js";
import { ChoiceCards, SegmentedControl } from "../ui/choice.js";
import { activeNozzles, defaultNozzles, optimizeArgs, orderedMaterials, printerNozzles } from "./args.js";

export { optimizeArgs };

function partKind(part) {
  if (!part) return "";
  if (part.kind) return String(part.kind).toLowerCase();
  const ext = part.name?.split(".").pop();
  return ext ? ext.toLowerCase() : "";
}

function render({ body, stage, store, go, openRoute }) {
  const s = store.get();
  const catalog = s.catalog;
  if (!catalog?.printers || !catalog?.materials) {
    body.replaceChildren(h("p", { class: "hint step-loading" }, t("setup.loading")));
    return;
  }

  const profile = s.profile ?? { printer: null, material: "", nozzle: "", nozzles: [] };
  const printer = profile.printer ? catalog.printers[profile.printer] : null;
  if (printer?.bed?.length >= 2) {
    stage.append(h("div", { class: "stage-note" },
      h("b", {}, printer.name ?? profile.printer),
      ` · ${t("setup.stageNote", { x: Math.round(printer.bed[0]), y: Math.round(printer.bed[1]) })}`));
  }

  const patchProfile = (patch) => store.set({ profile: { ...profile, ...patch } });

  // 1. Printer, Material, Nozzle, Nozzles to consider
  const printerOptions = Object.entries(catalog.printers).map(([id, p]) => ({
    value: id,
    label: p.name ?? id,
    hint: p.bed?.length >= 2 ? t("setup.bedSize", { x: Math.round(p.bed[0]), y: Math.round(p.bed[1]) }) : null,
  }));
  const printerSelect = Select({
    placeholder: t("setup.pickPrinter"),
    label: t("setup.printer"),
    options: printerOptions,
    value: profile.printer ?? "",
    decideLater: false,
    clearable: false,
    searchable: true,
    onChange: (id) => {
      const avail = printerNozzles(catalog, id);
      const nextNozzle = profile.nozzle && avail.includes(String(profile.nozzle)) ? String(profile.nozzle) : "";
      const def = defaultNozzles(catalog, id);
      const nextNozzles = nextNozzle && !def.includes(nextNozzle) ? avail.filter((n) => def.includes(n) || n === nextNozzle) : def;
      patchProfile({ printer: id, nozzles: nextNozzles, nozzle: nextNozzle });
    },
  });
  printerSelect.querySelector(".ui-select__button")?.setAttribute("data-focus-key", "setup-printer");

  const manageBtn = Button({
    label: t("setup.manage"),
    variant: "secondary",
    onClick: () => (openRoute ?? globalThis.fdmUI?.openRoute)?.("settings"),
  });

  const materialOptions = orderedMaterials(catalog).map((m) => ({
    value: m.id,
    label: m.name ?? m.id,
    badge: m.owned ? t("setup.owned") : null,
  }));
  const materialSelect = Select({
    label: t("setup.material"),
    options: materialOptions,
    value: profile.material || "",
    decideLater: true,
    clearable: true,
    searchable: true,
    onChange: (id) => patchProfile({ material: id }),
  });
  materialSelect.querySelector(".ui-select__button")?.setAttribute("data-focus-key", "setup-material");

  const availNozzles = printerNozzles(catalog, profile.printer);
  const tickedNozzles = activeNozzles(catalog, profile);
  const nozzleOptions = availNozzles.map((size) => ({
    value: size,
    label: t("setup.nozzleMm", { size }),
  }));
  const nozzleSelect = Select({
    label: t("setup.nozzle"),
    options: nozzleOptions,
    value: profile.nozzle ? String(profile.nozzle) : "",
    decideLater: true,
    clearable: true,
    searchable: false,
    onChange: (size) => {
      const nextNozzles = size && !tickedNozzles.includes(size)
        ? availNozzles.filter((n) => tickedNozzles.includes(n) || n === size)
        : tickedNozzles;
      patchProfile({ nozzle: size, nozzles: profile.printer ? nextNozzles : profile.nozzles });
    },
  });
  nozzleSelect.querySelector(".ui-select__button")?.setAttribute("data-focus-key", "setup-nozzle");

  const printerGroup = h("div", { class: "step-group" },
    h("div", { class: "ui-label" }, t("setup.printer")),
    h("div", { class: "step-row" }, printerSelect, manageBtn),
    h("div", { class: "step-row-two" },
      h("div", { class: "step-field" }, h("div", { class: "step-sub" }, t("setup.material")), materialSelect),
      h("div", { class: "step-field" }, h("div", { class: "step-sub" }, t("setup.nozzle")), nozzleSelect)));

  if (printer && availNozzles.length) {
    const chips = availNozzles.map((size) => {
      const chip = ToggleChip({
        label: size,
        pressed: tickedNozzles.includes(size),
        onChange: (on) => {
          if (!on && tickedNozzles.length <= 1) {
            chip.setAttribute("aria-pressed", "true");
            return;
          }
          const next = on
            ? availNozzles.filter((n) => tickedNozzles.includes(n) || n === size)
            : tickedNozzles.filter((n) => n !== size);
          const nextLock = !on && String(profile.nozzle) === size ? "" : profile.nozzle;
          patchProfile({ nozzles: next, nozzle: nextLock });
        },
      });
      chip.classList.add("num");
      chip.dataset.focusKey = `setup-nozzle-chip-${size}`;
      chip.dataset.nozzle = size;
      return chip;
    });
    printerGroup.append(
      h("div", { class: "step-field step-nozzles" },
        h("div", { class: "step-sub" }, t("setup.nozzles")),
        h("div", { class: "step-chips", role: "group", "aria-label": t("setup.nozzles") }, ...chips)));
  }

  // 2. What matters most?
  const goalCards = ChoiceCards({
    label: t("setup.goal"),
    columns: 2,
    value: s.goal || "balanced",
    options: [
      { value: "balanced", title: t("setup.goal.balanced"), line: t("setup.goal.balanced.line") },
      { value: "strong", title: t("setup.goal.strong"), line: t("setup.goal.strong.line") },
      { value: "fast", title: t("setup.goal.fast"), line: t("setup.goal.fast.line") },
      { value: "light", title: t("setup.goal.light"), line: t("setup.goal.light.line") },
      { value: "quality", title: t("setup.goal.quality"), line: t("setup.goal.quality.line") },
    ],
    onChange: (goal) => store.set({ goal }),
  });
  goalCards.classList.add("step-goal-cards");
  goalCards.querySelectorAll("[role=radio]").forEach((el) => { el.dataset.focusKey = `setup-goal-${el.dataset.value}`; });

  const goalGroup = h("div", { class: "step-group" },
    h("div", { class: "ui-label" }, t("setup.goal")),
    goalCards);

  // 3. What can you change? + STEP warning for STL/3MF
  const changeSeg = SegmentedControl({
    label: t("setup.change"),
    value: s.change || "slicer",
    options: [
      { value: "slicer", label: t("setup.change.slicer") },
      { value: "design", label: t("setup.change.design") },
      { value: "both", label: t("setup.change.both") },
    ],
    onChange: (change) => store.set({ change }),
  });
  changeSeg.querySelectorAll("[role=radio]").forEach((el) => { el.dataset.focusKey = `setup-change-${el.dataset.value}`; });

  const kind = partKind(s.part);
  const showStepWarning = (s.change === "design" || s.change === "both") && (kind === "stl" || kind === "3mf");
  const changeGroup = h("div", { class: "step-group" },
    h("div", { class: "ui-label" }, t("setup.change")),
    changeSeg,
    showStepWarning && h("div", { class: "step-notice step-notice--warn", role: "status", dataset: { notice: "step-warning" } },
      h("p", {}, t("setup.stepWarning")),
      h("div", { class: "step-notice__actions" },
        Button({ label: t("setup.loadStep"), variant: "secondary", size: "small", onClick: () => go("part") }))));

  body.replaceChildren(printerGroup, goalGroup, changeGroup);
}

registerStep("setup", { render });
