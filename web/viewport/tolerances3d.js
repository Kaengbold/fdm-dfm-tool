// Tolerances & fits in 3D (T-570, plan §15.2): the ghost mate (a see-through pin slides into a hole, or a sleeve
// over a peg) with a clearance ring at the mouth coloured by what will happen (jams / tight / fits / loose), and the
// printed-vs-CAD overlay: an orange circle at the size each hole or peg really prints, exaggerated ×5 or ×10 so a
// tenth of a millimetre shows. T-573: the feeler gauge for flat gaps (a blade slides through a slot, or slot walls
// slide over a tab, and stop red at the mouth where it jams) and the tightest spot against a mating part. Everything
// lives in the part's group (so it turns with the part) in part coordinates.
import * as THREE from "three";

const Y = new THREE.Vector3(0, 1, 0);
const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
const VERDICT_COLOR = { jam: ["--bad", "#B91C1C"], tight: ["--warn", "#B45309"], loose: ["--warn", "#B45309"], fits: ["--good", "#15803D"] };
const reducedMotion = () => globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

function layer(stage, name) {
  let g = stage.part?.group.getObjectByName(name);
  if (!g && stage.part) {
    g = new THREE.Group();
    g.name = name;
    stage.part.group.add(g);
  }
  return g;
}

function clear(stage, name) {
  const g = stage.part?.group.getObjectByName(name);
  if (!g) return;
  g.traverse((o) => { o.geometry?.dispose(); o.material?.dispose(); });
  g.removeFromParent();
  stage.render();
}

/** Where a feature sits in the shown part: its centre (moved like the part) and its axis, turned to point out of the
 * end the camera sees (a detected axis may point either way; a peg's free end or a hole's near mouth is the one that
 * reads), and that end. */
function placement(stage, f) {
  const o = stage.part.offset;
  const centre = new THREE.Vector3(f.center[0] + o.x, f.center[1] + o.y, f.center[2] + o.z);
  const axis = new THREE.Vector3(...f.axis).normalize();
  stage.part.group.updateMatrixWorld(true);
  const world = (v) => v.clone().applyMatrix4(stage.part.group.matrixWorld);
  const a = centre.clone().addScaledVector(axis, f.length / 2), b = centre.clone().addScaledVector(axis, -f.length / 2);
  if (world(b).distanceTo(stage.camera.position) < world(a).distanceTo(stage.camera.position)) axis.negate();
  return { centre, axis, mouth: centre.clone().addScaledVector(axis, f.length / 2) };
}

/**
 * The ghost mate for feature ``f`` ({kind: hole | peg, diameter, axis, center, length}): a pin of ``partnerMm`` in a
 * hole, a sleeve of ``partnerMm`` round a peg, slid in along the axis (no motion when the user asked for less), and
 * the clearance ring at the mouth coloured by ``verdict`` (jam / tight / fits / loose).
 */
export function showMate(stage, f, partnerMm, verdict) {
  clearMate(stage);
  if (!stage.part) return;
  const g = layer(stage, "tol-mate");
  const { centre, axis, mouth } = placement(stage, f);
  const length = Math.max(f.length * 1.6, f.diameter * 1.5);
  const r = Math.max(partnerMm, 0.2) / 2;
  const geo = f.kind === "hole" ? new THREE.CylinderGeometry(r, r, length, 48) : new THREE.CylinderGeometry(r, r, length, 48, 1, true);
  const mate = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: css("--accent", "#2563EB"), transparent: true, opacity: 0.45,
    depthWrite: false, side: THREE.DoubleSide }));
  mate.quaternion.setFromUnitVectors(Y, axis);
  mate.renderOrder = 3;
  const [token, fallback] = VERDICT_COLOR[verdict] ?? VERDICT_COLOR.fits;
  const ring = new THREE.Mesh(new THREE.TorusGeometry(f.diameter / 2, Math.max(0.25, f.diameter * 0.04), 12, 64),
    new THREE.MeshBasicMaterial({ color: css(token, fallback) }));
  ring.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), axis);
  ring.position.copy(mouth);
  ring.renderOrder = 4;
  g.add(mate, ring);
  const end = centre.clone();
  const start = centre.clone().addScaledVector(axis, length);
  if (reducedMotion()) {
    mate.position.copy(end);
    stage.render();
    return;
  }
  const t0 = performance.now();
  const step = (now) => {
    if (!mate.parent) return; // cleared while sliding
    const k = Math.min(1, (now - t0) / 600);
    mate.position.lerpVectors(start, end, 1 - (1 - k) ** 3);
    stage.render();
    if (k < 1) requestAnimationFrame(step);
  };
  mate.position.copy(start);
  requestAnimationFrame(step);
}

export function clearMate(stage) { clear(stage, "tol-mate"); }

/** Orange circles at the size each feature prints (``printed_mm``), off the modelled size by ``factor`` × the
 * difference, with a thin grey circle at the modelled size to compare with. */
export function showPrinted(stage, features, factor) {
  clearPrinted(stage);
  if (!stage.part || !factor) return;
  const g = layer(stage, "tol-printed");
  const orange = css("--warn", "#B45309"), grey = css("--text-3", "#6B7280");
  const circle = (radius, color, f) => {
    const pts = Array.from({ length: 96 }, (_, i) => new THREE.Vector3(Math.cos((i / 96) * Math.PI * 2) * radius, Math.sin((i / 96) * Math.PI * 2) * radius, 0));
    const line = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color }));
    const { axis, mouth } = placement(stage, f);
    line.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), axis);
    line.position.copy(mouth).addScaledVector(axis, 0.05);
    line.renderOrder = 5;
    return line;
  };
  for (const f of features) {
    const printed = f.diameter + factor * (f.printed_mm - f.diameter);
    g.add(circle(f.diameter / 2, grey, f), circle(Math.max(printed, 0.1) / 2, orange, f));
  }
  stage.render();
}

export function clearPrinted(stage) { clear(stage, "tol-printed"); }

const vec = (a) => new THREE.Vector3(...a);

/**
 * The feeler gauge for a flat gap (set_gap's ``plane``: origin on face a, its normal, in-plane u / v and the overlap's
 * ranges). A slot (``kind`` "slot") gets a blade ``partnerMm`` thick sliding through between the faces; a tab gets two
 * slot walls ``partnerMm`` apart sliding over it. Coloured by ``verdict``; when it jams it stops at the mouth.
 */
export function showFeeler(stage, plane, kind, distanceMm, partnerMm, verdict) {
  clearFeeler(stage);
  if (!stage.part || !plane) return;
  const g = layer(stage, "tol-feeler");
  const o = stage.part.offset;
  const n = vec(plane.normal).normalize(), u = vec(plane.u).normalize(), v = vec(plane.v).normalize();
  const [u0, u1] = plane.u_range, [v0, v1] = plane.v_range;
  const spanU = Math.max(u1 - u0, 1), spanV = Math.max(v1 - v0, 1);
  const length = spanU + 6;
  // Middle of the gap (a slot: between the faces; a tab: in its middle, the plastic between them).
  const mid = vec(plane.origin).add(o).addScaledVector(u, (u0 + u1) / 2).addScaledVector(v, (v0 + v1) / 2)
    .addScaledVector(n, kind === "slot" ? distanceMm / 2 : -distanceMm / 2);
  const [token, fallback] = VERDICT_COLOR[verdict] ?? VERDICT_COLOR.fits;
  const material = new THREE.MeshStandardMaterial({ color: css(token, fallback), transparent: true, opacity: 0.6, depthWrite: false, side: THREE.DoubleSide });
  const basis = new THREE.Matrix4().makeBasis(u, v, n);
  const plate = (thick, offset) => {
    const m = new THREE.Mesh(new THREE.BoxGeometry(length, spanV * 0.9, Math.max(thick, 0.05)), material);
    m.quaternion.setFromRotationMatrix(basis);
    m.userData.offset = offset;
    m.renderOrder = 3;
    return m;
  };
  const wall = 1.2;
  const pieces = kind === "slot" ? [plate(partnerMm, 0)]
    : [plate(wall, partnerMm / 2 + wall / 2), plate(wall, -(partnerMm / 2 + wall / 2))];
  g.add(...pieces);
  // Slides in along u from outside; a jam stops it just at the mouth.
  const out = spanU / 2 + length / 2 + 2;
  const stopAt = verdict === "jam" ? spanU / 2 + length / 2 : 0;
  const place = (along) => { for (const p of pieces) p.position.copy(mid).addScaledVector(u, -along).addScaledVector(n, p.userData.offset); stage.render(); };
  if (reducedMotion()) { place(stopAt); return; }
  const t0 = performance.now();
  const step = (now) => {
    if (!pieces[0].parent) return;
    const k = Math.min(1, (now - t0) / 700);
    place(out + (stopAt - out) * (1 - (1 - k) ** 3));
    if (k < 1) requestAnimationFrame(step);
  };
  place(out);
  requestAnimationFrame(step);
}

export function clearFeeler(stage) { clear(stage, "tol-feeler"); }

/** A dark dot at the tightest spot against a mating part (part coordinates): dark, so it stands out on the map's red. */
export function showTight(stage, at, sizeMm) {
  clearTight(stage);
  if (!stage.part || !at) return;
  const g = layer(stage, "tol-tight");
  const dot = new THREE.Mesh(new THREE.SphereGeometry(Math.max(sizeMm, 0.6), 24, 16), new THREE.MeshBasicMaterial({ color: css("--text", "#111827"), depthTest: false }));
  dot.position.copy(vec(at).add(stage.part.offset));
  dot.renderOrder = 6;
  g.add(dot);
  stage.render();
}

export function clearTight(stage) { clear(stage, "tol-tight"); }
