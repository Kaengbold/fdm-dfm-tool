// The user's own printers and materials: a small form, saved in this browser (localStorage),
// sent to the engine (api.custom_presets), shareable as a YAML snippet for presets.yaml.
// Each one starts from a built-in preset (based_on) and overrides a few values.
const STORAGE = "fdm.myPresets";

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
];
const ENCLOSURES = ["none", "passive", "heated"];

export function loadMine() {
  try {
    const v = JSON.parse(localStorage.getItem(STORAGE) ?? "{}");
    return { printers: v.printers ?? {}, materials: v.materials ?? {} };
  } catch {
    return { printers: {}, materials: {} };
  }
}

export function saveMine(mine) {
  localStorage.setItem(STORAGE, JSON.stringify(mine));
}

/** Form values -> engine spec (see profile.MATERIAL_FIELDS / PRINTER_FIELDS). */
export function toSpec(kind, values) {
  const spec = { based_on: values.based_on };
  if (kind === "printers") {
    Object.assign(spec, { name: values.name, enclosure: values.enclosure, speed: values.speed, multi_material: values.multi_material,
      hardened_nozzle: Boolean(values.hardened_nozzle), bed: [values.bed_x, values.bed_y, values.bed_z].map(Number) });
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
