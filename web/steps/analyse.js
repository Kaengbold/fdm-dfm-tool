// Analyse step (T-550, plan §10.1): how deep should we look? Fast / Extended (default) / Complete, each with this
// part's time estimate (api.estimate_runtime, calibrated; the browser figure). Complete needs design changes: shown
// but off with "Allow" when only slicer settings may change; hidden for Quality. Start runs the optimizer
// (api.optimize) in the engine worker with a progress card on the stage: named stages, elapsed vs estimate, Cancel, and
// Stop & show best so far once the engine has sent a partial result (T-552: the worker can't answer a second call while
// the optimizer runs, so the best options so far come with the progress; Stop keeps the latest and cancels). Extended
// and Complete get a time budget (BUDGET × the estimate): when it runs out the engine picks from what it checked and
// Options says "Search ended early" with Continue (a run without a budget), never "impossible". The result is kept
// with the input revisions it used, so a later change marks it stale; then on to Options.
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { ChoiceCards } from "../ui/choice.js";
import { ProgressCard } from "../ui/progress.js";
import { registerStep } from "../shell.js";
import { t } from "../i18n.js";
import { optimizeArgs } from "./args.js";
import { showFacesPanel, clearShowFaces } from "./showfaces.js";

export const DEPENDS = ["model", "profile", "goal", "environment", "loads", "depth"];
// The optimizer's own stage names (core/fdmcore/optimizer.py progress.report) → the plan's list (§10.1).
export const STAGES = ["orient", "strength", "settings", "nozzles", "reinforce", "pick"];
export const STAGE_OF = [["Finding orientations", "orient"], ["Checking strength", "strength"], ["Measuring support", "strength"],
  ["Trying walls", "settings"], ["Trying nozzles", "nozzles"], ["Testing local reinforcements", "reinforce"], ["Picking options", "pick"]];

const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
/** "~20 s", "~2 min" for an estimate in seconds. */
export const roughTime = (s) => (s < 50 ? t("analyse.seconds", { n: Math.max(5, Math.round(s / 5) * 5) }) : t("analyse.minutes", { n: Math.max(1, Math.round(s / 60)) }));

let run = null; // {depth, started, estimate, card, timer, partial} while the optimizer runs
// The time budget: this many times the browser estimate, at least BUDGET_MIN_S; none for Fast or without an estimate.
const BUDGET = 2.5;
const BUDGET_MIN_S = 120;
export const budgetFor = (depth, estimate) => (depth === "fast" || !estimate ? null : Math.max(BUDGET_MIN_S, Math.round(BUDGET * estimate)));
let estimates = { key: null, values: {} };
let lastError = null;

async function loadEstimates(services, store, args) {
  const key = JSON.stringify([store.revisions(), args.loads?.length, args.goal, args.change]);
  if (estimates.key === key) return;
  estimates = { key, values: {} };
  for (const depth of ["fast", "extended", "complete"]) {
    try {
      const { faces: _faces, ...rest } = args; // the estimate doesn't depend on marked faces
      const e = await services.engine.call("estimate_runtime", { ...rest, depth });
      if (estimates.key !== key) return;
      estimates.values[depth] = e.browser_seconds ?? e.seconds * 3;
    } catch { /* no estimate: the card shows none */ }
  }
  if (estimates.key === key && !run) services.rerender();
}

/** Run the optimizer. ``budget``: false for no time budget (Continue the search), else the depth's own.
 * ``goAfter``: false lets a caller (e.g. T-610 smart re-run) decide where to navigate next. Returns true on success. */
export async function start(services, store, go, args, { budget = true, goAfter = true } = {}) {
  const depth = store.get().depth;
  const deps = store.deps(...DEPENDS);
  lastError = null;
  const estimate = estimates.values[depth] ?? null;
  const me = { depth, started: performance.now(), estimate, done: 0, share: 0, partial: null, deps };
  run = me;
  services.onPartial = (result) => { if (run === me && result?.options?.length) { me.partial = result; update(); } };
  services.onProgress = (fraction, text) => {
    if (!run) return;
    const hit = STAGE_OF.find(([prefix]) => text?.startsWith(prefix));
    if (hit) run.done = Math.max(run.done, STAGES.indexOf(hit[1]));
    run.share = Math.max(run.share, fraction ?? 0);
    update();
  };
  const update = () => run?.card?.set(cardState(run));
  run.timer = setInterval(update, 500);
  services.rerender();
  try {
    const budget_s = budget ? budgetFor(depth, estimate) : null;
    const result = await services.engine.call("optimize", { ...args, depth, budget_s });
    if (run !== me) return false; // stopped: the partial result is in already
    store.setResult("optimize", result, deps);
    finish();
    store.set({ optimized: Date.now() }, { undoable: false });
    if (goAfter) go("options");
    return true;
  } catch (err) {
    if (run !== me) return false; // stopped with the best so far
    const cancelled = err?.name === "Cancelled" || err?.constructor?.name === "Cancelled";
    finish();
    if (!cancelled) lastError = err?.message ?? String(err);
    services.rerender();
    return false;
  }
}

function finish() {
  if (run?.timer) clearInterval(run.timer);
  run = null;
}

const cardState = (r) => ({ done: r.done, share: r.share, elapsed: (performance.now() - r.started) / 1000, estimate: r.estimate, canStop: Boolean(r.partial) });

/** Stop & show best so far: keep the latest partial result (marked stopped) and cancel the run. */
function stop(services, store, go) {
  const r = run;
  if (!r?.partial) return;
  finish();
  store.setResult("optimize", { ...r.partial, stopped: true }, r.deps);
  services.engine.cancel();
  store.set({ optimized: Date.now() }, { undoable: false });
  go("options");
}

let watching = false;
/** A changed input makes the result stale: then Next waits for a new run (flow.canLeave reads ``optimized``). */
function watchStale(store) {
  if (watching) return;
  watching = true;
  store.subscribe(() => { if (store.get().optimized && store.isStale("optimize")) store.set({ optimized: null }, { undoable: false }); });
}

registerStep("analyse", {
  render({ body, stage, store, go, services }) {
    watchStale(store);
    const s = store.get();
    const args = optimizeArgs(s);
    // Only for a part the engine has loaded (it reports a face count); a failed estimate just shows none.
    if (s.part?.faces) loadEstimates(services, store, args).catch(() => {});
    const quality = s.goal === "quality";
    const designAllowed = s.change !== "slicer";
    const est = (d) => (estimates.values[d] != null ? roughTime(estimates.values[d]) : null);
    const options = [
      { value: "fast", title: t("analyse.fast"), meta: est("fast"), line: t("analyse.fastLine") },
      { value: "extended", title: t("analyse.extended"), meta: est("extended"), line: t("analyse.extendedLine") },
    ];
    if (!quality) options.push({ value: "complete", title: t("analyse.complete"), meta: est("complete"), line: t("analyse.completeLine"), disabled: !designAllowed });
    const depth = !quality || s.depth !== "complete" ? s.depth : "extended";
    const goalWord = t(`analyse.goal.${s.goal}`), changeWord = t(`analyse.change.${s.change}`);
    body.append(h("p", { class: "ui-hint" }, [s.profile?.printer ? (s.catalog?.printers?.[s.profile.printer]?.name ?? s.profile.printer) : null, goalWord, changeWord].filter(Boolean).join(" · ")));
    body.append(h("div", { class: "ui-label" }, t("analyse.howDeep")),
      ChoiceCards({ label: t("analyse.howDeep"), columns: 1, value: depth, options, onChange: (v) => store.set({ depth: v }, { undoable: false }) }));
    if (quality && s.part) body.append(showFacesPanel(store, services));
    if (!quality && !designAllowed) {
      body.append(h("p", { class: "ui-hint analyse-allow" }, t("analyse.needsDesign"), " ",
        h("button", { type: "button", class: "ui-link", onclick: () => store.set({ change: "both" }) }, t("analyse.allow"))));
    }
    const result = store.result("optimize");
    if (result && store.isStale("optimize")) body.append(h("p", { class: "analyse-stale" }, t("analyse.stale")));
    if (lastError) body.append(h("p", { class: "drop-error", role: "alert" }, t("analyse.failed", { reason: lastError })));
    if (!s.part) body.append(h("p", { class: "ui-hint" }, t("analyse.noPart")));

    if (run) {
      const card = ProgressCard({ title: t("analyse.running"), stages: STAGES.map((k) => t(`analyse.stage.${k}`)),
        onCancel: () => { services.engine.cancel(); }, onStop: () => stop(services, store, go) });
      run.card = card;
      card.set(cardState(run));
      stage.append(h("div", { class: "analyse-progress" }, card));
      body.append(Button({ label: t("analyse.runningShort"), variant: "primary", disabled: true }));
    } else {
      body.append(Button({ label: result && !store.isStale("optimize") ? t("analyse.again") : t("analyse.start"), variant: "primary",
        disabled: !s.part, onClick: () => start(services, store, go, args) }));
      if (result && !store.isStale("optimize")) body.append(Button({ label: t("analyse.toOptions"), variant: "ghost", onClick: () => go("options") }));
    }
  },
  leave({ services }) { clearShowFaces(services); },
});

export { mmss };
