"""DIAMesh — geometric deviation between two meshes.

For each pair (original, repaired) compute:

* **Hausdorff distance** — max bidirectional surface distance, the
  worst-case point-to-surface error.
* **Chamfer distance** — mean bidirectional surface distance, an
  averaged error metric robust to outliers.
* **Volume difference** — only meaningful when both meshes are
  watertight; ``NaN`` otherwise.
* **Mean surface normal deviation** — average angle between corresponding
  face normals; flags where smooth surfaces flipped or got noisy.

Distances are reported in absolute units (matching the mesh's coordinate
system, typically millimetres for CAD) AND as a percentage of the input
bounding-box diagonal so the numbers are scale-aware. A 0.05% Hausdorff
on a 2 m machine ≈ 1 mm — that level of fidelity is what TIER 2/3 LOD
should target.

Sampling-based — for each mesh sample N surface points uniformly, then
find each sample's nearest point on the other mesh via
:func:`trimesh.proximity.closest_point`. Bidirectional avoids the
"one-sided coverage" failure mode where a repaired mesh that just keeps
adding holes to original geometry would otherwise appear to "match".

Pure trimesh + numpy — no Blender, no GPU. Fast enough for industrial
CAD (75 k face mesh × 50 k samples ≈ 5–10 s on laptop CPU).

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
    """Load a mesh from any format DIAMesh handles and concat parts.

    FBX goes through the vendored FBX2glTF transcoder; everything else
    goes straight to trimesh. Multi-part scenes are concatenated into a
    single :class:`trimesh.Trimesh` so distance queries see the union.
    """
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


def diff_meshes(
    original_path: str | Path,
    repaired_path: str | Path,
    n_samples: int = 50000,
    seed: int | None = 42,
) -> dict[str, float | int]:
    """Compute geometric deviation metrics between two meshes.

    Parameters
    ----------
    original_path : str or Path
        The reference mesh (e.g., the original CAD FBX).
    repaired_path : str or Path
        The mesh to evaluate (e.g., a TIER LOD or a repaired mesh).
    n_samples : int, default 50000
        Number of points sampled on each mesh's surface. Higher → more
        accurate Hausdorff/Chamfer estimates, slower. 50 k is a good
        balance for industrial CAD.
    seed : int or None, default 42
        RNG seed for reproducible sampling. ``None`` for non-deterministic.

    Returns
    -------
    dict
        Keys ``hausdorff_o2r``, ``hausdorff_r2o``, ``hausdorff_max``,
        ``hausdorff_max_pct_of_diag``, ``chamfer``, ``chamfer_pct_of_diag``,
        ``volume_orig``, ``volume_repaired``, ``volume_diff_abs``,
        ``volume_diff_pct``, ``mean_normal_dev_deg``, ``n_samples``,
        ``bbox_diagonal``, ``orig_faces``, ``repaired_faces``.

    Notes
    -----
    Volume metrics return ``NaN`` if either mesh is non-watertight.
    Normal deviation uses ``|cos|`` rather than signed cosine so a
    consistently flipped face does not artificially register as 180°
    error — useful when the repair pipeline normalises winding.
    """
    m_orig = _load_concat(original_path)
    m_rep = _load_concat(repaired_path)

    bbox_min = m_orig.vertices.min(axis=0)
    bbox_max = m_orig.vertices.max(axis=0)
    diag = float(np.linalg.norm(bbox_max - bbox_min))

    seed_b = (seed + 1) if seed is not None else None
    pts_o, faces_o = trimesh.sample.sample_surface(m_orig, int(n_samples), seed=seed)
    pts_r, faces_r = trimesh.sample.sample_surface(m_rep, int(n_samples), seed=seed_b)

    # Original samples → closest on repaired
    _, dists_o2r, faces_r_at_o = trimesh.proximity.closest_point(m_rep, pts_o)
    # Repaired samples → closest on original
    _, dists_r2o, _ = trimesh.proximity.closest_point(m_orig, pts_r)

    hausdorff_o2r = float(dists_o2r.max())
    hausdorff_r2o = float(dists_r2o.max())
    hausdorff_max = max(hausdorff_o2r, hausdorff_r2o)
    chamfer = float(0.5 * (dists_o2r.mean() + dists_r2o.mean()))

    # Volume — only valid for watertight meshes
    if m_orig.is_watertight:
        vol_o = float(m_orig.volume)
    else:
        vol_o = float("nan")
    if m_rep.is_watertight:
        vol_r = float(m_rep.volume)
    else:
        vol_r = float("nan")

    if np.isfinite(vol_o) and np.isfinite(vol_r):
        vol_diff_abs = abs(vol_o - vol_r)
        vol_diff_pct = 100.0 * vol_diff_abs / max(abs(vol_o), 1e-12)
    else:
        vol_diff_abs = float("nan")
        vol_diff_pct = float("nan")

    # Normal deviation
    normals_o = m_orig.face_normals[faces_o]
    normals_r = m_rep.face_normals[faces_r_at_o]
    cos_abs = np.clip(np.abs((normals_o * normals_r).sum(axis=1)), 0.0, 1.0)
    angle_dev_deg = np.degrees(np.arccos(cos_abs))
    mean_normal_dev_deg = float(angle_dev_deg.mean())

    return {
        "hausdorff_o2r": hausdorff_o2r,
        "hausdorff_r2o": hausdorff_r2o,
        "hausdorff_max": hausdorff_max,
        "hausdorff_max_pct_of_diag": 100.0 * hausdorff_max / max(diag, 1e-12),
        "chamfer": chamfer,
        "chamfer_pct_of_diag": 100.0 * chamfer / max(diag, 1e-12),
        "volume_orig": vol_o,
        "volume_repaired": vol_r,
        "volume_diff_abs": vol_diff_abs,
        "volume_diff_pct": vol_diff_pct,
        "mean_normal_dev_deg": mean_normal_dev_deg,
        "n_samples": int(n_samples),
        "bbox_diagonal": diag,
        "orig_faces": int(m_orig.faces.shape[0]),
        "repaired_faces": int(m_rep.faces.shape[0]),
    }
