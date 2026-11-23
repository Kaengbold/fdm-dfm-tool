// Settings & calibration hub (T-533, plan §15.3): Printers, Print profiles, Materials, Calibration tests,
// What the checks use. Full-canvas screen registered via registerScreen("settings", ...). Persists to localStorage
// (wrapped in try/catch) and IndexedDB autosave extras, pushes custom_presets / set_expert to the engine on start
// and on change, and exposes catalog.owned + per-printer nozzles in the store for Setup (T-532).

import { arrowIndex, h, icon, uid } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { SegmentedControl } from "../ui/choice.js";
import { ToggleChip } from "../ui/chip.js";
import { Select } from "../ui/select.js";
import { NumberField } from "../ui/numberfield.js";
import { Disclosure } from "../ui/disclosure.js";
import { openModal, showToast } from "../ui/overlay.js";
import { registerScreen } from "../shell.js";
import { t } from "../i18n.js";
import {
  fromAnswers,
  fromOrcaFilament,
  fromOrcaPrinter,
  fromPrintProfile,
  loadMine,
  loadPrintProfile,
  saveMine,
  savePrintProfile,
  toForm,
  toSpec,
  warpLength,
  zFactor,
} from "../mypresets.js";

export const STANDARD_NOZZLES = ["0.2", "0.4", "0.6", "0.8", "1.0"];
const SETTINGS_KEY = "fdm.settings.v1";
const SETTINGS_FORMAT = "fdm-check-settings";
const SETTINGS_VERSION = 1;
const BACK_ARROW = "M19 12H5m6-6-6 6 6 6";
const DOWNLOAD_ICON = ["M12 3v12m0 0-4-4m4 4 4-4", "M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"];

const TABS = [
  { id: "printers", labelKey: "settings.tab.printers" },
  { id: "profiles", labelKey: "settings.tab.profiles" },
  { id: "materials", labelKey: "settings.tab.materials" },
  { id: "calibration", labelKey: "settings.tab.calibration" },
  { id: "active", labelKey: "settings.tab.active" },
];

const PRINT_KEY_LABELS = {
  wall_loops: "Walls",
  sparse_infill_density: "Infill",
  sparse_infill_pattern: "Infill pattern",
  layer_height: "Layer height",
  top_shell_layers: "Top layers",
  bottom_shell_layers: "Bottom layers",
  enable_support: "Supports",
  support_type: "Support type",
  support_threshold_angle: "Support angle",
  brim_type: "Brim type",
  brim_width: "Brim width",
  bridge_speed: "Bridge speed",
  outer_wall_speed: "Outer wall speed",
  inner_wall_speed: "Inner wall speed",
  sparse_infill_speed: "Infill speed",
  internal_solid_infill_speed: "Solid infill speed",
  top_surface_speed: "Top surface speed",
};

const TYPICAL_FITS = { press: 0.1, snug: 0.2, sliding: 0.35, loose: 0.5 };
const TYPICAL_WARP = {
  pla: { none: 110, passive: 110, heated: 110 },
  petg: { none: 85, passive: 100, heated: 100 },
  abs: { none: 45, passive: 75, heated: 115 },
  pa_cf: { none: 85, passive: 110, heated: 140 },
  pla_plus: { none: 110, passive: 110, heated: 110 },
  asa: { none: 55, passive: 85, heated: 120 },
  pc: { none: 40, passive: 70, heated: 110 },
  tpu_95a: { none: 140, passive: 140, heated: 140 },
};

// Built-in fallback matching core/fdmcore/data/presets.yaml so Settings works immediately before Pyodide boots.
const FALLBACK_BUILTINS = {
  printers: {
    bambu_a1: { name: "Bambu Lab A1", moving_bed: true, bed: [256, 256, 256], enclosure: "none", hardened_nozzle: true, speed: "fast", multi_material: "optional", bed_surface: "unknown" },
    bambu_a1_mini: { name: "Bambu Lab A1 mini", moving_bed: true, bed: [180, 180, 180], enclosure: "none", hardened_nozzle: true, speed: "fast", multi_material: "optional", bed_surface: "unknown" },
    bambu_p1s: { name: "Bambu Lab P1S", moving_bed: false, bed: [256, 256, 256], enclosure: "passive", hardened_nozzle: false, speed: "fast", multi_material: "optional", bed_surface: "unknown" },
    bambu_x1c: { name: "Bambu Lab X1 Carbon", moving_bed: false, bed: [256, 256, 256], enclosure: "passive", hardened_nozzle: true, speed: "fast", multi_material: "optional", bed_surface: "unknown" },
    bambu_h2d: { name: "Bambu Lab H2D", moving_bed: false, bed: [325, 320, 325], enclosure: "heated", hardened_nozzle: true, speed: "fast", multi_material: "builtin", bed_surface: "unknown" },
    prusa_mk4s: { name: "Prusa MK4S", moving_bed: true, bed: [250, 210, 220], enclosure: "none", hardened_nozzle: false, speed: "medium", multi_material: "optional", bed_surface: "unknown" },
    prusa_mini: { name: "Prusa MINI+", moving_bed: true, bed: [180, 180, 180], enclosure: "none", hardened_nozzle: false, speed: "medium", multi_material: "none", bed_surface: "unknown" },
    prusa_core_one: { name: "Prusa CORE One", moving_bed: false, bed: [250, 220, 270], enclosure: "heated", hardened_nozzle: false, speed: "fast", multi_material: "optional", bed_surface: "unknown" },
    creality_k1c: { name: "Creality K1C", moving_bed: false, bed: [220, 220, 250], enclosure: "passive", hardened_nozzle: true, speed: "fast", multi_material: "optional", bed_surface: "unknown" },
    creality_k2_plus: { name: "Creality K2 Plus", moving_bed: false, bed: [350, 350, 350], enclosure: "heated", hardened_nozzle: true, speed: "fast", multi_material: "optional", bed_surface: "unknown" },
    creality_ender3_v3_se: { name: "Creality Ender-3 V3 SE", moving_bed: true, bed: [220, 220, 250], enclosure: "none", hardened_nozzle: false, speed: "medium", multi_material: "none", bed_surface: "unknown" },
    creality_ender3_v3_ke: { name: "Creality Ender-3 V3 KE", moving_bed: true, bed: [220, 220, 240], enclosure: "none", hardened_nozzle: false, speed: "fast", multi_material: "none", bed_surface: "unknown" },
    elegoo_centauri_carbon: { name: "Elegoo Centauri Carbon", moving_bed: false, bed: [256, 256, 256], enclosure: "passive", hardened_nozzle: true, speed: "fast", multi_material: "optional", bed_surface: "unknown" },
    anycubic_kobra_3: { name: "Anycubic Kobra 3", moving_bed: true, bed: [250, 250, 260], enclosure: "none", hardened_nozzle: false, speed: "fast", multi_material: "optional", bed_surface: "unknown" },
    prusa_xl: { name: "Prusa XL (single tool)", moving_bed: false, bed: [360, 360, 360], enclosure: "none", hardened_nozzle: false, speed: "fast", multi_material: "optional", bed_surface: "unknown" },
  },
  materials: {
    pla: { name: "PLA", max_overhang_deg: 55, max_bridge_mm: 35, min_layer_time_s: 8, warp_risk: "low", max_flow_mm3s: 15, z_strength_factor: { none: 0.55, passive: 0.55, heated: 0.55 }, tensile_xy_mpa: 50, density_g_cm3: 1.24, shrinkage_pct: 0.3, fibre: "none" },
    petg: { name: "PETG", max_overhang_deg: 45, max_bridge_mm: 15, min_layer_time_s: 10, warp_risk: "low", max_flow_mm3s: 12, z_strength_factor: { none: 0.6, passive: 0.65, heated: 0.7 }, tensile_xy_mpa: 45, density_g_cm3: 1.27, shrinkage_pct: 0.4, fibre: "none" },
    abs: { name: "ABS", max_overhang_deg: 40, max_bridge_mm: 10, min_layer_time_s: 10, warp_risk: "high", max_flow_mm3s: 15, z_strength_factor: { none: 0.3, passive: 0.5, heated: 0.65 }, tensile_xy_mpa: 38, density_g_cm3: 1.04, shrinkage_pct: 0.8, fibre: "none" },
    pa_cf: { name: "PA-CF (carbon fibre nylon)", max_overhang_deg: 45, max_bridge_mm: 20, min_layer_time_s: 10, warp_risk: "medium", max_flow_mm3s: 8, z_strength_factor: { none: 0.2, passive: 0.25, heated: 0.3 }, tensile_xy_mpa: 70, density_g_cm3: 1.1, shrinkage_pct: 0.3, fibre: "carbon" },
    pla_plus: { name: "PLA+", max_overhang_deg: 55, max_bridge_mm: 35, min_layer_time_s: 8, warp_risk: "low", max_flow_mm3s: 15, z_strength_factor: { none: 0.6, passive: 0.6, heated: 0.6 }, tensile_xy_mpa: 50, density_g_cm3: 1.24, shrinkage_pct: 0.3, fibre: "none" },
    asa: { name: "ASA", max_overhang_deg: 45, max_bridge_mm: 15, min_layer_time_s: 10, warp_risk: "high", max_flow_mm3s: 14, z_strength_factor: { none: 0.35, passive: 0.55, heated: 0.7 }, tensile_xy_mpa: 40, density_g_cm3: 1.07, shrinkage_pct: 0.6, fibre: "none" },
    pc: { name: "PC (polycarbonate)", max_overhang_deg: 40, max_bridge_mm: 10, min_layer_time_s: 10, warp_risk: "high", max_flow_mm3s: 13, z_strength_factor: { none: 0.3, passive: 0.5, heated: 0.7 }, tensile_xy_mpa: 60, density_g_cm3: 1.2, shrinkage_pct: 0.8, fibre: "none" },
    tpu_95a: { name: "TPU 95A (flexible)", max_overhang_deg: 40, max_bridge_mm: 8, min_layer_time_s: 12, warp_risk: "low", max_flow_mm3s: 3, z_strength_factor: { none: 0.85, passive: 0.85, heated: 0.85 }, tensile_xy_mpa: 30, density_g_cm3: 1.21, shrinkage_pct: 0.5, fibre: "none" },
  },
  nozzles: {
    "0.2": { line_width: 0.22, layer_height: 0.1 },
    "0.4": { line_width: 0.45, layer_height: 0.2 },
    "0.6": { line_width: 0.62, layer_height: 0.3 },
    "0.8": { line_width: 0.82, layer_height: 0.4 },
    "1.0": { line_width: 1.02, layer_height: 0.5 },
  },
  speeds: {
    fast: { outer_wall: 200, infill: 270 },
    medium: { outer_wall: 140, infill: 200 },
  },
};

function emptySettings() {
  return {
    format: SETTINGS_FORMAT,
    version: SETTINGS_VERSION,
    mine: { printers: {}, materials: {} },
    printerNozzles: {},
    owned: [],
    materialExtras: {},
    printProfile: null,
    calibrationMaterial: "pla",
    starterChecked: {},
    expert: {},
  };
}

function normalizeId(id) {
  return id.startsWith("my_") ? id : `my_${id}`;
}

function normalizeMine(mine) {
  const out = { printers: {}, materials: {} };
  for (const table of ["printers", "materials"]) {
    for (const [k, v] of Object.entries(mine?.[table] ?? {})) {
      if (v && typeof v === "object" && v.based_on) out[table][normalizeId(k)] = structuredClone(v);
    }
  }
  return out;
}

function normalizeNozzles(list) {
  const set = new Set((list ?? []).map(String).filter((n) => STANDARD_NOZZLES.includes(n)));
  return STANDARD_NOZZLES.filter((n) => set.has(n));
}

function normalizeSettings(raw) {
  const base = emptySettings();
  if (!raw || typeof raw !== "object") return base;
  base.mine = normalizeMine(raw.mine);
  if (raw.printerNozzles && typeof raw.printerNozzles === "object") {
    for (const [k, v] of Object.entries(raw.printerNozzles)) {
      const clean = normalizeNozzles(v);
      if (clean.length) base.printerNozzles[k] = clean;
    }
  }
  if (Array.isArray(raw.owned)) base.owned = [...new Set(raw.owned.map(String))];
  if (raw.materialExtras && typeof raw.materialExtras === "object") {
    for (const [k, v] of Object.entries(raw.materialExtras)) {
      if (v && typeof v === "object") base.materialExtras[k] = { ...v };
    }
  }
  if (raw.printProfile && typeof raw.printProfile === "object" && raw.printProfile.values) {
    base.printProfile = structuredClone(raw.printProfile);
  }
  if (typeof raw.calibrationMaterial === "string" && raw.calibrationMaterial) {
    base.calibrationMaterial = raw.calibrationMaterial;
  }
  if (raw.starterChecked && typeof raw.starterChecked === "object") {
    base.starterChecked = structuredClone(raw.starterChecked);
  }
  if (raw.expert && typeof raw.expert === "object") {
    for (const [k, v] of Object.entries(raw.expert)) {
      if (Number.isFinite(Number(v))) base.expert[k] = Number(v);
    }
  }
  return base;
}

function readLocalSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (raw) return normalizeSettings(JSON.parse(raw));
  } catch { /* storage blocked */ }
  // Migrate any presets saved by the previous UI so returning users keep them.
  const migrated = emptySettings();
  const legacyMine = loadMine();
  const legacyProf = loadPrintProfile();
  let legacyExpert = {};
  try { legacyExpert = JSON.parse(localStorage.getItem("fdm-expert") || "{}") || {}; } catch { /* ignore */ }
  migrated.mine = normalizeMine(legacyMine);
  migrated.printProfile = legacyProf;
  migrated.expert = legacyExpert;
  return migrated;
}

function writeLocalSettings(data) {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(data)); } catch { /* storage blocked */ }
  saveMine(data.mine);
  savePrintProfile(data.printProfile);
  try {
    if (Object.keys(data.expert).length) localStorage.setItem("fdm-expert", JSON.stringify(data.expert));
    else localStorage.removeItem("fdm-expert");
  } catch { /* storage blocked */ }
}

// Runtime module state (mirrors how web/steps/part.js holds transient screen state outside the undoable store).
let settings = emptySettings();
let basePresets = structuredClone(FALLBACK_BUILTINS);
let enginePresets = null; // last full presets object returned by engine.call("presets" | "custom_presets")
let engineReady = false;
let engineSynced = false;
let dirty = false;
let ctx = null; // { store, saving, engine, services }
let orcaPrintersCache = null;
let orcaProfilesCache = null;

let view = {
  tab: "printers", // printers | profiles | materials | calibration | active
  printerFilter: "",
  printerMode: null, // null | { kind: "orca" | "helper" | "edit", id?, values?, note?, nozzles?, source? }
  profilePreview: null, // null | { name, values, missingParent, missing, conflicts }
  profileError: "",
  materialEditor: null, // null | { id?, values, isNew }
  expandedSpool: null, // materialId with inline price/grams editor open
  calTest: null, // null | "starter" | "fit" | "overhang" | "bridge" | "warping" | "layer_strength"
  calDraft: {},
  expertData: null, // result of engine.call("expert_settings")
  expertOpen: false,
  downloading: null,
};

let pendingFocusSelector = null;

function builtinsOf() {
  const src = enginePresets ?? basePresets;
  const printers = Object.fromEntries(Object.entries(src.printers ?? {}).filter(([k]) => !k.startsWith("my_")));
  const materials = Object.fromEntries(Object.entries(src.materials ?? {}).filter(([k]) => !k.startsWith("my_")));
  return { printers, materials };
}

/** Merge built-in presets, custom presets, per-printer nozzles, owned materials and spool extras into store.catalog. */
function buildCatalog() {
  const base = enginePresets ?? basePresets;
  const builtins = builtinsOf();
  const printers = {};
  for (const [id, p] of Object.entries(builtins.printers)) {
    printers[id] = {
      ...structuredClone(p),
      source: "builtin",
      nozzles: settings.printerNozzles[id] ? [...settings.printerNozzles[id]] : [...STANDARD_NOZZLES],
    };
  }
  for (const [id, spec] of Object.entries(settings.mine.printers)) {
    const parent = builtins.printers[spec.based_on] ?? builtins.printers.bambu_p1s ?? Object.values(builtins.printers)[0];
    const merged = enginePresets?.printers?.[id] ? structuredClone(enginePresets.printers[id]) : { ...structuredClone(parent), ...structuredClone(spec) };
    printers[id] = {
      ...merged,
      source: merged.source ?? "custom",
      nozzles: settings.printerNozzles[id] ? [...settings.printerNozzles[id]] : [...STANDARD_NOZZLES],
    };
  }
  const materials = {};
  for (const [id, m] of Object.entries(builtins.materials)) {
    const extra = settings.materialExtras[id] ?? {};
    materials[id] = { ...structuredClone(m), ...extra, source: "builtin" };
  }
  for (const [id, spec] of Object.entries(settings.mine.materials)) {
    const parent = builtins.materials[spec.based_on] ?? builtins.materials.pla ?? Object.values(builtins.materials)[0];
    const merged = enginePresets?.materials?.[id] ? structuredClone(enginePresets.materials[id]) : { ...structuredClone(parent), ...structuredClone(spec) };
    const extra = settings.materialExtras[id] ?? {};
    materials[id] = { ...merged, ...extra, source: isMeasuredSpec(spec) ? "measured" : "custom" };
  }
  const owned = settings.owned.filter((id) => id in materials);
  return {
    ...base,
    printers,
    materials,
    nozzles: base.nozzles ?? FALLBACK_BUILTINS.nozzles,
    speeds: base.speeds ?? FALLBACK_BUILTINS.speeds,
    owned,
    printProfile: settings.printProfile ? structuredClone(settings.printProfile) : null,
  };
}

function isMeasuredSpec(spec) {
  if (!spec) return false;
  return Boolean(
    spec.max_overhang_deg != null ||
    spec.max_bridge_mm != null ||
    spec.warp_length_mm != null ||
    spec.z_strength_factor != null ||
    spec.fit_gap_mm != null ||
    spec.hole_error_mm != null ||
    spec.outer_error_mm != null ||
    spec.creep_readings?.length
  );
}

function pushCatalog() {
  if (!ctx?.store) return;
  const creepReadings = Object.fromEntries(Object.entries(settings.mine.materials)
    .filter(([, spec]) => spec.creep_readings?.length)
    .map(([id, spec]) => [id, spec.creep_readings]));
  ctx.store.set({ catalog: buildCatalog(), creepReadings }, { undoable: false });
}

function cleanSpecForEngine(spec) {
  const out = structuredClone(spec);
  delete out.source;
  delete out.nozzles;
  return out;
}

function engineMinePayload() {
  return {
    printers: Object.fromEntries(Object.entries(settings.mine.printers).map(([k, v]) => [k, cleanSpecForEngine(v)])),
    materials: Object.fromEntries(Object.entries(settings.mine.materials).map(([k, v]) => [k, cleanSpecForEngine(v)])),
  };
}

/** Push custom presets and expert overrides into a running (or freshly restarted) engine worker. */
export async function restoreSettingsToEngine(post) {
  const payload = engineMinePayload();
  const hasMine = Object.keys(payload.printers).length || Object.keys(payload.materials).length;
  const presetsRes = hasMine ? await post("custom_presets", payload) : await post("presets");
  enginePresets = presetsRes;
  if (Object.keys(settings.expert).length) {
    await post("set_expert", { values: settings.expert });
  }
  return presetsRes;
}

async function syncEngine() {
  if (!ctx?.engine || !engineReady) return;
  try {
    enginePresets = await ctx.engine.call("custom_presets", engineMinePayload());
    if (view.expertData || Object.keys(settings.expert).length) {
      view.expertData = await ctx.engine.call("set_expert", { values: settings.expert });
    }
    pushCatalog();
  } catch { /* Engine rejected an entry or is restarting; UI keeps working. */ }
}

function persistAndSync({ sync = true, rerender = true } = {}) {
  dirty = true;
  writeLocalSettings(settings);
  ctx?.saving?.putExtra("settings", structuredClone(settings));
  pushCatalog();
  if (sync) syncEngine();
  if (rerender) ctx?.services?.rerender?.();
}

export function openSettingsTab(tab = "printers") {
  if (TABS.some((item) => item.id === tab)) view.tab = tab;
  view.calTest = null;
  view.printerMode = null;
  view.materialEditor = null;
}

/** Initialise settings persistence, populate store.catalog immediately, and sync once the engine boots. */
export function initSettings({ store, saving, engine, services }) {
  ctx = { store, saving, engine, services };
  settings = readLocalSettings();
  pushCatalog();

  // If localStorage was empty (e.g. blocked or cleared) check IndexedDB autosave extra too.
  const localEmpty = !Object.keys(settings.mine.printers).length && !Object.keys(settings.mine.materials).length &&
    !settings.owned.length && !settings.printProfile && !Object.keys(settings.printerNozzles).length &&
    !Object.keys(settings.materialExtras).length && !Object.keys(settings.starterChecked).length &&
    !Object.keys(settings.expert).length;
  if (localEmpty && saving?.getExtra) {
    saving.getExtra("settings").then((saved) => {
      if (!saved || dirty) return;
      settings = normalizeSettings(saved);
      writeLocalSettings(settings);
      pushCatalog();
      syncEngine();
    }).catch(() => {});
  }

  // Sync with the Python engine once Pyodide finishes booting.
  engine.ready.then(async () => {
    engineReady = true;
    try {
      const payload = engineMinePayload();
      const hasMine = Object.keys(payload.printers).length || Object.keys(payload.materials).length;
      enginePresets = hasMine ? await engine.call("custom_presets", payload) : await engine.call("presets");
      if (Object.keys(settings.expert).length) {
        view.expertData = await engine.call("set_expert", { values: settings.expert });
      }
      engineSynced = true;
      pushCatalog();
    } catch { /* fall back to local presets */ }
  }).catch(() => {});

  services.settings = {
    openTab: openSettingsTab,
    getData: () => structuredClone(settings),
    exportJson: () => JSON.stringify(settings, null, 2),
    importJson: (text) => importSettingsText(text),
    resetAll: () => resetAllSettings(),
    syncEngine,
    isEngineSynced: () => engineSynced,
  };
}

// ---------- Export / Import / Reset ----------

function downloadBytes(bytes, filename, mime) {
  const blob = new Blob([bytes], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function exportAllSettings() {
  const json = JSON.stringify({ ...settings, format: SETTINGS_FORMAT, version: SETTINGS_VERSION }, null, 2);
  downloadBytes(json, "fdm-check-settings.json", "application/json");
  showToast({ message: t("settings.exported"), tone: "good" });
}

function importSettingsText(text) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    showToast({ message: t("settings.importFailed", { reason: err.message }), tone: "bad" });
    return false;
  }
  if (!parsed || typeof parsed !== "object" || (parsed.format && parsed.format !== SETTINGS_FORMAT)) {
    showToast({ message: t("settings.importInvalid"), tone: "bad" });
    return false;
  }
  settings = normalizeSettings(parsed);
  view.calTest = null;
  view.printerMode = null;
  view.materialEditor = null;
  view.profilePreview = null;
  persistAndSync();
  showToast({ message: t("settings.imported"), tone: "good" });
  return true;
}

function resetAllSettings() {
  const prevProfile = ctx?.store?.get().profile;
  settings = emptySettings();
  view.calTest = null;
  view.printerMode = null;
  view.materialEditor = null;
  view.profilePreview = null;
  view.expertData = null;
  if (prevProfile?.material?.startsWith("my_")) {
    ctx.store.set({ profile: { ...prevProfile, material: "" } }, { undoable: false });
  }
  if (prevProfile?.printer?.startsWith("my_")) {
    ctx.store.set({ profile: { ...ctx.store.get().profile, printer: null } }, { undoable: false });
  }
  persistAndSync();
  if (engineReady && ctx?.engine) {
    ctx.engine.call("set_expert", { values: {} }).then((res) => { view.expertData = res; }).catch(() => {});
  }
  showToast({ message: t("settings.resetAllDone"), tone: "info" });
}

// ---------- Helpers for calibration material resolution ----------

function activePrinterObj() {
  const catalog = ctx?.store?.get().catalog ?? buildCatalog();
  const pid = ctx?.store?.get().profile?.printer || "bambu_p1s";
  return catalog.printers[pid] ?? catalog.printers.bambu_p1s ?? Object.values(catalog.printers)[0];
}

function resolveCalMaterialId() {
  const catalog = buildCatalog();
  const selected = settings.calibrationMaterial;
  if (selected && catalog.materials[selected]) {
    // If user picked a built-in (e.g. "pla") and an auto-calibrated copy exists, use that copy.
    if (!selected.startsWith("my_") && catalog.materials[`my_cal_${selected}`]) return `my_cal_${selected}`;
    return selected;
  }
  const profMat = ctx?.store?.get().profile?.material;
  if (profMat && catalog.materials[profMat]) return profMat;
  return catalog.materials.my_cal_pla ? "my_cal_pla" : "pla";
}

function calContext() {
  const catalog = buildCatalog();
  const builtins = builtinsOf().materials;
  const matId = resolveCalMaterialId();
  const mat = catalog.materials[matId] ?? catalog.materials.pla;
  const spec = settings.mine.materials[matId] ?? null;
  const baseId = spec?.based_on ?? (matId in builtins ? matId : "pla");
  const base = builtins[baseId] ?? builtins.pla;
  const printer = activePrinterObj();
  const enc = printer?.enclosure || "none";
  return { catalog, matId, mat, spec, baseId, base, printer, enc };
}

function ensureCalSpec(matId, baseId, base) {
  if (settings.mine.materials[matId]) return { id: matId, spec: structuredClone(settings.mine.materials[matId]) };
  const id = `my_cal_${baseId}`;
  const existing = settings.mine.materials[id];
  if (existing) return { id, spec: structuredClone(existing) };
  return { id, spec: { based_on: baseId, name: `My ${base.name}` } };
}

function cleanupEmptyCalSpec(id) {
  const spec = settings.mine.materials[id];
  if (!spec || !id.startsWith("my_cal_")) return;
  const keys = Object.keys(spec).filter((k) => k !== "based_on" && k !== "name");
  if (!keys.length) {
    const baseId = spec.based_on || "pla";
    delete settings.mine.materials[id];
    if (settings.calibrationMaterial === id) settings.calibrationMaterial = baseId;
    const p = ctx?.store?.get().profile;
    if (p?.material === id) ctx.store.set({ profile: { ...p, material: baseId } }, { undoable: false });
  }
}

function saveCalibrationPatch(patch, { starterNote = null } = {}) {
  const { matId, baseId, base } = calContext();
  const { id, spec } = ensureCalSpec(matId, baseId, base);
  const converted = Object.keys(patch).length ? toSpec("materials", { based_on: spec.based_on, name: spec.name, ...patch }) : {};
  const mergedFits = spec.fit_gap_mm || converted.fit_gap_mm ? { fit_gap_mm: { ...spec.fit_gap_mm, ...converted.fit_gap_mm } } : {};
  settings.mine.materials[id] = { ...spec, ...converted, ...mergedFits };
  if (starterNote) {
    settings.starterChecked[id] = { note: starterNote, measuredAt: Date.now() };
  }
  settings.calibrationMaterial = id;
  const p = ctx?.store?.get().profile;
  if (p && (p.material === baseId || p.material === matId || p.material === id)) {
    ctx.store.set({ profile: { ...p, material: id } }, { undoable: false });
  }
  persistAndSync();
}

function clearCalibrationField(field) {
  const { matId } = calContext();
  const spec = settings.mine.materials[matId];
  if (field === "starter") {
    delete settings.starterChecked[matId];
    if (spec) {
      delete spec.hole_error_mm;
      delete spec.outer_error_mm;
    }
  } else if (field === "fit_snug" && spec?.fit_gap_mm) {
    delete spec.fit_gap_mm.snug;
    delete spec.fit_gap_mm.press;
    if (!Object.keys(spec.fit_gap_mm).length) delete spec.fit_gap_mm;
  } else if (field === "fit_sliding" && spec?.fit_gap_mm) {
    delete spec.fit_gap_mm.sliding;
    delete spec.fit_gap_mm.loose;
    if (!Object.keys(spec.fit_gap_mm).length) delete spec.fit_gap_mm;
  } else if (spec) {
    delete spec[field];
  }
  cleanupEmptyCalSpec(matId);
  persistAndSync();
}

// ---------- Tab 1: Printers ----------

async function loadOrcaPrinters() {
  orcaPrintersCache ??= fetch("../core/fdmcore/data/orca_printers.json")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error("List unavailable"))))
    .then((d) => d.printers)
    .catch((err) => { orcaPrintersCache = null; throw err; });
  return orcaPrintersCache;
}

function printerTraits(p) {
  const parts = [t("settings.printers.bedSummary", { x: Math.round(p.bed[0]), y: Math.round(p.bed[1]), z: Math.round(p.bed[2]) })];
  const encKey = p.enclosure === "heated" ? "settings.printers.encHeated" : p.enclosure === "passive" ? "settings.printers.encPassive" : "settings.printers.encNone";
  parts.push(t(encKey));
  if (p.hardened_nozzle) parts.push(t("settings.printers.hardened"));
  if (p.moving_bed) parts.push(t("settings.printers.bedSlinger"));
  return parts.join(" · ");
}

function togglePrinterNozzle(printerId, size, enabled) {
  const catalog = buildCatalog();
  const current = catalog.printers[printerId]?.nozzles ?? [...STANDARD_NOZZLES];
  const next = enabled ? normalizeNozzles([...current, size]) : current.filter((n) => n !== size);
  if (!next.length) {
    showToast({ message: t("settings.printers.keepOneNozzle"), tone: "warn" });
    ctx?.services?.rerender?.();
    return;
  }
  settings.printerNozzles[printerId] = next;
  pendingFocusSelector = `[data-nozzle-chip="${printerId}:${size}"]`;
  persistAndSync({ sync: false });
}

function renderPrinterCard(id, p, isCustom) {
  const sourceKey = p.source === "imported" ? "settings.source.imported" : isCustom ? "settings.source.custom" : "settings.source.builtin";
  const sourceClass = p.source === "imported" ? "set-badge--imported" : isCustom ? "set-badge--custom" : "set-badge--builtin";
  const chips = STANDARD_NOZZLES.map((size) => {
    const chip = ToggleChip({
      label: t("settings.printers.nozzleMm", { size }),
      pressed: (p.nozzles ?? STANDARD_NOZZLES).includes(size),
      onChange: (on) => togglePrinterNozzle(id, size, on),
    });
    chip.dataset.nozzleChip = `${id}:${size}`;
    return chip;
  });
  return h("article", { class: "set-card", dataset: { printerId: id } },
    h("div", { class: "set-card-head" },
      h("span", { class: "set-card-title" }, p.name),
      h("span", { class: ["set-badge", sourceClass] }, t(sourceKey)),
      h("div", { class: "set-card-actions" },
        Button({
          label: t("settings.edit"),
          variant: "ghost",
          size: "small",
          onClick: () => {
            const values = toForm("printers", p);
            values.based_on = p.based_on ?? id;
            view.printerMode = {
              kind: "edit",
              id: isCustom ? id : null,
              values,
              nozzles: [...(p.nozzles ?? STANDARD_NOZZLES)],
              source: isCustom ? (p.source ?? "custom") : "custom",
            };
            ctx?.services?.rerender?.();
          },
        }),
        isCustom && Button({
          label: t("settings.remove"),
          variant: "ghost",
          size: "small",
          onClick: () => {
            const name = p.name;
            delete settings.mine.printers[id];
            delete settings.printerNozzles[id];
            if (ctx?.store?.get().profile?.printer === id) {
              ctx.store.set({ profile: { ...ctx.store.get().profile, printer: null } }, { undoable: false });
            }
            persistAndSync();
            showToast({ message: t("settings.printers.removed", { name }), tone: "info" });
          },
        }))),
    h("div", { class: "set-card-meta num" }, printerTraits(p)),
    h("div", { class: "set-nozzle-row" }, h("span", { class: "ui-label" }, t("settings.printers.nozzlesLabel")), ...chips));
}

function renderOrcaPicker() {
  const mode = view.printerMode;
  const builtins = builtinsOf().printers;
  const card = h("section", { class: "set-card set-card--editor" },
    h("div", { class: "set-card-head" }, h("h2", { class: "set-card-title" }, t("settings.printers.orcaTitle"))),
    h("p", { class: "hint" }, t("settings.printers.orcaHint")));

  if (mode.loading) {
    card.append(h("p", { class: "set-note" }, t("settings.printers.orcaLoading")));
    return card;
  }
  if (mode.error) {
    card.append(h("p", { class: "set-note set-note--warn" }, mode.error),
      h("div", { class: "set-toolbar" },
        Button({ label: t("settings.printers.notListed"), variant: "primary", onClick: () => openHelperMode() }),
        Button({ label: t("settings.cancel"), variant: "ghost", onClick: () => { view.printerMode = null; ctx?.services?.rerender?.(); } })));
    return card;
  }
  const list = mode.list ?? [];
  const options = list.map((p) => ({ value: p.name, label: p.name, hint: `${p.bed[0]}×${p.bed[1]}×${p.bed[2]}` }));
  const pickedName = mode.picked ?? options[0]?.value ?? "";
  const enc = mode.enclosure ?? "none";

  const selectEl = Select({
    label: t("settings.printers.orcaPick"),
    options,
    value: pickedName,
    searchable: true,
    onChange: (v) => { mode.picked = v; },
  });
  const encSeg = SegmentedControl({
    label: t("settings.printers.fieldEnclosure"),
    value: enc,
    options: [
      { value: "none", label: t("settings.printers.encNone") },
      { value: "passive", label: t("settings.printers.encPassive") },
      { value: "heated", label: t("settings.printers.encHeated") },
    ],
    onChange: (v) => { mode.enclosure = v; },
  });

  card.append(
    h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.printers.orcaPick")), selectEl),
    h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.printers.fieldEnclosure")), encSeg),
    h("div", { class: "set-toolbar" },
      Button({
        label: t("settings.printers.orcaUse"),
        variant: "primary",
        onClick: () => {
          const entry = list.find((p) => p.name === (mode.picked || pickedName));
          if (!entry) return;
          const { values, note } = fromOrcaPrinter(entry, builtins, mode.enclosure || "none");
          const nozzles = normalizeNozzles(entry.nozzles);
          view.printerMode = {
            kind: "edit",
            id: null,
            values,
            note: t(note),
            nozzles: nozzles.length ? nozzles : ["0.4"],
            source: "imported",
          };
          ctx?.services?.rerender?.();
        },
      }),
      Button({ label: t("settings.cancel"), variant: "ghost", onClick: () => { view.printerMode = null; ctx?.services?.rerender?.(); } })));
  return card;
}

function openHelperMode() {
  view.printerMode = { kind: "helper", name: t("printer.myPrinter"), enclosure: "none", side: 220, directDrive: "yes" };
  ctx?.services?.rerender?.();
}

function renderNotListedHelper() {
  const mode = view.printerMode;
  const builtins = builtinsOf().printers;
  const nameId = uid("pr-help-name");
  const nameInput = h("input", { id: nameId, type: "text", class: "set-input", value: mode.name ?? "" });
  nameInput.addEventListener("input", () => { mode.name = nameInput.value; });

  const encSeg = SegmentedControl({
    label: t("settings.printers.fieldEnclosure"),
    value: mode.enclosure ?? "none",
    options: [
      { value: "none", label: t("settings.printers.encNone") },
      { value: "passive", label: t("settings.printers.encPassive") },
      { value: "heated", label: t("settings.printers.encHeated") },
    ],
    onChange: (v) => { mode.enclosure = v; },
  });
  const bedField = NumberField({
    label: t("settings.printers.helperBed"),
    value: mode.side ?? 220,
    min: 50,
    max: 1500,
    units: [{ value: "mm", label: "mm" }],
    onChange: (n) => { if (n != null) mode.side = n; },
  });
  const driveSeg = SegmentedControl({
    label: t("settings.printers.helperDrive"),
    value: mode.directDrive ?? "yes",
    options: [
      { value: "yes", label: t("settings.printers.driveDirect") },
      { value: "no", label: t("settings.printers.driveBowden") },
    ],
    onChange: (v) => { mode.directDrive = v; },
  });

  return h("section", { class: "set-card set-card--editor" },
    h("div", { class: "set-card-head" }, h("h2", { class: "set-card-title" }, t("settings.printers.helperTitle"))),
    h("p", { class: "hint" }, t("settings.printers.helperHint")),
    h("div", { class: "set-field" }, h("label", { class: "ui-label", for: nameId }, t("settings.printers.helperName")), nameInput),
    h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.printers.fieldEnclosure")), encSeg),
    h("div", { class: "set-grid-2" }, bedField,
      h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.printers.helperDrive")), driveSeg)),
    h("div", { class: "set-toolbar" },
      Button({
        label: t("settings.printers.helperStart"),
        variant: "primary",
        onClick: () => {
          const side = Number(mode.side);
          if (!(side >= 50 && side <= 1500)) {
            showToast({ message: t("printer.enterBed"), tone: "warn" });
            return;
          }
          const { values, note } = fromAnswers(builtins, {
            enclosure: mode.enclosure || "none",
            side,
            directDrive: mode.directDrive !== "no",
            name: (mode.name || "").trim() || t("printer.myPrinter"),
          });
          view.printerMode = {
            kind: "edit",
            id: null,
            values,
            note: t(note),
            nozzles: [...STANDARD_NOZZLES],
            source: "custom",
          };
          ctx?.services?.rerender?.();
        },
      }),
      Button({ label: t("settings.cancel"), variant: "ghost", onClick: () => { view.printerMode = null; ctx?.services?.rerender?.(); } })));
}

function renderPrinterEditor() {
  const mode = view.printerMode;
  const builtins = builtinsOf().printers;
  const v = mode.values;
  const nameId = uid("pr-name");
  const nameInput = h("input", { id: nameId, type: "text", class: "set-input", value: v.name ?? "" });
  nameInput.addEventListener("input", () => { v.name = nameInput.value; });

  const baseSelect = Select({
    label: t("settings.printers.fieldBase"),
    value: v.based_on || "bambu_p1s",
    options: Object.entries(builtins).map(([id, p]) => ({ value: id, label: p.name })),
    searchable: true,
    onChange: (baseId) => {
      const keepName = v.name;
      Object.assign(v, toForm("printers", builtins[baseId]), { based_on: baseId, name: keepName });
      ctx?.services?.rerender?.();
    },
  });

  const bedX = NumberField({ label: t("settings.printers.fieldBedX"), value: v.bed_x ?? 256, min: 20, max: 2000, units: [{ value: "mm", label: "mm" }], onChange: (n) => { if (n != null) v.bed_x = n; } });
  const bedY = NumberField({ label: t("settings.printers.fieldBedY"), value: v.bed_y ?? 256, min: 20, max: 2000, units: [{ value: "mm", label: "mm" }], onChange: (n) => { if (n != null) v.bed_y = n; } });
  const bedZ = NumberField({ label: t("settings.printers.fieldBedZ"), value: v.bed_z ?? 256, min: 20, max: 2000, units: [{ value: "mm", label: "mm" }], onChange: (n) => { if (n != null) v.bed_z = n; } });

  const encSeg = SegmentedControl({
    label: t("settings.printers.fieldEnclosure"),
    value: v.enclosure || "none",
    options: [
      { value: "none", label: t("settings.printers.encNone") },
      { value: "passive", label: t("settings.printers.encPassive") },
      { value: "heated", label: t("settings.printers.encHeated") },
    ],
    onChange: (val) => { v.enclosure = val; },
  });
  const plateSelect = Select({
    label: t("settings.printers.fieldPlate"),
    value: v.bed_surface || "unknown",
    searchable: false,
    options: [
      { value: "unknown", label: t("settings.printers.plateUnknown") },
      { value: "textured_pei", label: t("settings.printers.plateTextured") },
      { value: "smooth_pei", label: t("settings.printers.plateSmooth") },
      { value: "glass", label: t("settings.printers.plateGlass") },
      { value: "cool_plate", label: t("settings.printers.plateCool") },
    ],
    onChange: (val) => { v.bed_surface = val; },
  });
  const speedSeg = SegmentedControl({
    label: t("settings.printers.fieldSpeed"),
    value: v.speed || "fast",
    options: [
      { value: "fast", label: t("settings.printers.speedFast") },
      { value: "medium", label: t("settings.printers.speedMedium") },
    ],
    onChange: (val) => { v.speed = val; },
  });
  const multiSeg = SegmentedControl({
    label: t("settings.printers.fieldMulti"),
    value: v.multi_material || "none",
    options: [
      { value: "none", label: t("settings.printers.multiNone") },
      { value: "optional", label: t("settings.printers.multiOptional") },
      { value: "builtin", label: t("settings.printers.multiBuiltin") },
    ],
    onChange: (val) => { v.multi_material = val; },
  });

  const hardenedChip = ToggleChip({ label: t("settings.printers.hardened"), pressed: Boolean(v.hardened_nozzle), onChange: (on) => { v.hardened_nozzle = on; } });
  const slingerChip = ToggleChip({ label: t("settings.printers.bedSlinger"), pressed: Boolean(v.moving_bed), onChange: (on) => { v.moving_bed = on; } });
  const nozzleChips = STANDARD_NOZZLES.map((size) => ToggleChip({
    label: t("settings.printers.nozzleMm", { size }),
    pressed: mode.nozzles.includes(size),
    onChange: (on) => {
      const next = on ? normalizeNozzles([...mode.nozzles, size]) : mode.nozzles.filter((n) => n !== size);
      if (next.length) mode.nozzles = next;
      else showToast({ message: t("settings.printers.keepOneNozzle"), tone: "warn" });
    },
  }));

  return h("section", { class: "set-card set-card--editor" },
    h("div", { class: "set-card-head" }, h("h2", { class: "set-card-title" }, mode.id ? t("settings.printers.editorEdit") : t("settings.printers.editorNew"))),
    mode.note ? h("p", { class: "set-note" }, mode.note) : null,
    h("div", { class: "set-grid-2" },
      h("div", { class: "set-field" }, h("label", { class: "ui-label", for: nameId }, t("settings.printers.fieldName")), nameInput),
      h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.printers.fieldBase")), baseSelect)),
    h("div", { class: "set-grid-3" }, bedX, bedY, bedZ),
    h("div", { class: "set-grid-2" },
      h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.printers.fieldEnclosure")), encSeg),
      h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.printers.fieldPlate")), plateSelect)),
    h("div", { class: "set-grid-2" },
      h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.printers.fieldSpeed")), speedSeg),
      h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.printers.fieldMulti")), multiSeg)),
    h("div", { class: "set-chips" }, hardenedChip, slingerChip),
    h("div", { class: "set-nozzle-row" }, h("span", { class: "ui-label" }, t("settings.printers.nozzlesLabel")), ...nozzleChips),
    h("div", { class: "set-toolbar" },
      Button({
        label: t("settings.printers.savePrinter"),
        variant: "primary",
        onClick: () => {
          const cleanName = String(v.name ?? "").trim();
          if (!cleanName) {
            showToast({ message: t("settings.printers.needName"), tone: "warn" });
            nameInput.focus();
            return;
          }
          const slug = cleanName.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 24) || "printer";
          const id = mode.id || normalizeId(`${slug}_${Date.now().toString(36).slice(-4)}`);
          const spec = toSpec("printers", { ...v, name: cleanName });
          if (mode.source === "imported") spec.source = "imported";
          settings.mine.printers[id] = spec;
          settings.printerNozzles[id] = mode.nozzles.length ? normalizeNozzles(mode.nozzles) : [...STANDARD_NOZZLES];
          view.printerMode = null;
          persistAndSync();
          showToast({ message: t("settings.printers.saved", { name: cleanName }), tone: "good" });
        },
      }),
      Button({ label: t("settings.cancel"), variant: "ghost", onClick: () => { view.printerMode = null; ctx?.services?.rerender?.(); } })));
}

function renderPrintersTab() {
  const catalog = buildCatalog();
  const builtins = builtinsOf().printers;
  const customIds = Object.keys(settings.mine.printers);
  const query = view.printerFilter.trim().toLowerCase();
  const builtinEntries = Object.entries(catalog.printers)
    .filter(([id]) => !id.startsWith("my_"))
    .filter(([, p]) => !query || p.name.toLowerCase().includes(query));

  const toolbar = h("div", { class: "set-toolbar" },
    Button({
      label: t("settings.printers.addOrca"),
      variant: "secondary",
      onClick: () => {
        view.printerMode = { kind: "orca", loading: true, enclosure: "none" };
        ctx?.services?.rerender?.();
        loadOrcaPrinters()
          .then((list) => {
            if (view.printerMode?.kind !== "orca") return;
            view.printerMode = { kind: "orca", loading: false, list, picked: list[0]?.name ?? "", enclosure: "none" };
            ctx?.services?.rerender?.();
          })
          .catch(() => {
            if (view.printerMode?.kind !== "orca") return;
            view.printerMode = { kind: "orca", loading: false, error: t("printer.listFailed") };
            ctx?.services?.rerender?.();
          });
      },
    }),
    Button({ label: t("settings.printers.notListed"), variant: "ghost", onClick: () => openHelperMode() }),
    Button({
      label: t("settings.printers.addCustom"),
      variant: "ghost",
      onClick: () => {
        const baseId = ctx?.store?.get().profile?.printer?.replace(/^my_.*/, "") || "bambu_p1s";
        const useBase = builtins[baseId] ? baseId : "bambu_p1s";
        view.printerMode = {
          kind: "edit",
          id: null,
          values: { ...toForm("printers", builtins[useBase]), based_on: useBase, name: "" },
          nozzles: [...STANDARD_NOZZLES],
          source: "custom",
        };
        ctx?.services?.rerender?.();
      },
    }));

  const parts = [toolbar];
  if (view.printerMode?.kind === "orca") parts.push(renderOrcaPicker());
  else if (view.printerMode?.kind === "helper") parts.push(renderNotListedHelper());
  else if (view.printerMode?.kind === "edit") parts.push(renderPrinterEditor());

  if (customIds.length) {
    parts.push(h("div", { class: "set-section" },
      h("h2", { class: "ui-label" }, t("settings.printers.mine")),
      h("div", { class: "set-list" }, customIds.map((id) => renderPrinterCard(id, catalog.printers[id], true)))));
  }

  const filterInput = h("input", {
    type: "text",
    class: "set-input set-search",
    placeholder: t("settings.printers.search"),
    "aria-label": t("settings.printers.search"),
    value: view.printerFilter,
    dataset: { focusId: "printer-filter" },
  });
  filterInput.addEventListener("input", () => {
    view.printerFilter = filterInput.value;
    pendingFocusSelector = '[data-focus-id="printer-filter"]';
    ctx?.services?.rerender?.();
  });

  parts.push(h("div", { class: "set-section" },
    h("div", { class: "set-section-head" },
      h("h2", { class: "ui-label" }, t("settings.printers.builtin")),
      filterInput),
    h("div", { class: "set-list" }, builtinEntries.map(([id, p]) => renderPrinterCard(id, p, false)))));

  return parts;
}

// ---------- Tab 2: Print profiles ----------

async function loadOrcaProfiles() {
  if (orcaProfilesCache) return orcaProfilesCache;
  const response = await fetch("../core/fdmcore/data/orca_profiles.json.gz");
  if (!response.ok) throw new Error("Profiles unavailable");
  const parsed = await new Response(response.body.pipeThrough(new DecompressionStream("gzip"))).json();
  orcaProfilesCache = parsed.profiles ?? {};
  return orcaProfilesCache;
}

function detectProfileConflicts(values) {
  const conflicts = [];
  const nozzleStr = ctx?.store?.get().profile?.nozzle || "0.4";
  const nozzle = Number(nozzleStr) || 0.4;
  const printer = activePrinterObj();
  const { mat, spec } = calContext();

  if (values.layer_height != null) {
    const lh = parseFloat(values.layer_height);
    if (Number.isFinite(lh) && lh > nozzle * 0.75 + 1e-6) {
      conflicts.push(t("settings.profiles.conflictLayerHigh", { val: lh, nozzle: nozzleStr }));
    } else if (Number.isFinite(lh) && lh < 0.05) {
      conflicts.push(t("settings.profiles.conflictLayerLow", { val: lh }));
    }
  }
  if (values.support_threshold_angle != null && spec?.max_overhang_deg != null) {
    const angle = parseFloat(values.support_threshold_angle);
    if (Number.isFinite(angle) && Math.abs(angle - mat.max_overhang_deg) >= 10) {
      conflicts.push(t("settings.profiles.conflictOverhang", { val: angle, cal: mat.max_overhang_deg }));
    }
  }
  if (values.outer_wall_speed != null && printer?.speed === "medium") {
    const spd = parseFloat(values.outer_wall_speed);
    if (Number.isFinite(spd) && spd > 250) {
      conflicts.push(t("settings.profiles.conflictSpeed", { val: spd }));
    }
  }
  if (values.wall_loops != null) {
    const walls = parseFloat(values.wall_loops);
    if (Number.isFinite(walls) && (walls < 1 || walls > 20)) {
      conflicts.push(t("settings.profiles.conflictWalls", { val: walls }));
    }
  }
  return conflicts;
}

async function handleProfileFile(file) {
  view.profileError = "";
  try {
    const text = await file.text();
    const isIni = /\.(ini|cfg)$/i.test(file.name);
    let profiles = {};
    if (!isIni) {
      const parsed = JSON.parse(text);
      if (parsed?.inherits) {
        try { profiles = await loadOrcaProfiles(); } catch { /* report missing parent cleanly */ }
      }
    }
    const imported = fromPrintProfile(text, isIni, profiles);
    if (!Object.keys(imported.values).length) {
      throw new Error(t("settings.profiles.noValues"));
    }
    const conflicts = detectProfileConflicts(imported.values);
    if (engineReady && ctx?.engine) {
      try {
        const p = ctx.store.get().profile;
        const res = await ctx.engine.call("setup_check", {
          profile: {
            printer: p.printer || "bambu_p1s",
            material: p.material || resolveCalMaterialId(),
            nozzle: p.nozzle || "0.4",
          },
          settings: imported.values,
        });
        for (const f of res?.findings ?? []) {
          if (f.title && !conflicts.includes(f.title)) conflicts.push(f.title);
        }
      } catch { /* keep local conflict checks */ }
    }
    view.profilePreview = { ...imported, conflicts };
  } catch (err) {
    view.profilePreview = null;
    view.profileError = t("settings.profiles.readFailed", { reason: err.message });
  }
  ctx?.services?.rerender?.();
}

function renderProfileKv(values) {
  return h("div", { class: "prof-kv" },
    Object.entries(values).map(([k, v]) =>
      h("div", { class: "prof-kv-row" },
        h("span", {}, PRINT_KEY_LABELS[k] ?? k),
        h("b", {}, String(v)))));
}

function renderProfilesTab() {
  const input = h("input", { type: "file", accept: ".json,.ini,.cfg", class: "visually-hidden", tabindex: "-1", "aria-hidden": "true" });
  input.addEventListener("change", () => {
    const file = input.files[0];
    input.value = "";
    if (file) handleProfileFile(file);
  });

  const drop = h("div", { class: "prof-drop" },
    h("span", { class: "prof-drop-title" }, t("settings.profiles.drop")),
    Button({ label: t("settings.profiles.choose"), variant: "secondary", onClick: () => input.click() }),
    h("span", { class: "ui-hint" }, t("settings.profiles.formats")),
    input);
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("is-over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("is-over");
    const f = e.dataTransfer?.files?.[0];
    if (f) handleProfileFile(f);
  });

  const parts = [drop];
  if (view.profileError) parts.push(h("p", { class: "set-note set-note--error", role: "alert" }, view.profileError));

  if (view.profilePreview) {
    const pr = view.profilePreview;
    parts.push(h("section", { class: "set-card set-card--editor" },
      h("div", { class: "set-card-head" },
        h("h2", { class: "set-card-title" }, `${t("settings.profiles.preview")}: ${pr.name}`),
        h("span", { class: "set-badge set-badge--imported" }, t("settings.profiles.valuesCount", { count: Object.keys(pr.values).length }))),
      pr.missingParent ? h("p", { class: "set-note set-note--warn" }, t("settings.profiles.missingParent", { parent: pr.missingParent })) : null,
      h("div", { class: "prof-lists" },
        h("div", { class: "set-section" }, h("span", { class: "ui-label" }, t("settings.profiles.recognised")), renderProfileKv(pr.values)),
        h("div", { class: "set-section" },
          h("span", { class: "ui-label" }, t("settings.profiles.missing")),
          pr.missing.length
            ? h("p", { class: "hint" }, pr.missing.join(", "))
            : h("p", { class: "hint" }, t("settings.profiles.missingNone"))),
        h("div", { class: "set-section" },
          h("span", { class: "ui-label" }, t("settings.profiles.conflicts")),
          pr.conflicts.length
            ? h("ul", { class: "prof-bullets prof-bullets--warn" }, pr.conflicts.map((c) => h("li", {}, c)))
            : h("p", { class: "hint" }, t("settings.profiles.conflictsNone")))),
      h("div", { class: "set-toolbar" },
        Button({
          label: t("settings.profiles.saveBaseline"),
          variant: "primary",
          onClick: () => {
            settings.printProfile = {
              name: pr.name,
              values: { ...pr.values },
              missingParent: pr.missingParent,
              missing: [...pr.missing],
            };
            view.profilePreview = null;
            persistAndSync({ sync: false });
            showToast({ message: t("settings.profiles.saved", { name: settings.printProfile.name }), tone: "good" });
          },
        }),
        Button({ label: t("settings.cancel"), variant: "ghost", onClick: () => { view.profilePreview = null; ctx?.services?.rerender?.(); } }))));
  }

  if (settings.printProfile) {
    const saved = settings.printProfile;
    parts.push(h("section", { class: "set-card" },
      h("div", { class: "set-card-head" },
        h("span", { class: "set-card-title" }, saved.name),
        h("span", { class: "set-badge set-badge--imported" }, t("settings.profiles.active")),
        h("div", { class: "set-card-actions" },
          Button({
            label: t("settings.profiles.remove"),
            variant: "ghost",
            size: "small",
            onClick: () => {
              settings.printProfile = null;
              persistAndSync({ sync: false });
              showToast({ message: t("settings.profiles.removed"), tone: "info" });
            },
          }))),
      h("p", { class: "set-card-meta" }, t("settings.profiles.valuesCount", { count: Object.keys(saved.values).length })),
      Disclosure({
        label: t("settings.profiles.savedValues"),
        content: renderProfileKv(saved.values),
      })));
  }

  parts.push(Disclosure({
    label: t("settings.profiles.rulesMore"),
    content: h("p", {}, t("settings.profiles.rulesBody")),
  }));

  return parts;
}

// ---------- Tab 3: Materials ----------

function spoolSummary(m) {
  const price = m.price_per_kg != null && m.price_per_kg !== "" ? m.price_per_kg : null;
  const grams = m.spool_left_g != null && m.spool_left_g !== "" ? m.spool_left_g : null;
  if (price != null && grams != null) return t("settings.materials.spoolMeta", { price, grams });
  if (price != null) return t("settings.materials.priceOnly", { price });
  if (grams != null) return t("settings.materials.gramsOnly", { grams });
  return "";
}

function toggleOwnedMaterial(id, owned) {
  const set = new Set(settings.owned);
  if (owned) set.add(id);
  else set.delete(id);
  settings.owned = [...set];
  pendingFocusSelector = `[data-owned-chip="${id}"]`;
  persistAndSync({ sync: false });
}

function saveSpoolExtras(id, price, grams) {
  const extra = {};
  if (price != null && Number.isFinite(price)) extra.price_per_kg = price;
  if (grams != null && Number.isFinite(grams)) extra.spool_left_g = grams;
  if (Object.keys(extra).length) settings.materialExtras[id] = extra;
  else delete settings.materialExtras[id];
  if (settings.mine.materials[id]) {
    if (extra.price_per_kg != null) settings.mine.materials[id].price_per_kg = extra.price_per_kg;
    else delete settings.mine.materials[id].price_per_kg;
    if (extra.spool_left_g != null) settings.mine.materials[id].spool_left_g = extra.spool_left_g;
    else delete settings.mine.materials[id].spool_left_g;
  }
  persistAndSync({ sync: Boolean(settings.mine.materials[id]) });
  showToast({ message: t("settings.saved"), tone: "good" });
}

function renderMaterialEditor() {
  const ed = view.materialEditor;
  const builtins = builtinsOf().materials;
  const v = ed.values;
  const nameId = uid("mat-name");
  const nameInput = h("input", { id: nameId, type: "text", class: "set-input", value: v.name ?? "" });
  nameInput.addEventListener("input", () => { v.name = nameInput.value; });

  const baseSelect = Select({
    label: t("settings.materials.fieldBase"),
    value: v.based_on || "pla",
    options: Object.entries(builtins).map(([id, m]) => ({ value: id, label: m.name })),
    onChange: (baseId) => {
      const keepName = v.name;
      Object.assign(v, toForm("materials", builtins[baseId]), { based_on: baseId, name: keepName });
      ctx?.services?.rerender?.();
    },
  });

  const overhang = NumberField({ label: t("settings.materials.fieldOverhang"), value: v.max_overhang_deg ?? 55, min: 20, max: 75, units: [{ value: "deg", label: "°" }], onChange: (n) => { if (n != null) v.max_overhang_deg = n; } });
  const bridge = NumberField({ label: t("settings.materials.fieldBridge"), value: v.max_bridge_mm ?? 35, min: 1, max: 150, units: [{ value: "mm", label: "mm" }], onChange: (n) => { if (n != null) v.max_bridge_mm = n; } });
  const price = NumberField({ label: t("settings.materials.price"), value: v.price_per_kg ?? "", min: 0, max: 10000, units: [{ value: "kg", label: t("settings.materials.priceUnit") }], onChange: (n) => { v.price_per_kg = n ?? ""; } });
  const grams = NumberField({ label: t("settings.materials.gramsLeft"), value: v.spool_left_g ?? "", min: 0, max: 100000, units: [{ value: "g", label: t("settings.materials.gramsUnit") }], onChange: (n) => { v.spool_left_g = n ?? ""; } });

  const layerTime = NumberField({ label: t("settings.materials.fieldLayerTime"), value: v.min_layer_time_s ?? 8, min: 0, max: 60, units: [{ value: "s", label: "s" }], onChange: (n) => { if (n != null) v.min_layer_time_s = n; } });
  const flow = NumberField({ label: t("settings.materials.fieldFlow"), value: v.max_flow_mm3s ?? 15, min: 1, max: 100, units: [{ value: "mm3s", label: "mm³/s" }], onChange: (n) => { if (n != null) v.max_flow_mm3s = n; } });
  const density = NumberField({ label: t("settings.materials.fieldDensity"), value: v.density_g_cm3 ?? 1.24, min: 0.5, max: 3, units: [{ value: "g", label: "g/cm³" }], onChange: (n) => { if (n != null) v.density_g_cm3 = n; } });
  const shrinkage = NumberField({ label: t("settings.materials.fieldShrinkage"), value: v.shrinkage_pct ?? 0.3, min: 0, max: 5, units: [{ value: "pct", label: "%" }], onChange: (n) => { if (n != null) v.shrinkage_pct = n; } });
  const tensile = NumberField({ label: t("settings.materials.fieldTensile"), value: v.tensile_xy_mpa ?? 50, min: 1, max: 300, units: [{ value: "mpa", label: "MPa" }], onChange: (n) => { if (n != null) v.tensile_xy_mpa = n; } });

  const warpSeg = SegmentedControl({
    label: t("settings.materials.fieldWarp"),
    value: v.warp_risk || "low",
    options: [
      { value: "low", label: t("settings.materials.warpLow") },
      { value: "medium", label: t("settings.materials.warpMedium") },
      { value: "high", label: t("settings.materials.warpHigh") },
    ],
    onChange: (val) => { v.warp_risk = val; },
  });
  const fibreSeg = SegmentedControl({
    label: t("settings.materials.fieldFibre"),
    value: v.fibre || "none",
    options: [
      { value: "none", label: t("settings.materials.fibreNone") },
      { value: "carbon", label: t("settings.materials.fibreCarbon") },
      { value: "glass", label: t("settings.materials.fibreGlass") },
    ],
    onChange: (val) => { v.fibre = val; },
  });

  return h("section", { class: "set-card set-card--editor" },
    h("div", { class: "set-card-head" }, h("h2", { class: "set-card-title" }, ed.id ? t("settings.materials.editorEdit") : t("settings.materials.editorNew"))),
    h("div", { class: "set-grid-2" },
      h("div", { class: "set-field" }, h("label", { class: "ui-label", for: nameId }, t("settings.materials.fieldName")), nameInput),
      h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.materials.fieldBase")), baseSelect)),
    h("div", { class: "set-grid-2" }, price, grams),
    h("div", { class: "set-grid-2" }, overhang, bridge),
    Disclosure({
      label: t("ui.more"),
      content: h("div", { class: "set-section" },
        h("div", { class: "set-grid-3" }, layerTime, flow, density),
        h("div", { class: "set-grid-2" }, shrinkage, tensile),
        h("div", { class: "set-grid-2" },
          h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.materials.fieldWarp")), warpSeg),
          h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.materials.fieldFibre")), fibreSeg))),
    }),
    h("div", { class: "set-toolbar" },
      Button({
        label: t("settings.materials.saveMaterial"),
        variant: "primary",
        onClick: () => {
          const cleanName = String(v.name ?? "").trim();
          if (!cleanName) {
            showToast({ message: t("settings.materials.needName"), tone: "warn" });
            nameInput.focus();
            return;
          }
          const slug = cleanName.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 24) || "filament";
          const id = ed.id || normalizeId(`${slug}_${Date.now().toString(36).slice(-4)}`);
          settings.mine.materials[id] = toSpec("materials", { ...v, name: cleanName });
          if (!settings.owned.includes(id)) settings.owned = [...settings.owned, id];
          view.materialEditor = null;
          persistAndSync();
          showToast({ message: t("settings.materials.saved", { name: cleanName }), tone: "good" });
        },
      }),
      Button({ label: t("settings.cancel"), variant: "ghost", onClick: () => { view.materialEditor = null; ctx?.services?.rerender?.(); } })));
}

function renderMaterialRow(id, m, isCustom) {
  const owned = settings.owned.includes(id);
  const chip = ToggleChip({
    label: t("settings.materials.ownedBadge"),
    pressed: owned,
    onChange: (on) => toggleOwnedMaterial(id, on),
  });
  chip.setAttribute("aria-label", t("settings.materials.ownToggle", { name: m.name }));
  chip.dataset.ownedChip = id;

  const badgeKey = m.source === "measured" ? "settings.source.measured" : isCustom ? "settings.source.custom" : "settings.source.builtin";
  const badgeClass = m.source === "measured" ? "set-badge--measured" : isCustom ? "set-badge--custom" : "set-badge--builtin";
  const spool = spoolSummary(m);

  const row = h("article", { class: "mat-row", dataset: { materialId: id } },
    h("div", { class: "mat-row-top" },
      chip,
      h("div", { class: "mat-row-info" },
        h("span", { class: "mat-row-name" }, m.name, h("span", { class: ["set-badge", badgeClass] }, t(badgeKey))),
        h("span", { class: "set-card-meta num" }, t("settings.materials.meta", { overhang: m.max_overhang_deg, bridge: m.max_bridge_mm }))),
      spool ? h("span", { class: "mat-row-spool" }, spool) : null,
      h("div", { class: "set-card-actions" },
        Button({
          label: t("settings.materials.spoolDetails"),
          variant: "ghost",
          size: "small",
          onClick: () => {
            view.expandedSpool = view.expandedSpool === id ? null : id;
            ctx?.services?.rerender?.();
          },
        }),
        isCustom && Button({
          label: t("settings.edit"),
          variant: "ghost",
          size: "small",
          onClick: () => {
            const values = toForm("materials", m);
            values.based_on = m.based_on ?? "pla";
            view.materialEditor = { id, values };
            ctx?.services?.rerender?.();
          },
        }),
        isCustom && Button({
          label: t("settings.remove"),
          variant: "ghost",
          size: "small",
          onClick: () => {
            const name = m.name;
            delete settings.mine.materials[id];
            delete settings.materialExtras[id];
            delete settings.starterChecked[id];
            settings.owned = settings.owned.filter((x) => x !== id);
            if (settings.calibrationMaterial === id) settings.calibrationMaterial = "pla";
            if (ctx?.store?.get().profile?.material === id) {
              ctx.store.set({ profile: { ...ctx.store.get().profile, material: "" } }, { undoable: false });
            }
            persistAndSync();
            showToast({ message: t("settings.materials.removed", { name }), tone: "info" });
          },
        }))));

  if (view.expandedSpool === id) {
    let priceVal = m.price_per_kg ?? null;
    let gramsVal = m.spool_left_g ?? null;
    const priceField = NumberField({
      label: t("settings.materials.price"),
      value: m.price_per_kg ?? "",
      min: 0,
      max: 10000,
      units: [{ value: "kg", label: t("settings.materials.priceUnit") }],
      onChange: (n) => { priceVal = n; },
    });
    const gramsField = NumberField({
      label: t("settings.materials.gramsLeft"),
      value: m.spool_left_g ?? "",
      min: 0,
      max: 100000,
      units: [{ value: "g", label: t("settings.materials.gramsUnit") }],
      onChange: (n) => { gramsVal = n; },
    });
    row.append(
      h("div", { class: "set-grid-2" }, priceField, gramsField),
      h("div", { class: "set-toolbar" },
        Button({
          label: t("settings.save"),
          variant: "secondary",
          size: "small",
          onClick: () => {
            view.expandedSpool = null;
            saveSpoolExtras(id, priceVal, gramsVal);
          },
        }),
        Button({ label: t("settings.cancel"), variant: "ghost", size: "small", onClick: () => { view.expandedSpool = null; ctx?.services?.rerender?.(); } })));
  }
  return row;
}

function renderMaterialsTab() {
  const catalog = buildCatalog();
  const builtins = builtinsOf().materials;
  const orcaInput = h("input", { type: "file", accept: ".json", class: "visually-hidden", tabindex: "-1", "aria-hidden": "true" });
  orcaInput.addEventListener("change", async () => {
    const file = orcaInput.files[0];
    orcaInput.value = "";
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text());
      const values = fromOrcaFilament(parsed, builtins);
      view.materialEditor = { id: null, values };
      showToast({ message: t("settings.materials.importLoaded", { name: values.name }), tone: "info" });
      ctx?.services?.rerender?.();
    } catch (err) {
      showToast({ message: t("settings.materials.importFailed", { reason: err.message }), tone: "bad" });
    }
  });

  const toolbar = h("div", { class: "set-toolbar" },
    Button({
      label: t("settings.materials.addCustom"),
      variant: "secondary",
      onClick: () => {
        view.materialEditor = {
          id: null,
          values: { ...toForm("materials", builtins.pla), based_on: "pla", name: "" },
        };
        ctx?.services?.rerender?.();
      },
    }),
    Button({ label: t("settings.materials.importOrca"), variant: "ghost", onClick: () => orcaInput.click() }),
    orcaInput);

  const parts = [toolbar];
  if (view.materialEditor) parts.push(renderMaterialEditor());

  const customEntries = Object.entries(catalog.materials).filter(([id]) => id.startsWith("my_"));
  const builtinEntries = Object.entries(catalog.materials).filter(([id]) => !id.startsWith("my_"));

  parts.push(h("div", { class: "set-section" },
    h("h2", { class: "ui-label" }, t("settings.materials.ownedTitle")),
    h("div", { class: "set-list" },
      [...customEntries.map(([id, m]) => renderMaterialRow(id, m, true)),
        ...builtinEntries.map(([id, m]) => renderMaterialRow(id, m, false))])));

  return parts;
}

// ---------- Tab 4: Calibration tests ----------

const CAL_TESTS = [
  { id: "starter", apiKind: "all", steps: 4 },
  { id: "fit", apiKind: "fit", steps: 4 },
  { id: "overhang", apiKind: "overhang", steps: 3 },
  { id: "bridge", apiKind: "bridge", steps: 3 },
  { id: "warping", apiKind: "warping", steps: 3 },
  { id: "layer_strength", apiKind: "layer_strength", steps: 3 },
  // Quality (T-572): the material's own temperature and ironing flow.
  { id: "temp", apiKind: "temp", steps: 4 },
  { id: "ironing", apiKind: "ironing", steps: 3 },
  { id: "creep", apiKind: "creep", steps: 4 },
];
const CREEP_READINGS = [
  [1, "minute"], [60, "hour"], [1440, "day"], [10080, "week"], [40320, "month"],
];
// The temperature tower's blocks against the material's usual temperature, bottom first (api.TEMP_TOWER_STEPS_C).
const TEMP_TOWER_STEPS_C = [10, 5, 0, -5, -10, -15];
const IRONING_TEST_PCT = [6, 9, 12, 15, 18]; // api.IRONING_TEST_PCT
const towerTemps = (mat) => TEMP_TOWER_STEPS_C.map((d) => Math.round((mat?.nozzle_temp_c ?? 220) + d));

function testMeasuredInfo(testId, { matId, mat, spec, enc }) {
  if (testId === "creep" && spec?.creep_readings?.length) {
    const readings = spec.creep_readings;
    const ready = readings.some(([time]) => time === 1) && readings.some(([time]) => time >= 60);
    const complete = readings.length >= 4 && readings.some(([time]) => time >= 40320);
    const key = !ready ? "pending" : complete ? "summary" : "early";
    return { measured: true, summary: t(`settings.cal.creep.${key}`) };
  }
  if (testId === "starter") {
    const checked = settings.starterChecked[matId];
    if (spec?.hole_error_mm != null) {
      const sign = spec.hole_error_mm > 0 ? "+" : "";
      return { measured: true, summary: t("settings.cal.starter.summary", { hole: `${sign}${spec.hole_error_mm}` }) };
    }
    if (checked) return { measured: true, summary: t("settings.cal.starter.summaryChecked") };
    return { measured: false, summary: "" };
  }
  if (testId === "fit") {
    if (spec?.fit_gap_mm && (spec.fit_gap_mm.snug != null || spec.fit_gap_mm.sliding != null)) {
      const snug = spec.fit_gap_mm.snug ?? TYPICAL_FITS.snug;
      const sliding = spec.fit_gap_mm.sliding ?? TYPICAL_FITS.sliding;
      return { measured: true, summary: t("settings.cal.fit.summary", { snug, sliding }) };
    }
    return { measured: false, summary: "" };
  }
  if (testId === "overhang") {
    if (spec?.max_overhang_deg != null) return { measured: true, summary: t("settings.cal.overhang.summary", { deg: mat.max_overhang_deg }) };
    return { measured: false, summary: "" };
  }
  if (testId === "bridge") {
    if (spec?.max_bridge_mm != null) return { measured: true, summary: t("settings.cal.bridge.summary", { mm: mat.max_bridge_mm }) };
    return { measured: false, summary: "" };
  }
  if (testId === "warping") {
    if (spec?.warp_length_mm?.[enc] != null) return { measured: true, summary: t("settings.cal.warping.summary", { mm: Math.round(spec.warp_length_mm[enc] * 0.65) }) };
    return { measured: false, summary: "" };
  }
  if (testId === "layer_strength") {
    if (spec?.z_strength_factor?.[enc] != null) return { measured: true, summary: t("settings.cal.layer_strength.summary", { factor: spec.z_strength_factor[enc] }) };
    return { measured: false, summary: "" };
  }
  if (testId === "temp") {
    if (spec?.nozzle_temp_c != null) return { measured: true, summary: t("settings.cal.temp.summary", { c: spec.nozzle_temp_c }) };
    return { measured: false, summary: "" };
  }
  if (testId === "ironing") {
    if (spec?.ironing_flow_pct != null) return { measured: true, summary: t("settings.cal.ironing.summary", { pct: spec.ironing_flow_pct }) };
    return { measured: false, summary: "" };
  }
  return { measured: false, summary: "" };
}

function makeSvg(viewBox, children) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", viewBox);
  svg.setAttribute("aria-hidden", "true");
  for (const [tag, attrs] of children) {
    const el = document.createElementNS(ns, tag);
    for (const [k, val] of Object.entries(attrs)) el.setAttribute(k, String(val));
    svg.append(el);
  }
  return svg;
}

function calDiagram(testId) {
  if (testId === "creep") {
    return makeSvg("0 0 200 100", [
      ["path", { d: "M 16 20 H 54 L 64 25 H 149 L 160 31 L 149 37 H 64 L 54 42 H 16 Z", fill: "currentColor", opacity: 0.75 }],
      ["circle", { cx: 142, cy: 31, r: 4, fill: "var(--surface-sunken)" }],
      ["path", { d: "M 142 32 V 55 M 168 20 V 70 M 168 31 H 176 M 168 40 H 172 M 168 49 H 176 M 168 58 H 172 M 168 67 H 176",
        fill: "none", stroke: "currentColor", "stroke-width": 2 }],
      ["rect", { x: 130, y: 55, width: 24, height: 31, rx: 5, fill: "currentColor", opacity: 0.45 }],
    ]);
  }
  if (testId === "temp") { // six blocks, each with an overhang on its right
    return makeSvg("0 0 200 100", [
      ["rect", { x: 70, y: 88, width: 70, height: 5, rx: 2, fill: "currentColor", opacity: 0.3 }],
      ...[0, 1, 2, 3, 4, 5].map((i) => ["path", { d: `M 76 ${88 - i * 13} V ${76 - i * 13} H 116 L 124 ${88 - i * 13} Z`, fill: "currentColor", opacity: 0.35 + i * 0.1 }]),
    ]);
  }
  if (testId === "ironing") { // five steps, the highest on the left
    return makeSvg("0 0 200 100", [0, 1, 2, 3, 4].map((i) => ["rect",
      { x: 30 + i * 28, y: 30 + i * 10, width: 28, height: 58 - i * 10, fill: "currentColor", opacity: 0.75 - i * 0.1 }]));
  }
  if (testId === "overhang") {
    return makeSvg("0 0 200 100", [
      ["rect", { x: 10, y: 78, width: 180, height: 10, rx: 2, fill: "currentColor", opacity: 0.25 }],
      ...[40, 50, 60, 70].map((a, i) => ["path", {
        d: `M ${22 + i * 42} 78 L ${22 + i * 42} 36 L ${22 + i * 42 + 12 + i * 6} 36 L ${32 + i * 42} 78 Z`,
        fill: "currentColor",
        opacity: 0.45 + i * 0.14,
      }]),
    ]);
  }
  if (testId === "bridge") {
    return makeSvg("0 0 200 100", [
      ["rect", { x: 14, y: 34, width: 14, height: 52, fill: "currentColor", opacity: 0.5 }],
      ["rect", { x: 64, y: 34, width: 14, height: 52, fill: "currentColor", opacity: 0.5 }],
      ["rect", { x: 132, y: 34, width: 14, height: 52, fill: "currentColor", opacity: 0.5 }],
      ["rect", { x: 14, y: 26, width: 132, height: 8, rx: 2, fill: "currentColor", opacity: 0.8 }],
      ["rect", { x: 14, y: 86, width: 172, height: 6, rx: 2, fill: "currentColor", opacity: 0.25 }],
    ]);
  }
  if (testId === "fit") {
    return makeSvg("0 0 200 100", [
      ["rect", { x: 12, y: 26, width: 142, height: 48, rx: 6, fill: "currentColor", opacity: 0.2 }],
      ...[0, 1, 2, 3, 4].map((i) => ["circle", { cx: 28 + i * 28, cy: 50, r: 7 + i * 0.9, fill: "none", stroke: "currentColor", "stroke-width": 2.5 }]),
      ["rect", { x: 168, y: 28, width: 16, height: 44, rx: 8, fill: "currentColor", opacity: 0.75 }],
    ]);
  }
  if (testId === "warping") {
    return makeSvg("0 0 200 100", [
      ["rect", { x: 16, y: 20, width: 52, height: 10, rx: 2, fill: "currentColor", opacity: 0.45 }],
      ["rect", { x: 16, y: 38, width: 92, height: 10, rx: 2, fill: "currentColor", opacity: 0.55 }],
      ["rect", { x: 16, y: 56, width: 132, height: 10, rx: 2, fill: "currentColor", opacity: 0.7 }],
      ["rect", { x: 16, y: 74, width: 168, height: 10, rx: 2, fill: "currentColor", opacity: 0.85 }],
    ]);
  }
  if (testId === "layer_strength") {
    return makeSvg("0 0 200 100", [
      ["path", { d: "M 18 32 H 130 V 48 H 100 L 92 43 H 56 L 48 48 H 18 Z", fill: "currentColor", opacity: 0.65 }],
      ["circle", { cx: 30, cy: 40, r: 4, fill: "var(--surface-sunken)" }],
      ["circle", { cx: 118, cy: 40, r: 4, fill: "var(--surface-sunken)" }],
      ["rect", { x: 158, y: 14, width: 16, height: 72, rx: 4, fill: "currentColor", opacity: 0.8 }],
      ["rect", { x: 148, y: 84, width: 36, height: 5, rx: 2, fill: "currentColor", opacity: 0.35 }],
    ]);
  }
  return makeSvg("0 0 200 100", [
    ["rect", { x: 14, y: 14, width: 172, height: 72, rx: 6, fill: "none", stroke: "currentColor", "stroke-width": 2, opacity: 0.4 }],
    ["rect", { x: 24, y: 24, width: 64, height: 18, rx: 3, fill: "currentColor", opacity: 0.55 }],
    ["rect", { x: 24, y: 50, width: 96, height: 24, rx: 3, fill: "currentColor", opacity: 0.35 }],
    ["rect", { x: 132, y: 24, width: 42, height: 50, rx: 3, fill: "currentColor", opacity: 0.7 }],
  ]);
}

function visualRadioGroup({ label, options, value, onChange, renderVisual }) {
  const group = h("div", { role: "radiogroup", class: "cal-picker", "aria-label": label });
  const buttons = options.map((opt) => {
    const selected = String(opt.value) === String(value);
    const btn = h("button", {
      type: "button",
      role: "radio",
      class: "cal-pick",
      "aria-checked": String(selected),
      tabindex: selected ? 0 : -1,
      dataset: { pickValue: String(opt.value) },
      onclick: () => select(opt.value),
    },
    renderVisual(opt),
    h("span", { class: "cal-pick-label" }, opt.label),
    opt.sub && h("span", { class: "cal-pick-sub" }, opt.sub));
    return btn;
  });
  if (!options.some((o) => String(o.value) === String(value)) && buttons[0]) buttons[0].tabIndex = 0;

  function select(val) {
    value = val;
    buttons.forEach((b, i) => {
      const on = String(options[i].value) === String(val);
      b.setAttribute("aria-checked", String(on));
      b.tabIndex = on ? 0 : -1;
    });
    onChange(val);
  }
  group.addEventListener("keydown", (e) => {
    const at = buttons.indexOf(document.activeElement);
    const to = arrowIndex(e, Math.max(at, 0), buttons.length);
    if (to < 0) return;
    buttons[to].focus();
    select(options[to].value);
  });
  group.append(...buttons);
  return group;
}

function openCalibrationTest(testId) {
  const { matId, spec, enc } = calContext();
  view.calTest = testId;
  if (testId === "overhang") {
    view.calDraft = { deg: spec?.max_overhang_deg ?? null };
  } else if (testId === "bridge") {
    view.calDraft = { mm: spec?.max_bridge_mm ?? null };
  } else if (testId === "warping") {
    const flat = spec?.warp_length_mm?.[enc] != null ? Math.round((spec.warp_length_mm[enc] * 0.65) / 40) * 40 : null;
    view.calDraft = { mm: flat && [40, 80, 120, 160].includes(flat) ? flat : null };
  } else if (testId === "fit") {
    view.calDraft = {
      snug: spec?.fit_gap_mm?.snug ?? null,
      sliding: spec?.fit_gap_mm?.sliding ?? null,
    };
  } else if (testId === "layer_strength") {
    view.calDraft = { flat: "", upright: "" };
  } else if (testId === "temp") {
    view.calDraft = { c: spec?.nozzle_temp_c ?? null };
  } else if (testId === "ironing") {
    view.calDraft = { pct: spec?.ironing_flow_pct ?? null };
  } else if (testId === "creep") {
    view.calDraft = Object.fromEntries(spec?.creep_readings ?? []);
  } else if (testId === "starter") {
    view.calDraft = {
      look: settings.starterChecked[matId]?.note ?? "ok",
      hole: spec?.hole_error_mm != null ? Number((6 + spec.hole_error_mm).toFixed(2)) : "",
      outer: spec?.outer_error_mm != null ? Number((20 + spec.outer_error_mm).toFixed(2)) : "",
    };
  }
  ctx?.services?.rerender?.();
}

async function downloadCalibrationModel(testDef) {
  if (!engineReady || !ctx?.engine) {
    showToast({ message: t("settings.cal.downloadNeedEngine"), tone: "warn" });
    return;
  }
  view.downloading = testDef.id;
  ctx?.services?.rerender?.();
  try {
    const part = await ctx.engine.call("calibration_part", { kind: testDef.apiKind, profile: { material: calContext().matId } });
    // The ironing test comes as a 3MF: each step's ironing flow is set in it (OrcaSlicer, Bambu Studio, PrusaSlicer).
    const [bytes, name, mime] = part["3mf"] ? [part["3mf"], part.name_3mf, "model/3mf"] : [part.stl, part.name, "model/stl"];
    downloadBytes(new Uint8Array(bytes), name, mime);
    showToast({ message: t("settings.cal.downloaded", { name }), tone: "good" });
  } catch (err) {
    showToast({ message: t("settings.cal.downloadFailed", { reason: err.message }), tone: "bad" });
  } finally {
    view.downloading = null;
    ctx?.services?.rerender?.();
  }
}

/** The temperature tower's second step: the user's sliced tower with each block's temperature written in. */
async function towerGcode(file) {
  if (!file) return;
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const r = await ctx.engine.call("tuning_gcode", { kind: "temp", profile: { material: calContext().matId } }, bytes);
    downloadBytes(new Uint8Array(r.data), file.name.replace(/\.gcode$/i, "") + "_temps.gcode", "text/x.gcode");
    showToast({ message: t("settings.cal.temp.gcodeDone", { from: r.temps[0], to: r.temps[r.temps.length - 1] }), tone: "good" });
  } catch (err) {
    showToast({ message: t("settings.cal.temp.gcodeFailed", { reason: err.message }), tone: "bad" });
  }
}

function renderCalibrationDetail() {
  const testDef = CAL_TESTS.find((c) => c.id === view.calTest);
  const info = calContext();
  const status = testMeasuredInfo(testDef.id, info);
  const steps = Array.from({ length: testDef.steps }, (_, i) => h("li", {}, t(`settings.cal.${testDef.id}.s${i + 1}`)));
  const creepTest = testDef.id === "creep" ? info.catalog.creep_test : null;
  let creepFields = [];

  const backBtn = h("button", {
    type: "button",
    class: "ui-link",
    onclick: () => { view.calTest = null; ctx?.services?.rerender?.(); },
  }, `← ${t("settings.cal.backAll")}`);

  const dlBtn = Button({
    label: view.downloading === testDef.id ? t("settings.cal.downloading") : t("settings.cal.download"),
    variant: "primary",
    iconPath: DOWNLOAD_ICON,
    disabled: view.downloading === testDef.id,
    onClick: () => downloadCalibrationModel(testDef),
  });

  const pickerBox = h("div", { class: "set-card" },
    h("h2", { class: "set-card-title" }, t("settings.cal.enterTitle")));

  if (testDef.id === "overhang") {
    const angles = [40, 45, 50, 55, 60, 65, 70];
    pickerBox.append(h("div", { class: "cal-picker-group" },
      h("span", { class: "ui-label" }, t("settings.cal.overhang.pick")),
      visualRadioGroup({
        label: t("settings.cal.overhang.pick"),
        value: view.calDraft.deg,
        options: angles.map((a) => ({ value: a, label: `${a}°` })),
        onChange: (val) => { view.calDraft.deg = Number(val); },
        renderVisual: (o) => {
          const rad = (o.value * Math.PI) / 180;
          const dx = Math.round(Math.tan(rad) * 16);
          return makeSvg("0 0 44 26", [
            ["path", { d: `M 4 24 L 4 4 L ${Math.min(40, 10 + dx)} 4 L 14 24 Z`, fill: "currentColor", opacity: 0.75 }],
          ]);
        },
      })));
  } else if (testDef.id === "bridge") {
    const spans = [10, 20, 30, 40, 50];
    pickerBox.append(h("div", { class: "cal-picker-group" },
      h("span", { class: "ui-label" }, t("settings.cal.bridge.pick")),
      visualRadioGroup({
        label: t("settings.cal.bridge.pick"),
        value: view.calDraft.mm,
        options: spans.map((mm) => ({ value: mm, label: `${mm} mm` })),
        onChange: (val) => { view.calDraft.mm = Number(val); },
        renderVisual: (o) => h("div", { class: "cal-bridge-vis" },
          h("span", { class: "cal-bridge-pillar" }),
          h("span", { class: "cal-bridge-span", style: { width: `${10 + o.value * 0.75}px` } }),
          h("span", { class: "cal-bridge-pillar" })),
      })));
  } else if (testDef.id === "warping") {
    const lengths = [40, 80, 120, 160];
    pickerBox.append(h("div", { class: "cal-picker-group" },
      h("span", { class: "ui-label" }, t("settings.cal.warping.pick")),
      visualRadioGroup({
        label: t("settings.cal.warping.pick"),
        value: view.calDraft.mm,
        options: lengths.map((mm) => ({ value: mm, label: `${mm} mm` })),
        onChange: (val) => { view.calDraft.mm = Number(val); },
        renderVisual: (o) => h("span", { class: "cal-bar-vis", style: { width: `${18 + o.value * 0.35}px` } }),
      })));
  } else if (testDef.id === "fit") {
    const gaps = [0, 0.1, 0.2, 0.3, 0.4, 0.5];
    const holeOpts = gaps.map((g) => ({
      value: g,
      label: t("settings.cal.fit.holeLabel", { dia: (6 + g).toFixed(1) }),
      sub: t("settings.cal.fit.gapHint", { gap: g.toFixed(1) }),
    }));
    const renderHole = (o) => h("span", {
      class: "cal-hole-ring",
      style: { width: `${16 + o.value * 24}px`, height: `${16 + o.value * 24}px` },
    });
    pickerBox.append(
      h("div", { class: "cal-picker-group" },
        h("span", { class: "ui-label" }, t("settings.cal.fit.snug")),
        visualRadioGroup({
          label: t("settings.cal.fit.snug"),
          value: view.calDraft.snug,
          options: holeOpts,
          onChange: (val) => { view.calDraft.snug = Number(val); },
          renderVisual: renderHole,
        })),
      h("div", { class: "cal-picker-group" },
        h("span", { class: "ui-label" }, t("settings.cal.fit.sliding")),
        visualRadioGroup({
          label: t("settings.cal.fit.sliding"),
          value: view.calDraft.sliding,
          options: holeOpts,
          onChange: (val) => { view.calDraft.sliding = Number(val); },
          renderVisual: renderHole,
        })));
  } else if (testDef.id === "layer_strength") {
    const ratioText = h("p", { class: "hint num" });
    const barFill = h("i", { style: { width: "0%" } });
    const updateRatio = () => {
      const f = Number(view.calDraft.flat);
      const u = Number(view.calDraft.upright);
      if (f > 0 && u > 0) {
        const z = zFactor(f, u);
        ratioText.textContent = t("settings.cal.layer_strength.ratio", { factor: z.toFixed(2) });
        barFill.style.width = `${Math.round(z * 100)}%`;
      } else {
        ratioText.textContent = "";
        barFill.style.width = "0%";
      }
    };
    const flatField = NumberField({
      label: t("settings.cal.layer_strength.flat"),
      value: view.calDraft.flat ?? "",
      min: 0.1,
      max: 500,
      units: [{ value: "kg", label: "kg" }],
      onChange: (n) => { view.calDraft.flat = n ?? ""; updateRatio(); },
    });
    const uprightField = NumberField({
      label: t("settings.cal.layer_strength.upright"),
      value: view.calDraft.upright ?? "",
      min: 0.1,
      max: 500,
      units: [{ value: "kg", label: "kg" }],
      onChange: (n) => { view.calDraft.upright = n ?? ""; updateRatio(); },
    });
    updateRatio();
    pickerBox.append(
      h("div", { class: "set-grid-2" }, flatField, uprightField),
      h("div", { class: "cal-ratio-bar" }, barFill),
      ratioText);
  } else if (testDef.id === "temp") {
    const gcodeInput = h("input", { type: "file", accept: ".gcode,.gco,.g", class: "visually-hidden", tabindex: "-1", "aria-hidden": "true",
      onchange: (e) => { towerGcode(e.target.files[0]); e.target.value = ""; } });
    pickerBox.append(
      h("div", { class: "cal-picker-group" },
        h("span", { class: "ui-label" }, t("settings.cal.temp.gcodeTitle")),
        h("p", { class: "hint" }, t("settings.cal.temp.gcodeLine")),
        Button({ label: t("settings.cal.temp.gcode"), variant: "secondary", disabled: !engineReady, onClick: () => gcodeInput.click() }),
        gcodeInput),
      h("div", { class: "cal-picker-group" },
        h("span", { class: "ui-label" }, t("settings.cal.temp.pick")),
        visualRadioGroup({
          label: t("settings.cal.temp.pick"),
          value: view.calDraft.c,
          options: towerTemps(info.mat).map((c) => ({ value: c, label: `${c} °C` })),
          onChange: (val) => { view.calDraft.c = Number(val); },
          // The tower with this block lit: where it sits (hottest at the bottom).
          renderVisual: (o) => makeSvg("0 0 20 30", towerTemps(info.mat).map((c, i) => ["rect",
            { x: 3, y: 25 - i * 4.4, width: 14, height: 3.6, rx: 0.8, fill: "currentColor", opacity: c === o.value ? 0.9 : 0.22 }])),
        })));
  } else if (testDef.id === "ironing") {
    pickerBox.append(h("div", { class: "cal-picker-group" },
      h("span", { class: "ui-label" }, t("settings.cal.ironing.pick")),
      visualRadioGroup({
        label: t("settings.cal.ironing.pick"),
        value: view.calDraft.pct,
        options: IRONING_TEST_PCT.map((p) => ({ value: p, label: `${p} %` })),
        onChange: (val) => { view.calDraft.pct = Number(val); },
        renderVisual: (o) => makeSvg("0 0 30 20", IRONING_TEST_PCT.map((p, i) => ["rect",
          { x: 2 + i * 5.4, y: 4 + i * 2.2, width: 5, height: 14 - i * 2.2, fill: "currentColor", opacity: p === o.value ? 0.9 : 0.22 }])),
      })));
  } else if (testDef.id === "creep") {
    creepFields = CREEP_READINGS.map(([minutes, label]) => NumberField({
      label: t(`settings.cal.creep.${label}`),
      value: view.calDraft[minutes] ?? "",
      min: 0.1,
      max: 25,
      units: [{ value: "mm", label: "mm" }],
      onChange: (value) => { view.calDraft[minutes] = value; },
    }));
    pickerBox.append(
      h("p", { class: "hint" }, t("settings.cal.creep.readingsHint")),
      h("div", { class: "set-grid-2" }, creepFields),
      status.measured && h("p", { class: "hint" }, status.summary));
  } else if (testDef.id === "starter") {
    const lookSeg = SegmentedControl({
      label: t("settings.cal.starter.firstLayer"),
      value: view.calDraft.look || "ok",
      options: [
        { value: "ok", label: t("settings.cal.starter.ok") },
        { value: "adjusted", label: t("settings.cal.starter.adjusted") },
      ],
      onChange: (val) => { view.calDraft.look = val; },
    });
    const holeField = NumberField({
      label: t("settings.cal.starter.holeMeasured"),
      value: view.calDraft.hole ?? "",
      min: 4,
      max: 8,
      units: [{ value: "mm", label: "mm" }],
      onChange: (n) => { view.calDraft.hole = n ?? ""; },
    });
    const outerField = NumberField({
      label: t("settings.cal.starter.outerMeasured"),
      value: view.calDraft.outer ?? "",
      min: 18,
      max: 22,
      units: [{ value: "mm", label: "mm" }],
      onChange: (n) => { view.calDraft.outer = n ?? ""; },
    });
    pickerBox.append(
      h("div", { class: "set-field" }, h("span", { class: "ui-label" }, t("settings.cal.starter.firstLayer")), lookSeg),
      h("div", { class: "set-grid-2" }, holeField, outerField));
  }

  const saveBtn = Button({
    label: t("settings.cal.saveResult"),
    variant: "primary",
    onClick: () => {
      const patch = {};
      let starterNote = null;
      if (testDef.id === "overhang") {
        if (view.calDraft.deg == null) return showToast({ message: t("settings.cal.pickFirst"), tone: "warn" });
        patch.max_overhang_deg = view.calDraft.deg;
      } else if (testDef.id === "bridge") {
        if (view.calDraft.mm == null) return showToast({ message: t("settings.cal.pickFirst"), tone: "warn" });
        patch.max_bridge_mm = view.calDraft.mm;
      } else if (testDef.id === "warping") {
        if (view.calDraft.mm == null) return showToast({ message: t("settings.cal.pickFirst"), tone: "warn" });
        patch.warp_length = warpLength(view.calDraft.mm);
      } else if (testDef.id === "fit") {
        if (view.calDraft.snug == null && view.calDraft.sliding == null) {
          return showToast({ message: t("settings.cal.pickFirst"), tone: "warn" });
        }
        if (view.calDraft.snug != null) patch.fit_snug = view.calDraft.snug;
        if (view.calDraft.sliding != null) patch.fit_sliding = view.calDraft.sliding;
      } else if (testDef.id === "layer_strength") {
        const f = Number(view.calDraft.flat);
        const u = Number(view.calDraft.upright);
        if (!(f > 0 && u > 0)) return showToast({ message: t("settings.cal.layer_strength.needBoth"), tone: "warn" });
        patch.z_strength = Number(zFactor(f, u).toFixed(2));
      } else if (testDef.id === "temp") {
        if (view.calDraft.c == null) return showToast({ message: t("settings.cal.pickFirst"), tone: "warn" });
        patch.nozzle_temp_c = view.calDraft.c;
      } else if (testDef.id === "ironing") {
        if (view.calDraft.pct == null) return showToast({ message: t("settings.cal.pickFirst"), tone: "warn" });
        patch.ironing_flow_pct = view.calDraft.pct;
      } else if (testDef.id === "creep") {
        const invalid = creepFields.find(({ input }) => input.getAttribute("aria-invalid") === "true");
        if (invalid) return invalid.input.focus();
        patch.creep_readings = Object.entries(view.calDraft)
          .filter(([, drop]) => drop != null).map(([minutes, drop]) => [Number(minutes), drop]);
        if (!patch.creep_readings.length) return showToast({ message: t("settings.cal.creep.needReading"), tone: "warn" });
      } else if (testDef.id === "starter") {
        starterNote = view.calDraft.look || "ok";
        if (view.calDraft.hole !== "" && view.calDraft.hole != null) {
          patch.hole_error_mm = Number((Number(view.calDraft.hole) - 6).toFixed(2));
        }
        if (view.calDraft.outer !== "" && view.calDraft.outer != null) {
          patch.outer_error_mm = Number((Number(view.calDraft.outer) - 20).toFixed(2));
        }
      }
      saveCalibrationPatch(patch, { starterNote });
      const updated = calContext();
      showToast({ message: t("settings.cal.saved", { test: t(`settings.cal.${testDef.id}.title`), material: updated.mat.name }), tone: "good" });
    },
  });

  const resetBtn = status.measured && Button({
    label: t("settings.cal.resetTest"),
    variant: "ghost",
    onClick: () => {
      const map = {
        starter: "starter",
        fit: "fit_gap_mm",
        overhang: "max_overhang_deg",
        bridge: "max_bridge_mm",
        warping: "warp_length_mm",
        layer_strength: "z_strength_factor",
        temp: "nozzle_temp_c",
        ironing: "ironing_flow_pct",
        creep: "creep_readings",
      };
      clearCalibrationField(map[testDef.id]);
      openCalibrationTest(testDef.id);
      showToast({ message: t("settings.cal.resetDone", { test: t(`settings.cal.${testDef.id}.title`) }), tone: "info" });
    },
  });

  pickerBox.append(h("div", { class: "set-toolbar" }, saveBtn, resetBtn));

  return [
    h("div", { class: "cal-detail" },
      h("div", { class: "cal-detail-top" },
        backBtn,
        h("span", { class: ["set-badge", status.measured ? "set-badge--measured" : "set-badge--typical"] },
          status.measured ? t("settings.badge.measured") : t("settings.badge.typical"))),
      h("div", { class: "set-head" },
        h("h1", { class: "h-set" }, t(`settings.cal.${testDef.id}.title`)),
        h("p", { class: "hint" }, t(`settings.cal.${testDef.id}.improves`))),
      h("div", { class: "set-toolbar" }, dlBtn),
      h("div", { class: "cal-detail-body" },
        h("div", { class: "cal-diagram" }, calDiagram(testDef.id)),
        h("div", { class: "set-section" },
          h("h2", { class: "ui-label" }, t("settings.cal.stepsTitle")),
          h("ol", { class: "cal-steps" }, steps),
          creepTest && h("p", { class: "hint" }, t("settings.cal.creep.stress", creepTest)))),
      pickerBox),
  ];
}

function renderMaterialSelector(labelKey) {
  const { catalog, matId } = calContext();
  const options = Object.entries(catalog.materials).map(([id, m]) => ({
    value: id,
    label: m.name,
    badge: settings.owned.includes(id) ? t("settings.materials.ownedBadge") : null,
  }));
  return h("div", { class: "cal-context" },
    h("span", { class: "ui-label" }, t(labelKey)),
    Select({
      label: t(labelKey),
      options,
      value: matId,
      searchable: true,
      onChange: (val) => {
        settings.calibrationMaterial = val;
        persistAndSync({ sync: false });
      },
    }));
}

function renderCalibrationTab() {
  if (view.calTest) return renderCalibrationDetail();
  const info = calContext();
  const cards = CAL_TESTS.map((testDef) => {
    const status = testMeasuredInfo(testDef.id, info);
    return h("button", {
      type: "button",
      class: "cal-card",
      dataset: { calCard: testDef.id },
      onclick: () => openCalibrationTest(testDef.id),
    },
    h("span", { class: ["set-badge", status.measured ? "set-badge--measured" : "set-badge--typical"] },
      status.measured ? t("settings.badge.measured") : t("settings.badge.typical")),
    h("b", {}, t(`settings.cal.${testDef.id}.title`)),
    h("span", { class: "cal-card-line" }, t(`settings.cal.${testDef.id}.line`)),
    status.summary ? h("span", { class: "cal-card-val" }, status.summary) : null);
  });

  return [
    renderMaterialSelector("settings.cal.forMaterial"),
    h("div", { class: "cal-grid" }, cards),
  ];
}

// ---------- Tab 5: What the checks use ----------

function renderExpertDisclosure() {
  const modCount = Object.keys(settings.expert).length;
  const summary = modCount
    ? t("settings.active.expertModified", { count: modCount })
    : t("settings.active.expertAllDefault");

  const body = h("div", { class: "expert-grid" });
  if (!engineReady) {
    body.append(h("p", { class: "hint" }, t("settings.active.expertNeedEngine")));
  } else if (!view.expertData) {
    body.append(h("p", { class: "hint" }, t("settings.active.expertLoading")));
  } else {
    const sections = view.expertData.sections ?? [];
    for (const sec of sections) {
      body.append(h("h3", { class: "ui-label" }, sec.title));
      for (const item of sec.values.slice(0, 12)) {
        const isCustom = item.path in settings.expert;
        const field = NumberField({
          label: item.path.split(".").slice(-1)[0].replace(/_/g, " "),
          value: item.value,
          min: item.min ?? undefined,
          max: item.max ?? undefined,
          onCommit: (val) => {
            if (val === item.default) delete settings.expert[item.path];
            else settings.expert[item.path] = val;
            persistAndSync();
          },
        });
        body.append(h("div", { class: "expert-row" },
          h("div", { class: "expert-info" },
            h("span", {}, item.note || item.path),
            h("span", { class: "hint num" }, t("settings.active.expertDefault", { v: item.default }))),
          h("div", { class: "expert-val" }, field),
          isCustom && Button({
            label: t("settings.reset"),
            variant: "ghost",
            size: "small",
            onClick: () => {
              delete settings.expert[item.path];
              persistAndSync();
            },
          })));
      }
    }
    if (modCount) {
      body.append(h("div", { class: "set-toolbar" },
        Button({
          label: t("settings.active.expertReset"),
          variant: "ghost",
          size: "small",
          onClick: () => {
            settings.expert = {};
            persistAndSync();
          },
        })));
    }
  }

  return Disclosure({
    label: t("settings.active.expertMore"),
    summary,
    open: view.expertOpen,
    content: body,
    onToggle: (open) => {
      view.expertOpen = open;
      if (open && engineReady && !view.expertData && ctx?.engine) {
        ctx.engine.call("expert_settings").then((res) => {
          view.expertData = res;
          ctx?.services?.rerender?.();
        }).catch(() => {});
      }
    },
  });
}

function renderActiveTab() {
  const { mat, spec, baseId, base, enc } = calContext();
  const typicalWarp = TYPICAL_WARP[baseId]?.[enc] ?? 110;
  const activeWarp = spec?.warp_length_mm?.[enc] ?? typicalWarp;
  const activeLayer = spec?.z_strength_factor?.[enc] ?? base.z_strength_factor?.[enc] ?? 0.55;
  const activeSnug = spec?.fit_gap_mm?.snug ?? TYPICAL_FITS.snug;
  const activeSlide = spec?.fit_gap_mm?.sliding ?? TYPICAL_FITS.sliding;

  const rows = [
    {
      id: "overhang",
      label: t("settings.active.rowOverhang"),
      value: t("settings.active.deg", { v: mat.max_overhang_deg }),
      source: spec?.max_overhang_deg != null ? "measured" : "typical",
      onReset: () => clearCalibrationField("max_overhang_deg"),
    },
    {
      id: "bridge",
      label: t("settings.active.rowBridge"),
      value: t("settings.active.mm", { v: mat.max_bridge_mm }),
      source: spec?.max_bridge_mm != null ? "measured" : "typical",
      onReset: () => clearCalibrationField("max_bridge_mm"),
    },
    {
      id: "warping",
      label: t("settings.active.rowWarp"),
      value: t("settings.active.mm", { v: Math.round(activeWarp) }),
      source: spec?.warp_length_mm?.[enc] != null ? "measured" : "typical",
      onReset: () => clearCalibrationField("warp_length_mm"),
    },
    {
      id: "layer",
      label: t("settings.active.rowLayer"),
      value: t("settings.active.factor", { v: activeLayer }),
      source: spec?.z_strength_factor?.[enc] != null ? "measured" : "typical",
      onReset: () => clearCalibrationField("z_strength_factor"),
    },
    {
      id: "fit_snug",
      label: t("settings.active.rowFitSnug"),
      value: t("settings.active.mm", { v: Number(activeSnug).toFixed(2) }),
      source: spec?.fit_gap_mm?.snug != null ? "measured" : "typical",
      onReset: () => clearCalibrationField("fit_snug"),
    },
    {
      id: "fit_sliding",
      label: t("settings.active.rowFitSlide"),
      value: t("settings.active.mm", { v: Number(activeSlide).toFixed(2) }),
      source: spec?.fit_gap_mm?.sliding != null ? "measured" : "typical",
      onReset: () => clearCalibrationField("fit_sliding"),
    },
    {
      id: "hole_error",
      label: t("settings.active.rowHoleErr"),
      value: spec?.hole_error_mm != null ? t("settings.active.mm", { v: `${spec.hole_error_mm > 0 ? "+" : ""}${spec.hole_error_mm}` }) : t("settings.active.none"),
      source: spec?.hole_error_mm != null ? "measured" : "typical",
      onReset: () => clearCalibrationField("hole_error_mm"),
    },
    {
      id: "outer_error",
      label: t("settings.active.rowOuterErr"),
      value: spec?.outer_error_mm != null ? t("settings.active.mm", { v: `${spec.outer_error_mm > 0 ? "+" : ""}${spec.outer_error_mm}` }) : t("settings.active.none"),
      source: spec?.outer_error_mm != null ? "measured" : "typical",
      onReset: () => clearCalibrationField("outer_error_mm"),
    },
    {
      id: "profile",
      label: t("settings.active.rowProfile"),
      value: settings.printProfile
        ? `${settings.printProfile.name} (${Object.keys(settings.printProfile.values).length})`
        : t("settings.active.builtInDefaults"),
      source: settings.printProfile ? "imported" : "typical",
      onReset: () => { settings.printProfile = null; persistAndSync({ sync: false }); },
    },
  ];

  const table = h("table", { class: "active-table" },
    h("thead", {},
      h("tr", {},
        h("th", {}, t("settings.active.colCheck")),
        h("th", {}, t("settings.active.colValue")),
        h("th", {}, t("settings.active.colSource")),
        h("th", {}, t("settings.active.colAction")))),
    h("tbody", {},
      rows.map((r) => {
        const badgeClass = r.source === "measured" ? "set-badge--measured" : r.source === "imported" ? "set-badge--imported" : "set-badge--typical";
        const badgeText = r.source === "measured" ? t("settings.source.measured") : r.source === "imported" ? t("settings.source.imported") : t("settings.source.typical");
        return h("tr", { dataset: { activeRow: r.id } },
          h("td", {}, r.label),
          h("td", { class: "num" }, r.value),
          h("td", {}, h("span", { class: ["set-badge", badgeClass] }, badgeText)),
          h("td", {}, r.source !== "typical"
            ? Button({
              label: t("settings.reset"),
              variant: "ghost",
              size: "small",
              onClick: () => {
                r.onReset();
                showToast({ message: t("settings.active.resetValue", { name: r.label }), tone: "info" });
              },
            })
            : null));
      })));

  return [
    renderMaterialSelector("settings.active.forMaterial"),
    table,
    renderExpertDisclosure(),
  ];
}

// ---------- Screen registration ----------

const TAB_HEAD = {
  printers: ["settings.tab.printers", "settings.printers.purpose"],
  profiles: ["settings.tab.profiles", "settings.profiles.purpose"],
  materials: ["settings.tab.materials", "settings.materials.purpose"],
  calibration: ["settings.tab.calibration", "settings.cal.purpose"],
  active: ["settings.tab.active", "settings.active.purpose"],
};

registerScreen("settings", ({ root, store, openRoute, services }) => {
  const s = store.get();
  const panelId = "settings-panel";

  const backBtn = h("button", {
    type: "button",
    class: "settings-back",
    onclick: () => openRoute(s.part ? "flow" : "start"),
  }, icon(BACK_ARROW), h("span", {}, t("shell.backToPart")));

  const tabButtons = TABS.map((tab) => {
    const active = tab.id === view.tab;
    return h("button", {
      type: "button",
      role: "tab",
      id: `settings-tab-${tab.id}`,
      class: "settings-tab",
      "aria-selected": String(active),
      "aria-controls": panelId,
      tabindex: active ? 0 : -1,
      dataset: { tab: tab.id },
      onclick: () => {
        if (view.tab !== tab.id || view.calTest) {
          view.tab = tab.id;
          view.calTest = null;
          pendingFocusSelector = `#settings-tab-${tab.id}`;
          services.rerender?.();
        }
      },
    }, t(tab.labelKey));
  });

  const tablist = h("div", {
    role: "tablist",
    class: "settings-tabs",
    "aria-label": t("settings.nav"),
    "aria-orientation": "vertical",
  }, tabButtons);

  tablist.addEventListener("keydown", (e) => {
    const at = tabButtons.indexOf(document.activeElement);
    if (at < 0) return;
    const to = arrowIndex(e, at, tabButtons.length);
    if (to < 0) return;
    const nextTab = TABS[to].id;
    view.tab = nextTab;
    view.calTest = null;
    pendingFocusSelector = `#settings-tab-${nextTab}`;
    services.rerender?.();
  });

  const nav = h("nav", { class: "settings-nav", "aria-label": t("settings.nav") }, backBtn, tablist);

  // Content panel
  let content;
  if (view.tab === "printers") content = renderPrintersTab();
  else if (view.tab === "profiles") content = renderProfilesTab();
  else if (view.tab === "materials") content = renderMaterialsTab();
  else if (view.tab === "calibration") content = renderCalibrationTab();
  else content = renderActiveTab();

  const [titleKey, purposeKey] = TAB_HEAD[view.tab];
  const showHead = !(view.tab === "calibration" && view.calTest);

  const importInput = h("input", {
    type: "file",
    accept: ".json",
    class: "visually-hidden",
    tabindex: "-1",
    "aria-hidden": "true",
    dataset: { settingsImport: "1" },
  });
  importInput.addEventListener("change", async () => {
    const file = importInput.files[0];
    importInput.value = "";
    if (!file) return;
    importSettingsText(await file.text());
  });

  const resetAllBtn = view.tab === "active" && Button({
    label: t("settings.resetAll"),
    variant: "ghost",
    onClick: () => openModal({
      title: t("settings.resetAllTitle"),
      body: t("settings.resetAllBody"),
      actions: [
        { label: t("settings.cancel"), variant: "ghost" },
        { label: t("settings.resetAllConfirm"), variant: "danger", onClick: () => resetAllSettings() },
      ],
    }),
  });

  const foot = h("div", { class: "set-foot" },
    Button({ label: t("settings.exportAll"), variant: "secondary", onClick: exportAllSettings }),
    Button({ label: t("settings.import"), variant: "ghost", onClick: () => importInput.click() }),
    importInput,
    resetAllBtn && h("span", { class: "set-foot-right" }, resetAllBtn));

  const body = h("div", {
    id: panelId,
    role: "tabpanel",
    class: "settings-body",
    "aria-labelledby": `settings-tab-${view.tab}`,
  },
  showHead ? h("div", { class: "set-head" }, h("h1", { class: "h-set" }, t(titleKey)), h("p", { class: "hint" }, t(purposeKey))) : null,
  ...content,
  foot);

  root.append(h("section", { class: "settings-wrap" }, nav, body));

  if (pendingFocusSelector) {
    const sel = pendingFocusSelector;
    pendingFocusSelector = null;
    queueMicrotask(() => {
      const target = root.querySelector(sel);
      if (target) {
        target.focus();
        if (target.tagName === "INPUT" && target.type === "text") {
          const end = target.value.length;
          target.setSelectionRange?.(end, end);
        }
      }
    });
  }
});
