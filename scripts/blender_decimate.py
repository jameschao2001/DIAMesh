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
import sys

import bpy  # type: ignore[import-not-found]  # only resolvable inside Blender


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

    # Apply DECIMATE modifier per mesh object. Per-object ratio keeps
    # part-level material assignment intact (no global concat).
    for obj in meshes:
        bpy.context.view_layer.objects.active = obj
        mod = obj.modifiers.new(name="DIAMesh_Decimate", type="DECIMATE")
        mod.decimate_type = "COLLAPSE"          # quadric edge collapse
        mod.ratio = decimate_ratio
        mod.use_collapse_triangulate = True
        try:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except RuntimeError as e:
            # Some non-mesh-eval objects can fail apply; skip them silently
            print(f"WARN: could not apply decimate to {obj.name}: {e}", file=sys.stderr)
            obj.modifiers.remove(mod)

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
