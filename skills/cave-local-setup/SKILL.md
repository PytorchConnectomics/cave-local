---
name: cave-local-setup
description: Install this CAVE local package, prepare its supported image and segmentation sources, or create a separate deployment for another dataset.
---

Work from the repository containing `bin/cave`. Read [README](../../README.md)
and [first-installation handoff](../../docs/handoff.md), then the
[manifest example](../../templates/segmentation-iteration/manifest.example.json)
before preparing a real dataset. This release supports Linux
x86-64, OME-Zarr v2 images and precomputed uint64 source labels, using documented
public HTTP(S) sources or a read-only local source folder. Do not silently
reorder axes, change physical spacing, drop labels, or subdivide source labels.

Get the input locations, intended graph name, image/label alignment and free
local port. Use the metadata to establish XYZ shape, spacing in nanometers,
origin and the desired source-label policy. `source_labels` preserves
individual connected source-label components as editing units; `spatial_tiles`
requires an explicit choice and tile size; `agglomerated_supervoxels` takes
supervoxels plus an agglomeration `.npz` (supervoxel→segment, region-graph
affinities) and is the right basis when the labels came from an agglomeration. Unsupported input must fail with an
explanation before original data changes.

Prepare this clone:

```bash
bin/cave doctor
bin/cave deps
.venv/bin/python scripts/check_browser.py
bin/cave fetch
bin/cave build
```

`doctor` checks host prerequisites, architecture, Docker access, Node and free
disk, and reports CPU capacity from the actual Docker daemon. New and restored
deployments cap their declared CPU quotas to that capacity; larger machines
retain the defaults. Build fetches pinned upstream source and builds the graph runtime and
viewer. Fix the reported prerequisite instead of borrowing undocumented files
or images from a previous installation. Size real data separately from the
small example; generated caches and backups also need disk space.

The browser check launches an existing Playwright or system Chromium/Chrome.
If no browser is available on a Playwright-supported OS, install it with
`.venv/bin/python -m playwright install --with-deps chromium` and rerun the
check. Browser installation may need administrator access and rejects some
older Linux versions, including Ubuntu 20; a working system browser remains
usable. If none is available, complete API fixture validation
without `--browser`, then follow [browser-agent testing](../../docs/browser-agent-test.md).
Report that browser's results separately; a missing browser is not a passed check.

For a first disposable example:

```bash
bin/cave fixture /srv/cave-input/demo --variant zyx
bin/cave create /srv/cave-input/demo/manifest.json /srv/cave-deployments/demo --source-dir /srv/cave-input/demo
bin/cave start /srv/cave-deployments/demo
bin/cave validate /srv/cave-deployments/demo --edit-fixture --browser
```

For supplied data, inspect its actual metadata and explain the chosen editing
policy before ingestion. Begin with a separately prepared, aligned small crop
when dataset size is uncertain. This package does not itself convert or crop
arbitrary inputs; retain source provenance and verify the prepared metadata.
Create a manifest matching the documented schema, then use
the same `create`/`start` flow. Pass `--source-dir` only for local file inputs.
Create requires a new directory and a free port, generates private credentials
and a unique `cave-pkg-*` project, and mounts input read-only. For another
dataset, choose a separate manifest, deployment directory and port. Reuse
`start` on an existing deployment to preserve its edits.

`--edit-fixture` is for generated fixtures only and performs real merge, split,
undo and annotation tests. Never add acceptance edits to a user's graph just
to prove installation. On real data use `bin/cave validate DEPLOYMENT` and
observe the browser instead.

Run acceptance workflows one at a time on the host. Finish other deployment
start/stop/recreate jobs before browser testing; keep them paused until the
browser closes. Preserve any browser network failure in the test report.

Success requires healthy services, label/geometry checks, a read-only snapshot,
and browser evidence that image, segmentation and meshes align. A launched
container or an HTTP 200 alone is insufficient. Record commit, configuration,
reports and failures, and create a verified baseline backup before handing the
installation over (see [backup guidance](../cave-local-backup/SKILL.md)). Return
the viewer URL, deployment path, configuration and maintenance commands. If a
step fails, use `bin/cave status` and `bin/cave logs`
on this deployment and continue via [operation guidance](../cave-local-operate/SKILL.md).

On-demand supervoxel splitting is off by default and applies only to
`agglomerated_supervoxels`. For its disposable acceptance fixture add
`--supervoxel-split` to `bin/cave fixture`; this selects a grid that divides the
small fixture chunks and enables the dedicated verifier. See the
[refinement options and recovery procedure](../../docs/operations.md#on-demand-supervoxel-refinement-opt-in).
Keep real-volume enablement separate from fixture acceptance and within the
user's stated scope. Source volumes remain read-only; refinement writes only the
deployment's derived atomic volume and graph.

Use `bin/cave fixture NEW_INPUT --paint --graph-id GRAPH --port FREE_PORT` for
the independent painted-cut acceptance targets. `--paint` selects the
agglomerated policy and enables both switches. Follow the same create/start/
validate sequence with a new `cave-pkg-*` project and its own source directory.
`bin/cave create MANIFEST NEW_DEPLOYMENT --paint` is a convenience switch only
for generated agglomerated fixtures; it does not add paint geometry to their
source arrays. Configure a supplied dataset explicitly in its manifest.
Read the [paint API, limits and provenance contract](../../docs/operations.md#painted-cuts-opt-in).
Paint remains off by default, and no real-volume enablement follows from a
successful fixture test. Record the new paint cases separately from existing
svsplit acceptance. The client brush is a separate cave-seg-bio change.
