# Topology optimisation in the browser: what fits (T-291, research note)

Question: can the app run a real topology optimisation (the "lighter shape that still carries the
load" users see in CAD tools) on its own voxel strength grid, in Pyodide, in a time a hobbyist will
wait for? Short answer: **yes on a coarse grid, as an opt-in that takes 1–3 minutes**, with a few
changes to the solver. The method, the measured costs and a plan follow.

Status: the method references are standard textbook / paper knowledge, cited from memory (not
re-checked online for this note); the timings were measured here on 2026-11-06.

## Method: SIMP (the standard one)

Every voxel of the strength grid gets a density ρ between a small minimum and 1. Its stiffness is
E·ρ^p with p = 3, so half-dense material is a bad deal and the optimiser drives voxels to "solid" or
"empty". Each iteration:

1. one static solve (the existing `fea.solve`) with `scale = ρ_min + (1 − ρ_min)·ρ^p`;
2. sensitivities of compliance (how bendy the part is) per voxel: −p·ρ^(p−1)·uₑᵀ·kₑ·uₑ, where uₑᵀkₑuₑ
   is the element strain energy we already compute for stresses;
3. a density filter over about 1.5 voxels (stops chequerboards and features thinner than a line or two);
4. the optimality-criteria update (bisection on the volume limit, move limit 0.2).

Stop after ~30–60 iterations or when no density changes by more than 1 %. This is the scheme of
Sigmund's 99-line code (Struct. Multidisc. Optim. 21, 2001), its 88-line successor (Andreassen et
al., SMO 43, 2011) and the 3D version (Liu & Tovar, "An efficient 3D topology optimization code
written in Matlab", SMO 50, 2014). It needs only numpy and scipy, which the engine already loads.

Alternative: BESO (bi-directional evolutionary, Huang & Xie 2007) removes and re-adds whole voxels.
It gives clean solid / empty voxels directly (good for an STL), and we could drop empty voxels from the
model each round, so the solves get smaller over time. SIMP is better documented and smoother;
BESO is a reasonable second try if SIMP's grey zones are a problem.

What has to stay: voxels at loads, supports and screw holes (`LoadCase.ignore` / held nodes), the
outer skin the user marked as "keep" (optional), and nothing may grow into keep-clear boxes (we
only remove material, so the part's own outline is the design space).

## What it costs here (measured)

One static solve with `fea.solve` (CG with the two-level preconditioner), cantilever 120 × 12 × 12 mm,
native CPython, 2 threads (this PC, 2026-11-06):

| Grid (cubes) | Even density (0.5) | Smooth density field (0.1…1) | Sharp random solid / empty (ρ_min 10⁻³) |
|---|---|---|---|
| 3 300 | 0.22 s | 0.25 s | ~2 s |
| 12 900 | 1.0 s | 1.35 s | ~33 s |
| 40 700 | — | — | ~170 s, CG not converged in 2000 steps |

Pyodide is about 3× slower than native (AGENTS.md). So one iteration in the browser is about **1 s at
3 000 cubes and 3–4 s at 12 000** while the density field is smooth. 40 iterations: **~40 s (3 000)
and ~2.5 min (12 000)**.

The catch is the last column: once the optimiser has pushed voxels to near-empty, the stiffness
contrast (1000:1) makes CG converge badly, 10–30× slower. So late iterations would dominate unless:

- **ρ_min = 10⁻² instead of 10⁻³** (contrast 100:1; standard in practice, barely changes the shape);
- **warm start**: start CG from the last iteration's displacements (`x0`, already used in
  `lowest_mode`): densities change little between iterations, so a few dozen CG steps suffice;
- **drop near-empty voxels** once they've stayed empty for a few iterations (soft-kill, as in BESO):
  fewer unknowns and no extreme contrast;
- a looser tolerance in early iterations (rtol 10⁻³), tight only at the end.

With these, the cost should stay close to the "smooth" column throughout. To be confirmed by the
prototype.

## Plan (phase 2, after this note)

1. `core/fdmcore/topopt.py` (+ `make_manifest.py`): `optimise_topology(cases, up, profile, keep=0.4,
   iterations=40)` → densities per voxel. SIMP p = 3, ρ_min 10⁻², filter radius 1.5 h, OC update,
   warm-started CG (add `x0` to `fea.solve`), soft-kill after 5 iterations below 0.05. Load cases: sum
   of compliances (each case one solve per iteration: a cost to show in the time estimate).
   `progress.report` every iteration, and it can be cancelled like the other long runs.
2. `api.topology(up, loads, profile, keep, fine)`: default 3 000 cubes ("Quick", ~1 min), 12 000
   ("Fine", ~3 min), both with `askLong` and a time estimate from `runTimes` first. Result: the
   density field for the 3D view (the existing field / section view: voxels above 0.5 shown) and
   **an STL of the voxels above 0.5** (a blocky surface: no marching cubes in numpy/scipy without
   extra packages; fine as "rebuild this in CAD" guidance), plus the strength of that shape checked
   with the normal strength check and the plastic it saves.
3. UI: only when "What can you change?" includes the design. Wording: "A suggested lighter shape:
   keeps about 40 % of the plastic where the load runs. Use it as a guide to redesign in CAD;
   it's blocky and not a finished part."
4. Tests: the classic cantilever / MBB beam: material ends up along the top and bottom edges (a
   truss-like shape), the volume is right, keep regions stay solid, and it never adds plastic.
5. Threads: the solve runs in the Pyodide worker (one thread), native in the MCP server capped like
   the other solves, so the PC stays usable.

Not planned: infill-only optimisation as "topology" (that's T-290, zones), lattice infill, or
stress-constrained formulations (harder to converge; compliance is the usual and robust choice).
