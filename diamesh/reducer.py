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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import trimesh

from diamesh.loader import load_fbx


_BLENDER_DECIMATE_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "blender_decimate.py"
)


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


_VENDOR_BLENDER_DIR = Path(__file__).resolve().parent.parent / "vendor" / "blender"


def _find_blender_exe() -> Path | None:
    """Locate ``blender.exe`` (Windows) or ``blender`` (POSIX).

    Resolution order:

    1. ``BLENDER_EXE`` environment variable (operator override).
    2. **Vendored portable** under ``DIAMesh/vendor/blender/`` —
       the recommended deployment per project convention; treats Blender
       as a self-contained third-party tool checked into ``vendor/`` like
       FBX2glTF and Assimp. Not committed to git (too large) but resolves
       transparently when the operator drops a portable build into the
       directory.
    3. ``shutil.which("blender")`` — covers cases where Blender is on PATH.
    4. Common system install paths.
    """
    env = os.environ.get("BLENDER_EXE")
    if env and Path(env).exists():
        return Path(env)

    # Vendored portable — preferred location for DIAMesh
    if _VENDOR_BLENDER_DIR.exists():
        candidates = []
        bin_name = "blender.exe" if sys.platform == "win32" else "blender"
        direct = _VENDOR_BLENDER_DIR / bin_name
        if direct.exists():
            return direct
        for sub in _VENDOR_BLENDER_DIR.iterdir():
            if sub.is_dir():
                exe = sub / bin_name
                if exe.exists():
                    candidates.append(exe)
        if candidates:
            return candidates[0]

    on_path = shutil.which("blender")
    if on_path:
        return Path(on_path)

    if sys.platform == "win32":
        roots = [
            Path("C:/Program Files/Blender Foundation"),
            Path("C:/Program Files (x86)/Blender Foundation"),
        ]
        for root in roots:
            if not root.exists():
                continue
            direct = root / "blender.exe"
            if direct.exists():
                return direct
            for sub in root.iterdir():
                if sub.is_dir():
                    exe = sub / "blender.exe"
                    if exe.exists():
                        return exe

    return None


def _reduce_blender(
    input_path: Path,
    output_path: Path,
    target_faces: int | None,
    ratio: float | None,
    min_island_faces: int = 0,
    cull_disjoint: float = 0.0,
    cull_anchor_count: int = 10,
    auto_fill_holes: bool = False,
    fill_holes_max_sides: int = 8,
    fill_holes_skip_design: bool = False,
    fill_holes_design_min_radius_frac: float = 0.005,
    fill_holes_design_circularity: float = 0.85,
    weld_tolerance_frac: float = 5.0e-5,
    weld_tolerance_abs: float | None = None,
    fix_non_manifold: bool = False,
    fill_holes_smooth: bool = False,
    fill_holes_smooth_iter: int = 2,
    fill_holes_smooth_factor: float = 0.5,
) -> dict[str, int | float]:
    """Decimate via Blender headless — preserves materials, textures, hierarchy.

    Spawns ``blender --background --python scripts/blender_decimate.py``
    with the user's input/output and a ratio target. Parses
    ``DIAMESH_*=…`` sentinel lines from stdout for the metrics dict.
    """
    blender_exe = _find_blender_exe()
    if blender_exe is None:
        raise RuntimeError(
            "Blender executable not found. Install Blender (any 4.x release) "
            "and either:\n"
            "  - set BLENDER_EXE=C:\\path\\to\\blender.exe, or\n"
            "  - add Blender's install dir to PATH, or\n"
            "  - place portable Blender under D:\\Blender\\ or "
            "C:\\Program Files\\Blender Foundation\\.\n"
            "Download: https://www.blender.org/download/"
        )

    cmd = [
        str(blender_exe),
        "--background",
        "--python", str(_BLENDER_DECIMATE_SCRIPT),
        "--",
        "--input", str(input_path),
        "--output", str(output_path),
    ]
    if target_faces is not None:
        cmd += ["--target-faces", str(int(target_faces))]
    elif ratio is not None:
        cmd += ["--ratio", str(float(ratio))]
    if min_island_faces and min_island_faces > 0:
        cmd += ["--min-island-faces", str(int(min_island_faces))]
    if cull_disjoint and cull_disjoint > 0:
        cmd += ["--cull-disjoint", str(float(cull_disjoint))]
        cmd += ["--cull-anchor-count", str(int(cull_anchor_count))]
    if auto_fill_holes:
        cmd += ["--auto-fill-holes"]
        cmd += ["--fill-holes-max-sides", str(int(fill_holes_max_sides))]
        if fill_holes_skip_design:
            cmd += ["--fill-holes-skip-design"]
            cmd += [
                "--fill-holes-design-min-radius-frac",
                str(float(fill_holes_design_min_radius_frac)),
            ]
            cmd += [
                "--fill-holes-design-circularity",
                str(float(fill_holes_design_circularity)),
            ]
    cmd += ["--weld-tolerance-frac", str(float(weld_tolerance_frac))]
    if weld_tolerance_abs is not None:
        cmd += ["--weld-tolerance-abs", str(float(weld_tolerance_abs))]
    if fix_non_manifold:
        cmd += ["--fix-non-manifold"]
    if fill_holes_smooth:
        cmd += ["--fill-holes-smooth"]
        cmd += ["--fill-holes-smooth-iter", str(int(fill_holes_smooth_iter))]
        cmd += ["--fill-holes-smooth-factor", str(float(fill_holes_smooth_factor))]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Blender decimate failed (exit={proc.returncode}).\n"
            f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout[-2000:]}"
        )

    metrics: dict[str, int | float] = {}
    for line in proc.stdout.splitlines():
        if not line.startswith("DIAMESH_"):
            continue
        key, _, val = line.partition("=")
        key = key.removeprefix("DIAMESH_").lower()
        try:
            metrics[key] = int(val)
        except ValueError:
            try:
                metrics[key] = float(val)
            except ValueError:
                metrics[key] = val

    if not output_path.exists():
        raise RuntimeError(
            f"Blender reported success but produced no output at {output_path}"
        )

    result = {
        "input_faces": int(metrics.get("input_faces", 0)),
        "output_faces": int(metrics.get("output_faces", 0)),
        "achieved_ratio": float(metrics.get("ratio", 0.0)),
        "input_vertices": int(metrics.get("input_vertices", 0)),
        "output_vertices": int(metrics.get("output_vertices", 0)),
        "backend": "blender",
        "output_path": str(output_path),
        "blender_exe": str(blender_exe),
    }
    # Surface every additional sentinel — repair_*, joined_*,
    # material_slots — that the Blender script chose to emit. White-
    # listing is too brittle: every time the script grows a new metric
    # the operator would lose visibility until reducer.py was updated.
    for k, v in metrics.items():
        if k in result:
            continue
        if k in ("input_faces", "output_faces", "input_vertices",
                 "output_vertices", "ratio", "decimate_ratio_requested"):
            continue  # already mapped above
        result[k] = v
    return result


def reduce_mesh(
    input_path: str | Path,
    output_path: str | Path,
    target_faces: int | None = None,
    ratio: float | None = None,
    backend: str = "trimesh",
    min_island_faces: int = 0,
    cull_disjoint: float = 0.0,
    cull_anchor_count: int = 10,
    auto_fill_holes: bool = False,
    fill_holes_max_sides: int = 8,
    fill_holes_skip_design: bool = False,
    fill_holes_design_min_radius_frac: float = 0.005,
    fill_holes_design_circularity: float = 0.85,
    weld_tolerance_frac: float = 5.0e-5,
    weld_tolerance_abs: float | None = None,
    fix_non_manifold: bool = False,
    fill_holes_smooth: bool = False,
    fill_holes_smooth_iter: int = 2,
    fill_holes_smooth_factor: float = 0.5,
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

    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if backend == "blender":
        # Blender path bypasses trimesh entirely; it owns FBX I/O end-to-end
        # so materials, textures, and hierarchy survive the round-trip.
        return _reduce_blender(
            input_path, output_path, target_faces, ratio,
            min_island_faces=min_island_faces,
            cull_disjoint=cull_disjoint,
            cull_anchor_count=cull_anchor_count,
            auto_fill_holes=auto_fill_holes,
            fill_holes_max_sides=fill_holes_max_sides,
            fill_holes_skip_design=fill_holes_skip_design,
            fill_holes_design_min_radius_frac=fill_holes_design_min_radius_frac,
            fill_holes_design_circularity=fill_holes_design_circularity,
            weld_tolerance_frac=weld_tolerance_frac,
            weld_tolerance_abs=weld_tolerance_abs,
            fix_non_manifold=fix_non_manifold,
            fill_holes_smooth=fill_holes_smooth,
            fill_holes_smooth_iter=fill_holes_smooth_iter,
            fill_holes_smooth_factor=fill_holes_smooth_factor,
        )

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
