// Holds step (T-541, plan §7 and decision 3): where is the part fixed? All in 3D: hover shows what's under the
// cursor (viewport/picker.js), a click opens a radial menu at the cursor (Hold this face / edge / inside corner /
// screw hole, Cancel). Holds show blue with a lock marker; the list sets the kind (Fixed / Resting / Screw) and ✕
// removes. Many holds are allowed; at the 4th, or over 30 % of the surface, a toast warns. "No loads, skip strength"
// skips the strength steps (Strong and Balanced first offer to switch goal). The brush (T-573, "paint part of a big
// face"): the menu's Paint part of this face holds the view still, a drag lays discs on the surface, Done keeps the
// painted spots as one hold (the engine's {points, radius}).
import * as THREE from "three";
import { h, icon, ICONS, uid } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { SegmentedControl } from "../ui/choice.js";
import { openRadialMenu } from "../ui/radialmenu.js";
import { openModal, showToast } from "../ui/overlay.js";
import { registerStep } from "../shell.js";
import { t } from "../i18n.js";
import { clearBrush, showBrush } from "../viewport/brush3d.js";

const WARN_AT = 4; // holds
const WARN_SHARE = 0.3; // of the surface
const BRUSH_MM = [2, 4, 8]; // brush radii
const BRUSH_MAX = 200; // spots per painted hold (the engine gets them all)
let brush = null; // {spots: [{point, normal}], radius, region, painting} while painting

/** A hold as the strength engine takes it (core/fdmcore/strength.py: kind fixed, hold clamped | resting | screw). */
export function holdLoad(hold) {
  const where = hold.points?.length ? { points: hold.points, radius: hold.radius }
    : hold.edge != null ? { edge: hold.edge, edge_kind: hold.edgeKind } : { region: hold.region };
  return { ...where, kind: "fixed", hold: hold.type };
}

/** Plain name for what was picked: "Bottom face", "Ø5 mm hole", "Inside corner"… and its size line. */
export function describe(hit) {
  const i = hit.info ?? {};
  const mm = (v) => Math.round(v * 10) / 10;
  if (hit.kind === "edge") return { name: t("holds.edge"), size: t("holds.length", { length: mm(i.length_mm) }) };
  if (hit.kind === "corner") return { name: t("holds.corner"), size: t("holds.length", { length: mm(i.length_mm) }) };
  if (i.kind === "hole") return { name: t("holds.hole", { d: mm(i.hole_diameter) }), size: t("holds.area", { area: Math.round(i.area_mm2) }) };
  const nz = i.normal?.[2] ?? 0;
  const name = nz < -0.9 ? t("holds.bottomFace") : nz > 0.9 ? t("holds.topFace") : i.kind === "flat" ? t("holds.sideFace") : t("holds.roundFace");
  return { name, size: t("holds.area", { area: Math.round(i.area_mm2) }) };
}

let wired = false;
const markers = [];

function holdColor() {
  return new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue("--ov-hold").trim() || "#2563EB");
}

function addHold(store, hit, type) {
  const { name, size } = describe(hit);
  const hold = { id: uid("hold"), region: hit.region, edge: hit.kind === "face" ? null : hit.edge, edgeKind: hit.kind === "corner" ? "concave" : hit.kind === "edge" ? "feature" : null,
    kind: hit.kind === "face" ? (hit.info?.kind === "hole" ? "hole" : "face") : hit.kind, type, name, size, point: hit.point, area: hit.info?.area_mm2 ?? 0 };
  const holds = [...store.get().holds, hold];
  store.set({ holds, noLoads: false });
  return holds;
}

function warnIfTooMany(holds, picker) {
  const total = (picker.data?.regions ?? []).reduce((a, r) => a + (r.area_mm2 || 0), 0) || 1;
  const held = holds.reduce((a, x) => a + (x.edge == null ? x.area : 0), 0);
  if (holds.length === WARN_AT || (held / total > WARN_SHARE && held - (holds.at(-1)?.area ?? 0) <= WARN_SHARE * total)) {
    showToast({ message: t("holds.tooMany"), tone: "warn" });
  }
}

/** The menu for a picked spot: what can be held there, plus Cancel. */
function menuFor(hit, store, services) {
  const add = (target, type) => () => warnIfTooMany(addHold(store, target, type), services.picker);
  const face = { ...hit, kind: "face", info: services.picker.data.regions[hit.region] };
  const items = [];
  if (hit.kind === "edge") items.push({ label: t("holds.holdEdge"), primary: true, onSelect: add(hit, "clamped") });
  if (hit.kind === "corner") items.push({ label: t("holds.holdCorner"), primary: true, onSelect: add(hit, "clamped") });
  if (face.info?.kind === "hole") items.push({ label: t("holds.holdScrew"), primary: hit.kind === "face", onSelect: add(face, "screw") });
  items.push({ label: t("holds.holdFace"), primary: hit.kind === "face" && face.info?.kind !== "hole", onSelect: add(face, "clamped") });
  if (hit.kind === "face") items.push({ label: t("holds.paint"), onSelect: () => startBrush(hit, services) });
  items.push({ label: t("ui.cancel"), onSelect: () => {} });
  return items;
}

/** The surface normal at a picked triangle (part coordinates). */
function normalAt(services, tri) {
  const pos = services.stage.part?.geo.attributes.position;
  if (!pos || tri == null) return [0, 0, 1];
  const p = [0, 1, 2].map((k) => [pos.getX(tri * 3 + k), pos.getY(tri * 3 + k), pos.getZ(tri * 3 + k)]);
  const a = p[1].map((v, i) => v - p[0][i]), b = p[2].map((v, i) => v - p[0][i]);
  const n = [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const l = Math.hypot(...n) || 1;
  return n.map((v) => v / l);
}

function startBrush(hit, services) {
  brush = { spots: [{ point: hit.point, normal: normalAt(services, hit.triangle) }], radius: 4, region: hit.region, painting: false };
  services.stage.controls.enabled = false; // a drag paints instead of turning the view
  services.picker.enable(false); // no hover tint: only the painted spots show
  services.rerender();
}

function endBrush(services) {
  brush = null;
  services.stage.controls.enabled = true;
  services.picker.enable(true);
  services.rerender();
}

/** Keep the painted spots as one hold. */
function finishBrush(store, services) {
  const spots = brush.spots.length > BRUSH_MAX ? brush.spots.filter((_, i) => i % Math.ceil(brush.spots.length / BRUSH_MAX) === 0) : brush.spots;
  const r = brush.radius;
  const area = Math.round(Math.PI * r * r + (spots.length - 1) * r * r);
  const hold = { id: uid("hold"), kind: "brush", region: brush.region, edge: null, type: "clamped", name: t("holds.painted"),
    size: t("holds.paintedSize", { area }), points: spots.map((x) => x.point), normals: spots.map((x) => x.normal), radius: r,
    point: spots[0].point, area };
  const holds = [...store.get().holds, hold];
  store.set({ holds, noLoads: false });
  warnIfTooMany(holds, services.picker);
  endBrush(services);
}

/** The painted holds' discs, and while painting the spots so far. */
function drawSpots(store, services) {
  const painted = store.get().holds.filter((x) => x.points?.length).flatMap((x) => x.points.map((p, i) => ({ point: p, normal: x.normals?.[i], r: x.radius })));
  const spots = [...painted, ...(brush?.spots ?? []).map((x) => ({ ...x, r: brush.radius }))];
  if (spots.length) showBrush(services.stage, spots, 4, holdColor());
  else clearBrush(services.stage);
}

/** A drag over the part in brush mode adds a spot every half radius. */
function paintAt(e, store, services) {
  if (!brush?.painting) return;
  const hit = services.picker.pickAt?.(e.clientX, e.clientY);
  if (!hit?.point) return;
  const last = brush.spots.at(-1)?.point;
  if (last && Math.hypot(hit.point[0] - last[0], hit.point[1] - last[1], hit.point[2] - last[2]) < brush.radius / 2) return;
  brush.spots.push({ point: hit.point, normal: normalAt(services, hit.triangle) });
  drawSpots(store, services);
}

function wire(services, store) {
  if (wired) return;
  wired = true;
  const canvas = services.stage.canvas;
  canvas.addEventListener("pointerdown", (e) => { if (brush && store.get().step === "holds") { brush.painting = true; paintAt(e, store, services); } });
  canvas.addEventListener("pointermove", (e) => paintAt(e, store, services));
  const stop = () => { if (brush?.painting) { brush.painting = false; services.rerender(); } };
  canvas.addEventListener("pointerup", stop);
  canvas.addEventListener("pointerleave", stop);
  services.picker.on("pick", (hit, x, y) => {
    if (store.get().step !== "holds" || brush) return;
    const r = services.stage.canvas.getBoundingClientRect();
    openRadialMenu({ x, y, items: menuFor(hit, store, services), label: t("holds.menu"), bounds: r });
  });
  services.stage.controls.addEventListener("change", () => placeMarkers(services));
}

function placeMarkers(services) {
  for (const { el, point } of markers) {
    const p = services.picker.toClient(point);
    const host = el.parentElement?.getBoundingClientRect();
    if (!host) continue;
    el.style.left = `${p.x - host.left}px`;
    el.style.top = `${p.y - host.top}px`;
  }
}

registerStep("holds", {
  render({ body, stage, store, go, services }) {
    const s = store.get();
    wire(services, store);
    const blue = holdColor();
    const heldRegions = new Set(s.holds.filter((x) => x.edge == null).map((x) => x.region));
    if (services.picker.data) services.picker.setRegionColors((r) => (heldRegions.has(r) ? blue : null));

    // Lock markers at each hold, visible through the part, clickable for Remove.
    markers.length = 0;
    // Holds without a known spot (e.g. from an older project) get no marker, only their list row.
    for (const hold of s.holds.filter((x) => Array.isArray(x.point) && services.stage.part)) {
      const el = h("button", { type: "button", class: "hold-marker", title: hold.name, "aria-label": t("holds.markerLabel", { name: hold.name }) }, icon(ICONS.lock));
      el.addEventListener("click", () => {
        const p = el.getBoundingClientRect();
        openRadialMenu({ x: p.left + p.width / 2, y: p.top + p.height / 2, label: hold.name, bounds: services.stage.canvas.getBoundingClientRect(),
          items: [{ label: t("holds.remove"), primary: true, onSelect: () => store.set({ holds: store.get().holds.filter((x) => x.id !== hold.id) }) },
            { label: t("ui.cancel"), onSelect: () => {} }] });
      });
      stage.append(el);
      markers.push({ el, point: hold.point });
    }
    requestAnimationFrame(() => placeMarkers(services));
    stage.append(h("div", { class: "stage-pill" }, t(brush ? "holds.brushPill" : "holds.pill")));
    drawSpots(store, services); // painted holds as discs; while painting, also the spots so far
    if (brush) {
      body.append(h("div", { class: "hold-brush" },
        h("b", {}, t("holds.brushTitle")), h("p", { class: "ui-hint" }, t("holds.brushHow")),
        SegmentedControl({ label: t("holds.brushSize"), size: "small", value: String(brush.radius), options: BRUSH_MM.map((r) => ({ value: String(r), label: t("holds.brushMm", { mm: r }) })),
          onChange: (v) => { brush.radius = Number(v); services.rerender(); } }),
        h("span", { class: "ui-hint" }, t("holds.brushCount", { count: brush.spots.length })),
        h("div", { class: "row" }, Button({ label: t("holds.brushDone"), variant: "primary", size: "small", onClick: () => finishBrush(store, services) }),
          Button({ label: t("ui.cancel"), variant: "ghost", size: "small", onClick: () => endBrush(services) }))));
    }

    if (!s.holds.length) {
      body.append(h("ol", { class: "how-steps" }, h("li", {}, t("holds.howHover")), h("li", {}, t("holds.howClick"))));
    } else {
      const list = h("div", { class: "hold-list" }, s.holds.map((hold) => {
        const kinds = SegmentedControl({ label: t("holds.kindOf", { name: hold.name }), size: "small", value: hold.type, options: [
          { value: "clamped", label: t("holds.kind.clamped") }, { value: "resting", label: t("holds.kind.resting") }, { value: "screw", label: t("holds.kind.screw") }],
          onChange: (type) => store.set({ holds: store.get().holds.map((x) => (x.id === hold.id ? { ...x, type } : x)) }) });
        const row = h("div", { class: "hold-row", dataset: { hold: hold.id } },
          h("span", { class: "hold-swatch", "aria-hidden": "true" }),
          h("div", { class: "hold-text" }, h("b", {}, hold.name), h("span", {}, hold.size), kinds),
          h("button", { type: "button", class: "hold-x", "aria-label": t("holds.removeName", { name: hold.name }),
            onclick: () => store.set({ holds: store.get().holds.filter((x) => x.id !== hold.id) }) }, icon(ICONS.close)));
        // Hovering a row shows that hold on the part.
        const hit = hold.edge != null
          ? services.picker.data?.edges.find((l) => l.index === hold.edge && l.kind === (hold.edgeKind === "concave" ? "corner" : "edge"))
          : null;
        row.addEventListener("mouseenter", () => services.picker.showFeature(hold.edge != null
          ? (hit ? { kind: hit.kind, edge: hit.index, info: hit.meta, region: hold.region, point: hold.point, line: hit } : null)
          : { kind: "face", region: hold.region, info: services.picker.data?.regions[hold.region], point: hold.point }));
        row.addEventListener("mouseleave", () => services.picker.showFeature(null));
        return row;
      }));
      body.append(h("div", { class: "ui-label" }, t("holds.count", { count: s.holds.length })), list, h("p", { class: "ui-hint" }, t("holds.more")));
    }

    // Decision 3: "No loads, skip strength". Fast / Light / Quality just skip; Strong and Balanced ask first.
    const skip = () => {
      const goal = store.get().goal;
      const jump = (patch) => { store.set({ noLoads: true, ...patch }); go("analyse"); };
      if (goal !== "strong" && goal !== "balanced") return jump({});
      openModal({ title: t("holds.noLoadsTitle"), body: t("holds.noLoadsBody", { goal: t(`holds.goal.${goal}`) }), actions: [
        { label: t("holds.addLoads"), variant: "ghost" },
        { label: t("holds.switchQuality"), onClick: () => jump({ goal: "quality" }) },
        { label: t("holds.switchLight"), onClick: () => jump({ goal: "light" }) },
        { label: t("holds.switchFast"), variant: "primary", onClick: () => jump({ goal: "fast" }) }] });
    };
    body.append(h("div", { class: "hold-skip" }, Button({ label: t("holds.noLoads"), variant: "ghost", onClick: skip })));
  },
  leave({ services }) {
    if (brush) { brush = null; services.stage.controls.enabled = true; } // the next step turns the picker on or off itself
    clearBrush(services.stage);
  },
});
