// Tolerances & fits in 3D (T-570, plan §15.2): the ghost mate (a see-through pin slides into a hole, or a sleeve
// over a peg) with a clearance ring at the mouth coloured by what will happen (jams / tight / fits / loose), and the
// printed-vs-CAD overlay: an orange circle at the size each hole or peg really prints, exaggerated ×5 or ×10 so a
// tenth of a millimetre shows. Everything lives in the part's group (so it turns with the part) in part coordinates.
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
