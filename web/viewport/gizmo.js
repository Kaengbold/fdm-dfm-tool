// Direction gizmo for loads (T-542, plan §8 point 2): at the clicked spot, a violet arrow for the force, the axis triad,
// and two rotation rings like a slicer's rotate tool to tilt the arrow (15° steps, Shift = free) with a read-only
// angle label while dragging. One-click directions: Push in / Pull out (along the face normal) and ±X ±Y ±Z. No angle
// input boxes. Keyboard (§1.8): arrows tilt 15° (Shift 5°), P / O push / pull, X / Y / Z axes, Enter done, Esc cancel.
import * as THREE from "three";

// ---------- pure math (unit-tested under Node) ----------

/** ``deg`` to the nearest ``step`` (free = unchanged). */
export const snapAngle = (deg, step = 15, free = false) => (free ? deg : Math.round(deg / step) * step);

const norm = ([x, y, z]) => { const l = Math.hypot(x, y, z) || 1; return [x / l, y / l, z / l]; };
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

/** Two unit axes at right angles to ``base`` (the rings turn the arrow around them), stable for any base. */
export function ringAxes(base) {
  const b = norm(base);
  const helper = Math.abs(b[2]) < 0.9 ? [0, 0, 1] : [1, 0, 0];
  const u = norm(cross(helper, b));
  const v = norm(cross(b, u));
  return { u, v };
}

/** Rotate ``p`` around unit ``axis`` by ``deg`` (Rodrigues). */
export function rotate(p, axis, deg) {
  const a = (deg * Math.PI) / 180, c = Math.cos(a), s = Math.sin(a);
  const k = norm(axis), kd = dot(k, p), kx = cross(k, p);
  return [0, 1, 2].map((i) => p[i] * c + kx[i] * s + k[i] * kd * (1 - c));
}

/** The direction after tilting ``base`` by ``tiltU`` around the first ring's axis, then ``tiltV`` around the second. */
export function tiltedDirection(base, tiltU, tiltV) {
  const { u, v } = ringAxes(base);
  return norm(rotate(rotate(norm(base), u, tiltU), v, tiltV));
}

/** Angle between two directions in degrees (for the label). */
export const angleBetween = (a, b) => (Math.acos(Math.max(-1, Math.min(1, dot(norm(a), norm(b))))) * 180) / Math.PI;

/** Base direction for a button: push = into the face (−normal), pull = out of it, axes as given. */
export function baseDirection(mode, normal) {
  const n = norm(normal);
  return {
    push: n.map((x) => -x), pull: n, "+x": [1, 0, 0], "-x": [-1, 0, 0], "+y": [0, 1, 0], "-y": [0, -1, 0], "+z": [0, 0, 1], "-z": [0, 0, -1],
  }[mode];
}

// ---------- the gizmo ----------

const RING_PX = 70; // ring radius on screen
const ARROW_PX = 120; // arrow length on screen

/**
 * Open the gizmo on ``stage`` at ``point`` (part coordinates) with the face ``normal``. ``labels``: translated texts
 * {push, pull, done, cancel, title}. Calls onChange(direction) as it turns, onDone(direction) on Enter / Done,
 * onCancel() on Esc. Returns {close, setDirection, direction(), element}.
 */
export function openGizmo(stage, { point, normal, direction = null, labels = {}, onChange = () => {}, onDone = () => {}, onCancel = () => {} }) {
  const off = stage.part?.offset ?? new THREE.Vector3();
  const anchor = new THREE.Vector3(...point).add(off);
  let mode = "push", tiltU = 0, tiltV = 0;
  let base = baseDirection(mode, normal);
  if (direction) { mode = "custom"; base = norm(direction); }
  const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  const violet = new THREE.Color(css("--ov-load", "#9333EA"));

  // 3D parts: one group at the anchor, scaled each frame so it keeps its size on screen.
  const group = new THREE.Group();
  group.position.copy(anchor);
  group.renderOrder = 10;
  const mat = (color, opacity = 1) => new THREE.MeshBasicMaterial({ color, transparent: opacity < 1, opacity, depthTest: false });
  const shaft = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.035, 0.82, 12), mat(violet));
  const head = new THREE.Mesh(new THREE.ConeGeometry(0.09, 0.18, 16), mat(violet));
  const arrow = new THREE.Group();
  arrow.add(shaft, head);
  const triad = new THREE.Group();
  for (const [dir, color] of [[[1, 0, 0], "#DC2626"], [[0, 1, 0], "#16A34A"], [[0, 0, 1], "#2563EB"]]) {
    const g = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(...dir).multiplyScalar(0.32)]);
    triad.add(new THREE.Line(g, new THREE.LineBasicMaterial({ color, depthTest: false })));
  }
  const ringGeo = new THREE.TorusGeometry(1, 0.012, 8, 96);
  const hitGeo = new THREE.TorusGeometry(1, 0.08, 6, 64); // easier to grab than the drawn ring
  const rings = ["u", "v"].map((axisName, i) => {
    const ring = new THREE.Mesh(ringGeo, mat(violet, i ? 0.45 : 0.7));
    const hit = new THREE.Mesh(hitGeo, new THREE.MeshBasicMaterial({ visible: false }));
    hit.userData.axisName = axisName;
    const holder = new THREE.Group();
    holder.add(ring, hit);
    return { holder, ring, hit, axisName };
  });
  const anchorDot = new THREE.Mesh(new THREE.SphereGeometry(0.035, 16, 12), mat(violet));
  group.add(arrow, triad, anchorDot, ...rings.map((r) => r.holder));
  stage.scene.add(group);

  const toScreen = (v) => {
    const r = stage.canvas.getBoundingClientRect(), p = v.clone().project(stage.camera);
    return { x: (p.x + 1) / 2 * r.width, y: (1 - p.y) / 2 * r.height };
  };
  function screenScale() {
    // World units per screen pixel at the anchor.
    const dist = stage.camera.position.distanceTo(anchor);
    const h = stage.canvas.getBoundingClientRect().height || 1;
    return (2 * dist * Math.tan(THREE.MathUtils.degToRad(stage.camera.fov / 2))) / h;
  }
  // Keep the size on screen when the camera moves.
  const onCamera = () => layout();
  stage.controls.addEventListener("change", onCamera);

  function current() { return tiltedDirection(base, tiltU, tiltV); }

  function layout() {
    const d = new THREE.Vector3(...current());
    const n = new THREE.Vector3(...norm(normal));
    const k = screenScale();
    group.scale.setScalar(1);
    const len = ARROW_PX * k;
    // The arrow stays outside the part: a push ends at the spot, a pull starts there.
    const pushing = d.dot(n) < 0;
    const tail = pushing ? d.clone().multiplyScalar(-len) : new THREE.Vector3();
    arrow.position.copy(tail);
    arrow.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), d);
    arrow.scale.setScalar(len);
    shaft.position.set(0, 0.41, 0);
    head.position.set(0, 0.91, 0);
    triad.scale.setScalar(len);
    anchorDot.scale.setScalar(len * 0.5);
    const { u, v } = ringAxes(base);
    const R = RING_PX * k;
    for (const r of rings) {
      const axis = new THREE.Vector3(...(r.axisName === "u" ? u : v));
      r.holder.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), axis); // the torus lies in the plane ⟂ axis
      r.holder.scale.setScalar(R);
    }
    placeHtml();
  }

  // HTML: the one-click direction row and the angle label, in the stage overlay.
  const row = document.createElement("div");
  row.className = "gizmo-row";
  row.setAttribute("role", "toolbar");
  row.setAttribute("aria-label", labels.title ?? "Direction");
  const buttons = {};
  for (const [key, text] of [["push", labels.push ?? "Push in"], ["pull", labels.pull ?? "Pull out"], ["x", "±X"], ["y", "±Y"], ["z", "±Z"]]) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = text;
    b.dataset.key = key;
    b.addEventListener("click", () => choose(key));
    row.append(b);
    buttons[key] = b;
  }
  const label = Object.assign(document.createElement("div"), { className: "gizmo-angle", hidden: true });
  stage.el.append(row, label);

  function placeHtml() {
    const s = toScreen(anchor);
    row.style.left = `${Math.round(s.x - row.offsetWidth / 2)}px`;
    row.style.top = `${Math.round(s.y + RING_PX + 16)}px`;
  }
  function choose(key) {
    if (["x", "y", "z"].includes(key)) {
      const plus = `+${key}`, minus = `-${key}`;
      mode = mode === plus ? minus : plus; // a second click flips the sign
    } else mode = key;
    base = baseDirection(mode, normal);
    tiltU = tiltV = 0;
    refresh();
  }
  function refresh() {
    layout();
    for (const [key, b] of Object.entries(buttons)) b.classList.toggle("is-on", mode === key || mode === `+${key}` || mode === `-${key}`);
    buttons.x.textContent = mode === "-x" ? "−X" : "+X";
    buttons.y.textContent = mode === "-y" ? "−Y" : "+Y";
    buttons.z.textContent = mode === "-z" ? "−Z" : "+Z";
    stage.render();
    onChange(current());
  }
  function showAngle() {
    const a = Math.round(angleBetween(base, current()));
    label.textContent = `${a}°`;
    const s = toScreen(anchor);
    label.style.left = `${Math.round(s.x + RING_PX + 8)}px`;
    label.style.top = `${Math.round(s.y - RING_PX - 8)}px`;
    label.hidden = false;
  }

  // Ring dragging: the angle of the pointer around the ring's axis, from where the drag started.
  const ray = new THREE.Raycaster();
  let drag = null;
  const ndc = (e) => {
    const r = stage.canvas.getBoundingClientRect();
    return new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  };
  function angleOnPlane(e, axis) {
    ray.setFromCamera(ndc(e), stage.camera);
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(axis, anchor);
    const hit = ray.ray.intersectPlane(plane, new THREE.Vector3());
    if (!hit) return null;
    const p = hit.sub(anchor);
    const ref = new THREE.Vector3(...ringAxes(base)[drag?.axisName === "u" ? "v" : "u"]);
    const ref2 = new THREE.Vector3().crossVectors(axis, ref);
    return THREE.MathUtils.radToDeg(Math.atan2(p.dot(ref2), p.dot(ref)));
  }
  function down(e) {
    ray.setFromCamera(ndc(e), stage.camera);
    const hit = ray.intersectObjects(rings.map((r) => r.hit), false)[0];
    if (!hit) return;
    e.preventDefault();
    e.stopPropagation();
    const axisName = hit.object.userData.axisName;
    const axis = new THREE.Vector3(...ringAxes(base)[axisName]);
    drag = { axisName, axis, start: null, tilt0: axisName === "u" ? tiltU : tiltV };
    drag.start = angleOnPlane(e, axis);
    stage.controls.enabled = false;
    stage.canvas.setPointerCapture?.(e.pointerId);
    showAngle();
  }
  function move(e) {
    if (!drag) return;
    const a = angleOnPlane(e, drag.axis);
    if (a == null || drag.start == null) return;
    const t = snapAngle(drag.tilt0 + (a - drag.start), 15, e.shiftKey);
    if (drag.axisName === "u") tiltU = t; else tiltV = t;
    showAngle();
    refresh();
  }
  function up() {
    if (!drag) return;
    drag = null;
    stage.controls.enabled = true;
    label.hidden = true;
  }
  stage.canvas.addEventListener("pointerdown", down, true);
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);

  function key(e) {
    if (e.target.closest?.("input, textarea, [contenteditable]")) return;
    const step = e.shiftKey ? 5 : 15;
    const k = e.key;
    let used = true;
    if (k === "ArrowLeft") tiltU -= step;
    else if (k === "ArrowRight") tiltU += step;
    else if (k === "ArrowUp") tiltV += step;
    else if (k === "ArrowDown") tiltV -= step;
    else if (k === "p" || k === "P") { choose("push"); return e.preventDefault(); }
    else if (k === "o" || k === "O") { choose("pull"); return e.preventDefault(); }
    else if (["x", "y", "z", "X", "Y", "Z"].includes(k)) { choose(k.toLowerCase()); return e.preventDefault(); }
    else if (k === "Enter") { e.preventDefault(); const d = current(); close(); onDone(d); return; }
    else if (k === "Escape") { e.preventDefault(); e.stopPropagation(); close(); onCancel(); return; }
    else used = false;
    if (used) {
      e.preventDefault();
      showAngle();
      refresh();
      clearTimeout(key.timer);
      key.timer = setTimeout(() => { label.hidden = true; }, 900);
    }
  }
  window.addEventListener("keydown", key, true);

  function close() {
    stage.controls.removeEventListener("change", onCamera);
    stage.canvas.removeEventListener("pointerdown", down, true);
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("keydown", key, true);
    stage.controls.enabled = true;
    stage.scene.remove(group);
    for (const r of rings) r.ring.material.dispose();
    ringGeo.dispose(); hitGeo.dispose();
    row.remove(); label.remove();
    stage.render();
  }

  refresh();
  return {
    close,
    element: row,
    direction: current,
    setDirection(d) { mode = "custom"; base = norm(d); tiltU = tiltV = 0; refresh(); },
    _debug: { rings, anchor, toScreen, get tilts() { return [tiltU, tiltV]; } },
  };
}
