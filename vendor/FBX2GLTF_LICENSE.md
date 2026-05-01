# FBX2glTF — Vendored Binary

DIAMesh ships `vendor/fbx2gltf/fbx2gltf.exe` (originally `FBX2glTF-windows-x64.exe`)
from the FBX2glTF v0.9.7 release:

* Upstream: https://github.com/facebookincubator/FBX2glTF
* Release: v0.9.7 (2019-08-10)
* License: **MIT** (see below) — Copyright Facebook, Inc.

This binary is invoked by `diamesh.loader` to convert ``.fbx`` files into
``.glb`` (glTF 2.0 binary) on the fly, since trimesh + pyassimp on Windows
cannot read FBX without a system-wide assimp DLL.

---

## MIT License (FBX2glTF v0.9.7, upstream LICENSE)

Copyright (c) 2014-2019, Facebook, Inc. All rights reserved.

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
