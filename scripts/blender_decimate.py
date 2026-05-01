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
             "faces. CAD assemblies often contain genuinely disjoint micro-"
             "components (screw caps, sensor heads) that look attached only "
             "because dense triangulation visually surrounds them. After "
             "decimation those mini-islands hover as artefacts. 0 disables "
             "the cull (default). Try 50-200 for production-line LOD.",
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

    # Stage 0.5 — drop disjoint micro-islands (LOD-friendly cull)
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
