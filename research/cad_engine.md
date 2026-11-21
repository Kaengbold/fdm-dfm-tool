# A real CAD engine for design changes on STEP (T-526)

For the plan, see `research/redesign-2026-11/PLAN-COMBINED.md`: §5 point 3 ("The design"), §10.3 point 7 (Complete depth) and §16 decision 4. Decision 4 says that when only the design may change, strength comes mostly from design changes, and that this needs "a real CAD engine working on STEP".

This note compares the mesh path we have today with B-rep edits in OpenCascade, both measured on our own fixture. It ends with a recommendation and follow-up tasks.

## Recommendation (short)

1. **On STEP parts, do design edits as B-rep operations in OpenCascade**: fillet, chamfer, offset, booleans with exact primitives, and hole edits by feature. We already ship and load this engine for STEP write-back (`web/stepwrite-worker.js`, replicad's OCCT build). Every edit we need ran in 19–60 ms with a valid solid and kept real faces.
2. **The engine still decides what to change and where** (Python, voxels, today's rules). It sends an *edit intent* with numbers, for example "fillet the edge between faces 4 and 7, r 3 mm", instead of only triangles. Only the winning options are built, the same rule as §10.3 point 7.
3. **STL and 3MF parts keep the mesh path** (manifold-3d). It's 0.2 MB, fast and robust; the edits are faceted, but there is no B-rep to keep anyway.
4. **Any B-rep edit that fails falls back to the mesh edit** for that one change, with a plain note ("Couldn't make an exact fillet here; added a faceted one"). One bad fillet never blocks the rest.
5. **Phase it:** hole edits, slabs, voids and ribs first (booleans with exact tools, the most robust), then fillets and chamfers on picked edges, then offsets.

## What we have today

- **Import:** `occt-import-js` reads STEP into triangles plus a B-rep face id per triangle (`web/step-worker.js` → `api.load(face_ids=…)`). So the engine's regions already know their CAD face.
- **Edits:** the engine (`additions.py`, `features.py`) designs every edit as closed triangle pieces. Python never runs booleans (`booleans.py` is local only). The browser applies them with manifold-3d (`web/geometry.js`).
- **STEP write-back:** `web/stepwrite-worker.js` sews each triangle piece into a small solid and fuses or cuts it into the original B-rep, then merges coplanar faces. The untouched faces stay exact, but **every edited spot becomes facets**.
  - Measured: a Ø6 → Ø6.4 hole resize as a 64-segment mesh cut takes **741 ms** and leaves **73 faces** in the STEP.
  - The same edit as an exact cylinder cut takes **19 ms** and leaves **10 faces**, the same count as before.
  - A CAD user re-opening the file gets a hole made of 64 flat strips, which can't be re-dimensioned and breaks fillets next to it.

## Measured (Node 18, this PC; `node tools/bench_cad_engine.mjs`)

The fixture is `tools/fixtures/l_bracket.step`: 10 faces, 13 857 mm³. Engine: `replicad-opencascadejs@1.1.0`, the same build the app loads.

| Edit (the plan's design changes) | B-rep op | Time | Valid solid | Faces after | ΔV mm³ |
|---|---|---|---|---|---|
| Fillet the inside corner, r 3 | `BRepFilletAPI_MakeFillet` | 31 ms | yes | 11 | +58 |
| Chamfer the inside corner, 3 mm | `BRepFilletAPI_MakeChamfer` | 44 ms | yes | 11 | +135 |
| Thicken the whole part +0.5 mm | `BRepOffsetAPI_MakeOffsetShape` | 58 ms | yes | 41 | +3616 |
| Thicken the base locally (+2 mm slab) | box + fuse + unify | 32 ms | yes | 13 | +2400 |
| Hidden void (forces solid walls) | box + cut | 39 ms | yes | 17 | −1486 |
| Rib / gusset | prism + fuse | 36 ms | yes | 13 | +225 |
| Hole Ø6 → Ø6.4 | cylinder + cut | 19 ms | yes | 10 | −19 |
| Teardrop top on the sideways Ø8 hole | prism + cut | 40 ms | yes | 12 | −17 |

| Engine | Size | Start-up |
|---|---|---|
| OpenCascade (`replicad_single.wasm`) | 23.0 MB, **5.0 MB brotli** | 0.9–1.2 s |
| manifold-3d | 0.5 MB, 0.2 MB brotli | 0.03–0.16 s |

| Other operation | Time |
|---|---|
| STEP read | 53–66 ms |
| STEP write | 14–29 ms |
| Tessellation at 0.05 mm (for display and the engine's checks) | 16–25 ms |
| manifold union (bracket + slab) | ~4.5 ms |

In a browser, WASM usually runs 1.5–3× slower than in Node. That still puts a B-rep edit well under 0.2 s, which is fine because only the winning options are built (§10.3 point 7).

The OpenCascade download already happens for STEP write-back. Doing edits with it adds no download, it only moves the load earlier: when a STEP part enters design mode.

## Comparison

| | Mesh path (manifold-3d) | B-rep path (OpenCascade) |
|---|---|---|
| Holes: resize, teardrop, counterbore, nut pocket | Faceted; fine for printing | Exact; the CAD file stays editable |
| Slabs, hidden voids, ribs, gussets | Fine | Fine (booleans with exact boxes and prisms) |
| Fillets / chamfers at inside corners | Approximated by a faceted wedge | Exact; can fail on awkward edges (tangent faces, tiny faces next to the edge): needs the fallback |
| Thickening (offset) | Approximated by slabs | Exact for simple parts; offsets are OCCT's least robust operation: whole-part offsets only where the solid checks out, else slabs |
| STEP write-back | Edited spots become facets (73 faces for one hole) | Exact faces (10) |
| Size / start-up | 0.2 MB, instant | 5 MB brotli, ~1 s (already paid for write-back) |
| Works on STL / 3MF | Yes | No (no B-rep). Mesh-to-B-rep would only give facets |
| Licence | Apache-2.0 | OCCT LGPL-2.1 with exception; replicad MIT. Compatible with AGPL-3.0-or-later, and already shipped |

**Mapping picks to B-rep.**
- **Faces:** `face_ids` from the STEP import already tie each triangle and region to a B-rep face.
- **Edges:** an inside corner (T-521 `concave_edges`) between regions A and B is the shared edge of B-rep faces A and B. OCCT finds it with `TopExp.MapShapesAndAncestors`.
- **To check:** the face order from `occt-import-js` (`brep_faces`) must match OCCT's `TopExp_Explorer` order on the re-read file. Both walk the same STEP, but this needs a test (task T-527).

## Alternatives looked at

- **CadQuery / build123d in Pyodide:** they need OCP, a native OCCT binding with no Pyodide build. Not feasible.
- **truck (Rust B-rep → WASM):** small, but fillets and offsets are early-stage and there's no STEP write of edited solids that we could rely on. Not now.
- **Manifold plus feature recognition only:** smooth-looking edits with many segments, but STEP still gets facets. It doesn't solve decision 4.
- **occt-import-js** (what we use today): read only, so it can't do the edits. It stays the importer.

## Risks

- **Fillet and offset failures** on real-world STEP: tangent chains, small sliver faces, already-filleted edges. Mitigation: check validity (`BRepCheck_Analyzer`) after every op, fall back per edit, and test on a corpus of real STEP parts, not only the fixture.
- **Face-id drift** between the importer and OCCT (see above).
- **Memory:** OCCT plus a big STEP part in a worker can take a few hundred MB. Keep a single worker, free shapes (`.delete()`), and only build the winners.
- **Two code paths** for the same edit, so they can disagree. The engine's re-check (watertight, volume, slice) runs on the result of either path, as it does today.

## Follow-up tasks (added to TASKS.md, UI revision, Phase 2)

- **T-527:** test that STEP face ids match between the importer and OCCT; find a concave edge between two picked faces.
- **T-528:** edit intents from the engine (op + parameters + B-rep references) beside today's triangle pieces.
- **T-529:** the B-rep edit worker (booleans first) with a per-edit fallback to the mesh path.
- **T-534:** exact fillets and chamfers at inside corners on STEP parts.
- **T-535:** a STEP test corpus, and a robustness report for the B-rep edits.
