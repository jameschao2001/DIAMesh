"""DIAMesh — pre-repair mesh-health diagnostic.

A complement to :mod:`diamesh.diff`:

* ``diff`` quantifies *how much* a repaired mesh deviates from the
  original (Hausdorff / Chamfer / volume / normal deviation).
* ``diagnose`` quantifies *what is wrong with* the input mesh before any
  repair (watertightness, non-manifold edges, degenerate faces, floating
  islands, inverted normals).

Together they form the "look first, measure after" closed loop that
matches the production design philosophy: cure-vs-patch is only useful
when you can measure both ends.

Author: James Chao, Homi (AI Agent)
Version: 0.1.0
Date: 2026-05-02
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh

from diamesh.loader import load_fbx


def _load_concat(path: str | Path) -> trimesh.Trimesh:
    """Load a mesh and concatenate any multi-part scene into one ``Trimesh``."""
    p = Path(path)
    if p.suffix.lower() == ".fbx":
        meshes = load_fbx(p)
    else:
        loaded = trimesh.load(str(p), force="mesh")
        if isinstance(loaded, trimesh.Trimesh):
            meshes = [loaded]
        else:
            meshes = list(loaded.geometry.values())
    if not meshes:
        raise RuntimeError(f"No meshes loaded from {path}")
    if len(meshes) == 1:
        return meshes[0]
    return trimesh.util.concatenate(meshes)


def _count_non_manifold_edges(mesh: trimesh.Trimesh) -> int:
    """Count edges shared by 3 or more faces (non-manifold).

    Uses the raw ``mesh.edges`` array (3 × n_faces edges with duplicates)
    and groups by sorted vertex pair.
    """
    if mesh.faces.shape[0] == 0:
        return 0
    edges = np.sort(mesh.edges, axis=1)
    # Convert each (a, b) pair to a single int64 key for fast bincount.
    # Vertex index can exceed 2^31 on huge meshes, so use object packing.
    packed = edges[:, 0].astype(np.int64) * (mesh.vertices.shape[0] + 1) + \
             edges[:, 1].astype(np.int64)
    _, counts = np.unique(packed, return_counts=True)
    return int(np.sum(counts > 2))


def _count_inverted_normals(mesh: trimesh.Trimesh) -> int:
    """Count faces whose normal disagrees with the trimesh-fixed orientation.

    Uses :func:`trimesh.repair.fix_inversion` which performs a ray-cast
    majority vote on a copy of the mesh, then compares face normals
    cosine-by-cosine. A negative cosine = the face was flipped during
    fix-up = the face was originally inverted.

    Returns ``-1`` if detection fails (e.g., empty mesh, ray engine
    missing).
    """
    if mesh.faces.shape[0] == 0:
        return 0
    try:
        mesh_fixed = mesh.copy()
        trimesh.repair.fix_inversion(mesh_fixed, multibody=True)
        cos = (mesh.face_normals * mesh_fixed.face_normals).sum(axis=1)
        return int(np.sum(cos < 0.0))
    except Exception:
        return -1


def diagnose_mesh(
    path: str | Path,
    area_eps: float = 1.0e-12,
    n_top_islands: int = 5,
) -> dict[str, int | float | bool | list]:
    """Run a full pre-repair diagnostic.

    Parameters
    ----------
    path : str or Path
        Mesh file path (FBX, GLB, OBJ, PLY, STL, …).
    area_eps : float, default 1e-12
        Faces with area below this are flagged as degenerate.
    n_top_islands : int, default 5
        How many largest islands to list (by face count).

    Returns
    -------
    dict
        See keys in :func:`diamesh.cli._cmd_diagnose` for the human-
        readable layout. ``inverted_normals`` is ``-1`` when detection
        fails.
    """
    m = _load_concat(path)
    n_faces = int(m.faces.shape[0])
    n_verts = int(m.vertices.shape[0])

    bbox = m.vertices.max(axis=0) - m.vertices.min(axis=0)
    diag = float(np.linalg.norm(bbox))

    watertight = bool(m.is_watertight)
    winding_consistent = bool(m.is_winding_consistent)

    # Boundary edges = edges_unique − face_adjacency_edges. The latter
    # is exactly the set of edges with two adjacent faces; everything
    # else (1-face = boundary, ≥3-face = non-manifold) is the complement.
    n_unique_edges = int(m.edges_unique.shape[0])
    n_two_face_edges = int(m.face_adjacency_edges.shape[0])
    n_boundary_edges = max(0, n_unique_edges - n_two_face_edges)

    n_non_manifold_edges = _count_non_manifold_edges(m)

    # Degenerate faces (zero / near-zero area)
    n_degenerate_faces = int(np.sum(m.area_faces < area_eps))

    # Connected components — split with only_watertight=False so we see
    # every island including open shells (CAD parts are rarely watertight).
    components = m.split(only_watertight=False)
    n_islands = len(components)
    island_face_counts = sorted(
        (int(c.faces.shape[0]) for c in components), reverse=True
    )
    largest = island_face_counts[:n_top_islands]

    n_inverted = _count_inverted_normals(m)

    return {
        "face_count": n_faces,
        "vert_count": n_verts,
        "bbox_diagonal": diag,
        "watertight": watertight,
        "winding_consistent": winding_consistent,
        "boundary_edges": n_boundary_edges,
        "non_manifold_edges": n_non_manifold_edges,
        "degenerate_faces": n_degenerate_faces,
        "island_count": n_islands,
        "largest_islands": largest,
        "inverted_normals": n_inverted,
        "inverted_normals_pct": (
            100.0 * n_inverted / max(n_faces, 1) if n_inverted >= 0 else float("nan")
        ),
    }
