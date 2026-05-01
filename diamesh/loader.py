"""DIAMesh — FBX loading.

pyrender does not natively read FBX. We bridge through ``trimesh``, which
delegates the actual FBX parsing to ``pyassimp`` (Open Asset Import
Library). The result is a list of :class:`trimesh.Trimesh` instances —
one per FBX node — that can be wrapped as :class:`pyrender.Mesh` for
rendering.

Author: James Chao, Homi (AI Agent)
Version: 0.1.0
Date: 2026-05-01
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import trimesh


def load_fbx(path: str | Path) -> list[trimesh.Trimesh]:
    """Load an ``.fbx`` file and return its meshes.

    Parameters
    ----------
    path : str or Path
        Path to the ``.fbx`` file.

    Returns
    -------
    list of trimesh.Trimesh
        One :class:`trimesh.Trimesh` per geometry in the FBX scene. Empty
        list if the file contained no triangulated geometry.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    ValueError
        If the file extension is not ``.fbx``.
    RuntimeError
        If ``trimesh`` could not parse the file (typically a missing
        ``pyassimp`` backend or a malformed FBX).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"FBX file not found: {path}")
    if path.suffix.lower() != ".fbx":
        raise ValueError(f"Expected .fbx file, got: {path.suffix}")

    loaded = trimesh.load(str(path), force="scene")
    return _flatten(loaded)


def _flatten(loaded) -> list[trimesh.Trimesh]:
    """Recursively unwrap a trimesh load result into pure ``Trimesh`` parts.

    ``trimesh.load`` may return a single :class:`Trimesh`, a
    :class:`trimesh.Scene`, or a list. This helper normalises to a flat
    list of :class:`Trimesh` so downstream callers do not have to branch
    on type.
    """
    if isinstance(loaded, trimesh.Trimesh):
        return [loaded]
    if isinstance(loaded, trimesh.Scene):
        return [g for g in loaded.geometry.values() if isinstance(g, trimesh.Trimesh)]
    if isinstance(loaded, Iterable):
        out: list[trimesh.Trimesh] = []
        for item in loaded:
            out.extend(_flatten(item))
        return out
    raise RuntimeError(
        f"Unexpected load result type {type(loaded).__name__}; "
        f"expected Trimesh, Scene, or iterable thereof."
    )


def summarize(meshes: list[trimesh.Trimesh]) -> dict[str, int | float]:
    """Aggregate vertex / face counts across all loaded meshes."""
    total_vertices = sum(int(m.vertices.shape[0]) for m in meshes)
    total_faces = sum(int(m.faces.shape[0]) for m in meshes)
    return {
        "n_meshes": len(meshes),
        "total_vertices": total_vertices,
        "total_faces": total_faces,
        "watertight": int(sum(1 for m in meshes if m.is_watertight)),
    }
