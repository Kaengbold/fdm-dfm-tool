// The hold brush (T-573, plan §7 "paint part of a big face"): painted spots as flat discs on the surface, so a
// patch shows even on a big face made of two triangles. In the part's group, part coordinates.
import * as THREE from "three";

const NAME = "hold-brush";

/** Discs at each painted spot ({point, normal, r?}: its own radius, else ``radius`` mm), in ``color``; replaces what was drawn. */
export function showBrush(stage, spots, radius, color) {
  clearBrush(stage);
  if (!stage.part || !spots.length) return;
  const g = new THREE.Group();
  g.name = NAME;
  const geos = new Map(); // one disc per radius
  const geoOf = (r) => { if (!geos.has(r)) geos.set(r, new THREE.CircleGeometry(r, 24)); return geos.get(r); };
  const material = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.55, depthWrite: false, side: THREE.DoubleSide,
    polygonOffset: true, polygonOffsetFactor: -2 });
  const z = new THREE.Vector3(0, 0, 1);
  const o = stage.part.offset;
  for (const s of spots) {
    const n = new THREE.Vector3(...(s.normal ?? [0, 0, 1])).normalize();
    const disc = new THREE.Mesh(geoOf(s.r ?? radius), material);
    disc.quaternion.setFromUnitVectors(z, n);
    disc.position.set(s.point[0] + o.x, s.point[1] + o.y, s.point[2] + o.z).addScaledVector(n, 0.05);
    disc.renderOrder = 2;
    g.add(disc);
  }
  stage.part.group.add(g);
  stage.render();
}

export function clearBrush(stage) {
  const g = stage.part?.group.getObjectByName(NAME);
  if (!g) return;
  for (const geo of new Set(g.children.map((c) => c.geometry))) geo.dispose();
  g.children[0]?.material.dispose();
  g.removeFromParent();
  stage.render();
}
