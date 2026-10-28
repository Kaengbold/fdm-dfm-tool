import { Assistant, MODELS } from "./assistant.js";
import { GeminiAssistant, listGeminiModels } from "./gemini.js";
import { listOpenAIModels, OpenAIAssistant } from "./openai.js";
import { stlBytes, subtract } from "./geometry.js";
import { readStep } from "./step.js";
import { REGION_COLORS, Viewer } from "./viewer.js";

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
};

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
  if (src) {
    restoring = engineReady.then(async () => {
      await post("load", { name: src.name, face_ids: src.faceIds ? Array.from(src.faceIds) : undefined }, src.bytes);
      if (src.body) await post("select_body", { index: src.body });
      if (src.scale !== 1) await post("scale", { factor: src.scale });
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
  strength: null,
  showHeat: false,
  overrides: {}, // slicer settings the user applied from strength suggestions
  edits: [], // suggested design edits: {id, title, description, cutter: Float32Array, selected}
};

// Debug/test hook: lets automated browser tests and devtools inspect the app.
globalThis.fdm = { state, viewer, call: (...args) => call(...args) };

const profile = () => ({
  printer: $("printer").value,
  material: $("material").value,
  nozzle: $("nozzle").value,
  multi_material: $("multi-material").checked,
  goal: document.querySelector("input[name=goal]:checked").value,
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

async function busy(text, fn) {
  setStatus(text);
  document.body.dataset.busy = ++busyCount;
  // Only show the bar for work that takes a moment; quick checks would just flicker.
  if (busyCount === 1) progressTimer = setTimeout(() => ($("progress").hidden = false), 400);
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
  const pick = async (i) => loadFile(`${name.replace(/\.(step|stp)$/i, "")}${bodies.length > 1 ? `_${i + 1}` : ""}.stl`, stlBytes(bodies[i].soup), bodies[i].faceIds);
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

async function loadFile(name, bytes, faceIds = null) {
  await busy("Reading part…", async () => {
    await engineReady;
    const info = await call("load", { name, face_ids: faceIds ? Array.from(faceIds) : undefined }, bytes);
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
  Object.assign(state, { faceRegions: null, regionInfo: [], selectedRegion: -1, loads: [], strength: null, overrides: {} });
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
    await partLoaded(next);
  });
}

async function findOrientations(withLoads = false) {
  const loads = withLoads && readyForStrength() ? state.loads : undefined;
  await busy(loads ? "Finding the best orientation for printing and strength…" : "Finding the best orientation…", async () => {
    const r = await call("orientations", { profile: profile(), loads });
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
  $("nozzle-block").open = false; // orientation-specific
  clearEdits();
  recolor();
  await runCheck();
}

let checkToken = 0;

async function runCheck() {
  if (!activeOption()) return;
  // Only the latest request may update the UI: clicks during a running check would otherwise
  // let an older result (or an older orientation's result) overwrite the newer one.
  const token = ++checkToken;
  await busy("Checking printability…", async () => {
    const result = await call("check", { up: activeOption().up, profile: profile(), mode: mode() });
    if (token !== checkToken) return;
    if (!result?.findings) throw new Error("The analysis returned no result — please try again");
    state.check = result;
    renderCheck();
    $("design-block").hidden = mode() === "print";
    $("ai-block").hidden = false;
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
  ["fails", "Will fail"],
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
}

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
}

function fmtTime(min) {
  return min < 60 ? `${Math.round(min)} min` : `${Math.floor(min / 60)} h ${Math.round(min % 60)} min`;
}

// ---------- strength: picking faces and loads ----------
function recolor() {
  const option = activeOption();
  if (!option) return;
  if (state.showHeat && state.strength) return viewer.colorByHeat(state.strength.face_heat);
  const roles = new Map(state.loads.map((l) => [l.region, l.kind]));
  viewer.colorByClass(option.face_classes, (f) => {
    if (!state.faceRegions) return null;
    const r = state.faceRegions[f];
    if (r === state.selectedRegion) return REGION_COLORS.selected;
    return roles.has(r) ? REGION_COLORS[roles.get(r)] : null;
  });
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
  const face = viewer.pick(e);
  if (face < 0) return;
  await ensureRegions();
  state.selectedRegion = state.faceRegions[face];
  const info = state.regionInfo[state.selectedRegion];
  $("pick-info").textContent = `${info.kind} face, ${Math.round(info.area_mm2)} mm²`;
  $("pick-panel").hidden = false;
  state.showHeat = false;
  recolor();
});

function addLoad(load) {
  state.loads = state.loads.filter((l) => l.region !== load.region).concat(load);
  state.selectedRegion = -1;
  $("pick-panel").hidden = true;
  renderLoads();
  recolor();
}

$("as-fixed").onclick = () => addLoad({ region: state.selectedRegion, kind: "fixed" });
$("as-load").onclick = () =>
  addLoad({ region: state.selectedRegion, kind: "force", kg: Number($("kg").value), direction: $("dir").value });

const DIR_LABEL = { push: "pushing", pull: "pulling", down: "hanging down" };
const readyForStrength = () => state.loads.some((l) => l.kind === "fixed") && state.loads.some((l) => l.kind === "force");

function renderLoads() {
  $("loads").replaceChildren(
    ...state.loads.map((l, i) =>
      el(
        "li",
        { className: l.kind },
        l.kind === "fixed" ? "Held here" : `${l.kg} kg ${DIR_LABEL[l.direction]}`,
        el("button", {
          textContent: "×",
          title: "Remove",
          onclick: () => {
            state.loads.splice(i, 1);
            renderLoads();
            recolor();
          },
        }),
      ),
    ),
  );
  $("run-strength").disabled = !readyForStrength();
  $("pick-hint").hidden = state.loads.length > 1;
}

function currentWallsInfill() {
  const s = Object.fromEntries(effectiveSettings().map((x) => [x.key, x.value]));
  return { walls: Number(s.wall_loops ?? 2), infill: parseFloat(s.sparse_infill_density ?? "15") / 100 };
}

const MODE_TEXT = {
  layers: "The layers would separate first. Orientation matters most here — try the strongest orientation.",
  material: "The plastic itself is the limit. More walls or infill, or a thicker design, help most.",
};

async function runStrength() {
  return busy("Simulating the load…", async () => {
    const { walls, infill } = currentWallsInfill();
    const r = await call("strength", { up: activeOption().up, loads: state.loads, profile: profile(), walls, infill });
    state.strength = r;
    state.showHeat = true;
    recolor();
    viewer.showFindings([]);
    viewer.mark(r.critical_point_print);
    renderStrength();
    setStatus(state.summary);
    return r;
  });
}
$("run-strength").onclick = runStrength;

function renderStrength() {
  const r = state.strength;
  const sf = r.safety_factor;
  const heat = el("input", { type: "checkbox", checked: state.showHeat });
  heat.onchange = () => {
    state.showHeat = heat.checked;
    recolor();
  };
  $("strength-result").replaceChildren(
    el(
      "div",
      { className: `verdict ${r.verdict.replaceAll(" ", "-")}` },
      el("strong", { textContent: `${r.verdict[0].toUpperCase() + r.verdict.slice(1)} — safety ×${sf.toFixed(1)}` }),
      el("p", { textContent: MODE_TEXT[r.mode] }),
      el("p", { textContent: `${r.material}, ${r.walls} walls, ${Math.round(r.infill * 100)}% infill · bends about ${r.max_displacement_mm.toFixed(1)} mm` }),
    ),
    el("label", { className: "toggle" }, heat, "Show stress colours (red dot = weakest spot)"),
    el("p", { className: "hint", textContent: "Rough estimate. Aim for ×2 or more for anything that matters." }),
    el("button", { textContent: "How can I make it stronger?", onclick: showStrengthOptions }),
    el("button", { textContent: "Re-rank orientations including strength", onclick: () => findOrientations(true) }),
    el("ul", { id: "strength-options", className: "options" }),
  );
}

async function showStrengthOptions() {
  return busy("Trying alternatives (a few seconds)…", async () => {
    const { walls, infill } = currentWallsInfill();
    const opts = await call("strength_options", { up: activeOption().up, loads: state.loads, profile: profile(), walls, infill });
    $("strength-options").replaceChildren(
      ...opts.map((o) =>
        el(
          "li",
          {},
          `${o.change}: ×${o.safety_factor.toFixed(1)}`,
          el("button", { textContent: "Apply", onclick: () => applyOption(o) }),
        ),
      ),
    );
    setStatus(state.summary);
    return opts;
  });
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
  viewer.showGhosts(state.edits.filter((e) => e.selected).map((e) => e.cutter));
  $("apply-edits").hidden = !state.edits.some((e) => e.selected);
}

async function suggestDesign() {
  return busy("Looking for design improvements…", async () => {
    const r = await call("design", { up: activeOption().up, profile: profile(), params: designParams() });
    for (const [k, range] of Object.entries(r.param_ranges)) Object.assign($(`param-${k}`), { ...range, value: r.params[k] });
    $("edit-params").hidden = !r.edits.length;
    const holes = r.features.filter((f) => f.type === "hole");
    $("features-summary").textContent = holes.length
      ? `Found ${holes.length} hole(s): ${holes.map((h) => `Ø${h.diameter.toFixed(1)} mm`).join(", ")}.`
      : "No round holes found.";
    state.edits = r.edits.map((e) => ({ ...e, cutter: new Float32Array(e.cutter.slice().buffer), selected: true }));
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

/** Edit sizes from the inputs; empty until the first suggestion filled them in (engine defaults). */
const designParams = () =>
  Object.fromEntries([...document.querySelectorAll("#edit-params input")].filter((i) => i.value !== "").map((i) => [i.dataset.param, Number(i.value)]));
document.querySelectorAll("#edit-params input").forEach((i) => (i.onchange = suggestDesign));

async function applyEdits() {
  return busy("Applying design changes…", async () => {
    const cutters = state.edits.filter((e) => e.selected).map((e) => e.cutter);
    const soup = await subtract(state.triangles, cutters);
    const name = state.name.replace(/(_dfm)?\.stl$/i, "") + "_dfm.stl";
    await loadFile(name, stlBytes(soup));
    setStatus(`${state.summary} — design changes applied. Download the STL to bring them back into your CAD.`);
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
    for (const l of loads) if (!state.regionInfo[l.region]) throw new Error(`Unknown face ${l.region}`);
    state.loads = loads.map((l) =>
      l.kind === "fixed" ? { region: l.region, kind: "fixed" } : { region: l.region, kind: "force", kg: l.kg ?? 1, direction: l.direction ?? "push" },
    );
    renderLoads();
    recolor();
    return { loads: state.loads };
  },
  check_strength: async () => {
    if (!readyForStrength()) throw new Error("Set loads first: at least one fixed face and one force");
    const r = await runStrength();
    return { safety_factor: round(r.safety_factor), verdict: r.verdict, mode: r.mode, bends_mm: round(r.max_displacement_mm, 1), walls: r.walls, infill: r.infill };
  },
  strength_options: async () => {
    if (!state.strength) throw new Error("Run check_strength first");
    return (await showStrengthOptions()).map((o) => ({ ...o, safety_factor: round(o.safety_factor) }));
  },
  suggest_design_changes: async () => {
    const r = await suggestDesign();
    return {
      features: r.features.map((f) => ({ type: f.type, diameter_mm: round(f.diameter, 1), axis: round(f.axis), full_circle: f.full_circle })),
      edits: r.edits.map((e) => ({ id: e.id, title: e.title, description: e.description })),
    };
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
  suggest_design_changes: "looking for design changes",
  apply_design_changes: "applying design changes",
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
  const p = await call("presets");
  state.presets = p;
  const fill = (sel, entries, value) =>
    $(sel).replaceChildren(...entries.map(([k, label]) => el("option", { value: k, textContent: label, selected: k === value })));
  fill("printer", Object.entries(p.printers).map(([k, v]) => [k, v.name]), "bambu_p1s");
  fill("material", Object.entries(p.materials).map(([k, v]) => [k, v.name]), "pla");
  fill("nozzle", Object.keys(p.nozzles).map((k) => [k, `${k} mm`]), "0.4");
  viewer.setBed(p.printers[$("printer").value].bed);
  syncMultiMaterial();
  setStatus("Ready — drop an STL, 3MF or STEP file, or try a sample.");
}
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
}

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
    const bytes = await call("export", { up: activeOption().up, profile: profile(), settings });
    download(bytes, `${baseName()}.3mf`, "model/3mf");
    setStatus(`${state.summary} — 3MF downloaded with per-object slicer settings.`);
  });
};

$("dl-stl").onclick = () => download(stlBytes(state.placed), `${baseName()}_oriented.stl`, "model/stl");
$("dl-stl-model").onclick = () => download(stlBytes(state.triangles), `${baseName()}.stl`, "model/stl");

$("copy-report").onclick = async () => {
  const md = await call("report", { up: activeOption().up, profile: profile(), mode: mode() });
  await navigator.clipboard.writeText(md);
  setStatus("Report copied — paste it anywhere, e.g. into ChatGPT or Claude to ask follow-up questions.");
};
