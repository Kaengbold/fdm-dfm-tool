import { Assistant, MODELS } from "./assistant.js";
import { GeminiAssistant, listGeminiModels } from "./gemini.js";
import { listOpenAIModels, OpenAIAssistant } from "./openai.js";
import { applyEdits as applyDesignEdits, stlBytes } from "./geometry.js";
import { readStep } from "./step.js";
import { stepWithEdits } from "./stepwrite.js";
import { MATERIAL_FIELDS, PRINTER_FIELDS, fromOrcaFilament, loadMine, saveMine, toForm, toSpec, toYaml, warpLength, zFactor } from "./mypresets.js";
import { REGION_COLORS, Viewer, FIELD_BANDS, fieldColor } from "./viewer.js";

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
  if (src || Object.keys(mine.printers).length || Object.keys(mine.materials).length) {
    restoring = engineReady.then(async () => {
      await post("custom_presets", mine);
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
  fitNotes: {}, // region -> {partner, fit, text}: what goes into a hole / onto a post (set_fit)
  edits: [], // suggested design edits: {id, title, description, op: "add"|"cut", mesh: Float32Array, pieces, selected}
  features: [], // detected holes/posts from the last design suggestion
};

// Debug/test hook: lets automated browser tests and devtools inspect the app.
globalThis.fdm = { state, viewer, call: (...args) => call(...args) };

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

/** The "working" badge over the 3D view: what runs, for how long, and Cancel — visible wherever the sidebar is scrolled. */
function showWorking(text) {
  $("working-text").textContent = text;
  if (workingClock) return;
  const start = performance.now();
  const tick = () => ($("working-time").textContent = `${Math.round((performance.now() - start) / 1000)} s`);
  tick();
  workingClock = setInterval(tick, 1000);
  $("working").hidden = false;
}

function hideWorking() {
  clearInterval(workingClock);
  workingClock = 0;
  $("working").hidden = true;
}

async function busy(text, fn) {
  setStatus(text);
  document.body.dataset.busy = ++busyCount;
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
    document.body.dataset.busy = --busyCount;
    document.body.dataset.done = ++doneCount;
    if (!busyCount) {
      clearTimeout(progressTimer);
      $("progress").hidden = true;
      hideWorking();
    }
  }
}

/** ``fraction`` 0..1 from the engine, or null for "working, amount unknown". */
function showProgress(fraction) {
  const bar = $("progress-bar");
  if (fraction == null) bar.removeAttribute("value");
  else bar.value = fraction;
}

// ---------- flow ----------
/** Entry point for user files: STEP goes through OpenCascade first; STL and 3MF straight to the engine. */
async function openFile(name, bytes) {
  $("body-pick").hidden = true;
  if (!/\.(step|stp)$/i.test(name)) return loadFile(name, bytes);
  const bodies = await busy("Reading STEP file (first time downloads ~8 MB)…", () => readStep(bytes));
  if (!bodies.length) return setStatus("No solid bodies found in this STEP file.", true);
  const pick = async (i) => {
    // Kept so design edits can be written back into the original CAD solid (stepwrite.js).
    const step = { name, bytes, volume: bodies[i].volume, edits: [] };
    await loadFile(`${name.replace(/\.(step|stp)$/i, "")}${bodies.length > 1 ? `_${i + 1}` : ""}.stl`, stlBytes(bodies[i].soup), bodies[i].faceIds, step);
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

/** ``step``: the STEP file this part came from (and the edits applied so far), or null. */
async function loadFile(name, bytes, faceIds = null, step = null) {
  await busy("Reading part…", async () => {
    await engineReady;
    const info = await call("load", { name, face_ids: faceIds ? Array.from(faceIds) : undefined }, bytes);
    state.step = step;
    $("dl-step").hidden = !step?.edits.length;
    state.source = { name, bytes, faceIds, body: 0, scale: 1 }; // to restore the part after a cancel
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
  $("results").hidden = true;
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
  });
}

async function selectOption(i) {
  state.active = i;
  [...$("options").children].forEach((li, k) => li.classList.toggle("active", k === i));
  state.placed = viewer.showPart(state.triangles, activeOption().rotation);
  viewer.clearMarkers(); // remove old orientation's warning boxes immediately
  state.strength = null;
  state.showHeat = false;
  $("strength-result").replaceChildren();
  renderGcode();
  setZones([]); // zones belong to one orientation
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
    $("results").hidden = false;
  });
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
    fixes.push(el("li", { className: "hint-fix", textContent: `+ More fixes in ${hiddenLabels} (switch "What can you change?" to see them)` }));
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
        el("span", { className: "sev", textContent: SEVERITY_LABEL[worst] }), " ",
        first.title, items.length > 1 ? ` ×${items.length}` : "",
        first.goal_note ? el("small", { className: "goal-tag", textContent: ` (${first.goal_note})` }) : ""
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
  $("stats").textContent = `≈${fmtTime(stats.print_time_min)} · ≈${Math.round(stats.filament_g)} g`;
  $("findings").replaceChildren(
    ...(findings.length ? renderImpactGroups(findings) : [el("li", { className: "ok", textContent: "Should print as designed in this orientation." })]),
  );
  viewer.showFindings(findings);
  renderSettings();
  recolor();
}
$("show-temp").onchange = () => {
  if ($("show-temp").checked) state.showHeat = false;
  recolor();
};

/** Recommended settings with the user's applied strength suggestions on top. */
function effectiveSettings() {
  if (!state.check) return [];
  const list = state.check.settings.map((s) => (s.key in state.overrides ? { ...s, value: state.overrides[s.key], changed: true } : s));
  for (const [key, value] of Object.entries(state.overrides)) {
    if (!list.some((s) => s.key === key)) list.push({ key, value, label: key, reason: "From strength check", changed: true });
  }
  return list;
}

function renderSettings() {
  const settings = effectiveSettings();
  $("settings-block").hidden = !settings.length;
  $("settings").replaceChildren(
    ...settings.map((s) =>
      el("tr", { title: s.reason }, el("td", { textContent: s.label + (s.changed ? " ★" : "") }), el("td", { textContent: s.value })),
    ),
  );
  renderShells();
}

function fmtTime(min) {
  return min < 60 ? `${Math.round(min)} min` : `${Math.floor(min / 60)} h ${Math.round(min % 60)} min`;
}

// ---------- strength: picking faces and loads ----------
function recolor() {
  const option = activeOption();
  if (!option) return;
  viewer.showLoadSymbols(loadSymbols());
  if (strengthView()) return;
  if ($("show-temp").checked && state.check?.face_temp) return viewer.colorByHeat(state.check.face_temp);
  const roles = new Map([...state.marks.map((m) => [m.region, m.kind]), ...state.loads.filter((l) => !l.point).map((l) => [l.region, l.kind])]);
  const spot = $("at-spot").checked && !$("pick-panel").hidden && state.pickedPoint;
  viewer.colorByClass(option.face_classes, (f) => {
    if (!state.faceRegions) return null;
    const r = state.faceRegions[f];
    if (r === state.selectedRegion && !spot) return REGION_COLORS.selected;
    return roles.has(r) ? REGION_COLORS[roles.get(r)] : null;
  });
  const spots = state.loads.filter((l) => l.point).map((l) => ({ point: l.point, radius: l.radius, color: REGION_COLORS[l.kind] }));
  if (spot) spots.push({ point: state.pickedPoint, radius: SPOT_RADIUS, color: REGION_COLORS.selected });
  viewer.showSpots(spots);
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

const HOLD_TEXT = { clamped: "🔒 Clamped / glued here", resting: "🔒 Resting here (only pushes back)", screw: "🔒 Screw / bolt here" };
const loadText = (l) => (l.kind === "fixed" ? HOLD_TEXT[l.hold ?? "clamped"] : `➜ ${multiCase() ? `Case ${l.case}: ` : ""}${l.kg} kg ${dirLabel(l)}`) + (l.point ? " (at a spot)" : "");

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

async function runStrength(fine = false) {
  const before = state.strength;
  return busy(fine ? "Simulating again with a finer grid (up to a minute)…" : "Simulating the load…", async () => {
    const r = await call("strength", { up: activeOption().up, loads: state.loads, profile: profile(), ...currentShells(), fine, detailed: useGcode(), zones: state.zones });
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
    $("show-temp").checked = false;
    recolor();
    viewer.showFindings([]);
    if (r.weak_marker) viewer.markWeak(r.weak_marker.point, r.weak_marker.normal);
    else viewer.mark(r.critical_point_print);
    renderStrength();
    setStatus(state.summary);
    return r;
  });
}
$("run-strength").onclick = () => runStrength();

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
  if (state.showWarp && state.warp) return warpView();
  const r = state.strength;
  if (!(state.showHeat && r)) {
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
  const cubes = f.elements ? f32(els[f.elements]) : new Float32Array(els.centers.length / 12); // displacement: inside not shown
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
  viewer.showFindings([]);
  for (const p of w.lift_points_print) viewer.mark(p);
  return true;
}

$("run-warp").onclick = () =>
  busy("Simulating the print layer by layer (up to a minute)…", async () => {
    const w = await call("warping", { up: activeOption().up, profile: profile() });
    state.warp = w;
    state.showWarp = true;
    state.showHeat = false;
    $("show-temp").checked = false;
    const show = el("input", { type: "checkbox", checked: true });
    show.onchange = () => {
      state.showWarp = show.checked;
      if (!show.checked) viewer.showFindings(state.check?.findings ?? []);
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
    from("expert", el("p", { className: "hint", textContent: `Analysis: ${m.voxels.toLocaleString()} cubes of ${m.voxel_mm.toFixed(1)} mm · total load ${Math.round(m.load_n)} N · bends up to ${r.max_displacement_mm.toFixed(2)} mm${r.solver_converged ? "" : " · solver did not fully converge"}` })),
    r.accuracy ? el("p", { className: "hint", textContent: r.accuracy }) : "",
    r.fine ? "" : from("expert", el("button", { textContent: "Check accuracy with a finer grid (slower)", onclick: () => runStrength(true),
      title: "Runs again with 3× more cubes. If the answer barely changes, you can trust it (the usual check in FEA software)." })));
}

function renderStrength() {
  const r = state.strength;
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
    el("button", { textContent: "Re-rank orientations including strength", onclick: () => findOrientations(true) }),
    el("ul", { id: "strength-options", className: "options" }),
  );
}

async function showStrengthOptions() {
  return busy("Trying alternatives (a few seconds)…", async () => {
    const opts = await call("strength_options", { up: activeOption().up, loads: state.loads, profile: profile(), ...currentShells() });
    const KIND = { slicer: "Slicer setting", orientation: "Orientation", design: "Design change", material: "Material" };
    const ACTION = { slicer: "Apply", orientation: "Use it", design: "Show me", material: "Use it" };
    const now = state.strength?.safety_factor;
    $("strength-options").replaceChildren(
      ...opts.map((o) =>
        el("li", { className: `option ${o.kind}` },
          el("div", { className: "option-head" },
            el("small", { textContent: KIND[o.kind] }),
            el("strong", { textContent: o.change }),
            el("span", { className: "gain", textContent: o.safety_factor == null ? "re-check after"
              : `×${o.safety_factor.toFixed(1)}${now ? ` (${o.safety_factor >= now ? "+" : ""}${Math.round((o.safety_factor / now - 1) * 100)} %)` : ""}` })),
          el("p", { className: "hint", textContent: o.why + (o.cost ? ` Cost: ${o.cost}.` : "") }),
          el("button", { textContent: ACTION[o.kind], onclick: () => applyOption(o) }))),
      opts.some((o) => o.kind === "material") ? ""
        : el("li", { className: "hint", textContent: `No other material is much stronger than ${state.strength?.material ?? "this one"}; others help with heat or impact instead.` }),
    );
    setStatus(state.summary);
    return opts;
  });
}

async function runOptimise() {
  return busy("Trying settings to find the lightest print that holds (up to a minute or two)…", async () => {
    const o = await call("optimise_strength", { up: activeOption().up, loads: state.loads, profile: profile(), zones: true });
    const use = el("button", { className: "primary", textContent: "Use these settings", onclick: async () => {
      setZones([]);
      Object.assign(state.overrides, o.settings);
      renderSettings();
      if (o.orientation === "strongest") await applyOption({ up: o.up });
      else runStrength();
    } });
    const z = o.zoned;
    const zoned = z?.met && z.zones?.length
      ? el("div", { className: "verdict fine" },
        el("strong", { textContent: "Or: stronger only where it's needed" }),
        el("p", { textContent: z.explanation }),
        el("p", { className: "hint", textContent: "The zones go into the 3MF as modifier volumes: the slicer prints them with more walls and infill (orange boxes)." }),
        el("button", { textContent: "Use the zones", onclick: async () => {
          Object.assign(state.overrides, z.settings);
          renderSettings();
          if (o.orientation === "strongest") await applyOption({ up: z.up }); // clears zones: set them after
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
      zoned,
    );
    setStatus(state.summary);
    return o;
  });
}
$("run-optimise").onclick = runOptimise;

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
  viewer.showZones(zones.map((z) => z.corners_print));
  $("zones-for").hidden = !zones.length;
}

async function applyOption(o) {
  if (o.settings) {
    Object.assign(state.overrides, o.settings);
    renderSettings();
    $("run-strength").click();
  } else if (o.up) {
    const option = await call("orientation", { up: o.up, profile: profile(), label: "Strongest" });
    option.reasons.push("Strongest for your load");
    state.options.unshift(option);
    renderOptions();
    await selectOption(0);
  } else if (o.material) {
    $("material").value = o.material;
    $("material").dispatchEvent(new Event("change"));
  } else if (o.kind === "design") {
    $("design-on").click();
    await suggestDesign();
  }
}

// ---------- design changes ----------
function clearEdits() {
  state.edits = [];
  $("edits").replaceChildren();
  $("apply-edits").hidden = true;
  viewer.showGhosts([]);
}

function showSelectedGhosts() {
  viewer.showGhosts(state.edits.filter((e) => e.selected));
  $("apply-edits").hidden = !state.edits.some((e) => e.selected);
}

async function suggestDesign() {
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
            return el("li", {}, el("label", {}, box, el("span", {}, e.title, el("small", { textContent: e.description }))));
          })
        : [el("li", { className: "hint", textContent: "Nothing to change for this orientation and material." })]),
    );
    showSelectedGhosts();
    setStatus(state.summary);
    return r;
  });
}
$("suggest-design").onclick = suggestDesign;

// ---------- feature confirm ("is this a hole?") ----------
const shown = (f) => f.type === "hole" || f.full_circle; // partial bosses are rounded edges: not worth a line
const FEATURE_NAME = { hole: "hole", boss: "round post" };

function renderFeatures(features) {
  const list = features.filter(shown);
  const ask = list.filter((f) => f.confidence === "low" && !f.tag).length;
  $("features-summary").textContent = !list.length
    ? "No round holes or posts found."
    : `Found ${list.length} round feature(s).${ask ? ` Please check the ${ask} marked "?" — click a name to see it on the part.` : " Click a name to see it on the part."}`;
  $("features").replaceChildren(
    ...list.map((f) => {
      const sideways = Math.abs(f.axis[2]) < 0.3 ? "lying" : Math.abs(f.axis[2]) > 0.95 ? "upright" : "slanted";
      const name = el("button", {
        className: "link",
        textContent: `${f.confidence === "low" && !f.tag ? "? " : ""}Ø${f.diameter.toFixed(1)} mm ${FEATURE_NAME[f.type]} (${sideways} in the model)`,
        onclick: () => showFeature(f),
      });
      const answer = f.tag
        ? [el("span", { className: "hint", textContent: f.tag === "none" ? `ignored — not a ${FEATURE_NAME[f.type]}` : "confirmed" }),
           el("button", { textContent: "Undo", onclick: () => tagFeature(f.region, null) })]
        : [el("button", { textContent: "Yes", title: `It is a ${FEATURE_NAME[f.type]}`, onclick: () => tagFeature(f.region, f.type) }),
           el("button", { textContent: `Not a ${FEATURE_NAME[f.type]}`, onclick: () => tagFeature(f.region, "none") })];
      return el("li", { className: [f.tag === "none" ? "rejected" : "", f.confidence === "low" && !f.tag ? "ask" : ""].join(" ") }, name, ...answer,
        f.tag !== "none" && f.full_circle ? fitControls(f) : "");
    }),
  );
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
      const r = await call("set_fit", { region: f.region, up: activeOption().up, partner_mm: Number(size.value), fit: kind.value });
      state.fitNotes = { ...state.fitNotes, [f.region]: { partner: Number(size.value), fit: kind.value, text: r.explanation } };
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

async function applyEdits() {
  return busy("Applying design changes…", async () => {
    const chosen = state.edits.filter((e) => e.selected);
    const soup = await applyDesignEdits(state.triangles, chosen);
    const name = state.name.replace(/(_dfm)?\.stl$/i, "") + "_dfm.stl";
    const step = state.step && { ...state.step, edits: [...state.step.edits, ...chosen] };
    await loadFile(name, stlBytes(soup), null, step);
    setStatus(`${state.summary} — design changes applied. Download the ${step ? "STEP" : "STL"} to bring them back into your CAD.`);
  });
}
$("apply-edits").onclick = applyEdits;

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
    return { met: o.met, target: o.target, safety_factor: round(o.safety_factor), grams: o.grams, orientation: o.orientation, settings: o.settings, explanation: o.explanation };
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

// ---------- presets ----------
async function initPresets() {
  await engineReady;
  await applyMine(loadMine(), { printer: "bambu_p1s", material: "pla", nozzle: "0.4" });
  setStatus("Ready — drop an STL, 3MF or STEP file, or try a sample.");
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
          el("td", { textContent: `≈${fmtTime(r.print_time_min)}` }),
          el("td", { textContent: r.issues.length ? [...new Set(r.issues.map((i) => i.title))].join("; ") : "none" }),
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
  d.open = true;
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

$("cancel").onclick = cancelEngine;
$("working-cancel").onclick = cancelEngine;

// ---------- levels and the start guide ----------
const LEVELS = ["simple", "advanced", "expert"];
function setLevel(level) {
  if (!LEVELS.includes(level)) return;
  document.body.dataset.level = level;
  document.querySelector(`input[name=level][value=${level}]`).checked = true;
  localStorage.setItem("fdm-level", level);
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
  settings: { level: "simple", mode: "print", next: "Drop your part (or try a sample): you get the best orientation, what could go wrong, and slicer settings." },
  strength: { level: "simple", mode: "print", next: "Drop your part, then click the face where it's held and the face where the load is (Strength). The tool finds what holds." },
  steps: { level: "advanced", mode: "both", next: "Drop your part. Then go down the list: orientation, problems, strength, design changes, slicer settings." },
  expert: { level: "expert", mode: "both", next: "Everything is shown. Drop a part to start." },
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
$("show-guide").onclick = () => $("guide").showModal();
if (!localStorage.getItem("fdm-guided") && !new URLSearchParams(location.search).has("level")) $("guide").showModal();

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
    const bytes = await call("export", { up: activeOption().up, profile: profile(), settings, zones: state.zones, slicer: $("dl-slicer").value });
    download(bytes, `${baseName()}.3mf`, "model/3mf");
    setStatus(`${state.summary} — 3MF downloaded with per-object slicer settings.`);
  });
};

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
  setStatus("Report copied — paste it into ChatGPT, Claude or Gemini, then paste the reply under \"Paste an AI answer\".");
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
