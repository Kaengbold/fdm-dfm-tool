// Part step (T-531, plan §4): the stage is one big drop zone ("Drop your part here", Choose file, "STL, 3MF or
// STEP") with exactly one button under it, Sample parts. Loading shows progress and Cancel in the drop zone; a
// normal load moves on to Setup by itself. Special cases: several bodies (pick one), a size that looks like the
// wrong unit (scale bar), a damaged mesh (toast), errors (plain reason, try another file).
import { h, icon } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { openModal, showToast } from "../ui/overlay.js";
import { registerStep } from "../shell.js";
import { t } from "../i18n.js";
import { parseStl, thumbnail } from "../viewport/stage.js";

const UPLOAD = ["M12 15V3m0 0-4 4m4-4 4 4", "M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"];
export const SAMPLES = [
  ["l_bracket.stl", "sample.bracket"], ["hook.stl", "sample.hook"], ["phone_stand.stl", "sample.stand"],
  ["enclosure.stl", "sample.box"], ["cable_clip.stl", "sample.clip"], ["hole_block.stl", "sample.holes"],
  ["dumbbell.stl", "sample.dumbbell"], ["mushroom.stl", "sample.mushroom"],
];
/** The engine's triangle bytes as Float32Array (copied: the bytes may not start on a 4-byte boundary). */
export const trianglesOf = (info) => new Float32Array(new Uint8Array(info.triangles).slice().buffer);
const kindOf = (name) => (/\.(step|stp)$/i.test(name) ? "step" : /\.3mf$/i.test(name) ? "3mf" : "stl");
const isProject = (name) => /\.(json|html?)$/i.test(name);
// Old projects named goals the old way (T-580).
const OLD_GOALS = { strength: "strong", speed: "fast", material: "light" };

// What the step shows between renders (a load in progress, a question, an error). Not in the store: it isn't
// the user's input and shouldn't be undone or saved.
let view = { mode: "idle" }; // idle | loading {text, share} | bodies {bodies, pick} | unit {hint} | error {message}

/** Load a file (bytes) into the engine, show it, and move on (or ask what's needed first). */
export async function loadPart({ name, bytes, store, go, services, rerender }) {
  const { engine, stage } = services;
  const kind = kindOf(name);
  const set = (v) => { view = v; rerender(); };
  if (isProject(name)) return openProject({ name, bytes, store, go, services, set });
  try {
    set({ mode: "loading", text: t("part.reading") });
    let faceIds, stepBodies = null;
    let data = bytes;
    if (kind === "step") {
      set({ mode: "loading", text: t("part.readingStep") });
      const { readStep } = await import("../step.js");
      const { stlBytes } = await import("../geometry.js");
      const bodies = await readStep(bytes);
      if (!bodies.length) throw new Error(t("part.noSolid"));
      let index = 0;
      if (bodies.length > 1) index = await new Promise((resolve) => set({ mode: "bodies", bodies: bodies.map((b) => ({ name: b.name, volume: b.volume })), pick: resolve }));
      stepBodies = bodies;
      data = stlBytes(bodies[index].soup);
      faceIds = Array.from(bodies[index].faceIds);
      set({ mode: "loading", text: t("part.reading") });
    }
    let info = await engine.call("load", { name, face_ids: faceIds }, data);
    if (!stepBodies && info.bodies.length > 1) {
      const index = await new Promise((resolve) => set({ mode: "bodies", bodies: info.bodies.map((b) => ({ name: b.name, volume: b.volume_mm3 })), pick: resolve }));
      if (index) info = await engine.call("select_body", { index });
    }
    if (info.unit_hint) {
      const factor = await new Promise((resolve) => set({ mode: "unit", hint: info.unit_hint, pick: resolve }));
      if (factor !== 1) info = await engine.call("scale", { factor });
    }
    services.setPart?.({ name, bytes, faceIds, kind });
    stage?.showPart(trianglesOf(info));
    const health = info.mesh_health ?? {};
    const damaged = ["holes", "non_manifold_edges", "flipped_faces"].some((k) => health[k] > 0);
    store.set({ part: { name, kind, size: info.size_mm, faces: info.faces, volume: info.volume_mm3, organic: info.kind === "organic", damaged } }, { undoable: false });
    view = { mode: "idle" };
    if (damaged) {
      showToast({ message: t("part.damaged"), tone: "warn", action: { label: t("ui.more"), onClick: () => openModal({ title: t("part.damagedTitle"), body: t("part.damagedMore", health) }) } });
    }
    go("setup");
  } catch (err) {
    if (err?.name === "Cancelled" || err?.constructor?.name === "Cancelled") { set({ mode: "idle" }); return; }
    set({ mode: "error", message: err?.message ?? String(err) });
  }
}

/**
 * A project file (web/project.js: Save project, the old app's projects, an agent's save_project) or a shared report
 * with the project inside: the part, then what was set on it. Holds and loads come back on their faces; the build
 * direction and slicer settings come back once Analyse has run again (an option needs the optimizer's numbers).
 */
async function openProject({ name, bytes, store, go, services, set }) {
  const { engine, stage } = services;
  try {
    set({ mode: "loading", text: t("part.reading") });
    let text = new TextDecoder().decode(bytes);
    if (/\.html?$/i.test(name)) {
      const { projectInReport } = await import("../report.js");
      text = projectInReport(text);
      if (!text) throw new Error(t("part.noProject"));
    }
    const { readProject } = await import("../project.js");
    const p = readProject(text);
    const info = await engine.call("load", { name: p.name, face_ids: p.faceIds ?? undefined }, p.stl);
    services.setPart?.({ name: p.name, bytes: p.stl, faceIds: p.faceIds ?? undefined, kind: "stl" });
    stage?.showPart(trianglesOf(info));
    const regions = p.loads?.length ? (await engine.call("regions")).regions : [];
    const centre = (x) => (x.point ?? (x.region != null ? regions[x.region]?.centroid : null) ?? null);
    const holds = (p.loads ?? []).filter((l) => l.kind === "fixed").map((l, i) => ({ id: `hold-p${i}`, region: l.region ?? null,
      edge: l.edge ?? null, edgeKind: l.edge_kind ?? null, kind: l.edge != null ? "edge" : "face", type: l.hold ?? "clamped",
      name: t("part.projectHold", { n: i + 1 }), point: centre(l) }));
    const { migrateLoads } = await import("./loads.js");
    const loads = migrateLoads((p.loads ?? []).filter((l) => l.kind === "force")).map((l, i) => ({ ...l, id: `load-p${i}`,
      edgeKind: l.edge_kind ?? null, point: centre(l) }));
    const pr = p.profile ?? {};
    const was = store.get();
    store.set({
      part: { name: p.name, kind: "stl", size: info.size_mm, faces: info.faces, volume: info.volume_mm3, organic: info.kind === "organic", damaged: false },
      profile: { ...was.profile, ...(pr.printer ? { printer: pr.printer } : {}), ...(pr.material ? { material: pr.material } : {}), ...(pr.nozzle ? { nozzle: String(pr.nozzle) } : {}) },
      ...(pr.goal ? { goal: OLD_GOALS[pr.goal] ?? pr.goal } : {}),
      ...(pr.use_temp_c != null ? { environment: { ...was.environment, temp: "custom", tempC: pr.use_temp_c } } : {}),
      holds, loads, noLoads: false, showFaces: p.faces?.clean ?? [],
    }, { undoable: false });
    view = { mode: "idle" };
    go("setup");
  } catch (err) {
    if (err?.name === "Cancelled") { set({ mode: "idle" }); return; }
    set({ mode: "error", message: err?.message ?? String(err) });
  }
}

registerStep("part", {
  render({ body, stage, store, go, services }) {
    const input = h("input", { type: "file", accept: ".stl,.3mf,.step,.stp,.json,.html", class: "visually-hidden", tabindex: "-1", "aria-hidden": "true" });
    const open = async (file) => loadPart({ name: file.name, bytes: new Uint8Array(await file.arrayBuffer()), store, go, services, rerender: services.rerender });
    input.addEventListener("change", () => input.files[0] && open(input.files[0]));
    const drop = h("div", { class: ["drop", `is-${view.mode}`] });

    if (view.mode === "loading") {
      drop.append(h("div", { class: "drop-progress", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("b", {}, view.text)),
        Button({ label: t("ui.cancel"), variant: "ghost", onClick: () => { services.engine.cancel(); view = { mode: "idle" }; services.rerender(); } }));
    } else if (view.mode === "bodies") {
      drop.append(h("h2", {}, t("part.whichBody")), h("div", { class: "body-list" }, view.bodies.map((b, i) =>
        Button({ label: `${b.name} · ${Math.round(b.volume / 1000)} cm³`, variant: i ? "secondary" : "primary", onClick: () => view.pick(i) }))));
    } else if (view.mode === "unit") {
      const hint = view.hint;
      drop.append(h("div", { class: "unit-bar", role: "alert" }, h("b", {}, hint.text), h("span", {}, t("part.unitAsk")),
        Button({ label: t("part.scaleBy", { unit: hint.unit, factor: hint.factor }), variant: "primary", onClick: () => view.pick(hint.factor) }),
        // Inches can't be told from centimetres by size: the hint may offer both.
        ...(hint.also ? [hint.also] : []).map((a) => Button({ label: t("part.scaleBy", { unit: a.unit, factor: a.factor }), onClick: () => view.pick(a.factor) })),
        Button({ label: t("part.keepSize"), variant: "ghost", onClick: () => view.pick(1) })));
    } else {
      if (view.mode === "error") drop.append(h("p", { class: "drop-error", role: "alert" }, view.message));
      drop.append(
        h("div", { class: "drop-icon" }, icon(UPLOAD)),
        h("h2", {}, t("part.drop")),
        Button({ label: view.mode === "error" ? t("part.tryAnother") : t("part.choose"), variant: "primary", onClick: () => input.click() }),
        h("span", { class: "ui-hint" }, t("part.formats")));
    }
    const samples = Button({ label: t("part.samples"), variant: "secondary", onClick: () => openSamples(samples, services, (name, bytes) =>
      loadPart({ name, bytes, store, go, services, rerender: services.rerender })) });
    body.append(h("div", { class: "drop-wrap" }, drop, view.mode === "idle" || view.mode === "error" ? samples : null, input));

    // The whole stage takes a dropped file.
    const zone = stage.parentElement;
    zone.ondragover = (e) => { e.preventDefault(); drop.classList.add("is-over"); };
    zone.ondragleave = () => drop.classList.remove("is-over");
    zone.ondrop = (e) => { e.preventDefault(); drop.classList.remove("is-over"); const f = e.dataTransfer.files[0]; if (f) open(f); };
    if (!store.get().part) services.stage?.clearPart?.();
  },
});

const thumbCache = new Map();

/** The sample list: a popover of thumbnails with 2–3-word names (§4). */
function openSamples(anchor, services, onPick) {
  document.querySelector(".sample-pop")?.remove();
  const grid = h("div", { class: "sample-grid" });
  const pop = h("div", { class: "sample-pop", role: "dialog", "aria-label": t("part.samples") }, h("div", { class: "ui-label" }, t("part.samples")), grid);
  for (const [file, key] of SAMPLES) {
    const cached = thumbCache.get(file);
    const img = h("img", { alt: "", width: 96, height: 96, ...(cached ? { src: cached } : {}) });
    const thumb = h("span", { class: ["sample-thumb", cached ? null : "is-loading"] }, img);
    const card = h("button", { type: "button", class: "sample-card", onclick: async () => {
      close();
      const bytes = new Uint8Array(await (await fetch(new URL(`../samples/${file}`, import.meta.url))).arrayBuffer());
      onPick(file, bytes);
    } }, thumb, h("span", {}, t(key)));
    grid.append(card);
    if (!cached) {
      fetch(new URL(`../samples/${file}`, import.meta.url))
        .then((r) => r.arrayBuffer())
        .then((b) => {
          const url = thumbnail(parseStl(b));
          thumbCache.set(file, url);
          img.src = url;
          thumb.classList.remove("is-loading");
        })
        .catch(() => { thumb.classList.remove("is-loading"); });
    }
  }
  const r = anchor.getBoundingClientRect();
  pop.style.left = `${Math.round(r.left + r.width / 2)}px`;
  pop.style.top = `${Math.round(r.top - 8)}px`;
  document.getElementById("portal-root").append(pop);
  grid.querySelector("button")?.focus();
  const close = () => { pop.remove(); document.removeEventListener("pointerdown", outside, true); anchor.focus(); };
  const outside = (e) => { if (!pop.contains(e.target)) close(); };
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); close(); } });
  setTimeout(() => document.addEventListener("pointerdown", outside, true));
}
