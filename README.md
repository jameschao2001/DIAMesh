# DIAMesh

> Delta Intelligence Agent for **Mesh** processing — Python 3D mesh viewer + automatic mesh reduction toolkit, built on top of [pyrender](https://github.com/mmatl/pyrender).

## What it does

* **Phase 1 — View FBX**: load `.fbx` files, render them in an interactive window
* **Phase 2 — Auto reduce**: quadric edge collapse decimation with configurable target face counts / ratio
* **Phase 3+** — additional mesh processing features built on the embedded pyrender base

## Status

🚧 **In active development**. See [`ROADMAP.md`](ROADMAP.md).

## Architecture

```
DIAMesh
├── pyrender/           # ⭐ embedded copy of pyrender (MIT, see vendor/PYRENDER_LICENSE.md)
├── diamesh/            # ⭐ DIAMesh-native code on top of pyrender
│   ├── loader.py       # FBX → mesh data (via trimesh + pyassimp)
│   ├── viewer.py       # interactive viewer (wraps pyrender.Viewer)
│   ├── reducer.py      # mesh reduction (Phase 2)
│   └── cli.py          # `diamesh view` / `diamesh reduce`
├── scripts/
├── tests/
├── vendor/
│   └── PYRENDER_LICENSE.md   # pyrender's MIT license preserved
└── pyproject.toml
```

The `pyrender/` folder is an **internalized copy** of pyrender — DIAMesh treats its source as part of its own codebase. The MIT license is preserved at `vendor/PYRENDER_LICENSE.md` per the upstream LICENSE terms.

## Install

```bash
pip install -e .
```

### Platform-specific vendor binaries

Windows users get FBX2glTF + Assimp shipped in the repo and can use the
tool immediately. Linux / macOS users run a one-time setup that
auto-downloads the equivalent binaries:

```bash
python scripts/setup_vendor.py
```

The script downloads the right FBX2glTF release asset and the right
Assimp shared library for the host platform (Linux x64, macOS x64,
or macOS arm64) into `vendor/fbx2gltf/` and `vendor/assimp/`. Idempotent.

Blender Portable is **not** auto-downloaded (size + licence). All three
platforms get a manual placement step — see
[`vendor/BLENDER_SETUP.md`](vendor/BLENDER_SETUP.md).

Dependencies (auto-installed from `pyproject.toml`):
- numpy, pillow, pyglet<2, pyopengl, networkx, scipy, freetype-py, imageio, six, trimesh
- pyassimp (for FBX loading via assimp)

## Quick start

### Phase 1 — view an FBX

```bash
diamesh view path/to/model.fbx
```

A window pops up; mouse-drag to rotate, scroll to zoom.

### Phase 2 — reduce face count

```bash
diamesh reduce path/to/model.fbx --target-faces 5000 --output reduced.fbx
diamesh reduce path/to/model.fbx --ratio 0.25 --output reduced.fbx   # keep 25%
```

## Why pyrender as base

* Pure Python, MIT-licensed
* PBR-quality rendering with good defaults
* Active project, well-tested core
* Extensible scene graph that lets us slot in custom passes

We chose **C — internalized copy** (rather than `pip install pyrender` as a dependency) because future DIAMesh features will need to modify the pyrender internals (custom shaders for mesh-quality heatmaps, integrated GUI controls, etc).

## License

DIAMesh is MIT-licensed. The embedded pyrender code retains its original MIT license — see [`vendor/PYRENDER_LICENSE.md`](vendor/PYRENDER_LICENSE.md).

## Author

James Chao, Homi (AI Agent) — 2026-05
