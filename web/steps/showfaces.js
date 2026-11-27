// Show faces for the Quality goal (T-563, plan §14, decision 10). We first guess the faces people see: the outer
// ones (api.visible_faces), tinted lightly on the part, and Quality keeps support off them. A click on the part (on
// Analyse) marks a show face instead; marked faces replace the guess, a second click unmarks one. The optimizer,
// the checks and Simulation get them through args.facesArg.
import * as THREE from "three";
import { h } from "../ui/dom.js";
import { t } from "../i18n.js";

let guess = { part: null, regions: null }; // api.visible_faces for the part shown: null while loading, array once resolved
let wired = false;

const color = (name, fallback) => new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback);

function tint(store, services) {
  const picker = services.picker;
  if (!picker?.data || !services.stage?.part) return;
  const marked = new Set(store.get().showFaces ?? []);
  const guessed = new Set(marked.size ? [] : guess.regions ?? []);
  const strong = color("--accent", "#2563EB");
  // Linear-RGB lerp at 0.62 reads as a clear soft blue tint on both light and dark stages, still lighter than a mark.
  picker.setRegionColors((r) => (marked.has(r) ? strong : guessed.has(r) ? services.stage.part.base.clone().lerp(strong, 0.62) : null));
}

function wire(store, services) {
  if (wired) return;
  wired = true;
  services.picker.on("pick", (hit) => {
    const s = store.get();
    if (s.step !== "analyse" || s.goal !== "quality" || hit.region == null) return;
    const set = new Set(s.showFaces ?? []);
    if (set.has(hit.region)) set.delete(hit.region);
    else set.add(hit.region);
    store.set({ showFaces: [...set].sort((a, b) => a - b) });
  });
}

/** The panel line on Analyse: what counts as seen, and how to change it. Tints the part to match. */
export function showFacesPanel(store, services) {
  wire(store, services);
  const s = store.get();
  if (guess.part !== s.part) {
    const part = s.part;
    guess = { part, regions: null };
    services.engine.call("visible_faces").then((r) => {
      if (guess.part !== part) return;
      guess.regions = Array.isArray(r?.regions) ? r.regions : [];
      const cur = store.get();
      if (cur.step === "analyse" && cur.goal === "quality") {
        tint(store, services);
        services.rerender();
      }
    }).catch(() => {
      if (guess.part !== part) return;
      guess.regions = [];
      const cur = store.get();
      if (cur.step === "analyse" && cur.goal === "quality") services.rerender();
    });
  }
  tint(store, services);
  const n = s.showFaces?.length ?? 0;
  const ready = Boolean(services.picker?.data) && guess.regions !== null;
  const hint = n
    ? t("showFaces.marked", { count: n })
    : !ready
      ? t("showFaces.loading")
      : guess.regions.length
        ? t("showFaces.guess")
        : t("showFaces.none");
  return h("div", { class: "showfaces" },
    h("b", {}, t("showFaces.title")),
    h("span", { class: "ui-hint" }, hint),
    n ? h("button", { type: "button", class: "ui-link", onclick: () => store.set({ showFaces: [] }) }, t("showFaces.reset")) : null);
}

export function clearShowFaces(services) {
  services.picker?.setRegionColors(() => null);
}
