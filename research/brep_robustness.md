# B-rep design edits on real STEP parts (T-535)

Made by `python tools/step_robustness.py`: every part in `core/tests/corpus_step/`, one engine edit of each kind it makes there (up to 3 resized holes, teardrops, rounds or chamfers at up to 4 inside corners, a slab on top, a hidden void, ribs), each applied alone by `web/brep.js` in headless Edge as the STEP write-back worker does it: exact, or faceted (its triangle pieces) when the exact edit fails, or left out. Then all together.

## By kind of edit

| Edit | Tried | Exact | of them smaller | Faceted instead | Left out | Median time | Slowest |
|---|---|---|---|---|---|---|---|
| hole_resize | 34 | 34 (100 %) | 0 | 0 | 0 | 374 ms | 1739 ms |
| teardrop | 41 | 38 (92 %) | 0 | 3 | 0 | 397 ms | 992 ms |
| slab | 15 | 15 (100 %) | 0 | 0 | 0 | 255 ms | 1240 ms |
| void | 15 | 15 (100 %) | 0 | 0 | 0 | 244 ms | 1511 ms |
| rib | 0 | – | – | – | – | – | – |
| fillet | 35 | 19 (54 %) | 10 | 0 | 16 | 397 ms | 1725 ms |
| chamfer | 15 | 15 (100 %) | 8 | 0 | 0 | 278 ms | 979 ms |

## Verdict

An edit kind is on by default when it came out exact at least 90% of the time and was never left out. Every kind stays on: what can't be made in the STEP file is named in the status, and the STL / 3MF export always has every change.

- **hole_resize**: on by default: exact almost always, never left out.
- **teardrop**: on by default: exact almost always, never left out.
- **slab**: on by default: exact almost always, never left out.
- **void**: on by default: exact almost always, never left out.
- **fillet**: exact when it works, else left out of the STEP file and named in the status (16 of 35 here).
- **chamfer**: on by default: exact almost always, never left out.

## By part

| Part | Faces | Read | Edits: exact / faceted / left out | All together | OpenCascade memory |
|---|---|---|---|---|---|
| gen_block.step | 26 | 330 ms | 8 / 0 / 0 | valid, 99 faces, 3956 ms | 127 MB |
| gen_knob.step | 10 | 263 ms | 5 / 0 / 0 | valid, 24 faces, 559 ms | 144 MB |
| gen_two_bodies.step | 6 | 252 ms | 2 / 0 / 0 | valid, 14 faces, 101 ms | 100 MB |
| nist_ctc_01_asme1_rd.stp.gz | 139 | 612 ms | 9 / 0 / 2 | valid, 183 faces, 3134 ms | 239 MB |
| nist_ctc_02_asme1_rc.stp.gz | 663 | 1508 ms | 6 / 0 / 2 | valid, 519 faces, 7439 ms | 595 MB |
| nist_ctc_03_asme1_rc.stp.gz | 139 | 600 ms | 5 / 0 / 4 | valid, 150 faces, 3132 ms | 163 MB |
| nist_ctc_04_asme1_rd.stp.gz | 517 | 895 ms | 7 / 0 / 2 | valid, 421 faces, 5741 ms | 595 MB |
| nist_ctc_05_asme1_rd.stp.gz | 208 | 740 ms | 9 / 0 / 0 | valid, 183 faces, 3033 ms | 264 MB |
| nist_ftc_06_asme1_rd.stp.gz | 144 | 662 ms | 17 / 0 / 2 | valid, 213 faces, 4225 ms | 413 MB |
| nist_ftc_07_asme1_rd.stp.gz | 269 | 704 ms | 25 / 3 / 0 | valid, 398 faces, 7165 ms | 811 MB |
| nist_ftc_08_asme1_rc.stp.gz | 270 | 645 ms | 7 / 0 / 2 | valid, 278 faces, 3484 ms | 287 MB |
| nist_ftc_09_asme1_rd.stp.gz | 158 | 505 ms | 7 / 0 / 2 | valid, 158 faces, 2647 ms | 199 MB |
| nist_ftc_10_asme1_rb.stp.gz | 214 | 691 ms | 19 / 0 / 0 | valid, 293 faces, 5259 ms | 496 MB |
| nist_ftc_11_asme1_rb.stp.gz | 6 | 229 ms | 5 / 0 / 0 | valid, 20 faces, 643 ms | 178 MB |
| occt_screw.step.gz | 10 | 278 ms | 5 / 0 / 0 | valid, 15 faces, 453 ms | 120 MB |

## Why exact edits fell back

| Edit | Why | Times |
|---|---|---|
| fillet | OpenCascade could not make the chamfer; too many faceted pieces to add to the CAD part | 6 |
| fillet | OpenCascade could not make the round; too many faceted pieces to add to the CAD part | 4 |
| fillet | the result wasn't a valid solid; too many faceted pieces to add to the CAD part | 6 |
| teardrop | the outline isn't flat | 3 |

## Sources

The parts: `nist_*` are the eleven NIST MBE PMI validation and conformance test parts (CTC 1–5, FTC 6–11; from NIST's MBE PMI Validation and Conformance Testing project; US government work, public domain), geometry-only AP203 files, gzipped; `occt_screw` is OpenCascade's screw sample (LGPL-2.1 with exception, from the OCCT data folder); `gen_*` are made by `tools/make_step_corpus.mjs`.
