"""Rough linear-elastic FEA on a voxel grid, with FDM-style anisotropy.

Every solid voxel is an 8-node hexahedron. All elements share one stiffness
matrix (scaled per element for shell vs. infill), assembled into a sparse
system and solved with preconditioned conjugate gradients.

This is an indication tool: coarse mesh, linear material, no contact.
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
          springs: tuple | None = None):
    """Displacements for nodal ``forces`` (n_nodes, 3) with ``fixed`` node mask (n_nodes,).
    ``springs`` (nodes, directions (m, 3), stiffness (m,) N/mm): supports that only hold along a
    direction, e.g. the face normal of a part resting on a surface."""
    k = stiffness(model, c, scale, springs)
    n = model.n_nodes * 3
    free = np.repeat(~fixed, 3)
    kf = k[free][:, free]
    f = forces.ravel()[free]
    m = two_level_preconditioner(kf, prolongation(model)[free])
    its = [0]

    def tick(_):
        its[0] += 1
        if its[0] % 10 == 0:  # iteration count is unknown up front; creep towards the end
            report(1 - 30 / (30 + its[0]), "Simulating")

    uf, info = spla.cg(kf, f, rtol=rtol, maxiter=2000, M=m, callback=tick)
    u = np.zeros(n)
    u[free] = uf
    return u.reshape(-1, 3), info


def stiffness(model: Model, c: np.ndarray, scale: np.ndarray, springs: tuple | None = None) -> sp.csr_matrix:
    """Global stiffness matrix (N/mm), with ``springs`` as in ``solve``."""
    ke = element_stiffness(c, model.grid.h)
    n = model.n_nodes * 3
    rows = np.repeat(model.edof, 24, axis=1).ravel()
    cols = np.tile(model.edof, (1, 24)).ravel()
    vals = (scale[:, None] * ke.ravel()[None, :]).ravel()
    k = sp.csr_matrix((vals, (rows, cols)), shape=(n, n))
    if springs is not None and len(springs[0]):
        nodes, d, ks = springs
        block = ks[:, None, None] * d[:, :, None] * d[:, None, :]  # k · d dᵀ per node
        dof = nodes[:, None] * 3 + np.arange(3)
        k = k + sp.csr_matrix((block.ravel(), (np.repeat(dof, 3, axis=1).ravel(), np.tile(dof, (1, 3)).ravel())), shape=(n, n))
    return k


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
    k = stiffness(model, c, scale, springs)
    free = np.repeat(~fixed, 3)
    kf = k[free][:, free].tocsr()
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
    """Trilinear interpolation from a 2x-coarser node grid to the model's dofs."""
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
    return sp.kron(p, sp.eye(3), format="csr")  # same weights for x, y, z dofs


def two_level_preconditioner(k: sp.csr_matrix, p: sp.csr_matrix, sweeps: int = 2, omega: float = 0.5):
    """Jacobi smoothing + exact coarse-grid correction, as a symmetric CG preconditioner.

    ``omega`` 0.6 sat just past the stable limit for parts mixing stiff wall voxels with soft infill:
    a 572k-triangle CAD part needed ~800 CG iterations instead of 12 (and the hook 29 instead of 13)."""
    p = p[:, np.asarray(p.getnnz(axis=0) > 0).ravel()]
    kc = (p.T @ k @ p).tocsc()
    # In thin features several coarse nodes can reach the same one or two fine nodes, which makes
    # their columns dependent and kc singular. A tiny diagonal shift only touches the preconditioner,
    # not the solution CG converges to.
    kc = kc + sp.identity(kc.shape[0], format="csc") * (1e-9 * kc.diagonal().mean())
    coarse = spla.splu(kc)
    dinv = omega / k.diagonal()

    def apply(r):
        x = dinv * r
        for _ in range(sweeps - 1):
            x += dinv * (r - k @ x)
        x += p @ coarse.solve(p.T @ (r - k @ x))
        for _ in range(sweeps):
            x += dinv * (r - k @ x)
        return x

    return spla.LinearOperator(k.shape, matvec=apply)


def element_stress(model: Model, c: np.ndarray, scale: np.ndarray, u: np.ndarray) -> np.ndarray:
    """Stress (E, 6) at each element centre, Voigt order (xx, yy, zz, yz, xz, xy)."""
    b = b_matrix(0, 0, 0, model.grid.h)
    ue = u.ravel()[model.edof]  # (E, 24)
    strain = ue @ b.T
    return scale[:, None] * (strain @ c.T)
