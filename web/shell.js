// The new UI's shell (T-530, plan §1.2, §1.6, §1.7, §1.8, §2): header in grid row 1, the step rail, the 3D stage and
// the right panel whose footer keeps Back (left) and Next (right) in the same spot on every step. Steps plug in
// through registerStep(id, {render}); until their tasks land (T-531 …) a step shows only its title and purpose.
// Started by web/new.html (the old web/index.html stays until the cut-over, T-580).
import { h, icon } from "./ui/dom.js";
import { Button } from "./ui/button.js";
import { StepRail } from "./ui/steprail.js";
import { openModal } from "./ui/overlay.js";
import { t } from "./i18n.js";
import { autosave, createStore } from "./state.js";
import { STEPS, canLeave, nextStep, prevStep, railSteps, step } from "./flow.js";

const GEAR = ["M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z", "M12 2v3m0 14v3M2 12h3m14 0h3M4.9 4.9 7 7m10 10 2.1 2.1M4.9 19.1 7 17m10-10 2.1-2.1"];
const UNDO = ["M9 14 4 9l5-5", "M4 9h10a6 6 0 0 1 0 12h-3"];
const REDO = ["m15 14 5-5-5-5", "M20 9H10a6 6 0 0 0 0 12h3"];

const steps = new Map(); // id -> {render({body, stage, store, go, services})}
const screens = new Map(); // start | settings -> render({root, store, go, openRoute, services})
/** A step's content. ``render`` fills the panel body and the stage's overlay layer (``stage``; the 3D view itself
 * is ``services.stage``); it runs again when the state changes. */
export function registerStep(id, def) {
  step(id);
  steps.set(id, def);
}
/** A full-canvas screen (Start, Settings & calibration). */
export function registerScreen(name, render) {
  screens.set(name, render);
}

/** Build the app into ``root`` (the .app grid of new.html). Returns {store, go, route} for steps and tests. */
export function startShell(root, { store = createStore(), saving = autosave(store), services = {} } = {}) {
  let route = "start"; // start | flow | settings
  const header = buildHeader();
  const screen = h("main", { class: "app-screen", id: "screen" });
  // One stage for the whole flow: the 3D view (services.stage) lives in it; steps draw in its overlay layer.
  const stageEl = h("section", { class: "stage", "aria-label": t("shell.stage") });
  const stageUi = h("div", { class: "stage-ui" });
  stageEl.append(stageUi);
  root.replaceChildren(header.el, screen);

  function go(id) {
    const s = store.get();
    const visited = s.visited.includes(id) ? s.visited : [...s.visited, id];
    store.set({ step: id, visited }, { undoable: false });
    route = "flow";
    render(true);
  }
  function openRoute(name) {
    route = name;
    render(true);
  }

  function render(moved = false) {
    const s = store.get();
    // A re-render replaces the panel: keep focus on the active control when staying on the step, or move to the
    // step title when navigating (§1.8).
    const active = document.activeElement;
    const focusKey = !moved && screen.contains(active) ? active?.dataset?.focusKey : null;
    const sel = focusKey && "selectionStart" in active ? [active.selectionStart, active.selectionEnd] : null;
    const hadFocus = screen.contains(active) || active === document.body;
    header.update(s, route);
    if (route !== "flow") {
      screen.dataset.layout = "full";
      const own = screens.get(route);
      const body = h("div", { class: "screen-body" });
      screen.replaceChildren(body);
      if (own) own({ root: body, store, go, openRoute, services });
      else body.append(route === "start" ? startPlaceholder() : settingsPlaceholder());
      body.querySelector("h1")?.setAttribute("tabindex", "-1");
      if (moved) body.querySelector("h1")?.focus();
      return;
    }
    const def = step(s.step);
    screen.dataset.layout = def.layout;
    const rail = StepRail({
      label: t("shell.steps"), collapsed: def.layout === "options",
      steps: railSteps(s, s.step, (r) => store.isStale(r), (id) => t(`step.${id}.label`)),
      onGo: go,
    });
    rail.classList.add("rail");
    const title = h("h2", { tabindex: "-1", id: "step-title" }, t(`step.${s.step}.label`));
    const body = h("div", { class: "panel-body" });
    stageUi.replaceChildren();
    stageEl.className = "stage";
    const blocked = canLeave(s.step, s);
    const back = prevStep(s.step, s);
    const next = nextStep(s.step, s);
    const foot = h("div", { class: "panel-foot" },
      back ? Button({ label: t("shell.back"), variant: "ghost", onClick: () => go(back) }) : h("span"),
      next ? Button({ label: t("shell.next"), variant: "primary", disabled: Boolean(blocked), title: blocked ? t(blocked) : null, onClick: () => go(next) }) : h("span"));
    const head = h("div", { class: "panel-head" }, title, h("p", { class: "hint" }, t(`step.${s.step}.purpose`)));
    (steps.get(s.step)?.render ?? (() => {}))({ body, stage: stageUi, store, go, openRoute, services });
    if (def.layout === "part") {
      screen.replaceChildren(rail, stageEl);
      stageUi.prepend(head);
      stageUi.append(body, foot);
      stageEl.classList.add("stage--part");
    } else if (def.layout === "options") {
      screen.replaceChildren(rail, stageEl, h("section", { class: "compare panel" }, head, body, foot));
    } else {
      screen.replaceChildren(rail, stageEl, h("aside", { class: "panel", "aria-labelledby": "step-title" }, head, body, foot));
    }
    services.stage?.attach(stageEl);
    if (s.part && def.layout !== "part" && services.cameraBar) stageUi.append(services.cameraBar());
    const keep = focusKey && screen.querySelector(`[data-focus-key="${CSS.escape(focusKey)}"]`);
    if (keep) {
      keep.focus();
      if (sel && "setSelectionRange" in keep) try { keep.setSelectionRange(sel[0], sel[1]); } catch { /* non-selectable */ }
    } else if (moved || hadFocus) {
      title.focus();  // focus moves to each new step title (§1.8)
    }
  }

  function startPlaceholder() {
    return h("section", { class: "start" }, h("h1", { tabindex: "-1" }, t("start.title")),
      h("div", { class: "row" }, Button({ label: t("start.optimize"), variant: "primary", onClick: () => go(store.get().step) }),
        Button({ label: t("shell.settings"), onClick: () => openRoute("settings") })));
  }
  function settingsPlaceholder() {
    return h("section", { class: "start" }, h("h1", { tabindex: "-1" }, t("shell.settings")),
      Button({ label: t("shell.backToPart"), variant: "ghost", onClick: () => openRoute(store.get().part ? "flow" : "start") }));
  }

  function buildHeader() {
    const brand = h("button", { type: "button", class: "brand", title: t("shell.toStart") }, h("span", { class: "logo", "aria-hidden": "true" }), t("shell.brand"));
    const workflow = h("span", { class: "workflow" }, t("shell.workflow"));
    const pill = h("button", { type: "button", class: "part-pill", title: t("shell.replacePart"), hidden: true });
    const undo = Button({ iconPath: UNDO, title: t("shell.undo"), variant: "ghost", onClick: () => store.undo() });
    const redo = Button({ iconPath: REDO, title: t("shell.redo"), variant: "ghost", onClick: () => store.redo() });
    const exportBtn = Button({ label: t("shell.export"), variant: "secondary" });
    const settings = Button({ label: t("shell.settings"), variant: "ghost", iconPath: GEAR, onClick: () => openRoute("settings") });
    const help = Button({ label: t("shell.help"), variant: "ghost" });
    const el = h("header", { class: "app-header" }, brand, workflow, pill, h("span", { class: "ui-grow" }), undo, redo, exportBtn, settings, help);
    brand.addEventListener("click", () => {
      const s = store.get();
      if (!s.part) { openRoute("start"); return; }
      openModal({ title: t("shell.leaveTitle"), body: t("shell.leaveBody"), actions: [
        { label: t("shell.stay"), variant: "ghost" },
        { label: t("shell.leave"), variant: "primary", onClick: () => openRoute("start") }] });
    });
    pill.addEventListener("click", () => go("part"));
    return {
      el,
      update(s, where) {
        pill.hidden = !s.part;
        if (s.part) pill.replaceChildren(h("b", {}, s.part.name), h("span", { class: "num" }, s.part.size ? ` · ${s.part.size.map((v) => Math.round(v)).join(" × ")} mm` : ""));
        workflow.hidden = where !== "flow";
        undo.disabled = !store.canUndo();
        redo.disabled = !store.canRedo();
        exportBtn.disabled = !s.part || STEPS.indexOf(step(s.step)) < STEPS.indexOf(step("setup"));
      },
    };
  }

  // Ctrl+Z / Ctrl+Y (and Ctrl+Shift+Z), not while typing in a field (§1.8).
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.target.closest?.("input, textarea, [contenteditable]")) return;
    const key = e.key.toLowerCase();
    if (key === "z" && !e.shiftKey) { e.preventDefault(); store.undo(); }
    else if (key === "y" || (key === "z" && e.shiftKey)) { e.preventDefault(); store.redo(); }
  });
  services.rerender ??= () => render(false);
  store.subscribe((_, changed) => { if (!changed.every((k) => k === "step" || k === "visited")) render(false); });
  render(true);
  return { store, go, openRoute, saving, services, get route() { return route; } };
}
