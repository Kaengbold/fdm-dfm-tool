// The flow of the new UI as one declarative list (T-530, plan §1.3, §2): each step names its layout, the inputs its
// result depends on (for stale marking, §1.7) and when it's skipped. Labels are message keys (step.<id>.label /
// .purpose in web/messages/en.js). No DOM: pure logic, unit-tested under Node.

export const STEPS = [
  { id: "part", layout: "part" },
  { id: "setup", layout: "work", uses: ["model"] },
  { id: "env", layout: "work", uses: ["model", "profile"] },
  { id: "holds", layout: "work", uses: ["model"], skipWhen: (s) => s.goal === "quality" },
  { id: "loads", layout: "work", uses: ["model", "loads"], skipWhen: (s) => s.goal === "quality" || s.noLoads },
  { id: "details", layout: "work", uses: ["loads"], skipWhen: (s) => s.goal === "quality" || s.noLoads },
  { id: "analyse", layout: "work", uses: ["model", "profile", "goal", "environment", "loads", "depth"], result: "optimize" },
  { id: "options", layout: "options", uses: ["model", "profile", "goal", "environment", "loads", "depth"], result: "optimize" },
  { id: "sim", layout: "work", uses: ["model", "option"], result: "simulate" },
  { id: "problems", layout: "work", uses: ["model", "option"], result: "check" },
  { id: "done", layout: "work", uses: ["model", "option"] },
];
const byId = Object.fromEntries(STEPS.map((s) => [s.id, s]));

export const step = (id) => {
  if (!byId[id]) throw new Error(`Unknown step: ${id}`);
  return byId[id];
};

/** The steps this state goes through (skipped ones left out). */
export const visibleSteps = (state) => STEPS.filter((s) => !s.skipWhen?.(state));

export function nextStep(id, state) {
  const list = visibleSteps(state);
  const i = list.findIndex((s) => s.id === id);
  if (i < 0) return list.find((s) => STEPS.indexOf(s) > STEPS.indexOf(step(id)))?.id ?? null;
  return list[i + 1]?.id ?? null;
}

export function prevStep(id, state) {
  const list = visibleSteps(state);
  const i = list.findIndex((s) => s.id === id);
  if (i < 0) return [...list].reverse().find((s) => STEPS.indexOf(s) < STEPS.indexOf(step(id)))?.id ?? null;
  return list[i - 1]?.id ?? null;
}

/** May the user leave ``id`` forwards? (Next is disabled with a short reason otherwise.) */
export function canLeave(id, state) {
  switch (id) {
    case "part": return state.part ? null : "step.part.needPart";
    case "setup": return state.profile.printer ? null : "step.setup.needPrinter";
    case "holds": return state.holds.length || state.noLoads ? null : "step.holds.needHold";
    case "loads": return state.loads.length ? null : "step.loads.needLoad";
    case "analyse": return state.optimized ? null : "step.analyse.needRun";
    case "options": return state.option ? null : "step.options.needOption";
    default: return null;
  }
}

/**
 * Rail entries for StepRail (web/ui/steprail.js): done (visited before the current one), current, upcoming,
 * skipped; a done step whose result is stale gets ``stale`` ("Needs a re-run"). ``isStale(resultName)`` from the store.
 */
export function railSteps(state, current, isStale = () => false, label = (id) => id) {
  const cur = STEPS.indexOf(step(current));
  return STEPS.map((s, i) => {
    const skipped = Boolean(s.skipWhen?.(state));
    const status = skipped ? "skipped" : i < cur && state.visited.includes(s.id) ? "done" : i === cur ? "current" : "upcoming";
    return { id: s.id, label: label(s.id), state: status, stale: status === "done" && Boolean(s.result) && isStale(s.result) };
  });
}
