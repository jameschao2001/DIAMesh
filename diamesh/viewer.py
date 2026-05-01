"""DIAMesh — interactive FBX viewer wrapping pyrender.

The viewer:

1. uses :func:`diamesh.loader.load_fbx` to parse the FBX;
2. wraps each :class:`trimesh.Trimesh` as a :class:`pyrender.Mesh`;
3. assembles a :class:`pyrender.Scene` with a soft directional light;
4. opens an interactive :class:`pyrender.Viewer` window.

The viewer uses pyrender's built-in trackball: left-drag to rotate,
right-drag to pan, scroll to zoom.

Author: James Chao, Homi (AI Agent)
Version: 0.1.0
Date: 2026-05-01
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyrender
import trimesh

from diamesh.loader import load_fbx, summarize


def view_fbx(
    path: str | Path,
    window_title: str | None = None,
    background: tuple[float, float, float, float] = (0.95, 0.95, 0.95, 1.0),
) -> None:
    """Load an FBX file and open an interactive viewer.

    Parameters
    ----------
    path : str or Path
        Path to the ``.fbx`` file.
    window_title : str, optional
        Override the window title; defaults to the file name.
    background : tuple of 4 floats
        RGBA background colour in [0, 1]. Defaults to a neutral light grey.

    Notes
    -----
    The function blocks until the viewer window is closed.
    """
    meshes = load_fbx(path)
    if not meshes:
        raise RuntimeError(f"No triangulated meshes found in {path}")

    info = summarize(meshes)
    print(
        f"[viewer] loaded {info['n_meshes']} mesh(es) — "
        f"vertices={info['total_vertices']:,}, faces={info['total_faces']:,}, "
        f"watertight={info['watertight']}/{info['n_meshes']}"
    )

    scene = pyrender.Scene(bg_color=np.asarray(background, dtype=np.float32))

    for tm in meshes:
        scene.add(pyrender.Mesh.from_trimesh(tm, smooth=True))

    light = pyrender.DirectionalLight(
        color=np.ones(3, dtype=np.float32), intensity=3.0
    )
    light_pose = np.eye(4, dtype=np.float32)
    light_pose[:3, 3] = [3.0, 3.0, 3.0]
    scene.add(light, pose=light_pose)

    title = window_title or f"DIAMesh — {Path(path).name}"
    pyrender.Viewer(
        scene,
        viewport_size=(1280, 800),
        run_in_thread=False,
        window_title=title,
        use_raymond_lighting=True,
    )
