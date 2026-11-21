// Background browser slicer (T-525): drives the vendored single-thread Muon3D Slicer Engine
// (OrcaSlicer 2.5.0-dev compiled to WebAssembly, AGPL-3.0-only) in its own dedicated Web Worker
// so the page stays usable while slicing. Loaded lazily on first use, verified against pinned
// sha256 hashes (same pins as tools/vendor.py), cached by input hash, and returns a structured
// fallback result when unavailable, too large, or failed so callers can use the pseudo-slicer.

export const ENGINE_VERSION = "0.6.0-m1.1";
export const ENGINE_BASE = new URL(`../vendor/muon3d-slicer-engine@${ENGINE_VERSION}/`, import.meta.url).href;
export const PROFILES_URL = new URL("../core/fdmcore/data/orca_profiles.json.gz", import.meta.url).href;
export const MAX_SLICER_TRIANGLES = 500_000;

// Pinned hashes matching tools/vendor.py (checked by core/tests/test_web_csp.py).
export const ENGINE_SHA256 = Object.freeze({
  manifest: "8e6bb1925975d89e994a57f667efef5607a094c7e3befdcdf6bfb6a697ce8efb",
  host: "446c6d131da98dbf2bbb5e102472ad6e3e192b0d7e8af19103070528d4a1a6f2",
  mjs: "46602f1e095e45ddd8a96667486d0f4210dd967752e383e36207187474c5f53d",
  wasm: "290e5825b14a0e9cbed453304af32644c2f1e269e413cf0bf37d7efc5c1ab6ea",
});
export const ENGINE_INTEGRITY = Object.freeze({
  manifest: "sha256-jmuxkll12J6ZSlf2Z+/vVgeglMfjvv3N9r+2ppfOjvs=",
  host: "sha256-RGxtEx2pjb8ru14QJHKtbj4ZKw1+ivGRAwcFKNShpvI=",
  mjs: "sha256-RmAvHgleRd3YqWZnSG0PQhDdlndS44PjYgcYdHTF9T0=",
  wasm: "sha256-KQ5YJbFKDpy+1FMwSvMmRMLx4mnkE88L831+/Fwatuo=",
});

// Matches core/fdmcore/slicer_cli.py so browser and local OrcaSlicer pick the exact same presets.
export const FILAMENT_TYPE = Object.freeze({
  pla: "PLA",
  pla_plus: "PLA",
  petg: "PETG",
  abs: "ABS",
  asa: "ASA",
  pc: "PC",
  tpu_95a: "TPU",
  pa_cf: "PA-CF",
});

const PLATES = [
  ["Textured PEI Plate", "textured_plate_temp"],
  ["High Temp Plate", "hot_plate_temp"],
  ["Engineering Plate", "eng_plate_temp"],
  ["Cool Plate", "cool_plate_temp"],
];

// Fallback preset/type routing when OrcaSlicer's live config.definitions table isn't loaded yet.
const FILAMENT_SETTING_KEYS = new Set([
  "slow_down_for_layer_cooling",
  "slow_down_layer_time",
  "slow_down_min_speed",
  "nozzle_temperature",
  "nozzle_temperature_initial_layer",
  "hot_plate_temp",
  "hot_plate_temp_initial_layer",
  "textured_plate_temp",
  "textured_plate_temp_initial_layer",
  "eng_plate_temp",
  "eng_plate_temp_initial_layer",
  "cool_plate_temp",
  "cool_plate_temp_initial_layer",
  "fan_min_speed",
  "fan_max_speed",
  "overhang_fan_speed",
  "filament_max_volumetric_speed",
  "filament_flow_ratio",
  "pressure_advance",
]);

const VECTOR_SETTING_KEYS = new Set([
  ...FILAMENT_SETTING_KEYS,
  "bridge_speed",
]);

// ---- OrcaSlicer profile condition parser (safe, no eval — port of slicer_cli.py) -----------------

const norm = (s) => String(s).toLowerCase().replace(/[^a-z0-9]/g, "");

const TOKEN_RE = /\s*(?:(\/(?:\\.|[^/\\])*\/)|("(?:\\.|[^"\\])*")|(\d+(?:\.\d+)?)|([A-Za-z_]\w*)|(==|!=|<=|>=|=~|!~|&&|\|\||[<>!()[\]]))/y;

function condValue(v) {
  if (Array.isArray(v)) return v.map(condValue);
  if (typeof v === "string" && /^\d+(?:\.\d+)?$/.test(v)) return Number(v);
  return v;
}

/** Evaluate a profile's ``compatible_printers_condition`` against a machine profile without eval. */
export function evalCondition(expr, machine) {
  if (!expr) return false;
  const tokens = [];
  let pos = 0;
  while (pos < expr.length) {
    TOKEN_RE.lastIndex = pos;
    const m = TOKEN_RE.exec(expr);
    if (!m || TOKEN_RE.lastIndex === pos) {
      if (expr.slice(pos).trim()) return false;
      break;
    }
    const kind = m[1] ? "regex" : m[2] ? "str" : m[3] ? "num" : m[4] ? "word" : "op";
    tokens.push([kind, m[1] ?? m[2] ?? m[3] ?? m[4] ?? m[5]]);
    pos = TOKEN_RE.lastIndex;
  }

  let i = 0;
  const peek = () => tokens[i]?.[1];
  const parseOr = () => {
    let a = parseAnd();
    while (peek() === "or" || peek() === "||") {
      i++;
      const b = parseAnd();
      a = Boolean(a) || Boolean(b);
    }
    return a;
  };
  const parseAnd = () => {
    let a = parseNot();
    while (peek() === "and" || peek() === "&&") {
      i++;
      const b = parseNot();
      a = Boolean(a) && Boolean(b);
    }
    return a;
  };
  const parseNot = () => {
    if (peek() === "not" || peek() === "!") {
      i++;
      return !parseNot();
    }
    return parseCompare();
  };
  const parseCompare = () => {
    const a = parseAtom();
    const op = peek();
    if (op === "=~" || op === "!~") {
      i++;
      const tok = tokens[i++];
      if (!tok || tok[0] !== "regex") throw new Error("expected regex");
      const text = typeof a === "string" ? a : Array.isArray(a) ? a.map(String).join(" ") : String(a);
      const found = new RegExp(tok[1].slice(1, -1)).test(text);
      return found === (op === "=~");
    }
    if (op === "==" || op === "!=" || op === "<" || op === ">" || op === "<=" || op === ">=") {
      i++;
      const b = parseAtom();
      if (op === "==" || op === "!=") return (a === b) === (op === "==");
      if (op === "<") return a < b;
      if (op === ">") return a > b;
      if (op === "<=") return a <= b;
      return a >= b;
    }
    return a;
  };
  const parseAtom = () => {
    const tok = tokens[i++];
    if (!tok) throw new Error("unexpected end");
    const [kind, text] = tok;
    if (text === "(") {
      const a = parseOr();
      if (tokens[i++]?.[1] !== ")") throw new Error("missing )");
      return a;
    }
    if (kind === "num") return Number(text);
    if (kind === "str") return text.slice(1, -1).replace(/\\(.)/g, "$1");
    if (kind === "word") {
      if (text === "true" || text === "false") return text === "true";
      if (!(text in machine)) throw new Error(`unknown ${text}`);
      let a = condValue(machine[text]);
      if (peek() === "[") {
        i++;
        const idx = parseAtom();
        if (tokens[i++]?.[1] !== "]") throw new Error("missing ]");
        if (!Array.isArray(a) || Math.trunc(idx) < 0 || Math.trunc(idx) >= a.length) throw new Error("index");
        a = a[Math.trunc(idx)];
      }
      return a;
    }
    throw new Error(`unexpected ${text}`);
  };

  try {
    const value = parseOr();
    return i === tokens.length ? Boolean(value) : false;
  } catch {
    return false;
  }
}

export function fitsPrinter(data, machineName, machineData) {
  if (Array.isArray(data?.compatible_printers) && data.compatible_printers.length > 0) {
    return data.compatible_printers.includes(machineName);
  }
  const cond = data?.compatible_printers_condition || "";
  return Boolean(cond) && evalCondition(cond, machineData);
}

/** Pick the bed plate a filament allows (avoids OrcaSlicer's "Cool Plate does not support filament 1"). */
export function bedType(filamentData) {
  for (const [plate, key] of PLATES) {
    const temps = filamentData?.[key];
    const first = Array.isArray(temps) ? temps[0] : temps;
    if (first != null && Number(first) > 0) return plate;
  }
  return PLATES[0][0];
}

function cmpTuple(a, b) {
  for (let i = 0; i < a.length; i++) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return 0;
}

/**
 * Pick flattened OrcaSlicer machine, process and filament profiles from the bundled snapshot
 * (same selection logic as ``core/fdmcore/slicer_cli.py:match_profiles``).
 */
export function matchOrcaProfiles(snapshotOrProfiles, printerName, nozzle, material, layerHeight = 0.2) {
  const profiles = snapshotOrProfiles?.profiles ?? snapshotOrProfiles ?? {};
  const entries = Object.entries(profiles).filter(([, p]) => (p?.own?.instantiation ?? p?.data?.instantiation) === "true");
  const base = norm(String(printerName).replace(/\(.*?\)/g, "")).replace(/\++$/, "");
  const want = `${String(nozzle)} nozzle`;

  const machines = entries
    .filter(([name, p]) => p.kind === "machine" && name.endsWith(want) && norm(name).startsWith(base))
    .map(([name]) => name);
  if (!machines.length) {
    const err = new Error(`No OrcaSlicer profile for ${printerName} with a ${nozzle} mm nozzle`);
    err.code = "NO_PROFILE";
    throw err;
  }
  machines.sort((a, b) => cmpTuple([a.length, a], [b.length, b]));
  const machineName = machines[0];
  const machineData = profiles[machineName].data;

  const compatible = (name) => fitsPrinter(profiles[name].data, machineName, machineData);

  const processes = entries
    .filter(([name, p]) => p.kind === "process" && compatible(name))
    .map(([name]) => name);
  if (!processes.length) {
    const err = new Error(`No OrcaSlicer process profile for ${machineName}`);
    err.code = "NO_PROFILE";
    throw err;
  }
  const targetLh = Number.isFinite(Number(layerHeight)) ? Number(layerHeight) : 0.2;
  processes.sort((a, b) => {
    const la = Number(profiles[a].data.layer_height ?? 0.2);
    const lb = Number(profiles[b].data.layer_height ?? 0.2);
    return cmpTuple(
      [Math.abs(la - targetLh), a.toLowerCase().includes("standard") ? 0 : 1, a.length, a],
      [Math.abs(lb - targetLh), b.toLowerCase().includes("standard") ? 0 : 1, b.length, b],
    );
  });
  const processName = processes[0];

  const matKey = String(material).toLowerCase();
  const ftype = FILAMENT_TYPE[matKey] ?? String(material).toUpperCase();
  const typed = entries
    .filter(([, p]) => p.kind === "filament" && Array.isArray(p.data.filament_type) && p.data.filament_type.includes(ftype))
    .map(([name]) => name);

  const forAny = (name) => {
    const d = profiles[name].data;
    return !(Array.isArray(d.compatible_printers) && d.compatible_printers.length) && !d.compatible_printers_condition;
  };
  let filaments = typed.filter(compatible);
  if (!filaments.length) filaments = typed.filter(forAny);
  if (!filaments.length) {
    const err = new Error(`No OrcaSlicer ${ftype} filament profile for ${machineName}`);
    err.code = "NO_PROFILE";
    throw err;
  }
  filaments.sort((a, b) =>
    cmpTuple([a.includes("Generic") ? 0 : 1, a.length, a], [b.includes("Generic") ? 0 : 1, b.length, b]),
  );
  const filamentName = filaments[0];

  return {
    machine: { name: machineName, data: profiles[machineName].data },
    process: { name: processName, data: profiles[processName].data },
    filament: { name: filamentName, data: profiles[filamentName].data },
  };
}

// ---- Config building & mesh placement ------------------------------------------------------------

function clonePreset(data, kind) {
  const out = {};
  for (const [k, v] of Object.entries(data || {})) {
    if (k === "inherits") continue;
    out[k] = Array.isArray(v) ? [...v] : v;
  }
  out.type ??= kind;
  out.from ??= "system";
  return out;
}

function toOrcaString(value) {
  if (typeof value === "boolean") return value ? "1" : "0";
  return String(value);
}

/**
 * Flatten chosen profiles and merge FDM Check / user setting overrides into OrcaSlicer's
 * ``{ machine, process, filaments: [filament] }`` structure. Uses live ``config.definitions``
 * when available so every option lands in its owning preset with the right scalar/vector shape.
 */
export function applySettingsOverrides(chosen, settings = null, definitions = null) {
  const machine = clonePreset(chosen.machine?.data ?? chosen.machine, "machine");
  const process = clonePreset(chosen.process?.data ?? chosen.process, "process");
  const filament = clonePreset(chosen.filament?.data ?? chosen.filament, "filament");
  if (!process.curr_bed_type) process.curr_bed_type = bedType(filament);

  const pairs = Array.isArray(settings)
    ? settings.map((item) => [item.key, item.value])
    : Object.entries(settings || {});

  const presetKeys = definitions?.presetKeys || null;
  const options = definitions?.options || null;

  for (const [key, raw] of pairs) {
    if (!key || raw === undefined || raw === null) continue;
    let target = process;
    if (presetKeys) {
      if (presetKeys.filament?.includes(key)) target = filament;
      else if (presetKeys.machine?.includes(key)) target = machine;
      else target = process;
    } else if (FILAMENT_SETTING_KEYS.has(key) || (key in filament && !(key in process))) {
      target = filament;
    } else if (key in machine && !(key in process)) {
      target = machine;
    }

    const optType = options?.[key]?.type;
    const isVector = optType
      ? optType.endsWith("s")
      : Array.isArray(target[key]) || VECTOR_SETTING_KEYS.has(key);

    if (isVector) {
      if (Array.isArray(raw)) {
        target[key] = raw.map(toOrcaString);
      } else {
        const s = toOrcaString(raw);
        const len = Array.isArray(target[key]) && target[key].length > 1 ? target[key].length : 1;
        target[key] = Array.from({ length: len }, () => s);
      }
    } else {
      target[key] = Array.isArray(raw) ? toOrcaString(raw[0]) : toOrcaString(raw);
    }
  }

  return { machine, process, filaments: [filament] };
}

/** Centre [x, y] in mm of a machine profile's ``printable_area``. */
export function bedCentre(machine) {
  const area = [machine?.printable_area].flat().filter(Boolean).join(",");
  const points = area
    .split(",")
    .map((p) => p.split("x").map(Number))
    .filter((pt) => pt.length === 2 && Number.isFinite(pt[0]) && Number.isFinite(pt[1]));
  if (!points.length) return [128, 128];
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const [x, y] of points) {
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  return [(minX + maxX) / 2, (minY + maxY) / 2];
}

/** Place oriented vertex positions (mm, +Z up) centred on ``[cx, cy]`` with lowest Z at 0. */
export function placeOnBed(positions, centre) {
  const src = positions instanceof Float32Array ? positions : new Float32Array(positions);
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, minZ = Infinity;
  for (let i = 0; i < src.length; i += 3) {
    const x = src[i], y = src[i + 1], z = src[i + 2];
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) {
      throw new Error("Mesh contains non-finite coordinates");
    }
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
    if (z < minZ) minZ = z;
  }
  const dx = centre[0] - (minX + maxX) / 2;
  const dy = centre[1] - (minY + maxY) / 2;
  const dz = -minZ;
  const out = new Float32Array(src.length);
  for (let i = 0; i < src.length; i += 3) {
    out[i] = src[i] + dx;
    out[i + 1] = src[i + 1] + dy;
    out[i + 2] = src[i + 2] + dz;
  }
  return out;
}

function normalizeMesh(mesh) {
  if (mesh instanceof Float32Array || ArrayBuffer.isView(mesh) || Array.isArray(mesh)) {
    const positions = mesh instanceof Float32Array ? mesh : new Float32Array(mesh);
    if (positions.length === 0 || positions.length % 9 !== 0) {
      throw new Error("Mesh triangle soup must have a positive multiple of 9 floats");
    }
    return { positions, indices: null, triangles: positions.length / 9 };
  }
  if (mesh && mesh.positions) {
    const positions = mesh.positions instanceof Float32Array ? mesh.positions : new Float32Array(mesh.positions);
    const indices = mesh.indices
      ? mesh.indices instanceof Uint32Array
        ? mesh.indices
        : new Uint32Array(mesh.indices)
      : null;
    if (indices) {
      if (indices.length === 0 || indices.length % 3 !== 0) {
        throw new Error("Indexed mesh must have a positive multiple of 3 indices");
      }
      return { positions, indices, triangles: indices.length / 3 };
    }
    if (positions.length === 0 || positions.length % 9 !== 0) {
      throw new Error("Mesh positions must have a positive multiple of 9 floats");
    }
    return { positions, indices: null, triangles: positions.length / 9 };
  }
  throw new Error("Expected mesh as Float32Array or { positions, indices? }");
}

// ---- Deterministic input hashing -----------------------------------------------------------------

function canonicalJson(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  const keys = Object.keys(value).sort();
  return "{" + keys.map((k) => JSON.stringify(k) + ":" + canonicalJson(value[k])).join(",") + "}";
}

function fallbackHexHash(bytes) {
  // Two independent 64-bit FNV-1a lanes (for non-secure HTTP LAN origins without crypto.subtle).
  let h1 = 0xcbf29ce484222325n;
  let h2 = 0x84222325cbf29ce4n;
  const prime = 0x100000001b3n;
  const mask = 0xffffffffffffffffn;
  for (let i = 0; i < bytes.length; i++) {
    const b = BigInt(bytes[i]);
    h1 = ((h1 ^ b) * prime) & mask;
    h2 = ((h2 ^ (b + BigInt(i & 0xff))) * prime) & mask;
  }
  return h1.toString(16).padStart(16, "0") + h2.toString(16).padStart(16, "0");
}

/** SHA-256 hex digest of the exact slice input (placed geometry + canonical Orca configs + name). */
export async function hashSliceInput({ positions, indices = null, configs, objectName = "part.stl" }) {
  const meta = new TextEncoder().encode(canonicalJson({ v: ENGINE_VERSION, objectName, configs }));
  const posBytes = new Uint8Array(positions.buffer, positions.byteOffset, positions.byteLength);
  const idxBytes = indices ? new Uint8Array(indices.buffer, indices.byteOffset, indices.byteLength) : new Uint8Array(0);
  const combined = new Uint8Array(meta.byteLength + 8 + posBytes.byteLength + idxBytes.byteLength);
  const dv = new DataView(combined.buffer);
  combined.set(meta, 0);
  dv.setUint32(meta.byteLength, posBytes.byteLength, true);
  dv.setUint32(meta.byteLength + 4, idxBytes.byteLength, true);
  combined.set(posBytes, meta.byteLength + 8);
  combined.set(idxBytes, meta.byteLength + 8 + posBytes.byteLength);

  if (globalThis.crypto?.subtle) {
    const digest = await globalThis.crypto.subtle.digest("SHA-256", combined);
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
  }
  return fallbackHexHash(combined);
}

// ---- Bundled OrcaSlicer profiles (.json.gz) ------------------------------------------------------

async function gunzipJson(response) {
  if (typeof DecompressionStream === "function" && response.body) {
    const text = await new Response(response.body.pipeThrough(new DecompressionStream("gzip"))).text();
    return JSON.parse(text);
  }
  throw new Error("Browser lacks DecompressionStream for gzipped OrcaSlicer profiles");
}

// ---- Worker client -------------------------------------------------------------------------------

function defaultWorkerFactory(hostUrl) {
  if (typeof Worker !== "function") {
    throw new Error("Web Workers are not supported in this environment");
  }
  return new Worker(hostUrl, { type: "module", name: "muon3d-slicer-engine" });
}

function attachWorker(worker, onMessage, onError) {
  if (typeof worker.addEventListener === "function") {
    const msg = (e) => onMessage(e.data);
    const err = (e) => onError(new Error(e?.message || "Slicer worker failed"));
    worker.addEventListener("message", msg);
    worker.addEventListener("error", err);
    worker.addEventListener("messageerror", err);
    return () => {
      worker.removeEventListener("message", msg);
      worker.removeEventListener("error", err);
      worker.removeEventListener("messageerror", err);
    };
  }
  // Node worker_threads compatibility for automated tests.
  const msg = (m) => onMessage(m);
  const err = (e) => onError(e instanceof Error ? e : new Error(String(e)));
  const exit = (code) => {
    if (code !== 0) onError(new Error(`Slicer worker exited with code ${code}`));
  };
  worker.on("message", msg);
  worker.on("error", err);
  worker.on("exit", exit);
  return () => {
    worker.off("message", msg);
    worker.off("error", err);
    worker.off("exit", exit);
  };
}

/**
 * Create a self-contained browser slicer controller.
 * Options are mainly for tests (custom engineBase, profilesUrl, fetchImpl, workerFactory, cache limits).
 */
export function createBrowserSlicer(options = {}) {
  const engineBase = options.engineBase ?? ENGINE_BASE;
  const profilesUrl = options.profilesUrl ?? PROFILES_URL;
  const verifyIntegrity = options.verifyIntegrity ?? true;
  const fetchImpl = options.fetchImpl ?? ((url, init) => globalThis.fetch(url, init));
  const loadProfilesImpl = options.loadProfiles ?? null;
  const workerFactory = options.workerFactory ?? defaultWorkerFactory;
  const maxCacheEntries = options.maxCacheEntries ?? 8;
  const maxCacheBytes = options.maxCacheBytes ?? 64 * 1024 * 1024;
  const defaultMaxTriangles = options.maxTriangles ?? MAX_SLICER_TRIANGLES;

  let state = "idle"; // idle | loading | ready | slicing | unavailable | error
  let lastReason = null;
  let lastError = null;
  let engineInfo = null;
  let maxHeapBytes = 0;
  let definitions = null;
  let profilesPromise = null;

  let worker = null;
  let detachWorker = null;
  let readyPromise = null;
  let nextReqId = 0;
  const pending = new Map();
  let activeSlice = null; // { id, hash, cancel }
  const inFlightByHash = new Map();
  let progressSink = null;

  // Bounded LRU cache (insertion order = recency).
  const cache = new Map();
  let cacheBytes = 0;

  function cacheGet(hash) {
    const hit = cache.get(hash);
    if (!hit) return null;
    cache.delete(hash);
    cache.set(hash, hit);
    return hit;
  }

  function cachePut(hash, entry) {
    if (entry.byteLength > maxCacheBytes) return;
    if (cache.has(hash)) {
      cacheBytes -= cache.get(hash).byteLength;
      cache.delete(hash);
    }
    cache.set(hash, entry);
    cacheBytes += entry.byteLength;
    while (cache.size > maxCacheEntries || cacheBytes > maxCacheBytes) {
      const oldestKey = cache.keys().next().value;
      cacheBytes -= cache.get(oldestKey).byteLength;
      cache.delete(oldestKey);
    }
  }

  function clearCache() {
    cache.clear();
    cacheBytes = 0;
  }

  function tearDownWorker(reasonError = null) {
    if (detachWorker) {
      detachWorker();
      detachWorker = null;
    }
    if (worker) {
      try {
        worker.terminate();
      } catch {
        // ignore termination errors
      }
      worker = null;
    }
    readyPromise = null;
    for (const [id, entry] of pending.entries()) {
      pending.delete(id);
      entry.reject(reasonError || new Error("Slicer worker was stopped"));
    }
  }

  function sendRequest(op, params, { transfer = [], onProgress = null } = {}) {
    if (!worker) return Promise.reject(new Error("Slicer worker is not running"));
    const id = ++nextReqId;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject, onProgress });
      try {
        worker.postMessage({ v: 2, id, op, params }, transfer);
      } catch (err) {
        pending.delete(id);
        reject(err);
      }
    });
  }

  async function loadProfiles() {
    if (!profilesPromise) {
      profilesPromise = (async () => {
        if (loadProfilesImpl) return await loadProfilesImpl();
        const res = await fetchImpl(profilesUrl);
        if (!res.ok) throw new Error(`Could not load OrcaSlicer profiles (${res.status})`);
        return await gunzipJson(res);
      })().catch((err) => {
        profilesPromise = null;
        throw err;
      });
    }
    return profilesPromise;
  }

  async function fetchManifest() {
    const url = new URL("manifest.json", engineBase).href;
    const init = verifyIntegrity ? { integrity: ENGINE_INTEGRITY.manifest } : undefined;
    let res;
    try {
      res = await fetchImpl(url, init);
    } catch (cause) {
      const err = new Error(`Browser slicer engine is not available (${cause?.message || cause})`);
      err.reason = "unavailable";
      err.cause = cause;
      throw err;
    }
    if (!res.ok) {
      const err = new Error(`Browser slicer engine is not available (${res.status})`);
      err.reason = "unavailable";
      throw err;
    }
    let manifest;
    try {
      manifest = await res.json();
    } catch (cause) {
      const err = new Error("Browser slicer engine manifest is invalid");
      err.reason = "unavailable";
      err.cause = cause;
      throw err;
    }
    if (verifyIntegrity) {
      const ok =
        manifest?.host?.sha256 === ENGINE_SHA256.host &&
        manifest?.variants?.st?.sha256?.mjs === ENGINE_SHA256.mjs &&
        manifest?.variants?.st?.sha256?.wasm === ENGINE_SHA256.wasm;
      if (!ok) {
        const err = new Error("Browser slicer engine manifest does not match pinned hashes");
        err.reason = "unavailable";
        throw err;
      }
    }
    return manifest;
  }

  async function ensureReady(onProgress = null) {
    if (typeof WebAssembly !== "object") {
      state = "unavailable";
      lastReason = "unavailable";
      lastError = "WebAssembly is not available in this browser";
      const err = new Error(lastError);
      err.reason = "unavailable";
      throw err;
    }
    if (readyPromise) return readyPromise;
    state = "loading";
    lastReason = null;
    lastError = null;
    progressSink = onProgress;

    readyPromise = (async () => {
      const manifest = await fetchManifest();
      const hostUrl = new URL(manifest.host.file, engineBase).href;
      worker = workerFactory(hostUrl, engineBase);
      detachWorker = attachWorker(
        worker,
        (msg) => {
          if (!msg || typeof msg !== "object") return;
          if (msg.kind === "state") {
            const st = msg.state;
            if (st?.state === "loading" && progressSink) {
              const total = st.totalBytes || 0;
              const loaded = st.loadedBytes || 0;
              const fraction = total > 0 ? Math.min(1, loaded / total) : 0;
              progressSink({
                phase: "loading",
                fraction,
                percent: Math.round(fraction * 100),
                loadedBytes: loaded,
                totalBytes: total,
                stage: "loading",
                message: "Loading OrcaSlicer engine…",
              });
            } else if (st?.state === "fatal") {
              state = "error";
              lastReason = st.code === 2 ? "too_big" : "failed";
              lastError = st.message || "Browser slicer stopped unexpectedly";
            }
            return;
          }
          if (msg.kind === "progress") {
            const p = pending.get(msg.id);
            if (p?.onProgress && msg.progress) {
              const pct = Math.min(100, Math.max(0, Number(msg.progress.percent) || 0));
              p.onProgress({
                phase: "slicing",
                fraction: pct / 100,
                percent: Math.round(pct),
                stage: msg.progress.stage || "slicing",
                message: msg.progress.message || "Slicing…",
              });
            }
            return;
          }
          if (msg.kind === "warning") return;
          const p = pending.get(msg.id);
          if (!p) return;
          pending.delete(msg.id);
          if (msg.kind === "result") {
            p.resolve(msg.result);
          } else {
            const e = new Error(msg.error?.message || "Browser slicer failed");
            e.code = msg.error?.code;
            e.detail = msg.error?.detail;
            e.objects = msg.error?.objects;
            p.reject(e);
          }
        },
        (err) => {
          state = "error";
          lastReason = "failed";
          lastError = err.message;
          tearDownWorker(err);
        },
      );

      const hello = await sendRequest("hello", {
        protocol: { major: 2 },
        client: { name: "fdm-check", version: "1.0.0" },
      });
      const loaded = await sendRequest("load", { variant: "st" });
      if (!definitions) {
        try {
          definitions = await sendRequest("config.definitions", {});
        } catch {
          definitions = null; // fallback routing still works without definitions
        }
      }
      engineInfo = {
        name: hello.engine?.name || "Muon3D Slicer Engine",
        version: hello.engine?.version || ENGINE_VERSION,
        orca: hello.engine?.orca?.version || "2.5.0-dev",
        license: hello.engine?.license || "AGPL-3.0-only",
      };
      maxHeapBytes = hello.limits?.maxHeapBytes || 0;
      state = "ready";
      progressSink = null;
      return { engine: engineInfo, initMs: loaded.initMs };
    })().catch((err) => {
      progressSink = null;
      tearDownWorker(err);
      const isUnavailable = err.reason === "unavailable" || err.code === 6;
      state = isUnavailable ? "unavailable" : "error";
      lastReason = isUnavailable ? "unavailable" : "failed";
      lastError = err.message || String(err);
      throw err;
    });

    return readyPromise;
  }

  function status() {
    return {
      state,
      version: ENGINE_VERSION,
      engine: engineInfo,
      reason: lastReason,
      error: lastError,
      cacheEntries: cache.size,
      cacheBytes,
    };
  }

  async function checkAvailable() {
    if (typeof WebAssembly !== "object" || typeof Worker !== "function") {
      return {
        available: false,
        reason: "unavailable",
        message: "This browser cannot run WebAssembly workers; using fast estimate instead.",
      };
    }
    try {
      const manifest = await fetchManifest();
      return {
        available: true,
        version: manifest.version || ENGINE_VERSION,
        orca: manifest.orca?.version || "2.5.0-dev",
        wasmBytes: manifest.variants?.st?.wasmBytes || 0,
      };
    } catch (err) {
      return {
        available: false,
        reason: "unavailable",
        message: err.message || "Browser slicer engine is not available.",
      };
    }
  }

  async function warmup(onProgress = null) {
    try {
      const [boot] = await Promise.all([ensureReady(onProgress), loadProfiles()]);
      return { ok: true, available: true, initMs: boot.initMs, engine: boot.engine };
    } catch (err) {
      return {
        ok: false,
        available: false,
        fallback: true,
        reason: err.reason === "unavailable" ? "unavailable" : "not_loaded",
        message: err.message || "Could not load the browser slicer engine.",
      };
    }
  }

  function cancel() {
    if (!activeSlice) return false;
    activeSlice.cancel();
    return true;
  }

  function dispose() {
    cancel();
    tearDownWorker(new Error("Slicer disposed"));
    state = "idle";
    engineInfo = null;
  }

  async function slice(job, { onProgress = null, signal = null } = {}) {
    const started = typeof performance !== "undefined" ? performance.now() : Date.now();
    if (signal?.aborted) {
      return { ok: false, fallback: false, cancelled: true, reason: "cancelled", message: "Slicing was cancelled." };
    }

    // 1. Validate mesh and check size cap BEFORE loading the 11 MB WASM engine.
    let normalized;
    try {
      normalized = normalizeMesh(job?.mesh);
    } catch (err) {
      return { ok: false, fallback: true, reason: "failed", message: err.message };
    }
    const maxTris = job?.maxTriangles ?? defaultMaxTriangles;
    if (normalized.triangles > maxTris) {
      return {
        ok: false,
        fallback: true,
        reason: "too_big",
        message: `Part has ${normalized.triangles.toLocaleString("en")} triangles (limit ${maxTris.toLocaleString("en")} for browser slicing); using fast estimate instead.`,
      };
    }

    // 2. Resolve OrcaSlicer profiles (either caller-supplied or matched from bundled orca_profiles.json.gz).
    let chosen;
    try {
      if (job.configs?.machine && job.configs?.process && (job.configs?.filament || job.configs?.filaments?.[0])) {
        const fil = job.configs.filament ?? job.configs.filaments[0];
        chosen = {
          machine: { name: job.configs.machine.name || "custom", data: job.configs.machine.data ?? job.configs.machine },
          process: { name: job.configs.process.name || "custom", data: job.configs.process.data ?? job.configs.process },
          filament: { name: fil.name || "custom", data: fil.data ?? fil },
        };
      } else {
        const bundled = await loadProfiles();
        const printerName = typeof job.printer === "string" ? job.printer : job.printer?.name;
        const materialId = typeof job.material === "string" ? job.material : job.material?.based_on ?? job.material?.id;
        const lh = job.layerHeight ?? job.settings?.layer_height ?? 0.2;
        if (!printerName || !job.nozzle || !materialId) {
          const err = new Error("Printer name, nozzle size and material are required to pick OrcaSlicer profiles");
          err.code = "NO_PROFILE";
          throw err;
        }
        chosen = matchOrcaProfiles(bundled, printerName, String(job.nozzle), materialId, Number(lh));
      }
    } catch (err) {
      return {
        ok: false,
        fallback: true,
        reason: err.code === "NO_PROFILE" ? "no_profile" : "unavailable",
        message: err.message || "Could not match an OrcaSlicer profile for this printer and material.",
      };
    }

    // 3. Place mesh on bed and check cache using canonical hash (before touching WASM).
    const objectName = job.name || "part.stl";
    let placed;
    try {
      const centre = Array.isArray(job.at) && job.at.length === 2 ? job.at : bedCentre(chosen.machine.data);
      placed = placeOnBed(normalized.positions, centre);
    } catch (err) {
      return { ok: false, fallback: true, reason: "failed", message: err.message };
    }

    const preliminaryConfigs = applySettingsOverrides(chosen, job.settings, definitions);
    const inputHash = await hashSliceInput({
      positions: placed,
      indices: normalized.indices,
      configs: preliminaryConfigs,
      objectName,
    });

    const cached = cacheGet(inputHash);
    if (cached) {
      onProgress?.({ phase: "cached", fraction: 1, percent: 100, stage: "cached", message: "Using cached G-code" });
      return { ...cached, ok: true, fallback: false, cached: true };
    }

    if (inFlightByHash.has(inputHash)) {
      const shared = await inFlightByHash.get(inputHash);
      return shared.ok ? { ...shared, cached: true } : shared;
    }

    // 4. Lazily start/load the worker, then slice off the main thread.
    let cancelled = false;
    let resolveCancel = null;
    const cancelRace = new Promise((res) => {
      resolveCancel = res;
    });
    const triggerCancel = () => {
      if (cancelled) return;
      cancelled = true;
      tearDownWorker(new Error("Slicing was cancelled"));
      state = "idle";
      resolveCancel({ ok: false, fallback: false, cancelled: true, reason: "cancelled", message: "Slicing was cancelled." });
    };
    const onAbort = () => triggerCancel();
    signal?.addEventListener("abort", onAbort, { once: true });

    const mySlice = { hash: inputHash, cancel: triggerCancel };
    const runPromise = (async () => {
      activeSlice = mySlice;
      try {
        await ensureReady(onProgress);
        if (cancelled) return { ok: false, fallback: false, cancelled: true, reason: "cancelled", message: "Slicing was cancelled." };

        // Rebuild configs with live config.definitions if definitions just loaded for the first time.
        const configs = applySettingsOverrides(chosen, job.settings, definitions);
        const finalHash = definitions
          ? await hashSliceInput({ positions: placed, indices: normalized.indices, configs, objectName })
          : inputHash;

        const meshPayload = normalized.indices
          ? { positions: placed, indices: normalized.indices }
          : { positions: placed };

        state = "slicing";
        mySlice.hash = finalHash;

        const result = await Promise.race([
          sendRequest(
            "slice",
            {
              configs,
              objects: [{ name: objectName, mesh: meshPayload }],
              output: { toolpaths: false, toolpathExtras: false },
            },
            {
              transfer: [placed.buffer],
              onProgress,
            },
          ),
          cancelRace,
        ]);

        if (cancelled || result?.cancelled) {
          return { ok: false, fallback: false, cancelled: true, reason: "cancelled", message: "Slicing was cancelled." };
        }

        state = "ready";
        const wallMs = Math.round((typeof performance !== "undefined" ? performance.now() : Date.now()) - started);
        const gcodeBytes = result.gcode instanceof Uint8Array ? result.gcode : new Uint8Array(result.gcode);
        const gcode = new TextDecoder("utf-8").decode(gcodeBytes);

        // Recycle the worker if WASM heap grew past half of its 4 GB limit (per Muon3D PROTOCOL §4).
        if (maxHeapBytes > 0 && result.heapBytes && result.heapBytes > maxHeapBytes * 0.5) {
          tearDownWorker();
          state = "idle";
        }

        const entry = {
          hash: finalHash,
          gcode,
          byteLength: gcodeBytes.byteLength,
          stats: result.stats || {},
          warnings: result.warnings || [],
          timings: { ...(result.timings || {}), wallMs },
          profiles: {
            machine: chosen.machine.name,
            process: chosen.process.name,
            filament: chosen.filament.name,
          },
          engine: engineInfo,
        };
        cachePut(inputHash, entry);
        if (finalHash !== inputHash) cachePut(finalHash, entry);
        return { ...entry, ok: true, fallback: false, cached: false };
      } catch (err) {
        if (cancelled) {
          return { ok: false, fallback: false, cancelled: true, reason: "cancelled", message: "Slicing was cancelled." };
        }
        if (err.code === 2) {
          tearDownWorker(err);
          state = "idle";
          return {
            ok: false,
            fallback: true,
            reason: "too_big",
            code: 2,
            message: "Part ran out of memory in the browser slicer; using fast estimate instead.",
            detail: err.detail,
          };
        }
        if (err.code === 1) {
          tearDownWorker(err);
          state = "error";
        } else if (state === "slicing") {
          state = "ready";
        }
        const reason = err.reason === "unavailable" || err.code === 6 ? "unavailable" : "failed";
        return {
          ok: false,
          fallback: true,
          reason,
          code: err.code,
          message: err.message || "Browser slicer could not slice this part; using fast estimate instead.",
          detail: err.detail,
        };
      } finally {
        if (activeSlice === mySlice) activeSlice = null;
        signal?.removeEventListener("abort", onAbort);
        inFlightByHash.delete(inputHash);
      }
    })();

    inFlightByHash.set(inputHash, runPromise);
    return runPromise;
  }

  return {
    status,
    checkAvailable,
    warmup,
    slice,
    cancel,
    clearCache,
    dispose,
  };
}

// Default singleton used by the web app (T-560 print simulation, T-562 G-code download).
const defaultSlicer = createBrowserSlicer();

export const slicerStatus = () => defaultSlicer.status();
export const checkAvailable = () => defaultSlicer.checkAvailable();
export const warmup = (onProgress) => defaultSlicer.warmup(onProgress);
export const sliceInBrowser = (job, opts) => defaultSlicer.slice(job, opts);
export const cancelSlice = () => defaultSlicer.cancel();
export const clearSliceCache = () => defaultSlicer.clearCache();
export const disposeSlicer = () => defaultSlicer.dispose();
