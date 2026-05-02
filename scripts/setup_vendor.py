"""DIAMesh — auto-download platform-specific vendored binaries.

DIAMesh ships Windows binaries (FBX2glTF + Assimp) directly in
``vendor/`` so the typical Windows operator can ``git clone`` and run
without extra steps. For Linux and macOS the equivalent binaries are
fetched on demand by this script, which keeps the repo small and avoids
uploading per-OS artefacts that the bulk of users don't need.

Run once after ``git clone``::

    python scripts/setup_vendor.py

The script is idempotent: if a binary is already present at the
expected path it skips the download.

Blender Portable is too large to download from a script (≈250 MB) and
licensing-wise we don't want to redistribute it; see
``vendor/BLENDER_SETUP.md`` for manual placement.

Author: James Chao, Homi (AI Agent)
Version: 0.1.0
Date: 2026-05-02
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path


VENDOR = Path(__file__).resolve().parent.parent / "vendor"

FBX2GLTF_VERSION = "v0.9.7"
ASSIMP_VERSION = "v6.0.5"

FBX2GLTF_BASE = (
    "https://github.com/facebookincubator/FBX2glTF/releases/download/"
    f"{FBX2GLTF_VERSION}"
)
ASSIMP_BASE = (
    f"https://github.com/assimp/assimp/releases/download/{ASSIMP_VERSION}"
)


def _platform_key() -> str:
    """Return one of ``"windows"``, ``"linux"``, ``"darwin"``."""
    sys_name = platform.system()
    if sys_name == "Windows":
        return "windows"
    if sys_name == "Linux":
        return "linux"
    if sys_name == "Darwin":
        return "darwin"
    raise RuntimeError(f"Unsupported platform: {sys_name}")


def _download(url: str, dest: Path) -> None:
    print(f"  ↓ {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)


def setup_fbx2gltf() -> None:
    print("[setup_vendor] FBX2glTF")
    plat = _platform_key()
    asset_map = {
        "windows": ("FBX2glTF-windows-x64.exe", "fbx2gltf.exe"),
        "linux":   ("FBX2glTF-linux-x64",       "fbx2gltf"),
        "darwin":  ("FBX2glTF-darwin-x64",      "fbx2gltf"),
    }
    asset_name, local_name = asset_map[plat]
    out_dir = VENDOR / "fbx2gltf"
    out_path = out_dir / local_name

    if out_path.exists():
        print(f"  · already present: {out_path}")
        return

    _download(f"{FBX2GLTF_BASE}/{asset_name}", out_path)
    if plat != "windows":
        os.chmod(out_path, 0o755)
    print(f"  ✓ ready at {out_path}")


def setup_assimp() -> None:
    print("[setup_vendor] Assimp")
    plat = _platform_key()
    if plat == "darwin":
        # Apple Silicon vs Intel
        asset_name = (
            "macos-arm64-v6.0.5.zip"
            if platform.machine().lower() in ("arm64", "aarch64")
            else "macos-x64-v6.0.5.zip"
        )
    else:
        asset_name = f"{plat}-x64-{ASSIMP_VERSION}.zip"

    lib_suffixes = {
        "windows": (".dll",),
        "linux":   (".so", ".so.5", ".so.6"),
        "darwin":  (".dylib",),
    }[plat]

    out_dir = VENDOR / "assimp"

    # Skip if any plausible shared library is already there
    existing = []
    for suffix in lib_suffixes:
        existing.extend(out_dir.glob(f"*{suffix}*"))
    if existing:
        print(f"  · already present: {existing[0]}")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_zip = out_dir / asset_name
    _download(f"{ASSIMP_BASE}/{asset_name}", tmp_zip)

    # Extract — find any shared library inside the archive and flatten
    # them into vendor/assimp/. Assimp release archives nest under a
    # ``Release/`` (Windows) or ``lib/`` (Linux/macOS) directory; we
    # don't care, just extract by basename.
    with zipfile.ZipFile(tmp_zip, "r") as zf:
        lib_members = [
            m for m in zf.namelist()
            if any(s in Path(m).name.lower() for s in (".dll", ".so", ".dylib"))
            and not m.endswith(".pdb")
        ]
        if not lib_members:
            print(
                "  ! no shared library found in archive — extracting "
                "everything to vendor/assimp/ for manual inspection"
            )
            zf.extractall(out_dir)
        else:
            for member in lib_members:
                target = out_dir / Path(member).name
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                print(f"  ✓ {target.name}")

    tmp_zip.unlink()


def setup_blender() -> None:
    print("[setup_vendor] Blender")
    print(
        "  · DIAMesh does not auto-download Blender (250+ MB, licensing).\n"
        "    Manual install — see vendor/BLENDER_SETUP.md.\n"
        "    Linux:   blender-4.2.X-linux-x64.tar.xz\n"
        "    macOS:   blender-4.2.X-macos-arm64.dmg / -macos-x64.dmg\n"
        "    Windows: blender-4.2.X-windows-x64.zip\n"
        "  Drop the unpacked tree under vendor/blender/."
    )


def main() -> int:
    print(
        f"DIAMesh setup_vendor — host: {platform.system()} "
        f"{platform.machine()} (python {sys.version_info.major}."
        f"{sys.version_info.minor})"
    )
    setup_fbx2gltf()
    setup_assimp()
    setup_blender()
    print("[setup_vendor] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
