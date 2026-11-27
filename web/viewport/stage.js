// The new UI's 3D stage (T-531; picking, gizmo and overlays come with T-540…): the part on the printer's bed,
// orbit controls with Z up, the camera bar (Iso, Top, Front, Fit) and small thumbnails for the sample list.
// Colours come from the style A tokens, so light and dark both work (§1.5).
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const token = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

/** Part colours per theme (the approved mockups' renders, design/mocks/render.html). */
function palette() {
  const dark = document.documentElement.dataset.theme === "dark";
  return dark
    ? { part: "#8C95A3", edge: "#0F1216", bed: "#242A33", grid: "#4B5563" }
    : { part: "#C9CED6", edge: "#4B5563", bed: "#DCE1E7", grid: "#9AA3AF" };
}

const FIELD_BANDS = 10;
/** Banded rainbow for results (blue low … red high), the same scale as the old viewer's FEA view. */
export function fieldColor(v, c = new THREE.Color()) {
  const top = FIELD_BANDS - 1;
  const band = Math.min(top, Math.max(0, Math.floor(v * FIELD_BANDS)));
  return c.setHSL((0.66 * (top - band)) / top, 0.85, 0.5);
}

/** A binary or ASCII STL as a flat Float32Array of triangle corners (9 per triangle). */
export function parseStl(buffer) {
  const view = new DataView(buffer);
  const n = buffer.byteLength >= 84 ? view.getUint32(80, true) : 0;
  if (84 + n * 50 === buffer.byteLength) {
    const out = new Float32Array(n * 9);
    for (let i = 0; i < n; i++) for (let k = 0; k < 9; k++) out[i * 9 + k] = view.getFloat32(84 + i * 50 + 12 + k * 4, true);
    return out;
  }
  const nums = new TextDecoder().decode(buffer).match(/vertex\s+(\S+)\s+(\S+)\s+(\S+)/g) ?? [];
  return Float32Array.from(nums.flatMap((v) => v.split(/\s+/).slice(1).map(Number)));
}

const MAX_EDGE_TRIANGLES = 20_000;

/** Dispose every geometry and material in a group so replaced parts, beds and thumbnails don't leak GPU memory. */
function disposeGroup(group) {
  if (!group) return;
  group.traverse((obj) => {
    obj.geometry?.dispose?.();
    if (Array.isArray(obj.material)) obj.material.forEach((m) => m?.dispose?.());
    else obj.material?.dispose?.();
  });
  group.clear();
}

function partGroup(triangles, colors, { edges = true } = {}) {
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(triangles, 3));
  geo.computeVertexNormals();
  geo.computeBoundingBox();
  const bb = geo.boundingBox;
  // Centred on the bed, resting on it; ``offset`` maps part coordinates (the engine's) to shown ones.
  const offset = new THREE.Vector3(-(bb.min.x + bb.max.x) / 2, -(bb.min.y + bb.max.y) / 2, -bb.min.z);
  geo.translate(offset.x, offset.y, offset.z);
  geo.computeBoundingBox();
  geo.computeBoundingSphere();
  // Per-vertex colours, so a picker can tint face regions (viewport/picker.js); all the part colour to start.
  const base = new THREE.Color(colors.part);
  const col = new Float32Array(triangles.length);
  for (let i = 0; i < col.length; i += 3) { col[i] = base.r; col[i + 1] = base.g; col[i + 2] = base.b; }
  geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
  const group = new THREE.Group();
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.62, metalness: 0.02 }));
  group.add(mesh);
  let edgeLines = null;
  // EdgesGeometry is O(triangles) and heavy on organic/scanned meshes; keep it for CAD-sized parts where crisp edges help.
  if (edges && triangles.length / 9 <= MAX_EDGE_TRIANGLES) {
    edgeLines = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 28),
      new THREE.LineBasicMaterial({ color: colors.edge, transparent: true, opacity: 0.55 }));
    group.add(edgeLines);
  }
  return { group, geo, mesh, edges: edgeLines, offset, base, orientCache: new Map() };
}

/** Placement [x, y, z] that centres a rotated part on the bed with its lowest vertex at z = 0, cached per rotation. */
function rotatedPlacement(part, quat, key, exactXY = false) {
  const hit = part.orientCache.get(key);
  if (hit) return hit;
  const e = new THREE.Matrix4().makeRotationFromQuaternion(quat).elements;
  const m00 = e[0], m01 = e[4], m02 = e[8];
  const m10 = e[1], m11 = e[5], m12 = e[9];
  const m20 = e[2], m21 = e[6], m22 = e[10];
  const arr = part.geo.attributes.position.array;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, minZ = Infinity;
  if (exactXY) {
    for (let i = 0; i < arr.length; i += 3) {
      const x = arr[i], y = arr[i + 1], z = arr[i + 2];
      const rx = m00 * x + m01 * y + m02 * z;
      const ry = m10 * x + m11 * y + m12 * z;
      const rz = m20 * x + m21 * y + m22 * z;
      if (rx < minX) minX = rx;
      if (rx > maxX) maxX = rx;
      if (ry < minY) minY = ry;
      if (ry > maxY) maxY = ry;
      if (rz < minZ) minZ = rz;
    }
  } else {
    const { min, max } = part.geo.boundingBox;
    for (const x of [min.x, max.x]) {
      for (const y of [min.y, max.y]) {
        for (const z of [min.z, max.z]) {
          const rx = m00 * x + m01 * y + m02 * z;
          const ry = m10 * x + m11 * y + m12 * z;
          if (rx < minX) minX = rx;
          if (rx > maxX) maxX = rx;
          if (ry < minY) minY = ry;
          if (ry > maxY) maxY = ry;
        }
      }
    }
    // Exact min z in one tight pass so the part sits flush on the bed even for diagonal orientations.
    for (let i = 0; i < arr.length; i += 3) {
      const rz = m20 * arr[i] + m21 * arr[i + 1] + m22 * arr[i + 2];
      if (rz < minZ) minZ = rz;
    }
  }
  const pos = [-(minX + maxX) / 2, -(minY + maxY) / 2, -minZ];
  part.orientCache.set(key, pos);
  return pos;
}

function lights(scene) {
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8a93a0, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(-60, -90, 140);
  scene.add(sun);
  const rim = new THREE.DirectionalLight(0xffffff, 0.5);
  rim.position.set(80, 60, 40);
  scene.add(rim);
}

export class Stage {
  constructor(el) {
    this.el = el;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.setClearColor(0x000000, 0); // the stage gradient (CSS) shows through
    this.canvas = this.renderer.domElement;
    this.canvas.className = "stage-canvas";
    this.scene = new THREE.Scene();
    lights(this.scene);
    this.camera = new THREE.PerspectiveCamera(30, 1, 0.5, 10000);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this._dragging = false;
    this.frame = 0;
    this._inTick = false;
    const tick = () => {
      this.frame = 0;
      this._inTick = true;
      const moved = this.controls.update();
      this._inTick = false;
      if (moved || this._dragging) this.frame = requestAnimationFrame(tick);
    };
    this._startLoop = () => {
      if (!this.frame) this.frame = requestAnimationFrame(tick);
    };
    this.controls.addEventListener("start", () => { this._dragging = true; this._startLoop(); });
    this.controls.addEventListener("end", () => { this._dragging = false; this._startLoop(); });
    this.controls.addEventListener("change", () => {
      this.render();
      if (!this._inTick) this._startLoop();
    });
    this.bed = new THREE.Group();
    this.scene.add(this.bed);
    this.part = null;
    this.oriented = null;
    this._orientKey = null;
    this._w = 0;
    this._h = 0;
    this.bedSize = [256, 256];
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.attach(el);
    this.setBed(this.bedSize);
    this.themeObserver = new MutationObserver(() => this.recolor());
    this.themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }

  /** Move the canvas into another stage element (the shell rebuilds its panels, the 3D view stays). */
  attach(el) {
    if (this.el !== el || this.canvas.parentElement !== el) {
      this.resizeObserver.disconnect();
      this.el = el;
      el.prepend(this.canvas);
      this.resizeObserver.observe(el);
    }
    this.resize();
  }

  resize() {
    const { width, height } = this.el.getBoundingClientRect();
    if (!width || !height) return;
    const w = Math.round(width), h = Math.round(height);
    if (w === this._w && h === this._h) return;
    this._w = w;
    this._h = h;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.render();
  }

  render() { this.renderer.render(this.scene, this.camera); }

  /** Square PNG data URL from the existing renderer (avoids a second WebGL context and rebuilding the mesh). */
  snapshot(size = 112) {
    if (!this.part || !this.canvas.width || !this.canvas.height) return null;
    const bedVis = this.bed.visible;
    this.bed.visible = false;
    this.renderer.render(this.scene, this.camera);
    const out = document.createElement("canvas");
    out.width = size;
    out.height = size;
    const sw = this.canvas.width, sh = this.canvas.height;
    const side = Math.min(sw, sh);
    out.getContext("2d").drawImage(this.canvas, (sw - side) / 2, (sh - side) / 2, side, side, 0, 0, size, size);
    this.bed.visible = bedVis;
    this.renderer.render(this.scene, this.camera);
    return out.toDataURL("image/png");
  }

  setBed([w, d]) {
    this.bedSize = [w, d];
    disposeGroup(this.bed);
    const c = palette();
    const plate = new THREE.Mesh(new THREE.PlaneGeometry(w, d), new THREE.MeshStandardMaterial({ color: c.bed, roughness: 0.9 }));
    plate.position.z = -0.05;
    const grid = new THREE.GridHelper(Math.max(w, d), Math.round(Math.max(w, d) / 10), c.grid, c.grid);
    grid.rotateX(Math.PI / 2);
    grid.material.transparent = true;
    grid.material.opacity = 0.35;
    this.bed.add(plate, grid);
    this.render();
  }

  /** Show ``triangles`` (Float32Array, part coordinates, as loaded) standing on the bed, and frame it. */
  showPart(triangles) {
    this.hideField();
    this.clearGhosts();
    this.clearMarker();
    if (this.part) { this.scene.remove(this.part.group); disposeGroup(this.part.group); }
    this.part = partGroup(triangles, palette());
    this.oriented = null;
    this._orientKey = null;
    this.scene.add(this.part.group);
    if (this.printUp) this.orient(this.printUp); // an edited part comes back as loaded: stand it as it prints again
    this.view("iso");
  }

  /** Colour every triangle: ``colorOf(triangle index)`` → THREE.Color, or null for the part colour. */
  paint(colorOf, triangles = null) {
    if (!this.part) return;
    const attr = this.part.geo.attributes.color;
    const base = this.part.base;
    const list = triangles ?? { length: attr.count / 3 };
    for (let k = 0; k < list.length; k++) {
      const t = triangles ? triangles[k] : k;
      const c = colorOf(t) ?? base;
      for (let v = 0; v < 3; v++) attr.setXYZ(t * 3 + v, c.r, c.g, c.b);
    }
    attr.needsUpdate = true;
    this.render();
  }

  /** Show the part in a build orientation: ``up`` (part coordinates) points up, resting on the bed, centred. The
   * hover picker maps points for the part as loaded, so steps that pick call ``orient(null)`` to go back. */
  orient(up) {
    if (!this.part) return;
    const identity = !up || (Math.abs(up[0]) < 1e-6 && Math.abs(up[1]) < 1e-6 && up[2] > 0);
    const key = identity ? "id" : `u:${up[0]},${up[1]},${up[2]}`;
    if (this._orientKey === key) return;
    const g = this.part.group;
    g.quaternion.identity();
    g.position.set(0, 0, 0);
    if (!identity) {
      g.quaternion.setFromUnitVectors(new THREE.Vector3(...up).normalize(), new THREE.Vector3(0, 0, 1));
      const [px, py, pz] = rotatedPlacement(this.part, g.quaternion, key, false);
      g.position.set(px, py, pz);
    }
    this.oriented = up ?? null;
    this._orientKey = key;
    g.updateMatrixWorld(true);
    this.render();
  }

  /** Back to the resting pose: as it prints once an option is chosen (``printUp``, set by main.js, T-585), else as
   * loaded. Overlays that turned the part themselves (the heat view) call this when they go. */
  rest() { this.orient(this.printUp ?? null); }

  /** Turn the part with the engine's own rotation (3 × 3 rows: oriented = part @ rotation.T), resting on the bed and
   * centred like the engine's oriented frame, so overlays in that frame (heat spots, G-code lines) sit on it. */
  orientBy(rotation) {
    if (!this.part) return;
    const [a, b, c] = rotation;
    const key = `m:${a.join(",")};${b.join(",")};${c.join(",")}`;
    if (this._orientKey === key) return;
    const g = this.part.group;
    g.quaternion.setFromRotationMatrix(new THREE.Matrix4().set(a[0], a[1], a[2], 0, b[0], b[1], b[2], 0, c[0], c[1], c[2], 0, 0, 0, 0, 1));
    const [px, py, pz] = rotatedPlacement(this.part, g.quaternion, key, true);
    g.position.set(px, py, pz);
    this.oriented = rotation;
    this._orientKey = key;
    g.updateMatrixWorld(true);
    this.render();
  }

  /** X-ray: see through the part (holds and loads behind it stay visible). */
  setXray(on) {
    if (!this.part) return;
    const m = this.part.mesh.material;
    m.transparent = on;
    m.opacity = on ? 0.35 : 1;
    m.depthWrite = !on;
    m.needsUpdate = true;
    this.xray = on;
    this.render();
  }

  clearPart() {
    this.hideField();
    this.clearGhosts();
    this.clearMarker();
    if (!this.part) return;
    this.scene.remove(this.part.group);
    disposeGroup(this.part.group);
    this.part = null;
    this.oriented = null;
    this._orientKey = null;
    this.render();
  }

  /** A result on the part (T-560): the engine's refined surface ``triangles`` (part coordinates) coloured per corner by
   * ``values`` from 0 to ``max`` in bands, like FEA software. ``disp`` (3 per corner, mm) lets ``setDeformation`` show
   * the curled shape. Lives in the part's group, so it turns with the part; hides the plain part until ``hideField``. */
  showField({ triangles, values, disp = null, max }) {
    this.hideField();
    if (!this.part) return;
    const { offset } = this.part;
    const pos = new Float32Array(triangles.length);
    for (let i = 0; i < pos.length; i += 3) { pos[i] = triangles[i] + offset.x; pos[i + 1] = triangles[i + 1] + offset.y; pos[i + 2] = triangles[i + 2] + offset.z; }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    const col = new Float32Array(pos.length);
    const c = new THREE.Color();
    for (let i = 0; i < values.length; i++) { fieldColor(values[i] / (max || 1), c); col.set([c.r, c.g, c.b], i * 3); }
    geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
    geo.computeVertexNormals();
    const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.8, side: THREE.DoubleSide }));
    this.field = { mesh, base: pos.slice(), disp };
    for (const child of this.part.group.children) child.visible = false;
    this.part.group.add(mesh);
    this.render();
  }

  hideField() {
    if (!this.field) return;
    this.field.mesh.removeFromParent();
    this.field.mesh.geometry.dispose();
    this.field.mesh.material.dispose();
    this.field = null;
    if (this.part) for (const child of this.part.group.children) child.visible = true;
    this.render();
  }

  /** Bend the shown result by ``scale`` × its displacement (1 = true size; FEA software exaggerates it). */
  setDeformation(scale) {
    if (!this.field?.disp) return;
    const attr = this.field.mesh.geometry.attributes.position;
    const { base, disp } = this.field;
    for (let i = 0; i < base.length; i++) attr.array[i] = base[i] + scale * disp[i];
    attr.needsUpdate = true;
    this.field.mesh.geometry.computeVertexNormals();
    this.render();
  }

  /** The part's triangles in part coordinates (a copy: the shown ones are moved onto the bed). */
  partTriangles() {
    if (!this.part) return null;
    const out = this.part.geo.attributes.position.array.slice();
    const { x, y, z } = this.part.offset;
    for (let i = 0; i < out.length; i += 3) { out[i] -= x; out[i + 1] -= y; out[i + 2] -= z; }
    return out;
  }

  /** Pending design changes as ghosts (T-561): ``list`` of {mesh: Float32Array in part coordinates, op: add | cut};
   * plastic added shows green, plastic cut away red, both see-through so the part stays readable. */
  showGhosts(list) {
    this.clearGhosts();
    if (!this.part || !list.length) return;
    const { x, y, z } = this.part.offset;
    const css = getComputedStyle(document.documentElement);
    this.ghosts = list.map(({ mesh, op }) => {
      const pos = mesh.slice();
      for (let i = 0; i < pos.length; i += 3) { pos[i] += x; pos[i + 1] += y; pos[i + 2] += z; }
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
      geo.computeVertexNormals();
      const color = css.getPropertyValue(op === "cut" ? "--bad" : "--good").trim() || (op === "cut" ? "#d33" : "#2a2");
      const ghost = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color, transparent: true, opacity: 0.55, depthWrite: false, side: THREE.DoubleSide }));
      ghost.renderOrder = 2;
      this.part.group.add(ghost);
      return ghost;
    });
    this.render();
  }

  clearGhosts() {
    for (const g of this.ghosts ?? []) { g.removeFromParent(); g.geometry.dispose(); g.material.dispose(); }
    this.ghosts = [];
    this.render();
  }

  /** Highlight one problem's bounding box on the part (T-613): ``corners`` in part coordinates, ``severity`` bad|warn|info. */
  showMarker(corners, severity = "warn") {
    this.clearMarker();
    if (!this.part || !corners?.length) return;
    const { x, y, z } = this.part.offset;
    const box = new THREE.Box3();
    for (const c of corners) box.expandByPoint(new THREE.Vector3(c[0] + x, c[1] + y, c[2] + z));
    const size = box.getSize(new THREE.Vector3());
    const pad = Math.max(0.6, size.length() * 0.04);
    const sx = Math.max(size.x + pad, 1.5), sy = Math.max(size.y + pad, 1.5), sz = Math.max(size.z + pad, 1.5);
    const centre = box.getCenter(new THREE.Vector3());
    const css = getComputedStyle(document.documentElement);
    const tok = severity === "bad" ? "--bad" : severity === "info" ? "--accent" : "--warn";
    const color = css.getPropertyValue(tok).trim() || (severity === "bad" ? "#DC2626" : "#D97706");
    const boxGeo = new THREE.BoxGeometry(sx, sy, sz);
    const fill = new THREE.Mesh(boxGeo, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.14, depthWrite: false }));
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(boxGeo), new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.9, depthTest: false }));
    const group = new THREE.Group();
    group.position.copy(centre);
    group.renderOrder = 5;
    group.add(fill, edges);
    this.marker = { group, geos: [boxGeo, edges.geometry], mats: [fill.material, edges.material] };
    this.part.group.add(group);
    this.render();
  }

  clearMarker() {
    if (!this.marker) return;
    this.marker.group.removeFromParent();
    for (const g of this.marker.geos) g.dispose();
    for (const m of this.marker.mats) m.dispose();
    this.marker = null;
    this.render();
  }

  /** Point the camera at ``corners`` (part coordinates), close enough to see that one feature (T-561). */
  frameBox(corners) {
    if (!this.part || !corners?.length) return;
    this.part.group.updateMatrixWorld(true);
    const { x, y, z } = this.part.offset;
    const box = new THREE.Box3();
    for (const c of corners) box.expandByPoint(new THREE.Vector3(c[0] + x, c[1] + y, c[2] + z).applyMatrix4(this.part.group.matrixWorld));
    const centre = box.getCenter(new THREE.Vector3());
    // Keep at least 35 % of the part radius in view so tiny features (e.g. a 0.3 mm² corner) don't zoom into a featureless grey wall (T-617).
    const minR = Math.max(4, (this.part.geo.boundingSphere?.radius ?? 0) * 0.35);
    const r = Math.max(box.getSize(new THREE.Vector3()).length() / 2, minR);
    const d = this.camera.position.clone().sub(this.controls.target).normalize();
    const dist = (r / Math.sin(THREE.MathUtils.degToRad(15))) * 1.3;
    this.controls.target.copy(centre);
    this.camera.position.copy(centre).addScaledVector(d, dist);
    this.controls.update();
    this.render();
  }

  /** Camera presets for the camera bar. */
  view(name) {
    const box = this.part?.geo.boundingBox ?? new THREE.Box3(new THREE.Vector3(-30, -30, 0), new THREE.Vector3(30, 30, 30));
    const size = box.getSize(new THREE.Vector3());
    const centre = box.getCenter(new THREE.Vector3());
    const dist = Math.max(size.length() / 2, 10) / Math.sin(THREE.MathUtils.degToRad(15)) * 1.1;
    const dir = { top: [0, -0.001, 1], front: [0, -1, 0.15], iso: [0.62, -0.78, 0.55], under: [0.62, -0.78, -0.35], fit: null }[name] ?? null;
    const d = dir ? new THREE.Vector3(...dir).normalize() : this.camera.position.clone().sub(this.controls.target).normalize();
    this.controls.target.copy(centre);
    this.camera.position.copy(centre).addScaledVector(d, dist);
    this.camera.near = dist / 100;
    this.camera.far = dist * 20;
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.render();
  }

  recolor() {
    this.setBed(this.bedSize);
    if (this.part) {
      const c = palette();
      this.part.base.set(c.part);
      this.part.edges?.material.color.set(c.edge);
      this.onRecolor?.(); // a picker re-tints its regions on the new base colour
      if (!this.onRecolor) this.paint(() => null);
    }
    this.render();
  }
}

let thumbRenderer = null;
/** A small picture of ``triangles`` for lists (data URL), drawn by one shared off-screen renderer. */
export function thumbnail(triangles, size = 112) {
  thumbRenderer ??= new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
  thumbRenderer.setPixelRatio(2);
  thumbRenderer.setSize(size, size, false);
  thumbRenderer.setClearColor(0x000000, 0);
  const scene = new THREE.Scene();
  lights(scene);
  const { group, geo } = partGroup(triangles, palette(), { edges: false });
  scene.add(group);
  const box = geo.boundingBox, s = box.getSize(new THREE.Vector3()), c = box.getCenter(new THREE.Vector3());
  const cam = new THREE.PerspectiveCamera(30, 1, 0.1, 10000);
  cam.up.set(0, 0, 1);
  const dist = (s.length() / 2) / Math.sin(THREE.MathUtils.degToRad(15));
  cam.position.copy(c).add(new THREE.Vector3(0.62, -0.78, 0.55).normalize().multiplyScalar(dist));
  cam.lookAt(c);
  thumbRenderer.render(scene, cam);
  const url = thumbRenderer.domElement.toDataURL("image/png");
  disposeGroup(group);
  return url;
}

/** Camera bar element (Iso, Top, Front, Fit), bottom-left of the stage (§1.6). */
export function cameraBar(stage, labels) {
  const bar = document.createElement("div");
  bar.className = "stage-camera";
  bar.setAttribute("role", "toolbar");
  bar.setAttribute("aria-label", labels.title);
  for (const name of ["iso", "top", "front", "fit"]) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = labels[name];
    b.addEventListener("click", () => {
      stage.view(name);
      bar.querySelectorAll("button").forEach((x) => x.classList.toggle("is-on", x === b && name !== "fit"));
    });
    if (name === "iso") b.classList.add("is-on");
    bar.append(b);
  }
  return bar;
}
