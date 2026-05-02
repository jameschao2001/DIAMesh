"""DIAMesh — command-line entry point.

Subcommands:

* ``diamesh view <file.fbx>`` — open an interactive viewer.
* ``diamesh info <file.fbx>`` — quick mesh statistics.
* ``diamesh diagnose <file.fbx>`` — full pre-repair health report
  (watertight, non-manifold, degenerate, islands, inverted normals).
* ``diamesh reduce ...`` — auto quadric mesh reduction.
* ``diamesh diff <orig> <repaired>`` — geometric deviation metrics
  (Hausdorff / Chamfer / volume / normal deviation) between two meshes.

Author: James Chao, Homi (AI Agent)
Version: 0.1.0
Date: 2026-05-01
"""

from __future__ import annotations

import argparse
import sys

from diamesh.loader import load_fbx, summarize


def _cmd_view(args: argparse.Namespace) -> int:
    from diamesh.viewer import view_fbx  # defer heavy import

    view_fbx(args.file)
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    meshes = load_fbx(args.file)
    info = summarize(meshes)
    print(f"file: {args.file}")
    for k, v in info.items():
        print(f"  {k}: {v}")
    for i, m in enumerate(meshes):
        bbox = m.bounds.tolist() if m.bounds is not None else None
        print(
            f"  mesh[{i}]: V={m.vertices.shape[0]}, F={m.faces.shape[0]}, "
            f"watertight={m.is_watertight}, bounds={bbox}"
        )
    return 0


def _cmd_diagnose(args: argparse.Namespace) -> int:
    from diamesh.diagnose import diagnose_mesh

    metrics = diagnose_mesh(args.file)

    if args.json:
        import json
        print(json.dumps(metrics, indent=2, default=str))
        return 0

    print(f"diagnose: {args.file}")
    print(f"  face_count:           {metrics['face_count']}")
    print(f"  vert_count:           {metrics['vert_count']}")
    print(f"  bbox_diagonal:        {metrics['bbox_diagonal']:.4f}  (units of input)")
    print()
    print(f"  watertight:           {metrics['watertight']}")
    print(f"  winding_consistent:   {metrics['winding_consistent']}")
    print(f"  boundary_edges:       {metrics['boundary_edges']}")
    print(f"  non_manifold_edges:   {metrics['non_manifold_edges']}  (edges shared by 3+ faces)")
    print(f"  degenerate_faces:     {metrics['degenerate_faces']}  (zero / near-zero area)")
    print()
    print(f"  island_count:         {metrics['island_count']}")
    print(f"  largest_islands:      {metrics['largest_islands']}  (top 5 by face count)")
    print()
    inv = metrics["inverted_normals"]
    if inv >= 0:
        print(f"  inverted_normals:     {inv}  ({metrics['inverted_normals_pct']:.2f}% of faces)")
        if inv > 0:
            print("    => Suggest fixing in CAD export rather than relying on `reduce`")
            print("       to mask the issue. DIAMesh's recalc_face_normals only")
            print("       *unifies* winding, it does not flip an entire part outward.")
    else:
        print("  inverted_normals:     (detection failed - ray engine missing or empty mesh)")

    si = metrics["self_intersect_faces"]
    if si == -2:
        print("  self_intersect_faces: (skipped - pymeshlab not installed; pip install pymeshlab)")
    elif si == -1:
        print("  self_intersect_faces: (detection failed - pymeshlab error)")
    else:
        print(f"  self_intersect_faces: {si}  ({metrics['self_intersect_pct']:.2f}% of faces)")
        if si > 0:
            print("    => DIAMesh does NOT auto-fix self-intersection: proper repair")
            print("       (boolean-union remesh) destroys UVs/materials. Fix at CAD")
            print("       export, or accept it as a visual artifact for LOD use.")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    from diamesh.diff import diff_meshes

    metrics = diff_meshes(
        original_path=args.original,
        repaired_path=args.repaired,
        n_samples=args.n_samples,
        seed=args.seed,
    )
    diag = metrics["bbox_diagonal"]
    print(f"diff: {args.original}  ->  {args.repaired}")
    print(f"  bbox_diagonal:        {diag:.4f}  (units of input)")
    print(f"  orig_faces:           {metrics['orig_faces']}")
    print(f"  repaired_faces:       {metrics['repaired_faces']}")
    print(f"  n_samples:            {metrics['n_samples']}")
    print()
    print(f"  hausdorff_max:        {metrics['hausdorff_max']:.6f}  "
          f"({metrics['hausdorff_max_pct_of_diag']:.4f}% of diagonal)")
    print(f"    o->r:               {metrics['hausdorff_o2r']:.6f}")
    print(f"    r->o:               {metrics['hausdorff_r2o']:.6f}")
    print(f"  chamfer:              {metrics['chamfer']:.6f}  "
          f"({metrics['chamfer_pct_of_diag']:.4f}% of diagonal)")
    print(f"  mean_normal_dev_deg:  {metrics['mean_normal_dev_deg']:.4f} deg")
    print()
    if metrics['volume_orig'] == metrics['volume_orig']:  # not NaN
        print(f"  volume_orig:          {metrics['volume_orig']:.4f}")
        print(f"  volume_repaired:      {metrics['volume_repaired']:.4f}")
        print(f"  volume_diff_abs:      {metrics['volume_diff_abs']:.4f}")
        print(f"  volume_diff_pct:      {metrics['volume_diff_pct']:.4f}%")
    else:
        print("  volume_*: NaN (one or both meshes non-watertight)")
    return 0


def _cmd_reduce(args: argparse.Namespace) -> int:
    from diamesh.reducer import reduce_mesh

    if not args.output:
        # default: <input>_reduced.glb next to the source
        from pathlib import Path

        src = Path(args.file)
        args.output = str(src.with_name(f"{src.stem}_reduced.glb"))

    metrics = reduce_mesh(
        input_path=args.file,
        output_path=args.output,
        target_faces=args.target_faces,
        ratio=args.ratio,
        backend=args.backend,
        min_island_faces=args.min_island_faces,
        cull_disjoint=args.cull_disjoint,
        cull_anchor_count=args.cull_anchor_count,
        auto_fill_holes=args.auto_fill_holes,
        fill_holes_max_sides=args.fill_holes_max_sides,
        fill_holes_skip_design=args.fill_holes_skip_design,
        fill_holes_design_min_radius_frac=args.fill_holes_design_min_radius_frac,
        fill_holes_design_circularity=args.fill_holes_design_circularity,
        weld_tolerance_frac=args.weld_tolerance_frac,
        weld_tolerance_abs=args.weld_tolerance_abs,
        fix_non_manifold=args.fix_non_manifold,
        fill_holes_smooth=args.fill_holes_smooth,
        fill_holes_smooth_iter=args.fill_holes_smooth_iter,
        fill_holes_smooth_factor=args.fill_holes_smooth_factor,
    )
    print(f"reduced: {args.file}")
    for k, v in metrics.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="diamesh",
        description="DIAMesh — Python FBX viewer + mesh reduction toolkit.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_view = sub.add_parser("view", help="open an interactive FBX viewer")
    p_view.add_argument("file", help="path to .fbx file")
    p_view.set_defaults(func=_cmd_view)

    p_info = sub.add_parser("info", help="print mesh statistics for an FBX")
    p_info.add_argument("file", help="path to .fbx file")
    p_info.set_defaults(func=_cmd_info)

    p_diag = sub.add_parser(
        "diagnose",
        help="full pre-repair health report (watertight, non-manifold, "
             "degenerate, islands, inverted normals)",
    )
    p_diag.add_argument("file", help="path to .fbx (or other format)")
    p_diag.add_argument(
        "--json", action="store_true",
        help="emit metrics as JSON (machine-readable, for tooling)",
    )
    p_diag.set_defaults(func=_cmd_diagnose)

    p_diff = sub.add_parser(
        "diff",
        help="geometric deviation between two meshes (Hausdorff / Chamfer "
             "/ volume / normal deviation)",
    )
    p_diff.add_argument("original", help="reference mesh (e.g., original CAD FBX)")
    p_diff.add_argument("repaired", help="mesh to compare (e.g., LOD output)")
    p_diff.add_argument(
        "--n-samples", type=int, default=50000,
        help="surface samples per mesh for distance estimation (default 50000)",
    )
    p_diff.add_argument(
        "--seed", type=int, default=42,
        help="RNG seed for reproducible sampling (default 42)",
    )
    p_diff.set_defaults(func=_cmd_diff)

    p_reduce = sub.add_parser("reduce", help="auto quadric mesh reduction")
    p_reduce.add_argument("file", help="path to .fbx (or other trimesh-supported) file")
    p_reduce.add_argument(
        "--target-faces", type=int, help="absolute target face count"
    )
    p_reduce.add_argument(
        "--ratio", type=float, help="keep this fraction of original faces, in (0, 1]"
    )
    p_reduce.add_argument(
        "--output", "-o",
        help="output path; suffix decides format. Default <input>_reduced.glb",
    )
    p_reduce.add_argument(
        "--backend",
        choices=["trimesh", "pymeshlab", "blender"],
        default="trimesh",
        help="reduction backend. trimesh (default) = fast-simplification. "
             "pymeshlab = MeshLab with boundary/normal preservation. "
             "blender = headless Blender (preserves materials, textures, "
             "hierarchy — recommended for FBX-out workflows; requires "
             "vendor/blender/blender.exe or BLENDER_EXE env var)",
    )
    p_reduce.add_argument(
        "--min-island-faces",
        type=int,
        default=0,
        help="(blender backend, blunt) Drop disconnected mesh islands "
             "with fewer than this many faces. Crude — frame bars are "
             "small islands too. Prefer --cull-disjoint.",
    )
    p_reduce.add_argument(
        "--cull-disjoint",
        type=float,
        default=0.0,
        help="(blender backend, recommended) Distance-based island cull. "
             "Drop islands whose bounding box is further from the largest "
             "anchor islands than this fraction of the overall mesh "
             "diagonal. 0 disables (default). Try 0.03-0.08 for "
             "production-line LOD: parts that *touch* the main structure "
             "stay, parts floating mm off the assembly by CAD design "
             "(screw caps, sensor probes) get culled.",
    )
    p_reduce.add_argument(
        "--cull-anchor-count",
        type=int,
        default=10,
        help="With --cull-disjoint: how many of the largest islands to "
             "treat as 'anchor structure' (the rest are checked for "
             "distance to any anchor). Default 10.",
    )
    p_reduce.add_argument(
        "--auto-fill-holes",
        action="store_true",
        help="(blender backend) After collapse, run Blender's hole-fill "
             "operator to patch boundary loops opened by decimation. "
             "Avoids visible breakage on production LOD.",
    )
    p_reduce.add_argument(
        "--fill-holes-max-sides",
        type=int,
        default=8,
        help="Max boundary loop length (edges) for --auto-fill-holes. "
             "Default 8 covers most CAD seam holes; lower is conservative, "
             "higher caps even open surfaces.",
    )
    p_reduce.add_argument(
        "--fill-holes-skip-design",
        action="store_true",
        help="(blender backend) Classify each boundary loop and SKIP "
             "filling design holes (vents, fastener holes, line slots — "
             "circular & regular & dimensionally significant). Defect "
             "cracks (irregular, small) are still filled. Avoids "
             "accidentally capping ventilation grilles or screw holes.",
    )
    p_reduce.add_argument(
        "--fill-holes-design-min-radius-frac",
        type=float,
        default=0.005,
        help="With --fill-holes-skip-design: min loop radius (as fraction "
             "of mesh bbox diagonal) for design-hole classification. "
             "Smaller loops always treated as defects. Default 0.005.",
    )
    p_reduce.add_argument(
        "--fill-holes-design-circularity",
        type=float,
        default=0.85,
        help="With --fill-holes-skip-design: circularity threshold (0-1) "
             "for design-hole classification. 1.0 = perfect circle. "
             "Default 0.85.",
    )
    p_reduce.add_argument(
        "--weld-tolerance-frac",
        type=float,
        default=5.0e-5,
        help="(blender backend) Stage 0 weld tolerance as a fraction of "
             "the mesh bbox diagonal. Default 5e-5 ≈ 0.1 mm on a 2 m "
             "machine (matching legacy 0.1 mm). Auto-scales: 5 m cell = "
             "0.25 mm, 30 cm part = 0.015 mm.",
    )
    p_reduce.add_argument(
        "--weld-tolerance-abs",
        type=float,
        default=None,
        help="(blender backend) Override weld tolerance with an absolute "
             "value (mesh units, mm for CAD). When set, overrides "
             "--weld-tolerance-frac.",
    )
    p_reduce.add_argument(
        "--fix-non-manifold",
        action="store_true",
        help="(blender backend) Dissolve any edges shared by 3+ faces "
             "during Stage 0 mesh repair. Off by default — some CAD "
             "assemblies intentionally share 3-patch junctions. Turn on "
             "when post-reduce mesh has shading artifacts or boolean "
             "failures, or when `diamesh diagnose` reports non_manifold > 0.",
    )
    p_reduce.add_argument(
        "--fill-holes-smooth",
        action="store_true",
        help="(blender backend) After --auto-fill-holes, run Laplacian "
             "smoothing on the patched faces and their 1-ring neighbours "
             "so patches blend into surrounding curvature instead of "
             "reading as flat triangle bandages.",
    )
    p_reduce.add_argument(
        "--fill-holes-smooth-iter",
        type=int,
        default=2,
        help="Iterations for --fill-holes-smooth. Default 2 (subtle).",
    )
    p_reduce.add_argument(
        "--fill-holes-smooth-factor",
        type=float,
        default=0.5,
        help="Per-iteration relaxation strength in (0, 1]. Default 0.5.",
    )
    p_reduce.set_defaults(func=_cmd_reduce)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
