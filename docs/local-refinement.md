# Local supervoxel refinement — offline prototype

This is an **offline, small-volume prototype**, not a live CAVE editing feature.
It never updates PyChunkedGraph, Bigtable, CAVE annotations, the Spelunker viewer,
or any deployment. It preserves the supplied base atom labels and root-membership
snapshot and writes new labels separately. Do not copy its generated IDs or
patches into a live graph. Existing browser Split remains unchanged.

The prototype refines atomic editing units; every child initially inherits the
parent's existing proofread root. It does not decide which other atoms should
follow either child into a different neuron. A later graph split is a separate
operation requiring native integration. Thus refinement alone does not separate
neurons in the root view.

## Native support audit

The dependency image is locked in `images.lock.json` and `docker/pcg.Dockerfile`:
`caveconnectome/pychunkedgraph@sha256:3794e14d0d862d33d98483dff2f6449052e6c43393117a47cb0c099ed8a26550`,
labelled `v2.22.0.dev11`. The corresponding upstream Git tag resolves to commit
`5327b224a6b7afac4582e501ae53cfe45a9ca5fa`. Source was checked out read-only under
`.runtime/dependencies/PyChunkedGraph`. Docker was unavailable in the audited
login; source equivalence to the image-installed bytes was not verified.

At that source revision:

- `pychunkedgraph/graph/chunkedgraph.py:732–843` exposes merge/split/undo/redo
  through edits of existing atomic IDs.
- `graph/operation.py:124–153` recognizes merge, split, multicut, undo and redo.
  `GraphEditOperation.execute` (line 396) supplies graph root locks, timestamps
  and logs; it has no voxel-patch/replacement operation.
- `ingest/create/atomic_layer.py:add_atomic_edges` (line 25) can write atomic
  Parent/L2 Child/connectivity rows, but writes directly through the client.
  It does not provide an incremental atom-plus-voxel transaction or undo.
- `graph/meta.py` configures a watershed CloudVolume. Its `DATA_VERSION` does
  not constitute a voxel-patch journal coordinated with graph publication.
- `graph/chunks/utils.py:_compute_chunk_id` (line 158) encodes layer, XYZ chunk
  coordinates and low segment bits. The offline encoder is tested against the
  exact upstream function when that source is present.
- `graph/client/bigtable/client.py:create_node_ids` (line 571) increments a
  per-chunk counter. Our existing importer instead assigns `cid + 1..N` in
  `scripts/ingest_segmentation_iteration.py`, without reconciling that counter.
  Any native atom creation must first prove it cannot collide with those IDs.

Locally, `scripts/source_label_policy.py` rejects cuts within a source label;
`prepare`/`ingest` preserve immutable atom storage and existing ingested graphs.
The audit therefore found **no complete native refinement/history primitive**.
No backend source, package lock or live deployment has been changed.

## Inputs and limits

Use Linux local disk only (POSIX rename, directory fsync and `flock` are required;
atomic export additionally requires libc `renameat2` with `RENAME_NOREPLACE`).
The store's final directory name must begin `cave-pkg-`; it must not exist.
Inputs are a disposable/exported **native atom-label** uint64 ZYX `.npy` volume,
not arbitrary raw ABISS label IDs; an exact atom-to-current-root JSON map; and
optional annotations as integer XYZ voxel points. Existing proofreading must
already be captured in that root map. This prototype does not fetch/export a
live proofread state itself. Atom IDs must match their layer-1 chunk and a single
atom may not span chunks. Multiple atoms can share one source ABISS label.

The total volume is limited to 262,144 voxels (64 cubed). Roots and labels may
use the full uint64 range. Arrays are ZYX; points and chunk dimensions are XYZ.
Default ID layout is 8 layer bits, 10 spatial bits per dimension and 26 segment
bits. Supply the actual layout on init if different. Labels are voxel-aligned;
there is no world-coordinate/resolution conversion or affinity/image input.
Anisotropic EM spacing is not represented: this version explicitly uses
isotropic voxel distances, not physical distances.
The watershed uses voxel-distance shape only and should be visually assessed;
it is a segmentation proposal, not evidence of a correct biological boundary.

## Commands

Use the environment with repository NumPy, SciPy and pytest dependencies.
The standalone entry point deliberately avoids the live `bin/cave` lifecycle:

```bash
.venv/bin/python scripts/local_refinement.py --store /tmp/cave-pkg-refine init \
  --labels atoms.npy --roots roots.json --annotations points.json --chunk-xyz 64 64 64
.venv/bin/python scripts/local_refinement.py --store /tmp/cave-pkg-refine preview \
  --parent 72057594037927937 --seeds '[[10,10,10],[20,10,10]]'
```

Preview returns a version, per-child counts and XYZ bounding boxes, and
`preview_hash`. Components mode ignores seeds and normalizes them to an empty list. Apply must supply the reviewed
version; supplying its preview hash additionally verifies the exact partition:

```bash
.venv/bin/python scripts/local_refinement.py --store /tmp/cave-pkg-refine apply \
  --parent 72057594037927937 --seeds '[[10,10,10],[20,10,10]]' \
  --expected-version VERSION --preview-hash PREVIEW_HASH
.venv/bin/python scripts/local_refinement.py --store /tmp/cave-pkg-refine read \
  --point-xyz 10 10 10 --version VERSION
.venv/bin/python scripts/local_refinement.py --store /tmp/cave-pkg-refine export \
  --output /tmp/cave-pkg-export --version VERSION
.venv/bin/python scripts/local_refinement.py --store /tmp/cave-pkg-refine undo \
  --expected-version CURRENT_VERSION
.venv/bin/python scripts/local_refinement.py --store /tmp/cave-pkg-refine verify
.venv/bin/python scripts/local_refinement.py --store /tmp/cave-pkg-refine recover
```

`--mode components` separates already disconnected pieces; `--seeds` is then
unnecessary. Seed mode needs at least two unique integer points in the selected
atom and at least one seed in every disconnected component. The preview is
pure: no store writes. New IDs are allocated above every historical segment in
that chunk, including undone and quarantined events, but are **not reserved
in native Bigtable**. Reversing seed order gives the same deterministic result.

Exit 3 means stale version/preview; exit 2 means invalid input or storage error.
A preview hash is optional because scripts may explicitly accept recomputation;
interactive callers should always provide it. Undo currently reverses only the
latest event. It appends a compensating event; undoing an undo acts as redo, restoring the
same historical child IDs for the same fragments. New refinements never reuse
those IDs.
Arbitrary historical selective undo is deferred.

## Persistence and consistency

`base.npy` and `meta.json` define a hash-checked immutable base; `events/HASH/`
holds a canonical `event.json`, bounded before/after voxel-label patches, a
full derived `snapshot.json`, and `meshes.json`. `CURRENT` selects one complete
chain. There are no writes to the original supplied `.npy` or ABISS data.

A mutation holds a store lock, checks expected CURRENT, replays and verifies the
base and published events, writes/fsyncs a staging directory, renames it into
`events/`, then atomically replaces/fsyncs CURRENT. CURRENT is the only logical
publication point. Readers select one published hash and replay that immutable chain. Explicit
historical versions must be ancestors of CURRENT; complete but unpublished
crash orphans cannot be read, previewed or exported as valid versions.
Partial events never appear as a published version. `verify` also refuses
unpublished event directories or staging leftovers; `status` reports them.
`recover` removes abandoned
staging files and moves unpublished event directories into `orphans/`; it
preserves their allocated IDs. Recovery never advances CURRENT automatically.
Initialization failure may leave an incomplete new directory, which is rejected
on reads/re-init and must be inspected; recovery of initialization is deferred.

Every published event includes hashes of its voxel patch, snapshot and mesh;
read verifies them and recomputes snapshot/mesh consistency. Snapshots include
all face-contact adjacency, edges whose endpoints share root membership, and
point annotations remapped by location. This graph is a **derived offline
contact graph**, not PyChunkedGraph topology, native multicut weights, or native
root lineage. It is not sufficient to migrate a native proofreading history.

Mesh files hold oriented unit voxel-face quads `[x,y,z,axis,sign]` by root; they
are an explicit small-volume surface representation, not a Neuroglancer mesh
format. Root meshes stay identical under atom refinement because root membership
is inherited. Export stages resolved atom labels, the same-version snapshot
and mesh quads in a sibling temporary directory, then fsyncs and atomically
renames it with `RENAME_NOREPLACE` to the requested new output directory. A
competing output directory is rejected even if it is empty. An interrupted export can
leave a sibling `.tmp-cave-pkg-*` directory for inspection, but never a partial
final export; it is separate from store recovery. All snapshots mark `offline_prototype: true` and
`native_history_consistent: false`.

## Native integration gate

Before a viewer can accept this as a live edit, implement and prove: safe native
ID reservation/counter reconciliation; a version-aware atomic-label lookup;
replacement atom insertion and adjacency/affinity inheritance; root locks and
operation logging; one recoverable publication across voxel and graph storage;
compensating graph and voxel undo; annotation ownership by position; mesh/cache
invalidation keyed by version; preservation of prior root history and replay;
and stale edit rejection across the native API. Existing graph history alone
cannot satisfy these requirements. No live/browser acceptance is claimed here.

## Verification

```bash
.venv/bin/python -m pytest tests/test_local_refinement.py -q
```

Tests cover seed/component partition, preview purity, exact voxel preservation,
uint64 chunk compatibility and upstream encoder parity, non-reused IDs after
undo, inherited root/annotation mapping, root mesh/export consistency, historical
reads, CAS races, four injected crash boundaries, corruption rejection,
compensating undo/redo and an end-to-end disposable CLI run. The full Docker,
browser and native deployment suites are separate and have not been run for
this offline increment.
