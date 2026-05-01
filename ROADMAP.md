# DIAMesh Roadmap

## Phase 0 — Bootstrap (in progress)

* [x] `git init` + repo skeleton
* [x] Internalize pyrender source under `pyrender/`
* [x] Preserve pyrender MIT LICENSE under `vendor/PYRENDER_LICENSE.md`
* [x] Install dependencies: pyrender, trimesh, pyassimp
* [ ] `pyproject.toml` + editable install (`pip install -e .`)
* [ ] `.gitignore`

## Phase 1 — View FBX (target: today)

* [ ] `diamesh/loader.py` — `load_fbx(path) → trimesh.Trimesh` via `trimesh.load` (assimp backend)
* [ ] `diamesh/viewer.py` — wrap `pyrender.Viewer` with FBX entry point
* [ ] `diamesh/cli.py` — `diamesh view <file.fbx>`
* [ ] Test on a sample FBX (`tests/fixtures/cube.fbx`)
* [ ] Smoke test: viewer opens, mesh visible, mouse rotation works

## Phase 2 — Mesh Reduction (✅ implemented)

* [x] `diamesh/reducer.py` — quadric edge collapse decimation
  * Default backend: `trimesh.simplify_quadric_decimation` (fast-simplification C++ wrapper)
  * Optional: `pymeshlab` backend with boundary + normal preservation
* [x] `diamesh reduce <file.fbx> --target-faces N` CLI
* [x] `diamesh reduce <file.fbx> --ratio 0.25` CLI (keep 25%)
* [x] Output GLB (default, material-friendly), OBJ, PLY, STL via trimesh exporters
* [x] Quality metrics: input/output face count, vertex count, achieved ratio
* [ ] Per-part reduction (preserve material assignment) — currently concatenates parts
* [ ] FBX output (would need a fbx2gltf reverse pipeline or autodesk SDK)

## Phase 3 — Integrated GUI (Future)

* [ ] In-viewer toolbar: Load / Reduce / Save buttons
* [ ] LOD slider (interactive reduction with live preview)
* [ ] Mesh-quality heatmap (custom shader, modifies pyrender internals)
* [ ] Save reduced mesh from viewer

## Phase 4 — Advanced (Future)

* [ ] Batch processing (multiple FBX → multi-LOD outputs)
* [ ] Texture atlas optimization
* [ ] UV-aware simplification
* [ ] Web viewer (export to GLB + Three.js)

---

## Why pyrender as base

We chose to internalize pyrender (Option C in design discussion 2026-05-01) instead of treating it as a pip dependency because Phase 3 features (custom shaders for quality heatmaps, integrated GUI controls inside the viewer) will need to modify pyrender internals. Owning the source rather than fighting upstream is the safer long-term path.

## License obligations (MIT)

Every commit that touches `pyrender/` must keep:
- Original copyright notice in source files
- `vendor/PYRENDER_LICENSE.md` intact
- DIAMesh's own LICENSE file noting embedded pyrender code
