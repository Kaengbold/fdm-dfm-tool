// Project files shared with agents (core/fdmcore/project.py writes and reads the same format): the part as
// it is now plus the settings, loads, marks, zones, boxes, environment, load details, copies, pauses and
// sized holes, so either side can pick up the work.
// edit_selections: edit id -> boolean; suggested_boxes: box key -> pending box (including use).
// Box keys are JSON of [kind, source region, point, normal], with null for missing source fields.
// excluded_settings: per-object setting keys left to the user's slicer profile.
// load_settings: optional strength-derived settings and their explanation.

export const FORMAT = "fdm-check-project";
export const VERSION = 1;

// Kept self-contained (no relative imports) so Node tests can import this module from a data: URL.
const OLD_GOALS = { strength: "strong", speed: "fast", material: "light" };
const TEMP_TIERS = { indoors: 20, warm: 40, sun: 50, car: 70, freezing: -10 };
const TOUCH_IDS = ["water", "food", "outdoors", "abrasion", "oil_fuel", "solvent", "skin"];

function toBase64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

/** Project JSON text. ``faceIds``: region id per triangle, so loads and marks find the same faces again. */
export function writeProject({ name, stl, faceIds, ...fields }) {
  const part = { name, stl: toBase64(stl), ...(faceIds ? { face_ids: Array.from(faceIds) } : {}) };
  const kept = Object.fromEntries(Object.entries(fields).filter(([, v]) => v != null && !(Array.isArray(v) && !v.length)
    && !(typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length)));
  return JSON.stringify({ format: FORMAT, version: VERSION, part, ...kept });
}

/** The project's fields, with the part as ``name``, ``stl`` (Uint8Array) and ``faceIds`` (or null). */
export function readProject(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error("This isn't a project file (not JSON)");
  }
  if (data?.format !== FORMAT) throw new Error("This isn't an FDM Check project file");
  if ((data.version ?? 0) > VERSION) throw new Error("This project was saved by a newer version of FDM Check: reload the page to update");
  const { part, format, version, ...fields } = data;
  return { ...fields, name: part.name, stl: Uint8Array.from(atob(part.stl), (c) => c.charCodeAt(0)), faceIds: part.face_ids ?? null };
}

/** Map the UI store's setup fields into project JSON fields for ``writeProject``. */
export function stateToProject(state, { profile, up, loads, faces, settings, zones } = {}) {
  const env = state.environment ?? {};
  const tempC = env.tempC ?? TEMP_TIERS[env.temp] ?? 20;
  const touches = Array.isArray(env.touches) ? env.touches.filter((x) => TOUCH_IDS.includes(x)) : [];
  const d = state.details;
  const details = d ? {
    duration: d.duration ?? "often",
    vibration: d.vibration ?? "no",
    ...(d.mountedKg != null ? { mounted_kg: d.mountedKg } : {}),
    ...(d.dropMm != null ? { drop_mm: d.dropMm } : {}),
    ...(d.tempOk != null ? { temp_ok: d.tempOk } : {}),
  } : undefined;
  return {
    profile: { ...(profile ?? {}), ...(state.goal ? { goal: state.goal } : {}) },
    up,
    loads,
    faces,
    settings,
    excluded_settings: [...new Set(state.exportOff ?? [])],
    environment: { temp: env.temp ?? "indoors", temp_c: tempC, touches },
    details,
    copies: Number.isInteger(state.copies) && state.copies > 1 ? state.copies : undefined,
    pauses: Array.isArray(state.pauses) && state.pauses.length ? state.pauses.map(Number).filter(Number.isFinite) : undefined,
    boxes: Array.isArray(state.boxes) && state.boxes.length ? state.boxes : undefined,
    zones: Array.isArray(zones) && zones.length ? zones : undefined,
  };
}

/** Map a read project's setup fields back into a store.set() patch. Keeps older projects without the new fields working. */
export function projectToState(p, was = {}) {
  const pr = p.profile ?? {};
  const rawGoal = p.goal ?? pr.goal;
  const wasEnv = was.environment ?? { temp: "indoors", tempC: 20, touches: [] };
  let environment;
  if (p.environment && typeof p.environment === "object") {
    const tempC = p.environment.temp_c ?? p.environment.tempC ?? pr.use_temp_c ?? wasEnv.tempC;
    const tier = p.environment.temp ?? (Object.entries(TEMP_TIERS).find(([, v]) => v === tempC)?.[0] ?? "custom");
    const touches = Array.isArray(p.environment.touches) ? p.environment.touches.filter((x) => TOUCH_IDS.includes(x)) : [];
    environment = { ...wasEnv, temp: tier, tempC, touches };
  } else if (pr.use_temp_c != null) {
    environment = { ...wasEnv, temp: "custom", tempC: pr.use_temp_c, touches: [] };
  }
  let details;
  if (p.details && typeof p.details === "object") {
    details = {
      duration: p.details.duration ?? "often",
      vibration: p.details.vibration ?? "no",
      ...(p.details.mounted_kg != null || p.details.mountedKg != null ? { mountedKg: p.details.mounted_kg ?? p.details.mountedKg } : {}),
      ...(p.details.drop_mm != null || p.details.dropMm != null ? { dropMm: p.details.drop_mm ?? p.details.dropMm } : {}),
      ...(p.details.temp_ok != null || p.details.tempOk != null ? { tempOk: p.details.temp_ok ?? p.details.tempOk } : {}),
    };
  } else if (pr.constant_load != null || pr.repeated_load != null) {
    const duration = pr.constant_load ? "always" : pr.repeated_load === false ? "short" : "often";
    details = { duration, vibration: "no" };
  }
  const boxes = Array.isArray(p.boxes)
    ? p.boxes.map((b, i) => ({ ...b, id: b.id ?? `box-p${i}` }))
    : [...(p.zones ?? []), ...(p.volumes ?? [])].filter((z) => z?.lo && z?.hi && p.up).map((z, i) => ({
      id: `box-p${i}`, kind: z.kind ?? "stronger", up: p.up, region: z.region ?? null, box: z, notes: z.notes ?? [],
    }));
  return {
    profile: { ...(was.profile ?? {}), ...(pr.printer ? { printer: pr.printer } : {}), ...(pr.material ? { material: pr.material } : {}), ...(pr.nozzle ? { nozzle: String(pr.nozzle) } : {}) },
    ...(rawGoal ? { goal: OLD_GOALS[rawGoal] ?? rawGoal } : {}),
    ...(environment ? { environment } : {}),
    ...(details ? { details } : {}),
    exportOff: Array.isArray(p.excluded_settings) ? [...p.excluded_settings] : [],
    copies: Number.isInteger(p.copies) && p.copies > 0 ? p.copies : 1,
    pauses: Array.isArray(p.pauses) ? p.pauses.map(Number).filter(Number.isFinite) : [],
    boxes,
    showFaces: p.faces?.clean ?? [],
  };
}
