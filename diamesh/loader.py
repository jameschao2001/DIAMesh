"""DIAMesh — FBX loading via vendored FBX2glTF converter.

Why the FBX → glTF detour?

trimesh on Windows cannot read FBX directly: the Python wrapper
``pyassimp`` requires a system-wide ``libassimp`` shared library that
is not packaged with pip on Windows, and there is no FBX wheel for
Python 3.14 on PyPI yet (ufbx / fbx-python / aspose-3d all fail to
install). To stay self-contained, DIAMesh ships the upstream
**FBX2glTF v0.9.7** Windows binary under
``vendor/fbx2gltf/fbx2gltf.exe`` (MIT, see vendor/FBX2GLTF_LICENSE.md)
and uses it to transcode ``.fbx`` to a temporary ``.glb`` before
loading the result via trimesh's native glTF reader.

Public API:

* :func:`load_fbx` — load FBX (or any format trimesh handles) into a
  flat list of :class:`trimesh.Trimesh`.
* :func:`summarize` — aggregate vertex / face counts.

Author: James Chao, Homi (AI Agent)
Version: 0.1.0
Date: 2026-05-01
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

import trimesh


_VENDOR_BIN_DIR = Path(__file__).resolve().parent.parent / "vendor" / "fbx2gltf"
_BIN_NAME = "fbx2gltf.exe" if platform.system() == "Windows" else "fbx2gltf"


def _vendored_fbx2gltf() -> Path | None:
    """Return the path to the vendored fbx2gltf binary, or ``None`` if absent."""
    candidate = _VENDOR_BIN_DIR / _BIN_NAME
    return candidate if candidate.exists() else None


def _fbx_to_glb(fbx_path: Path, glb_out: Path) -> None:
    """Invoke FBX2glTF to convert ``fbx_path`` into the binary glTF ``glb_out``.

    Raises
    ------
    RuntimeError
        If the vendored binary is missing or the conversion fails.
    """
    bin_path = _vendored_fbx2gltf()
    if bin_path is None:
        raise RuntimeError(
            f"FBX2glTF binary not found at {_VENDOR_BIN_DIR}. The DIAMesh "
            f"checkout is incomplete — run `git pull` to fetch the vendored "
            f"binary, or download it manually from "
            f"https://github.com/facebookincubator/FBX2glTF/releases/tag/v0.9.7"
        )

    cmd = [
        str(bin_path),
        "--binary",                # emit single .glb
        "--input", str(fbx_path),
        "--output", str(glb_out.with_suffix("")),  # tool appends .glb
        "--compute-normals", "missing",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"FBX2glTF conversion failed (exit={proc.returncode}).\n"
            f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
        )

    # FBX2glTF writes to <output>.glb; ensure it landed where we asked.
    actual = glb_out.with_suffix(".glb")
    if not actual.exists():
        raise RuntimeError(
            f"FBX2glTF reported success but produced no output at {actual}. "
            f"stderr:\n{proc.stderr}"
        )


def load_fbx(path: str | Path) -> list[trimesh.Trimesh]:
    """Load an FBX (or any trimesh-supported format) into a flat list of meshes.

    For ``.fbx`` inputs, transcodes via the vendored FBX2glTF binary into a
    temporary ``.glb`` first, then hands off to trimesh's glTF reader. For
    other extensions, calls trimesh.load directly.

    Parameters
    ----------
    path : str or Path
        Source mesh file. ``.fbx`` is the primary target; other formats
        (``.glb``, ``.obj``, ``.stl``, ``.ply``) are also accepted as a
        convenience for testing.

    Returns
    -------
    list of trimesh.Trimesh
        One :class:`trimesh.Trimesh` per geometry node.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    RuntimeError
        If FBX conversion or trimesh parsing fails.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Mesh file not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".fbx":
        with tempfile.TemporaryDirectory(prefix="diamesh_") as tmp:
            glb = Path(tmp) / (path.stem + ".glb")
            _fbx_to_glb(path, glb)
            loaded = trimesh.load(str(glb), force="scene")
            return _flatten(loaded)

    # Anything else: pass through to trimesh.
    loaded = trimesh.load(str(path), force="scene")
    return _flatten(loaded)


def _flatten(loaded) -> list[trimesh.Trimesh]:
    """Recursively unwrap a trimesh load result into pure ``Trimesh`` parts."""
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
