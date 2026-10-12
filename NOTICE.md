# Licenses and upstream credits

The original deployment scripts, tests, documentation and agent skills in this
repository use the MIT license in [LICENSE](LICENSE). That grant applies to
our work; it does not replace the licenses of downloaded source, Python/npm
packages, container images or components built from them.

The design combines the CAVE services maintained by CAVEconnectome with the
seung-lab Spelunker fork of Neuroglancer and Fullstory's Bigtable emulator.
Native graph editing algorithms and the viewer TypeScript source remain
upstream. The viewer build applies the exact dependency-only overlay in
[config/viewer-dependencies.lock.json](config/viewer-dependencies.lock.json).

| Component | Acquisition | Inspected upstream license |
| --- | --- | --- |
| [Spelunker / Neuroglancer](https://github.com/seung-lab/neuroglancer/blob/d4aeac3b66eb4bd3fa86dbdc0202a7f006439740/LICENSE) | Source revision in `upstream.lock.json` | Apache-2.0 |
| [Fullstory Bigtable emulator](https://github.com/fullstorydev/emulators/blob/4b55687158696cd3d906e467b9afc2f9a41c9575/bigtable/LICENSE) | Source revision in `upstream.lock.json` | MIT, Copyright (c) 2021 Engineering at Fullstory |
| [PyChunkedGraph](https://github.com/CAVEconnectome/PyChunkedGraph/blob/v2.22.0.dev11/LICENSE) | Digest-pinned upstream v2 image | MPL-2.0 |
| [AnnotationFrameworkInfoService](https://github.com/CAVEconnectome/AnnotationFrameworkInfoService/blob/master/LICENSE) | Digest-pinned upstream image | MIT, Copyright (c) 2024 CAVE |
| [EMAnnotationSchemas](https://github.com/CAVEconnectome/EMAnnotationSchemas/blob/master/LICENSE) | Digest-pinned upstream image | MIT, Copyright (c) 2018 Forrest Collman |
| [AnnotationEngine](https://github.com/CAVEconnectome/AnnotationEngine/blob/master/LICENSE) | Digest-pinned upstream image | MIT, Copyright (c) 2018 Forrest Collman |
| [MaterializationEngine](https://github.com/CAVEconnectome/MaterializationEngine/blob/master/LICENSE) | Digest-pinned upstream image | MIT |
| [DynamicAnnotationDB](https://github.com/CAVEconnectome/DynamicAnnotationDB/blob/master/LICENSE) | Upstream annotation/materialization dependency | MIT, Copyright (c) 2024 CAVE |

The PyChunkedGraph link deliberately identifies the pinned v2 release. Its
legacy `pcgv1` branch has a different license and is outside this package.
Container source links identify their projects; source/image digest mappings
are not a complete component inventory.

Redis, PostgreSQL/PostGIS, nginx, Go and Alpine images retain their own licenses.
So do all Python/npm dependencies. Keep their distribution metadata and license
files when redistributing those components. This repository publishes source
for the packaging layer, not vendor trees, binary images or a repackaged service
bundle. Any future redistribution of upstream binaries or modified covered
source must also fulfill that component's license and source availability terms.

Local adapters call upstream APIs and supply emulator networking, a single
local editor identity, configuration, database connection recovery, serialized
linked-table creation, graph construction, source-label constraints and bounded
on-demand meshes. The tracked source review found no vendored upstream graph or
viewer implementation. Minimal linked-table setup was adapted to upstream CAVE
interfaces; the corresponding MIT permission and attribution notices are
retained below. These adapters do not relicense the services they load.

Each local viewer build keeps the upstream Apache license in
`LICENSE.upstream` and collected upstream/npm runtime license texts in
`THIRD_PARTY_NOTICES.txt`, beside `build-info.json`. Generated viewer assets and
these generated notices are not committed to the repository. Keep those files
if you copy or redistribute the built viewer.

See [the dependency review](docs/dependencies.md) for advisory findings and the
limits of this release review.

## Retained CAVE MIT notices

DynamicAnnotationDB and AnnotationFrameworkInfoService:

```text
MIT License

Copyright (c) 2024 CAVE

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

AnnotationEngine and EMAnnotationSchemas:

```text
MIT License

Copyright (c) 2018 Forrest Collman

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

MaterializationEngine:

```text
MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
