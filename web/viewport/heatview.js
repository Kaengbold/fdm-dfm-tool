// Heat overlay of the simulation step (T-478): the heat model's spots as small blocks on the oriented part (window
// map or importance), the G-code's lines coloured by how their speed changed with markers where the fan changes,
// a layer limit for playback, and a click on a spot. Points come in the oriented part's frame (api orient:
// centred, on z = 0), which is the stage's frame once the part is turned with Stage.orientBy.
import * as THREE from "three";

const token = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

/** The overlay colours, from the tokens (light and dark both set them). */
export function heatColors() {
  return {
    ok: new THREE.Color(token("--heat-ok", "#16A34A")), hot: new THREE.Color(token("--heat-hot", "#DC2626")),
    cold: new THREE.Color(token("--heat-cold", "#2563EB")), slow: new THREE.Color(token("--heat-slow", "#0EA5E9")),
    faded: new THREE.Color(token("--heat-faded", "#9CA3AF")), slower: new THREE.Color(token("--heat-cold", "#2563EB")),
    faster: new THREE.Color(token("--heat-faster", "#EA580C")), same: new THREE.Color(token("--heat-same", "#9CA3AF")),
    fan: new THREE.Color(token("--heat-fan", "#7C3AED")), lowImportance: new THREE.Color(token("--heat-low", "#E5E7EB")),
    highImportance: new THREE.Color(token("--heat-high", "#7C3AED")),
  };
}

export class HeatView {
  constructor(stage) {
    this.stage = stage;
    this.group = new THREE.Group();
    this.group.renderOrder = 2;
    stage.scene.add(this.group);
    this.spots = null; // {mesh, order (instance -> spot), upto (layer -> instances)}
    this.lines = null; // {mesh, upto}
    this.marks = null; // {mesh, layers}
    this.onPick = null;
    this.pickedBox = null;
    const ray = new THREE.Raycaster();
    let down = null;
    this.pointerdown = (e) => { down = [e.clientX, e.clientY]; };
    this.pointerup = (e) => {
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4 || !this.spots?.mesh.visible || !this.onPick) return;
      const rect = stage.canvas.getBoundingClientRect();
      ray.setFromCamera(new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1), stage.camera);
      const hit = ray.intersectObject(this.spots.mesh)[0];
      if (hit?.instanceId != null) this.onPick(this.spots.order[hit.instanceId]);
    };
    stage.canvas.addEventListener("pointerdown", this.pointerdown);
    stage.canvas.addEventListener("pointerup", this.pointerup);
  }

  /** The spots as blocks one heat cell wide. ``order``/``upto``: simlogic.byLayer, so a layer limit is a count. */
  setSpots(points, cell, order, upto) {
    this.clearSpots();
    const zs = [...new Set(order.slice(0, 2000).map((i) => Math.round(points[i * 3 + 2] * 100)))].sort((a, b) => a - b);
    const dz = zs.length > 1 ? Math.max(0.05, Math.min(cell, (zs[1] - zs[0]) / 100)) : cell;
    const mesh = new THREE.InstancedMesh(new THREE.BoxGeometry(cell * 0.92, cell * 0.92, dz), new THREE.MeshBasicMaterial({ toneMapped: false }), order.length);
    const m = new THREE.Matrix4();
    order.forEach((i, n) => mesh.setMatrixAt(n, m.makeTranslation(points[i * 3], points[i * 3 + 1], points[i * 3 + 2] - dz / 2)));
    mesh.instanceMatrix.needsUpdate = true;
    mesh.computeBoundingSphere();
    this.group.add(mesh);
    this.spots = { mesh, order, upto, points, cell, dz };
  }

  /** Colour each spot: ``colorOf(spot index)`` → THREE.Color. */
  colorSpots(colorOf) {
    if (!this.spots) return;
    const { mesh, order } = this.spots;
    order.forEach((i, n) => mesh.setColorAt(n, colorOf(i)));
    mesh.instanceColor.needsUpdate = true;
    this.stage.render();
  }

  /** The G-code lines (x0 y0 z0 x1 y1 z1 each) coloured by ``colorOf(line index)``, sorted by ``layer``. */
  setLines(segments, layer, colorOf) {
    this.clearLines();
    const n = layer.length;
    const order = Array.from(layer.keys()).sort((x, y) => layer[x] - layer[y] || x - y);
    const pos = new Float32Array(n * 6), col = new Float32Array(n * 6);
    const top = layer.reduce((a, k) => Math.max(a, k), -1);
    const upto = new Int32Array(top + 1);
    order.forEach((i, j) => {
      pos.set(segments.subarray(i * 6, i * 6 + 6), j * 6);
      const c = colorOf(i);
      col.set([c.r, c.g, c.b, c.r, c.g, c.b], j * 6);
      upto[layer[i]] += 1;
    });
    for (let k = 1; k <= top; k++) upto[k] += upto[k - 1];
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
    const mesh = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ vertexColors: true, toneMapped: false }));
    this.group.add(mesh);
    this.lines = { mesh, upto };
  }

  /** Small balls where the plan changes the fan: [{layer, point}]. */
  setMarks(marks, radius, color) {
    this.clearMarks();
    if (!marks.length) return;
    const mesh = new THREE.InstancedMesh(new THREE.SphereGeometry(radius, 12, 8), new THREE.MeshBasicMaterial({ color, toneMapped: false }), marks.length);
    const sorted = [...marks].sort((a, b) => a.layer - b.layer);
    const m = new THREE.Matrix4();
    sorted.forEach((x, i) => mesh.setMatrixAt(i, m.makeTranslation(...x.point)));
    mesh.instanceMatrix.needsUpdate = true;
    mesh.computeBoundingSphere();
    this.group.add(mesh);
    this.marks = { mesh, layers: sorted.map((x) => x.layer) };
  }

  /** Which overlay shows: "spots", "lines" or nothing. */
  show(what) {
    if (this.spots) this.spots.mesh.visible = what === "spots";
    if (this.lines) this.lines.mesh.visible = what === "lines";
    if (this.marks) this.marks.mesh.visible = what === "lines";
    if (this.pickedBox) this.pickedBox.visible = what === "spots";
    this.stage.render();
  }

  /** Show layers up to ``k`` (0-based; Infinity: all). */
  upTo(k) {
    const cut = (upto) => (k >= upto.length - 1 || !Number.isFinite(k) ? upto[upto.length - 1] ?? 0 : k < 0 ? 0 : upto[k]);
    if (this.spots) this.spots.mesh.count = cut(this.spots.upto);
    if (this.lines) this.lines.mesh.geometry.setDrawRange(0, cut(this.lines.upto) * 2);
    if (this.marks) this.marks.mesh.count = this.marks.layers.filter((l) => l <= k).length;
    this.stage.render();
  }

  /** Outline the picked spot (null: none). */
  pick(i) {
    if (this.pickedBox) { this.group.remove(this.pickedBox); this.pickedBox.geometry.dispose(); this.pickedBox = null; }
    if (i != null && this.spots) {
      const { points, cell, dz } = this.spots;
      const box = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(cell * 1.3, cell * 1.3, dz * 2.5)),
        new THREE.LineBasicMaterial({ color: token("--text", "#111827"), depthTest: false }));
      box.position.set(points[i * 3], points[i * 3 + 1], points[i * 3 + 2] - dz / 2);
      box.renderOrder = 5;
      this.group.add(box);
      this.pickedBox = box;
    }
    this.stage.render();
  }

  clearSpots() { if (this.spots) { this.group.remove(this.spots.mesh); this.spots.mesh.geometry.dispose(); this.spots = null; } }
  clearLines() { if (this.lines) { this.group.remove(this.lines.mesh); this.lines.mesh.geometry.dispose(); this.lines = null; } }
  clearMarks() { if (this.marks) { this.group.remove(this.marks.mesh); this.marks.mesh.geometry.dispose(); this.marks = null; } }

  clear() {
    this.pick(null);
    this.clearSpots();
    this.clearLines();
    this.clearMarks();
    this.stage.render();
  }
}
