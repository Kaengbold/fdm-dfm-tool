// Heat overlay of the simulation step (T-478, T-587): the sliced print itself, its G-code paths drawn as beads
// (short pieces, as wide and high as the line each comes from), coloured by the heat model's spot it lies in (window
// map or importance) or by how the plan changed its speed, with markers where the fan changes, a layer limit for
// playback, and a click on a bead for its spot. The part's own surface is hidden meanwhile, so what shows is the
// print the simulation ran on. Points come in the oriented part's frame (api orient: centred, on z = 0), which is
// the stage's frame once the part is turned with Stage.orientBy.
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
    set: new THREE.Color(token("--heat-set", "#1E3A8A")), warm: new THREE.Color(token("--heat-warm", "#EAB308")),
  };
}

export class HeatView {
  constructor(stage) {
    this.stage = stage;
    this.group = new THREE.Group();
    this.group.renderOrder = 2;
    stage.scene.add(this.group);
    this.paths = null; // {mesh, order (instance -> piece), spot (instance -> spot or -1), upto (layer -> instances)}
    this.spotAt = null; // {points, cell, dz}: where the spots are, to outline the picked one
    this.marks = null; // {mesh, layers}
    this.onPick = null;
    this.pickedBox = null;
    const ray = new THREE.Raycaster();
    let down = null;
    this.pointerdown = (e) => { down = [e.clientX, e.clientY]; };
    this.pointerup = (e) => {
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4 || !this.paths?.mesh.visible || !this.onPick) return;
      const rect = stage.canvas.getBoundingClientRect();
      ray.setFromCamera(new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1), stage.camera);
      const hit = ray.intersectObject(this.paths.mesh)[0];
      const spot = hit?.instanceId != null ? this.paths.spot[hit.instanceId] : -1;
      if (spot >= 0) this.onPick(spot);
    };
    stage.canvas.addEventListener("pointerdown", this.pointerdown);
    stage.canvas.addEventListener("pointerup", this.pointerup);
  }

  /** The printed paths as beads: ``segments`` (x0 y0 z0 x1 y1 z1 at the nozzle, i.e. the bead's top), each piece's
   * ``width`` and ``height`` (mm), ``layer`` and ``spot`` (heat spot, -1: none). Sorted by layer, so a layer limit
   * is a count. */
  setPaths(segments, width, height, layer, spot) {
    this.clearPaths();
    const n = layer.length;
    const order = Array.from(layer.keys()).sort((x, y) => layer[x] - layer[y] || x - y);
    const mesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial({ roughness: 0.75, metalness: 0 }), n);
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), sc = new THREE.Vector3(), zAxis = new THREE.Vector3(0, 0, 1);
    const top = layer.reduce((a, k) => Math.max(a, k), -1);
    const upto = new Int32Array(top + 1), spotOf = new Int32Array(n);
    order.forEach((i, j) => {
      const o = i * 6, dx = segments[o + 3] - segments[o], dy = segments[o + 4] - segments[o + 1];
      const hgt = height[i];
      p.set((segments[o] + segments[o + 3]) / 2, (segments[o + 1] + segments[o + 4]) / 2, (segments[o + 2] + segments[o + 5]) / 2 - hgt / 2);
      q.setFromAxisAngle(zAxis, Math.atan2(dy, dx));
      // A little longer than the piece, so the beads of one line join without gaps at the corners.
      sc.set(Math.hypot(dx, dy) + width[i] * 0.5, width[i], hgt * 0.95);
      mesh.setMatrixAt(j, m.compose(p, q, sc));
      spotOf[j] = spot[i];
      upto[layer[i]] += 1;
    });
    for (let k = 1; k <= top; k++) upto[k] += upto[k - 1];
    mesh.instanceMatrix.needsUpdate = true;
    mesh.computeBoundingSphere();
    this.group.add(mesh);
    this.paths = { mesh, order, spot: spotOf, upto };
  }

  /** Where the spots are (for the outline of a picked one). */
  setSpots(points, cell, dz) { this.spotAt = { points, cell, dz }; }

  /** Colour each bead: ``colorOf(piece index, spot index or -1)`` → THREE.Color. ``shownOnly``: only the beads up
   * to the layer limit (the replay recolours every frame). */
  colorPaths(colorOf, shownOnly = false) {
    if (!this.paths) return;
    const { mesh, order, spot } = this.paths;
    const n = shownOnly ? mesh.count : order.length;
    for (let j = 0; j < n; j++) mesh.setColorAt(j, colorOf(order[j], spot[j]));
    mesh.instanceColor.needsUpdate = true;
    this.stage.render();
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

  /** Show the fan markers (the G-code changes view) or not. */
  showMarks(on) {
    if (this.marks) this.marks.mesh.visible = on;
    this.stage.render();
  }

  /** Show layers up to ``k`` (0-based; Infinity: all). */
  upTo(k) {
    const cut = (upto) => (k >= upto.length - 1 || !Number.isFinite(k) ? upto[upto.length - 1] ?? 0 : k < 0 ? 0 : upto[k]);
    if (this.paths) this.paths.mesh.count = cut(this.paths.upto);
    if (this.marks) this.marks.mesh.count = this.marks.layers.filter((l) => l <= k).length;
    this.stage.render();
  }

  /** Outline the picked spot (null: none). */
  pick(i) {
    if (this.pickedBox) { this.group.remove(this.pickedBox); this.pickedBox.geometry.dispose(); this.pickedBox = null; }
    if (i != null && this.spotAt) {
      const { points, cell, dz } = this.spotAt;
      const box = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(cell * 1.3, cell * 1.3, dz * 2.5)),
        new THREE.LineBasicMaterial({ color: token("--text", "#111827"), depthTest: false }));
      box.position.set(points[i * 3], points[i * 3 + 1], points[i * 3 + 2] - dz / 2);
      box.renderOrder = 5;
      this.group.add(box);
      this.pickedBox = box;
    }
    this.stage.render();
  }

  clearPaths() { if (this.paths) { this.group.remove(this.paths.mesh); this.paths.mesh.geometry.dispose(); this.paths = null; } }
  clearMarks() { if (this.marks) { this.group.remove(this.marks.mesh); this.marks.mesh.geometry.dispose(); this.marks = null; } }

  clear() {
    this.pick(null);
    this.clearPaths();
    this.clearMarks();
    this.spotAt = null;
    this.stage.render();
  }
}
