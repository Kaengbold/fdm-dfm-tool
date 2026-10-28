import { stlBytes, subtract } from "./geometry.js";
import { REGION_COLORS, Viewer } from "./viewer.js";

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
};

// ---------- engine (Pyodide worker) ----------
const worker = new Worker(new URL("worker.js", import.meta.url), { type: "module" });
const pending = new Map();
let nextId = 0;

function call(method, args = {}, data = null) {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    worker.postMessage({ id, method, args, data });
  });
}

const engineReady = new Promise((resolve, reject) => {
  worker.addEventListener("message", ({ data }) => {
    if (data.type === "ready") resolve();
    else if (data.type === "boot-error") reject(new Error(data.error));
    else if (pending.has(data.id)) {
      const { resolve: ok, reject: fail } = pending.get(data.id);
      pending.delete(data.id);
      data.error ? fail(new Error(data.error)) : ok(data.result);
    }
  });
});

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

const profile = () => ({ printer: $("printer").value, material: $("material").value, nozzle: $("nozzle").value });
const mode = () => document.querySelector("input[name=mode]:checked").value;
const activeOption = () => state.options[state.active];

function setStatus(text, isError = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", isError);
}

async function busy(text, fn) {
  setStatus(text);
  try {
    return await fn();
  } catch (err) {
    setStatus(err.message, true);
    throw err;
  }
}

// ---------- flow ----------
async function loadFile(name, bytes) {
  await busy("Reading part…", async () => {
    await engineReady;
    const info = await call("load", { name }, bytes);
    state.name = name;
    // Copy: bytes from Pyodide may not be 4-byte aligned for a Float32Array view.
    state.triangles = new Float32Array(info.triangles.slice().buffer);
    Object.assign(state, { faceRegions: null, regionInfo: [], selectedRegion: -1, loads: [], strength: null, overrides: {} });
    viewer.framed = false;
    const [x, y, z] = info.size_mm.map((v) => v.toFixed(1));
    state.summary = `${name}: ${x} × ${y} × ${z} mm`;
    renderLoads();
    await findOrientations();
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
  state.strength = null;
  state.showHeat = false;
  $("strength-result").replaceChildren();
  clearEdits();
  recolor();
  await runCheck();
}

async function runCheck() {
  await busy("Checking printability…", async () => {
    state.check = await call("check", { up: activeOption().up, profile: profile(), mode: mode() });
    renderCheck();
    $("design-block").hidden = mode() === "print";
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
      ),
    ),
  );
}

const SEVERITY_LABEL = { problem: "Problem", warn: "Warning", info: "Tip" };
const DOMAIN_LABEL = { geometry: "Design", slicer: "Slicer", process: "Printer & material" };

function renderCheck() {
  const { findings, stats } = state.check;
  $("stats").textContent = `≈${fmtTime(stats.print_time_min)} · ≈${Math.round(stats.filament_g)} g`;
  $("findings").replaceChildren(
    ...(findings.length
      ? findings.map((f, i) => {
          const fixes = f.fixes.map((fx) =>
            el("li", {}, el("span", { className: `chip ${fx.domain}`, textContent: DOMAIN_LABEL[fx.domain] }), " ", fx.text),
          );
          return el(
            "li",
            { className: f.severity },
            el(
              "details",
              { ontoggle: (e) => e.target.open && viewer.showFindings(findings, i) },
              el("summary", {}, el("span", { className: "sev", textContent: SEVERITY_LABEL[f.severity] }), " ", f.title),
              el("p", { className: "detail", textContent: f.detail }),
              el("p", { className: "why", textContent: f.why }),
              fixes.length ? el("ul", { className: "fixes" }, ...fixes) : el("p", { className: "why", textContent: "No fixes for this mode." }),
            ),
          );
        })
      : [el("li", { className: "ok", textContent: "No issues found in this orientation." })]),
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

$("run-strength").onclick = async () => {
  await busy("Simulating the load…", async () => {
    const { walls, infill } = currentWallsInfill();
    const r = await call("strength", { up: activeOption().up, loads: state.loads, profile: profile(), walls, infill });
    state.strength = r;
    state.showHeat = true;
    recolor();
    viewer.showFindings([]);
    viewer.mark(r.critical_point_print);
    renderStrength();
    setStatus(state.summary);
  });
};

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
  await busy("Trying alternatives (a few seconds)…", async () => {
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

$("suggest-design").onclick = async () => {
  await busy("Looking for design improvements…", async () => {
    const r = await call("design", { up: activeOption().up, profile: profile() });
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
  });
};

$("apply-edits").onclick = async () => {
  await busy("Applying design changes…", async () => {
    const cutters = state.edits.filter((e) => e.selected).map((e) => e.cutter);
    const soup = await subtract(state.triangles, cutters);
    const name = state.name.replace(/(_dfm)?\.stl$/i, "") + "_dfm.stl";
    await loadFile(name, stlBytes(soup));
    setStatus(`${state.summary} — design changes applied. Download the STL to bring them back into your CAD.`);
  });
};

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
  setStatus("Ready — drop an STL or try a sample.");
}
initPresets().catch((err) => setStatus(`Engine failed to start: ${err.message}`, true));

for (const id of ["printer", "material", "nozzle"]) {
  $(id).onchange = () => {
    viewer.setBed(state.presets.printers[$("printer").value].bed);
    if (state.triangles) findOrientations();
  };
}
document.querySelectorAll("input[name=mode]").forEach((r) => (r.onchange = () => state.triangles && runCheck()));

// ---------- input ----------
$("file").onchange = async (e) => {
  const file = e.target.files[0];
  if (file) loadFile(file.name, new Uint8Array(await file.arrayBuffer()));
};
const drop = $("drop");
for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => (e.preventDefault(), drop.classList.add("over")));
for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, () => drop.classList.remove("over"));
drop.addEventListener("drop", async (e) => {
  e.preventDefault();
  const file = e.dataTransfer.files[0];
  if (file) loadFile(file.name, new Uint8Array(await file.arrayBuffer()));
});
document.querySelectorAll("[data-sample]").forEach((btn) => {
  btn.onclick = async () => {
    const res = await fetch(`samples/${btn.dataset.sample}.stl`);
    loadFile(`${btn.dataset.sample}.stl`, new Uint8Array(await res.arrayBuffer()));
  };
});

// ---------- output ----------
const baseName = () => state.name.replace(/\.stl$/i, "");

function download(bytes, filename, type) {
  const a = el("a", { href: URL.createObjectURL(new Blob([bytes], { type })), download: filename });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

$("dl-3mf").onclick = async () => {
  const settings = Object.fromEntries(effectiveSettings().map((s) => [s.key, s.value]));
  const bytes = await call("export", { up: activeOption().up, profile: profile(), settings });
  download(bytes, `${baseName()}.3mf`, "model/3mf");
};

$("dl-stl").onclick = () => download(stlBytes(state.placed), `${baseName()}_oriented.stl`, "model/stl");
$("dl-stl-model").onclick = () => download(stlBytes(state.triangles), `${baseName()}.stl`, "model/stl");

$("copy-report").onclick = async () => {
  const md = await call("report", { up: activeOption().up, profile: profile(), mode: mode() });
  await navigator.clipboard.writeText(md);
  setStatus("Report copied — paste it anywhere, e.g. into ChatGPT or Claude to ask follow-up questions.");
};
