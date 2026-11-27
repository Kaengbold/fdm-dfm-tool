// Hover picker for the 3D stage (T-540, plan §7 "Hover before click", §1.8): what is under the cursor — a face region
// (flat, cylinder, hole, curved), a sharp edge or an inside corner. Edges within ~6 px win over the face. Hovering
// only shows (tint, outline, a small badge); a click hands the hit to the step (onPick), which opens its menu.
//
// Fast on big parts: the face under the cursor comes from the GPU (each triangle drawn in its own colour into a
// 1 × 1 render target at the cursor), not from testing every triangle in JavaScript; edges are tested on screen
// only on the hovered face's own edges, so hidden edges behind the part never win.
import * as THREE from "three";

const EDGE_PX = 6;
const ID_VERTEX = `attribute vec3 idColor; varying vec3 vId;
void main() { vId = idColor; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const ID_FRAGMENT = `varying vec3 vId; void main() { gl_FragColor = vec4(vId, 1.0); }`;

/** Region data from api.regions(): {face_regions (uint32 bytes), regions [{id, kind, area_mm2, normal, hole_diameter?,
 * diameter?}], feature_edges / concave_edges {points (float32 bytes), offsets (uint32 bytes), meta [{length_mm, regions}]}}. */
export function unpackRegions(data) {
  const u32 = (b) => new Uint32Array(new Uint8Array(b).slice().buffer);
  const f32 = (b) => new Float32Array(new Uint8Array(b).slice().buffer);
  const lines = (packed, kind) => {
    if (!packed?.meta?.length) return [];
    const pts = f32(packed.points), off = u32(packed.offsets);
    return packed.meta.map((m, i) => ({ kind, index: i, meta: m, points: pts.subarray(off[i] * 3, off[i + 1] * 3) }));
  };
  return {
    faceRegion: u32(data.face_regions),
    regions: data.regions,
    edges: [...lines(data.feature_edges, "edge"), ...lines(data.concave_edges, "corner")],
  };
}

export class Picker {
  /** ``stage``: viewport/stage.js Stage with a part shown. ``label(hit)`` → badge text (the caller translates). */
  constructor(stage, { label = () => "", hoverColor = "#38BDF8" } = {}) {
    this.stage = stage;
    this.label = label;
    this.hover = new THREE.Color(hoverColor);
    this.enabled = false;
    this.hit = null;
    this.listeners = { hover: new Set(), pick: new Set() };
    this.target = new THREE.WebGLRenderTarget(1, 1);
    this.pixel = new Uint8Array(4);
    this.badge = Object.assign(document.createElement("div"), { className: "pick-badge", hidden: true });
    this.badge.setAttribute("role", "status");
    this.edgeLines = new THREE.Group();
    stage.scene.add(this.edgeLines);
    this.pending = null;
    const canvas = stage.canvas;
    canvas.addEventListener("pointermove", (e) => this.queue(e));
    canvas.addEventListener("pointerleave", () => this.setHit(null));
    canvas.addEventListener("pointerdown", (e) => { this.down = [e.clientX, e.clientY]; });
    canvas.addEventListener("click", (e) => {
      // A drag turns the camera; only a click (no movement) picks.
      if (!this.enabled || !this.hit || (this.down && Math.hypot(e.clientX - this.down[0], e.clientY - this.down[1]) > 4)) return;
      this.listeners.pick.forEach((fn) => fn(this.hit, e.clientX, e.clientY));
    });
    stage.onRecolor = () => this.recolor();
  }

  on(event, fn) { this.listeners[event].add(fn); return () => this.listeners[event].delete(fn); }

  /** Repaint the part after a theme change: new base colour, any step region colours, then the hover highlight. */
  recolor() {
    // Only on picking steps: elsewhere the part may carry another step's paint (overhangs, heat), and the
    // region colours left from an earlier step must not come back.
    if (!this.stage.part || !this.enabled) return;
    this.stage.paint(() => null);
    if (this.data && this.colorFor) {
      this.colored = null;
      this.setRegionColors(this.colorFor);
    }
    this.retint();
  }

  /** Drop region data when a new part is loaded so stale regions aren't painted onto it. */
  clearData() {
    this.data = null;
    this.colored = null;
    this.tinted = null;
    this.hit = null;
    this.badge.hidden = true;
    this.edgeLines.clear();
    this.outlines = new Map();
  }

  /** Colour regions a step marks (holds, loads): ``colorFor(region)`` → THREE.Color or null. Repaints them now. */
  setRegionColors(colorFor) {
    const before = this.colored ?? new Set();
    this.colorFor = colorFor;
    if (!this.data || !this.stage.part) return;
    const now = new Set();
    const colors = new Map();
    for (let r = 0; r < this.data.regions.length; r++) {
      const c = colorFor(r);
      if (c) { now.add(r); colors.set(r, c); }
    }
    const dirty = new Set([...before, ...now]);
    if (!dirty.size) { this.colored = now; return; }
    const attr = this.stage.part.geo.attributes.color;
    const arr = attr.array;
    const base = this.stage.part.base;
    // Write every changed region into the vertex buffer before one single WebGL upload + render.
    for (const r of dirty) {
      const c = colors.get(r) ?? base;
      const cr = c.r, cg = c.g, cb = c.b;
      const tris = this.trianglesOf(r);
      for (let k = 0; k < tris.length; k++) {
        const o = tris[k] * 9;
        arr[o] = cr; arr[o + 1] = cg; arr[o + 2] = cb;
        arr[o + 3] = cr; arr[o + 4] = cg; arr[o + 5] = cb;
        arr[o + 6] = cr; arr[o + 7] = cg; arr[o + 8] = cb;
      }
    }
    attr.needsUpdate = true;
    this.stage.render();
    this.colored = now;
  }

  /** Where a part-coordinate point is on screen (client px), for markers and menus. */
  toClient(point) {
    if (!this.stage.part) return { x: 0, y: 0, behind: true };
    const v = new THREE.Vector3(...point).add(this.stage.part.offset).project(this.stage.camera);
    const r = this.stage.canvas.getBoundingClientRect();
    return { x: (v.x + 1) / 2 * r.width + r.left, y: (1 - v.y) / 2 * r.height + r.top, behind: v.z > 1 };
  }

  /** New part data (call after stage.showPart with the same triangles). */
  setData(data) {
    this.data = unpackRegions(data);
    const n = this.data.faceRegion.length;
    // Triangles of each region (CSR), for tinting and outlines.
    const counts = new Uint32Array(this.data.regions.length + 1);
    for (let t = 0; t < n; t++) counts[this.data.faceRegion[t] + 1]++;
    for (let r = 1; r < counts.length; r++) counts[r] += counts[r - 1];
    const order = new Uint32Array(n), fill = counts.slice();
    for (let t = 0; t < n; t++) order[fill[this.data.faceRegion[t]]++] = t;
    this.regionStart = counts;
    this.regionOrder = order;
    this.buildIdMesh();
    this.colored = null;
    this.tinted = null;
    this.outlines = new Map();
    this.setHit(null);
  }

  /** The triangle-id twin of the part, rendered off screen for the GPU pick. */
  buildIdMesh() {
    this.idMesh?.geometry.dispose();
    const geo = this.stage.part?.geo;
    if (!geo) { this.idMesh = null; this.idScene = null; return; }
    const n = geo.attributes.position.count / 3;
    const ids = new Uint8Array(n * 9);
    for (let t = 0; t < n; t++) {
      const id = t + 1; // 0 = nothing
      for (let v = 0; v < 3; v++) ids.set([id & 255, (id >> 8) & 255, (id >> 16) & 255], (t * 3 + v) * 3);
    }
    const idGeo = new THREE.BufferGeometry();
    idGeo.setAttribute("position", geo.attributes.position);
    idGeo.setAttribute("idColor", new THREE.BufferAttribute(ids, 3, true));
    this.idMesh = new THREE.Mesh(idGeo, new THREE.ShaderMaterial({ vertexShader: ID_VERTEX, fragmentShader: ID_FRAGMENT, side: THREE.DoubleSide }));
    this.idScene = new THREE.Scene();
    this.idScene.add(this.idMesh);
  }

  enable(on = true, host = this.stage.el) {
    this.enabled = on;
    if (host && this.badge.parentElement !== host) host.append(this.badge);
    if (!on) this.setHit(null);
  }

  queue(e) {
    if (!this.enabled || !this.data) return;
    this.pending = e;
    this.frame ??= requestAnimationFrame(() => { this.frame = null; const ev = this.pending; this.pending = null; this.setHit(this.pickAt(ev.clientX, ev.clientY)); });
  }

  /** The triangle under a client point, from the GPU (−1 if none). */
  triangleAt(x, y) {
    if (!this.stage.part?.geo) return -1;
    if (!this.idScene) this.buildIdMesh();
    const { renderer, camera, canvas } = this.stage;
    const r = canvas.getBoundingClientRect();
    const dpr = renderer.getPixelRatio();
    const w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
    camera.setViewOffset(w, h, Math.round((x - r.left) * dpr), Math.round((y - r.top) * dpr), 1, 1);
    const before = renderer.getRenderTarget();
    renderer.setRenderTarget(this.target);
    renderer.setClearColor(0x000000, 1);
    renderer.clear();
    renderer.render(this.idScene, camera);
    renderer.readRenderTargetPixels(this.target, 0, 0, 1, 1, this.pixel);
    renderer.setRenderTarget(before);
    renderer.setClearColor(0x000000, 0);
    camera.clearViewOffset();
    return this.pixel[0] + this.pixel[1] * 256 + this.pixel[2] * 65536 - 1;
  }

  /** What is under the client point: {kind: face|edge|corner, region, info, point (part coords), …} or null. */
  pickAt(x, y) {
    if (!this.data || !this.stage.part?.geo) return null;
    const t = this.triangleAt(x, y);
    if (t < 0 || t >= this.data.faceRegion.length) return null;
    const region = this.data.faceRegion[t];
    const point = this.surfacePoint(t, x, y);
    // An edge or inside corner of this face within EDGE_PX wins (only this face's edges: hidden ones can't).
    const edge = this.nearestEdge(region, x, y);
    if (edge) return { kind: edge.line.kind, edge: edge.line.index, info: edge.line.meta, region, point, line: edge.line };
    return { kind: "face", region, info: this.data.regions[region], point, triangle: t };
  }

  surfacePoint(t, x, y) {
    const { camera, canvas } = this.stage;
    const r = canvas.getBoundingClientRect();
    const ray = new THREE.Raycaster();
    ray.setFromCamera(new THREE.Vector2(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1), camera);
    const p = this.stage.part.geo.attributes.position;
    const a = new THREE.Vector3().fromBufferAttribute(p, t * 3), b = new THREE.Vector3().fromBufferAttribute(p, t * 3 + 1);
    const c = new THREE.Vector3().fromBufferAttribute(p, t * 3 + 2);
    const hit = ray.ray.intersectTriangle(a, b, c, false, new THREE.Vector3()) ?? a.clone().add(b).add(c).divideScalar(3);
    return hit.sub(this.stage.part.offset).toArray().map((v) => Math.round(v * 1000) / 1000);
  }

  nearestEdge(region, x, y) {
    const { camera, canvas } = this.stage;
    const r = canvas.getBoundingClientRect();
    const off = this.stage.part.offset;
    const v = new THREE.Vector3();
    const screen = (i, pts) => {
      v.set(pts[i] + off.x, pts[i + 1] + off.y, pts[i + 2] + off.z).project(camera);
      return [(v.x + 1) / 2 * r.width + r.left, (1 - v.y) / 2 * r.height + r.top];
    };
    let best = null;
    for (const line of this.data.edges) {
      if (!line.meta.regions?.includes(region)) continue;
      const pts = line.points;
      let prev = screen(0, pts);
      for (let i = 3; i < pts.length; i += 3) {
        const cur = screen(i, pts);
        const d = segmentDistance(x, y, prev, cur);
        if (d <= EDGE_PX && (!best || d < best.d)) best = { d, line };
        prev = cur;
      }
    }
    return best;
  }

  setHit(hit) {
    const same = (a, b) => a?.kind === b?.kind && a?.region === b?.region && a?.edge === b?.edge;
    if (same(hit, this.hit)) { if (hit) this.hit = hit; return; }
    this.hit = hit;
    this.retint();
    this.badge.hidden = !hit;
    if (hit && this.stage.part) {
      this.badge.textContent = this.label(hit);
      const p = new THREE.Vector3(...hit.point).add(this.stage.part.offset).project(this.stage.camera);
      const host = this.badge.parentElement?.getBoundingClientRect();
      const r = this.stage.canvas.getBoundingClientRect();
      if (host) {
        this.badge.style.left = `${(p.x + 1) / 2 * r.width + r.left - host.left + 12}px`;
        this.badge.style.top = `${(1 - p.y) / 2 * r.height + r.top - host.top - 28}px`;
      }
    }
    this.stage.canvas.style.cursor = hit ? "pointer" : "";
    this.listeners.hover.forEach((fn) => fn(hit));
  }

  /** Tint the hovered region (soft) or draw the hovered edge; everything else back to the part colour. */
  retint() {
    if (!this.data || !this.stage.part?.geo) return;
    const base = this.stage.part.base;
    const tint = base.clone().lerp(this.hover, 0.55);
    // A region a step coloured (e.g. a hold) gets its own colour back, not the plain part colour.
    if (this.tinted != null) { const keep = this.colorFor?.(this.tinted) ?? base; this.stage.paint(() => keep, this.trianglesOf(this.tinted)); }
    this.tinted = null;
    this.edgeLines.clear();
    const hit = this.hit;
    if (hit?.kind === "face") {
      this.tinted = hit.region;
      this.stage.paint(() => tint, this.trianglesOf(hit.region));
      this.edgeLines.add(this.outline(hit.region));
    } else if (hit) {
      const pts = hit.line.points, off = this.stage.part.offset;
      const arr = new Float32Array(pts.length);
      for (let i = 0; i < pts.length; i += 3) arr.set([pts[i] + off.x, pts[i + 1] + off.y, pts[i + 2] + off.z], i);
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(arr, 3));
      this.edgeLines.add(new THREE.Line(g, new THREE.LineBasicMaterial({ color: this.hover, depthTest: false, linewidth: 2 })));
    }
    this.stage.render();
  }

  trianglesOf(region) {
    return this.regionOrder.subarray(this.regionStart[region], this.regionStart[region + 1]);
  }

  /** The region's border as lines (edges used by one of its triangles only), cached per region. */
  outline(region) {
    if (!this.outlines.has(region)) {
      const tris = this.trianglesOf(region);
      const pos = this.stage.part.geo.attributes.position;
      const key = (i) => `${pos.getX(i).toFixed(3)},${pos.getY(i).toFixed(3)},${pos.getZ(i).toFixed(3)}`;
      const count = new Map();
      if (tris.length <= 60_000) {
        for (const t of tris) for (let k = 0; k < 3; k++) {
          const a = t * 3 + k, b = t * 3 + ((k + 1) % 3);
          const ka = key(a), kb = key(b), id = ka < kb ? `${ka}|${kb}` : `${kb}|${ka}`;
          const e = count.get(id);
          if (e) e.n++; else count.set(id, { n: 1, a, b });
        }
      }
      const arr = [];
      for (const { n, a, b } of count.values()) if (n === 1) arr.push(pos.getX(a), pos.getY(a), pos.getZ(a), pos.getX(b), pos.getY(b), pos.getZ(b));
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(arr), 3));
      this.outlines.set(region, g);
    }
    return new THREE.LineSegments(this.outlines.get(region), new THREE.LineBasicMaterial({ color: this.hover, depthTest: false }));
  }

  /** Keyboard: the features in a list (faces with a name, then edges and inside corners), for the stage's list. */
  features() {
    if (!this.data) return [];
    const faces = this.data.regions.map((info, region) => ({ kind: "face", region, info }))
      .filter((f) => f.info.kind !== "flat" || f.info.area_mm2 >= 1)
      .sort((a, b) => b.info.area_mm2 - a.info.area_mm2);
    const lines = this.data.edges.slice().sort((a, b) => b.meta.length_mm - a.meta.length_mm).slice(0, 60)
      .map((line) => ({ kind: line.kind, edge: line.index, info: line.meta, region: line.meta.regions?.[0], line }));
    return [...faces, ...lines].map((h) => ({ ...h, point: h.kind === "face" ? h.info.centroid : h.info.midpoint }));
  }

  /** Show a feature as hovered (from the keyboard list), or clear with null. */
  showFeature(hit) { this.setHit(hit); }
  pickFeature(hit) {
    if (!this.stage.part) return;
    const p = new THREE.Vector3(...hit.point).add(this.stage.part.offset).project(this.stage.camera);
    const r = this.stage.canvas.getBoundingClientRect();
    this.listeners.pick.forEach((fn) => fn(hit, (p.x + 1) / 2 * r.width + r.left, (1 - p.y) / 2 * r.height + r.top));
  }
}

function segmentDistance(px, py, [ax, ay], [bx, by]) {
  const dx = bx - ax, dy = by - ay;
  const len = dx * dx + dy * dy;
  const t = len ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len)) : 0;
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}
