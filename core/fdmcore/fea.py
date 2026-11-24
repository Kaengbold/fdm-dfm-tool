"""Rough linear-elastic FEA on a voxel grid, with FDM-style anisotropy.

Every solid voxel is an 8-node hexahedron. All elements share one stiffness
matrix (scaled per element for shell vs. infill), assembled into a sparse
system and solved with preconditioned conjugate gradients. The bed bond adds
nonlinear springs to this coarse, linear-elastic model.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

from .progress import report
from .voxel import VoxelGrid

# Hex8 node order in natural coordinates (ξ, η, ζ) ∈ {-1, 1}³.
NODES = np.array(list(product((-1, 1), repeat=3)))[:, ::-1]  # x fastest
VOIGT = [(0, 0), (1, 1), (2, 2), (1, 2), (0, 2), (0, 1)]


@dataclass
class Material:
    """Transversely isotropic: weaker and softer along the build direction (print Z)."""

    e_xy: float  # MPa
    e_z: float
    nu_xy: float = 0.35
    nu_zx: float = 0.3
    strength_xy: float = 50.0  # MPa, in-plane tensile
    strength_z: float = 25.0  # MPa, between layers

    def stiffness_print_frame(self) -> np.ndarray:
        """6x6 Voigt stiffness (xx, yy, zz, yz, xz, xy) with Z = build direction."""
        e1, e3, n12, n31 = self.e_xy, self.e_z, self.nu_xy, self.nu_zx
        g13 = e3 / (2 * (1 + n31))
        s = np.zeros((6, 6))
        s[0, 0] = s[1, 1] = 1 / e1
        s[2, 2] = 1 / e3
        s[0, 1] = s[1, 0] = -n12 / e1
        s[0, 2] = s[2, 0] = s[1, 2] = s[2, 1] = -n31 / e3
        s[3, 3] = s[4, 4] = 1 / g13
        s[5, 5] = 2 * (1 + n12) / e1
        return np.linalg.inv(s)


def rotate_stiffness(c: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Stiffness given in the print frame -> part frame, where print = r @ part."""
    t = np.zeros((3, 3, 3, 3))
    for a, (i, j) in enumerate(VOIGT):
        for b, (k, l) in enumerate(VOIGT):
            for p, q in {(i, j), (j, i)}:
                for m, n in {(k, l), (l, k)}:
                    t[p, q, m, n] = c[a, b]
    t = np.einsum("ai,bj,ck,dl,abcd->ijkl", r, r, r, r, t)
    return np.array([[t[i, j, k, l] for (k, l) in VOIGT] for (i, j) in VOIGT])


def b_matrix(xi, eta, zeta, h) -> np.ndarray:
    """Strain-displacement matrix (6 x 24) of a cube element with edge h."""
    nx, ny, nz = NODES.T
    dx = nx * (1 + ny * eta) * (1 + nz * zeta) / 8 * (2 / h)
    dy = ny * (1 + nx * xi) * (1 + nz * zeta) / 8 * (2 / h)
    dz = nz * (1 + nx * xi) * (1 + ny * eta) / 8 * (2 / h)
    b = np.zeros((6, 24))
    b[0, 0::3], b[1, 1::3], b[2, 2::3] = dx, dy, dz
    b[3, 1::3], b[3, 2::3] = dz, dy
    b[4, 0::3], b[4, 2::3] = dz, dx
    b[5, 0::3], b[5, 1::3] = dy, dx
    return b


def element_stiffness(c: np.ndarray, h: float) -> np.ndarray:
    g = 1 / np.sqrt(3)
    ke = np.zeros((24, 24))
    for xi, eta, zeta in product((-g, g), repeat=3):
        b = b_matrix(xi, eta, zeta, h)
        ke += b.T @ c @ b * (h / 2) ** 3
    return ke


@dataclass
class Model:
    grid: VoxelGrid
    elems: np.ndarray  # (E, 3) voxel indices of solid voxels
    edof: np.ndarray  # (E, 24) dof indices
    n_nodes: int
    node_index: np.ndarray  # (nx+1, ny+1, nz+1) -> node id or -1


def build_model(grid: VoxelGrid) -> Model:
    elems = np.argwhere(grid.solid)
    shape = np.array(grid.solid.shape) + 1
    corner = elems[:, None, :] + (NODES[None] + 1) // 2  # (E, 8, 3) node grid coords
    flat = np.ravel_multi_index(corner.reshape(-1, 3).T, shape)
    used, inverse = np.unique(flat, return_inverse=True)
    node_index = np.full(np.prod(shape), -1, dtype=np.int64)
    node_index[used] = np.arange(len(used))
    nodes = inverse.reshape(-1, 8)
    edof = (nodes[:, :, None] * 3 + np.arange(3)).reshape(-1, 24)
    return Model(grid, elems, edof, len(used), node_index.reshape(shape))


def solve(model: Model, c: np.ndarray, scale: np.ndarray, forces: np.ndarray, fixed: np.ndarray, rtol: float = 1e-4,
          springs: tuple | None = None, x0: np.ndarray | None = None, precond_cache: dict | None = None):
    """Displacements for nodal ``forces`` (n_nodes, 3) with ``fixed`` node mask (n_nodes,).
    ``springs`` (nodes, directions (m, 3), stiffness (m,) N/mm): supports that only hold along a
    direction, e.g. the face normal of a part resting on a surface. ``x0``: displacements to start
    from (n_nodes, 3), e.g. the last solve's when only the stiffness changed a little (topopt).
    ``precond_cache``: optional dict to reuse the coarse-grid LU factorisation across solves on the
    same model, stiffness tensor and supports (only accelerates CG; the solved system ``kf`` is exact)."""
    n = model.n_nodes * 3
    free = np.repeat(~fixed, 3)
    kf = _stiffness_free(model, c, scale, free, springs, precond_cache)
    f = forces.ravel()[free]
    reused = False
    cache_key = None
    if precond_cache is not None and springs is None:
        cache_key = (id(model), c.tobytes(), fixed.tobytes())
        cur_mean = float(np.mean(scale))
        entry = precond_cache.get(cache_key)
        if entry is not None and entry.get("model") is not model:
            entry = None
        if entry is not None and cur_mean > 0 and abs(np.log(cur_mean / entry["ref_mean"])) <= 0.6:
            m = _two_level_operator(kf, entry["p"], entry["coarse"], coarse_scale=entry["ref_mean"] / cur_mean)
            reused = True
        else:
            p = prolongation(model)[free]
            p = p[:, np.asarray(p.getnnz(axis=0) > 0).ravel()]
            coarse = _factor_coarse(kf, p)
            precond_cache[cache_key] = {"model": model, "p": p, "coarse": coarse, "ref_mean": max(cur_mean, 1e-12)}
            m = _two_level_operator(kf, p, coarse)
    else:
        m = two_level_preconditioner(kf, prolongation(model)[free])
    its = [0]

    def tick(_):
        its[0] += 1
        if its[0] % 10 == 0:  # iteration count is unknown up front; creep towards the end
            report(1 - 30 / (30 + its[0]), "Simulating")

    start = None if x0 is None else x0.ravel()[free]
    uf, info = spla.cg(kf, f, x0=start, rtol=rtol, maxiter=2000, M=m, callback=tick)
    if reused and its[0] > 28 and precond_cache is not None and cache_key is not None:
        precond_cache.pop(cache_key, None)
    u = np.zeros(n)
    u[free] = uf
    return u.reshape(-1, 3), info


def _stiffness_free(model: Model, c: np.ndarray, scale: np.ndarray, free: np.ndarray,
                    springs: tuple | None = None, precond_cache: dict | None = None) -> sp.csr_matrix:
    """Global stiffness matrix restricted to ``free`` DOFs, assembled directly with int32 indices.
    When ``precond_cache`` is given, the CSR sparsity pattern and COO->CSR scatter indices for
    ``(model, free)`` are built once and reused across orientations and shell recipes."""
    ke = element_stiffness(c, model.grid.h)
    n = model.n_nodes * 3
    nf = int(free.sum())
    if nf == 0:
        return sp.csr_matrix((0, 0))
    if precond_cache is not None:
        pat_key = ("_kf_pat", id(model), free.tobytes())
        held = precond_cache.get(pat_key)
        # id() can be reused once a model is freed: the entry keeps its model and must be the same object.
        pat = held[1] if held is not None and held[0] is model else None
        if pat is None:
            dof_map = np.full(n, -1, dtype=np.int32)
            dof_map[free] = np.arange(nf, dtype=np.int32)
            fe = dof_map[model.edof]
            rows = np.repeat(fe, 24, axis=1).ravel()
            cols = np.tile(fe, (1, 24)).ravel()
            keep = (rows >= 0) & (cols >= 0) if nf < n else None
            r, col = (rows[keep], cols[keep]) if keep is not None else (rows, cols)
            csr = sp.csr_matrix((np.ones(len(r), dtype=np.int8), (r, col)), shape=(nf, nf))
            csr.sort_indices()
            csr_row = np.repeat(np.arange(nf, dtype=np.int64), np.diff(csr.indptr))
            csr_key = csr_row * nf + csr.indices
            slot = np.searchsorted(csr_key, r.astype(np.int64) * nf + col).astype(np.int32)
            pat = (dof_map, keep, slot, csr.indices, csr.indptr, len(csr.data))
            precond_cache[pat_key] = (model, pat)
        dof_map, keep, slot, indices, indptr, nnz = pat
        vals = (scale[:, None] * ke.ravel()[None, :]).ravel()
        if keep is not None:
            vals = vals[keep]
        data = np.bincount(slot, weights=vals, minlength=nnz)
        kf = sp.csr_matrix((data, indices, indptr), shape=(nf, nf), copy=False)
    else:
        dof_map = np.full(n, -1, dtype=np.int32)
        dof_map[free] = np.arange(nf, dtype=np.int32)
        fe = dof_map[model.edof]
        rows = np.repeat(fe, 24, axis=1).ravel()
        cols = np.tile(fe, (1, 24)).ravel()
        vals = (scale[:, None] * ke.ravel()[None, :]).ravel()
        if nf < n:
            keep = (rows >= 0) & (cols >= 0)
            rows, cols, vals = rows[keep], cols[keep], vals[keep]
        kf = sp.csr_matrix((vals, (rows, cols)), shape=(nf, nf))
    if springs is not None and len(springs[0]):
        nodes, d, ks = springs
        block = ks[:, None, None] * d[:, :, None] * d[:, None, :]  # k · d dᵀ per node
        dof = dof_map[nodes[:, None] * 3 + np.arange(3)]
        sr = np.repeat(dof, 3, axis=1).ravel()
        sc = np.tile(dof, (1, 3)).ravel()
        sv = block.ravel()
        skeep = (sr >= 0) & (sc >= 0)
        if skeep.any():
            kf = kf + sp.csr_matrix((sv[skeep], (sr[skeep], sc[skeep])), shape=(nf, nf))
    return kf


def stiffness(model: Model, c: np.ndarray, scale: np.ndarray, springs: tuple | None = None) -> sp.csr_matrix:
    """Global stiffness matrix (N/mm), with ``springs`` as in ``solve``."""
    ke = element_stiffness(c, model.grid.h)
    n = model.n_nodes * 3
    edof = model.edof.astype(np.int32, copy=False)
    rows = np.repeat(edof, 24, axis=1).ravel()
    cols = np.tile(edof, (1, 24)).ravel()
    vals = (scale[:, None] * ke.ravel()[None, :]).ravel()
    k = sp.csr_matrix((vals, (rows, cols)), shape=(n, n))
    if springs is not None and len(springs[0]):
        nodes, d, ks = springs
        block = ks[:, None, None] * d[:, :, None] * d[:, None, :]  # k · d dᵀ per node
        dof = (nodes[:, None] * 3 + np.arange(3)).astype(np.int32)
        k = k + sp.csr_matrix((block.ravel(), (np.repeat(dof, 3, axis=1).ravel(), np.tile(dof, (1, 3)).ravel())), shape=(n, n))
    return k


def cohesive_bed(k: sp.csr_matrix, loads: np.ndarray, dofs: np.ndarray, area: np.ndarray,
                 initial_slope: float, peak: float, failure: float,
                 contact_slope: float, cache: dict, *, stop_at_lift: bool = False,
                 coarse_grid: sp.csr_matrix | None = None) -> tuple[np.ndarray, np.ndarray, bool]:
    """Relax the part after weakening the bond so load transfers to nearby springs.

    Slip and positive opening share an effective separation. Keep its maximum so
    a weakened spring cannot heal on unloading. Compression still meets the bed
    after the bond breaks. Return the opening seen during lift, peak separation
    history and convergence. The cache reuses the linear solver across grip trials.
    """
    u = cache["u"].copy() if "u" in cache else np.zeros(k.shape[0])
    largest = np.zeros(len(dofs))
    lift = largest.copy()
    intact = np.ones(len(dofs))
    diagonal = np.zeros(k.shape[0])
    normal_dofs = dofs[:, 2]
    contact_k = area * contact_slope
    for _ in range(80):
        opening = u[dofs]
        contact = opening[:, 2] < 0
        tension_k = area * initial_slope * intact
        diagonal[dofs] = tension_k[:, None]
        diagonal[normal_dofs] = np.where(contact, contact_k, tension_k)
        tangent = k + sp.diags(diagonal, format="csr")
        candidate = _bed_step(tangent, loads, u, cache, coarse_grid)
        if candidate is None or not np.isfinite(candidate).all():
            break
        next_gap = candidate[dofs]
        if np.any((contact != (next_gap[:, 2] < 0)) & (np.abs(next_gap[:, 2]) > 1e-8)):
            # A full step can cross the stiff contact branch. Backtrack on the
            # elastic energy before using the new contact set.
            delta = candidate - u
            linear = (k @ u - loads) @ delta
            quadratic = delta @ (k @ delta) / 2
            energy = np.sum(diagonal[dofs] * opening**2) / 2
            fraction = 1.0
            while fraction > 1e-6:
                gap = opening + fraction * delta[dofs]
                bond = tension_k @ np.sum(gap[:, :2]**2, axis=1)
                normal = np.where(gap[:, 2] < 0, contact_k, tension_k) @ gap[:, 2]**2
                change = (bond + normal) / 2 - energy
                if change + fraction * (linear + fraction * quadratic) <= 0:
                    break
                fraction /= 2
            if fraction <= 1e-6:
                break
            u += fraction * delta
            continue
        u = candidate
        opening = u[dofs]
        gap = np.maximum(0, opening[:, 2])
        separation = np.sqrt(np.sum(opening[:, :2]**2, axis=1) + gap**2)
        largest = np.maximum(largest, separation)
        softened = largest > peak * (1 + 1e-5)
        lift = np.maximum(lift, np.where((softened | (initial_slope == 0)) & (gap > 1e-8), gap, 0.0))
        above_peak = np.maximum(peak, largest)
        intact_next = (peak / above_peak) * np.clip((failure - above_peak) / (failure - peak), 0.0, 1.0)
        if initial_slope == 0 or (stop_at_lift and lift.any()) or np.max(intact - intact_next) < 1e-5:
            cache["u"] = u
            return lift, largest, True
        intact = intact_next
    return lift, largest, False


def _bed_step(k: sp.csr_matrix, loads: np.ndarray, u: np.ndarray, cache: dict,
              coarse_grid: sp.csr_matrix | None) -> np.ndarray | None:
    """Reuse a factor while spring stiffness changes, with a smaller factor for large parts."""
    preconditioner = cache.get("preconditioner")
    if preconditioner is not None:
        step, info = spla.cg(k, loads, x0=u, M=preconditioner, rtol=1e-7, maxiter=35)
        if info == 0:
            return step
    try:
        if coarse_grid is None:
            factor = spla.splu(k.tocsc(), permc_spec="MMD_AT_PLUS_A")
            cache["preconditioner"] = spla.LinearOperator(k.shape, matvec=factor.solve)
            return factor.solve(loads)
        preconditioner = cache["preconditioner"] = two_level_preconditioner(k, coarse_grid)
    except RuntimeError:  # A disconnected or degenerate grid can leave a free component.
        return None
    step, info = spla.cg(k, loads, x0=u, M=preconditioner, rtol=1e-7, maxiter=500)
    return step if info == 0 else None


def lowest_mode(model: Model, c: np.ndarray, scale: np.ndarray, node_mass: np.ndarray, fixed: np.ndarray,
                springs: tuple | None = None, block: int = 3, rtol: float = 1e-3, max_rounds: int = 30) -> tuple[float, np.ndarray]:
    """Lowest natural frequency (Hz) and its mode shape (n_nodes, 3, largest movement 1) with lumped
    node masses (kg). K is in N/mm, so ω² = 1000 · λ for K x = λ M x.

    Subspace iteration: each round solves K Y = M X for a few vectors with the same preconditioned CG
    as the static solve, then picks the best mix of them (Rayleigh-Ritz). Memory stays at a few vectors
    the size of the model; eigsh shift-invert (a direct factorisation) and LOBPCG both needed gigabytes
    on a 47k-unknown part. Several vectors because the lowest two modes are often a pair (bending one
    way or the other) that a single vector converges to only slowly. Loose tolerances (inner solves to
    1e-3, the frequency to 0.1 %): the Rayleigh-Ritz step still finds the frequency to 0.01 Hz on the
    test parts, in about 60 % of the time of 1e-6 / 1e-4."""
    free = np.repeat(~fixed, 3)
    kf = _stiffness_free(model, c, scale, free, springs).tocsr()
    m = np.repeat(node_mass, 3)[free]
    precond = two_level_preconditioner(kf, prolongation(model)[free])
    x = np.random.default_rng(0).standard_normal((kf.shape[0], block))
    y = np.zeros_like(x)
    lam = np.inf
    for i in range(max_rounds):
        report(i / max_rounds, "Finding how it swings")
        for j in range(block):
            y[:, j], _ = spla.cg(kf, m * x[:, j], x0=y[:, j], rtol=1e-3, maxiter=2000, M=precond)
        kr, mr = y.T @ (kf @ y), y.T @ (m[:, None] * y)
        vals, vecs = _small_eig((kr + kr.T) / 2, (mr + mr.T) / 2)
        x = y @ vecs
        y = x / vals  # next round's CG starts close: K⁻¹ M x ≈ x / λ
        done = abs(vals[0] - lam) <= rtol * vals[0]
        lam = vals[0]
        if done:
            break
    shape = np.zeros(model.n_nodes * 3)
    shape[free] = x[:, 0]
    shape = shape.reshape(-1, 3)
    shape /= max(np.linalg.norm(shape, axis=1).max(), 1e-12)
    return float(np.sqrt(max(lam, 0.0) * 1000) / (2 * np.pi)), shape


def _small_eig(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Generalised eigenpairs of a small symmetric pair (a, b positive definite), ascending, b-normalised."""
    w, v = np.linalg.eigh(b)
    t = v / np.sqrt(np.maximum(w, 1e-300))  # b = (t⁻¹)ᵀ t⁻¹, so tᵀ a t is the ordinary problem
    vals, u = np.linalg.eigh(t.T @ a @ t)
    return vals, t @ u


def prolongation(model: Model) -> sp.csr_matrix:
    """Trilinear interpolation from a 2x-coarser node grid to the model's dofs (cached on ``model``)."""
    cached = model.__dict__.get("_prolong")
    if cached is not None:
        return cached
    shape = np.array(model.node_index.shape)
    coords = np.argwhere(model.node_index >= 0)
    ids = model.node_index[tuple(coords.T)]
    cshape = shape // 2 + 1
    rows, cols, vals = [], [], []
    # Each fine coordinate maps to 1 (even) or 2 (odd) coarse coordinates per axis.
    for corner in product((0, 1), repeat=3):
        c = coords // 2 + np.array(corner)
        w = np.ones(len(coords))
        for ax in range(3):
            odd = coords[:, ax] % 2 == 1
            w *= np.where(odd, 0.5, np.where(np.array(corner)[ax] == 0, 1.0, 0.0))
        keep = w > 0
        rows.append(ids[keep])
        cols.append(np.ravel_multi_index(c[keep].T, cshape))
        vals.append(w[keep])
    rows, cols, vals = map(np.concatenate, (rows, cols, vals))
    used, cols = np.unique(cols, return_inverse=True)
    p = sp.csr_matrix((vals, (rows, cols)), shape=(model.n_nodes, len(used)))
    out = sp.kron(p, sp.eye(3), format="csr")  # same weights for x, y, z dofs
    model.__dict__["_prolong"] = out
    return out


def _factor_coarse(k: sp.csr_matrix, p: sp.csr_matrix):
    kc = (p.T @ k @ p).tocsc()
    # In thin features several coarse nodes can reach the same one or two fine nodes, which makes
    # their columns dependent and kc singular. A tiny diagonal shift only touches the preconditioner,
    # not the solution CG converges to.
    kc = kc + sp.identity(kc.shape[0], format="csc") * (1e-9 * kc.diagonal().mean())
    return spla.splu(kc)


def _two_level_operator(k: sp.csr_matrix, p: sp.csr_matrix, coarse, sweeps: int = 2, omega: float = 0.5,
                        coarse_scale: float = 1.0):
    dinv = omega / k.diagonal()

    def apply(r):
        x = dinv * r
        for _ in range(sweeps - 1):
            x += dinv * (r - k @ x)
        x += coarse_scale * (p @ coarse.solve(p.T @ (r - k @ x)))
        for _ in range(sweeps):
            x += dinv * (r - k @ x)
        return x

    return spla.LinearOperator(k.shape, matvec=apply)


def two_level_preconditioner(k: sp.csr_matrix, p: sp.csr_matrix, sweeps: int = 2, omega: float = 0.5):
    """Jacobi smoothing + exact coarse-grid correction, as a symmetric CG preconditioner.

    ``omega`` 0.6 sat just past the stable limit for parts mixing stiff wall voxels with soft infill:
    a 572k-triangle CAD part needed ~800 CG iterations instead of 12 (and the hook 29 instead of 13)."""
    p = p[:, np.asarray(p.getnnz(axis=0) > 0).ravel()]
    return _two_level_operator(k, p, _factor_coarse(k, p), sweeps=sweeps, omega=omega)


def element_stress(model: Model, c: np.ndarray, scale: np.ndarray, u: np.ndarray) -> np.ndarray:
    """Stress (E, 6) at each element centre, Voigt order (xx, yy, zz, yz, xz, xy)."""
    b = b_matrix(0, 0, 0, model.grid.h)
    ue = u.ravel()[model.edof]  # (E, 24)
    strain = ue @ b.T
    return scale[:, None] * (strain @ c.T)
