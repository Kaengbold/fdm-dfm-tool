// Do STEP face ids agree between the importer (occt-import-js, web/step-worker.js) and OpenCascade (replicad's build,
// which the edit worker uses)? Reads every test STEP both ways and compares each body's faces by area and centre, and
// finds the L-bracket's inside corner as the edge its two faces share (T-527). Prints JSON; core/tests/test_step_faces.py
// runs it. ``--dump DIR`` also writes each file's largest body as the engine gets it: triangles (NAME.stl) and the face
// id of each (NAME_faces.json), for the engine side of that test and for core/tests/step/.
// Dev-only:
//   npm install --no-save --no-package-lock --prefix tools replicad-opencascadejs@1.1.0 occt-import-js@0.0.23
//   node tools/check_step_faces.mjs [--dump DIR]
import Module from "replicad-opencascadejs";
import fs from "node:fs";
import os from "node:os";
import zlib from "node:zlib";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, "..");
// web/ has no package.json, so Node would read brep.js as CommonJS: load a copy as a module.
const copy = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "brep-")), "brep.mjs");
fs.copyFileSync(path.join(root, "web", "brep.js"), copy);
const B = await import(pathToFileURL(copy).href);
const occtimport = createRequire(import.meta.url)("occt-import-js");

const dump = process.argv.includes("--dump") ? process.argv[process.argv.indexOf("--dump") + 1] : null;
const files = [path.join(here, "fixtures", "l_bracket.step"),
  ...fs.readdirSync(path.join(root, "core", "tests", "corpus_step")).filter((f) => /\.ste?p(\.gz)?$/i.test(f)).sort()
    .map((f) => path.join(root, "core", "tests", "corpus_step", f))];

const importer = await occtimport();
const oc = await Module();

/** The importer's bodies as the app gets them: triangle soup and a face id per triangle (as step-worker.js). */
function imported(bytes) {
  const r = importer.ReadStepFile(bytes, { linearUnit: "millimeter", linearDeflectionType: "bounding_box_ratio", linearDeflection: 0.0005, angularDeflection: 0.3 });
  if (!r.success) throw new Error("importer failed");
  return r.meshes.map((m) => {
    const pos = m.attributes.position.array, idx = m.index.array;
    const soup = new Float32Array(idx.length * 3);
    for (let k = 0; k < idx.length; k++) soup.set([pos[idx[k] * 3], pos[idx[k] * 3 + 1], pos[idx[k] * 3 + 2]], k * 3);
    const faceIds = new Uint32Array(idx.length / 3);
    (m.brep_faces ?? []).forEach((f, id) => faceIds.fill(id, f.first, f.last + 1));
    return { soup, faceIds };
  });
}

function meshVolume(t) {
  let v = 0;
  for (let i = 0; i < t.length; i += 9) {
    v += t[i] * (t[i + 4] * t[i + 8] - t[i + 5] * t[i + 7]) - t[i + 1] * (t[i + 3] * t[i + 8] - t[i + 5] * t[i + 6]) + t[i + 2] * (t[i + 3] * t[i + 7] - t[i + 4] * t[i + 6]);
  }
  return v / 6;
}

const out = { files: [] };
for (const file of files) {
  const raw = fs.readFileSync(file);
  const bytes = new Uint8Array(file.endsWith(".gz") ? zlib.gunzipSync(raw) : raw); // the real-world parts are kept gzipped
  const bodies = imported(bytes);
  oc.FS.writeFile("/in.step", bytes);
  const reader = new oc.STEPControl_Reader();
  reader.ReadFile("/in.step");
  reader.TransferRoots(new oc.Message_ProgressRange());
  const all = B.solids(oc, reader.OneShape());
  // A body is the solid with the nearest volume (as the write-back worker picks it); a body with no solid of its
  // volume is a loose surface or a shell the importer also meshes, which the engine never edits.
  const volumes = all.map((s) => B.volume(oc, s));
  const checks = bodies.map((b) => {
    const v = Math.abs(meshVolume(b.soup));
    const k = volumes.reduce((best, w, i) => (Math.abs(w - v) < Math.abs(volumes[best] - v) ? i : best), 0);
    if (!all.length || Math.abs(volumes[k] - v) > 0.02 * Math.max(volumes[k], 1)) return { solid: null, volume: v };
    return { solid: k, ...B.checkFaceOrder(oc, all[k], b.soup, b.faceIds) };
  });
  out.files.push({ name: path.basename(file), bodies: bodies.length, solids: all.length, checks });

  if (path.basename(file) === "l_bracket.step") {
    // The inside corner as the engine sees it: the base's top face (z = 5) and the upright's inner face (x = -25),
    // picked from the importer's triangles, then the edge they share in OpenCascade.
    const sig = B.meshSignatures(bodies[0].soup, bodies[0].faceIds);
    const top = sig.findIndex((f) => Math.abs(f.centre[2] - 5) < 0.01 && f.area > 100);
    const wall = sig.findIndex((f) => Math.abs(f.centre[0] + 25) < 0.01 && f.area > 100);
    const edges = B.sharedEdges(oc, all[0], top, wall).map((e) => B.edgeSignature(oc, e));
    const apart = B.sharedEdges(oc, all[0], top, sig.findIndex((f) => Math.abs(f.centre[2] - 40) < 0.01));
    // And the comparison does catch ids that drifted: the same triangles with the face order turned round.
    const turned = bodies[0].faceIds.map((f) => sig.length - 1 - f);
    out.corner = { faces: [top, wall], edges, apart: apart.length, driftCaught: !B.checkFaceOrder(oc, all[0], bodies[0].soup, turned).ok };
  }
  if (dump) {
    fs.mkdirSync(dump, { recursive: true });
    // The body the app loads: the largest (web/step-worker.js sorts them by volume).
    const { soup, faceIds } = bodies.reduce((a, b) => (Math.abs(meshVolume(b.soup)) > Math.abs(meshVolume(a.soup)) ? b : a));
    const stl = Buffer.alloc(84 + faceIds.length * 50);
    stl.writeUInt32LE(faceIds.length, 80);
    for (let t = 0; t < faceIds.length; t++) for (let k = 0; k < 9; k++) stl.writeFloatLE(soup[t * 9 + k], 84 + t * 50 + 12 + k * 4);
    const name = path.basename(file).replace(/\.ste?p(\.gz)?$/i, "");
    fs.writeFileSync(path.join(dump, `${name}.stl`), stl);
    fs.writeFileSync(path.join(dump, `${name}_faces.json`), JSON.stringify(Array.from(faceIds)));
  }
  oc.FS.unlink("/in.step");
}
console.log(JSON.stringify(out));
