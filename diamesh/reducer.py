"""DIAMesh — automatic mesh reduction (Phase 2).

Quadric edge collapse decimation via :meth:`trimesh.Trimesh.simplify_quadric_decimation`,
which delegates to the ``fast-simplification`` C++ backend. Optional
``pymeshlab`` backend is exposed for cases where boundary / normal
preservation requires the heavier MeshLab pipeline.

The reducer accepts FBX inputs by reusing :func:`diamesh.loader.load_fbx`
(which transcodes FBX → GLB via the vendored FBX2glTF binary). Output
goes to GLB by default (preserves materials and is viewable directly
with ``diamesh view``); OBJ / PLY / STL also supported via trimesh's
exporters.

Author: James Chao, Homi (AI Agent)
Version: 0.1.0
Date: 2026-05-01
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import trimesh

from diamesh.loader import load_fbx


_VENDOR_ASSIMP_DIR = Path(__file__).resolve().parent.parent / "vendor" / "assimp"


def _ensure_assimp_dll_path() -> None:
    """Make the vendored ``assimp-vc143-mt.dll`` discoverable to ``pyassimp``.

    pyassimp searches the system PATH for the assimp shared library; we
    inject the vendored directory at the front so the bundled binary
    wins over any older system install. Idempotent.
    """
    if not _VENDOR_ASSIMP_DIR.exists():
        return
    dll_dir = str(_VENDOR_ASSIMP_DIR)
    if dll_dir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):  # Windows-only safe-no-op elsewhere
        try:
            os.add_dll_directory(dll_dir)
        except (FileNotFoundError, OSError):
            pass


def _combine(meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh:
    """Concatenate multiple Trimesh parts into a single mesh.

    Material / texture info is dropped on concatenation; for assets that
    rely on per-part materials, prefer reducing each part individually.
    """
    if len(meshes) == 1:
        return meshes[0]
    return trimesh.util.concatenate(meshes)


def reduce_mesh(
    input_path: str | Path,
    output_path: str | Path,
    target_faces: int | None = None,
    ratio: float | None = None,
    backend: str = "trimesh",
) -> dict[str, int | float]:
    """Reduce a mesh file's triangle count.

    Parameters
    ----------
    input_path : str or Path
        Source file. ``.fbx`` is transcoded automatically; other formats
        pass straight through trimesh.
    output_path : str or Path
        Destination file. Suffix decides the format — ``.glb`` (default,
        material-friendly), ``.obj``, ``.ply``, ``.stl``.
    target_faces : int, optional
        Absolute target face count (mutually exclusive with ``ratio``).
    ratio : float, optional
        Keep this fraction of the original faces, in (0, 1] (mutually
        exclusive with ``target_faces``).
    backend : {"trimesh", "pymeshlab"}
        ``"trimesh"`` uses fast-simplification (lightweight, fast).
        ``"pymeshlab"`` uses MeshLab's quadric edge collapse with
        boundary preservation — slower but better for textured /
        boundary-rich meshes.

    Returns
    -------
    dict
        ``{
            "input_faces": int,
            "output_faces": int,
            "achieved_ratio": float,
            "input_vertices": int,
            "output_vertices": int,
            "backend": str,
            "output_path": str,
        }``

    Raises
    ------
    ValueError
        If neither ``target_faces`` nor ``ratio`` is given, or both are.
    NotImplementedError
        If ``backend`` is unrecognised.
    """
    if (target_faces is None) == (ratio is None):
        raise ValueError(
            "Specify exactly one of --target-faces or --ratio (got "
            f"target_faces={target_faces}, ratio={ratio})."
        )
    if ratio is not None and not (0.0 < ratio <= 1.0):
        raise ValueError(f"ratio must be in (0, 1], got {ratio}.")

    meshes = load_fbx(input_path)
    if not meshes:
        raise RuntimeError(f"No meshes found in {input_path}")

    combined = _combine(meshes)
    n_faces_orig = int(combined.faces.shape[0])
    n_verts_orig = int(combined.vertices.shape[0])

    if target_faces is None:
        target_faces = max(1, int(n_faces_orig * float(ratio)))
    target_faces = min(target_faces, n_faces_orig)

    if backend == "trimesh":
        reduced = combined.simplify_quadric_decimation(face_count=target_faces)
    elif backend == "pymeshlab":
        reduced = _reduce_pymeshlab(combined, target_faces)
    else:
        raise NotImplementedError(f"unknown backend: {backend!r}")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() == ".fbx":
        _export_fbx_via_assimp(reduced, output_path)
    else:
        reduced.export(str(output_path))

    return {
        "input_faces": n_faces_orig,
        "output_faces": int(reduced.faces.shape[0]),
        "achieved_ratio": float(reduced.faces.shape[0]) / n_faces_orig,
        "input_vertices": n_verts_orig,
        "output_vertices": int(reduced.vertices.shape[0]),
        "backend": backend,
        "output_path": str(output_path),
    }


def _export_fbx_via_assimp(mesh: trimesh.Trimesh, output_path: Path) -> None:
    """Write ``mesh`` as an FBX file using the vendored assimp library.

    trimesh has no FBX exporter, so we round-trip through OBJ:
    trimesh writes a temporary ``.obj``, pyassimp reads it back into an
    assimp scene, then assimp exports as FBX. The vendored
    ``assimp-vc143-mt.dll`` (Assimp 6.0.5) is added to ``PATH`` first.

    Materials / textures are dropped on this round-trip; for textured
    output prefer ``.glb``.
    """
    _ensure_assimp_dll_path()
    import pyassimp  # heavy import — defer until we know we need it

    with tempfile.TemporaryDirectory(prefix="diamesh_fbxout_") as tmp:
        tmp_obj = Path(tmp) / "intermediate.obj"
        mesh.export(str(tmp_obj))
        with pyassimp.load(str(tmp_obj)) as scene:
            pyassimp.export(scene, str(output_path), "fbx")
    if not output_path.exists():
        raise RuntimeError(
            f"FBX export reported success but produced no file at {output_path}"
        )


def _reduce_pymeshlab(mesh: trimesh.Trimesh, target_faces: int) -> trimesh.Trimesh:
    """Quadric edge collapse via pymeshlab; preserves boundary + normals."""
    import pymeshlab  # heavy import — defer

    ms = pymeshlab.MeshSet()
    ml_mesh = pymeshlab.Mesh(
        vertex_matrix=mesh.vertices.astype("float64"),
        face_matrix=mesh.faces.astype("int32"),
    )
    ms.add_mesh(ml_mesh, "input")
    ms.apply_filter(
        "meshing_decimation_quadric_edge_collapse",
        targetfacenum=int(target_faces),
        preserveboundary=True,
        preservenormal=True,
        preservetopology=False,
        optimalplacement=True,
        autoclean=True,
    )
    out = ms.current_mesh()
    return trimesh.Trimesh(
        vertices=out.vertex_matrix(),
        faces=out.face_matrix(),
        process=False,
    )
