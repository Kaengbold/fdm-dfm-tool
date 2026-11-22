// The user's own printers and materials: a small form, saved in this browser (localStorage),
// sent to the engine (api.custom_presets), shareable as a YAML snippet for presets.yaml.
// Each one starts from a built-in preset (based_on) and overrides a few values.
const STORAGE = "fdm.myPresets";
const PRINT_STORAGE = "fdm.printProfile";

export function loadPrintProfile() {
  try { return JSON.parse(localStorage.getItem(PRINT_STORAGE)) || null; } catch { return null; }
}
export function savePrintProfile(profile) {
  try { if (profile) localStorage.setItem(PRINT_STORAGE, JSON.stringify(profile)); else localStorage.removeItem(PRINT_STORAGE); } catch { /* Works for this visit. */ }
}

const PRINT_KEYS = {
  wall_loops: "perimeters", sparse_infill_density: "fill_density", sparse_infill_pattern: "fill_pattern",
  layer_height: "layer_height", top_shell_layers: "top_solid_layers", bottom_shell_layers: "bottom_solid_layers",
  enable_support: "support_material", support_type: "support_material_style", support_threshold_angle: "support_material_threshold",
  brim_type: "brim_type", brim_width: "brim_width", bridge_speed: "bridge_speed",
  outer_wall_speed: "external_perimeter_speed", inner_wall_speed: "perimeter_speed",
  sparse_infill_speed: "infill_speed", internal_solid_infill_speed: "solid_infill_speed", top_surface_speed: "top_solid_infill_speed",
};
const PRINT_NAMES = { wall_loops: "walls", sparse_infill_density: "infill", sparse_infill_pattern: "infill pattern", layer_height: "layer height",
  top_shell_layers: "top layers", bottom_shell_layers: "bottom layers", enable_support: "supports", support_type: "support type",
  support_threshold_angle: "support angle", brim_type: "brim type", brim_width: "brim width", bridge_speed: "bridge speed",
  outer_wall_speed: "outer wall speed", inner_wall_speed: "inner wall speed", sparse_infill_speed: "infill speed",
  internal_solid_infill_speed: "solid infill speed", top_surface_speed: "top surface speed" };

/** Read process settings; bundled parents are already flattened by tools/orca_profiles.py. */
export function fromPrintProfile(text, isIni, profiles = {}) {
  let own;
  if (isIni) {
    own = {};
    for (const line of text.split(/\r?\n/)) {
      const match = line.match(/^\s*([\w]+)\s*=\s*(.*?)\s*$/);
      if (match) own[match[1]] = match[2];
    }
  } else own = JSON.parse(text);
  if (!own || Array.isArray(own) || typeof own !== "object") throw new Error("This file is not a print profile.");
  const parent = own.inherits && profiles[own.inherits]?.kind === "process" ? profiles[own.inherits].data : null;
  const data = { ...(parent || {}), ...own }, values = {};
  for (const [key, prusa] of Object.entries(PRINT_KEYS)) {
    let v = data[isIni ? prusa : key];
    if (Array.isArray(v)) v = v[0];
    if (v == null || v === "") continue;
    v = String(v).trim();
    if (key === "sparse_infill_density") { const n = parseFloat(v); if (!Number.isFinite(n)) continue; v = `${n}%`; }
    if (key === "enable_support") v = /^(1|true|yes)$/i.test(v) ? "1" : "0";
    if (isIni && key === "support_type") v = /organic|tree/i.test(v) ? "tree(auto)" : "normal(auto)";
    values[key] = v;
  }
  if (isIni && !values.brim_type && values.brim_width != null) values.brim_type = Number(values.brim_width) > 0 ? "outer_only" : "no_brim";
  return { name: own.name || "Your print settings", values, missingParent: own.inherits && !parent ? own.inherits : null,
    missing: Object.keys(PRINT_KEYS).filter((key) => values[key] == null).map((key) => PRINT_NAMES[key]) };
}

export const MATERIAL_FIELDS = [
  ["name", "Name", "text"],
  ["max_overhang_deg", "Max. overhang (° from vertical)", "number", 1],
  ["max_bridge_mm", "Max. bridge (mm)", "number", 1],
  ["min_layer_time_s", "Min. layer time (s)", "number", 1],
  ["max_flow_mm3s", "Max. flow (mm³/s)", "number", 1],
  ["density_g_cm3", "Density (g/cm³)", "number", 0.01],
  ["shrinkage_pct", "Shrinkage (%)", "number", 0.1],
  ["tensile_xy_mpa", "Tensile strength (MPa)", "number", 1],
  ["warp_risk", "Warping", ["low", "medium", "high"]],
  ["fibre", "Fibre filled (-CF / -GF: stiffer, not stronger)", ["none", "carbon", "glass"]],
  ["z_strength", "Layer strength factor (0–1, from the test below)", "number", 0.01],
  ["warp_length", "Warping length on your printer (mm, from the test below)", "number", 1],
  ["fit_snug", "Gap for a snug fit (mm, from the fit test below)", "number", 0.05],
  ["fit_sliding", "Gap for a sliding fit (mm, from the fit test below)", "number", 0.05],
  ["hole_error_mm", "Holes come out (mm, measured − modelled Ø, from a print below)", "number", 0.01],
  ["outer_error_mm", "Outer sizes come out (mm, measured − modelled, from a print below)", "number", 0.01],
  ["sag_limit_c", "Too hot to keep its shape above (°C, from the sag test)", "number", 1],
  ["min_bond", "Layer bond it needs (0–1, from the snap test)", "number", 0.01],
  ["nozzle_temp_c", "Nozzle temperature (°C, from the temperature tower)", "number", 1],
  ["ironing_flow_pct", "Ironing flow (%, from the ironing test)", "number", 1],
  ["price_per_kg", "Price per kg (your currency, for the cost of a print)", "number", 0.01],
  ["spool_left_g", "Grams left on the spool (warns when a print won't fit)", "number", 1],
];
export const PRINTER_FIELDS = [
  ["name", "Name", "text"],
  ["bed_x", "Bed X (mm)", "number", 1],
  ["bed_y", "Bed Y (mm)", "number", 1],
  ["bed_z", "Height (mm)", "number", 1],
  ["enclosure", "Enclosure", ["none", "passive", "heated"]],
  ["speed", "Speed class", ["fast", "medium"]],
  ["multi_material", "Multi-material", ["none", "optional", "builtin"]],
  ["hardened_nozzle", "Hardened nozzle", "checkbox"],
  ["moving_bed", "Bed moves back and forth (bed slinger)", "checkbox"],
  ["bed_surface", "Build plate", ["unknown", "textured_pei", "smooth_pei", "glass", "cool_plate"]],
];
const ENCLOSURES = ["none", "passive", "heated"];

/** The built-in printer most like the user's: same enclosure first, then the same kind of bed (moving or not,
 * when known), then the nearest bed size. ``builtins``: {id: engine printer preset}. */
export function closestPrinter(builtins, { enclosure, side, moving_bed = null }) {
  const cost = (p) => (p.enclosure === enclosure ? 0 : 1000) + (moving_bed == null || Boolean(p.moving_bed) === moving_bed ? 0 : 300)
    + Math.abs(Math.max(p.bed[0], p.bed[1]) - side);
  return Object.entries(builtins).sort(([, a], [, b]) => cost(a) - cost(b))[0][0];
}

// Text as {key, params} messages (web/i18n.js formats them): this module stays free of imports so Node can test it.
const msg = (key, params = {}) => ({ key, params });

/** Printer form values from an OrcaSlicer catalogue entry (core/fdmcore/data/orca_printers.json) and the user's
 * enclosure, on top of the closest built-in printer. ``note`` (a message for t()) says which values came from
 * the profile and which are assumed (to check before saving). */
export function fromOrcaPrinter(entry, builtins, enclosure) {
  const side = Math.max(entry.bed[0], entry.bed[1]);
  const based_on = closestPrinter(builtins, { enclosure, side, moving_bed: entry.moving_bed });
  const base = builtins[based_on];
  const [bed_x, bed_y, bed_z] = entry.bed;
  const values = { ...toForm("printers", base), based_on, name: entry.name, bed_x, bed_y, bed_z, enclosure,
    multi_material: "none", hardened_nozzle: false, bed_surface: "unknown" };
  const from = [msg("printer.fact.bed", { x: bed_x, y: bed_y, z: bed_z })];
  const assumed = [msg("printer.fact.noHardened"), msg("printer.fact.noMultiMaterial"), msg("printer.fact.plateUnknown")];
  const bedKind = (moves) => msg(moves ? "printer.fact.bedMoves" : "printer.fact.bedStill");
  if (entry.moving_bed == null) assumed.push(bedKind(base.moving_bed));
  else {
    values.moving_bed = entry.moving_bed;
    from.push(bedKind(entry.moving_bed));
  }
  if (entry.speed) {
    values.speed = entry.speed;
    from.push(msg("printer.fact.speed", { speed: entry.speed }));
  } else assumed.push(msg("printer.fact.speed", { speed: values.speed }));
  return { values, note: msg("printer.noteFromProfile", { from, assumed, base: base.name }) };
}

/** Printer form values from three answers (enclosure, bed size, direct drive), on top of the closest built-in. */
export function fromAnswers(builtins, { enclosure, side, directDrive, name }) {
  const based_on = closestPrinter(builtins, { enclosure, side });
  const base = builtins[based_on];
  const values = { ...toForm("printers", base), based_on, name: name ?? "My printer", bed_x: side, bed_y: side, enclosure };
  if (!directDrive) values.speed = "medium"; // a Bowden tube needs slower printing, more so with flexible filament
  return { values, note: msg("printer.noteFromAnswers", { base: base.name, height: values.bed_z }) };
}

export function loadMine() {
  try {
    const v = JSON.parse(localStorage.getItem(STORAGE) ?? "{}");
    return { printers: v.printers ?? {}, materials: v.materials ?? {} };
  } catch {
    return { printers: {}, materials: {} };
  }
}

export function saveMine(mine) {
  try {
    localStorage.setItem(STORAGE, JSON.stringify(mine));
  } catch {
    // Storage blocked or full: the presets still work for this visit.
  }
}

/** Form values -> engine spec (see profile.MATERIAL_FIELDS / PRINTER_FIELDS). */
export function toSpec(kind, values) {
  const spec = { based_on: values.based_on };
  if (kind === "printers") {
    Object.assign(spec, { name: values.name, enclosure: values.enclosure, speed: values.speed, multi_material: values.multi_material,
      hardened_nozzle: Boolean(values.hardened_nozzle), bed: [values.bed_x, values.bed_y, values.bed_z].map(Number),
      bed_surface: values.bed_surface || "unknown", moving_bed: Boolean(values.moving_bed) });
    return spec;
  }
  for (const [key] of MATERIAL_FIELDS) {
    const v = values[key];
    if (v === "" || v == null) continue;
    if (key === "z_strength") spec.z_strength_factor = Object.fromEntries(ENCLOSURES.map((e) => [e, Number(v)]));
    else if (key === "warp_length") spec.warp_length_mm = Object.fromEntries(ENCLOSURES.map((e) => [e, Number(v)]));
    else if (key === "fit_snug" || key === "fit_sliding") spec.fit_gap_mm = { ...spec.fit_gap_mm, ...fitGaps(key, Number(v)) };
    else spec[key] = ["name", "warp_risk", "fibre"].includes(key) ? v : Number(v);
  }
  return spec;
}

/** Form values from a built-in or saved preset (engine format), for editing. */
export function toForm(kind, preset) {
  if (kind === "printers") {
    const [bed_x, bed_y, bed_z] = preset.bed;
    return { ...preset, bed_x, bed_y, bed_z };
  }
  return { ...preset, z_strength: preset.z_strength_factor?.none, warp_length: preset.warp_length_mm?.none ?? "",
    fit_snug: preset.fit_gap_mm?.snug ?? "", fit_sliding: preset.fit_gap_mm?.sliding ?? "" };
}

/** Measured snug / sliding gaps; press and loose follow them (a tenth tighter, 0.15 mm looser). */
function fitGaps(key, gap) {
  const r = (v) => Math.round(Math.max(0, v) * 100) / 100;
  return key === "fit_snug" ? { snug: r(gap), press: r(gap - 0.1) } : { sliding: r(gap), loose: r(gap + 0.15) };
}

const ORCA_TYPES = { PLA: "pla", "PLA+": "pla_plus", PETG: "petg", ABS: "abs", ASA: "asa", PC: "pc", TPU: "tpu_95a", "PA-CF": "pa_cf", PA: "pa_cf",
  PA6: "pa_cf", PA12: "pa_cf", PAHT: "pa_cf", PET: "petg" };
// Fibre filled types start from their plain plastic, with the strength a fibre filament really has:
// Polymaker's PLA-CF is 31 MPa where their PLA is 47 (×0.67), and the layers hold less (15 MPa, about
// a quarter less than plain PLA's share) — stiffer, not stronger (rules.yaml fibre_filled).
const FIBRE_TYPE = /^(.*?)[-\s]?([CG])F$/;
const FIBRE_STRENGTH = 0.67;
const FIBRE_LAYERS = 0.75;

/** An OrcaSlicer / Bambu Studio filament profile (.json, exported or from the user folder) as form values. */
export function fromOrcaFilament(json, builtins) {
  const first = (k) => (Array.isArray(json[k]) ? json[k][0] : json[k]);
  const type = String(first("filament_type") ?? "").toUpperCase();
  const filled = type !== "PA-CF" && FIBRE_TYPE.exec(type);
  const plain = filled ? filled[1] : type;
  const based_on = ORCA_TYPES[plain] ?? Object.keys(builtins).find((k) => builtins[k].name.toUpperCase() === plain) ?? "pla";
  const values = toForm("materials", builtins[based_on]);
  values.based_on = based_on;
  if (filled && !builtins[based_on].fibre) {
    values.fibre = filled[2] === "C" ? "carbon" : "glass";
    values.tensile_xy_mpa = Math.round(values.tensile_xy_mpa * FIBRE_STRENGTH);
    values.z_strength = Math.round(values.z_strength * FIBRE_LAYERS * 100) / 100;
  } else values.fibre = builtins[based_on].fibre ?? "none";
  values.name = String(json.name ?? `${type} filament`).replace(/\s*@.*$/, "").slice(0, 60);
  const num = (k) => (first(k) != null && !Number.isNaN(Number(first(k))) ? Number(first(k)) : undefined);
  values.density_g_cm3 = num("filament_density") || values.density_g_cm3;
  values.max_flow_mm3s = num("filament_max_volumetric_speed") || values.max_flow_mm3s;
  values.min_layer_time_s = num("slow_down_layer_time") ?? values.min_layer_time_s;
  return values;
}

/** Layer strength factor from the two test bars (kg at break). */
export const zFactor = (flatKg, uprightKg) => Math.min(1, Math.max(0.05, uprightKg / flatKg));

/** Warping length from the longest bar that stayed flat: our check warns from 0.7 × that length. */
export const warpLength = (longestFlat) => Math.round(longestFlat / 0.65);

/** YAML for presets.yaml (the engine's merged entries, so reviewers see every value). */
export function toYaml(presets, mine) {
  const lines = ["# Paste into core/fdmcore/data/presets.yaml (rename the ids) — please add a source for each value."];
  for (const table of ["printers", "materials"]) {
    const ids = Object.keys(mine[table]).map((k) => (k.startsWith("my_") ? k : `my_${k}`));
    if (!ids.length) continue;
    lines.push(`${table}:`);
    for (const id of ids) {
      lines.push(`  ${id}:`);
      for (const [k, v] of Object.entries(presets[table][id] ?? {})) lines.push(`    ${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`);
    }
  }
  return lines.join("\n") + "\n";
}
