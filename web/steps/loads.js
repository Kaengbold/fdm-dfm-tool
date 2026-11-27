// Loads step (T-543, plan §8): where is it pushed or pulled? Click a spot → the direction gizmo (viewport/gizmo.js)
// and a force box at the arrow: a number in N or MPa (MPa × face area shown; MPa only on faces), "≈ 5.1 kg of
// weight". Enter or OK sets it, Esc cancels this draft. Loads stay on the part as violet arrows (length on a log scale,
// capped) labelled "50 N"; clicking a label edits it. All loads act together unless "+ Separate load case".
// Stored in newtons; old projects with kg are converted (migrateLoads).
import * as THREE from "three";
import { h, icon, ICONS, uid } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { NumberField } from "../ui/numberfield.js";
import { openPopover3D } from "../ui/popover3d.js";
import { registerStep } from "../shell.js";
import { t } from "../i18n.js";
import { describeHit, hitNormal } from "./pickinfo.js";

const G = 9.81;

/** Old projects kept loads in kg: give every load ``newtons``. */
export const migrateLoads = (loads) => (loads ?? []).map((l) => ({
  ...l,
  newtons: l.newtons ?? (l.kg != null ? l.kg * G : 0),
  // Old projects: "down", "push" / "pull" or nothing: a vector (straight down when unknown).
  direction: Array.isArray(l.direction) ? l.direction : l.direction === "down" || l.direction == null ? [0, 0, -1] : l.direction === "pull" ? (l.normal ?? [0, 0, 1]) : (l.normal ?? [0, 0, 1]).map((x) => -x),
  name: l.name ?? t("loads.unnamed"),
}));

/** Arrow length in mm: grows with the force on a log scale and is capped, so small and big loads stay clickable. */
export const arrowLength = (newtons, partSize) => {
  const s = Math.max(partSize, 10);
  return Math.min(s * 0.25 + s * 0.12 * Math.log10(1 + Math.max(newtons, 0)), s * 0.75);
};

/** Short direction word for the list: Push / Pull / Down / Up / Sideways. */
export function directionWord(dir, normal) {
  const d = new THREE.Vector3(...dir).normalize(), n = new THREE.Vector3(...normal).normalize();
  if (d.dot(n) < -0.94) return t("loads.dir.push");
  if (d.dot(n) > 0.94) return t("loads.dir.pull");
  if (d.z < -0.94) return t("loads.dir.down");
  if (d.z > 0.94) return t("loads.dir.up");
  return t("loads.dir.tilted");
}

/** Where a part-coordinate point is on screen (client px). */
function toClient(services, point) {
  const { stage } = services;
  if (!stage.part) return { x: 0, y: 0 };
  const v = new THREE.Vector3(...point).add(stage.part.offset).project(stage.camera);
  const r = stage.canvas.getBoundingClientRect();
  return { x: (v.x + 1) / 2 * r.width + r.left, y: (1 - v.y) / 2 * r.height + r.top };
}

let wired = false;
let arrows = null; // THREE.Group of the load arrows in the stage
let labels = [];
let draft = null; // {gizmo, pop} while a load is being placed or edited

function closeDraft() {
  draft?.gizmo?.close();
  draft?.pop?.close();
  draft = null;
}

/** Place a new load at ``hit``, or edit ``existing``. */
function openDraft(services, store, hit, existing = null) {
  closeDraft();
  services.picker.setHit(null);
  let done = false; // Enter reaches both the field and the popover: set the load once
  const regions = services.picker.data?.regions;
  const normal = existing?.normal ?? hitNormal(hit, regions);
  const point = existing?.point ?? hit.point;
  const onFace = (existing ? existing.edge : (hit.kind === "face" ? null : hit.edge)) == null;
  const area = existing?.area ?? (onFace ? regions?.[hit.region]?.area_mm2 ?? 0 : 0);
  let direction = existing?.direction ?? normal.map((x) => -x);
  const gizmo = services.openGizmo(point, normal, { direction: existing?.direction ?? null, onChange: (d) => { direction = d; },
    onDone: () => commit(), onCancel: () => closeDraft() });
  const units = [{ value: "N", label: "N" }, { value: "MPa", label: "MPa", ...(onFace ? {} : { reason: t("loads.mpaFaceOnly") }) }];
  const field = NumberField({ label: t("loads.force"), value: existing ? Math.round(existing.newtons * 10) / 10 : "", units, autofocus: true, min: 0,
    convert: (v, unit) => (unit === "MPa"
      ? (onFace ? t("loads.mpaHint", { mpa: v, area: Math.round(area), n: Math.round(v * area) }) : t("loads.mpaFaceOnly"))
      : t("loads.kgHint", { kg: Math.round((v / G) * 10) / 10 })),
    onCommit: () => commit() });
  const ok = Button({ label: t("loads.ok"), variant: "primary", size: "small", onClick: () => commit() });
  const cancel = Button({ label: t("ui.cancel"), variant: "ghost", size: "small", onClick: () => closeDraft() });
  const content = h("div", { class: "force-pop" }, field, h("div", { class: "force-actions" }, cancel, ok));
  const pop = openPopover3D({ anchor: point, title: t("loads.forceTitle"), content, offset: 90,
    project: (p) => toClient(services, p), bounds: () => services.stage.canvas.getBoundingClientRect(),
    avoid: () => gizmo.element.getBoundingClientRect(),
    onConfirm: () => { commit(); return false; }, onCancel: () => closeDraft() });
  draft = { gizmo, pop };

  function commit() {
    if (done) return;
    const v = field.value;
    if (v == null || !(v > 0)) { field.input.focus(); return; }
    if (field.unit === "MPa" && !onFace) { field.input.focus(); return; }
    const newtons = field.unit === "MPa" ? v * area : v;
    const dir = gizmo.direction();
    const { name } = existing ?? describeHit(hit);
    const load = { id: existing?.id ?? uid("load"), region: existing ? existing.region : hit.region,
      edge: existing ? existing.edge : (hit.kind === "face" ? null : hit.edge),
      edgeKind: existing ? existing.edgeKind : (hit.kind === "corner" ? "concave" : hit.kind === "edge" ? "feature" : null),
      point, normal, area, name, direction: dir.map((x) => Math.round(x * 1e4) / 1e4), newtons, unit: field.unit,
      case: existing?.case ?? store.get().nextCase ?? 1 };
    const loads = existing ? store.get().loads.map((l) => (l.id === existing.id ? load : l)) : [...store.get().loads, load];
    done = true;
    closeDraft();
    store.set({ loads });
  }
}

function wire(services, store) {
  if (wired) return;
  wired = true;
  services.picker.on("pick", (hit) => {
    if (store.get().step !== "loads" || draft) return;
    openDraft(services, store, hit);
  });
  services.stage.controls.addEventListener("change", () => { placeLabels(services); draft?.pop?.update(); });
}

function drawArrows(services, loads) {
  const stage = services.stage;
  if (!stage.part?.geo?.boundingSphere) return;
  if (!arrows) { arrows = new THREE.Group(); arrows.renderOrder = 8; }
  // Attach to part.group so force arrows rotate and shift with the part whenever print orientation changes (T-607).
  if (arrows.parent !== stage.part.group) stage.part.group.add(arrows);
  arrows.clear();
  const size = stage.part.geo.boundingSphere.radius * 2;
  const violet = new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue("--ov-load").trim() || "#9333EA");
  for (const l of loads) {
    if (!Array.isArray(l.point)) continue;
    const d = new THREE.Vector3(...l.direction).normalize();
    const n = new THREE.Vector3(...(l.normal ?? [0, 0, 1]));
    const len = arrowLength(l.newtons, size);
    const at = new THREE.Vector3(...l.point).add(stage.part.offset);
    const pushing = d.dot(n) < 0;
    const tail = pushing ? at.clone().addScaledVector(d, -len) : at;
    const arrow = new THREE.ArrowHelper(d, tail, len, violet, len * 0.22, len * 0.12);
    arrow.traverse((o) => { if (o.material) { o.material.depthTest = true; } });
    arrow.userData = { id: l.id, tail: pushing ? tail : at.clone().addScaledVector(d, len) };
    arrows.add(arrow);
  }
  stage.render();
}

function placeLabels(services) {
  if (arrows) arrows.updateWorldMatrix(true, false);
  for (const { el, arrow } of labels) {
    const p = arrow.userData.tail.clone();
    if (arrows) arrows.localToWorld(p);
    p.project(services.stage.camera);
    const r = services.stage.canvas.getBoundingClientRect(), host = el.parentElement?.getBoundingClientRect();
    if (!host) continue;
    el.style.left = `${(p.x + 1) / 2 * r.width + r.left - host.left}px`;
    el.style.top = `${(1 - p.y) / 2 * r.height + r.top - host.top}px`;
  }
}

export const formatNewtons = (n) => (n >= 1000 ? `${Math.round(n / 100) / 10} kN` : `${Math.round(n)} N`);

registerStep("loads", {
  render({ body, stage, store, services }) {
    const s = store.get();
    wire(services, store);
    const loads = migrateLoads(s.loads);
    drawArrows(services, loads);
    labels = [];
    for (const arrow of arrows?.children ?? []) {
      const l = loads.find((x) => x.id === arrow.userData.id);
      const el = h("button", { type: "button", class: "load-label num", title: t("loads.edit"), onclick: () => openDraft(services, store, null, l) }, formatNewtons(l.newtons));
      stage.append(el);
      labels.push({ el, arrow });
    }
    requestAnimationFrame(() => placeLabels(services));
    stage.append(h("div", { class: "stage-pill" }, t("loads.pill")));

    if (!loads.length) {
      body.append(h("ol", { class: "how-steps" }, h("li", {}, t("loads.howClick")), h("li", {}, t("loads.howDir")), h("li", {}, t("loads.howForce"))));
    } else {
      const cases = new Set(loads.map((l) => l.case ?? 1));
      body.append(h("div", { class: "ui-label" }, t("loads.count", { count: loads.length })), h("div", { class: "load-list" }, loads.map((l) => {
        const words = `${l.name} · ${directionWord(l.direction, l.normal ?? [0, 0, 1])}`;
        return h("div", { class: "load-row", dataset: { load: l.id } },
          h("span", { class: "load-swatch", "aria-hidden": "true" }),
          h("div", { class: "load-text" }, h("b", {}, words), cases.size > 1 ? h("span", {}, t("loads.case", { n: l.case ?? 1 })) : null),
          h("b", { class: "num load-value" }, formatNewtons(l.newtons)),
          h("button", { type: "button", class: "load-x", "aria-label": t("loads.editName", { name: words }), onclick: () => openDraft(services, store, null, l) }, t("loads.editShort")),
          h("button", { type: "button", class: "load-x", "aria-label": t("loads.removeName", { name: words }),
            onclick: () => store.set({ loads: store.get().loads.filter((x) => x.id !== l.id) }) }, icon(ICONS.close)));
      })));
    }
    const nextCase = s.nextCase ?? 1;
    const maxCase = Math.max(1, ...loads.map((l) => l.case ?? 1));
    body.append(h("button", { type: "button", class: "ui-link", onclick: () => store.set({ nextCase: maxCase + 1 }, { undoable: false }) },
      nextCase > maxCase ? t("loads.nextIsNewCase", { n: nextCase }) : t("loads.separateCase")));
  },
  leave() { closeDraft(); },
});
