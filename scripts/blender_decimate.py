"""DIAMesh — Blender headless decimation script.

This script runs *inside Blender's Python runtime* (not the system
Python). It is invoked by ``diamesh.reducer._reduce_blender`` like so::

    blender --background --python scripts/blender_decimate.py -- \\
        --input model.fbx --output model_lod.fbx \\
        [--target-faces N | --ratio R]

Blender swallows everything before ``--`` for its own arg parser; we
parse the rest. The script:

1. clears the default cube/light/camera scene,
2. imports the source FBX (preserves materials, textures, hierarchy),
3. applies a ``DECIMATE`` modifier (collapse mode) to every mesh
   object, computing the per-object ratio so the *aggregate* face
   count hits the requested target,
4. exports the result back to FBX with embedded textures and
   ``COPY`` path mode (textures stay self-contained),
5. emits ``DIAMESH_*=…`` metric lines to stdout for the parent
   Python process to scrape.

Author: James Chao, Homi (AI Agent)
Version: 0.1.0
Date: 2026-05-01
"""

import argparse
import math
import sys

import bpy  # type: ignore[import-not-found]  # only resolvable inside Blender
import bmesh  # type: ignore[import-not-found]


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--target-faces", type=int)
    p.add_argument("--ratio", type=float)
    return p.parse_args(argv)


def _mesh_objects():
    return [obj for obj in bpy.data.objects if obj.type == "MESH"]


def _total_faces(meshes) -> int:
    return sum(len(o.data.polygons) for o in meshes)


def _total_verts(meshes) -> int:
    return sum(len(o.data.vertices) for o in meshes)


def main() -> int:
    args = parse_args()

    # Start from an empty scene to avoid the default cube polluting outputs.
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # Import FBX. Blender's importer preserves material/texture/animation.
    bpy.ops.import_scene.fbx(filepath=args.input)

    meshes = _mesh_objects()
    if not meshes:
        print("ERROR: no mesh objects found in FBX", file=sys.stderr)
        return 2

    in_faces = _total_faces(meshes)
    in_verts = _total_verts(meshes)
    if in_faces == 0:
        print("ERROR: input FBX has zero faces", file=sys.stderr)
        return 2

    if args.target_faces is not None:
        decimate_ratio = max(0.001, min(1.0, args.target_faces / in_faces))
    elif args.ratio is not None:
        decimate_ratio = max(0.001, min(1.0, args.ratio))
    else:
        print("ERROR: specify --target-faces or --ratio", file=sys.stderr)
        return 2

    # === Two-stage adaptive decimation ===
    #
    # Stage 1 — DISSOLVE (planar/limited dissolve):
    #   Merges co-planar faces (within angle_limit) into larger n-gons.
    #   This is *visually lossless* on flat regions: it removes redundant
    #   triangulation artefacts without disturbing silhouettes or
    #   boundaries. Industrial CAD-style FBX (5-axis glue sprayer, screw
    #   machine) has many planar panels — dissolve eats the redundant
    #   triangulations first, before COLLAPSE has to touch the geometry.
    #
    # Stage 2 — COLLAPSE (quadric edge collapse):
    #   Hits the user's target ratio. Runs *after* dissolve so the
    #   target budget is spent on actually-needed reduction, not on
    #   re-shrinking already-flat panels.
    #
    # === Adaptive per-part ratio ===
    #
    # Industrial scenes are heterogeneous: a single panel may have 10k
    # faces while a frame bar has 50. Applying the same global ratio
    # naively makes thin parts disintegrate. Instead we use a logarithmic
    # weighting:
    #
    #   per_obj_ratio = decimate_ratio ** (log(n_obj) / log(in_faces))
    #
    # Properties (with global decimate_ratio = 0.10):
    #   * n_obj == in_faces (single mesh)        → per_obj_ratio = 0.10
    #   * n_obj is large fraction of total       → per_obj_ratio ≈ 0.10
    #   * n_obj is tiny (frame bar, screw)       → per_obj_ratio → 1.0
    # Small parts are protected automatically without a hardcoded floor.
    # The aggregate face count still lands close to the user's target
    # because large parts dominate the budget.

    PLANAR_ANGLE_DEG = 5.0   # within this angle, faces are treated as co-planar
    WELD_DIST = 0.0001        # 0.1 mm — heal CAD-import vertex duplicates
    DEGEN_DIST = 1.0e-5        # collapse edges shorter than this
    SHARP_ANGLE_DEG = 30.0     # mark edges sharper than this so delimit SHARP fires
    sharp_angle_rad = math.radians(SHARP_ANGLE_DEG)
    log_total = math.log(max(in_faces, 2))

    # === Stage 0 — mesh repair (CAD-import cleanup) ===
    #
    # Industrial FBX coming out of CAD exporters (SolidWorks, Catia,
    # Inventor) carries a load of subtle topology defects that DECIMATE
    # is not robust against. We do a single bmesh sweep per object that
    # applies six repairs in the right order BEFORE the decimator runs:
    #
    #   A. recalc_face_normals — fix flipped/inconsistent winding so
    #      the dissolve step's NORMAL delimit can detect feature edges.
    #   0. remove_doubles (weld) — merge coincident verts within
    #      WELD_DIST. CAD exports each NURBS patch as its own triangle
    #      island; verts at patch seams match in space but not in
    #      index. Welding gives DECIMATE a consistent edge graph.
    #   B. dissolve_degenerate — collapse zero-length edges / zero-area
    #      sliver faces. These are noise that throws off the quadric
    #      error metric.
    #   C. delete loose verts/edges — drop geometry not connected to
    #      any face. Source of "floating fragment" artefacts after
    #      collapse.
    #   E. triangulate — CAD FBX is mostly triangulated already, but
    #      mixed quad/ngon meshes confuse COLLAPSE; force pure tri.
    #   D. mark sharp by face angle — set edge.smooth=False on edges
    #      whose dihedral exceeds SHARP_ANGLE_RAD. Without this, the
    #      delimit SHARP setting in stage 2 has no edges to honour.
    repair_stats = {
        "welded_verts": 0,
        "loose_verts_removed": 0,
        "loose_edges_removed": 0,
        "degenerate_dissolved": 0,
        "sharp_marked": 0,
        "objects_repaired": 0,
    }
    for obj in meshes:
        n_verts_before = len(obj.data.vertices)
        n_edges_before = len(obj.data.edges)
        n_faces_before = len(obj.data.polygons)

        bm = bmesh.new()
        bm.from_mesh(obj.data)

        # A. Recalc face normals (consistent winding)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

        # 0. Weld coincident verts (CAD seam heal)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=WELD_DIST)

        # B. Dissolve degenerate edges/faces (zero-area slivers)
        bmesh.ops.dissolve_degenerate(bm, dist=DEGEN_DIST, edges=bm.edges)

        # C. Remove loose geometry
        loose_verts = [v for v in bm.verts if not v.link_faces]
        if loose_verts:
            bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")
        loose_edges = [e for e in bm.edges if not e.link_faces]
        if loose_edges:
            bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")

        # E. Triangulate (uniform topology for COLLAPSE)
        bmesh.ops.triangulate(
            bm, faces=bm.faces[:], quad_method="BEAUTY", ngon_method="BEAUTY"
        )

        # D. Mark sharp edges by dihedral angle
        marked = 0
        for edge in bm.edges:
            if len(edge.link_faces) == 2:
                try:
                    if edge.calc_face_angle() > sharp_angle_rad:
                        edge.smooth = False
                        marked += 1
                except ValueError:
                    pass

        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()

        n_verts_after = len(obj.data.vertices)
        n_edges_after = len(obj.data.edges)
        repair_stats["welded_verts"] += max(0, n_verts_before - n_verts_after - len(loose_verts))
        repair_stats["loose_verts_removed"] += len(loose_verts)
        repair_stats["loose_edges_removed"] += len(loose_edges)
        # degenerate_dissolved is approximated by remaining edge delta after
        # subtracting loose edge removal — exact count requires bmesh result inspection.
        edge_delta = max(0, n_edges_before - n_edges_after - len(loose_edges))
        repair_stats["degenerate_dissolved"] += edge_delta
        repair_stats["sharp_marked"] += marked
        if (
            n_verts_after != n_verts_before
            or len(loose_verts) > 0
            or len(loose_edges) > 0
            or marked > 0
        ):
            repair_stats["objects_repaired"] += 1

    print(
        f"DIAMESH_REPAIR: welded={repair_stats['welded_verts']} "
        f"loose_v={repair_stats['loose_verts_removed']} "
        f"loose_e={repair_stats['loose_edges_removed']} "
        f"degen={repair_stats['degenerate_dissolved']} "
        f"sharp_marked={repair_stats['sharp_marked']} "
        f"objects={repair_stats['objects_repaired']}/{len(meshes)}"
    )

    for obj in meshes:
        bpy.context.view_layer.objects.active = obj
        n_obj_orig = len(obj.data.polygons)
        if n_obj_orig < 4:  # below this DECIMATE refuses to touch
            continue

        # --- Stage 1: planar dissolve (lossless on flat regions) ---
        diss = obj.modifiers.new(name="DIAMesh_Dissolve", type="DECIMATE")
        diss.decimate_type = "DISSOLVE"
        diss.angle_limit = math.radians(PLANAR_ANGLE_DEG)
        diss.delimit = {"NORMAL"}        # don't dissolve across normal discontinuities
        diss.use_dissolve_boundaries = False  # keep mesh open boundaries pinned
        try:
            bpy.ops.object.modifier_apply(modifier=diss.name)
        except RuntimeError as e:
            print(f"WARN: dissolve failed on {obj.name}: {e}", file=sys.stderr)
            obj.modifiers.remove(diss)

        # --- Stage 2: collapse to adaptive target ---
        n_after_dissolve = len(obj.data.polygons)
        if n_after_dissolve < 4:
            continue

        size_weight = math.log(max(n_obj_orig, 2)) / log_total  # in [0, 1]
        per_obj_ratio = decimate_ratio ** size_weight
        target = max(4, int(round(n_obj_orig * per_obj_ratio)))
        if target >= n_after_dissolve:
            # Adaptive ratio already protective enough; dissolve was
            # sufficient on its own.
            continue

        col = obj.modifiers.new(name="DIAMesh_Collapse", type="DECIMATE")
        col.decimate_type = "COLLAPSE"
        col.ratio = max(0.001, min(1.0, target / n_after_dissolve))
        col.use_collapse_triangulate = True
        # Keep collapse from eating through hard boundaries.
        col.delimit = {"MATERIAL", "SHARP", "SEAM"}
        try:
            bpy.ops.object.modifier_apply(modifier=col.name)
        except RuntimeError as e:
            print(f"WARN: collapse failed on {obj.name}: {e}", file=sys.stderr)
            obj.modifiers.remove(col)

    out_meshes = _mesh_objects()
    out_faces = _total_faces(out_meshes)
    out_verts = _total_verts(out_meshes)

    bpy.ops.export_scene.fbx(
        filepath=args.output,
        use_selection=False,
        embed_textures=True,
        path_mode="COPY",
        bake_anim=False,
        apply_unit_scale=True,
        global_scale=1.0,
    )

    # Sentinel lines for the parent Python process to scrape.
    print(f"DIAMESH_INPUT_FACES={in_faces}")
    print(f"DIAMESH_OUTPUT_FACES={out_faces}")
    print(f"DIAMESH_INPUT_VERTICES={in_verts}")
    print(f"DIAMESH_OUTPUT_VERTICES={out_verts}")
    print(f"DIAMESH_RATIO={out_faces / max(in_faces, 1):.6f}")
    print(f"DIAMESH_DECIMATE_RATIO_REQUESTED={decimate_ratio:.6f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
