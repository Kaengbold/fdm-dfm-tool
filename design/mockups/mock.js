// Clickable UI mockup, shared by mockup-a.html and mockup-b.html. No engine: all results are fake.
// The two pages differ only in layout and look; the flow and the texts are the same.

const MODES = {
  simple:   { label: "Simple",   hint: "One click: printer, material, done.", steps: ["part", "have", "result"] },
  basic:    { label: "Basic",    hint: "All choices, no deep simulation.",    steps: ["part", "have", "orient", "problems", "settings", "download"] },
  advanced: { label: "Advanced", hint: "Everything, step by step.",           steps: ["part", "have", "orient", "problems", "strength", "design", "settings", "download"] },
};
const STEP_NAMES = {
  part: "Your part", have: "What you have", result: "Result", orient: "On the bed", problems: "Problems",
  strength: "Will it hold?", design: "Design changes", settings: "Slicer settings", download: "Download",
};
// Which later steps a change in this step updates (shown in the panel and used to mark steps outdated).
const AFFECTS = { have: ["problems", "strength", "settings", "result"], orient: ["problems", "strength", "settings"], design: ["problems", "strength"] };

const S = {
  mode: localStorage.getItem("mock-mode") || null,
  step: "part", loaded: false, checked: false, printer: "Bambu Lab A1", material: "PLA", load: false,
  orient: 0, picked: null, outdated: new Set(), running: null, extra: { vib: false, drop: false }, fixes: { fillet: true, insert: true },
  slicer: "OrcaSlicer",
};
const $ = (sel) => document.querySelector(sel);
const steps = () => MODES[S.mode].steps;

// ---------- fake 3D view (isometric SVG) ----------
function P(x, y, z) { const s = 2.6; return [270 + (x - y) * 0.866 * s, 190 + (x + y) * 0.5 * s - z * s]; }
const pts = (a) => a.map((p) => P(...p).map((v) => v.toFixed(1)).join(",")).join(" ");
function box(id, x0, y0, z0, dx, dy, dz, fills) {
  const [x1, y1, z1] = [x0 + dx, y0 + dy, z0 + dz];
  return `<g id="${id}">
    <polygon points="${pts([[x0, y1, z0], [x1, y1, z0], [x1, y1, z1], [x0, y1, z1]])}" fill="${fills[2]}"/>
    <polygon points="${pts([[x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [x1, y0, z1]])}" fill="${fills[1]}"/>
    <polygon points="${pts([[x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]])}" fill="${fills[0]}"/></g>`;
}
function ring(id, cx, cy, z, r, fill, stroke) {
  const a = Array.from({ length: 28 }, (_, i) => [cx + r * Math.cos(i / 28 * 2 * Math.PI), cy + r * Math.sin(i / 28 * 2 * Math.PI), z]);
  return `<polygon id="${id}" points="${pts(a)}" fill="${fill}" stroke="${stroke}" stroke-width="2"/>`;
}
function drawView() {
  const v = $("#view");
  if (!S.loaded) { v.innerHTML = `<div class="empty">Your part shows up here</div>`; return; }
  const ctx = S.step === "strength" ? "heat" : S.step === "design" ? "design" : ["problems", "result"].includes(S.step) ? "problems" : "plain";
  const N = ["var(--p-top)", "var(--p-s1)", "var(--p-s2)"];
  const bad = ["var(--bad-1)", "var(--bad-2)", "var(--bad-3)"];
  const heatA = ["url(#heat)", "url(#heat)", "url(#heat2)"];
  const up = ctx === "heat" ? heatA : N, base = ctx === "heat" ? ["url(#heat3)", "var(--p-s1)", "url(#heat3)"] : N;
  const shelf = ctx === "problems" ? bad : N;
  const gusset = ctx === "design" && S.fixes.fillet;
  const bed = pts([[-25, -25, 0], [115, -25, 0], [115, 65, 0], [-25, 65, 0]]);
  const grid = [];
  for (let i = -25; i <= 115; i += 14) grid.push(`<polyline points="${pts([[i, -25, 0], [i, 65, 0]])}"/>`);
  for (let j = -25; j <= 65; j += 15) grid.push(`<polyline points="${pts([[-25, j, 0], [115, j, 0]])}"/>`);
  const insertBad = ctx === "problems", insertFix = ctx === "design" && S.fixes.insert;
  v.innerHTML = `<svg viewBox="0 0 560 400" class="part">
    <defs>
      <pattern id="pei" width="14" height="14" patternUnits="userSpaceOnUse"><rect width="14" height="14" fill="#a8803f"/>
        <circle cx="3" cy="4" r="1.1" fill="#d9b46a"/><circle cx="10" cy="2" r=".8" fill="#7d5c27"/><circle cx="8" cy="9" r="1.2" fill="#c99f55"/>
        <circle cx="2" cy="11" r=".7" fill="#e6c27c"/><circle cx="12" cy="12" r=".9" fill="#8a6630"/><circle cx="6" cy="6" r=".6" fill="#f0d08f"/></pattern>
      <linearGradient id="heat" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#e5484d"/><stop offset=".35" stop-color="#f5a524"/><stop offset="1" stop-color="#4cc38a"/></linearGradient>
      <linearGradient id="heat2" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#c83a3f"/><stop offset=".35" stop-color="#d98c1a"/><stop offset="1" stop-color="#3aa876"/></linearGradient>
      <linearGradient id="heat3" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#f5a524"/><stop offset=".5" stop-color="#4cc38a"/><stop offset="1" stop-color="#4cc38a"/></linearGradient>
    </defs>
    <polygon points="${bed}" class="bed"/><g class="grid">${grid.join("")}</g>
    ${box("base", 0, 0, 0, 82, 40, 8, base)}
    ${ring("hole", 52, 20, 8, 5, "var(--hole)", "transparent")}
    ${ring("insert", 74, 20, 8, insertFix ? 3 : 3.6, "var(--hole)", insertBad ? "var(--bad-1)" : insertFix ? "var(--fix)" : "transparent")}
    ${box("upright", 0, 0, 8, 8, 40, 52, up)}
    ${gusset ? `<g id="gusset"><polygon points="${pts([[8, 40, 8], [24, 40, 8], [8, 40, 24]])}" fill="var(--fix)"/>
      <polygon points="${pts([[24, 0, 8], [24, 40, 8], [8, 40, 24], [8, 0, 24]])}" fill="var(--fix-2)"/></g>` : ""}
    ${box("shelf", 8, 0, 48, 22, 40, 6, shelf)}
  </svg>
  <div class="view-label">${["Lying on its back", "Standing up", "On its side"][S.orient]}, 82 × 40 × 60 mm</div>
  ${ctx === "heat" ? `<div class="legend"><span style="background:#4cc38a"></span>easy <span style="background:#f5a524"></span>busy <span style="background:#e5484d"></span>close to the limit</div>` : ""}`;
  if (S.picked) document.getElementById(S.picked)?.classList.add("pulse");
}

// ---------- steps list ----------
function drawSteps() {
  const list = steps(), i = list.indexOf(S.step);
  $("#steps").innerHTML = list.map((s, k) => {
    const state = s === S.step ? "now" : S.running && S.running.affects.includes(s) ? "busy"
      : S.outdated.has(s) ? "old" : k < i || (S.checked && k > 1) ? "done" : reachable(s) ? "open" : "locked";
    const icon = { now: k + 1, busy: "", old: "↻", done: "✓", open: k + 1, locked: k + 1 }[state];
    return `<button class="step ${state}" data-step="${s}" ${state === "locked" ? "disabled" : ""}>
      <span class="dot">${icon}</span><span class="name">${STEP_NAMES[s]}</span></button>`;
  }).join("");
  document.querySelectorAll("#steps .step").forEach((b) => b.onclick = () => go(b.dataset.step));
}
const reachable = (s) => s === "part" || (s === "have" && S.loaded) || S.checked;

// ---------- panel per step ----------
const more = (text) => `<details class="more"><summary>More information</summary><p>${text}</p></details>`;
const affects = (step) => AFFECTS[step] ? `<p class="affects">Changing this updates ${AFFECTS[step].filter((s) => steps().includes(s)).map((s) => `<b>${STEP_NAMES[s]}</b>`).join(", ")}</p>` : "";
const chips = (name, options, value) => `<div class="chips" data-name="${name}">${options.map((o) => `<button class="chip ${o === value ? "on" : ""}" data-v="${o}">${o}</button>`).join("")}</div>`;

const PANELS = {
  part: () => S.loaded
    ? `<h2>Your part</h2><div class="file"><b>bracket.stl</b><span>82 × 40 × 60 mm · about 31 g of PLA</span><button class="link" data-act="unload">Change</button></div>`
    : `<h2>Your part</h2><p class="lead">Drop a 3D file to start.</p>
       <button class="drop" data-act="load"><b>Drop STL, 3MF or STEP here</b><span>or click to choose a file</span></button>
       <p class="small">No file at hand? Try a sample: <button class="link" data-act="load">Shelf bracket</button> · <button class="link" data-act="load">Hook</button></p>
       ${more("Your file never leaves your computer. Everything runs in this browser tab.")}`,
  have: () => `<h2>What you have</h2>
    <label>Printer</label>${chips("printer", ["Bambu Lab A1", "Prusa MK4", "Prusa Core One", "Ender-3 V3", "Other…"], S.printer)}
    <label>Material</label>${chips("material", ["PLA", "PETG", "ABS", "ASA", "TPU"], S.material)}
    ${S.mode === "simple" ? "" : `<label>What matters most?</label>${chips("goal", ["Strong", "Looks good", "Fast"], "Strong")}`}
    <label class="toggle"><input type="checkbox" data-act="load-toggle" ${S.load ? "checked" : ""}> Something pushes or pulls on it <small>optional</small></label>
    ${S.load ? `<div class="sub">Tap the face where it's pushed, then: ${chips("kg", ["0.5 kg", "2 kg", "5 kg", "10 kg"], "2 kg")}</div>` : ""}
    ${affects("have")}
    ${more("We use your printer's bed size and enclosure, and the material's strength, shrinkage and temperature limits. The load is optional: without it we still find print problems, but we can't say if it holds.")}`,
  orient: () => {
    const o = [["Lying on its back", "No support", "Strong", "1 h 40 min", "best"], ["Standing up", "Support 6 g", "Weak across layers", "1 h 55 min", ""], ["On its side", "Support 2 g", "Strong", "2 h 10 min", ""]];
    return `<h2>How it lies on the bed</h2><p class="lead">We picked the best one. Tap another to compare.</p>
      <div class="cards">${o.map((r, k) => `<button class="card pick ${k === S.orient ? "on" : ""}" data-orient="${k}">
        <b>${r[0]}${r[4] ? ` <em class="tag good">best</em>` : ""}</b>
        <span class="${k === 0 ? "" : k === 1 ? "worse" : "meh"}">${r[1]}</span><span class="${k === 1 ? "worse" : ""}">${r[2]}</span><span>${r[3]}</span></button>`).join("")}</div>
      ${S.orient ? `<div class="delta">Compared with the best: ${S.orient === 1 ? "<b class='worse'>+6 g support</b> · <b class='worse'>breaks 3× sooner</b> · +15 min" : "<b class='meh'>+2 g support</b> · same strength · +30 min"}</div>` : ""}
      ${affects("orient")}
      ${more("Layers stick to each other less well than the plastic inside a layer. So a part is weakest when it's pulled apart across its layers. We turn the part so the load runs along the layers and as little as possible hangs in the air.")}`;
  },
  problems: () => problemList(false),
  result: () => `<div class="verdict warn"><b>Prints, but 2 spots can fail</b><span>Change them in your CAD (or see how in Advanced). 2 more are fixed by our settings.</span></div>
    ${problemList(true)}
    <button class="primary big" data-act="dl">Download 3MF for ${S.slicer}</button>
    <p class="small center">Turned the right way, with ${S.material} settings for your ${S.printer}. ${chips("slicer", ["OrcaSlicer", "Bambu Studio", "PrusaSlicer"], S.slicer)}</p>
    <p class="small center"><button class="link" data-act="to-basic">See all details (Basic)</button></p>`,
  strength: () => `<h2>Will it hold?</h2>
    ${S.load ? "" : `<p class="lead">Tap where it's pushed. ${chips("kg", ["0.5 kg", "2 kg", "5 kg", "10 kg"], "2 kg")}</p>`}
    <div class="gauge"><b>Holds about 3.4× your load</b><span>Weakest spot: the inside corner (red)</span><div class="bar"><i style="width:70%"></i></div></div>
    <h3>Extra simulations <small>optional</small></h3>
    <label class="toggle"><input type="checkbox" data-act="vib" ${S.extra.vib ? "checked" : ""}> Shaking: is something mounted on it?</label>
    ${S.extra.vib ? `<div class="sub">Weight on it: ${chips("mkg", ["0.1 kg", "0.5 kg", "1 kg"], "0.5 kg")}
      <div class="res">Swings about <b>85 times a second</b>. <span class="meh">Close to fans and motors (20–200 Hz): it may buzz.</span></div></div>` : ""}
    <label class="toggle"><input type="checkbox" data-act="drop" ${S.extra.drop ? "checked" : ""}> Knocks: can the load drop onto it?</label>
    ${S.extra.drop ? `<div class="sub">From ${chips("h", ["1 cm", "5 cm", "20 cm"], "5 cm")}
      <div class="res">Hits <b>4.1× harder</b> than resting. <span class="worse">Holds 0.8× that: may crack.</span></div></div>` : ""}
    ${more("We split the part into small cubes and work out how each one bends. Layer lines count as weaker. It's a good estimate, not a guarantee: print a test piece for anything that matters.")}`,
  design: () => `<h2>Design changes</h2><p class="lead">Changes to the shape. Blue = added.</p>
    <label class="toggle"><input type="checkbox" data-act="fix-fillet" ${S.fixes.fillet ? "checked" : ""}> Round the inside corner <small>5 mm</small></label>
    <p class="effect">Holds <b class="better">+45 %</b> · +2 g</p>
    <label class="toggle"><input type="checkbox" data-act="fix-insert" ${S.fixes.insert ? "checked" : ""}> Smaller hole for the M3 insert</label>
    <p class="effect">Insert hole 4.0 mm, wall around it <b class="better">1.6 mm</b></p>
    ${affects("design")}
    <button class="secondary" data-act="dl">Download changed part (STEP)</button>`,
  settings: () => {
    const rows = [["Walls", "4", "Strength comes mostly from walls"], ["Infill", "15 % gyroid", "Enough with 4 walls"], ["Support", "Only under the shelf", "Nothing else hangs"],
      ["Brim", "Off", "Big flat base sticks well"], ["Ironing", "Top face", "Smooth flat top"], [S.material === "PETG" ? "Fan" : "Speed", S.material === "PETG" ? "40 %" : "Normal", "From your material"]];
    return `<h2>Slicer settings</h2><p class="lead">What we change from the ${S.material} default, and why.</p>
      <table>${rows.map((r) => `<tr><td>${r[0]}</td><td><b>${r[1]}</b></td><td class="why">${r[2]}</td></tr>`).join("")}</table>
      <label>For</label>${chips("slicer", ["OrcaSlicer", "Bambu Studio", "PrusaSlicer"], S.slicer)}
      ${more("These go into the 3MF file as part settings, so your own printer and material profiles stay as they are.")}`;
  },
  download: () => `<h2>Ready to print</h2>
    <div class="verdict ok"><b>All set</b><span>${S.mode === "advanced" ? "2 design changes · " : ""}6 settings · lying on its back</span></div>
    <button class="primary big" data-act="dl">Download 3MF for ${S.slicer}</button>
    ${S.mode === "advanced" ? `<button class="secondary" data-act="dl">Download changed part (STEP)</button>` : ""}
    <p class="small">Open it in ${S.slicer}, check the preview, print.</p>`,
};

function problemList(short) {
  const all = [
    ["fail", "insert", "Wall around the M3 insert is too thin", "It can split when you press the insert in.", "Change the design", "Make the hole 4.0 mm or move it 2 mm inwards."],
    ["fail", "upright", "Sharp inside corner", "Under load it cracks here first.", "Change the design", "Round it with about 5 mm."],
    ["look", "shelf", "The shelf hangs in the air", "Needs support underneath. Leaves a rough bottom.", "Fixed by a setting", "Support is turned on just under the shelf."],
    ["tip", "base", "First layers bulge out", "Common on big flat bases.", "Fixed by a setting", "We set elephant-foot compensation."],
  ];
  const groups = [["fail", "Can fail"], ["look", "Looks worse"], ["tip", "Good to know"]];
  const items = short ? all.slice(0, 3) : all;
  return `${short ? "" : `<h2>Problems</h2><p class="lead">Tap one to see where it is.</p>`}
    ${groups.map(([g, title]) => {
      const it = items.filter((p) => p[0] === g);
      return it.length ? `<h3 class="group ${g}">${title}</h3>${it.map((p) => `<div class="problem ${g} ${S.picked === p[1] ? "on" : ""}" data-pick="${p[1]}">
        <b>${p[2]}</b><span>${p[3]}</span><em class="tag ${p[4].startsWith("Fixed") ? "good" : "todo"}">${p[4].startsWith("Fixed") ? "✓ " : ""}${p[4]}</em>
        ${short ? "" : more(p[5])}</div>`).join("")}` : "";
    }).join("")}`;
}

function drawPanel() {
  const list = steps(), i = list.indexOf(S.step);
  const next = list[i + 1];
  let foot = "";
  if (S.step === "have" && !S.checked) foot = `<button class="primary big" data-act="check">${S.mode === "simple" ? "Check my part" : "Check and continue"}</button>`;
  else if (next && reachable(next)) foot = `<button class="primary" data-act="next">Next: ${STEP_NAMES[next]}</button>`;
  else if (S.step === "part" && S.loaded) foot = `<button class="primary" data-act="next">Next: ${STEP_NAMES[next]}</button>`;
  $("#panel").innerHTML = `<div class="panel-body">${PANELS[S.step]()}</div>
    <div class="panel-foot">${i > 0 ? `<button class="ghost" data-act="back">Back</button>` : "<span></span>"}${foot}</div>`;
  wirePanel();
}

function wirePanel() {
  const p = $("#panel");
  p.querySelectorAll("[data-act]").forEach((el) => {
    const act = el.dataset.act;
    el.addEventListener(el.type === "checkbox" ? "change" : "click", () => ACTIONS[act]?.(el));
  });
  p.querySelectorAll(".chips").forEach((c) => c.querySelectorAll(".chip").forEach((b) => b.onclick = () => {
    c.querySelectorAll(".chip").forEach((x) => x.classList.remove("on")); b.classList.add("on");
    const name = c.dataset.name;
    if (name === "printer" || name === "material") { const old = S[name]; S[name] = b.dataset.v; if (S.checked && old !== S[name]) recheck(`${name === "material" ? "Material" : "Printer"} changed to ${S[name]}`, AFFECTS.have); }
    if (name === "slicer") { S.slicer = b.dataset.v; drawPanel(); }
  }));
  p.querySelectorAll("[data-orient]").forEach((b) => b.onclick = () => { S.orient = +b.dataset.orient; recheck(`Turned: ${["lying on its back", "standing up", "on its side"][S.orient]}`, AFFECTS.orient); });
  p.querySelectorAll("[data-pick]").forEach((b) => b.onclick = (e) => { if (e.target.closest("details")) return; S.picked = S.picked === b.dataset.pick ? null : b.dataset.pick; render(); });
}

const ACTIONS = {
  load: () => run("Opening bracket.stl", [["Reading the file", 0.6], ["Repairing small gaps", 0.5]], [], () => { S.loaded = true; S.step = "have"; }),
  unload: () => { Object.assign(S, { loaded: false, checked: false, step: "part", picked: null }); S.outdated.clear(); render(); },
  "load-toggle": (el) => { S.load = el.checked; drawPanel(); },
  check: () => run("Checking your part", [["Cutting it into layers", 1.2], ["Finding the best way to lay it", 1.6], ["Looking for problems", 1.0], ["Picking slicer settings", 0.6]].concat(S.load ? [["Testing the load", 1.4]] : []),
    steps().slice(2), () => { S.checked = true; S.step = steps()[2]; }),
  next: () => go(steps()[steps().indexOf(S.step) + 1]),
  back: () => go(steps()[steps().indexOf(S.step) - 1]),
  dl: () => toast("Downloaded (mockup: no file)"),
  "to-basic": () => setMode("basic"),
  vib: (el) => { S.extra.vib = el.checked; if (el.checked) run("Shaking simulation", [["Finding how it swings", 2.5]], ["strength"], () => {}); else drawPanel(); },
  drop: (el) => { S.extra.drop = el.checked; if (el.checked) run("Knock simulation", [["Working out how far it bends", 1.5]], ["strength"], () => {}); else drawPanel(); },
  "fix-fillet": (el) => { S.fixes.fillet = el.checked; recheck(el.checked ? "Inside corner rounded" : "Rounding removed", AFFECTS.design); },
  "fix-insert": (el) => { S.fixes.insert = el.checked; recheck("Insert hole changed", AFFECTS.design); },
};

function recheck(what, affected) {
  const list = affected.filter((s) => steps().includes(s));
  list.forEach((s) => S.outdated.add(s));
  toast(`${what}. Updating ${list.map((s) => STEP_NAMES[s]).join(", ")}`);
  const st = [["Looking for problems", 0.9]].concat(list.includes("strength") ? [["Testing the load", 1.1]] : [], [["Picking slicer settings", 0.5]]);
  run("Updating results", st, list, () => list.forEach((s) => S.outdated.delete(s)));
}

// ---------- background work: what, how long, what it updates, cancel ----------
function run(title, stages, affected, done) {
  if (S.running) clearInterval(S.running.timer);
  const total = stages.reduce((a, s) => a + s[1], 0);
  const t0 = performance.now();
  S.running = { title, stages, affected, affects: affected, t0, total };
  const tick = () => {
    const el = (performance.now() - t0) / 1000;
    let acc = 0, cur = stages.length;
    for (let k = 0; k < stages.length; k++) { if (el < acc + stages[k][1]) { cur = k; break; } acc += stages[k][1]; }
    if (el >= total) { clearInterval(S.running.timer); S.running = null; done(); hideActivity(); render(); return; }
    showActivity(title, stages, cur, el, total, affected);
  };
  S.running.timer = setInterval(tick, 100);
  render(); tick();
}
function showActivity(title, stages, cur, el, total, affected) {
  const a = $("#activity");
  a.hidden = false;
  a.innerHTML = `<div class="act-head"><span class="spin"></span><b>${title}</b><span class="time">${el.toFixed(0)} s · about ${Math.max(1, Math.ceil(total - el))} s left</span>
      <button class="ghost small" id="cancel">Cancel</button></div>
    <div class="bar"><i style="width:${(100 * el / total).toFixed(0)}%"></i></div>
    <ol class="stages">${stages.map((s, k) => `<li class="${k < cur ? "done" : k === cur ? "now" : ""}">${k < cur ? "✓" : k === cur ? "…" : "·"} ${s[0]}</li>`).join("")}</ol>
    ${affected.length ? `<div class="act-aff">Updates: ${affected.filter((s) => steps().includes(s)).map((s) => STEP_NAMES[s]).join(", ")}</div>` : ""}`;
  $("#cancel").onclick = () => { clearInterval(S.running.timer); S.running = null; hideActivity(); toast("Stopped. Old results kept, marked ↻"); render(); };
}
const hideActivity = () => { $("#activity").hidden = true; };

let toastTimer;
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => t.hidden = true, 3200); }

// ---------- modes ----------
function setMode(m) {
  S.mode = m; localStorage.setItem("mock-mode", m);
  if (!steps().includes(S.step)) S.step = S.checked ? steps()[2] : S.loaded ? "have" : "part";
  document.body.dataset.mode = m; render();
}
function drawModes() {
  $("#modes").innerHTML = Object.entries(MODES).map(([k, m]) => `<button class="mode ${k === S.mode ? "on" : ""}" data-mode="${k}" title="${m.hint}">${m.label}</button>`).join("")
    + `<button class="mode" disabled title="Comes later: everything on one page">Expert <small>later</small></button>`;
  document.querySelectorAll("[data-mode]").forEach((b) => b.onclick = () => setMode(b.dataset.mode));
}
function go(s) { if (!reachable(s)) return; S.step = s; S.picked = null; render(); }
function render() { drawModes(); drawSteps(); drawPanel(); drawView(); window.afterRender?.(); }

window.startMock = () => {
  if (!S.mode) { window.chooseMode?.(); S.mode = "basic"; }
  document.body.dataset.mode = S.mode; render();
};
window.mockSetMode = setMode; window.setMode = setMode; window.S = S; window.render = render;
