// StepRail (§1.6): the steps down the left. Done steps are buttons (tick, clickable), the current one is marked
// aria-current, upcoming ones are plain text, skipped ones are left out. "Needs a re-run" shows on done steps whose
// input changed upstream. Collapses to dots in the Options layout (CSS, .ui-rail.is-collapsed).
import { h } from "./dom.js";
import { t } from "../i18n.js";

/** steps: [{id, label, state: done|current|upcoming|skipped, stale?}]. */
export function StepRail({ steps, onGo, collapsed = false, label }) {
  const nav = h("nav", { class: ["ui-rail", collapsed && "is-collapsed"], "aria-label": label });
  const list = h("ol", { class: "ui-rail__list" });
  nav.append(list);

  function render(next) {
    list.replaceChildren(...next.filter((s) => s.state !== "skipped").map((s, i) => {
      const inner = [h("i", { class: "ui-rail__mark", "aria-hidden": "true" }), h("span", { class: "ui-rail__label" }, s.label),
        s.stale && h("span", { class: "ui-rail__stale" }, t("ui.needsRerun"))];
      const item = s.state === "done"
        ? h("button", { type: "button", class: "ui-rail__step", title: collapsed ? s.label : null, onclick: () => onGo?.(s.id) }, inner)
        : h("span", { class: "ui-rail__step", "aria-current": s.state === "current" ? "step" : null, title: collapsed ? s.label : null }, inner);
      return h("li", { class: `is-${s.state}`, dataset: { step: s.id, n: i + 1 } }, item);
    }));
  }
  render(steps);
  return Object.assign(nav, { render });
}
