import { Assistant, MODELS } from "./assistant.js";
import { GeminiAssistant, listGeminiModels } from "./gemini.js";
import { listOpenAIModels, OpenAIAssistant } from "./openai.js";
import { applyEdits as applyDesignEdits, stlBytes } from "./geometry.js";
import { readStep } from "./step.js";
import { stepWithEdits } from "./stepwrite.js";
import { MATERIAL_FIELDS, PRINTER_FIELDS, fromOrcaFilament, loadMine, saveMine, toForm, toSpec, toYaml, warpLength, zFactor } from "./mypresets.js";
import { REGION_COLORS, Viewer, FIELD_BANDS, fieldColor } from "./viewer.js";
import { Steps } from "./steps.js";
import { reportHtml } from "./report.js";

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
};

/** Show ``node`` only from this level up (Simple / Advanced / Expert, see style.css). */
const from = (level, node) => ((node.dataset.min = level), node);

// The AI features (Ask AI, paste back an answer) are being redone; hidden unless the page is opened with ?ai=1.
const AI_ENABLED = new URLSearchParams(location.search).has("ai");

// ---------- engine (Pyodide worker) ----------
// Cancelling kills the worker (Python can't be interrupted mid-call); a fresh one boots and
// gets the current part again before any other call runs.
let worker;
let engineReady;
let restoring = Promise.resolve();
const pending = new Map();
let nextId = 0;

class Cancelled extends Error {}

function startWorker() {
  worker = new Worker(new URL("worker.js", import.meta.url), { type: "module" });
  engineReady = new Promise((resolve, reject) => {
    worker.addEventListener("message", ({ data }) => {
      if (data.type === "ready") resolve();
      else if (data.type === "boot-error") reject(new Error(data.error));
      else if (data.type === "progress") showProgress(data.fraction);
      else if (pending.has(data.id)) {
        const { resolve: ok, reject: fail } = pending.get(data.id);
        pending.delete(data.id);
        data.error ? fail(new Error(data.error)) : ok(data.result);
      }
    });
  });
}
startWorker();

function post(method, args = {}, data = null) {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    showProgress(null);
    worker.postMessage({ id, method, args, data });
  });
}

async function call(method, args = {}, data = null) {
  await restoring;
  return post(method, args, data);
}

function cancelEngine() {
  worker.terminate();
  for (const { reject } of pending.values()) reject(new Cancelled("Cancelled"));
  pending.clear();
  startWorker();
  const src = state.source;
  const mine = loadMine();
  const expert = loadExpert();
  if (src || Object.keys(mine.printers).length || Object.keys(mine.materials).length || Object.keys(expert).length) {
    restoring = engineReady.then(async () => {
      await post("custom_presets", mine);
      if (Object.keys(expert).length) await post("set_expert", { values: expert });
      if (!src) return;
      await post("load", { name: src.name, face_ids: src.faceIds ? Array.from(src.faceIds) : undefined }, src.bytes);
      if (src.body) await post("select_body", { index: src.body });
      if (src.scale !== 1) await post("scale", { factor: src.scale });
      if (state.gcode) await post("load_gcode", { up: state.gcode.up, profile: profile() }, state.gcode.bytes);
    }).catch((err) => setStatus(`Could not restart the engine: ${err.message}`, true));
  }
}

// Cancelled work rejects up through every await; that's expected, not an error to log.
addEventListener("unhandledrejection", (e) => e.reason instanceof Cancelled && e.preventDefault());

// ---------- state ----------
const viewer = new Viewer($("viewer"));
const state = {
  name: "part",
  triangles: null,
  options: [],
  active: 0,
  check: null,
  placed: null,
  presets: null,
  // strength
  faceRegions: null, // Uint32Array: region id per triangle
  regionInfo: [],
  selectedRegion: -1,
  loads: [],
  marks: [], // faces the user marked: {region, kind: "clean" | "bed"}
  strength: null,
  showHeat: false,
  overrides: {}, // slicer settings the user applied from strength suggestions
  zones: [], // strengthening zones from the optimiser: {lo, hi, walls, infill, corners_print}
  volumes: [], // advanced design boxes: {kind, lo, hi, corners_print, notes, from, margin, what}
  fitNotes: {}, // region -> {partner, fit, text}: what goes into a hole / onto a post (set_fit)
  edits: [], // suggested design edits: {id, title, description, op: "add"|"cut", mesh: Float32Array, pieces, selected}
  features: [], // detected holes/posts from the last design suggestion
  versions: [], // models to switch between and compare: {label, source, step, results: {time, grams, problems, safety}}
  version: 0,
};

// Which step can open: everything after the part needs one, everything after "What you have" a finished check.
const steps = new Steps({
  ready: (step) => step === "part" ? true
    : !state.triangles ? "Load a part first"
    : step === "have" || state.check ? true : "Checking your part…",
  changed: () => {
    recolor();
    suggestDesignIfShown();
    // A note about the last action ("Report copied …") belongs to the step it was made in.
    if (!busyCount && state.summary) setStatus(state.summary);
  },
});

// Debug/test hook: lets automated browser tests and devtools inspect the app.
globalThis.fdm = { state, viewer, steps, call: (...args) => call(...args) };

const profile = () => ({
  printer: $("printer").value,
  material: $("material").value,
  nozzle: $("nozzle").value,
  multi_material: $("multi-material").checked,
  goal: document.querySelector("input[name=goal]:checked").value,
  use_temp_c: $("use-temp").value === "" ? null : Number($("use-temp").value),
  constant_load: $("constant-load").checked,
});
const mode = () => document.querySelector("input[name=mode]:checked").value;
const activeOption = () => state.options[state.active];

function setStatus(text, isError = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", isError);
}

// Busy bookkeeping on <body data-busy data-done> lets automated tests wait for the app to settle.
let busyCount = 0;
let doneCount = 0;

let progressTimer = 0;
let workingClock = 0;
let workFraction = null;
let workingTick = () => {};

/** The "working" badge over the 3D view: what runs, for how long, and Cancel — visible wherever the sidebar is scrolled. */
function showWorking(text) {
  $("working-text").textContent = text;
  if (workingClock) return;
  const start = performance.now();
  workFraction = null;
  const tick = (workingTick = () => {
    const s = (performance.now() - start) / 1000;
    // Once the engine reports progress, extrapolate: rough, but says whether to wait or come back.
    const left = workFraction > 0.05 && workFraction < 1 ? Math.max(1, Math.round((s * (1 - workFraction)) / workFraction)) : null;
    $("working-time").textContent = left ? `${fmtSeconds(s)}, about ${fmtSeconds(left)} left` : fmtSeconds(s);
  });
  tick();
  workingClock = setInterval(tick, 1000);
  $("working").hidden = false;
}

function hideWorking() {
  clearInterval(workingClock);
  workingClock = 0;
  $("working").hidden = true;
}

/** Run ``fn`` with the working badge; ``affects``: the steps whose results it will update. */
async function busy(text, fn, affects = []) {
  setStatus(text);
  document.body.dataset.busy = ++busyCount;
  steps.setBusy(affects, true);
  showAffects();
  // Only show the bar for work that takes a moment; quick checks would just flicker.
  if (busyCount === 1) progressTimer = setTimeout(() => (($("progress").hidden = false), showWorking($("status").textContent)), 400);
  else if (workingClock) showWorking(text);
  try {
    return await fn();
  } catch (err) {
    if (err instanceof Cancelled) setStatus("Cancelled — the last result stays shown.");
    else setStatus(err.message, true);
    throw err;
  } finally {
    steps.setBusy(affects, false);
    showAffects();
    document.body.dataset.busy = --busyCount;
    document.body.dataset.done = ++doneCount;
    if (!busyCount) {
      clearTimeout(progressTimer);
      $("progress").hidden = true;
      hideWorking();
    }
  }
}

/** Under the working badge: which other steps will change when this finishes. */
function showAffects() {
  const names = steps.busyNames();
  $("working-affects").hidden = !names.length;
  $("working-affects").textContent = `Updates ${names.join(", ")}`;
}

/** ``fraction`` 0..1 from the engine, or null for "working, amount unknown". */
function showProgress(fraction) {
  const bar = $("progress-bar");
  workFraction = fraction;
  if (workingClock) workingTick();
  if (fraction == null) bar.removeAttribute("value");
  else bar.value = fraction;
}

// ---------- flow ----------
/** Entry point for user files: STEP goes through OpenCascade first; STL and 3MF straight to the engine. */
/** ``as``: "new" starts over (a new part), "version" adds it as a version to compare (see versions). */
async function openFile(name, bytes, as = "new") {
  $("body-pick").hidden = true;
  if (as === "version") addVersion(name.replace(/\.(stl|3mf|step|stp)$/i, ""));
  if (!/\.(step|stp)$/i.test(name)) return loadFile(name, bytes, null, null, as);
  const bodies = await busy("Reading STEP file (first time downloads ~8 MB)…", () => readStep(bytes));
  if (!bodies.length) return setStatus("No solid bodies found in this STEP file.", true);
  const pick = async (i) => {
    // Kept so design edits can be written back into the original CAD solid (stepwrite.js).
    const step = { name, bytes, volume: bodies[i].volume, edits: [] };
    await loadFile(`${name.replace(/\.(step|stp)$/i, "")}${bodies.length > 1 ? `_${i + 1}` : ""}.stl`, stlBytes(bodies[i].soup), bodies[i].faceIds, step, as);
  };
  if (bodies.length > 1) {
    showBodyPicker(bodies.map((b) => ({ name: b.name, volume_mm3: b.volume })), 0, pick);
  }
  await pick(0);
}

/** Dropdown for files with several parts; ``pick(i)`` loads body i. */
function showBodyPicker(bodies, active, pick) {
  $("body").replaceChildren(
    ...bodies.map((b, i) => el("option", { value: i, textContent: `${b.name} (${(b.volume_mm3 / 1000).toFixed(1)} cm³)`, selected: i === active })),
  );
  $("body").onchange = () => pick(Number($("body").value));
  $("body-pick").hidden = false;
}

/** ``step``: the STEP file this part came from (and the edits applied so far), or null. ``as``: "new"
 * starts the version list over; otherwise the model is the current version (see versions). */
async function loadFile(name, bytes, faceIds = null, step = null, as = "new") {
  await busy("Reading the part…", async () => {
    await engineReady;
    const info = await call("load", { name, face_ids: faceIds ? Array.from(faceIds) : undefined }, bytes);
    state.step = step;
    $("dl-step").hidden = !step?.edits.length;
    state.source = { name, bytes, faceIds, body: 0, scale: 1 }; // to restore the part after a cancel
    if (as === "new" || !state.versions.length) {
      state.versions = [{ label: "Original" }];
      state.version = 0;
      undoStack.length = 0;
      renderUndo();
    }
    Object.assign(state.versions[state.version], { source: state.source, step });
    renderVersions();
    if (info.bodies.length) {
      showBodyPicker(info.bodies, info.body, (i) => busy("Switching part…", async () => {
        const next = await call("select_body", { index: i });
        state.source.body = i;
        await partLoaded(next);
      }));
    }
    await partLoaded(info);
  });
}

/** Common tail after load / body switch / rescale: reset part state, then orient and check. */
async function partLoaded(info) {
  state.name = info.name;
  // Copy: bytes from Pyodide may not be 4-byte aligned for a Float32Array view.
  state.triangles = new Float32Array(info.triangles.slice().buffer);
  Object.assign(state, { faceRegions: null, regionInfo: [], selectedRegion: -1, loads: [], marks: [], strength: null, overrides: {}, fitNotes: {} });
  state.gcode = null; // a new part (or body, or scale): the old G-code doesn't belong to it
  state.runTimes = {}; // how long each simulation took on this part
  showEstimates();
  renderGcode();
  renderMarks();
  viewer.framed = false;
  const [x, y, z] = info.size_mm.map((v) => v.toFixed(1));
  const body = info.bodies.length ? ` (${info.bodies[info.body].name})` : "";
  state.summary = `${info.name}${body}: ${x} × ${y} × ${z} mm`;
  showUnitHint(info.unit_hint);
  renderLoads();
  // Drop the previous part's options now, so a cancel mid-way can't leave them on the new part.
  state.options = [];
  state.check = null;
  renderOptions();
  $("view-empty").hidden = true;
  $("view-key").hidden = false;
  if (steps.current === "part") steps.go("have");
  else steps.render();
  await findOrientations();
}

/** Parts exported in inches or metres arrive 25.4× / 1000× off; offer a one-click fix. */
function showUnitHint(hint) {
  $("unit-hint").hidden = !hint;
  if (!hint) return;
  $("unit-text").textContent = `${hint.text} — was it exported in ${hint.unit}?`;
  $("unit-scale").textContent = `Scale ×${hint.factor} → ${hint.scaled_mm} mm`;
  $("unit-scale").onclick = () => busy("Rescaling…", async () => {
    const next = await call("scale", { factor: hint.factor });
    state.source.scale *= hint.factor;
    state.step = null; // the CAD file keeps its own units: edits would no longer line up
    $("dl-step").hidden = true;
    await partLoaded(next);
  });
}

async function findOrientations(withLoads = false) {
  // With the Strength goal, known loads always count (the simulation beats the pin/neck heuristics).
  const loads = (withLoads || profile().goal === "strength") && readyForStrength() ? state.loads : undefined;
  await busy(loads ? "Finding the best orientation for printing and strength…" : "Finding the best orientation…", async () => {
    const r = await call("orientations", { profile: profile(), loads, faces: faceMarks() });
    state.options = [...r.orientations, r.as_loaded];
    renderOptions();
    await selectOption(0);
  }, ["orient", "problems", "strength", "settings", "result"]);
}

async function selectOption(i) {
  state.active = i;
  [...$("options").children].forEach((li, k) => li.classList.toggle("active", k === i));
  state.placed = viewer.showPart(state.triangles, activeOption().rotation);
  $("view-label").hidden = false;
  $("view-label").textContent = i === 0 ? "Best orientation" : activeOption().label;
  viewer.clearMarkers(); // remove old orientation's warning boxes immediately
  state.strength = null;
  state.showHeat = false;
  $("strength-result").replaceChildren();
  $("dynamics-result").textContent = "";
  state.swing = null;
  state.showSwing = false;
  renderGcode();
  setZones([]); // zones belong to one orientation
  setVolumes([]);
  state.warp = null;
  state.showWarp = false;
  $("warp-result").replaceChildren();
  clearEdits();
  recolor();
  await runCheck();
  if ($("nozzle-block").open) compareNozzles(); // orientation-specific: refresh, don't collapse
}

let checkToken = 0;

async function runCheck() {
  if (!activeOption()) return;
  // Only the latest request may update the UI: clicks during a running check would otherwise
  // let an older result (or an older orientation's result) overwrite the newer one.
  const token = ++checkToken;
  await busy("Checking printability…", async () => {
    const result = await call("check", { up: activeOption().up, profile: profile(), mode: mode(), faces: faceMarks(), detailed: sameUp(state.gcode?.up, activeOption().up) });
    if (token !== checkToken) return;
    if (!result?.findings) throw new Error("The analysis returned no result — please try again");
    state.check = result;
    renderCheck();
    $("design-block").hidden = mode() === "print";
    $("ai-block").hidden = !AI_ENABLED;
    $("paste-block").hidden = !AI_ENABLED;
    setStatus(state.summary);
    steps.render();
    suggestDesignIfShown();
  }, ["problems", "settings", "result"]);
}

// ---------- rendering ----------
function renderOptions() {
  $("options").replaceChildren(
    ...state.options.map((o, i) =>
      el(
        "li",
        { onclick: () => selectOption(i) },
        el("strong", { textContent: o.label }, i === 0 ? el("span", { className: "tag", textContent: "Recommended" }) : ""),
        el("ul", {}, ...o.reasons.map((r) => el("li", { textContent: r }))),
        o.why_lower ? el("p", { className: "why-lower", textContent: o.why_lower }) : "",
      ),
    ),
  );
}

const SEVERITY_LABEL = { problem: "Problem", warn: "Warning", info: "Tip" };
const DOMAIN_LABEL = { geometry: "Design", slicer: "Slicer", process: "Printer & material" };
const FIX_PATH = { geometry: "Fix: change the design", slicer: "Fix: a slicer setting", process: "Fix: printer or material" };

// Findings are grouped by how the print differs from the CAD model; "info" starts collapsed.
const IMPACTS = [
  ["fails", "Can fail"], // severity on each card says how likely (a tip here = a setting handles it)
  ["shape", "Shape or size will differ from your CAD"],
  ["surface", "Surface quality"],
  ["weak", "Weaker than it looks"],
  ["info", "Good to know"],
];
const SEVERITY_RANK = { problem: 0, warn: 1, info: 2 };

function renderImpactGroups(findings) {
  const groups = [];
  for (const [impact, label] of IMPACTS) {
    const inGroup = findings.map((f, i) => ({ f, i })).filter(({ f }) => f.impact === impact);
    if (!inGroup.length) continue;
    // Same rule found several times (e.g. 4 small holes) -> one card listing each place.
    const byRule = new Map();
    for (const item of inGroup) byRule.set(item.f.rule, [...(byRule.get(item.f.rule) ?? []), item]);
    const cards = [...byRule.values()].map((items) => findingCard(findings, items));
    groups.push(
      el(
        "li",
        { className: `impact ${impact}` },
        el(
          "details",
          { open: impact !== "info" },
          el("summary", { className: "impact-head", textContent: `${label} (${byRule.size})` }),
          el("ol", { className: "findings" }, ...cards),
        ),
      ),
    );
  }
  return groups;
}

function findingCard(findings, items) {
  const first = items[0].f;
  const worst = items.reduce((a, b) => (SEVERITY_RANK[b.f.severity] < SEVERITY_RANK[a.f.severity] ? b : a)).f.severity;
  const fixes = first.fixes.map((fx) =>
    el("li", {}, el("span", { className: `chip ${fx.domain}`, textContent: DOMAIN_LABEL[fx.domain] }), " ", fx.text),
  );
  if (first.hidden_fixes?.length) {
    const hiddenLabels = first.hidden_fixes.map((d) => DOMAIN_LABEL[d]).join(" / ");
    const where = document.body.dataset.level === "simple" || document.body.dataset.level === "basic" ? "in Advanced" : "with \"What can you change?\"";
    fixes.push(el("li", { className: "hint-fix", textContent: `More fixes in ${hiddenLabels} ${where}.` }));
  }

  const selectFinding = (idx, targetNode = null) => {
    document.querySelectorAll(".findings .selected-place, .findings > li.focused").forEach((n) => n.classList.remove("selected-place", "focused"));
    if (targetNode) targetNode.classList.add("selected-place");
    viewer.showFindings(findings, idx);
  };

  const places = items.map(({ f, i }) =>
    el("li", {
      className: "place",
      textContent: f.detail,
      onclick: (e) => {
        e.stopPropagation();
        selectFinding(i, e.currentTarget);
      },
      title: "Click to zoom camera and highlight on the part"
    }),
  );

  const card = el(
    "li",
    { className: worst },
    el(
      "details",
      {
        ontoggle: (e) => {
          if (e.target.open) {
            document.querySelectorAll(".findings > li.focused").forEach((n) => n.classList.remove("focused"));
            card.classList.add("focused");
            selectFinding(items[0].i);
          }
        }
      },
      el("summary", {
        onclick: () => {
          document.querySelectorAll(".findings > li.focused").forEach((n) => n.classList.remove("focused"));
          card.classList.add("focused");
          selectFinding(items[0].i);
        }
      },
        el("span", { className: "f-head" },
          el("b", { textContent: first.title + (items.length > 1 ? ` ×${items.length}` : "") }),
          el("span", { className: "sev", textContent: SEVERITY_LABEL[worst] })),
        // Readable without opening: what happens, and what kind of fix there is.
        el("span", { className: "gist", textContent: first.why.split(/(?<=\.)\s/)[0] }),
        el("span", { className: "paths" },
          first.fixes.length ? el("span", { className: `chip ${first.fixes[0].domain}`, textContent: FIX_PATH[first.fixes[0].domain] }) : "",
          first.goal_note ? el("small", { className: "goal-tag", textContent: first.goal_note }) : ""),
      ),
      items.length > 1 ? el("ul", { className: "places" }, ...places) : el("p", {
        className: "detail clickable",
        textContent: first.detail,
        onclick: () => selectFinding(items[0].i),
        title: "Click to zoom camera and highlight on the part"
      }),
      el("p", { className: "why", textContent: first.why }),
      fixes.length ? el("ul", { className: "fixes" }, ...fixes) : el("p", { className: "why", textContent: "No fixes for this mode." }),
    ),
  );
  return card;
}

function renderCheck() {
  if (!state.check) return;
  const { findings, stats } = state.check;
  $("stats").textContent = `About ${fmtTime(stats.print_time_min)} to print and ${Math.round(stats.filament_g)} g of filament.`;
  noteVersion({ time: stats.print_time_min, grams: stats.filament_g, problems: new Set(findings.filter((f) => f.impact !== "info").map((f) => f.rule)).size, safety: null });
  $("findings").replaceChildren(
    ...(findings.length ? renderImpactGroups(findings) : [el("li", { className: "ok", textContent: "Should print as designed in this orientation." })]),
  );
  renderSettings();
  renderVerdict();
  renderCooling();
  recolor();
}

/** One line on top of Simple's result: can it fail, and what the settings already handle. */
function renderVerdict() {
  const v = verdict();
  $("verdict").replaceChildren(el("div", { className: `summary-box ${v.kind}` }, el("strong", { textContent: v.head }), el("span", { textContent: v.text })));
}

function verdict() {
  const { findings } = state.check;
  const rules = (pred) => new Set(findings.filter(pred).map((f) => f.rule)).size;
  const fails = rules((f) => f.impact === "fails" && f.severity !== "info");
  const others = rules((f) => !(f.impact === "fails" && f.severity !== "info") && f.impact !== "info");
  const settings = effectiveSettings().length;
  const [kind, head] = fails ? ["warn", `Prints, but ${fails === 1 ? "one thing" : `${fails} things`} can fail`]
    : others ? ["ok", `Prints, with ${others === 1 ? "one thing" : `${others} things`} to know`]
    : ["ok", "Prints as designed"];
  const p = profile();
  return { kind, head, text: `${state.active === 0 ? "Turned the best way" : `Turned as you picked: ${activeOption()?.label}`}. ${settings} slicer settings for ${state.presets.materials[p.material]?.name ?? p.material} on your ${state.presets.printers[p.printer]?.name ?? p.printer}.` };
}
const ABOVE_BED_MM = 3; // a little above thresholds.yaml cooling.above_bed_mm (2 mm): the bed warmth fades out

/** The cooling check in one sentence: the hottest spot against the material's softening point. */
function renderCooling() {
  const h = state.check.stats.layer_heat;
  const mat = state.presets.materials[profile().material];
  if (!h?.temp_c.length) return $("cooling-summary").replaceChildren();
  // Like the check: the first few mm are kept warm by the bed on purpose, not by the print.
  let at = -1;
  h.temp_c.forEach((t, i) => h.z[i] > ABOVE_BED_MM && (at < 0 || t > h.temp_c[at]) && (at = i));
  if (at < 0) return $("cooling-summary").replaceChildren();
  const finding = state.check.findings.find((f) => f.rule === "small_layers_cooling");
  const fan = mat?.fan_pct != null ? ` With the part fan at ${mat.fan_pct} %${state.check.stats.heat_from_gcode ? ", layer times from your G-code" : ""}.` : "";
  $("cooling-summary").replaceChildren(finding
    ? el("span", {}, el("b", { textContent: "Some layers stay too warm. " }), finding.detail, ".")
    : el("span", {}, el("b", { textContent: "Every layer cools in time. " }),
      `Warmest about ${Math.round(h.temp_c[at])} °C at ${h.z[at].toFixed(0)} mm (${mat?.name ?? "it"} softens from about ${Math.round(h.softening_c)} °C; a bit above is normal right under the nozzle).${fan}`));
}

$("show-temp").onchange = () => {
  if ($("show-temp").checked) state.showHeat = false;
  recolor();
};

const OWN_LABEL = { xy_hole_compensation: "Hole compensation", hole_to_polyhole: "Polyholes" };

/** Recommended settings with the user's applied strength suggestions on top. */
function effectiveSettings() {
  if (!state.check) return [];
  const list = state.check.settings.map((s) => (s.key in state.overrides ? { ...s, value: state.overrides[s.key], changed: true } : s));
  for (const [key, value] of Object.entries(state.overrides)) {
    if (!list.some((s) => s.key === key)) list.push({ key, value, label: OWN_LABEL[key] ?? key, reason: OWN_LABEL[key] ? "Set by you" : "From strength check", changed: true });
  }
  return list;
}

function renderSettings() {
  const settings = effectiveSettings();
  const value = (key) => settings.find((s) => s.key === key)?.value;
  $("hole-comp").value = Number(value("xy_hole_compensation") ?? 0);
  $("polyholes").checked = ["1", "true"].includes(String(value("hole_to_polyhole")));
  $("settings-block").hidden = !settings.length;
  $("settings").replaceChildren(
    ...settings.map((s) =>
      el("tr", { title: s.reason },
        el("td", {}, s.label, s.changed ? el("small", { className: "changed", textContent: " changed by you" }) : ""),
        el("td", { textContent: settingText(s.key, s.value) })),
    ),
  );
  renderShells();
}

// Slicer values as a person reads them; the 3MF keeps the raw values.
const ON_OFF = new Set(["enable_support", "hole_to_polyhole", "slow_down_for_layer_cooling", "precise_outer_wall"]);
const VALUE_WORDS = {
  brim_type: { no_brim: "Off", outer_only: "Outside only", brim_ears: "Brim ears", outer_and_inner: "Outside and inside" },
  support_type: { "tree(auto)": "Tree (automatic)", "normal(auto)": "Normal (automatic)" },
  ironing_type: { topmost: "Top surface only", top: "All top surfaces", "no ironing": "Off" },
  seam_slope_type: { external: "Outer wall", all: "All walls", none: "Off" },
  support_interface_pattern: { rectilinear_interlaced: "Rectilinear, crossed" },
};
const UNITS = { layer_height: "mm", brim_width: "mm", xy_hole_compensation: "mm", elefant_foot_compensation: "mm", support_threshold_angle: "°", bridge_speed: "mm/s" };
function settingText(key, value) {
  const v = String(value);
  if (ON_OFF.has(key)) return v === "1" || v === "true" ? "On" : "Off";
  if (VALUE_WORDS[key]?.[v]) return VALUE_WORDS[key][v];
  if (UNITS[key]) return `${v}${UNITS[key] === "°" ? "" : " "}${UNITS[key]}`;
  return /^[a-z_()]+$/.test(v) ? (v[0].toUpperCase() + v.slice(1)).replaceAll("_", " ") : v;
}

/** Durations a person reads at a glance: "45 s", "2 min 05 s", "1 h 03 min". */
function fmtSeconds(s) {
  s = Math.round(s);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`;
  return `${Math.floor(s / 3600)} h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")} min`;
}

function fmtTime(min) {
  return min < 60 ? `${Math.round(min)} min` : `${Math.floor(min / 60)} h ${Math.round(min % 60)} min`;
}

// ---------- strength: picking faces and loads ----------
/** The step shown now (in Expert's long page: the one scrolled to). null = show everything. */
const viewStep = () => steps.current;

/** Does the 3D view show the things of ``block`` (strength, problems, design) now? Blocks move
 * between steps per level (Simple shows the load under "What you have"), so ask the page. */
let pictureOf = null; // while the report takes pictures: draw only this block's things

function shows(block) {
  if (pictureOf) return block === pictureOf;
  const at = viewStep();
  return at == null || $(`blk-${block}`)?.closest(".pane")?.dataset.pane === at;
}

/** Everything drawn on the part, for the step shown: loads and results in "Will it hold?", heat and
 * warping in "Problems", edit previews in "Design changes" — each step shows its own things only. */
function recolor() {
  const option = activeOption();
  if (!option) return;
  viewer.showLoadSymbols(shows("strength") ? loadSymbols() : []);
  viewer.showGhosts(shows("design") ? state.edits.filter((e) => e.selected) : []);
  drawBoxes();
  showMarkers();
  if (strengthView()) return;
  if (shows("problems") && $("show-temp").checked && state.check?.face_temp) return viewer.colorByHeat(state.check.face_temp);
  const roles = new Map([...state.marks.map((m) => [m.region, m.kind]), ...state.loads.filter((l) => !l.point).map((l) => [l.region, l.kind])]);
  const spot = $("at-spot").checked && !$("pick-panel").hidden && state.pickedPoint;
  const regions = shows("strength") || shows("design") || shows("fits") || viewStep() === "orient";
  viewer.colorByClass(option.face_classes, (f) => {
    if (!state.faceRegions || !regions) return null;
    const r = state.faceRegions[f];
    if (r === state.selectedRegion && !spot) return REGION_COLORS.selected;
    return roles.has(r) ? REGION_COLORS[roles.get(r)] : null;
  });
  const spots = shows("strength") ? state.loads.filter((l) => l.point).map((l) => ({ point: l.point, radius: l.radius, color: REGION_COLORS[l.kind] })) : [];
  if (spot) spots.push({ point: state.pickedPoint, radius: SPOT_RADIUS, color: REGION_COLORS.selected });
  viewer.showSpots(spots);
}

/** Boxes and dots on the part: the weakest spot with a strength result, the first corners to lift
 * with the warp shape, else the problem boxes (not over a design preview or the load setup). */
function showMarkers() {
  if (shows("sims") && state.showSwing && state.swing) return viewer.showFindings([]);
  const r = state.strength;
  if (shows("strength") && state.showHeat && r) {
    viewer.showFindings([]);
    return r.weak_marker ? viewer.markWeak(r.weak_marker.point, r.weak_marker.normal) : viewer.mark(r.critical_point_print);
  }
  if (shows("sims") && state.showWarp && state.warp) {
    viewer.showFindings([]);
    for (const p of state.warp.lift_points_print) viewer.mark(p);
    return;
  }
  viewer.showFindings(viewStep() == null || ["orient", "problems", "result"].includes(viewStep()) ? state.check?.findings ?? [] : []);
}

/** Lock on held faces, arrow + weight on loads, for the 3D view. */
function loadSymbols() {
  if (!state.regionInfo.length) return [];
  return state.loads.map((l, i) => {
    const info = state.regionInfo[l.region];
    const normal = l.normal ?? info?.normal ?? [0, 0, 1];
    const point = l.point ?? info?.centroid;
    if (l.kind === "fixed") return { kind: "fixed", point, normal };
    const dir = loadVector(l);
    return { kind: "force", load: i, point, normal, dir, label: `${l.kg} kg${multiCase() ? ` · case ${l.case}` : ""}` };
  }).filter((s) => s.point);
}

async function ensureRegions() {
  if (state.faceRegions) return;
  await busy("Loading strength tools (first time downloads ~10 MB)…", async () => {
    const r = await call("regions");
    state.faceRegions = new Uint32Array(r.face_regions.slice().buffer);
    state.regionInfo = r.regions;
    setStatus(state.summary);
  });
}

// Treat a pointer press as a click only if the camera wasn't dragged.
let downAt = null;
$("viewer").addEventListener("pointerdown", (e) => (downAt = [e.clientX, e.clientY]));
$("viewer").addEventListener("pointerup", async (e) => {
  if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 4 || !state.triangles) return;
  const hit = viewer.pickHit(e);
  if (!hit) return;
  await ensureRegions();
  if (state.zonePick) return addZone(hit);
  if (state.volPick) return pickVolume(hit);
  state.selectedRegion = state.faceRegions[hit.face];
  state.pickedPoint = hit.point;
  state.pickedOnce = true;
  hoverTip.hidden = true;
  const info = state.regionInfo[state.selectedRegion];
  $("pick-info").textContent = info.hole_diameter
    ? `Ø${info.hole_diameter.toFixed(1)} mm hole`
    : `${info.kind} face, ${Math.round(info.area_mm2)} mm²`;
  $("hold").value = info.hole_diameter ? "screw" : "clamped";
  $("pick-panel").hidden = false;
  steps.reveal($("pick-panel"));
  state.showHeat = false;
  recolor();
});

const SPOT_RADIUS = 3; // mm, for loads "only around the clicked spot"
const where = (l) => (l.point ? `spot ${l.point.map((v) => v.toFixed(1)).join(",")}` : `face ${l.region}`);

/** The clicked face, or the spot around the clicked point when "only around the spot" is ticked. */
function target() {
  return $("at-spot").checked
    ? { point: state.pickedPoint, radius: SPOT_RADIUS, normal: state.regionInfo[state.selectedRegion]?.normal }
    : { region: state.selectedRegion };
}

function addLoad(load) {
  remember(load.kind === "fixed" ? "where it is held" : "a load");
  // One entry per face (or spot) and case: it can be loaded differently in each case, and "held" is shared.
  const same = (l) => where(l) === where(load) && (l.kind === "fixed" || load.kind === "fixed" || l.case === load.case);
  state.loads = state.loads.filter((l) => !same(l)).concat(load);
  state.selectedRegion = -1;
  $("pick-panel").hidden = true;
  renderLoads();
  recolor();
}

// ---------- marked faces (critical faces) ----------
/** {clean: [region ids], bed: [...]} for the engine, or undefined when nothing is marked. */
function faceMarks() {
  if (!state.marks?.length) return undefined;
  const of = (kind) => state.marks.filter((m) => m.kind === kind).map((m) => m.region);
  return { clean: of("clean"), bed: of("bed"), vertical: of("vertical") };
}

const MARK_LABEL = { clean: "Keep clean (no support marks)", bed: "On the bed", vertical: "Upright (accurate shape)" };

function setMark(kind) {
  remember("a marked face");
  const region = state.selectedRegion;
  state.marks = state.marks.filter((m) => m.region !== region && !(kind === "bed" && m.kind === "bed")).concat({ region, kind });
  state.selectedRegion = -1;
  $("pick-panel").hidden = true;
  renderMarks();
  findOrientations(Boolean(state.strength)); // keep strength in the ranking if it was checked
}

function renderMarks() {
  $("marks").replaceChildren(
    ...(state.marks ?? []).map((m, i) =>
      el("li", { className: m.kind }, el("span", { textContent: MARK_LABEL[m.kind] }),
        el("button", { textContent: "×", title: "Remove", onclick: () => {
          remember("removing a marked face");
          state.marks.splice(i, 1);
          renderMarks();
          findOrientations(Boolean(state.strength)); // keep strength in the ranking if it was checked
        } })),
    ),
  );
}
$("mark-clean").onclick = () => setMark("clean");
$("mark-bed").onclick = () => setMark("bed");
$("mark-vertical").onclick = () => setMark("vertical");

$("as-fixed").onclick = () => addLoad({ ...target(), kind: "fixed", ...($("hold").value === "clamped" ? {} : { hold: $("hold").value }) });
$("as-load").onclick = () =>
  addLoad({ ...target(), kind: "force", kg: Number($("kg").value), direction: $("dir").value, case: Number($("load-case").value) });
$("at-spot").onchange = recolor;

const DIR_LABEL = { push: "pushing", pull: "pulling", down: "hanging down" };
const DEG = Math.PI / 180;

/** Direction a force acts in (unit vector, part coordinates). */
function loadVector(l) {
  if (Array.isArray(l.direction)) return l.direction;
  const n = l.normal ?? state.regionInfo[l.region]?.normal ?? [0, 0, 1];
  return { push: n.map((v) => -v), pull: n, down: [0, 0, -1] }[l.direction] ?? n;
}
/** turn: around the model's Z axis from +X; tilt: up from horizontal (degrees). */
const toAngles = ([x, y, z]) => ({ turn: Math.round(Math.atan2(y, x) / DEG), tilt: Math.round(Math.asin(Math.max(-1, Math.min(1, z))) / DEG) });
const fromAngles = (turn, tilt) => [Math.cos(tilt * DEG) * Math.cos(turn * DEG), Math.cos(tilt * DEG) * Math.sin(turn * DEG), Math.sin(tilt * DEG)].map((v) => Math.round(v * 1e4) / 1e4);
function dirLabel(l) {
  if (!Array.isArray(l.direction)) return DIR_LABEL[l.direction];
  const a = toAngles(l.direction);
  return `at an angle (turn ${a.turn}°, tilt ${a.tilt}°)`;
}

/** Edit a load in place: weight, direction (preset or any angle), load case. */
function loadEditor(l) {
  const a = toAngles(loadVector(l));
  const kg = el("input", { type: "number", min: 0.1, step: 0.5, value: l.kg, "aria-label": "Load in kg" });
  const dir = el("select", { "aria-label": "Direction" },
    ...[["push", "pushing on the face"], ["pull", "pulling the face"], ["down", "hanging down (−Z as modelled)"], ["angle", "at an angle…"]]
      .map(([v, t]) => el("option", { value: v, textContent: t })));
  dir.value = Array.isArray(l.direction) ? "angle" : l.direction;
  const kase = el("select", { "aria-label": "Load case" }, ...[1, 2, 3].map((c) => el("option", { value: c, textContent: `case ${c}` })));
  kase.value = l.case ?? 1;
  const out = el("span", { textContent: `turn ${a.turn}°, tilt ${a.tilt}°` });
  const turn = el("input", { type: "range", min: -180, max: 180, step: 5, value: a.turn, "aria-label": "Turn" });
  const tilt = el("input", { type: "range", min: -90, max: 90, step: 5, value: a.tilt, "aria-label": "Tilt" });
  const angles = el("div", { className: "angles", hidden: dir.value !== "angle" },
    el("label", {}, "Turn ", turn), el("label", {}, "Tilt ", tilt), out,
    el("p", { className: "hint", textContent: "Or drag the arrow on the part: it follows the mouse." }));
  const changed = () => {
    viewer.showLoadSymbols(loadSymbols());
    $("loads").querySelector(`#load-${state.loads.indexOf(l)} > span`).textContent = loadText(l);
  };
  kg.oninput = () => {
    if (Number(kg.value) > 0) l.kg = Number(kg.value);
    changed();
  };
  kase.onchange = () => ((l.case = Number(kase.value)), renderLoads());
  dir.onchange = () => {
    angles.hidden = dir.value !== "angle";
    l.direction = dir.value === "angle" ? fromAngles(Number(turn.value), Number(tilt.value)) : dir.value;
    changed();
  };
  turn.oninput = tilt.oninput = () => {
    l.direction = fromAngles(Number(turn.value), Number(tilt.value));
    out.textContent = `turn ${turn.value}°, tilt ${tilt.value}°`;
    changed();
  };
  return el("div", { className: "angle-editor" },
    el("div", { className: "row" }, kg, " kg ", dir), el("label", {}, "in ", kase), angles,
    el("p", { className: "hint", textContent: "Changes apply the next time you check strength." }));
}

const HOLD_TEXT = { clamped: "Held: clamped or glued", resting: "Held: resting (only pushes back)", screw: "Held: screw or bolt" };
const loadText = (l) => (l.kind === "fixed" ? HOLD_TEXT[l.hold ?? "clamped"] : `Load: ${multiCase() ? `case ${l.case}, ` : ""}${l.kg} kg ${dirLabel(l)}`) + (l.point ? " (at a spot)" : "");

// The 3D view looks like a passive preview: show a pointer and a hint over the part until the first click.
const hoverTip = document.body.appendChild(el("div", { className: "hover-tip", hidden: true,
  textContent: "Click a surface: put it on the bed, keep it clean, or hold / load it for the strength check" }));
let hoverQueued = false;
$("viewer").addEventListener("pointermove", (e) => {
  if (hoverQueued || drag) return;
  hoverQueued = true;
  requestAnimationFrame(() => {
    hoverQueued = false;
    const over = Boolean(state.triangles) && viewer.pick(e) >= 0;
    viewer.renderer.domElement.classList.toggle("hovering", over || viewer.pickSymbol(e) != null);
    hoverTip.hidden = !over || state.pickedOnce;
    if (!hoverTip.hidden) Object.assign(hoverTip.style, { left: `${e.clientX + 14}px`, top: `${e.clientY + 14}px` });
  });
});
$("viewer").addEventListener("pointerleave", () => (hoverTip.hidden = true));

// Drag a load arrow in the 3D view to change its direction.
let drag = null;
$("viewer").addEventListener("pointerdown", (e) => {
  const i = viewer.pickSymbol(e);
  if (i == null || state.loads[i]?.kind !== "force") return;
  drag = { i, x: e.clientX, y: e.clientY, dir: loadVector(state.loads[i]) };
  viewer.controls.enabled = false; // before OrbitControls sees this press (capture phase)
}, { capture: true });
addEventListener("pointermove", (e) => {
  if (!drag) return;
  state.loads[drag.i].direction = viewer.dragDirection(drag.dir, e.clientX - drag.x, e.clientY - drag.y);
  viewer.showLoadSymbols(loadSymbols());
});
addEventListener("pointerup", () => {
  if (!drag) return;
  drag = null;
  downAt = null; // not a click on the part
  viewer.controls.enabled = true;
  renderLoads();
});
const multiCase = () => new Set(state.loads.filter((l) => l.kind === "force").map((l) => l.case)).size > 1;
const readyForStrength = () => state.loads.some((l) => l.kind === "fixed") && state.loads.some((l) => l.kind === "force");

function renderLoads() {
  $("loads").replaceChildren(
    ...state.loads.map((l, i) =>
      el(
        "li",
        { className: l.kind, id: `load-${i}` },
        el("span", { textContent: loadText(l) }),
        l.kind === "force" ? el("button", { textContent: "✎", title: "Edit weight, direction or case", className: "icon", onclick: () => {
          state.angleOpen = state.angleOpen === i ? null : i;
          renderLoads();
        } }) : "",
        el("button", {
          textContent: "×",
          title: "Remove",
          onclick: () => {
            remember("removing a load");
            state.loads.splice(i, 1);
            state.angleOpen = null;
            renderLoads();
            recolor();
          },
        }),
        state.angleOpen === i && l.kind === "force" ? loadEditor(l) : "",
      ),
    ),
  );
  $("run-strength").disabled = !readyForStrength();
  $("run-optimise").disabled = !readyForStrength();
  $("run-dynamics").disabled = !readyForStrength() || !($("dyn-shake").checked || $("dyn-drop").checked);
  $("dynamics-need").hidden = readyForStrength();
  $("pick-hint").hidden = state.loads.length > 1;
}

/** How the part will be printed (walls, solid top/bottom layers, infill 0–1), from the settings shown. */
function currentShells() {
  const s = Object.fromEntries(effectiveSettings().map((x) => [x.key, x.value]));
  const num = (v, d) => (v == null || v === "" || Number.isNaN(parseFloat(v)) ? d : parseFloat(v));
  return { walls: num(s.wall_loops, 2), top: num(s.top_shell_layers, 4), bottom: num(s.bottom_shell_layers, 3), infill: num(s.sparse_infill_density, 15) / 100 };
}

const SHELL_KEYS = { walls: "wall_loops", top: "top_shell_layers", bottom: "bottom_shell_layers", infill: "sparse_infill_density" };

function renderShells() {
  const sh = currentShells();
  for (const k of Object.keys(SHELL_KEYS)) $(`sh-${k}`).value = k === "infill" ? Math.round(sh.infill * 100) : sh[k];
  $("shells-summary").textContent = `${sh.walls} walls, ${sh.top} top / ${sh.bottom} bottom layers, ${Math.round(sh.infill * 100)}% infill`;
}
for (const k of Object.keys(SHELL_KEYS)) {
  $(`sh-${k}`).onchange = (e) => {
    const v = Math.max(0, Number(e.target.value));
    state.overrides[SHELL_KEYS[k]] = k === "infill" ? `${Math.min(100, v)}%` : String(Math.round(v));
    renderSettings();
    if (state.strength) runStrength();
  };
}

const MODE_TEXT = {
  layers: "The layers would separate first. Orientation matters most here — try the strongest orientation.",
  material: "The plastic itself is the limit. More walls or infill, or a thicker design, help most.",
  lines: "Printed lines lying side by side would pull apart first. More walls there, or another orientation, help most.",
};

// ---------- strength: detailed check from the user's G-code ----------
const sameUp = (a, b) => a && b && Math.abs(a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) > 0.999 * Math.hypot(...a) * Math.hypot(...b);
/** The loaded G-code is used when ticked and it was sliced for the orientation shown. */
const useGcode = () => $("use-gcode").checked && !!state.gcode && sameUp(state.gcode.up, activeOption()?.up);

function renderGcode() {
  const g = state.gcode;
  const fits = g && sameUp(g.up, activeOption()?.up);
  $("use-gcode").disabled = !fits;
  if (!fits) $("use-gcode").checked = false;
  $("gcode-summary").textContent = !g ? "" : fits
    ? `— ${g.name}: ${g.info.layers} layers, ${g.info.part_grams} g, ${g.info.print_time_min} min`
    : "— the loaded G-code is for another orientation";
}

$("gcode-file").onchange = async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file || !activeOption()) return;
  const bytes = new Uint8Array(await file.arrayBuffer());
  const up = activeOption().up;
  await busy("Reading the G-code…", async () => {
    const info = await call("load_gcode", { up, profile: profile() }, bytes);
    state.gcode = { name: file.name, bytes, up, info };
    $("use-gcode").checked = true;
    renderGcode();
    setStatus(`G-code read: ${info.layers} layers, ${info.part_grams} g of part` + (info.turned_deg ? ` (the slicer turned it ${info.turned_deg}°)` : ""));
    await runCheck(); // the heat check uses the real layer times now
    if (readyForStrength()) await runStrength();
  }).catch(() => {});
};
$("use-gcode").onchange = () => state.strength && runStrength();

// ---------- how long a simulation takes, asked before the long ones ----------
// The strength grid has about the same number of cubes for every part, so one strength check takes
// about the same time on a given computer; the others are a steady multiple of it (measured on the
// samples in the browser: fine grid 2.6–5.4×, warping 1.8–2.9×, shaking 2.5–5.2×, optimiser 3.6–5.1×).
const RUN_RATIO = { strength: 1, fine: 3.5, warping: 2.4, dynamics: 3.5, optimise: 4.5, optimise_quick: 3 };
const ASK_ABOVE_S = 30;
let strengthSeconds = Number(localStorage.getItem("fdm-strength-s")) || 8; // this computer, once measured

/** Seconds ``kind`` will take: as measured on this part, else from this computer's strength time. */
const estimate = (kind) => state.runTimes?.[kind] ?? RUN_RATIO[kind] * strengthSeconds;

/** Run ``fn`` and remember how long ``kind`` took (on this part; strength also sets the computer's pace). */
async function timed(kind, fn) {
  const t0 = performance.now();
  const r = await fn();
  const s = (performance.now() - t0) / 1000;
  state.runTimes = { ...state.runTimes, [kind]: s };
  if (kind === "strength") localStorage.setItem("fdm-strength-s", String((strengthSeconds = s)));
  showEstimates();
  return r;
}

/** "about 20 s" after a button's text, kept up to date by showEstimates. */
function etaTag(kind) {
  const tag = el("small", { className: "eta", textContent: `about ${fmtSeconds(estimate(kind))}` });
  tag.dataset.eta = kind;
  return tag;
}

function showEstimates() {
  for (const e of document.querySelectorAll(".eta")) e.textContent = `about ${fmtSeconds(estimate(e.dataset.eta))}`;
}

/** Ask before a long run. ``choices``: [{value, label, kind}] (e.g. faster or more thorough); resolves
 * to the chosen value (or true), or null for "Not now". Short runs just go. */
function askLong(what, kind, choices = null) {
  const longest = Math.max(estimate(kind), ...(choices ?? []).map((c) => estimate(c.kind)));
  if (longest < ASK_ABOVE_S) return Promise.resolve(choices ? choices[0].value : true);
  const d = $("long-run");
  $("long-title").textContent = `${what}: about ${fmtSeconds(estimate(kind))}`;
  $("long-text").textContent = state.runTimes?.[kind] ? "As long as it took last time on this part." : "Estimated from how fast your computer ran the strength check.";
  $("long-choices").replaceChildren(...(choices ?? []).map((c, i) =>
    el("label", {}, el("input", { type: "radio", name: "long-choice", value: c.value, checked: i === 0 }), `${c.label} — about ${fmtSeconds(estimate(c.kind))}`)));
  d.showModal();
  return new Promise((resolve) => {
    const done = (v) => (d.close(), resolve(v));
    $("long-yes").onclick = () => done(choices ? document.querySelector("input[name=long-choice]:checked").value : true);
    $("long-no").onclick = () => done(null);
    d.oncancel = () => resolve(null);
  });
}

async function runStrength(fine = false) {
  const before = state.strength;
  if (fine && !(await askLong("Finer grid", "fine"))) return;
  return busy(fine ? `Simulating again with a finer grid (about ${fmtSeconds(estimate("fine"))})…` : "Simulating the load…", async () => {
    const r = await timed(fine ? "fine" : "strength", () => call("strength", { up: activeOption().up, loads: state.loads, profile: profile(), ...currentShells(), fine, detailed: useGcode(), zones: printZones() }));
    if (fine && before) {
      const change = r.safety_factor / before.safety_factor - 1;
      r.accuracy = `Finer grid: ×${r.safety_factor.toFixed(2)} instead of ×${before.safety_factor.toFixed(2)} (${change >= 0 ? "+" : ""}${Math.round(change * 100)} %). ` +
        (Math.abs(change) <= 0.1 ? "Barely changes: the result is stable." : Math.abs(change) <= 0.25
          ? "A normal difference for a rough estimate; the finer one is closer to the truth."
          : "Changes a lot: the weak spot is small or sharp. Trust the finer result and keep a bigger safety margin.");
    }
    state.strength = r;
    state.showHeat = true;
    state.showWarp = false;
    state.showSwing = false;
    $("show-temp").checked = false;
    recolor();
    renderStrength();
    setStatus(state.summary);
    return r;
  }, ["strength", "result"]);
}
$("run-strength").onclick = () => runStrength();

// ---------- strength: shaking and knocks (optional, Advanced) ----------
for (const [box, row] of [["dyn-shake", "dyn-shake-row"], ["dyn-drop", "dyn-drop-row"]]) {
  $(box).onchange = () => {
    $(row).hidden = !$(box).checked;
    $("run-dynamics").disabled = !readyForStrength() || !($("dyn-shake").checked || $("dyn-drop").checked);
  };
}
$("run-dynamics").onclick = async () => {
  if (!(await askLong("Shaking and knocks", "dynamics"))) return;
  return busy("Working out how it swings and how hard a drop hits…", async () => {
    const r = await timed("dynamics", () => call("dynamics", {
      up: activeOption().up, loads: state.loads, profile: profile(), ...currentShells(), zones: printZones(),
      shake: $("dyn-shake").checked,
      mounted_kg: $("dyn-shake").checked ? Number($("dyn-kg").value) || 0 : 0,
      drop_mm: $("dyn-drop").checked ? Number($("dyn-mm").value) || 0 : 0,
    }));
    state.swing = r.vertex_disp ? r : null; // only the shaking check has a swing to show
    state.showSwing = Boolean(state.swing);
    const show = el("input", { type: "checkbox", checked: true });
    show.onchange = () => {
      state.showSwing = show.checked;
      if (show.checked) state.showWarp = false;
      recolor();
    };
    $("dynamics-result").replaceChildren(
      el("span", { textContent: r.explanation.replace(" Shown: how it swings (exaggerated).", "") }),
      state.swing ? el("label", { className: "toggle" }, show, " Show how it swings on the part") : "",
    );
    recolor();
    setStatus(state.summary);
  }, ["sims"]);
};

// ---------- heat inside an electronics box ----------
const HEAT_CLASS = { fine: "fine", warm: "borderline", "too hot": "likely-to-break" };
$("run-box-heat").onclick = () =>
  busy("Working out the heat inside…", async () => {
    const r = await call("box_heat", { power_w: Number($("heat-w").value) || 0, ambient_c: Number($("heat-room").value) || 25,
      cooling: $("heat-cooling").value, profile: profile() });
    const t = Math.round(r.inside_c);
    // The walls sit at about the inside temperature: let the other checks use it as "where it's used".
    const use = el("button", { textContent: `Check the part at ${t} °C`, onclick: async () => {
      let opt = [...$("use-temp").options].find((o) => o.value === String(t));
      if (!opt) $("use-temp").append((opt = el("option", { value: String(t), textContent: `Inside the electronics box (${t} °C)` })));
      $("use-temp").value = opt.value;
      await $("use-temp").onchange();
      setStatus(`The checks now use ${t} °C, the air inside the box.`);
    } });
    $("box-heat-result").replaceChildren(
      el("div", { className: `verdict ${HEAT_CLASS[r.verdict]}` }, el("p", { textContent: r.explanation }), r.inside_c > 30 ? use : ""));
    setStatus(state.summary);
  }, ["sims"]);

// ---------- strength: FEA result view ----------
const f32 = (b) => new Float32Array(b.slice().buffer);
const dispLength = (r) => {
  const d = f32(r.vertex_disp);
  const out = new Float32Array(d.length / 3);
  for (let i = 0; i < out.length; i++) out[i] = Math.hypot(d[3 * i], d[3 * i + 1], d[3 * i + 2]);
  return out;
};
const FIELD = {
  fi: { title: "Strength used (%)", note: "100 % = breaks", values: (r) => f32(r.vertex_fi), elements: "fi", max: (r) => r.fi_max_pct, fmt: (v) => `${Math.round(v)} %` },
  vm: { title: "Stress, von Mises (MPa)", note: "", values: (r) => f32(r.vertex_vm), elements: "vm", max: (r) => r.vm_max_mpa, fmt: (v) => v.toFixed(v < 10 ? 1 : 0) },
  disp: { title: "Moves by (mm)", note: "how far each spot bends away", values: dispLength, elements: null, max: (r) => r.max_displacement_mm, fmt: (v) => v.toFixed(v < 0.1 ? 3 : 2) },
};
const view = { field: "fi", bend: 0, axis: "", cut: 0.5 };

/** Show the FEA colours when they're switched on; returns false (and restores the part) otherwise. */
function strengthView() {
  if (shows("sims") && state.showSwing && state.swing) return swingView();
  stopSwing();
  if (shows("sims") && state.showWarp && state.warp) return warpView();
  const r = state.strength;
  if (!(shows("strength") && state.showHeat && r)) {
    viewer.hideField();
    $("legend").hidden = true;
    return false;
  }
  const f = FIELD[view.field];
  const max = Math.max(f.max(r), 1e-9);
  const key = `${view.field}|${r.critical_point}|${activeOption().up}`;
  if (viewer.fieldKey !== key || !viewer.field) {
    viewer.showField({ triangles: f32(r.field_triangles), values: f.values(r), disp: f32(r.vertex_disp), max });
    viewer.fieldKey = key;
  }
  const radius = viewer.part.geometry.boundingSphere.radius;
  viewer.setDeformation(view.bend * (0.08 * radius) / Math.max(r.max_displacement_mm, 1e-9));
  const els = r.elements;
  const cubes = f.elements ? f32(els[f.elements]) : null; // displacement: only known on the surface
  viewer.setSection(view.axis || null, view.axis ? view.cut : null, { centers: f32(els.centers), values: cubes, size_mm: els.size_mm }, max);
  renderLegend(f, max);
  return true;
}

function renderLegend(f, max) {
  const bands = [];
  for (let i = FIELD_BANDS - 1; i >= 0; i--) {
    bands.push(el("div", { className: "band", style: `background:${fieldColor((i + 0.5) / FIELD_BANDS).getStyle()}` },
      el("span", { textContent: f.fmt((max * (i + 1)) / FIELD_BANDS) })));
  }
  $("legend").replaceChildren(el("strong", { textContent: f.title }), f.note ? el("small", { textContent: f.note }) : "", ...bands,
    el("div", { className: "band-0", textContent: f.fmt(0) }));
  $("legend").hidden = false;
}

// ---------- shaking: the part rocks in its lowest mode ----------
const SWING_FIELD = { title: "Swings most here", note: "how it moves when it shakes (exaggerated)", fmt: (v) => `${Math.round(v * 100)} %` };
let swingFrame = 0;

/** The swing shape as colours, and the part rocking back and forth in it: you see where it moves and how. */
function swingView() {
  const s = state.swing;
  const values = f32(s.vertex_value);
  let max = 1e-9; // not Math.max(...values): hundreds of thousands of arguments overflow the stack
  for (const v of values) if (v > max) max = v;
  const key = `swing|${s.frequency_hz}|${activeOption().up}`;
  if (viewer.fieldKey !== key || !viewer.field) {
    viewer.showField({ triangles: f32(s.field_triangles), values: values.map((v) => v / max), disp: f32(s.vertex_disp), max: 1 });
    viewer.fieldKey = key;
  }
  renderLegend(SWING_FIELD, 1);
  const amplitude = (0.08 * viewer.part.geometry.boundingSphere.radius) / max;
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!swingFrame) {
    const t0 = performance.now();
    let last = 0;
    const step = (t) => {
      // About one swing a second: slow enough to follow, whatever the real frequency is.
      // ~20 updates a second keep big parts light on the CPU.
      if (t - last > 50) {
        last = t;
        viewer.setDeformation(still ? amplitude : amplitude * Math.sin(((t - t0) / 1000) * 2 * Math.PI), false);
      }
      swingFrame = still ? 0 : requestAnimationFrame(step);
    };
    swingFrame = requestAnimationFrame(step);
  }
  return true;
}

function stopSwing() {
  if (!swingFrame) return;
  cancelAnimationFrame(swingFrame);
  swingFrame = 0;
  viewer.setDeformation(0);
}

// ---------- warping simulation ----------
const WARP_FIELD = { title: "Curls by (mm)", note: "exaggerated shape, after it comes off the bed", fmt: (v) => v.toFixed(v < 0.1 ? 3 : 2) };

function warpView() {
  const w = state.warp;
  const key = `warp|${w.curl_mm}|${activeOption().up}`;
  if (viewer.fieldKey !== key || !viewer.field) {
    viewer.showField({ triangles: f32(w.field_triangles), values: f32(w.vertex_value), disp: f32(w.vertex_disp), max: Math.max(w.max_mm, 1e-9) });
    viewer.fieldKey = key;
  }
  viewer.setDeformation((0.06 * viewer.part.geometry.boundingSphere.radius) / Math.max(w.max_mm, 1e-9));
  renderLegend(WARP_FIELD, Math.max(w.max_mm, 1e-9));
  return true;
}

$("run-warp").onclick = async () => {
  if (!(await askLong("Warping simulation", "warping"))) return;
  return busy(`Simulating the print layer by layer (about ${fmtSeconds(estimate("warping"))})…`, async () => {
    const w = await timed("warping", () => call("warping", { up: activeOption().up, profile: profile() }));
    state.warp = w;
    state.showWarp = true;
    state.showHeat = false;
    $("show-temp").checked = false;
    const show = el("input", { type: "checkbox", checked: true });
    show.onchange = () => {
      state.showWarp = show.checked;
      recolor();
    };
    $("warp-result").replaceChildren(el("div", { className: `verdict ${w.verdict === "fine" ? "fine" : w.verdict === "might lift" ? "borderline" : "likely-to-break"}` },
      el("strong", { textContent: w.verdict === "fine" ? "Stays flat" : w.verdict === "might lift" ? "Corners might lift" : "Corners will likely lift" }),
      el("p", { textContent: w.explanation }),
      el("label", { className: "toggle" }, show, "Show the curled shape (red dots = lifts first)"),
      el("p", { className: "hint", textContent: "Rough: rated against the longest test bar that stays flat in this material and printer (your warping calibration print if you made one)." })));
    recolor();
    setStatus(state.summary);
  });
};

function strengthViewControls(r) {
  const heat = el("input", { type: "checkbox", checked: state.showHeat });
  heat.onchange = () => {
    state.showHeat = heat.checked;
    if (heat.checked) $("show-temp").checked = false;
    recolor();
  };
  const show = () => {
    state.showHeat = true;
    heat.checked = true;
    $("show-temp").checked = false;
    recolor();
  };
  const field = el("select", {}, el("option", { value: "fi", textContent: "Strength used (% — where it breaks)" }),
    el("option", { value: "vm", textContent: "Stress (MPa, von Mises)" }), el("option", { value: "disp", textContent: "How far it moves (mm)" }));
  field.value = view.field;
  field.onchange = () => ((view.field = field.value), show());
  const bend = el("input", { type: "range", min: 0, max: 1, step: 0.05, value: view.bend });
  bend.oninput = () => ((view.bend = Number(bend.value)), show());
  const axis = el("select", {}, ...[["", "No cut"], ["x", "Cut across X"], ["y", "Cut across Y"], ["z", "Cut at a height"]]
    .map(([v, t]) => el("option", { value: v, textContent: t })));
  axis.value = view.axis;
  const cut = el("input", { type: "range", min: 0, max: 1, step: 0.01, value: view.cut, disabled: !view.axis });
  axis.onchange = () => ((view.axis = axis.value), (cut.disabled = !view.axis), show());
  cut.oninput = () => ((view.cut = Number(cut.value)), show());
  const m = r.mesh;
  return el("div", { className: "fea-view" },
    el("label", { className: "toggle" }, heat, "Show results on the part (red arrow = weakest spot)"),
    from("advanced", el("label", {}, "Colour by ", field)),
    from("advanced", el("label", {}, "Show bending (exaggerated) ", bend)),
    from("advanced", el("div", { className: "row" }, axis, cut)),
    from("advanced", el("p", { className: "hint", textContent: "Cut through the part to see inside — the weakest spot is often under the surface." })),
    from("advanced", el("p", { className: "hint", textContent: `Analysis: ${m.voxels.toLocaleString()} cubes of ${m.voxel_mm.toFixed(1)} mm · total load ${Math.round(m.load_n)} N · bends up to ${r.max_displacement_mm.toFixed(2)} mm${r.solver_converged ? "" : " · solver did not fully converge"}` })),
    r.accuracy ? el("p", { className: "hint", textContent: r.accuracy }) : "",
    r.fine ? "" : from("advanced", el("button", { onclick: () => runStrength(true),
      title: "Runs again with 3× more cubes. If the answer barely changes, you can trust it (the usual check in FEA software)." },
      "Check accuracy with a finer grid ", etaTag("fine"))));
}

function renderStrength() {
  const r = state.strength;
  noteVersion({ safety: r.safety_factor });
  const sf = r.safety_factor;
  $("strength-result").replaceChildren(
    el(
      "div",
      { className: `verdict ${r.verdict.replaceAll(" ", "-")}` },
      el("strong", { textContent: `${r.verdict[0].toUpperCase() + r.verdict.slice(1)} — safety ×${sf.toFixed(1)}` }),
      el("p", { textContent: r.explanation ?? MODE_TEXT[r.mode] }),
      el("p", { className: "hint", textContent: (r.detailed
        ? `${r.material}, as sliced in ${state.gcode.name} (${r.detailed.missed_pct}% of the plastic falls outside the analysis cubes)`
        : `${r.material}, ${r.walls} walls, ${r.top} top / ${r.bottom} bottom layers, ${Math.round(r.infill * 100)}% infill`) + ` · bends about ${r.max_displacement_mm.toFixed(1)} mm` }),
    ),
    r.cases.length > 1
      ? el("p", { className: "hint", textContent: `Weakest is case ${r.case} (shown). ` + r.cases.map((c) => `Case ${c.case}: ×${c.safety_factor.toFixed(1)}`).join(" · ") })
      : "",
    strengthViewControls(r),
    el("p", { className: "hint", textContent: "Rough estimate. Aim for ×2 or more for anything that matters." }),
    el("button", { className: "primary", textContent: "How can I make it stronger?", onclick: showStrengthOptions,
      title: "Shows every option: slicer settings, orientation, design changes and materials, each with what it costs" }),
    el("button", { textContent: "Find the best orientation including strength", onclick: rankWithStrength }),
    el("div", { id: "strength-options", className: "option-groups" }),
  );
}

/** Re-rank with the loads, then show the strength of the new best orientation (not the old result). */
async function rankWithStrength() {
  const before = activeOption()?.label;
  await findOrientations(true);
  await runStrength();
  setStatus(`${activeOption().label === before ? "Still the best" : "New best orientation"} with your load: safety ×${state.strength.safety_factor.toFixed(1)}.`);
}

async function showStrengthOptions() {
  return busy("Trying alternatives (a few seconds)…", async () => {
    const opts = await call("strength_options", { up: activeOption().up, loads: state.loads, profile: profile(), ...currentShells() });
    const noMaterial = opts.some((o) => o.kind === "material") ? ""
      : el("p", { className: "hint", textContent: `No other material is much stronger than ${state.strength?.material ?? "this one"}; others help with heat or impact instead.` });
    $("strength-options").replaceChildren(...optionGroups(opts, state.strength?.safety_factor), noMaterial);
    setStatus(state.summary);
    return opts;
  });
}

const OPTION_GROUP = { slicer: "Slicer settings", orientation: "Orientation", design: "Design changes", material: "Material", load: "Less load" };
const OPTION_ACTION = { slicer: "Apply", orientation: "Use it", design: "Show me", material: "Use it" };

/** Ways to make it stronger in one fold-out box per kind; the box with the strongest option starts open. */
function optionGroups(opts, now) {
  const kinds = [...new Set(opts.map((o) => o.kind))];
  return kinds.map((kind, i) => {
    const mine = opts.filter((o) => o.kind === kind);
    const top = mine.find((o) => o.safety_factor != null)?.safety_factor;
    return el("details", { className: "more-block option-group", open: i === 0 },
      el("summary", {}, OPTION_GROUP[kind], el("small", { textContent: top != null ? `up to ×${top.toFixed(1)}` : `${mine.length} idea${mine.length > 1 ? "s" : ""}` })),
      el("ul", { className: "options strength-options" }, ...mine.map((o) => optionCard(o, now))));
  });
}

function optionCard(o, now) {
  return el("li", { className: `option ${o.kind}` },
    el("div", { className: "option-head" },
      el("strong", { textContent: o.change }),
      el("span", { className: "gain", textContent: o.safety_factor == null ? "re-check after"
        : `×${o.safety_factor.toFixed(1)}${now && o.kind !== "load" ? ` (${o.safety_factor >= now ? "+" : ""}${Math.round((o.safety_factor / now - 1) * 100)} %)` : ""}` })),
    el("p", { className: "hint", textContent: o.why + (o.cost ? ` Cost: ${o.cost}.` : "") }),
    OPTION_ACTION[o.kind] || o.kind === "design" ? el("div", { className: "row" },
      OPTION_ACTION[o.kind] ? el("button", { textContent: OPTION_ACTION[o.kind], onclick: () => applyOption(o) }) : "",
      o.kind === "design" && /chamfer/i.test(o.change) ? el("button", { textContent: "Test it", onclick: testStressChamfer,
        title: "Adds the chamfer as a new version of your model and checks the strength again" }) : "") : "");
}

async function runOptimise() {
  const how = await askLong("Lightest settings that hold", "optimise", [
    { value: "zones", kind: "optimise", label: "Also try stronger zones only where needed (saves the most plastic)" },
    { value: "quick", kind: "optimise_quick", label: "Settings for the whole part only (faster)" }]);
  if (!how) return;
  const kind = how === "quick" ? "optimise_quick" : "optimise";
  return busy(`Trying settings to find the lightest print that holds (about ${fmtSeconds(estimate(kind))})…`, async () => {
    const o = await timed(kind, () => call("optimise_strength", { up: activeOption().up, loads: state.loads, profile: profile(), zones: how !== "quick" }));
    const use = el("button", { className: "primary", textContent: "Use these settings", onclick: async () => {
      remember("the lightest settings");
      setZones([]);
      Object.assign(state.overrides, o.settings);
      renderSettings();
      if (o.orientation === "strongest") await applyOption({ up: o.up });
      else await runStrength();
    } });
    const z = o.zoned;
    const zoned = z?.met && z.zones?.length
      ? el("div", { className: "verdict fine" },
        el("strong", { textContent: "Or: stronger only where it's needed" }),
        el("p", { textContent: z.explanation }),
        el("p", { className: "hint", textContent: "The zones go into the 3MF as modifier volumes: the slicer prints them with more walls and infill (orange boxes)." }),
        el("button", { textContent: "Use the zones", onclick: async () => {
          remember("the stronger zones");
          Object.assign(state.overrides, z.settings);
          renderSettings();
          if (o.orientation === "strongest") await applyOption({ up: z.up }, false); // clears zones: set them after
          setZones(z.zones);
          runStrength();
        } }))
      : "";
    $("optimise-result").replaceChildren(
      el("div", { className: `verdict ${o.met ? "fine" : "likely-to-break"}` },
        el("strong", { textContent: o.met ? `Lightest print that holds ×${o.target}` : "Settings alone aren't enough" }),
        el("p", { textContent: o.explanation }),
        o.steps.length ? el("p", { className: "hint", textContent: `Added step by step, cheapest first: ${o.steps.map((s) => s.change).join(" → ")}` }) : "",
        use),
      o.beyond?.length
        ? el("div", { className: "option-groups beyond" },
          el("strong", { textContent: `What else gets it to ×${o.target}` }),
          el("p", { className: "hint", textContent: "Worked out with the strongest settings above." }),
          ...optionGroups(o.beyond, o.safety_factor))
        : "",
      zoned,
    );
    setStatus(state.summary);
    return o;
  });
}
$("run-optimise").onclick = runOptimise;

// ---------- material advisor ----------
const RATING_CLASS = { good: "fine", "with care": "borderline", "not suitable": "likely-to-break" };
$("run-advice").onclick = () =>
  busy("Comparing materials…", async () => {
    const withLoad = Boolean(activeOption()) && readyForStrength();
    const list = await call("material_advice", { profile: profile(), outdoors: $("adv-outdoors").checked, flexible: $("adv-flexible").checked,
      ...(withLoad ? { up: activeOption().up, loads: state.loads } : {}) });
    $("advice-list").replaceChildren(...list.map((r) =>
      el("li", { className: `verdict ${RATING_CLASS[r.rating]}` },
        el("strong", { textContent: `${r.name} — ${r.rating}${r.current ? " (selected)" : ""}` }),
        el("p", { className: "hint", textContent: r.reasons.join(" · ") || "Nothing against it." }),
        r.current || r.rating === "not suitable" ? "" : el("button", { textContent: `Use ${r.name}`, onclick: () => {
          $("material").value = r.material;
          $("material").dispatchEvent(new Event("change"));
        } }))));
    setStatus(withLoad ? "Materials compared, including your load." : "Materials compared (set a load in Strength to compare strength too).");
  });

// Where the part is used changes the checks (too hot?) and the strength (weaker when warm, more margin for loads that stay on).
for (const id of ["use-temp", "constant-load"]) {
  $(id).onchange = async () => {
    if (!activeOption()) return;
    await runCheck();
    if (state.strength) await runStrength();
  };
}

/** Strengthening zones (slicer modifier volumes) from the optimiser; shown as boxes and put in the 3MF. */
function setZones(zones) {
  state.zones = zones;
  drawBoxes();
  $("zones-for").hidden = !printZones().some((z) => z.kind !== "keep_clear");
  renderZones();
}

const zonesShown = () => shows("strength") || shows("settings") || shows("download");
const volumesShown = () => zonesShown() || shows("extra");
const VOLUME_COLOR = { support: "--support", blocker: "--ok", void: "--fixed", keep_clear: "--clean" };

/** Zones (orange) and the advanced design boxes (their own colours) in the 3D view. */
function drawBoxes() {
  viewer.showZones([
    ...(zonesShown() ? state.zones.map((z) => ({ corners: z.corners_print, shape: z.shape })) : []),
    ...(volumesShown() ? state.volumes.map((v) => ({ corners: v.corners_print, color: VOLUME_COLOR[v.kind], shape: v.shape })) : []),
  ]);
}

/** What the slicer and the strength check get: zones plus the advanced design boxes. */
const printZones = () => [...state.zones, ...state.volumes.map(({ kind, lo, hi, shape }) => ({ kind, lo, hi, shape, walls: 0, infill: 0 }))];

/** Box / cylinder / ball: round shapes fit round things (a boss, the wall round a hole). */
function shapePicker(value, onchange) {
  const pick = el("select", { className: "shape-pick", title: "Shape (round ones sit inside the box)" },
    ...[["box", "Box"], ["cylinder", "Cylinder"], ["ball", "Ball"]].map(([v, t]) => el("option", { value: v, textContent: t })));
  pick.value = value ?? "box";
  pick.onchange = () => onchange(pick.value);
  return el("label", {}, "Shape ", pick);
}

// ---------- stronger in one area (zones the user picks) ----------
const BIG_FACE_MM2 = 400; // a face this big gets a box around the clicked spot, not the whole face
const SPOT_ZONE_MM = 8;

$("zone-pick").onclick = async () => {
  if (!activeOption()) return;
  await ensureRegions();
  state.zonePick = true;
  $("zone-hint").hidden = false;
  setStatus("Click the face or spot that needs to be stronger.");
};

/** A zone around the clicked face (small faces: holes, bosses) or around the clicked spot (big faces). */
async function addZone(hit) {
  state.zonePick = false;
  $("zone-hint").hidden = true;
  const region = state.faceRegions[hit.face];
  const info = state.regionInfo[region];
  const from = info.area_mm2 > BIG_FACE_MM2 ? { point: hit.point } : { region };
  const margin = from.point ? SPOT_ZONE_MM : 3;
  const z = await busy("Making the area…", () => call("zone_at", { up: activeOption().up, profile: profile(), ...from, margin }));
  remember("a stronger area");
  setZones([...state.zones, { ...z, from, margin, what: info.hole_diameter ? `Ø${info.hole_diameter.toFixed(1)} mm hole` : from.point ? "the clicked spot" : `${info.kind} face` }]);
  setStatus("Stronger area added (orange box). Set its walls and infill below.");
}

/** Resize a picked zone: its box is worked out again from the face or spot it was made for. */
async function resizeZone(i, margin, shape = state.zones[i].shape) {
  const old = state.zones[i];
  const z = await busy("Resizing the area…", () => call("zone_at", { up: activeOption().up, profile: profile(), ...old.from, margin,
    walls: old.walls, infill: old.infill, shape }));
  remember("the area's size");
  setZones(state.zones.map((q, k) => (k === i ? { ...old, ...z, margin } : q)));
}

function renderZones() {
  $("zone-list").replaceChildren(...state.zones.map((z, i) => {
    const set = (key, value) => {
      remember("the area's settings");
      setZones(state.zones.map((q, k) => (k === i ? { ...q, [key]: value } : q)));
    };
    const num = (value, min, max, step, onchange) => Object.assign(el("input", { type: "number", min, max, step, value }), { onchange });
    const base = currentShells(); // an area is never weaker than the rest (the strength check counts it that way)
    return el("li", {},
      el("strong", { textContent: z.from ? `Around ${z.what}` : "From the lightest-settings search" }),
      el("div", { className: "row" },
        el("label", {}, "Walls ", num(z.walls, base.walls, 20, 1, (e) => set("walls", Math.max(base.walls, Math.round(Number(e.target.value) || 0))))),
        el("label", {}, "Infill ", num(Math.round(z.infill * 100), Math.round(base.infill * 100), 100, 5,
          (e) => set("infill", Math.min(1, Math.max(base.infill, Number(e.target.value) / 100)))), " %"),
        z.from ? el("label", {}, "Reaches ", num(z.margin, 0, 30, 1, (e) => resizeZone(i, Math.max(0, Number(e.target.value) || 0))), " mm past it") : "",
        shapePicker(z.shape, (shape) => (z.from ? resizeZone(i, z.margin, shape) : set("shape", shape)))),
      el("button", { className: "link", textContent: "Remove", onclick: () => {
        remember("removing an area");
        setZones(state.zones.filter((_, k) => k !== i));
      } }));
  }));
}

// ---------- advanced design changes (boxes placed on the part) ----------
const VOLUME_HINT = {
  support: "The slicer prints supports inside the box, even with its automatic supports off. For an overhang it misses or a spot you want held.",
  blocker: "No supports inside the box: keeps marks off a surface that should stay clean. Overhangs in it print in the air.",
  void: "A thin hidden gap inside the part. The slicer prints walls all round it, so the plastic around it comes out solid: a stronger spot without more infill everywhere. Click a spot for a pin hole up through the part, or two spots for a slot.",
  keep_clear: "Space that has to stay empty: a screw head, a cable, a part that moves. Only for you (not in the 3MF): it tells you if the part reaches into it.",
};
const VOLUME_NAME = { support: "Support here", blocker: "No support here", void: "Hidden gap", keep_clear: "Keep clear" };

$("vol-kind").onchange = () => ($("vol-kind-hint").textContent = VOLUME_HINT[$("vol-kind").value]);
$("vol-kind").onchange();
for (const [id, clicks] of [["vol-pick", 1], ["vol-pick2", 2]]) {
  $(id).onclick = async () => {
    if (!activeOption()) return;
    await ensureRegions();
    state.volPick = { kind: $("vol-kind").value, clicks, points: [] };
    $("vol-hint").hidden = false;
    $("vol-hint").textContent = clicks === 2 ? "Click the first spot…" : "Click a face or spot on the part…";
  };
}

/** One click: a box around the face (small faces) or the spot; two clicks: a box between the spots. */
async function pickVolume(hit) {
  const pick = state.volPick;
  pick.points.push(hit.point);
  if (pick.points.length < pick.clicks) {
    $("vol-hint").textContent = "Now click the second spot…";
    return;
  }
  state.volPick = null;
  $("vol-hint").hidden = true;
  const region = state.faceRegions[hit.face];
  const info = state.regionInfo[region];
  let from, what;
  if (pick.clicks === 2) [from, what] = [{ point: pick.points[0], point2: pick.points[1] }, "between two spots"];
  else if (pick.kind === "void") [from, what] = [{ point: hit.point }, "pin hole at the spot"];
  else if (info.area_mm2 > BIG_FACE_MM2) [from, what] = [{ point: hit.point }, "the clicked spot"];
  else [from, what] = [{ region }, info.hole_diameter ? `Ø${info.hole_diameter.toFixed(1)} mm hole` : `${info.kind} face`];
  try {
    const v = await busy("Placing the box…", () => call("volume_at", { up: activeOption().up, kind: pick.kind, profile: profile(), ...from }));
    remember("an advanced design change");
    setVolumes([...state.volumes, { ...v, from, what, margin: v.kind === "void" && !from.region && !from.point2 ? 0.3 : 3 }]);
    setStatus(`${VOLUME_NAME[v.kind]} added. ${v.notes[0] ?? ""}`);
  } catch (err) {
    setStatus(String(err.message ?? err).replace(/^.*Error: /, ""));
  }
}

async function resizeVolume(i, margin, shape = state.volumes[i].shape ?? "box") {
  const old = state.volumes[i];
  const v = await busy("Resizing the box…", () => call("volume_at", { up: activeOption().up, kind: old.kind, profile: profile(), ...old.from, margin, shape }));
  remember("the box's size");
  setVolumes(state.volumes.map((q, k) => (k === i ? { ...old, ...v, margin } : q)));
}

/** Advanced design boxes: shown in the 3D view, listed with their notes, sent with the zones. */
function setVolumes(volumes) {
  state.volumes = volumes;
  drawBoxes();
  $("zones-for").hidden = !printZones().some((z) => z.kind !== "keep_clear");
  $("vol-list").replaceChildren(...volumes.map((v, i) => {
    const pin = v.kind === "void" && !v.from.region && !v.from.point2;
    const num = (value, min, max, step, onchange) => Object.assign(el("input", { type: "number", min, max, step, value }), { onchange });
    const size = pin
      ? el("label", {}, "Width ", num(+(v.margin * 2).toFixed(1), 0.2, 4, 0.1, (e) => resizeVolume(i, Math.max(0.1, Number(e.target.value) / 2 || 0.3))), " mm")
      : el("label", {}, "Reaches ", num(v.margin, 0, 30, 1, (e) => resizeVolume(i, Math.max(0, Number(e.target.value) || 0))), " mm past it");
    const li = el("li", {},
      el("strong", { textContent: `${VOLUME_NAME[v.kind]}: ${v.what}` }),
      el("div", { className: "row" }, size, pin ? "" : shapePicker(v.shape, (shape) => resizeVolume(i, v.margin, shape))),
      ...v.notes.map((n) => el("p", { className: "hint", textContent: n })),
      el("button", { className: "link", textContent: "Remove", onclick: () => {
        remember("removing a box");
        setVolumes(state.volumes.filter((_, k) => k !== i));
      } }));
    li.dataset.kind = v.kind;
    return li;
  }));
}

/** Apply a suggestion; a new orientation is checked for strength again (``recheck: false``: the caller does). */
async function applyOption(o, recheck = true) {
  if (o.settings) {
    remember("slicer settings from the strength check");
    Object.assign(state.overrides, o.settings);
    renderSettings();
    $("run-strength").click();
  } else if (o.up) {
    const option = await call("orientation", { up: o.up, profile: profile(), label: "Strongest" });
    option.reasons.push("Strongest for your load");
    state.options.unshift(option);
    renderOptions();
    await selectOption(0);
    if (recheck && readyForStrength()) await runStrength();
  } else if (o.material) {
    $("material").value = o.material;
    $("material").dispatchEvent(new Event("change"));
  } else if (o.kind === "design") {
    // Open Design changes (an Advanced step) with the change lit up on the part.
    if (!steps.steps.includes("design")) setLevel("advanced");
    if (mode() === "print") $("design-on").click();
    steps.go("design");
    const r = await suggestDesign();
    const edit = state.edits.find((e) => e.rule === "stress_at_inside_corner");
    if (edit) {
      showSelectedGhosts(edit);
      viewer.focusEdit(edit);
      setStatus("The chamfer at the weak spot is lit up in yellow. Apply it, then check the strength again.");
    } else if (r) setStatus("No chamfer fits at the weak spot: make the part thicker there in your CAD.");
  }
}

// ---------- design changes ----------
function clearEdits() {
  state.edits = [];
  state.designFor = null;
  $("edits").replaceChildren();
  $("apply-edits").hidden = true;
  viewer.showGhosts([]);
}

function showSelectedGhosts(highlight = null) {
  viewer.showGhosts(shows("design") ? state.edits.filter((e) => e.selected) : [], highlight);
  $("apply-edits").hidden = !state.edits.some((e) => e.selected);
}

/** Design changes are suggested as soon as their step is shown (Expert: always on the page), once per orientation. */
function suggestDesignIfShown() {
  if (!activeOption() || !state.check) return;
  const wanted = shows("fits") || ((shows("design") || steps.level === "expert") && !$("design-block").hidden);
  if (!wanted) return;
  if (state.designFor !== activeOption().up.join()) suggestDesign().catch(() => {});
}

async function suggestDesign() {
  state.designFor = activeOption().up.join();
  return busy("Looking for design improvements…", async () => {
    const r = await call("design", { up: activeOption().up, profile: profile(), params: designParams() });
    for (const [k, range] of Object.entries(r.param_ranges)) Object.assign($(`param-${k}`), { ...range, value: r.params[k] });
    $("edit-params").hidden = !r.edits.length;
    state.features = r.features;
    renderFeatures(r.features);
    state.edits = r.edits.map((e) => ({ ...e, mesh: new Float32Array(e.mesh.slice().buffer), selected: true }));
    $("edits").replaceChildren(
      ...(state.edits.length
        ? state.edits.map((e) => {
            const box = el("input", { type: "checkbox", checked: true });
            box.onchange = () => {
              e.selected = box.checked;
              showSelectedGhosts();
            };
            // Pointing at a change turns the part to it and lights it up.
            return el("li", { onmouseenter: () => (showSelectedGhosts(e), viewer.focusEdit(e)), onmouseleave: () => showSelectedGhosts() },
              el("label", {}, box, el("span", {}, e.title, el("small", { textContent: e.description }))));
          })
        : [el("li", { className: "hint", textContent: "Nothing to change for this orientation and material." })]),
    );
    showSelectedGhosts();
    setStatus(state.summary);
    return r;
  });
}

// Tolerances & tuning: the slicer's hole settings go into the 3MF like any other change.
$("hole-comp").onchange = () => {
  remember("hole compensation");
  state.overrides.xy_hole_compensation = String(Math.max(0, Number($("hole-comp").value) || 0));
  renderSettings();
};
$("polyholes").onchange = () => {
  remember("polyholes");
  state.overrides.hole_to_polyhole = $("polyholes").checked ? "1" : "0";
  renderSettings();
};
$("open-fit-test").onclick = () => {
  $("my-presets").open = true;
  $("new-material").click();
  steps.reveal($("calibration"));
  $("calibration").querySelector("[data-calibration=fit]").scrollIntoView({ block: "center" });
};

// ---------- feature confirm ("is this a hole?") ----------
const shown = (f) => f.type === "hole" || f.full_circle; // partial bosses are rounded edges: not worth a line
const FEATURE_NAME = { hole: "hole", boss: "round post" };

function renderFeatures(features) {
  const list = features.filter(shown);
  // Only unsure ones are a question; a clear hole is just listed (with a quiet way out if it isn't one).
  const unsure = (f) => f.confidence === "low" && !f.tag;
  const ask = list.filter(unsure).length;
  $("features-summary").textContent = !list.length
    ? "No round holes or posts found."
    : `Found ${list.length} round feature(s).${ask ? ` ${ask === 1 ? "One needs" : `${ask} need`} your answer.` : ""} Click a name to see it on the part.`;
  $("features").replaceChildren(
    ...list.map((f) => {
      const sideways = Math.abs(f.axis[2]) < 0.3 ? "lying" : Math.abs(f.axis[2]) > 0.95 ? "upright" : "slanted";
      const name = el("button", {
        className: "link",
        textContent: `Ø${f.diameter.toFixed(1)} mm ${FEATURE_NAME[f.type]} (${sideways} in the model)`,
        onclick: () => showFeature(f),
      });
      const answer = f.tag
        ? [el("span", { className: "hint", textContent: f.tag === "none" ? `ignored — not a ${FEATURE_NAME[f.type]}` : "confirmed" }),
           el("button", { textContent: "Undo", onclick: () => tagFeature(f.region, null) })]
        : unsure(f)
          ? [el("span", { className: "question", textContent: `Is this a ${FEATURE_NAME[f.type]}? We're not sure.` }),
             el("button", { textContent: "Yes", title: `It is a ${FEATURE_NAME[f.type]}`, onclick: () => tagFeature(f.region, f.type) }),
             el("button", { textContent: `Not a ${FEATURE_NAME[f.type]}`, onclick: () => tagFeature(f.region, "none") })]
          : [el("button", { className: "link quiet", textContent: `Not a ${FEATURE_NAME[f.type]}?`, onclick: () => tagFeature(f.region, "none") })];
      return el("li", { className: [f.tag === "none" ? "rejected" : "", unsure(f) ? "ask" : ""].join(" ") }, name, ...answer);
    }),
  );
  renderFits(list.filter((f) => f.tag !== "none" && f.full_circle));
}

/** Tolerances & tuning: each round hole or post with what goes in it. */
function renderFits(list) {
  $("fits-summary").textContent = list.length ? "Click a name to see it on the part." : "No round holes or posts found.";
  $("fits").replaceChildren(...list.map((f) => {
    const sideways = Math.abs(f.axis[2]) < 0.3 ? "lying" : Math.abs(f.axis[2]) > 0.95 ? "upright" : "slanted";
    return el("li", {}, el("button", { className: "link", textContent: `Ø${f.diameter.toFixed(1)} mm ${FEATURE_NAME[f.type]} (${sideways} in the model)`, onclick: () => showFeature(f) }),
      fitControls(f));
  }));
}

const FIT_LABEL = { press: "pressed in", snug: "snug", sliding: "sliding / turning", loose: "loose" };

/** "What fits in it?": the mating part's size and the fit → the size to model, offered as a design change. */
function fitControls(f) {
  const hole = f.type === "hole";
  const size = el("input", { type: "number", min: 0.5, step: 0.1, value: state.fitNotes?.[f.region]?.partner ?? Math.round(f.diameter * 2) / 2,
    title: hole ? "Diameter of what goes into the hole (rod, pin, bearing, screw…)" : "Diameter of the hole the post goes into" });
  const kind = el("select", {}, ...Object.entries(FIT_LABEL).map(([v, t]) => el("option", { value: v, textContent: t })));
  kind.value = state.fitNotes?.[f.region]?.fit ?? "sliding";
  const note = state.fitNotes?.[f.region]?.text;
  const go = el("button", { textContent: "Size it", onclick: () =>
    busy("Working out the size…", async () => {
      const r = await call("set_fit", { region: f.region, up: activeOption().up, partner_mm: Number(size.value), fit: kind.value, profile: profile() });
      state.fitNotes = { ...state.fitNotes, [f.region]: { partner: Number(size.value), fit: kind.value, text: r.explanation } };
      if (mode() === "print") $("design-on").click(); // the new size is a design change: show them
      await suggestDesign();
    }) });
  return el("div", { className: "fit row" }, el("span", { className: "hint", textContent: hole ? "What goes in: Ø" : "Goes into: Ø" }), size, "mm,", kind, go,
    note ? el("p", { className: "hint", textContent: note }) : "");
}

async function showFeature(f) {
  await ensureRegions();
  state.selectedRegion = f.region;
  state.showHeat = false;
  recolor();
}

/** Record the user's answer, then redo the edits and the check (rejected holes drop out of both). */
async function tagFeature(region, tag) {
  await busy("Updating…", () => call("tag_feature", { region, tag }));
  state.selectedRegion = -1;
  await runCheck();
  return suggestDesign();
}

/** Edit sizes from the inputs; empty until the first suggestion filled them in (engine defaults). */
const designParams = () =>
  Object.fromEntries([...document.querySelectorAll("#edit-params input")].filter((i) => i.value !== "").map((i) => [i.dataset.param, Number(i.value)]));
document.querySelectorAll("#edit-params input").forEach((i) => (i.onchange = suggestDesign));

/** The chamfer at the weak spot on a new version, checked again: the strength before and after side by side. */
async function testStressChamfer() {
  const before = state.strength?.safety_factor;
  await suggestDesign();
  const edit = state.edits.find((e) => e.rule === "stress_at_inside_corner");
  if (!edit) return setStatus("No chamfer fits at the weak spot: make the part thicker there in your CAD.");
  await applyEdits([edit]);
  if (!readyForStrength()) return setStatus("The chamfer is on a new version, but the loads didn't carry over: set them again to compare.");
  await runStrength();
  const after = state.strength.safety_factor;
  setStatus(`With the chamfer: safety ×${after.toFixed(1)}${before ? ` (was ×${before.toFixed(1)})` : ""}. Keep it, or go back with the Model list or Ctrl+Z.`);
}

/** Apply ``chosen`` (default: the ticked suggestions) as a new version of the model. */
async function applyEdits(chosen = state.edits.filter((e) => e.selected)) {
  return busy("Applying design changes…", async () => {
    const soup = await applyDesignEdits(state.triangles, chosen);
    const name = state.name.replace(/(_dfm)?\.stl$/i, "") + "_dfm.stl";
    const step = state.step && { ...state.step, edits: [...state.step.edits, ...chosen] };
    remember("design changes");
    const carry = carryLoads();
    addVersion(chosen[0].title.replace(/ \(.*\)$/, "") + (chosen.length > 1 ? ` + ${chosen.length - 1} more` : ""));
    await loadFile(name, stlBytes(soup), null, step, "version");
    await restoreLoads(carry);
    setStatus(`${state.summary} — design changes applied as a new version (Undo or the Model list goes back). Download the ${step ? "STEP" : "STL"} to bring them back into your CAD.`);
  });
}

// ---------- undo and model versions ----------
// Every applied design change keeps the model before it as a version: switch back, compare, or undo.
const undoStack = [];

/** Call before a change: Undo (and Ctrl+Z) puts the loads, marks, settings and model version back. */
function remember(label) {
  undoStack.push({ label, version: state.version, loads: structuredClone(state.loads), marks: structuredClone(state.marks), overrides: { ...state.overrides },
    zones: structuredClone(state.zones), volumes: structuredClone(state.volumes) });
  if (undoStack.length > 50) undoStack.shift();
  renderUndo();
}

function renderUndo() {
  const last = undoStack.at(-1);
  $("undo").hidden = !last;
  if (last) $("undo").title = `Undo ${last.label} (Ctrl+Z)`;
}

async function undo() {
  const u = undoStack.pop();
  renderUndo();
  if (!u || busyCount) return;
  const marksChanged = JSON.stringify(u.marks) !== JSON.stringify(state.marks);
  if (u.version !== state.version) await showVersion(u.version, false);
  Object.assign(state, { loads: u.loads, marks: u.marks, overrides: u.overrides });
  if (state.loads.length) await ensureRegions();
  renderLoads();
  renderMarks();
  renderSettings();
  setZones(u.zones ?? []);
  setVolumes(u.volumes ?? []);
  recolor();
  if (marksChanged) await findOrientations(Boolean(state.strength));
  setStatus(`Undone: ${u.label}.`);
}
$("undo").onclick = undo;
addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "z" && !e.target.closest?.("input, textarea, select")) {
    e.preventDefault();
    undo();
  }
});

/** A new version (the model about to be loaded) after the current ones. */
function addVersion(label) {
  state.versions.push({ label, results: {} });
  state.version = state.versions.length - 1;
}

/** Results of the shown version, for the comparison table. */
function noteVersion(results) {
  const v = state.versions?.[state.version];
  if (!v) return;
  v.results = { ...v.results, ...results };
  renderVersions();
}

const versionName = (v, i) => (i === 0 ? v.label : `${i + 1}: ${v.label}`);

function renderVersions() {
  const list = state.versions ?? [];
  $("version-pick").hidden = list.length < 2;
  $("version").replaceChildren(...list.map((v, i) => el("option", { value: i, textContent: versionName(v, i), selected: i === state.version })));
  const fmt = (r) => [r?.time != null ? fmtTime(r.time) : "–", r?.grams != null ? `${Math.round(r.grams)} g` : "–",
    r?.problems != null ? String(r.problems) : "–", r?.safety != null ? `×${r.safety.toFixed(1)}` : "–"];
  $("versions").replaceChildren(
    el("tr", {}, ...["Version", "Time", "Plastic", "Problems", "Strength", ""].map((t) => el("th", { textContent: t }))),
    ...list.map((v, i) => el("tr", { className: i === state.version ? "current" : "" },
      el("td", { className: "name", textContent: versionName(v, i), title: versionName(v, i) }), ...fmt(v.results).map((t) => el("td", { textContent: t })),
      el("td", {}, i === state.version ? "shown" : el("button", { className: "small", textContent: "Show", onclick: () => showVersion(i) })))),
  );
}
$("version").onchange = () => showVersion(Number($("version").value));
$("compare-file").onchange = async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (file) await openFile(file.name, new Uint8Array(await file.arrayBuffer()), "version");
};

/** Loads and held faces by where they are on the part, to put them back on an edited model. */
function carryLoads() {
  return state.loads.map((l) => ({ load: l, at: l.point ? null : state.regionInfo[l.region] }));
}

/** Put carried loads on the same faces of the new model: the face nearest the old one's middle that
 * faces the same way (spots keep their point). Faces that are gone drop out. */
async function restoreLoads(carry) {
  if (!carry.length) return;
  await ensureRegions();
  const loads = [];
  for (const { load, at } of carry) {
    if (!at) {
      loads.push(load);
      continue;
    }
    let best = -1;
    let bestD = Infinity;
    state.regionInfo.forEach((r, i) => {
      const facing = r.normal[0] * at.normal[0] + r.normal[1] * at.normal[1] + r.normal[2] * at.normal[2];
      const d = Math.hypot(r.centroid[0] - at.centroid[0], r.centroid[1] - at.centroid[1], r.centroid[2] - at.centroid[2]);
      if (facing > 0.9 && d < bestD) [best, bestD] = [i, d];
    });
    if (best >= 0) loads.push({ ...load, region: best });
  }
  state.loads = loads;
  renderLoads();
  recolor();
}

/** Show version ``i``: load its model again, with the loads carried over (``carry: false`` for undo,
 * which restores its own). */
async function showVersion(i, carry = true) {
  const v = state.versions[i];
  if (!v?.source || i === state.version) return renderVersions();
  const loads = carry ? carryLoads() : [];
  state.version = i;
  await loadFile(v.source.name, v.source.bytes, v.source.faceIds, v.step, "version");
  if (v.source.body) await busy("Switching part…", async () => partLoaded(await call("select_body", { index: v.source.body })));
  await restoreLoads(loads);
  setStatus(`${state.summary} — showing ${versionName(v, i)}.`);
}
$("apply-edits").onclick = () => applyEdits();

// ---------- AI assistant (bring your own key) ----------
const round = (v, d = 2) => (Array.isArray(v) ? v.map((x) => round(x, d)) : typeof v === "number" ? Number(v.toFixed(d)) : v);

function summarizeState() {
  const o = activeOption();
  const s = state.strength;
  return {
    part: state.summary,
    profile: profile(),
    mode: mode(),
    orientation: o && { index: state.active, label: o.label, up: round(o.up, 3), reasons: o.reasons },
    findings: state.check?.findings.map((f) => ({ severity: f.severity, rule: f.rule, title: f.title, detail: f.detail })),
    estimate: state.check && { print_time_min: Math.round(state.check.stats.print_time_min), filament_g: Math.round(state.check.stats.filament_g) },
    settings: Object.fromEntries(effectiveSettings().map((x) => [x.key, x.value])),
    loads: state.loads,
    strength: s && { safety_factor: round(s.safety_factor), verdict: s.verdict, mode: s.mode, walls: s.walls, infill: s.infill },
  };
}

// What the AI may do: the same actions as the UI. Shared by all providers.
const aiActions = {
  get_current_analysis: async () => summarizeState(),
  list_orientations: async () => state.options.map((o, i) => ({ index: i, label: o.label, up: round(o.up, 3), reasons: o.reasons, why_lower: o.why_lower })),
  select_orientation: async ({ index }) => {
    if (!state.options[index]) throw new Error(`No orientation ${index}`);
    await selectOption(index);
    return summarizeState();
  },
  list_faces: async () => {
    await ensureRegions();
    return state.regionInfo.map((r) => ({ id: r.id, kind: r.kind, area_mm2: Math.round(r.area_mm2), normal: round(r.normal), centroid: round(r.centroid, 1) }));
  },
  set_loads: async ({ loads }) => {
    await ensureRegions();
    for (const l of loads) if (!l.point && !state.regionInfo[l.region]) throw new Error(`Unknown face ${l.region}`);
    state.loads = loads.map((l) => {
      const at = l.point ? { point: l.point, radius: l.radius ?? SPOT_RADIUS } : { region: l.region };
      return l.kind === "fixed" ? { ...at, kind: "fixed", ...(l.hold && l.hold !== "clamped" ? { hold: l.hold } : {}) } : { ...at, kind: "force", kg: l.kg ?? 1, direction: l.direction ?? "push", case: l.case ?? 1 };
    });
    renderLoads();
    recolor();
    return { loads: state.loads };
  },
  check_strength: async () => {
    if (!readyForStrength()) throw new Error("Set loads first: at least one fixed face and one force");
    const r = await runStrength();
    return { safety_factor: round(r.safety_factor), verdict: r.verdict, mode: r.mode, explanation: r.explanation, bends_mm: round(r.max_displacement_mm, 1),
      walls: r.walls, top: r.top, bottom: r.bottom, infill: r.infill };
  },
  optimise_strength: async () => {
    if (!readyForStrength()) throw new Error("Set loads first: at least one fixed face and one force");
    const o = await runOptimise();
    return { met: o.met, target: o.target, safety_factor: round(o.safety_factor), grams: o.grams, orientation: o.orientation, settings: o.settings, explanation: o.explanation,
      ...(o.beyond ? { beyond: o.beyond.map((b) => ({ change: b.change, kind: b.kind, safety_factor: round(b.safety_factor), why: b.why })) } : {}) };
  },
  strength_options: async () => {
    if (!state.strength) throw new Error("Run check_strength first");
    return (await showStrengthOptions()).map((o) => ({ ...o, safety_factor: round(o.safety_factor) }));
  },
  suggest_design_changes: async () => {
    const r = await suggestDesign();
    return {
      features: r.features.filter(shown).map((f) => ({ region: f.region, type: f.type, diameter_mm: round(f.diameter, 1), axis: round(f.axis), confidence: f.confidence, tag: f.tag })),
      edits: r.edits.map((e) => ({ id: e.id, op: e.op, title: e.title, description: e.description })),
    };
  },
  tag_feature: async ({ region, tag }) => {
    const r = await tagFeature(region, tag === "forget" ? null : tag);
    return r.features.filter(shown).map((f) => ({ region: f.region, type: f.type, diameter_mm: round(f.diameter, 1), tag: f.tag }));
  },
  apply_design_changes: async ({ ids }) => {
    if (!state.edits.length) await suggestDesign();
    const unknown = ids.filter((id) => !state.edits.some((e) => e.id === id));
    if (unknown.length) throw new Error(`Unknown edit ids: ${unknown.join(", ")}`);
    state.edits.forEach((e) => (e.selected = ids.includes(e.id)));
    await applyEdits();
    return summarizeState();
  },
  set_slicer_settings: async ({ settings }) => {
    Object.assign(state.overrides, settings);
    renderSettings();
    return Object.fromEntries(effectiveSettings().map((x) => [x.key, x.value]));
  },
};

const PROVIDERS = {
  anthropic: {
    assistant: new Assistant(aiActions),
    storage: "fdmdfm.anthropicKey",
    placeholder: "sk-ant-...",
    keyHelp: "console.anthropic.com -> API keys",
    models: async () => MODELS,
  },
  gemini: {
    assistant: new GeminiAssistant(aiActions),
    storage: "fdmdfm.geminiKey",
    placeholder: "AIza...",
    keyHelp: "aistudio.google.com -> Get API key",
    models: (key) => listGeminiModels(key),
  },
  openai: {
    assistant: new OpenAIAssistant(aiActions),
    storage: "fdmdfm.openaiKey",
    placeholder: "sk-...",
    keyHelp: "platform.openai.com -> API keys",
    models: (key) => listOpenAIModels(key),
  },
};
const provider = () => PROVIDERS[$("ai-provider").value];

async function refreshModels() {
  const p = provider();
  const key = $("ai-key").value.trim();
  if (!key && p !== PROVIDERS.anthropic) {
    $("ai-model").replaceChildren(el("option", { value: "", textContent: "Enter your key to load models" }));
    return;
  }
  try {
    const models = await p.models(key);
    $("ai-model").replaceChildren(...models.map((m) => el("option", { value: m.id, textContent: m.label })));
  } catch (err) {
    $("ai-model").replaceChildren(el("option", { value: "", textContent: "Could not load models" }));
    aiLog("tool", err.message);
  }
}

function switchProvider() {
  const p = provider();
  $("ai-key").placeholder = p.placeholder;
  $("ai-key").value = localStorage.getItem(p.storage) ?? "";
  $("ai-remember").checked = Boolean(localStorage.getItem(p.storage));
  refreshModels();
}

const rememberKey = () =>
  $("ai-remember").checked ? localStorage.setItem(provider().storage, $("ai-key").value) : localStorage.removeItem(provider().storage);
$("ai-provider").onchange = switchProvider;
$("ai-remember").onchange = rememberKey;
$("ai-key").onchange = () => {
  rememberKey();
  refreshModels();
};
switchProvider();

function aiLog(cls, text) {
  const node = el("div", { className: `msg ${cls}`, textContent: text });
  $("ai-log").append(node);
  node.scrollIntoView({ block: "nearest" });
}

const TOOL_LABEL = {
  get_current_analysis: "reading the analysis",
  list_orientations: "comparing orientations",
  select_orientation: "switching orientation",
  list_faces: "looking at the faces",
  set_loads: "setting loads",
  check_strength: "running the strength check",
  strength_options: "trying stronger options",
  optimise_strength: "finding the lightest settings that hold",
  suggest_design_changes: "looking for design changes",
  apply_design_changes: "applying design changes",
  tag_feature: "noting what a feature is",
  set_slicer_settings: "changing slicer settings",
};

$("ai-form").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("ai-input").value.trim();
  const apiKey = $("ai-key").value.trim();
  if (!text) return;
  if (!apiKey) return aiLog("tool", `Enter your API key first (${provider().keyHelp}).`);
  if (!$("ai-model").value) return aiLog("tool", "Pick a model first.");
  $("ai-input").value = "";
  aiLog("user", text);
  const button = $("ai-form").querySelector("button");
  button.disabled = true;
  try {
    await provider().assistant.ask(text, { apiKey, model: $("ai-model").value }, (ev) =>
      ev.type === "text" ? aiLog("assistant", ev.text) : aiLog("tool", `... ${TOOL_LABEL[ev.name] ?? ev.name}`),
    );
  } catch (err) {
    aiLog("tool", `Error: ${err.message}`);
  } finally {
    button.disabled = false;
  }
};
$("ai-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("ai-form").requestSubmit();
  }
});

// ---------- Expert: all settings ----------
const loadExpert = () => JSON.parse(localStorage.getItem("fdm-expert") ?? "{}");
let expertChanged = false;

/** Send the saved Expert values; ones the engine no longer knows (an update renamed them) are dropped. */
async function sendExpert() {
  const values = loadExpert();
  if (!Object.keys(values).length) return;
  try {
    await call("set_expert", { values });
  } catch (err) {
    localStorage.removeItem("fdm-expert");
    await call("set_expert", { values: {} });
    setStatus(`Your changed settings were reset: ${err.message}`, true);
  }
}

$("show-expert").onclick = async () => {
  renderExpert(await call("expert_settings"));
  $("expert").showModal();
};
$("expert-close").onclick = () => $("expert").close();
$("expert").onclose = async () => {
  if (!expertChanged) return;
  expertChanged = false;
  if (!activeOption()) return;
  await runCheck(); // detection limits changed: check again (strength runs again when asked)
  setStatus("Checked again with your settings.");
};
$("expert-filter").oninput = () => filterExpert();
$("expert-reset").onclick = async () => {
  localStorage.removeItem("fdm-expert");
  expertChanged = true;
  renderExpert(await call("set_expert", { values: {} }));
};

function renderExpert(r) {
  const saved = loadExpert();
  const open = new Set([...$("expert-sections").querySelectorAll("details[open]")].map((d) => d.dataset.section));
  $("expert-sections").replaceChildren(...r.sections.map((sec) => {
    const rows = sec.values.flatMap((v) => {
      const input = el("input", { type: "number", value: v.value, step: "any", ...(v.min != null ? { min: v.min, max: v.max } : {}) });
      const name = el("span", { className: `name${v.path in saved ? " changed" : ""}`, textContent: v.path.split(".").slice(2).join(".") || v.path.split(".").at(-1) });
      input.onchange = async () => {
        const values = loadExpert();
        const n = Number(input.value);
        if (input.value === "" || n === v.default) delete values[v.path];
        else values[v.path] = n;
        try {
          const r = await call("set_expert", { values });
          localStorage.setItem("fdm-expert", JSON.stringify(values)); // before drawing: it marks the changed ones
          expertChanged = true;
          renderExpert(r);
        } catch (err) {
          setStatus(err.message, true);
        }
      };
      const row = [name, input, el("span", { className: "note", textContent: `${v.note}${v.note ? " · " : ""}default ${v.default}` })];
      for (const x of row) x.dataset.find = `${v.path} ${v.note}`.toLowerCase();
      return row;
    });
    const box = el("details", { className: "more-block" },
      el("summary", {}, sec.title, el("small", { textContent: `${sec.values.length} value${sec.values.length > 1 ? "s" : ""}` })),
      sec.about ? el("p", { className: "hint", textContent: sec.about }) : "",
      el("div", { className: "expert-values" }, ...rows));
    box.dataset.section = sec.name;
    box.open = open.has(sec.name);
    return box;
  }));
  filterExpert();
}

function filterExpert() {
  const q = $("expert-filter").value.trim().toLowerCase();
  for (const box of $("expert-sections").children) {
    let any = false;
    for (const x of box.querySelectorAll(".expert-values > *")) {
      const hit = !q || x.dataset.find.includes(q) || box.dataset.section.includes(q);
      x.hidden = !hit;
      any ||= hit;
    }
    box.hidden = !any;
    if (q) box.open = any;
  }
}

// ---------- presets ----------
async function initPresets() {
  await engineReady;
  // The printer and material you have, remembered from your last visit: Simple is then one drop.
  const saved = JSON.parse(localStorage.getItem("fdm-setup") ?? "{}");
  await applyMine(loadMine(), { printer: "bambu_p1s", material: "pla", nozzle: "0.4", ...saved });
  await sendExpert();
  setStatus(saved.printer
    ? `Ready. Set up for your ${state.presets.printers[$("printer").value].name} with ${state.presets.materials[$("material").value].name}: drop a part, or try a sample.`
    : "Ready — drop an STL, 3MF or STEP file, or try a sample.");
}

/** Send the user's own presets to the engine and refill the dropdowns, keeping (or setting) the selection. */
async function applyMine(mine, select = {}) {
  const p = await call("custom_presets", mine);
  state.presets = p;
  const fill = (sel, entries, value) =>
    $(sel).replaceChildren(...entries.map(([k, label]) => el("option", { value: k, textContent: label, selected: k === value })));
  const keep = { printer: $("printer").value, material: $("material").value, nozzle: $("nozzle").value, ...select };
  const has = (table, id) => id && id in p[table];
  fill("printer", Object.entries(p.printers).map(([k, v]) => [k, v.name]), has("printers", keep.printer) ? keep.printer : "bambu_p1s");
  fill("material", Object.entries(p.materials).map(([k, v]) => [k, v.name]), has("materials", keep.material) ? keep.material : "pla");
  fill("nozzle", Object.keys(p.nozzles).map((k) => [k, `${k} mm`]), keep.nozzle || "0.4");
  viewer.setBed(p.printers[$("printer").value].bed);
  syncMultiMaterial();
  renderMine(mine);
}

// ---------- my printers & materials ----------
let editing = null; // {table: "printers"|"materials", id}

function renderMine(mine) {
  const rows = [];
  for (const table of ["printers", "materials"]) {
    for (const id of Object.keys(mine[table])) {
      const pid = id.startsWith("my_") ? id : `my_${id}`;
      rows.push(el("li", {}, el("span", { textContent: `${state.presets[table][pid]?.name ?? id} (${table === "printers" ? "printer" : "material"})` }),
        el("button", { textContent: "Edit", onclick: () => openPresetForm(table, id) }),
        el("button", { textContent: "×", title: "Delete", onclick: () => {
          delete mine[table][id];
          saveMine(mine);
          applyMine(mine).then(() => state.triangles && findOrientations());
        } })));
    }
  }
  $("my-list").replaceChildren(...rows);
  $("copy-yaml").hidden = !rows.length;
}

function openPresetForm(table, id = null, values = null) {
  const builtins = Object.fromEntries(Object.entries(state.presets[table]).filter(([k]) => !k.startsWith("my_")));
  const current = id ? state.presets[table][id.startsWith("my_") ? id : `my_${id}`] : null;
  const base = values?.based_on ?? current?.based_on ?? (table === "materials" ? $("material").value : $("printer").value);
  const start = values ?? toForm(table, current ?? builtins[base] ?? Object.values(builtins)[0]);
  start.based_on = builtins[base] ? base : Object.keys(builtins)[0];
  if (!id && !values) start.name = `My ${start.name}`;
  editing = { table, id: id ?? `${table === "materials" ? "mat" : "printer"}_${Date.now().toString(36)}` };
  const input = ([key, label, type, step]) => {
    const v = start[key] ?? "";
    const field = Array.isArray(type)
      ? el("select", { name: key }, ...type.map((o) => el("option", { value: o, textContent: o, selected: o === v })))
      : el("input", { name: key, type, step: step ?? "any", value: type === "checkbox" ? "" : v, checked: type === "checkbox" && Boolean(v) });
    return el("label", {}, label, field);
  };
  const basedOn = el("label", {}, "Based on",
    el("select", { name: "based_on" }, ...Object.entries(builtins).map(([k, b]) => el("option", { value: k, textContent: b.name, selected: k === start.based_on }))));
  $("preset-fields").replaceChildren(basedOn, ...(table === "materials" ? MATERIAL_FIELDS : PRINTER_FIELDS).map(input));
  // A new base fills in its values; the name the user typed stays.
  basedOn.querySelector("select").onchange = (e) =>
    openPresetForm(table, id, { ...toForm(table, builtins[e.target.value]), based_on: e.target.value, name: formValues().name });
  $("calibration").hidden = table !== "materials";
  $("preset-form").hidden = false;
  $("my-presets").open = true;
}

function formValues() {
  const out = {};
  for (const f of $("preset-form").querySelectorAll("#preset-fields [name]")) out[f.name] = f.type === "checkbox" ? f.checked : f.value;
  return out;
}

$("new-material").onclick = () => openPresetForm("materials");
$("new-printer").onclick = () => openPresetForm("printers");
$("preset-cancel").onclick = () => ($("preset-form").hidden = true);
$("preset-form").onsubmit = async (e) => {
  e.preventDefault();
  const mine = loadMine();
  const { table, id } = editing;
  const before = structuredClone(mine);
  mine[table][id] = toSpec(table, formValues());
  try {
    await applyMine(mine, { [table === "materials" ? "material" : "printer"]: id.startsWith("my_") ? id : `my_${id}` });
  } catch (err) {
    await applyMine(before);
    return setStatus(`Not saved: ${err.message}`, true);
  }
  saveMine(mine);
  $("preset-form").hidden = true;
  setStatus(`Saved "${formValues().name}". It's selected now.`);
  if (state.triangles) findOrientations();
};
$("import-orca").onchange = async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    const builtins = Object.fromEntries(Object.entries(state.presets.materials).filter(([k]) => !k.startsWith("my_")));
    openPresetForm("materials", null, fromOrcaFilament(JSON.parse(await file.text()), builtins));
    setStatus(`Read ${file.name} — check the values, then Save.`);
  } catch (err) {
    setStatus(`Could not read ${file.name}: ${err.message}`, true);
  }
};
$("copy-yaml").onclick = async () => {
  await navigator.clipboard.writeText(toYaml(state.presets, loadMine()));
  setStatus("Copied — paste it into a pull request for core/fdmcore/data/presets.yaml (with sources).");
};
document.querySelectorAll("[data-calibration]").forEach((b) => (b.onclick = async () => {
  const part = await call("calibration_part", { kind: b.dataset.calibration });
  download(part.stl, part.name, "model/stl");
}));
$("cal-z").onclick = () => {
  const flat = Number($("cal-flat").value), upright = Number($("cal-upright").value);
  if (!(flat > 0 && upright > 0)) return setStatus("Enter both weights first.", true);
  $("preset-fields").querySelector("[name=z_strength]").value = zFactor(flat, upright).toFixed(2);
};
// Fit / overhang / bridge test results go straight into the form fields they measure.
const options = (id, values, label) => $(id).append(...values.map((v) => el("option", { value: v, textContent: label(v) })));
options("cal-fit-snug", [0, 0.1, 0.2, 0.3, 0.4, 0.5], (g) => `Ø${(6 + g).toFixed(1)} mm`);
options("cal-fit-slide", [0, 0.1, 0.2, 0.3, 0.4, 0.5], (g) => `Ø${(6 + g).toFixed(1)} mm`);
options("cal-overhang", [40, 45, 50, 55, 60, 65, 70], (a) => `${a}°`);
options("cal-bridge", [10, 20, 30, 40, 50], (l) => `${l} mm`);
const setField = (name, value) => ($("preset-fields").querySelector(`[name=${name}]`).value = value);
$("cal-fit-use").onclick = () => {
  const snug = $("cal-fit-snug").value, slide = $("cal-fit-slide").value;
  if (snug === "" && slide === "") return setStatus("Pick the holes first.", true);
  if (snug !== "") setField("fit_snug", snug);
  if (slide !== "") setField("fit_sliding", slide);
};
$("cal-overhang-use").onclick = () => $("cal-overhang").value ? setField("max_overhang_deg", $("cal-overhang").value) : setStatus("Pick the steepest clean block first.", true);
$("cal-bridge-use").onclick = () => $("cal-bridge").value ? setField("max_bridge_mm", $("cal-bridge").value) : setStatus("Pick the longest bridge first.", true);
$("cal-warp-use").onclick = () => {
  const longest = Number($("cal-warp").value);
  if (!longest) return setStatus("Pick the longest bar that stayed flat.", true);
  $("preset-fields").querySelector("[name=warp_length]").value = warpLength(longest);
};
initPresets().catch((err) => setStatus(`Engine failed to start: ${err.message}`, true));

/** Multi-material checkbox follows the printer: none = off, built-in = on, optional add-on = user's choice. */
function syncMultiMaterial() {
  const mm = state.presets.printers[$("printer").value].multi_material;
  const box = $("multi-material");
  box.disabled = mm !== "optional";
  if (mm !== "optional") box.checked = mm === "builtin";
  $("mm-row").title = { none: "This printer has no multi-material option", builtin: "Built in on this printer", optional: "AMS / MMU add-on" }[mm];
}

/** A nozzle's problems: the ones a smaller nozzle doesn't have first, then how much detail it can't trace. */
function nozzleProblems(r) {
  const titles = (fresh) => [...new Set(r.issues.filter((i) => i.new === fresh).map((i) => i.title))];
  const out = [
    ...titles(true).map((t) => el("div", { className: "new-issue", textContent: `${t} (only with bigger nozzles)` })),
    ...titles(false).map((t) => el("div", { textContent: t })),
  ];
  if (r.lost_mm3 >= 0.5) out.push(el("div", { className: "note", textContent: `≈${r.lost_mm3.toFixed(0)} mm³ too thin to print at all (most at ${r.lost_at_mm} mm)` }));
  if (r.rough_mm3 >= 0.5) out.push(el("div", { className: "note", textContent: `≈${r.rough_mm3.toFixed(0)} mm³ of fine detail prints rough` }));
  return out.length ? out : ["none"];
}

async function compareNozzles() {
  await busy("Comparing nozzle sizes…", async () => {
    const rows = await call("compare_nozzles", { up: activeOption().up, profile: profile() });
    $("nozzle-table").replaceChildren(
      el("tr", {}, ...["Nozzle", "Time", "Problems with this nozzle", ""].map((t) => el("th", { textContent: t }))),
      ...rows.map((r) =>
        el(
          "tr",
          { className: r.current ? "current" : "" },
          el("td", { textContent: `${r.nozzle} mm` }),
          el("td", {}, `≈${fmtTime(r.print_time_min)} with ${r.walls} walls`,
            ...(r.note ? [el("div", { className: "note", textContent: r.note })] : [])),
          el("td", {}, ...nozzleProblems(r)),
          el("td", {}, r.current ? "current" : el("button", {
            textContent: "Use",
            onclick: () => {
              $("nozzle").value = r.nozzle;
              $("nozzle").onchange();
            },
          })),
        ),
      ),
    );
    setStatus(state.summary);
  });
}
$("nozzle-block").ontoggle = (e) => e.target.open && state.triangles && compareNozzles();

for (const id of ["printer", "material", "nozzle", "multi-material"]) {
  $(id).onchange = () => {
    localStorage.setItem("fdm-setup", JSON.stringify({ printer: $("printer").value, material: $("material").value, nozzle: $("nozzle").value }));
    viewer.setBed(state.presets.printers[$("printer").value].bed);
    syncMultiMaterial();
    $("nozzle-block").open = false; // comparison belongs to the old setup
    if (state.triangles) findOrientations();
  };
}
const GOAL_NOTES = {
  balanced: "Balanced: standard walls & infill for everyday printing.",
  strength: "Strength: 4 walls, 25% infill; weak spots and thin walls are treated as high-priority problems.",
  speed: "Speed: fewer walls, fast grid infill, taller layers to print as fast as possible.",
  material: "Material: minimal walls & lightning infill to save maximum filament.",
};

const MODE_NOTES = {
  print: "Just print it: only slicer & printer settings are suggested — your CAD model is unchanged.",
  design: "The design: only geometry improvements you can take back into CAD (e.g. teardrop holes).",
  both: "Both: shows all design geometry edits, slicer settings, and printer adjustments.",
};

function updateGoalNote() {
  const g = document.querySelector("input[name=goal]:checked")?.value;
  if ($("goal-note")) $("goal-note").textContent = GOAL_NOTES[g] || "";
}

function updateModeNote() {
  const m = mode();
  if ($("mode-note")) $("mode-note").textContent = MODE_NOTES[m] || "";
  $("design-block").hidden = m === "print";
  $("design-off").hidden = m !== "print";
}
$("design-on").onclick = () => {
  const both = document.querySelector("input[name=mode][value=both]");
  both.checked = true;
  both.dispatchEvent(new Event("change"));
  $("design-block").scrollIntoView({ behavior: "smooth", block: "start" });
};
$("edit-shells").onclick = () => {
  const d = document.querySelector(".shells");
  steps.reveal(d);
  d.scrollIntoView({ behavior: "smooth", block: "center" });
};
// Clicking the option that is already chosen still answers, so it doesn't look frozen.
document.querySelectorAll("input[name=mode], input[name=goal]").forEach((r) => r.parentElement.addEventListener("click", () => {
  const label = r.parentElement;
  label.classList.remove("pulse");
  void label.offsetWidth; // restart the animation
  label.classList.add("pulse");
}));

document.querySelectorAll("input[name=mode]").forEach((r) => (r.onchange = () => {
  updateModeNote();
  if (state.triangles) runCheck();
}));
document.querySelectorAll("input[name=goal]").forEach((r) => (r.onchange = () => {
  updateGoalNote();
  if (state.triangles) findOrientations();
}));
updateModeNote();
updateGoalNote();

$("working-cancel").onclick = cancelEngine;

// ---------- levels and the start guide ----------
const LEVELS = ["simple", "basic", "advanced", "expert"];
function setLevel(level) {
  if (level === "advanced-old") level = "advanced";
  if (!LEVELS.includes(level)) return;
  document.body.dataset.level = level;
  document.querySelector(`input[name=level][value=${level}]`).checked = true;
  localStorage.setItem("fdm-level", level);
  steps.setLevel(level);
  setTimeout(() => viewer.resize()); // the step list comes and goes (Expert), so the 3D view changes size
}
for (const r of document.querySelectorAll("input[name=level]")) r.onchange = () => setLevel(r.value);
setLevel(new URLSearchParams(location.search).get("level") ?? localStorage.getItem("fdm-level") ?? "simple");

/** Set "What can you change?" like a click would (it re-checks the part). */
function setMode(value) {
  const r = document.querySelector(`input[name=mode][value=${value}]`);
  if (r.checked) return;
  r.checked = true;
  r.dispatchEvent(new Event("change", { bubbles: true }));
}

const GUIDE = {
  settings: { level: "simple", mode: "print", next: "Drop your part, or try a sample." },
  strength: { level: "basic", mode: "print", next: "Drop your part, or try a sample. Then follow the steps on the left." },
  steps: { level: "advanced", mode: "both", next: "Drop your part, or try a sample. Then follow the steps on the left." },
  expert: { level: "expert", mode: "both", next: "Everything is on this page. Drop a part to start." },
};
for (const b of document.querySelectorAll("[data-guide]")) {
  b.onclick = () => {
    const g = GUIDE[b.dataset.guide];
    setLevel(g.level);
    setMode(g.mode);
    localStorage.setItem("fdm-guided", "1");
    $("guide").close();
    setStatus(g.next);
  };
}
/** The guide marks the level you are on and starts there, so Enter keeps it. */
function openGuide() {
  const level = document.body.dataset.level;
  for (const b of document.querySelectorAll("[data-guide]")) {
    const now = GUIDE[b.dataset.guide].level === level;
    b.classList.toggle("current", now);
    b.toggleAttribute("autofocus", now);
  }
  $("guide").showModal();
}
$("show-guide").onclick = openGuide;
if (!localStorage.getItem("fdm-guided") && !new URLSearchParams(location.search).has("level")) openGuide();

// ---------- input ----------
$("file").onchange = async (e) => {
  const file = e.target.files[0];
  if (file) openFile(file.name, new Uint8Array(await file.arrayBuffer()));
};
const drop = $("drop");
for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => (e.preventDefault(), drop.classList.add("over")));
for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, () => drop.classList.remove("over"));
drop.addEventListener("drop", async (e) => {
  e.preventDefault();
  const file = e.dataTransfer.files[0];
  if (file) openFile(file.name, new Uint8Array(await file.arrayBuffer()));
});
document.querySelectorAll("[data-sample]").forEach((btn) => {
  btn.onclick = async () => {
    const res = await fetch(`samples/${btn.dataset.sample}.stl`);
    openFile(`${btn.dataset.sample}.stl`, new Uint8Array(await res.arrayBuffer()));
  };
});

// ---------- output ----------
const baseName = () => state.name.replace(/\.(stl|3mf|step|stp)$/i, "");

function download(bytes, filename, type) {
  if (!bytes || (bytes.length === 0 && bytes.byteLength === 0)) {
    setStatus(`Export failed: no data generated for ${filename}`, true);
    return;
  }
  const a = el("a", { href: URL.createObjectURL(new Blob([bytes], { type })), download: filename });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

$("dl-3mf").onclick = async () => {
  await busy("Exporting 3MF for OrcaSlicer / Bambu Studio…", async () => {
    const settings = Object.fromEntries(effectiveSettings().map((s) => [s.key, s.value]));
    const bytes = await call("export", { up: activeOption().up, profile: profile(), settings, zones: printZones(), slicer: $("dl-slicer").value });
    download(bytes, `${baseName()}.3mf`, "model/3mf");
    setStatus(`${state.summary} — 3MF downloaded with per-object slicer settings.`);
  });
};

// ---------- the report for people: one HTML file with pictures ----------
/** Take a picture with only ``block``'s things drawn (problems, strength, design), then put the view back. */
function pictureWith(block, fn) {
  pictureOf = block;
  try {
    recolor();
    return fn();
  } finally {
    pictureOf = null;
  }
}

$("dl-report").onclick = () =>
  busy("Making the report with pictures…", async () => {
    const p = profile();
    const o = activeOption();
    const { findings, stats } = state.check;
    const keep = { showHeat: state.showHeat, showWarp: state.showWarp, showSwing: state.showSwing };
    const report = {
      title: `Print check: ${state.name}`,
      date: new Date().toLocaleDateString(),
      setup: `${state.presets.printers[p.printer]?.name ?? p.printer} · ${state.presets.materials[p.material]?.name ?? p.material} · ${p.nozzle} mm nozzle`,
      verdict: verdict(),
      orientation: { label: state.active === 0 ? "Best orientation" : o.label, reasons: o.reasons },
      stats: `About ${fmtTime(stats.print_time_min)} to print and ${Math.round(stats.filament_g)} g of filament (rough estimate).`,
      settings: effectiveSettings().map((s) => ({ label: s.label, value: settingText(s.key, s.value), reason: s.reason })),
    };
    try {
      Object.assign(state, { showWarp: false, showSwing: false });
      report.overview = pictureWith("problems", () => viewer.snapshot());
      // One card per kind of problem, with a picture of the first place it happens.
      const byRule = new Map();
      findings.forEach((f, i) => byRule.set(f.rule, [...(byRule.get(f.rule) ?? []), i]));
      report.problems = [...byRule.values()].map((idx) => {
        const f = findings[idx[0]];
        const picture = f.bbox ? pictureWith("problems", () => (viewer.showFindings(findings, idx[0], false), viewer.snapshot(f.bbox))) : null;
        return { title: f.title, severity: SEVERITY_LABEL[f.severity], picture, details: idx.map((i) => findings[i].detail), why: f.why,
          fixes: f.fixes.map((fx) => ({ kind: DOMAIN_LABEL[fx.domain], text: fx.text })) };
      });
      const s = state.strength;
      if (s) {
        state.showHeat = true;
        report.strength = { kind: s.verdict === "fine" ? "ok" : s.verdict === "borderline" ? "warn" : "bad",
          head: `${s.verdict[0].toUpperCase() + s.verdict.slice(1)}: safety ×${s.safety_factor.toFixed(1)}`,
          lines: [s.explanation ?? MODE_TEXT[s.mode] ?? "", `${s.material}, ${s.walls} walls, ${Math.round(s.infill * 100)} % infill · bends about ${s.max_displacement_mm.toFixed(1)} mm`,
            ...state.loads.map(loadText)],
          picture: pictureWith("strength", () => viewer.snapshot()),
          note: `Colours: blue = little stress, red = close to breaking (${FIELD[view.field].title}). Rough estimate: aim for ×2 or more.` };
      }
      const w = state.warp;
      if (w) {
        state.showWarp = true;
        report.warp = { kind: w.verdict === "fine" ? "ok" : w.verdict === "might lift" ? "warn" : "bad",
          head: w.verdict === "fine" ? "Stays flat" : w.verdict === "might lift" ? "Corners might lift" : "Corners will likely lift",
          lines: [w.explanation], picture: pictureWith("sims", () => viewer.snapshot()), note: "Shape exaggerated; red dots lift first." };
      }
      const chosen = state.edits.filter((e) => e.selected);
      if (chosen.length) report.design = { items: chosen, picture: pictureWith("design", () => viewer.snapshot()) };
    } finally {
      Object.assign(state, keep);
      recolor();
    }
    download(new TextEncoder().encode(reportHtml(report)), `${baseName()}_report.html`, "text/html");
    setStatus("Report downloaded: open it in your browser, or print it to PDF.");
  });

$("dl-stl").onclick = () => download(stlBytes(state.placed), `${baseName()}_oriented.stl`, "model/stl");
$("dl-stl-model").onclick = () => download(stlBytes(state.triangles), `${baseName()}.stl`, "model/stl");
$("dl-step").onclick = () =>
  busy("Writing STEP with your design changes (first time downloads ~23 MB)…", async () => {
    const out = await stepWithEdits(state.step.bytes, state.step.volume, state.step.edits);
    download(out, `${baseName()}.step`, "model/step");
    setStatus(state.summary);
  });

$("copy-report").onclick = async () => {
  const md = await call("report", { up: activeOption().up, profile: profile(), mode: mode() });
  await navigator.clipboard.writeText(md + answerFormat());
  setStatus("Copied. Paste it into your AI chat so it can help you with the design of your part.");
};

// ---------- paste-back: an answer from any AI chat ----------
/** Report appendix telling a chat AI how to answer so the tool can apply it (only the choices that exist now). */
function answerFormat() {
  const lines = [
    "", "## For the AI assistant: how to answer", "",
    "Answer the user's question in plain language. If you recommend changes, end your reply with one JSON block",
    "the tool can apply (leave out what you don't want to change):", "",
    "```json",
    JSON.stringify({ orientation: 0, settings: { wall_loops: "4" }, design_changes: ["edit-id"], not_features: [0] }),
    "```", "",
    "- `orientation`: index from this list:",
    ...state.options.map((o, i) => `  - ${i}: ${o.label}, up = [${round(o.up, 2).join(", ")}]${i === state.active ? " (current)" : ""}`),
    "- `settings`: OrcaSlicer setting keys with string values (see the settings table above).",
  ];
  if (state.edits.length) lines.push("- `design_changes`: ids of these suggested edits:", ...state.edits.map((e) => `  - ${e.id}: ${e.title}`));
  const features = state.features.filter(shown);
  if (features.length) lines.push("- `not_features`: regions of detected features that aren't really holes/posts:", ...features.map((f) => `  - ${f.region}: Ø${f.diameter.toFixed(1)} mm ${FEATURE_NAME[f.type]}`));
  return lines.join("\n") + "\n";
}

/** The JSON object in a pasted reply: a ```json block, or the last {...} that parses. */
function parseAnswer(text) {
  const fenced = [...text.matchAll(/```(?:json)?\s*([\s\S]*?)```/g)].map((m) => m[1]);
  const candidates = [...fenced.reverse(), ...[...text.matchAll(/\{[\s\S]*\}/g)].map((m) => m[0])];
  for (const c of candidates) {
    try {
      const v = JSON.parse(c);
      if (v && typeof v === "object" && !Array.isArray(v)) return v;
    } catch {
      // not this one
    }
  }
  throw new Error("No JSON answer found — ask the AI to end its reply with the JSON block from the report.");
}

async function applyAnswer(answer) {
  const done = [];
  const step = async (what, fn) => {
    try {
      done.push({ text: `${what}: ${(await fn()) ?? "done"}` });
    } catch (err) {
      done.push({ text: `${what}: ${err.message ?? err}`, error: true });
    }
  };
  if (answer.orientation != null) {
    await step("Orientation", async () => (await aiActions.select_orientation({ index: Number(answer.orientation) }), state.options[state.active].label));
  }
  for (const region of answer.not_features ?? []) {
    await step(`Feature ${region}`, async () => (await aiActions.tag_feature({ region: Number(region), tag: "none" }), "marked as not a feature"));
  }
  if (answer.settings && Object.keys(answer.settings).length) {
    const settings = Object.fromEntries(Object.entries(answer.settings).map(([k, v]) => [k, String(v)]));
    await step("Slicer settings", async () => (await aiActions.set_slicer_settings({ settings }), Object.entries(settings).map(([k, v]) => `${k} = ${v}`).join(", ")));
  }
  if (answer.design_changes?.length) {
    await step("Design changes", async () => (await aiActions.apply_design_changes({ ids: answer.design_changes.map(String) }), answer.design_changes.join(", ")));
  }
  if (!done.length) done.push({ text: "The answer suggests no changes." });
  return done;
}

$("paste-apply").onclick = async () => {
  let rows;
  try {
    rows = await applyAnswer(parseAnswer($("paste-input").value));
  } catch (err) {
    rows = [{ text: err.message, error: true }];
  }
  $("paste-result").replaceChildren(...rows.map((r) => el("li", { className: r.error ? "error" : "", textContent: r.text })));
};

// Offline after the first visit (see sw.js). Not on file:// pages, where service workers don't run.
if ("serviceWorker" in navigator && location.protocol.startsWith("http")) navigator.serviceWorker.register("sw.js");
