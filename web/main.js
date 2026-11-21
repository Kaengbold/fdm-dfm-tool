// Entry of the new UI (web/new.html, T-530/T-531): the engine, the 3D stage and the shell, plus the steps.
// The theme follows the saved switch (light by default, §16.6).
import { startShell } from "./shell.js";
import "./steps/setup.js";
import "./steps/environment.js";
import { autosave, createStore } from "./state.js";
import { createEngine } from "./engine.js";
import { Stage, cameraBar, thumbnail } from "./viewport/stage.js";
import { t } from "./i18n.js";
import { Picker } from "./viewport/picker.js";
import { attachKeyboard } from "./viewport/featurelist.js";
import { loadPart, trianglesOf } from "./steps/part.js";
import "./steps/start.js";
import "./steps/options.js";
import "./steps/loads.js";
import "./steps/details.js";
import "./steps/holds.js";
import { openGizmo } from "./viewport/gizmo.js";
import { initSettings, openSettingsTab, restoreSettingsToEngine } from "./steps/settings.js";

try {
  if (localStorage.getItem("fdm-theme") === "dark") document.documentElement.dataset.theme = "dark";
} catch { /* storage blocked: light */ }

const store = createStore();
const saving = autosave(store);
let source = null; // the part's file: {name, bytes, faceIds, kind}, to give a restarted engine the part again
const engine = createEngine({ onProgress: ({ fraction, text }) => services.onProgress?.(fraction, text) });
engine.onRestore(async (post) => {
  await restoreSettingsToEngine(post);
  if (source) await post("load", { name: source.name, face_ids: source.faceIds }, source.bytes);
});
const stageHost = document.createElement("section");
const services = {
  engine,
  stage: new Stage(stageHost),
  setPart(part) {
    source = part;
    saving.putExtra("part", part);
    saving.putExtra("thumb", null);
  },
  restoreLast: async () => {
    const last = await saving.restore();
    if (!last?.state?.part) return null;
    return { ...last, thumb: await saving.getExtra("thumb") };
  },
  async continueLast(last) {
    const part = await saving.getExtra("part");
    if (!part) return app.go("part");
    store.load({ ...last.state, catalog: store.get().catalog });
    const info = await engine.call("load", { name: part.name, face_ids: part.faceIds }, part.bytes);
    source = part;
    services.stage.showPart(trianglesOf(info));
    app.go(last.state.step);
  },
  cameraBar: () => cameraBar(services.stage, { title: t("camera.title"), iso: t("camera.iso"), top: t("camera.top"), front: t("camera.front"), fit: t("camera.fit") }),
  // Direction gizmo for a load at ``point`` with face ``normal`` (part coordinates). The Loads step (T-543) uses it;
  // until then it is the dev hook for trying it out.
  openGizmo: (point, normal, options = {}) => openGizmo(services.stage, { point, normal, ...options,
    labels: { push: t("gizmo.push"), pull: t("gizmo.pull"), title: t("gizmo.title") } }),
  loadPart: (name, bytes) => loadPart({ name, bytes, store, go: app.go, services, rerender: () => services.rerender() }),
  openSettings: (tab) => {
    openSettingsTab(tab);
    app.openRoute("settings");
  },
};
// What the picker's badge says for a hovered face, edge or inside corner (§7).
const pickLabel = (hit) => {
  const i = hit.info ?? {};
  const mm = (v) => (Math.round(v * 10) / 10).toString();
  if (hit.kind === "edge") return t("picker.edge", { length: mm(i.length_mm) });
  if (hit.kind === "corner") return t("picker.corner", { length: mm(i.length_mm) });
  if (i.kind === "hole") return t("picker.hole", { d: mm(i.hole_diameter) });
  if (i.kind === "cylinder") return t("picker.cylinder", { d: mm(i.diameter) });
  if (i.kind === "curved") return t("picker.curved", { area: Math.round(i.area_mm2) });
  return t("picker.flat", { area: Math.round(i.area_mm2) });
};
services.picker = new Picker(services.stage, { label: pickLabel });
attachKeyboard(services.stage, services.picker, { label: pickLabel, placeholder: t("picker.search"), title: t("picker.title") });
// Picking happens on Holds and Loads (T-541, T-543); the face regions come once per part.
const PICK_STEPS = new Set(["holds", "loads"]);
let regionsFor = null;
async function preparePicker() {
  const s = store.get();
  const on = Boolean(s.part) && PICK_STEPS.has(s.step) && app.route === "flow";
  if (on && regionsFor !== s.part) {
    regionsFor = s.part;
    services.picker.setData(await engine.call("regions"));
  }
  services.picker.enable(on);
  if (s.step !== "options" && services.stage.oriented) services.stage.orient(null);
}
const app = startShell(document.getElementById("app"), { store, saving, services });
// Load persisted settings, populate store.catalog (printers + nozzles + owned materials), and push to the engine once ready.
initSettings({ store, saving, engine, services });
store.subscribe((_, changed) => {
  if (changed.includes("part") && services.stage.part) saving.putExtra("thumb", thumbnail(services.stage.part.geo.attributes.position.array));
});
store.subscribe((_, changed) => { if (changed.includes("step") || changed.includes("part")) preparePicker(); });
globalThis.fdmUI = app; // for the browser tests and for agents driving the page
document.body.dataset.ready = "1";
