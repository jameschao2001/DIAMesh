"""DIAMesh — Blender headless decimation script.

Runs inside Blender's Python runtime (``blender --background --python``)
and is driven by ``diamesh.reducer._reduce_blender``::

    blender --background --python scripts/blender_decimate.py -- \\
        --input model.fbx --output model_lod.fbx \\
        [--target-faces N | --ratio R]

Pipeline (single mesh, multi-material approach for industrial CAD LOD):

1. Import FBX (preserves materials, textures, transforms).
2. **Join all mesh objects into a single mesh.** This is the key step
   that lets cross-part vertex pairs share an index after the global
   weld in stage 0; without it, parts that *visually* touch but live
   in separate ``bpy.data.objects`` collapse independently and split
   apart. Materials survive: each face keeps its ``material_index``
   pointing at the merged material slot table.
3. Stage 0 — global bmesh repair on the joined mesh:
     A. recalc_face_normals (consistent winding)
     0. remove_doubles weld (heals CAD seam coincident verts AND
        cross-part contact verts at one shot)
     B. dissolve_degenerate (zero-area sliver triangles)
     C. delete loose verts/edges (kills floating-fragment sources)
     D. mark sharp edges by dihedral (so delimit SHARP fires)
        AND mark every boundary edge (1-face edge) sharp so COLLAPSE
        does not destroy boundaries — eliminating most decimation-
        induced holes at the source rather than patching them after.
     E. triangulate (uniform topology for COLLAPSE)
4. Stage 1 — DISSOLVE modifier (planar merge, 5° threshold).
5. Stage 2 — COLLAPSE modifier (target ratio, delimit MATERIAL/SHARP/SEAM/UV).
6. Export FBX with embedded textures, ``COPY`` path mode.
7. Emit ``DIAMESH_*=…`` metric sentinel lines on stdout for the parent
   Python process to scrape.

Author: James Chao, Homi (AI Agent)
Version: 0.2.0
Date: 2026-05-02
"""

import argparse
import math
import sys

import bpy     # type: ignore[import-not-found]
import bmesh   # type: ignore[import-not-found]


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--target-faces", type=int)
    p.add_argument("--ratio", type=float)
    p.add_argument(
        "--min-island-faces",
        type=int,
        default=0,
        help="Drop disconnected mesh islands with fewer than this many "
             "faces. Crude — frame bars are also small islands and get "
             "swept along. Prefer --cull-disjoint for CAD assemblies.",
    )
    p.add_argument(
        "--cull-disjoint",
        type=float,
        default=0.0,
        help="Distance-based island cull. Drop islands whose bounding "
             "box is further from the largest anchor islands than this "
             "fraction of the overall mesh diagonal. 0 disables (default). "
             "Try 0.03-0.08 for production-line LOD: small parts that "
             "*touch* the main structure stay (frame bars, fasteners on "
             "panels), but parts floating millimeters off the assembly "
             "by CAD design (screw caps, sensor probes) get culled.",
    )
    p.add_argument(
        "--cull-anchor-count",
        type=int,
        default=10,
        help="With --cull-disjoint: number of largest islands (by face "
             "count) to use as anchors. An island is kept if it is close "
             "to ANY anchor. Default 10.",
    )
    p.add_argument(
        "--auto-fill-holes",
        action="store_true",
        help="After Stage 2 collapse, run Blender's mesh.fill_holes "
             "operator to patch boundary loops up to --fill-holes-max-sides "
             "edges. Useful for production LOD where small holes opened "
             "during decimation read as visual breakage.",
    )
    p.add_argument(
        "--fill-holes-max-sides",
        type=int,
        default=8,
        help="Maximum boundary loop length (in edges) that --auto-fill-holes "
             "will attempt to patch. 4 is conservative (only quads); 8 is "
             "the recommended default (covers most CAD seam holes); 32+ "
             "fills aggressively at the risk of capping intentionally open "
             "surfaces.",
    )
    p.add_argument(
        "--fill-holes-skip-design",
        action="store_true",
        help="Before fill_holes, classify each boundary loop as a 'design "
             "hole' (circular, regular, large enough — vents, fastener "
             "holes, line slots) vs a 'defect crack' (irregular, small). "
             "Skip filling design holes so vents/screw holes survive.",
    )
    p.add_argument(
        "--fill-holes-design-min-radius-frac",
        type=float,
        default=0.005,
        help="Min loop radius (as fraction of mesh bbox diagonal) for "
             "design-hole classification. Smaller loops always treated as "
             "defects. Default 0.005 (=0.5%% of diagonal).",
    )
    p.add_argument(
        "--fill-holes-design-circularity",
        type=float,
        default=0.85,
        help="Circularity threshold (0-1) for design-hole classification. "
             "1.0 = perfect circle. Default 0.85.",
    )
    p.add_argument(
        "--weld-tolerance-frac",
        type=float,
        default=5.0e-5,
        help="Stage 0 weld tolerance as a fraction of the mesh bbox "
             "diagonal. Default 5e-5 ≈ 0.1 mm on a 2 m machine, "
             "matching the legacy 0.1 mm constant. Auto-scales: a 5 m "
             "robotic cell uses 0.25 mm, a 30 cm tabletop part uses "
             "0.015 mm. Override with --weld-tolerance-abs.",
    )
    p.add_argument(
        "--weld-tolerance-abs",
        type=float,
        default=None,
        help="Absolute weld tolerance in mesh units (mm for CAD). "
             "When set, overrides --weld-tolerance-frac.",
    )
    p.add_argument(
        "--fix-non-manifold",
        action="store_true",
        help="In Stage 0, dissolve any edges shared by 3+ faces "
             "(non-manifold). Off by default since some CAD assemblies "
             "intentionally share 3-patch junctions; turn on when the "
             "post-reduce mesh has shading artifacts or boolean failures.",
    )
    p.add_argument(
        "--fill-holes-smooth",
        action="store_true",
        help="After --auto-fill-holes, run Laplacian relaxation on the "
             "newly-added faces and their 1-ring vertex neighbours, so "
             "patched regions blend with surrounding curvature instead "
             "of reading as a flat triangle bandage.",
    )
    p.add_argument(
        "--fill-holes-smooth-iter",
        type=int,
        default=2,
        help="Iterations for --fill-holes-smooth. Default 2 (subtle); "
             "increase for stronger blending, but excessive smoothing "
             "shrinks fine features near the patch.",
    )
    p.add_argument(
        "--fill-holes-smooth-factor",
        type=float,
        default=0.5,
        help="Per-iteration relaxation strength in (0, 1]. 0 = no move, "
             "1 = move fully to centroid of neighbours. Default 0.5.",
    )
    p.add_argument(
        "--post-collapse-weld",
        action="store_true",
        help="After Stage 2 COLLAPSE, run a second weld pass at "
             "(pre-weld tolerance × multiplier) to fuse vertices that "
             "COLLAPSE left close-but-disconnected — the root cause of "
             "many post-decimation cracks. Cures more thoroughly than "
             "boundary preservation alone but preserves input detail "
             "(unlike just enlarging Stage 0 weld).",
    )
    p.add_argument(
        "--post-collapse-weld-multiplier",
        type=float,
        default=5.0,
        help="Multiplier on the Stage 0 weld tolerance for the post-"
             "collapse pass. Default 5.0 — on a 2 m machine with "
             "default --weld-tolerance-frac, post-weld ≈ 0.5 mm. "
             "Higher fuses more cracks but risks merging fine details "
             "that survived Stage 2 collapse.",
    )
    p.add_argument(
        "--bridge-loops",
        action="store_true",
        help="After post-weld and BEFORE auto-fill-holes, find pairs of "
             "boundary loops within --bridge-loops-max-distance-frac of "
             "each other and bridge them with a face strip "
             "(bmesh.ops.bridge_loops). Different concept from fill_holes: "
             "bridges link two opposing open loops (panel seams), while "
             "fill_holes triangulates a single open loop with a planar "
             "fan. Successfully bridged pairs disappear from the boundary "
             "set; remaining loops still go through fill_holes.",
    )
    p.add_argument(
        "--bridge-loops-max-distance-frac",
        type=float,
        default=0.005,
        help="Max centroid distance between two loops to consider them a "
             "bridge candidate, as a fraction of mesh bbox diagonal. "
             "Default 0.005 (0.5%% of diagonal). Loops are also filtered "
             "by edge-count similarity (within 2x).",
    )
    p.add_argument(
        "--aggressive-collapse",
        action="store_true",
        help="Trade visual integrity for face-budget compliance. "
             "Disables boundary preservation (Stage 0.D no longer marks "
             "boundary edges sharp) AND relaxes COLLAPSE delimit from "
             "{MATERIAL,SHARP,SEAM,UV} to {MATERIAL} only. Result: the "
             "requested --ratio is reached much more closely, at the "
             "cost of more cracked boundaries and possibly distorted UVs. "
             "Use when --target-faces is hard contract and visual "
             "completeness can be re-patched downstream.",
    )
    p.add_argument(
        "--post-collapse-cull",
        action="store_true",
        help="After all repair passes (post-weld, bridge_loops, "
             "fill_holes, smooth), walk connected components on the "
             "post-everything mesh and remove any island whose face "
             "count falls below --post-collapse-cull-min-faces. Stage "
             "0.5/0.6 only catches PRE-collapse islands; orphans born "
             "from material delimit, sharp delimit, or collapse-induced "
             "topology splits survive into output. This stage cures "
             "those — independent of how many decimation rounds ran.",
    )
    p.add_argument(
        "--post-collapse-cull-min-faces",
        type=int,
        default=10,
        help="With --post-collapse-cull: minimum face count for an "
             "island to be kept. Islands below this threshold are "
             "removed. Default 10 — strict enough to clear shrapnel "
             "(1-2 face fragments are nearly always orphans) but lenient "
             "enough to preserve small legitimate parts (sensors, "
             "fasteners, HMI corners). Increase for cleaner output, "
             "decrease if losing too many small parts.",
    )
    return p.parse_args(argv)


def _group_boundary_edges_into_loops(bm):
    """Group boundary edges (edges with exactly one adjacent face) into loops.

    Uses union-find by shared vertex — every two boundary edges that
    share a vertex end up in the same group. For simple closed cycles
    (the dominant case in CAD seams) each group is one closed loop.

    Returns a list of lists of ``BMEdge``.
    """
    boundary = [e for e in bm.edges if e.is_boundary]
    if not boundary:
        return []

    parent = {e.index: e.index for e in boundary}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    v_to_edges: dict[int, list] = {}
    for e in boundary:
        for v in e.verts:
            v_to_edges.setdefault(v.index, []).append(e)

    for shared in v_to_edges.values():
        if len(shared) >= 2:
            base = shared[0].index
            for other in shared[1:]:
                union(base, other.index)

    groups: dict[int, list] = {}
    for e in boundary:
        groups.setdefault(find(e.index), []).append(e)
    return list(groups.values())


def _classify_boundary_loop(
    loop_edges,
    mesh_diagonal: float,
    min_radius_frac: float,
    circularity_threshold: float,
) -> str:
    """Decide if a boundary loop is a design hole (skip fill) or a defect.

    Design hole criteria (must satisfy both):
      * Mean radius from loop centroid ≥ ``min_radius_frac × mesh_diagonal``.
      * Circularity = ``1 - std_radius / mean_radius`` ≥ ``circularity_threshold``.

    Implementation: PCA on loop vertices to find best-fit plane, project
    onto plane, measure radii from centroid. Pure numpy — no SciPy.

    Returns ``"design"`` or ``"defect"``.
    """
    seen = set()
    pts = []
    for e in loop_edges:
        for v in e.verts:
            if v.index not in seen:
                seen.add(v.index)
                pts.append((v.co.x, v.co.y, v.co.z))
    if len(pts) < 4:
        return "defect"  # too small to be a design feature

    import numpy as np

    P = np.asarray(pts, dtype=np.float64)
    centroid = P.mean(axis=0)
    Q = P - centroid

    # PCA: smallest eigenvector ≈ plane normal; largest two ≈ in-plane basis
    cov = Q.T @ Q
    eigvals, eigvecs = np.linalg.eigh(cov)
    basis_u = eigvecs[:, 2]
    basis_v = eigvecs[:, 1]

    P2 = np.column_stack([Q @ basis_u, Q @ basis_v])
    radii = np.linalg.norm(P2, axis=1)
    mean_r = float(radii.mean())

    if mean_r < min_radius_frac * mesh_diagonal:
        return "defect"

    std_r = float(radii.std())
    circularity = 1.0 - std_r / max(mean_r, 1e-9)
    return "design" if circularity >= circularity_threshold else "defect"


def _mesh_objects():
    return [obj for obj in bpy.data.objects if obj.type == "MESH"]


def _total_faces(meshes) -> int:
    return sum(len(o.data.polygons) for o in meshes)


def _total_verts(meshes) -> int:
    return sum(len(o.data.vertices) for o in meshes)


def _join_all(meshes):
    """Join every mesh object into the first one; return the joined object.

    Material slots accumulate (Blender's join op handles slot merging
    and re-points each face's material_index), so per-face material
    assignment survives. Object-level hierarchy is lost, which is
    acceptable for LOD-style outputs.
    """
    if len(meshes) <= 1:
        return meshes[0]
    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def _bbox_distance(a_min, a_max, b_min, b_max) -> float:
    """Closest distance between two axis-aligned bounding boxes (R^3).

    Returns 0 if they overlap or touch.
    """
    dx = max(0.0, max(a_min[0], b_min[0]) - min(a_max[0], b_max[0]))
    dy = max(0.0, max(a_min[1], b_min[1]) - min(a_max[1], b_max[1]))
    dz = max(0.0, max(a_min[2], b_min[2]) - min(a_max[2], b_max[2]))
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _cull_disjoint_islands(obj, distance_threshold: float, anchor_count: int):
    """Drop islands far from the largest anchor islands.

    Returns ``(islands_removed, faces_removed)``. A no-op when
    ``distance_threshold <= 0``.

    Strategy:

    1. Find every connected face island (BFS over edge-shared faces).
    2. Compute each island's axis-aligned bounding box and the global
       mesh's bbox diagonal.
    3. Pick the top ``anchor_count`` islands by face count as
       "anchors" — these are the structural skeleton (panels, frame
       runs, large enclosures).
    4. Every other island gets its closest-bbox-distance to ANY anchor.
       If that distance, normalised by the mesh diagonal, exceeds
       ``distance_threshold``, the island is genuinely floating off the
       assembly and gets deleted. If it touches or sits within the
       threshold of an anchor, it stays — even if it's tiny — because
       it's part of the visual skeleton.

    This is more selective than ``--min-island-faces`` which kills
    everything below a face-count floor regardless of placement.
    """
    if distance_threshold <= 0.0:
        return 0, 0

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.verts.ensure_lookup_table()

    # 1. Connected components
    visited = set()
    islands = []  # list of list[BMFace]
    for seed in bm.faces:
        if seed.index in visited:
            continue
        stack = [seed]
        island = []
        while stack:
            f = stack.pop()
            if f.index in visited:
                continue
            visited.add(f.index)
            island.append(f)
            for edge in f.edges:
                for adj in edge.link_faces:
                    if adj.index not in visited:
                        stack.append(adj)
        islands.append(island)

    if len(islands) <= 1:
        bm.free()
        return 0, 0

    # 2. Per-island bbox
    INF = float("inf")
    bboxes = []  # (min(x,y,z), max(x,y,z))
    for island in islands:
        bmin = [INF, INF, INF]
        bmax = [-INF, -INF, -INF]
        for face in island:
            for v in face.verts:
                co = v.co
                for i in range(3):
                    if co[i] < bmin[i]:
                        bmin[i] = co[i]
                    if co[i] > bmax[i]:
                        bmax[i] = co[i]
        bboxes.append((tuple(bmin), tuple(bmax)))

    # Global mesh diagonal
    global_min = [INF, INF, INF]
    global_max = [-INF, -INF, -INF]
    for bmin, bmax in bboxes:
        for i in range(3):
            if bmin[i] < global_min[i]:
                global_min[i] = bmin[i]
            if bmax[i] > global_max[i]:
                global_max[i] = bmax[i]
    diag = math.sqrt(sum((global_max[i] - global_min[i]) ** 2 for i in range(3)))
    abs_threshold = distance_threshold * diag if diag > 0 else 0.0

    # 3. Pick anchor islands by face count
    sorted_indices = sorted(range(len(islands)), key=lambda i: -len(islands[i]))
    anchor_indices = set(sorted_indices[:anchor_count])

    # 4. For each non-anchor island, check distance to nearest anchor
    to_delete = []
    removed_islands = 0
    for idx, island in enumerate(islands):
        if idx in anchor_indices:
            continue
        my_min, my_max = bboxes[idx]
        # Closest distance to any anchor
        min_dist = INF
        for a_idx in anchor_indices:
            a_min, a_max = bboxes[a_idx]
            d = _bbox_distance(my_min, my_max, a_min, a_max)
            if d < min_dist:
                min_dist = d
                if min_dist <= abs_threshold:
                    break  # close enough; keep
        if min_dist > abs_threshold:
            to_delete.extend(island)
            removed_islands += 1

    faces_removed = len(to_delete)
    if to_delete:
        bmesh.ops.delete(bm, geom=to_delete, context="FACES")

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()

    return removed_islands, faces_removed


def _drop_small_islands(obj, min_faces: int):
    """Delete disconnected mesh islands smaller than ``min_faces`` faces.

    Returns ``(islands_removed, faces_removed)``. A no-op when
    ``min_faces <= 0``.

    Use case: industrial CAD-to-FBX commonly has mini sub-components
    (screw caps, sensor probes, brackets) modelled as standalone
    geometry that floats a few mm off the parent frame *by design*.
    They visually merge into the assembly only because the surrounding
    dense triangulation hides the gap. After decimation those
    components remain disjoint and become visible "floating chunks".
    For LOD-style production-line viewers the right answer is to
    cull them — they're invisible at line-level zoom.
    """
    if min_faces <= 0:
        return 0, 0

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    visited = set()
    islands = []
    for seed in bm.faces:
        if seed.index in visited:
            continue
        stack = [seed]
        island_faces = []
        while stack:
            f = stack.pop()
            if f.index in visited:
                continue
            visited.add(f.index)
            island_faces.append(f)
            for edge in f.edges:
                for adj in edge.link_faces:
                    if adj.index not in visited:
                        stack.append(adj)
        islands.append(island_faces)

    to_delete = []
    removed_islands = 0
    for island in islands:
        if len(island) < min_faces:
            to_delete.extend(island)
            removed_islands += 1

    faces_removed = len(to_delete)
    if to_delete:
        bmesh.ops.delete(bm, geom=to_delete, context="FACES")

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()

    return removed_islands, faces_removed


def _repair(
    obj,
    weld_dist: float,
    sharp_angle_rad: float,
    degen_dist: float,
    fix_non_manifold: bool = False,
    skip_boundary_preservation: bool = False,
):
    """Run the full Stage-0 mesh repair sweep on a single object.

    Returns a dict with the count of each repair operation. Operations
    in order: recalc normals, weld, dissolve degenerate, drop loose,
    triangulate, mark sharp + boundary, optional non-manifold fix.
    """
    n_verts_before = len(obj.data.vertices)
    n_edges_before = len(obj.data.edges)

    bm = bmesh.new()
    bm.from_mesh(obj.data)

    # A. Recalc face normals (consistent winding)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    # 0. Weld coincident verts (intra- AND cross-part)
    weld_result = bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_dist)
    welded = len(weld_result.get("targetmap", {})) if isinstance(weld_result, dict) else 0

    # B. Dissolve degenerate edges/faces
    bmesh.ops.dissolve_degenerate(bm, dist=degen_dist, edges=bm.edges)

    # C. Delete loose
    loose_v = [v for v in bm.verts if not v.link_faces]
    if loose_v:
        bmesh.ops.delete(bm, geom=loose_v, context="VERTS")
    loose_e = [e for e in bm.edges if not e.link_faces]
    if loose_e:
        bmesh.ops.delete(bm, geom=loose_e, context="EDGES")

    # E. Triangulate
    bmesh.ops.triangulate(
        bm, faces=bm.faces[:], quad_method="BEAUTY", ngon_method="BEAUTY"
    )

    # D. Mark sharp by dihedral angle AND on every boundary edge.
    # Boundary edges have only 1 adjacent face — without explicit
    # protection, COLLAPSE happily kills them, opening new holes that
    # we then have to patch with --auto-fill-holes. Marking them sharp
    # lets `delimit={SHARP}` in the COLLAPSE modifier preserve them.
    # When aggressive-collapse is requested, skip boundary marking so
    # COLLAPSE can hit the requested ratio at the cost of cracks.
    sharp_marked = 0
    boundary_marked = 0
    for edge in bm.edges:
        n_faces = len(edge.link_faces)
        if n_faces == 1:
            if not skip_boundary_preservation:
                edge.smooth = False
                boundary_marked += 1
        elif n_faces == 2:
            try:
                if edge.calc_face_angle() > sharp_angle_rad:
                    edge.smooth = False
                    sharp_marked += 1
            except ValueError:
                pass

    # F. Optional non-manifold edge fix.
    # An edge shared by ≥3 faces is non-manifold — common in CAD when
    # different patches meet at a shared boundary or after aggressive
    # collapse folds two surfaces together. We only act on user opt-in
    # because dissolving these edges can also collapse legitimate
    # patch junctions; for diagnose-only callers the default off
    # preserves backward compatibility.
    non_manifold_initial = sum(
        1 for e in bm.edges if len(e.link_faces) > 2
    )
    non_manifold_fixed = 0
    if fix_non_manifold and non_manifold_initial > 0:
        nm_edges = [e for e in bm.edges if len(e.link_faces) > 2]
        try:
            bmesh.ops.dissolve_edges(bm, edges=nm_edges, use_verts=False)
            non_manifold_fixed = non_manifold_initial - sum(
                1 for e in bm.edges if len(e.link_faces) > 2
            )
        except (RuntimeError, ValueError) as e:
            print(f"WARN: dissolve_edges (non-manifold) failed: {e}", file=sys.stderr)

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()

    n_verts_after = len(obj.data.vertices)
    n_edges_after = len(obj.data.edges)

    return {
        "welded_verts": max(0, n_verts_before - n_verts_after - len(loose_v)),
        "loose_verts": len(loose_v),
        "loose_edges": len(loose_e),
        "degen_edges": max(0, n_edges_before - n_edges_after - len(loose_e)),
        "sharp_marked": sharp_marked,
        "boundary_marked": boundary_marked,
        "non_manifold_initial": non_manifold_initial,
        "non_manifold_fixed": non_manifold_fixed,
    }


def main() -> int:
    args = parse_args()

    # Constants
    DEGEN_DIST = 1.0e-5
    SHARP_ANGLE_DEG = 30.0
    PLANAR_ANGLE_DEG = 5.0
    sharp_angle_rad = math.radians(SHARP_ANGLE_DEG)

    # Empty starter scene
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # Import
    bpy.ops.import_scene.fbx(filepath=args.input)
    meshes_in = _mesh_objects()
    if not meshes_in:
        print("ERROR: no mesh objects found in FBX", file=sys.stderr)
        return 2
    in_faces = _total_faces(meshes_in)
    in_verts = _total_verts(meshes_in)
    n_objects_in = len(meshes_in)
    if in_faces == 0:
        print("ERROR: input FBX has zero faces", file=sys.stderr)
        return 2

    # Decide ratio
    if args.target_faces is not None:
        decimate_ratio = max(0.001, min(1.0, args.target_faces / in_faces))
    elif args.ratio is not None:
        decimate_ratio = max(0.001, min(1.0, args.ratio))
    else:
        print("ERROR: specify --target-faces or --ratio", file=sys.stderr)
        return 2

    # Join all into a single mesh — this is the key change vs v0.1
    joined = _join_all(meshes_in)
    print(f"DIAMESH_JOINED_OBJECTS={n_objects_in}")
    print(f"DIAMESH_MATERIAL_SLOTS={len(joined.material_slots)}")

    # Adaptive weld tolerance — scale with mesh bbox so a 5 m robotic
    # cell uses ~0.25 mm and a 30 cm tabletop part uses ~0.015 mm,
    # rather than the legacy 0.1 mm being too tight for the former and
    # too loose for the latter.
    if args.weld_tolerance_abs is not None:
        weld_dist = float(args.weld_tolerance_abs)
        weld_source = "abs"
    else:
        xs = [v.co.x for v in joined.data.vertices]
        ys = [v.co.y for v in joined.data.vertices]
        zs = [v.co.z for v in joined.data.vertices]
        if xs:
            dx = max(xs) - min(xs)
            dy = max(ys) - min(ys)
            dz = max(zs) - min(zs)
            mesh_diag = math.sqrt(dx * dx + dy * dy + dz * dz)
        else:
            mesh_diag = 0.0
        weld_dist = max(1.0e-7, mesh_diag * float(args.weld_tolerance_frac))
        weld_source = "frac"
    print(f"DIAMESH_WELD_DIST={weld_dist:.6e}")
    print(f"DIAMESH_WELD_SOURCE={weld_source}")

    # Stage 0 — full repair sweep on the joined mesh
    repair = _repair(
        joined,
        weld_dist,
        sharp_angle_rad,
        DEGEN_DIST,
        fix_non_manifold=args.fix_non_manifold,
        skip_boundary_preservation=args.aggressive_collapse,
    )
    print(f"DIAMESH_REPAIR_WELDED_VERTS={repair['welded_verts']}")
    print(f"DIAMESH_REPAIR_LOOSE_VERTS={repair['loose_verts']}")
    print(f"DIAMESH_REPAIR_LOOSE_EDGES={repair['loose_edges']}")
    print(f"DIAMESH_REPAIR_DEGEN_EDGES={repair['degen_edges']}")
    print(f"DIAMESH_REPAIR_SHARP_MARKED={repair['sharp_marked']}")
    print(f"DIAMESH_REPAIR_BOUNDARY_MARKED={repair['boundary_marked']}")
    print(f"DIAMESH_REPAIR_NON_MANIFOLD_INITIAL={repair['non_manifold_initial']}")
    print(f"DIAMESH_REPAIR_NON_MANIFOLD_FIXED={repair['non_manifold_fixed']}")
    print(f"DIAMESH_AGGRESSIVE_COLLAPSE={int(args.aggressive_collapse)}")

    # Stage 0.5 — distance-based island cull (LOD-friendly, preferred)
    cull_islands, cull_faces = _cull_disjoint_islands(
        joined, args.cull_disjoint, args.cull_anchor_count
    )
    print(f"DIAMESH_CULLED_ISLANDS={cull_islands}")
    print(f"DIAMESH_CULLED_ISLAND_FACES={cull_faces}")

    # Stage 0.6 — face-count island cull (legacy/blunt option)
    removed_islands, removed_island_faces = _drop_small_islands(
        joined, args.min_island_faces
    )
    print(f"DIAMESH_REMOVED_ISLANDS={removed_islands}")
    print(f"DIAMESH_REMOVED_ISLAND_FACES={removed_island_faces}")

    # Stage 1 — planar dissolve (lossless on flat regions)
    bpy.context.view_layer.objects.active = joined
    diss = joined.modifiers.new(name="DIAMesh_Dissolve", type="DECIMATE")
    diss.decimate_type = "DISSOLVE"
    diss.angle_limit = math.radians(PLANAR_ANGLE_DEG)
    diss.delimit = {"NORMAL"}
    diss.use_dissolve_boundaries = False
    try:
        bpy.ops.object.modifier_apply(modifier=diss.name)
    except RuntimeError as e:
        print(f"WARN: dissolve failed: {e}", file=sys.stderr)
        joined.modifiers.remove(diss)

    # Stage 2 — collapse to target ratio
    n_after_dissolve = len(joined.data.polygons)
    target = max(4, int(round(in_faces * decimate_ratio)))
    if target < n_after_dissolve:
        col = joined.modifiers.new(name="DIAMesh_Collapse", type="DECIMATE")
        col.decimate_type = "COLLAPSE"
        col.ratio = max(0.001, min(1.0, target / n_after_dissolve))
        col.use_collapse_triangulate = True
        if args.aggressive_collapse:
            col.delimit = {"MATERIAL"}
        else:
            col.delimit = {"MATERIAL", "SHARP", "SEAM", "UV"}
        try:
            bpy.ops.object.modifier_apply(modifier=col.name)
        except RuntimeError as e:
            print(f"WARN: collapse failed: {e}", file=sys.stderr)
            joined.modifiers.remove(col)

    # Stage 2.4 — post-collapse weld (cure cracks introduced by COLLAPSE).
    # COLLAPSE leaves vertices that *should* coincide millimetres apart
    # because the two surfaces collapsed independently. A second weld
    # pass at a tolerance scaled up from the pre-collapse value catches
    # these without touching input-level detail (which already passed
    # the stricter Stage 0 weld). Order matters: must run BEFORE
    # auto-fill-holes so boundary loops shrink first and fill_holes
    # has less to patch.
    if args.post_collapse_weld:
        bm = bmesh.new()
        bm.from_mesh(joined.data)
        bm.verts.ensure_lookup_table()
        post_weld_dist = weld_dist * float(args.post_collapse_weld_multiplier)
        n_verts_pre = len(bm.verts)
        try:
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=post_weld_dist)
        except (RuntimeError, ValueError) as e:
            print(f"WARN: post-collapse weld failed: {e}", file=sys.stderr)
        n_verts_post = len(bm.verts)
        n_post_welded = max(0, n_verts_pre - n_verts_post)
        bm.to_mesh(joined.data)
        bm.free()
        print(f"DIAMESH_POST_COLLAPSE_WELD_DIST={post_weld_dist:.6e}")
        print(f"DIAMESH_POST_COLLAPSE_WELD_VERTS={n_post_welded}")

    # Stage 2.45 — bridge nearby boundary loops (different concept from
    # fill_holes: instead of triangulating a single open loop with a
    # planar fan, find pairs of opposing open loops and link them with
    # a face strip. Suits panel seams where the two halves of a split
    # surface should merge. Runs BEFORE fill_holes so successfully
    # bridged pairs disappear from the boundary set and fill_holes only
    # patches what bridge couldn't pair up.
    if args.bridge_loops:
        bm = bmesh.new()
        bm.from_mesh(joined.data)
        bm.edges.ensure_lookup_table()
        bm.verts.ensure_lookup_table()

        loops = _group_boundary_edges_into_loops(bm)
        n_pairs_found = 0
        n_pairs_bridged = 0

        if loops:
            xs = [v.co.x for v in joined.data.vertices]
            ys = [v.co.y for v in joined.data.vertices]
            zs = [v.co.z for v in joined.data.vertices]
            if xs:
                dx = max(xs) - min(xs)
                dy = max(ys) - min(ys)
                dz = max(zs) - min(zs)
                br_diag = math.sqrt(dx * dx + dy * dy + dz * dz)
            else:
                br_diag = 0.0
            max_dist = br_diag * float(args.bridge_loops_max_distance_frac)

            # Compute centroid + size for each loop
            loop_info = []
            for li, loop_edges in enumerate(loops):
                seen = set()
                pts = []
                for e in loop_edges:
                    for v in e.verts:
                        if v.index not in seen:
                            seen.add(v.index)
                            pts.append((v.co.x, v.co.y, v.co.z))
                if not pts:
                    continue
                cx = sum(p[0] for p in pts) / len(pts)
                cy = sum(p[1] for p in pts) / len(pts)
                cz = sum(p[2] for p in pts) / len(pts)
                loop_info.append({
                    "idx": li,
                    "edges": loop_edges,
                    "centroid": (cx, cy, cz),
                    "size": len(loop_edges),
                })

            # Greedy pairing: each loop pairs with its nearest unpaired
            # peer if within max_dist AND of similar size (within 2x).
            paired = set()
            pairs = []
            for i, a in enumerate(loop_info):
                if a["idx"] in paired:
                    continue
                best_j = -1
                best_d = float("inf")
                for j, b in enumerate(loop_info):
                    if i == j or b["idx"] in paired:
                        continue
                    size_ratio = max(a["size"], b["size"]) / max(1, min(a["size"], b["size"]))
                    if size_ratio > 2.0:
                        continue
                    dx = a["centroid"][0] - b["centroid"][0]
                    dy = a["centroid"][1] - b["centroid"][1]
                    dz = a["centroid"][2] - b["centroid"][2]
                    d = math.sqrt(dx * dx + dy * dy + dz * dz)
                    if d < best_d and d <= max_dist:
                        best_d = d
                        best_j = j
                if best_j >= 0:
                    pairs.append((a, loop_info[best_j]))
                    paired.add(a["idx"])
                    paired.add(loop_info[best_j]["idx"])

            n_pairs_found = len(pairs)

            for a, b in pairs:
                edges = list(a["edges"]) + list(b["edges"])
                try:
                    bmesh.ops.bridge_loops(bm, edges=edges)
                    n_pairs_bridged += 1
                except (RuntimeError, ValueError):
                    # bridge_loops fails when topology is too complex;
                    # skip silently — the unbridged loop falls through
                    # to fill_holes for triangulation.
                    pass

            bm.to_mesh(joined.data)

        bm.free()
        print(f"DIAMESH_BRIDGE_LOOPS_PAIRS_FOUND={n_pairs_found}")
        print(f"DIAMESH_BRIDGE_LOOPS_PAIRS_BRIDGED={n_pairs_bridged}")

    # Stage 2.5 — auto-fill holes opened during decimation
    if args.auto_fill_holes:
        edges_before = len(joined.data.edges)
        faces_before = len(joined.data.polygons)
        design_skipped = 0
        defect_to_fill = 0

        # Pre-classify boundary loops if the user asked us to preserve
        # design holes (vents, fastener holes, slots, etc.).
        if args.fill_holes_skip_design:
            # Mesh diagonal in object-local coords (same coord system as
            # bmesh vertex .co). Object scale is identity post-import.
            xs = [v.co.x for v in joined.data.vertices]
            ys = [v.co.y for v in joined.data.vertices]
            zs = [v.co.z for v in joined.data.vertices]
            if xs:
                dx = max(xs) - min(xs)
                dy = max(ys) - min(ys)
                dz = max(zs) - min(zs)
                mesh_diagonal = math.sqrt(dx * dx + dy * dy + dz * dz)
            else:
                mesh_diagonal = 0.0

            bm = bmesh.new()
            bm.from_mesh(joined.data)
            bm.edges.ensure_lookup_table()
            bm.verts.ensure_lookup_table()

            for e in bm.edges:
                e.select = False
            for v in bm.verts:
                v.select = False

            loops = _group_boundary_edges_into_loops(bm)
            for loop_edges in loops:
                cls = _classify_boundary_loop(
                    loop_edges,
                    mesh_diagonal,
                    args.fill_holes_design_min_radius_frac,
                    args.fill_holes_design_circularity,
                )
                if cls == "defect":
                    defect_to_fill += 1
                    for e in loop_edges:
                        e.select = True
                        for v in e.verts:
                            v.select = True
                else:
                    design_skipped += 1

            bm.to_mesh(joined.data)
            bm.free()

            try:
                bpy.context.view_layer.objects.active = joined
                bpy.ops.object.mode_set(mode="EDIT")
                # Selection was set above on mesh data; do NOT select_all.
                bpy.ops.mesh.fill_holes(sides=int(args.fill_holes_max_sides))
                bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError as e:
                print(f"WARN: fill_holes (selective) failed: {e}", file=sys.stderr)
                try:
                    bpy.ops.object.mode_set(mode="OBJECT")
                except RuntimeError:
                    pass
        else:
            # Original blunt behaviour — fill every boundary loop ≤ N edges.
            try:
                bpy.context.view_layer.objects.active = joined
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_all(action="SELECT")
                bpy.ops.mesh.fill_holes(sides=int(args.fill_holes_max_sides))
                bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError as e:
                print(f"WARN: fill_holes failed: {e}", file=sys.stderr)
                try:
                    bpy.ops.object.mode_set(mode="OBJECT")
                except RuntimeError:
                    pass

        faces_added = len(joined.data.polygons) - faces_before
        edges_added = len(joined.data.edges) - edges_before
        print(f"DIAMESH_FILL_HOLES_FACES_ADDED={max(0, faces_added)}")
        print(f"DIAMESH_FILL_HOLES_EDGES_ADDED={max(0, edges_added)}")
        print(f"DIAMESH_FILL_HOLES_MAX_SIDES={int(args.fill_holes_max_sides)}")
        if args.fill_holes_skip_design:
            print(f"DIAMESH_FILL_HOLES_DESIGN_SKIPPED={design_skipped}")
            print(f"DIAMESH_FILL_HOLES_DEFECT_FILLED={defect_to_fill}")

        # Stage 2.6 — Laplacian smooth around the just-filled patches.
        # The newly-added faces (indices ≥ faces_before) plus their
        # 1-ring vertex neighbours get a small Laplacian relaxation so
        # the patch transitions blend with surrounding curvature instead
        # of reading as a flat triangle bandage.
        smooth_verts_count = 0
        if args.fill_holes_smooth and faces_added > 0:
            bm = bmesh.new()
            bm.from_mesh(joined.data)
            bm.faces.ensure_lookup_table()
            bm.verts.ensure_lookup_table()
            new_face_indices = range(faces_before, len(bm.faces))
            smooth_set = set()
            for fi in new_face_indices:
                if fi >= len(bm.faces):
                    continue
                for v in bm.faces[fi].verts:
                    smooth_set.add(v)
                    for e in v.link_edges:
                        other = e.other_vert(v)
                        if other is not None:
                            smooth_set.add(other)
            smooth_verts = list(smooth_set)
            smooth_verts_count = len(smooth_verts)
            if smooth_verts:
                for _ in range(int(args.fill_holes_smooth_iter)):
                    try:
                        bmesh.ops.smooth_vert(
                            bm,
                            verts=smooth_verts,
                            factor=float(args.fill_holes_smooth_factor),
                            use_axis_x=True,
                            use_axis_y=True,
                            use_axis_z=True,
                        )
                    except (RuntimeError, ValueError) as e:
                        print(
                            f"WARN: smooth_vert (boundary smoothing) failed: {e}",
                            file=sys.stderr,
                        )
                        break
                bm.to_mesh(joined.data)
            bm.free()
        print(f"DIAMESH_FILL_HOLES_SMOOTH_VERTS={smooth_verts_count}")

    # Stage 2.7 — post-collapse island cull. Decimation can leave
    # behind tiny disconnected fragments (orphans) introduced by
    # material delimit, sharp delimit, or collapse-induced topology
    # splits. Stage 0.5/0.6's island cull only catches PRE-collapse
    # islands; orphans born during Stage 2 survive into output. This
    # stage walks connected components on the post-everything mesh
    # and removes any island whose face count falls below the
    # threshold. Independent of decimation rounds — also cures
    # iterative-decimation amplification.
    post_cull_islands = 0
    post_cull_faces = 0
    if args.post_collapse_cull:
        bm = bmesh.new()
        bm.from_mesh(joined.data)
        bm.faces.ensure_lookup_table()

        # BFS connected components over face adjacency
        visited = set()
        islands = []
        for seed in bm.faces:
            if seed in visited:
                continue
            stack = [seed]
            component = []
            while stack:
                f = stack.pop()
                if f in visited:
                    continue
                visited.add(f)
                component.append(f)
                for e in f.edges:
                    for adj in e.link_faces:
                        if adj not in visited:
                            stack.append(adj)
            islands.append(component)

        min_faces = int(args.post_collapse_cull_min_faces)
        to_remove = []
        for island in islands:
            if len(island) < min_faces:
                to_remove.extend(island)
                post_cull_islands += 1
                post_cull_faces += len(island)

        if to_remove:
            try:
                bmesh.ops.delete(bm, geom=to_remove, context="FACES")
            except (RuntimeError, ValueError) as e:
                print(
                    f"WARN: post-collapse cull delete failed: {e}",
                    file=sys.stderr,
                )

        bm.to_mesh(joined.data)
        bm.free()
        joined.data.update()

        print(f"DIAMESH_POST_COLLAPSE_CULL_MIN_FACES={min_faces}")
    print(f"DIAMESH_POST_COLLAPSE_CULL_REMOVED_ISLANDS={post_cull_islands}")
    print(f"DIAMESH_POST_COLLAPSE_CULL_REMOVED_FACES={post_cull_faces}")

    # Output stats
    out_faces = len(joined.data.polygons)
    out_verts = len(joined.data.vertices)

    # Export FBX
    bpy.ops.export_scene.fbx(
        filepath=args.output,
        use_selection=False,
        embed_textures=True,
        path_mode="COPY",
        bake_anim=False,
        apply_unit_scale=True,
        global_scale=1.0,
    )

    # Sentinel lines for the parent Python process
    print(f"DIAMESH_INPUT_FACES={in_faces}")
    print(f"DIAMESH_OUTPUT_FACES={out_faces}")
    print(f"DIAMESH_INPUT_VERTICES={in_verts}")
    print(f"DIAMESH_OUTPUT_VERTICES={out_verts}")
    print(f"DIAMESH_RATIO={out_faces / max(in_faces, 1):.6f}")
    print(f"DIAMESH_DECIMATE_RATIO_REQUESTED={decimate_ratio:.6f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
