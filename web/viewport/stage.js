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

function partGroup(triangles, colors) {
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(triangles, 3));
  geo.computeVertexNormals();
  geo.computeBoundingBox();
  const bb = geo.boundingBox;
  geo.translate(-(bb.min.x + bb.max.x) / 2, -(bb.min.y + bb.max.y) / 2, -bb.min.z); // centred on the bed, resting on it
  geo.computeBoundingBox();
  geo.computeBoundingSphere();
  const group = new THREE.Group();
  group.add(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: colors.part, roughness: 0.62, metalness: 0.02 })));
  group.add(new THREE.LineSegments(new THREE.EdgesGeometry(geo, 28),
    new THREE.LineBasicMaterial({ color: colors.edge, transparent: true, opacity: 0.55 })));
  return { group, geo };
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
    this.controls.addEventListener("change", () => this.render());
    this.bed = new THREE.Group();
    this.scene.add(this.bed);
    this.part = null;
    this.bedSize = [256, 256];
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.attach(el);
    this.setBed(this.bedSize);
    const tick = () => { if (this.controls.update()) this.render(); this.frame = requestAnimationFrame(tick); };
    this.frame = requestAnimationFrame(tick);
    this.themeObserver = new MutationObserver(() => this.recolor());
    this.themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }

  /** Move the canvas into another stage element (the shell rebuilds its panels, the 3D view stays). */
  attach(el) {
    this.resizeObserver.disconnect();
    this.el = el;
    el.prepend(this.canvas);
    this.resizeObserver.observe(el);
    this.resize();
  }

  resize() {
    const { width, height } = this.el.getBoundingClientRect();
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.render();
  }

  render() { this.renderer.render(this.scene, this.camera); }

  setBed([w, d]) {
    this.bedSize = [w, d];
    this.bed.clear();
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
    if (this.part) { this.scene.remove(this.part.group); this.part.geo.dispose(); }
    this.part = partGroup(triangles, palette());
    this.scene.add(this.part.group);
    this.view("iso");
  }

  clearPart() {
    if (!this.part) return;
    this.scene.remove(this.part.group);
    this.part.geo.dispose();
    this.part = null;
    this.render();
  }

  /** Camera presets for the camera bar. */
  view(name) {
    const box = this.part?.geo.boundingBox ?? new THREE.Box3(new THREE.Vector3(-30, -30, 0), new THREE.Vector3(30, 30, 30));
    const size = box.getSize(new THREE.Vector3());
    const centre = box.getCenter(new THREE.Vector3());
    const dist = Math.max(size.length() / 2, 10) / Math.sin(THREE.MathUtils.degToRad(15)) * 1.1;
    const dir = { top: [0, -0.001, 1], front: [0, -1, 0.15], iso: [0.62, -0.78, 0.55], fit: null }[name] ?? null;
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
      this.part.group.children[0].material.color.set(c.part);
      this.part.group.children[1].material.color.set(c.edge);
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
  const { group, geo } = partGroup(triangles, palette());
  scene.add(group);
  const box = geo.boundingBox, s = box.getSize(new THREE.Vector3()), c = box.getCenter(new THREE.Vector3());
  const cam = new THREE.PerspectiveCamera(30, 1, 0.1, 10000);
  cam.up.set(0, 0, 1);
  const dist = (s.length() / 2) / Math.sin(THREE.MathUtils.degToRad(15));
  cam.position.copy(c).add(new THREE.Vector3(0.62, -0.78, 0.55).normalize().multiplyScalar(dist));
  cam.lookAt(c);
  thumbRenderer.render(scene, cam);
  geo.dispose();
  return thumbRenderer.domElement.toDataURL("image/png");
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
