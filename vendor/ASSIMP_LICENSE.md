# Assimp — Vendored Shared Library

DIAMesh ships `vendor/assimp/assimp-vc143-mt.dll` from the Assimp v6.0.5
release for use by `diamesh.reducer` when exporting FBX files via
`pyassimp`.

* Upstream: https://github.com/assimp/assimp
* Release: v6.0.5 (2026-04-30)
* License: **BSD-3-Clause** (Open Asset Import Library) — see below.

## Why this is bundled

trimesh does not write FBX. The DIAMesh reducer round-trips mesh
geometry through Assimp (load OBJ → export FBX) when the user requests
``.fbx`` output. That requires Assimp's shared library at runtime;
shipping it removes the manual install burden on Windows machines
(especially behind corporate firewalls).

---

## Open Asset Import Library (Assimp) License (BSD-3-Clause)

Copyright (c) 2006-2025, assimp team.
All rights reserved.

Redistribution and use of this software in source and binary forms,
with or without modification, are permitted provided that the
following conditions are met:

* Redistributions of source code must retain the above copyright
  notice, this list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright
  notice, this list of conditions and the following disclaimer in
  the documentation and/or other materials provided with the
  distribution.

* Neither the name of the assimp team, nor the names of its
  contributors may be used to endorse or promote products derived
  from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
