// ProgressCard (§10.1): long work with named stages, elapsed vs estimate, Cancel, and "Stop & show best so far"
// once a valid option exists. Updated in place with set().
import { h, uid } from "./dom.js";
import { Button } from "./button.js";
import { t } from "../i18n.js";

const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

/** stages: [label]; state: {done: number of finished stages, share: 0..1, elapsed: s, estimate: s, canStop: bool}. */
export function ProgressCard({ title, stages, onCancel, onStop }) {
  const titleId = uid("progress");
  const time = h("span", { class: "ui-progress__time num" });
  const fill = h("i");
  const bar = h("div", { class: "ui-progress__bar", role: "progressbar", "aria-label": title, "aria-valuemin": "0", "aria-valuemax": "100" }, fill);
  const items = stages.map((s) => h("li", {}, s));
  const stop = onStop ? Button({ label: t("ui.stopBest"), variant: "secondary", disabled: true, onClick: onStop }) : null;
  const live = h("span", { class: "visually-hidden", role: "status", "aria-live": "polite" });
  const el = h("div", { class: "ui-progress", "aria-labelledby": titleId },
    h("div", { class: "ui-progress__head" }, h("div", { id: titleId, class: "ui-progress__title" }, title), time),
    bar, h("ol", { class: "ui-progress__stages" }, items), live,
    h("div", { class: "ui-progress__foot" }, Button({ label: t("ui.cancel"), variant: "ghost", onClick: onCancel }), stop));

  let lastStage = null;
  function set({ done = 0, share = 0, elapsed = 0, estimate, canStop = false }) {
    items.forEach((li, i) => { li.className = i < done ? "is-done" : i === done ? "is-running" : ""; });
    const stageText = stages[done] ?? "";
    if (stageText !== lastStage) {
      lastStage = stageText;
      live.textContent = stageText;
    }
    fill.style.width = `${Math.round(share * 100)}%`;
    bar.setAttribute("aria-valuenow", String(Math.round(share * 100)));
    time.textContent = estimate ? t("ui.elapsedOf", { elapsed: mmss(elapsed), estimate: mmss(estimate) }) : mmss(elapsed);
    if (stop) stop.disabled = !canStop;
  }
  set({});
  return Object.assign(el, { set });
}
