// three.js viewer: the part on a printer bed, coloured by overhang class,
// with boxes marking findings. Printer convention: Z up, bed = XY plane.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

THREE.Object3D.DEFAULT_UP.set(0, 0, 1);

// Removing an object from the scene doesn't free its GPU buffers, shaders and textures: over a long
// session (every check redraws the markers, every strength run a coloured copy of the part) the
// tab's GPU memory grows until it slows down or the context is lost. Free them on the way out.
function dispose(obj) {
  obj?.traverse((o) => {
    o.geometry?.dispose();
    for (const m of [o.material].flat()) {
      if (!m) continue;
      for (const v of Object.values(m)) if (v?.isTexture) v.dispose();
      m.dispose();
    }
    o.dispose?.(); // InstancedMesh
  });
  obj?.removeFromParent();
}

/** Remove and free everything in a group, keeping the group itself. */
function empty(group) {
  if (!group) return;
  for (const child of [...group.children]) dispose(child);
}

const css = (v) => new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue(v).trim());
const CLASS_COLORS = ["--ok", "--borderline", "--support", "--bed"].map(css);
const SEVERITY_COLORS = { problem: css("--support"), warn: css("--borderline"), info: css("--accent") };

export const REGION_COLORS = { fixed: css("--fixed"), force: css("--force"), selected: css("--accent"), fit: css("--now"), clean: css("--clean"), bed: css("--bed"), vertical: css("--vertical") };

export class Viewer {
  constructor(el) {
    this.el = el;
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(devicePixelRatio);
    el.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = css("--bg");
    this.scene.add(new THREE.HemisphereLight("#ffffff", "#8a8f98", 2.2));
    const sun = new THREE.DirectionalLight("#ffffff", 1.5);
    sun.position.set(100, -150, 250);
    this.scene.add(sun);

    this.bedGroup = new THREE.Group();
    this.scene.add(this.bedGroup);
    this.markers = new THREE.Group();
    this.scene.add(this.markers);

    this.camera = new THREE.PerspectiveCamera(40, 1, 0.1, 5000);
    this.camera.position.set(150, -200, 150);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.part = null;
    this.framed = false;

    addEventListener("resize", () => this.resize());
    this.resize();
    this.renderer.setAnimationLoop(() => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    });
  }

  resize() {
    const { clientWidth: w, clientHeight: h } = this.el;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  setBed([w, d]) {
    this.bedSize = [w, d];
    empty(this.bedGroup);
    const plate = new THREE.Mesh(new THREE.PlaneGeometry(w, d), new THREE.MeshStandardMaterial({ color: "#dde3df" }));
    plate.position.z = -0.05;
    const grid = new THREE.GridHelper(Math.max(w, d), Math.round(Math.max(w, d) / 10), "#bac4bd", "#ccd4ce");
    grid.rotation.x = Math.PI / 2;
    this.bedGroup.add(plate, grid);
  }

  /** Show triangles (float32, part coords) rotated into print orientation. */
  showPart(triangles, rotation) {
    this.hidePieces();
    const { out: positions, shift } = placeTriangles(triangles, rotation);
    this.placement = { rotation, shift };
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute("color", new THREE.BufferAttribute(new Float32Array(positions.length), 3));
    geometry.computeVertexNormals();
    if (!this.part) {
      this.part = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.7 }));
      this.scene.add(this.part);
    } else {
      this.part.geometry.dispose();
      this.part.geometry = geometry;
    }
    geometry.computeBoundingSphere();
    if (!this.framed) this.frame(geometry.boundingSphere);
    return positions;
  }

  /** A split part's pieces (``[{triangles, rotation}]``, part coordinates), each on a bed of its own in a row,
   * turned the way it prints; the part, its markers and edits are hidden until hidePieces / showPart. */
  showPieces(pieces) {
    this.hidePieces();
    this.pieces = new THREE.Group();
    const [w, d] = this.bedSize ?? [256, 256];
    const colors = [css("--accent"), css("--clean")];
    pieces.forEach(({ triangles, rotation }, i) => {
      const x = i * (w + 20);
      const plate = new THREE.Mesh(new THREE.PlaneGeometry(w, d), new THREE.MeshStandardMaterial({ color: "#dde3df" }));
      plate.position.set(x, 0, -0.05);
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(placeTriangles(triangles, rotation).out, 3));
      g.computeVertexNormals();
      const piece = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color: colors[i % 2], roughness: 0.7 }));
      piece.position.x = x;
      this.pieces.add(plate, piece);
    });
    for (const o of [this.part, this.markers, this.ghosts, this.bedGroup]) if (o) o.visible = false;
    this.scene.add(this.pieces);
    const sphere = new THREE.Box3().setFromObject(this.pieces).getBoundingSphere(new THREE.Sphere());
    this.frame({ center: sphere.center, radius: sphere.radius * 0.8 });
  }

  hidePieces() {
    if (!this.pieces) return;
    dispose(this.pieces);
    this.pieces = null;
    for (const o of [this.part, this.markers, this.ghosts, this.bedGroup]) if (o) o.visible = true;
    if (this.part) this.frame(this.part.geometry.boundingSphere);
  }

  /** Colour each triangle: ``colorOf(faceIndex)`` returns a THREE.Color. */
  colorFaces(count, colorOf) {
    const attr = this.part.geometry.getAttribute("color");
    for (let f = 0; f < count; f++) {
      const c = colorOf(f);
      for (let v = 0; v < 3; v++) attr.setXYZ(f * 3 + v, c.r, c.g, c.b);
    }
    attr.needsUpdate = true;
  }

  /** Overhang classes (uint8 per face), optionally with highlighted regions on top. */
  colorByClass(classes, highlight = () => null) {
    this.colorFaces(classes.length, (f) => highlight(f) ?? CLASS_COLORS[classes[f]]);
  }

  /** Stress heat map: uint8 per face, 0 = unloaded, 255 = beyond breaking. */
  colorByHeat(heat) {
    const c = new THREE.Color();
    this.colorFaces(heat.length, (f) => {
      const t = heat[f] / 255;
      // green -> yellow -> red; ~0.8 (failure index 1) is fully red
      return c.setHSL(Math.max(0, 0.33 - (0.33 * t) / 0.8), 0.75, 0.5).clone();
    });
  }

  /** Screen position (CSS px, page coordinates) of triangle ``f``'s centre — for automated tests. */
  screenPointOfFace(f) {
    const pos = this.part.geometry.getAttribute("position");
    const c = new THREE.Vector3();
    for (let v = 0; v < 3; v++) c.add(new THREE.Vector3().fromBufferAttribute(pos, f * 3 + v));
    c.divideScalar(3).project(this.camera);
    const rect = this.renderer.domElement.getBoundingClientRect();
    return [rect.left + ((c.x + 1) / 2) * rect.width, rect.top + ((1 - c.y) / 2) * rect.height];
  }

  /** Face index under a click, or -1. */
  pick(event) {
    return this.pickHit(event)?.face ?? -1;
  }

  /** {face, point} under a click (point in part coordinates), or null. */
  pickHit(event) {
    if (!this.part) return null;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, this.camera);
    const hit = ray.intersectObject(this.part)[0];
    return hit ? { face: hit.faceIndex, point: this.toPart(hit.point.toArray()) } : null;
  }

  /** Shown (print-frame) point -> part coordinates: the inverse of placeTriangles. */
  toPart(p) {
    const { rotation: R, shift } = this.placement;
    const d = p.map((v, i) => v - shift[i]);
    return [0, 1, 2].map((k) => R[0][k] * d[0] + R[1][k] * d[1] + R[2][k] * d[2]);
  }

  /** Part-coordinate point -> where it is shown. */
  toShown(p) {
    const { rotation: R, shift } = this.placement;
    return R.map((row, i) => row[0] * p[0] + row[1] * p[1] + row[2] * p[2] + shift[i]);
  }

  /** Translucent balls for loads on a spot: [{point (part coords), radius, color}]. */
  showSpots(spots) {
    empty(this.spots);
    if (!this.spots) this.scene.add((this.spots = new THREE.Group()));
    for (const s of spots) {
      const material = new THREE.MeshBasicMaterial({ color: s.color, transparent: true, opacity: 0.5, depthWrite: false });
      const ball = new THREE.Mesh(new THREE.SphereGeometry(s.radius, 24, 16), material);
      ball.position.set(...this.toShown(s.point));
      this.spots.add(ball);
    }
  }

  /** FEA result view: the refined surface coloured by ``values`` (per corner) from 0 to ``max``,
   * in banded rainbow colours like FEA software. ``disp`` (3 per corner, mm, part coords) lets
   * ``setDeformation`` show the bent shape. Hides the normal part until ``hideField``. */
  showField({ triangles, values, disp, max }) {
    this.hideField();
    const { out } = placeTriangles(triangles, this.placement.rotation, this.placement.shift);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(out, 3));
    const colors = new Float32Array(out.length);
    const c = new THREE.Color();
    for (let i = 0; i < values.length; i++) {
      fieldColor(values[i] / max, c);
      colors.set([c.r, c.g, c.b], i * 3);
    }
    geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    geometry.computeVertexNormals();
    this.renderer.localClippingEnabled = true;
    const material = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.8, side: THREE.DoubleSide, clippingPlanes: [] });
    this.field = { mesh: new THREE.Mesh(geometry, material), base: out.slice(), disp: disp && this.turnAll(disp) };
    this.scene.add(this.field.mesh);
    this.part.visible = false;
  }

  hideField() {
    if (!this.field) return;
    dispose(this.field.mesh);
    dispose(this.section);
    this.section = null;
    this.field = null;
    if (this.part) this.part.visible = true;
  }

  /** Bend the shown result by ``scale`` × the computed displacement (FEA software exaggerates it).
   * ``shade: false`` skips recomputing the normals: cheap enough for an animation. */
  setDeformation(scale, shade = true) {
    if (!this.field?.disp) return;
    const pos = this.field.mesh.geometry.getAttribute("position");
    const { base, disp } = this.field;
    for (let i = 0; i < base.length; i++) pos.array[i] = base[i] + scale * disp[i];
    pos.needsUpdate = true;
    if (shade) this.field.mesh.geometry.computeVertexNormals();
  }

  /** Cut through the result across ``axis`` ("x"/"y"/"z" on the bed) at ``t`` (0–1 of the part's
   * extent); ``t`` null removes the cut. With per-cube ``values`` the part up to the cut is shown as
   * the analysis cubes themselves, solid and coloured, so the cut face reads as clean blocks (a
   * clipped surface shows its hollow inside instead). Without values (displacement: only known on
   * the surface) the surface is clipped. ``elements``: {centers (part coords), values|null, size_mm}. */
  setSection(axis, t, elements, max) {
    dispose(this.section); // its own cubes and material, never the field's
    this.section = null;
    if (!this.field) return;
    const material = this.field.mesh.material;
    this.field.mesh.visible = true;
    if (t == null) {
      material.clippingPlanes = [];
      material.needsUpdate = true;
      return;
    }
    const k = { x: 0, y: 1, z: 2 }[axis];
    const box = new THREE.Box3().setFromBufferAttribute(this.field.mesh.geometry.getAttribute("position"));
    const at = box.min.getComponent(k) + t * (box.max.getComponent(k) - box.min.getComponent(k));
    if (!elements.values) {
      material.clippingPlanes = [new THREE.Plane(new THREE.Vector3().setComponent(k, -1), at)];
      material.needsUpdate = true;
      return;
    }
    this.field.mesh.visible = false;
    const h = elements.size_mm;
    const shown = [];
    for (let i = 0; i < elements.values.length; i++) {
      const p = this.toShown([elements.centers[3 * i], elements.centers[3 * i + 1], elements.centers[3 * i + 2]]);
      if (p[k] <= at) shown.push([p, elements.values[i]]);
    }
    // Shaded (not flat) so neighbouring cubes and the cut face stay readable as blocks.
    const cubes = new THREE.InstancedMesh(new THREE.BoxGeometry(h, h, h), new THREE.MeshStandardMaterial({ roughness: 0.85 }), shown.length);
    const m = new THREE.Matrix4();
    const c = new THREE.Color();
    shown.forEach(([p, v], i) => {
      cubes.setMatrixAt(i, m.makeTranslation(p[0], p[1], p[2]));
      cubes.setColorAt(i, fieldColor(v / max, c));
    });
    this.section = cubes;
    this.scene.add(cubes);
  }

  /** Part-coordinate vectors (flat array, 3 per vector) turned into the shown orientation. */
  turnAll(v) {
    const R = this.placement.rotation;
    const out = new Float32Array(v.length);
    for (let i = 0; i < v.length; i += 3) {
      for (let r = 0; r < 3; r++) out[i + r] = R[r][0] * v[i] + R[r][1] * v[i + 1] + R[r][2] * v[i + 2];
    }
    return out;
  }

  /** Symbols for the strength setup: a lock on held faces, an arrow with its weight on loads.
   * [{kind: "fixed"|"force", point, normal, dir (force direction), label}] — part coordinates. */
  showLoadSymbols(symbols) {
    empty(this.symbols);
    if (!this.symbols) this.scene.add((this.symbols = new THREE.Group()));
    if (!this.part) return;
    const r = this.part.geometry.boundingSphere.radius;
    const size = Math.max(6, 0.12 * r);
    const turn = (v) => this.toShown(v).map((x, i) => x - this.placement.shift[i]); // directions: rotate only
    for (const s of symbols) {
      const at = new THREE.Vector3(...this.toShown(s.point));
      const n = new THREE.Vector3(...turn(s.normal)).normalize();
      if (s.kind === "fixed") {
        const lock = iconSprite("🔒", size);
        lock.position.copy(at).addScaledVector(n, size * 0.7);
        this.symbols.add(lock);
        continue;
      }
      const d = new THREE.Vector3(...turn(s.dir)).normalize();
      const len = Math.max(12, 0.4 * r);
      // Pushing ends at the face; pulling and hanging start at it.
      const into = d.dot(n) < 0;
      const tail = into ? at.clone().addScaledVector(d, -len) : at.clone();
      const color = css("--force");
      const arrow = solidArrow(d, tail, len, size, color);
      arrow.userData.load = s.load; // pickSymbol: drag it to change the direction
      const tag = iconSprite(s.label, size * 0.75, color.getStyle());
      // Beyond the arrow's free end, far enough that the (wide) label doesn't overlap the head.
      const gap = tag.scale.x / 2 + size * 0.5;
      tag.position.copy(into ? tail : tail.clone().addScaledVector(d, len)).addScaledVector(d, into ? -gap : gap);
      this.symbols.add(arrow, tag);
    }
  }

  /** Turn a direction (part coordinates) by a mouse drag of (dx, dy) px, like a trackball: the
   * arrow follows the mouse whichever way the camera looks. */
  dragDirection(dir, dx, dy) {
    const R = this.placement.rotation;
    const shown = new THREE.Vector3(...R.map((row) => row[0] * dir[0] + row[1] * dir[1] + row[2] * dir[2]));
    const m = this.camera.matrixWorld.elements;
    const right = new THREE.Vector3(m[0], m[1], m[2]).normalize();
    const up = new THREE.Vector3(m[4], m[5], m[6]).normalize();
    shown.applyAxisAngle(up, dx * 0.01).applyAxisAngle(right, dy * 0.01).normalize();
    return [0, 1, 2].map((k) => Math.round((R[0][k] * shown.x + R[1][k] * shown.y + R[2][k] * shown.z) * 1e4) / 1e4);
  }

  /** Index of the load whose arrow is under the pointer, or null. */
  pickSymbol(event) {
    if (!this.symbols?.children.length) return null;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, this.camera);
    for (const hit of ray.intersectObjects(this.symbols.children, true)) {
      for (let o = hit.object; o; o = o.parent) if (o.userData.load != null) return o.userData.load;
    }
    return null;
  }

  /** Red arrow pointing at the weakest spot (print-frame surface ``point`` and outward ``normal``);
   * hidden where the part is in front of it, like the load symbols. */
  markWeak(point, normal) {
    const r = this.part.geometry.boundingSphere.radius;
    const size = Math.max(5, 0.11 * r);
    const n = new THREE.Vector3(...normal).normalize();
    const len = size * 3;
    const tail = new THREE.Vector3(...point).addScaledVector(n, len);
    const arrow = solidArrow(n.clone().negate(), tail, len, size, css("--support"));
    this.markers.add(arrow);
  }

  /** Strengthening zones: translucent orange boxes, each given by its 8 corners (print frame, in the
   * order lo/hi per axis with z fastest). */
  showZones(zones) {
    empty(this.zones);
    if (!this.zones) this.scene.add((this.zones = new THREE.Group()));
    for (const z of zones) { // corner arrays (zones, orange) or {corners, color, shape}
      const [c, color] = Array.isArray(z) ? [z, css("--vertical")] : [z.corners, css(z.color ?? "--vertical")];
      if (z.shape && z.shape !== "box") {
        this.roundZone(c, z.shape, color);
        continue;
      }
      const idx = [];
      for (let axis = 0; axis < 3; axis++) {
        for (const side of [0, 1]) {
          const bit = 2 - axis; // corner index = 4·ix + 2·iy + iz
          const others = [0, 1, 2].filter((a) => a !== axis).map((a) => 2 - a);
          const face = [[0, 0], [1, 0], [1, 1], [0, 1]].map(([u, v]) => (side << bit) | (u << others[0]) | (v << others[1]));
          idx.push(face[0], face[1], face[2], face[0], face[2], face[3]);
        }
      }
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(c.flat()), 3));
      geometry.setIndex(idx);
      this.zones.add(new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.18, depthWrite: false, side: THREE.DoubleSide })));
      this.zones.add(new THREE.LineSegments(new THREE.EdgesGeometry(geometry), new THREE.LineBasicMaterial({ color })));
    }
  }

  /** A cylinder or ball inside the box given by its 8 corners (index 4·ix + 2·iy + iz): see-through,
   *  with its rims (cylinder) or three rings (ball) drawn as lines. */
  roundZone(c, shape, color) {
    const v = (i) => new THREE.Vector3(...c[i]);
    const center = v(0).add(v(7)).multiplyScalar(0.5);
    const axes = [4, 2, 1].map((i) => v(i).sub(v(0)).multiplyScalar(0.5));
    const at = (x, y, zz) => center.clone().addScaledVector(axes[0], x).addScaledVector(axes[1], y).addScaledVector(axes[2], zz);
    const geometry = shape === "ball" ? new THREE.SphereGeometry(1, 24, 12) : new THREE.CylinderGeometry(1, 1, 2, 32).rotateX(Math.PI / 2);
    const pos = geometry.attributes.position;
    for (let i = 0; i < pos.count; i++) pos.setXYZ(i, ...at(pos.getX(i), pos.getY(i), pos.getZ(i)).toArray());
    this.zones.add(new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.18, depthWrite: false, side: THREE.DoubleSide })));
    const ring = (f) => {
      const pts = Array.from({ length: 48 }, (_, k) => f((2 * Math.PI * k) / 48));
      this.zones.add(new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color })));
    };
    const rings = shape === "ball"
      ? [(t) => at(Math.cos(t), Math.sin(t), 0), (t) => at(Math.cos(t), 0, Math.sin(t)), (t) => at(0, Math.cos(t), Math.sin(t))]
      : [(t) => at(Math.cos(t), Math.sin(t), -1), (t) => at(Math.cos(t), Math.sin(t), 1)];
    rings.forEach(ring);
  }

  /** Small sphere marking a point (e.g. where the part would break). */
  mark(point, color = "--support") {
    const dot = new THREE.Mesh(new THREE.SphereGeometry(1.5), new THREE.MeshBasicMaterial({ color: css(color), depthTest: false }));
    dot.position.set(...point);
    dot.renderOrder = 10;
    this.markers.add(dot);
  }

  frame({ center, radius }) {
    this.controls.target.copy(center);
    this.camera.position.copy(center).add(new THREE.Vector3(1.2, -1.6, 1.1).multiplyScalar(radius * 1.6));
    this.framed = true;
  }

  /** Boxes for findings; when a finding is selected, it gets a filled highlight, pin, and camera moves
   * (``focus: false``: stays, for pictures that place the camera themselves). */
  showFindings(findings, selected = -1, focus = true) {
    empty(this.markers);
    findings.forEach((f, i) => {
      if (!f.bbox) return;
      const [[x0, y0, z0], [x1, y1, z1]] = f.bbox;
      const pad = 0.5;
      const box = new THREE.Box3(new THREE.Vector3(x0 - pad, y0 - pad, z0), new THREE.Vector3(x1 + pad, y1 + pad, z1 + pad));
      const isSel = i === selected;
      if (selected < 0 || isSel) {
        const helper = new THREE.Box3Helper(box, SEVERITY_COLORS[f.severity]);
        helper.material.transparent = true;
        helper.material.opacity = isSel ? 1 : 0.3;
        helper.material.depthTest = !isSel;
        this.markers.add(helper);
      }
      if (isSel) {
        // Translucent filled box so the entire volume stands out vividly
        const dx = Math.max(x1 - x0, 1);
        const dy = Math.max(y1 - y0, 1);
        const dz = Math.max(z1 - z0, 1);
        const fillGeom = new THREE.BoxGeometry(dx + pad * 2, dy + pad * 2, dz + pad * 2);
        const fillMat = new THREE.MeshBasicMaterial({
          color: SEVERITY_COLORS[f.severity],
          transparent: true,
          opacity: 0.35,
          depthWrite: false,
        });
        const fillMesh = new THREE.Mesh(fillGeom, fillMat);
        fillMesh.position.set((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2);
        this.markers.add(fillMesh);

        // Prominent marker pin at the center
        const pin = new THREE.Mesh(
          new THREE.SphereGeometry(Math.max(1.8, Math.min(dx, dy, dz) * 0.25)),
          new THREE.MeshBasicMaterial({ color: "#ffffff", depthTest: false })
        );
        pin.position.set((x0 + x1) / 2, (y0 + y1) / 2, z1 + 1.2);
        this.markers.add(pin);
      }
    });
    if (focus && selected >= 0 && findings[selected]?.bbox) {
      this.focusFinding(findings[selected]);
    }
  }

  /** A picture of the view as a JPEG data URL, ``width`` px wide: the whole part, or close to ``bbox``
   * (print frame) seen from the current direction. The camera goes back to where the user had it. */
  snapshot(bbox = null, width = 720) {
    const pos = this.camera.position.clone();
    const target = this.controls.target.clone();
    const dir = pos.clone().sub(target).normalize();
    if (dir.lengthSq() < 0.01) dir.set(1.2, -1.6, 1.1).normalize();
    let center, dist;
    if (bbox) {
      const [[x0, y0, z0], [x1, y1, z1]] = bbox;
      center = new THREE.Vector3((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2);
      dist = Math.max(Math.hypot(x1 - x0, y1 - y0, z1 - z0) * 2.4, 45);
    } else {
      const geometry = (this.field?.mesh ?? this.part).geometry;
      if (!geometry.boundingSphere) geometry.computeBoundingSphere();
      const sphere = geometry.boundingSphere;
      center = sphere.center.clone();
      dist = sphere.radius * 3.2;
    }
    this.controls.target.copy(center);
    this.camera.position.copy(center).addScaledVector(dir, dist);
    this.camera.lookAt(center);
    this.renderer.render(this.scene, this.camera);
    const src = this.renderer.domElement;
    const out = document.createElement("canvas");
    out.width = width;
    out.height = Math.round((width * src.height) / src.width);
    out.getContext("2d").drawImage(src, 0, 0, out.width, out.height);
    this.camera.position.copy(pos);
    this.controls.target.copy(target);
    this.controls.update();
    return out.toDataURL("image/jpeg", 0.85);
  }

  /** Move the camera to ``f.bbox`` (print frame). ``from``: look from this direction (default:
   * keep the current one). */
  focusFinding(f, from = null) {
    if (!f?.bbox) return;
    const [[x0, y0, z0], [x1, y1, z1]] = f.bbox;
    const center = new THREE.Vector3((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2);
    const span = Math.max(Math.hypot(x1 - x0, y1 - y0, z1 - z0), 20);

    const startTarget = this.controls.target.clone();
    const startPos = this.camera.position.clone();
    const dir = from ? from.clone().normalize() : this.camera.position.clone().sub(this.controls.target).normalize();
    if (dir.lengthSq() < 0.01) dir.set(1.2, -1.6, 1.1).normalize();
    const endTarget = center;
    const endPos = center.clone().add(dir.multiplyScalar(Math.max(span * 2.4, 45)));

    const t0 = performance.now();
    const dur = 350;
    const anim = (now) => {
      const p = Math.min((now - t0) / dur, 1);
      const ease = p * (2 - p);
      this.controls.target.lerpVectors(startTarget, endTarget, ease);
      this.camera.position.lerpVectors(startPos, endPos, ease);
      this.controls.update();
      if (p < 1) requestAnimationFrame(anim);
    };
    requestAnimationFrame(anim);
  }

  resetCamera() {
    if (this.part?.geometry?.boundingSphere) {
      this.frame(this.part.geometry.boundingSphere);
    }
  }

  clearMarkers() {
    empty(this.markers);
  }

  /** Translucent preview of design edits (part coordinates): red what is cut away, green what is added.
   * ``highlight``: one edit drawn bright and in front of the part (pointed at in the list). */
  showGhosts(edits, highlight = null) {
    empty(this.ghosts);
    if (!this.ghosts) this.scene.add((this.ghosts = new THREE.Group()));
    const dim = highlight ? 0.25 : 1;
    const material = {
      cut: new THREE.MeshBasicMaterial({ color: css("--support"), transparent: true, opacity: 0.45 * dim, depthWrite: false }),
      add: new THREE.MeshBasicMaterial({ color: css("--clean"), transparent: true, opacity: 0.55 * dim, depthWrite: false }),
      now: new THREE.MeshBasicMaterial({ color: css("--now"), transparent: true, opacity: 0.85, depthWrite: false, depthTest: false }),
    };
    for (const e of highlight && !edits.includes(highlight) ? [...edits, highlight] : edits) {
      const { out } = placeTriangles(e.mesh, this.placement.rotation, this.placement.shift);
      // An edit that adds and cuts (grip fins): each piece in its own colour.
      const parts = { [e.op]: [] };
      let start = 0;
      e.pieces.forEach((n, i) => {
        const op = e.piece_ops?.[i] ?? e.op;
        (parts[op] ??= []).push(out.subarray(start * 9, (start + n) * 9));
        start += n;
      });
      for (const [op, chunks] of Object.entries(parts)) {
        if (!chunks.length) continue;
        const pos = new Float32Array(chunks.reduce((k, c) => k + c.length, 0));
        chunks.reduce((at, c) => (pos.set(c, at), at + c.length), 0);
        const g = new THREE.BufferGeometry();
        g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
        const mesh = new THREE.Mesh(g, material[e === highlight ? "now" : op]);
        if (e === highlight) mesh.renderOrder = 10;
        this.ghosts.add(mesh);
      }
    }
  }

  /** Turn the camera to look at a design edit from outside the part, so the part doesn't hide it. */
  focusEdit(edit) {
    const { out } = placeTriangles(edit.mesh, this.placement.rotation, this.placement.shift);
    const box = new THREE.Box3().setFromArray(out);
    const center = box.getCenter(new THREE.Vector3());
    box.expandByScalar(this.part.geometry.boundingSphere.radius * 0.35); // some of the part around it, to see where it is
    const from = center.clone().sub(this.part.geometry.boundingSphere.center);
    from.z = Math.max(from.z, 0) + from.length() * 0.6; // a little from above
    if (from.lengthSq() < 1e-6) from.set(1.2, -1.6, 1.1);
    this.focusFinding({ bbox: [box.min.toArray(), box.max.toArray()] }, from);
  }
}

/** Rotate the triangle soup and sit it centred on the bed origin at z = 0 (matches fdmcore.checks.orient).
 * Pass ``fixedShift`` to place other geometry (ghosts) exactly like the part. */
export const FIELD_BANDS = 10;

/** Classic FEA rainbow in bands: blue (0) → cyan → green → yellow → red (1 and above). */
export function fieldColor(t, c = new THREE.Color()) {
  const band = Math.min(FIELD_BANDS - 1, Math.max(0, Math.floor(t * FIELD_BANDS)));
  return c.setHSL((0.66 * (FIELD_BANDS - 1 - band)) / (FIELD_BANDS - 1), 0.85, 0.5);
}

/** A thick arrow (shaft + head) from ``tail`` along unit ``dir``. */
function solidArrow(dir, tail, len, size, color) {
  const material = new THREE.MeshBasicMaterial({ color });
  const head = size * 0.7;
  const shaft = new THREE.Mesh(new THREE.CylinderGeometry(size * 0.1, size * 0.1, len - head, 12), material);
  shaft.position.y = (len - head) / 2;
  const tip = new THREE.Mesh(new THREE.ConeGeometry(size * 0.28, head, 16), material);
  tip.position.y = len - head / 2;
  const arrow = new THREE.Group();
  arrow.add(shaft, tip);
  arrow.position.copy(tail);
  arrow.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir);
  return arrow;
}

/** A label that always faces the camera (hidden where the part is in front of it): an emoji or
 * short text on a white rounded badge, ``size`` mm tall. */
function iconSprite(text, size, color = "#1f2328") {
  const h = 96;
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  ctx.font = `bold ${h * 0.62}px system-ui, "Segoe UI Emoji", sans-serif`;
  const w = Math.max(h, Math.ceil(ctx.measureText(text).width) + h * 0.5);
  canvas.width = w;
  canvas.height = h;
  ctx.fillStyle = "rgba(255,255,255,0.92)";
  ctx.beginPath();
  ctx.roundRect(2, 2, w - 4, h - 4, h * 0.3);
  ctx.fill();
  ctx.font = `bold ${h * 0.62}px system-ui, "Segoe UI Emoji", sans-serif`;
  ctx.fillStyle = color;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, w / 2, h / 2 + h * 0.04);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture }));
  sprite.scale.set((size * w) / h, size, 1);
  return sprite;
}

function placeTriangles(triangles, [r0, r1, r2], fixedShift = null) {
  const out = new Float32Array(triangles.length);
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < triangles.length; i += 3) {
    const x = triangles[i], y = triangles[i + 1], z = triangles[i + 2];
    const p = [r0[0] * x + r0[1] * y + r0[2] * z, r1[0] * x + r1[1] * y + r1[2] * z, r2[0] * x + r2[1] * y + r2[2] * z];
    for (let k = 0; k < 3; k++) {
      out[i + k] = p[k];
      min[k] = Math.min(min[k], p[k]);
      max[k] = Math.max(max[k], p[k]);
    }
  }
  const shift = fixedShift ?? [-(min[0] + max[0]) / 2, -(min[1] + max[1]) / 2, -min[2]];
  for (let i = 0; i < out.length; i += 3) for (let k = 0; k < 3; k++) out[i + k] += shift[k];
  return { out, shift };
}
