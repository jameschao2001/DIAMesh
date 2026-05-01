"""DIAMesh — command-line entry point.

Subcommands:

* ``diamesh view <file.fbx>`` — open an interactive viewer (Phase 1).
* ``diamesh reduce ...`` — placeholder for Phase 2 mesh reduction.
* ``diamesh info <file.fbx>`` — print mesh statistics without rendering.

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
        choices=["trimesh", "pymeshlab"],
        default="trimesh",
        help="reduction backend (default: trimesh = fast-simplification; "
             "pymeshlab uses MeshLab with boundary/normal preservation)",
    )
    p_reduce.set_defaults(func=_cmd_reduce)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
