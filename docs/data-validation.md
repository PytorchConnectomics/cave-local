# Data support and checks

The repository accepts a one-channel Neuroglancer precomputed segmentation
with unsigned integer labels, paired with a three-dimensional OME-Zarr v2
image. The image must name its axes as ZYX or XYZ and state a supported length
unit. Dimensions and physical spacing must agree with the manifest and
segmentation. Nanometers, micrometers, millimeters and meters are normalized to
nanometers. Dataset and group scale transforms are composed in their declared
order. Channel/time axes, unknown transforms or units, and nonzero origins are
rejected with an explanation; they are not silently reinterpreted.

Local sources are mounted read-only under `/source`. Public HTTP(S) sources
use the same input checks. The initial tests exercise an actual HTTP server;
they do not establish compatibility with authenticated cloud services.

`source_labels` preserves each six-connected source component. Fragments that
touch across graph chunks are joined with edges that cannot be cut. Separate
regions sharing the same source ID remain separate roots. A split with the
same source ID in both seed groups is rejected. `spatial_tiles` explicitly
allows source components to be divided into connected pieces within spatial
tiles. It uses compact categorical IDs, avoiding overflow even when a source
label is the largest possible uint64 value. `agglomerated_supervoxels` keeps
supervoxels indivisible like `source_labels`, joins same-segment contacts from
its mapping file, and checks every root against exactly one initial segment.

`scripts/make_fixture.py` creates invented, redistributable inputs and an
`expected.json` describing their source components. It refuses to overwrite
existing output. The two variants differ in shape, axis order, physical
spacing, units and topology. Both contain background, sparse IDs, disconnected
regions sharing an ID, objects crossing chunk boundaries, and uint64 extremes.

## Automated checks

Run the input and storage regressions with:

```sh
.venv/bin/python -m pytest -q tests/data
```

The 17 cases verify actual precomputed/Zarr writes and reads, HTTP source
loading, both input geometries and atom policies, a concrete uint64 overflow
collision, metadata failures, scale composition and clipped storage chunks.
They also cover the storage alignment bug found by the first fresh install.

For an installed disposable fixture:

```sh
.venv/bin/python scripts/choose_iteration_scenario.py DEPLOYMENT
.venv/bin/python scripts/verify_segmentation_iteration.py DEPLOYMENT
.venv/bin/python scripts/verify_iteration_persistence.py DEPLOYMENT --holding-edit
```

The API verifier independently re-reads every source and served atomic voxel,
checks every atomic component and bounding box, derives face adjacency from
voxels, and compares the complete native root partition with those expected
connections. It performs native merge, split and both Undo operations; checks
complete restored memberships; decodes meshes and checks physical bounds;
inserts and reads an annotation through CAVEclient; and verifies the inserted
row and its segmentation IDs in a materialized snapshot.

For the source-label policy, also run:

```sh
.venv/bin/python scripts/verify_source_label_policy.py DEPLOYMENT
```

It attempts a same-atom cut and a cut across joined chunk fragments. Both must
fail clearly, and the roots of every atom must remain unchanged. Tests that
make edit requests require `test_fixture: true` and a matching `cave-pkg-`
Compose project. They are not suitable for real proofreading data.

The persistence verifier leaves a real merge or split applied, stops and
starts the entire fixture stack, compares complete snapshots exactly, and
then undoes the edit and checks all original memberships. A failure saves its
evidence and any operation that could not be cleaned up.

`scripts/snapshot.py DEPLOYMENT --output PATH` performs read-only checks of all
atomic roots and complete root leaf memberships, live annotation rows
(including invalidated rows in active tables), all available materialization
versions and their tables, Redis edit history and corresponding native
operation details. It excludes ports and host paths so the JSON can be compared
across restart or restoration. Large mappings are represented by hashes and
counts; small ones also include complete IDs.

## Evidence from the initial isolated runs

The ZYX/nanometer/source-label deployment passed exhaustive checks of 38,400
voxels, 19 atoms, six source components and 22 same-label edges. Native editing,
Undo, decoded meshes, annotation readback and materialization passed. Both
indivisible-source cut cases were rejected without changing any roots.

The XYZ/micrometer/spatial-tile deployment passed exhaustive checks of 46,080
voxels, 74 atoms, eight source components and 124 same-label edges. Its edit
test used a largest-uint64 source object spanning 36 atoms. A genuine split
also survived a complete stack restart with an identical graph/log/annotation
snapshot, and Undo restored every original membership afterward.

These are initial implementation checks. The release report must separately
record fresh installations of the final commit, browser validation, backup
restoration and the tested host environment. They do not prove arbitrary
platform support or whole-dataset production-scale meshing. The local mesher
has a bounded root volume; larger workloads need a separately tested meshing
arrangement.

## Recovering closed database connections

Materializing annotations copies their database, and the upstream workflow
closes existing connections while making that copy. We found that the old
annotation service could keep one of those closed connections after a request,
then fail the next request with HTTP 500. Its connection check could accidentally
check a different, healthy connection. The local adapter now releases each
request's or worker task's session and checks a pooled connection before using
it. It does not retry annotation writes.

The regression uses real fixture services and PostgreSQL connections:

```sh
.venv/bin/python scripts/verify_database_reconnect.py DEPLOYMENT
```

It first captures all graph, annotation, materialization and edit-log data.
Then it deliberately closes idle connections in the fixture's annotation
database, closes connections in an actual materialized database, and restarts
only PostgreSQL while the application services keep running. The first real
read after each interruption must return HTTP 200, as must independent
concurrent reads. Returned data and the final complete snapshot must remain
unchanged. No failed request is retried. The script requires a disposable
`cave-pkg-` fixture, and saves any failure with its phase.

The original implementation was tested as a negative control: closing only
its held idle transactions made the very first annotation read return HTTP
500. The fixed implementation returned HTTP 200 on its first annotation and
materialized-table reads. After restarting only PostgreSQL, its first dataset
information, annotation and materialized-table reads also returned HTTP 200.
All concurrent reads passed, and the complete graph/log/annotation snapshot
was identical, including both available materialization versions. These
checks belong in each final fresh-install run; the control result alone is
not release evidence.

## Creating linked tables at the same time

Posting an annotation starts background processing. If a user immediately asks
for a materialized snapshot, both jobs can try to create the same linked
segmentation table. The upstream creators checked whether it existed and then
inserted its metadata separately. We observed two workers pass that check
together; one then failed with duplicate metadata and the snapshot never
finished.

Both creator entry points now use the same database lock and transaction for
checking metadata, creating the table and recording its metadata. The next
creator waits and sees the committed result. Existing compatible metadata,
including its timestamps, is retained. Incompatible metadata or an unexplained
existing table causes a clear failure instead of being silently adopted.

```sh
.venv/bin/python scripts/verify_segmentation_metadata.py DEPLOYMENT
```

This regression starts four independent processes using the actual annotation
and materialization creators. A temporary PostgreSQL lock holds their inserts
so their attempts overlap reliably. All four must succeed, with exactly one
correct metadata entry and a readable linked table. It then posts an annotation
and immediately materializes it, checking the annotation and segmentation IDs.
Existing graph edits, annotation rows and older snapshots must remain intact.
The fresh-install harness includes this check.

The old implementation reproduced both physical-table and duplicate-metadata
errors under this staged overlap. Development checks of the proposed adapter
are separate from the final fresh installations, which must test the deployed
adapter through the regular service startup.
