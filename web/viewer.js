// three.js viewer: the part on a printer bed, coloured by overhang class,
// with boxes marking findings. Printer convention: Z up, bed = XY plane.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

THREE.Object3D.DEFAULT_UP.set(0, 0, 1);

const css = (v) => new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue(v).trim());
const CLASS_COLORS = ["--ok", "--borderline", "--support", "--bed"].map(css);
const SEVERITY_COLORS = { problem: css("--support"), warn: css("--borderline"), info: css("--accent") };

export const REGION_COLORS = { fixed: css("--fixed"), force: css("--force"), selected: css("--accent") };

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
    this.bedGroup.clear();
    const plate = new THREE.Mesh(new THREE.PlaneGeometry(w, d), new THREE.MeshStandardMaterial({ color: "#dfe3e8" }));
    plate.position.z = -0.05;
    const grid = new THREE.GridHelper(Math.max(w, d), Math.round(Math.max(w, d) / 10), "#b9bec6", "#cdd1d7");
    grid.rotation.x = Math.PI / 2;
    this.bedGroup.add(plate, grid);
  }

  /** Show triangles (float32, part coords) rotated into print orientation. */
  showPart(triangles, rotation) {
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
    if (!this.part) return -1;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, this.camera);
    const hit = ray.intersectObject(this.part)[0];
    return hit ? hit.faceIndex : -1;
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

  /** Boxes for findings; the selected one is drawn solid, the rest faint. */
  showFindings(findings, selected = -1) {
    this.markers.clear();
    findings.forEach((f, i) => {
      if (!f.bbox || (selected >= 0 && i !== selected)) return;
      const [[x0, y0, z0], [x1, y1, z1]] = f.bbox;
      const pad = 0.5;
      const box = new THREE.Box3(new THREE.Vector3(x0 - pad, y0 - pad, z0), new THREE.Vector3(x1 + pad, y1 + pad, z1 + pad));
      const helper = new THREE.Box3Helper(box, SEVERITY_COLORS[f.severity]);
      helper.material.transparent = true;
      helper.material.opacity = i === selected ? 1 : 0.35;
      helper.material.depthTest = i !== selected;
      this.markers.add(helper);
    });
  }

  clearMarkers() {
    this.markers.clear();
  }

  /** Translucent preview of geometry that a design edit would remove (part coordinates). */
  showGhosts(cutters) {
    this.ghosts?.clear();
    if (!this.ghosts) this.scene.add((this.ghosts = new THREE.Group()));
    const material = new THREE.MeshBasicMaterial({ color: css("--support"), transparent: true, opacity: 0.45, depthWrite: false });
    for (const tris of cutters) {
      const { out } = placeTriangles(tris, this.placement.rotation, this.placement.shift);
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(out, 3));
      this.ghosts.add(new THREE.Mesh(g, material));
    }
  }
}

/** Rotate the triangle soup and sit it centred on the bed origin at z = 0 (matches fdmcore.checks.orient).
 * Pass ``fixedShift`` to place other geometry (ghosts) exactly like the part. */
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
