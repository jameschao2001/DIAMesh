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
     E. triangulate (uniform topology for COLLAPSE)
4. Stage 1 — DISSOLVE modifier (planar merge, 5° threshold).
5. Stage 2 — COLLAPSE modifier (target ratio, delimit MATERIAL/SHARP/SEAM).
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
    return p.parse_args(argv)


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


def _repair(obj, weld_dist: float, sharp_angle_rad: float, degen_dist: float):
    """Run the full Stage-0 mesh repair sweep on a single object.

    Returns a dict with the count of each repair operation. Operations
    in order: recalc normals, weld, dissolve degenerate, drop loose,
    triangulate, mark sharp.
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

    # D. Mark sharp by dihedral angle
    sharp_marked = 0
    for edge in bm.edges:
        if len(edge.link_faces) == 2:
            try:
                if edge.calc_face_angle() > sharp_angle_rad:
                    edge.smooth = False
                    sharp_marked += 1
            except ValueError:
                pass

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
    }


def main() -> int:
    args = parse_args()

    # Constants
    WELD_DIST = 0.0001        # 0.1 mm — heal CAD-import vertex duplicates
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

    # Stage 0 — full repair sweep on the joined mesh
    repair = _repair(joined, WELD_DIST, sharp_angle_rad, DEGEN_DIST)
    print(f"DIAMESH_REPAIR_WELDED_VERTS={repair['welded_verts']}")
    print(f"DIAMESH_REPAIR_LOOSE_VERTS={repair['loose_verts']}")
    print(f"DIAMESH_REPAIR_LOOSE_EDGES={repair['loose_edges']}")
    print(f"DIAMESH_REPAIR_DEGEN_EDGES={repair['degen_edges']}")
    print(f"DIAMESH_REPAIR_SHARP_MARKED={repair['sharp_marked']}")

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
        col.delimit = {"MATERIAL", "SHARP", "SEAM"}
        try:
            bpy.ops.object.modifier_apply(modifier=col.name)
        except RuntimeError as e:
            print(f"WARN: collapse failed: {e}", file=sys.stderr)
            joined.modifiers.remove(col)

    # Stage 2.5 — auto-fill holes opened during decimation
    if args.auto_fill_holes:
        edges_before = len(joined.data.edges)
        faces_before = len(joined.data.polygons)
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
