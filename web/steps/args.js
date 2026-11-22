// Pure helpers for Setup and Environment (T-532, plan §5, §6, §10.3): maps the UI store into api.optimize()
// arguments and evaluates material suitability with the same rules as core/fdmcore/optimizer.py allowed_materials.
// No DOM imports so this module can be unit-tested directly under Node (core/tests/test_steps_setup_env.py).

export const TEMP_TIERS = {
  indoors: 20,
  warm: 40,
  sun: 50,
  car: 70,
  freezing: -10,
  custom: null,
};

export const TOUCH_IDS = ["water", "food", "outdoors", "abrasion", "oil_fuel", "solvent", "skin"];

const HEAT_MARGIN_C = 10; // matches optimizer.py: heat_limit_c < temp + 10

/** Available nozzle sizes (sorted strings) for a printer, falling back to the catalog's nozzle list. */
export function printerNozzles(catalog, printerId) {
  const printer = catalog?.printers?.[printerId];
  const raw = Array.isArray(printer?.nozzles) && printer.nozzles.length
    ? printer.nozzles
    : Object.keys(catalog?.nozzles ?? {});
  return [...new Set(raw.map(String))].sort((a, b) => Number(a) - Number(b));
}

/** Ticked nozzle sizes for the current profile (all printer sizes by default when profile.nozzles is empty). */
export function activeNozzles(catalog, profile) {
  const avail = printerNozzles(catalog, profile?.printer);
  if (!avail.length) return [];
  const chosen = Array.isArray(profile?.nozzles) ? profile.nozzles.map(String) : [];
  if (!chosen.length) return avail;
  const kept = avail.filter((n) => chosen.includes(n));
  return kept.length ? kept : avail;
}

/** Materials ordered with owned spools first (when catalog.owned exists), preserving catalog order otherwise. */
export function orderedMaterials(catalog) {
  const mats = catalog?.materials ?? {};
  const owned = Array.isArray(catalog?.owned) ? catalog.owned.filter((id) => id in mats) : [];
  const ownedSet = new Set(owned);
  const rest = Object.keys(mats).filter((id) => !ownedSet.has(id));
  return [...owned, ...rest].map((id) => ({ id, ...mats[id], owned: ownedSet.has(id) }));
}

/**
 * Evaluate which catalog materials fit the printer and environment (matches optimizer.py allowed_materials).
 * Returns { suitable, skipped, lockedConflict, hasVerified, hasFood }.
 */
export function evaluateMaterials(catalog, profile, environment) {
  const mats = catalog?.materials ?? {};
  const printer = catalog?.printers?.[profile?.printer] ?? null;
  const tempC = environment?.tempC;
  const touches = (environment?.touches ?? []).filter((x) => TOUCH_IDS.includes(x));
  const ownedSet = new Set(Array.isArray(catalog?.owned) ? catalog.owned : []);
  const hasFood = touches.includes("food");

  function check(id, m) {
    const reasons = [];
    const unverified = [];
    if (tempC != null && m.heat_limit_c != null && Number(m.heat_limit_c) < Number(tempC) + HEAT_MARGIN_C) {
      reasons.push({ kind: "heat", limit: Math.round(Number(m.heat_limit_c)) });
    }
    if ((m.abrasive || (m.fibre && m.fibre !== "none")) && printer && !printer.hardened_nozzle) {
      reasons.push({ kind: "hardened" });
    }
    for (const touch of touches) {
      if (touch === "food") continue;
      const rating = touch === "outdoors" ? m.uv_resistance : m[touch];
      if (rating === "poor") reasons.push({ kind: "poor", touch });
      else if (rating == null) unverified.push(touch);
    }
    const warpWarn = Boolean(m.warp_risk === "high" && printer && printer.enclosure === "none");
    return { id, name: m.name ?? id, owned: ownedSet.has(id), reasons, unverified, verified: unverified.length === 0, warpWarn };
  }

  const suitable = [];
  const skipped = [];
  for (const [id, m] of Object.entries(mats)) {
    const res = check(id, m);
    if (res.reasons.length === 0) suitable.push(res);
    else skipped.push(res);
  }
  // Verified materials first, then owned spools, preserving catalog order within each group.
  suitable.sort((a, b) => Number(b.verified) - Number(a.verified) || Number(b.owned) - Number(a.owned));

  let lockedConflict = null;
  const lockedId = profile?.material || "";
  if (lockedId && lockedId in mats) {
    const res = check(lockedId, mats[lockedId]);
    if (res.reasons.length > 0) lockedConflict = { id: res.id, name: res.name, reasons: res.reasons };
  }

  return {
    suitable,
    skipped,
    lockedConflict,
    hasVerified: suitable.some((m) => m.verified),
    hasFood,
  };
}

/**
 * Map the UI store state into keyword arguments for core/fdmcore/api.py optimize().
 * Locked material / nozzle go into constraints; ticked nozzles go into nozzles; environment carries temp_c & touches.
 */
const G = 9.81;
const where = (x) => (Array.isArray(x.points) && x.points.length ? { points: x.points, radius: x.radius } // painted with the brush (T-573)
  : x.edge != null ? { edge: x.edge, edge_kind: x.edgeKind ?? "feature" } : { region: x.region });

/** A hold as the strength engine takes it (core/fdmcore/strength.py: kind fixed, hold clamped | resting | screw). */
export function holdToEngine(hold) {
  if (hold.kind === "fixed" && !("type" in hold)) return hold; // already in the engine's format (old projects)
  return { ...where(hold), kind: "fixed", hold: hold.type ?? "clamped" };
}

/** A load (stored in N) as the engine takes it: kg along a direction vector, with its load case. */
export function loadToEngine(load) {
  if (load.kind === "force" && load.kg != null && load.newtons == null) return load; // old projects
  return { ...where(load), kind: "force", kg: load.newtons / G, direction: load.direction, ...(load.case > 1 ? { case: load.case } : {}) };
}

/** The faces that should stay free of support marks (Quality, §14): the ones the user marked as show faces, else our
 * guess (``"visible"``: the outer faces, api.visible_faces). Other goals: none. */
export function facesArg(state) {
  if (state?.goal !== "quality") return null;
  return { clean: state.showFaces?.length ? [...state.showFaces] : "visible" };
}

/** Only the show faces the user marked (for the checks and the export: support marks there are a problem, and the
 * slicer keeps the seam off them). Our guess only steers the optimizer. */
export function markedFaces(state) {
  return state?.goal === "quality" && state.showFaces?.length ? { clean: [...state.showFaces] } : null;
}

export function optimizeArgs(state) {
  const goal = state?.goal || "balanced";
  const depth = state?.depth || "extended";
  const change = state?.change || "slicer";
  const prof = state?.profile ?? {};
  const env = state?.environment ?? {};
  const tempC = env.tempC != null && Number.isFinite(Number(env.tempC))
    ? Number(env.tempC)
    : (TEMP_TIERS[env.temp] ?? 20);
  const touches = [...new Set((env.touches ?? []).filter((x) => TOUCH_IDS.includes(x)))];

  const profile = {
    use_temp_c: tempC,
    constant_load: state?.details?.duration === "always" || state?.details?.duration === "constant",
  };
  if (prof.printer) profile.printer = prof.printer;
  if (prof.nozzle) profile.nozzle = String(prof.nozzle);

  const constraints = { ...(state?.constraints ?? {}) };
  if (prof.material) constraints.material = prof.material;
  if (prof.nozzle) constraints.nozzle = String(prof.nozzle);

  let nozzles = null;
  if (Array.isArray(prof.nozzles) && prof.nozzles.length) {
    nozzles = [...new Set(prof.nozzles.map(String))].sort((a, b) => Number(a) - Number(b));
  } else if (state?.catalog) {
    const avail = printerNozzles(state.catalog, prof.printer);
    if (avail.length) nozzles = avail;
  }

  const owned = Array.isArray(state?.catalog?.owned) ? [...state.catalog.owned] : [];
  const materials = Array.isArray(state?.materials) && state.materials.length ? [...state.materials] : null;
  const skipStrength = goal === "quality" || Boolean(state?.noLoads);
  const rawLoads = [...(state?.holds ?? []).map(holdToEngine), ...(state?.loads ?? []).map(loadToEngine)];
  const loads = skipStrength || !rawLoads.length ? null : rawLoads;

  return {
    goal,
    loads,
    depth,
    profile,
    owned,
    materials,
    nozzles,
    environment: { temp_c: tempC, touches },
    constraints,
    change,
    faces: facesArg(state),
  };
}
