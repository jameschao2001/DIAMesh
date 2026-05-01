# Blender Portable — Setup Guide

DIAMesh's `--backend blender` mode runs decimation inside a real
Blender runtime, which is the only path that preserves **materials,
textures, hierarchy, and animations** end-to-end through FBX I/O.

Blender Portable is **not committed to git** (~450 MB unpacked, way
over GitHub's per-file and per-repo recommended limits). You drop it
into `vendor/blender/` once, and DIAMesh finds it automatically.

## One-time setup

1. **Download Blender Portable (Windows ZIP)**
   - https://www.blender.org/download/
   - Pick the "Portable (.zip)" build for your OS, e.g.
     `blender-4.2.X-windows-x64.zip` (LTS recommended).

2. **Place it under `vendor/blender/`**
   ```
   DIAMesh/
   └── vendor/
       └── blender/
           ├── blender.exe          ← either layout works
           ├── 4.2/
           ├── ...
       — or —
       └── blender/
           └── blender-4.2.0-windows-x64/
               ├── blender.exe
               ├── 4.2/
               └── ...
   ```
   Both flat (binary directly under `vendor/blender/`) and nested
   (a single sub-folder under `vendor/blender/`) layouts are auto-detected.

3. **Verify**
   ```bash
   diamesh reduce data\Robot.fbx --ratio 0.25 --backend blender -o data\Robot_lod.fbx
   ```
   First run prints `blender_exe: <path>` in the metrics output to
   confirm the right binary was picked.

## Alternative: BLENDER_EXE env var

If you keep Blender elsewhere (e.g. system install, shared drive, or
just don't want to copy it under DIAMesh):

```cmd
set BLENDER_EXE=D:\Blender\blender-4.2\blender.exe
diamesh reduce ... --backend blender ...
```

This overrides any `vendor/blender/` lookup.

## Why portable, not system install

- No admin privileges required (corporate IT-friendly)
- Multiple Blender versions can co-exist on the same machine
- Repo-relative path means same command works for every developer
- Self-contained: Python, addons, Bundled FBX exporter — no PATH pollution

## Why not bundle in git

- Blender 4.x portable is ~450 MB after unzip
- GitHub strongly discourages files > 100 MB and repos > 1 GB
- LFS works but adds bandwidth cost on every clone
- Each developer downloads Blender once; that's a one-off step, not a
  per-clone cost

## License

Blender is GPL-3.0+. Bundling it inside a repository would compel
DIAMesh to be GPL-licensed too (linking-style entanglement). Treating
Blender as an external runtime tool — invoked via subprocess only —
keeps DIAMesh under MIT and Blender under GPL, no license collision.
