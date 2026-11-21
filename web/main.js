// Entry of the new UI (web/new.html, T-530/T-531): the engine, the 3D stage and the shell, plus the steps.
// The theme follows the saved switch (light by default, §16.6).
import { startShell } from "./shell.js";
import { autosave, createStore } from "./state.js";
import { createEngine } from "./engine.js";
import { Stage, cameraBar, thumbnail } from "./viewport/stage.js";
import { t } from "./i18n.js";
import { loadPart, trianglesOf } from "./steps/part.js";
import "./steps/start.js";
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
  loadPart: (name, bytes) => loadPart({ name, bytes, store, go: app.go, services, rerender: () => services.rerender() }),
  openSettings: (tab) => {
    openSettingsTab(tab);
    app.openRoute("settings");
  },
};
const app = startShell(document.getElementById("app"), { store, saving, services });
// Load persisted settings, populate store.catalog (printers + nozzles + owned materials), and push to the engine once ready.
initSettings({ store, saving, engine, services });
store.subscribe((_, changed) => {
  if (changed.includes("part") && services.stage.part) saving.putExtra("thumb", thumbnail(services.stage.part.geo.attributes.position.array));
});
globalThis.fdmUI = app; // for the browser tests and for agents driving the page
document.body.dataset.ready = "1";
