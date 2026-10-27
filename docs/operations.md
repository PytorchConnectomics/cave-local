# Looking after a local installation

Run commands from this repository root. `bin/cave` selects the repo's Python
environment; use it rather than a different global Python. Substitute actual
deployment and backup paths in the examples. Read `deployment.json` to confirm
the selected graph, URL and `project_name` before operating it.

The package accepts only projects named `cave-pkg-*`. Each has its own Compose
network and Bigtable, PostgreSQL and Redis volumes. Existing CAVE setups and
other applications on the machine are outside these commands' scope.

## Start, inspect and stop

```bash
bin/cave status /srv/cave/example
bin/cave start /srv/cave/example
bin/cave logs /srv/cave/example
bin/cave validate /srv/cave/example
bin/cave stop /srv/cave/example
```

`start` can be repeated on the same deployment. It preserves completed
ingestion and existing edits; changed dataset metadata requires a new
installation. `validate` without `--edit-fixture` reads the graph, annotations
and materializations and writes `data/snapshot.json`. It does not add test
edits. `validate --edit-fixture --browser` deliberately adds acceptance edits
and annotations and is restricted to generated, disposable fixtures.

For a restart persistence check, save snapshots outside the deployment:

```bash
.venv/bin/python scripts/snapshot.py /srv/cave/example --output /srv/cave/check-before.json
bin/cave stop /srv/cave/example
bin/cave start /srv/cave/example
.venv/bin/python scripts/snapshot.py /srv/cave/example --output /srv/cave/check-after.json
cmp /srv/cave/check-before.json /srv/cave/check-after.json
```

An unchanged snapshot means all recorded atom-to-root mappings, root leaf
memberships, annotations, materialization versions and recorded edit details
match. Pause editing and background materialization requests for this check.

## Make a backup and check it

Choose a new backup directory outside the deployment. Keep the installation
free of concurrent edits and materialization requests during the backup.

```bash
bin/cave backup /srv/cave/example /srv/cave-backups/example-001 --include-source
bin/cave verify-backup /srv/cave-backups/example-001
```

The backup stops this deployment's running services, copies its three
persistent volumes and deployment files, then restarts exactly the services
that were running before. It checks graph, edit and annotation snapshots
before and after. A copy failure still triggers restart; an incomplete copy
has no completed `manifest.json` and must not be treated as a backup.

`--include-source` also copies the read-only local input folder. For large
datasets, omit it and retain that unchanged folder separately. A restore then
needs `--source-dir`. HTTP(S) sources must remain reachable and unchanged;
their contents are not downloaded by this backup. The copy includes `.env`
and cloud-volume secrets if present, so treat it as confidential. Its
directory is mode `0700`, files are mode `0600`; do not commit or upload it to
the public repo.

An already stopped installation can be copied with `--offline`. That provides
a consistent volume copy and checksum checks but has no before/after API
comparison. Its report states `state_checked: false`. Do not describe that as
a proven graph-and-annotation recovery.

`verify-backup` reads every file, checks SHA-256 hashes, and rejects missing
files, unexpected files and archive paths that escape extraction. Refined
deployments retain their atom-table generations and history: backup/restore
preserves the known relative `data/atom_tables/current` pointer and three
legacy array aliases, with targets checked inside `data/`. Other links,
missing link targets and archive entries beneath a link are rejected.
Checksums detect accidental corruption; they do not establish who supplied a
backup. Use backups from a trusted operator.

## Restore to a separate copy

Install dependencies and build the repo version recorded in the backup before
restoring. Restore refuses a different PostgreSQL image pin; database-image
changes require a separately tested migration. Choose a new directory and a
free loopback port:

```bash
bin/cave restore /srv/cave-backups/example-001 /srv/cave/example-restored --port 8182
```

When the backup excludes local input data, add its unchanged folder:

```bash
bin/cave restore /srv/cave-backups/example-001 /srv/cave/example-restored --port 8182 --source-dir /srv/data/example
```

Restore verifies the archive before creating anything. It generates fresh
configuration, uses a separate project and new volumes, preserves the graph,
datastack and Bigtable namespace, and restores database credentials before
starting PostgreSQL. Included input data is placed in a new sibling folder
named `example-restored-source`. The original installation can remain running.

Scripts, configuration and viewer files are generated from the repository
running `restore`; archived copies are kept privately under
`provenance/archived-runtime` for reference. They do not overwrite candidate
code. `restore-report.json` records the effective repository revision and
hashes of both runtimes. Supported configuration is expressed in the manifest.
Hand-edited scripts, gateway settings or viewer files are outside automatic
migration: review the retained copies and represent needed changes in the
candidate before relying on it.

The restore checks all saved graph/annotation/edit observations and writes
`restore-report.json`. A successful online restore reports
`state_compared_to_backup: true`. Open its new URL and check image/segmentation
alignment in the browser. An offline backup can be restored and observed, but
the script cannot compare it with an absent original snapshot.

Restore keeps the original graph identity. `--graph-id` is an optional assertion
of that identity, not a request to rename the graph. A different segmentation
gets a new installation; carrying old proofreading edits into it is not
supported automatically.

If restore fails, its separate files and volumes remain for inspection. Use
`status` and `logs` on that target. Do not redirect the original users to it
until its checks pass. To abandon the failed copy, remove only its confirmed
project as described below; its source folder and backups remain.

## Update with a working way back

Supported update procedure: test a candidate repository version on a restored
copy before directing users to it. Automatic database schema migration and
changing the PostgreSQL major version are not supported by this release.

1. Stop new editing requests, record the current commit, and make a verified
   backup with included input or a retained unchanged input folder.
2. Clone the candidate into a separate repository directory. Run its
   `bin/cave deps`, `bin/cave fetch` and `bin/cave build`.
3. Use that candidate's `bin/cave restore` to create a new deployment on a new
   port. Require its saved-state comparison and browser checks to pass. Check
   the effective revision/script hashes and review runtime differences in
   `restore-report.json`; archived adapters are reference copies only.
4. If its image pins or release notes require a database migration, stop the
   update here until there is a documented and tested migration. Do not point
   a new database version at old volumes to try it.
5. Once the candidate is proved and the user wants it, direct access to the
   new copy and allow editing there. Keep the original stopped deployment and
   verified backup for recovery.

Before any edits are made on the candidate, recovery is `bin/cave start` on
the original and returning access to its URL. After new edits, take a new
backup and plan compatible recovery; starting the old copy alone would lose
those new edits. No automatic merging of divergent editing histories is
promised.

## Refresh the generated credentials

Pause editing/materialization requests and keep a verified backup, then run:

```bash
bin/cave rotate-secrets /srv/cave/example
```

This rotates the PostgreSQL role password and `CAVE_SESSION_SECRET`. It stops
only this deployment's running application services, sends password SQL over
captured stdin, writes a new private `.env` atomically, and recreates those
applications with the new environment. Its databases remain running. It
compares the complete graph/edit/annotation snapshot before and after and
writes `rotation-report.json`; require `rotated: true` and
`state_unchanged: true`.

The original `.env` is retained in a mode `0600` recovery file under `secrets/`.
Do not print that file or attach it to a report. If any step fails, the command
tries to restore the old database password, environment and application
services, then verifies the old state. Its failure report states whether that
recovery was proved. If recovery could not be verified, stop further changes
and restore the verified backup into a separate target. This does not enable
web authentication; the local stack remains accessible through loopback/SSH.

## Access from another computer

The stack has authentication disabled and binds its gateway to loopback.
Keep that binding and use an SSH tunnel from the client computer:

```bash
ssh -N -L 8181:127.0.0.1:8181 USER@SERVER
```

Open `http://127.0.0.1:8181` on the client. Both port numbers should match the
deployment's `local_url`; the viewer contains that URL. If the client port is
occupied, use a deployment configured with another port, or arrange another
local tunnel address that preserves the configured URL. Do not expose this
authentication-disabled stack through an internet tunnel or a public bind as
a routine setup step. Public access with authentication needs its own tested
deployment configuration.

## Remove a specifically identified installation

Back up wanted edits first, read the exact `project_name` in
`deployment.json`, then supply it explicitly:

```bash
bin/cave teardown /srv/cave/example-restored --confirm cave-pkg-EXACT-PROJECT-NAME
```

This deletes only that project's containers, network and persistent graph,
annotation and Redis volumes. It checks ownership labels before deletion.
Deployment files, source data, backups and other applications remain. Do not
use global Docker pruning to clean up this project.

## On-demand supervoxel refinement (opt-in)

An `agglomerated_supervoxels` deployment can refine an atom when split seeds
share it, or after a native mincut fails. It first divides that atom on a
volume-aligned grid, then uses a seeded distance-transform watershed if opposing
seeds still share a piece. The calculation uses the current ABISS-derived labels
and region-graph edges; it needs no affinity volume. Refinement preserves the
object and its root. The subsequent native multicut changes the object and is
undoable. Refinement itself is permanent.

The manifest option is off by default. Grid dimensions are XYZ voxel counts,
positive integers that divide `graph_chunk_xyz`; the default is `[16,16,16]`.
Limits bound the number of refined atoms and time spent starting refinements in
one split request. An in-progress publication must finish or enter recovery.

```json
"supervoxel_split": {
  "enabled": true,
  "grid_xyz": [16, 16, 16],
  "max_atoms": 64,
  "max_seconds": 30
}
```

This option is deployment configuration, outside the ingestion data fingerprint.
Do not enable it on a proofreading volume as part of fixture acceptance. Enabling
cereb32x2 requires DW's approval; cereb32x1/v2 remains unchanged.

Use a fresh disposable installation to exercise the feature. Choose an unused
loopback port; this example's small graph chunks use an explicit `[4,4,4]` grid:

```bash
bin/cave fixture .runtime/svsplit-input --atom-policy agglomerated_supervoxels \
  --supervoxel-split --graph-id pkg_svsplit --port 18281
bin/cave create .runtime/svsplit-input/manifest.json .runtime/svsplit \
  --source-dir .runtime/svsplit-input
bin/cave start .runtime/svsplit
bin/cave validate .runtime/svsplit --edit-fixture
```

The dedicated verifier records its actual case results and failures in
`DEPLOYMENT/data/supervoxel_splits/verification/report-<run_id>.json`, with probe
logs beside it. Cases cover refinement, historical exports, undo/redo, bounded
retries, publication crashes, concurrent edits/readers, and different-root
neighbours across chunk faces. Pending cases are not passes. Fixture
fault injection is enabled only on generated `test_fixture` installations; a
`CAVE_SVSPLIT_FAULT` environment value is ignored on other deployments. Tests
must never edit a real proofreading graph or reuse another test's volumes.

A point-seeded split retries the native multicut at most once after a recognized failure.
`X-Cave-Supervoxels-Refined: N` reports the number of refined atoms, including when
the retry fails. Same-source-label seeds in different atoms are allowed when the
switch is enabled. When disabled, the existing indivisible-source rejection
message remains in effect. Disabling new refinements keeps history translation,
publication barriers and recovery active for an installation with refinement
data. Identical opposing seed voxels are rejected.

Each deployment serializes refinements with `data/supervoxel_splits/.lock`.
Before publication it also locks **every affected root**, including a neighbour
across a chunk face that a previous native split put in a different root. The
journal records the complete sorted root set and level-2 preconditions. After
raising the fence, the adapter checks latest lock ownership, current roots and
operation timestamps again before publishing. A native edit checks the journal
fence after acquiring its own root locks. An expired historical lock cell is not
accepted as proof of current ownership.

The retained files are:

- `data/supervoxel_splits/journal/<operation_id>/journal.json` and replay payloads:
  `prepared`, `fenced`, `publishing`, `published`, `verified`, then `done`;
  failures can be `aborted` before publication or `verify_failed` afterward.
- `data/supervoxel_splits/log.jsonl`: one committed record per refinement.
- `data/supervoxel_splits/epoch`: odd during publication, even when stable.
- `data/atom_tables/current`: one atomic generation containing row-aligned,
  sorted `atom_index.npy`, `atom_bounds.npy`, `initial_roots.npy`, plus sparse
  `lineage.npy` and `edge_map.npy`. Legacy file paths point into this generation.

Publication errors attempt recovery before releasing the mutation lock. Worker
startup and the next refinement also recover interrupted publications from
retained payloads. If an odd epoch remains with no active publisher, public
readers, local meshing and snapshots trigger recovery using a nonblocking
mutation-lock attempt. A failed reader recovery starts a ten-second cooldown
for that operation, shared across workers; reads return 503 during the cooldown
without constructing a graph or retrying replay. The next read after the
cooldown can retry. Explicit repair and new refinements bypass this cooldown.
Errors before publication abort the preparation and remove its fence. Abort
persists the state and archives the preparation separately, so either can lift
the fence if the other fails; cleanup failures remain attached to the original
error. Persistent failure of both filesystem actions still requires recovery.
Publication and recovery also remove only recognized temporary atom-table
symlinks left by an interrupted pointer replacement, so those links cannot
prevent a subsequent portable backup.
`verified` is the commit point: recovery only finalizes its log
and clears its fence, without replaying or re-verifying. A failed verification
keeps all affected roots fenced until explicit repair succeeds:

```bash
bin/cave svsplit repair DEPLOYMENT
```

Repair remains available after disabling new refinements when journals or atom-table
generations are retained.

New refinements are refused until that repair, including on other roots, so they cannot
overwrite a shared atomic chunk needed to verify the retained payload.

Keep the journal and payloads when investigating a 503; never clear the fence or
edit/delete its files by hand. Repair takes the same deployment mutation lock
and all journal roots, then verifies before clearing the fence. It does not
re-ingest the graph or modify source data.

Timestamped root queries map a newer atom through its lineage to an ancestor
that existed at that time. Undo/redo translate retired edge endpoints through
the edge map; the translated ids are checked again under native locks, with a
bounded retry if a refinement intervened. The shared factory interception runs
before native undo's edge-existence checks. Root-id coordinate lookups use the
same lineage mapping internally. Dense tables retain retired rows for history;
current snapshots count current leaves only.

PCG reads, local meshes and repository snapshots use the publication epoch to
reject mixed hierarchy/voxel/table observations. A publication that lasts beyond
the read bound returns 503 with `Retry-After: 1`. PCG responses include
`X-Cave-Svsplit-Epoch`. An existing mesh remains valid because refinement alone
changes neither the root nor its voxel set. Plain Spelunker may need a reload to
fetch changed segmentation chunks.

Follow-ups in **cave-seg-bio** remain outside this repository: permit same-fragment
requests when enabled, reload chunks after the refinement header, and validate
the epoch around leaves/voxel/export reads. Its export reader should resolve the
atom-table generation once. Static gateway atomic-volume requests have no hard
publication barrier; external clients must validate epochs. `export_preview.py`
and `export_release.py` export package source, not segmentation data, and do not
read atom tables or atomic voxels.

## Painted cuts (opt-in)

Painted cuts use two labels: source keeps the painted side and sink cuts it
off. Enable both `supervoxel_split.enabled` and
`supervoxel_split.paint.enabled` on an `agglomerated_supervoxels` deployment.
Both switches are off by default. This run does not enable any real volume.

For disposable acceptance choose a fresh directory, project and unused
loopback port:

```bash
bin/cave fixture .runtime/paint-input --paint --graph-id pkg_paint --port 18282
bin/cave create .runtime/paint-input/manifest.json .runtime/paint \
  --source-dir .runtime/paint-input --project-name cave-pkg-paint-example
bin/cave start .runtime/paint
bin/cave validate .runtime/paint --edit-fixture
```

`fixture --paint` selects `agglomerated_supervoxels`, enables refinement and
paint, preserves the existing svsplit targets, and adds independent neck,
multi-atom, seam, bypass, distant-contact and fake-merge targets. Their source
labels and XYZ voxel coordinates are recorded in `expected.json.paint_geometry`.
`create --paint` enables the default paint limits only for a generated
agglomerated fixture; a supplied dataset must declare its intended settings in
its own manifest. Create never changes the input manifest or source arrays.

### Split request and client contract

Send `POST /segmentation/api/v1/table/<table>/split` with a JSON body:

```json
{
  "paint": {
    "version": 1,
    "sources": [{"points_nm": [[100, 100, 200], [150, 100, 200]], "radius_nm": 40}],
    "sinks": [{"points_nm": [[250, 100, 200]], "radius_nm": 40}]
  }
}
```

Coordinates are XYZ physical nanometers. Each stroke contains one or more
points and a positive radius; one point paints a sphere. Longer strokes paint
capsules around each consecutive segment, using voxel centres and the actual
anisotropic resolution. A radius wider than one slice paints adjacent slices.
Voxel-index inputs should be converted using `(index + 0.5) * resolution_nm_xyz`.
Only `points_nm` is accepted by this version. Top-level `sources` and `sinks`
must be absent or empty when `paint` is present. Painted requests always use
mincut; `mincut=false` is refused.

Background paint is discarded. Both labels must retain foreground voxels on
one current object, and the retained label masks must not overlap. The server
refines only current atoms containing both paint colours, using every painted
voxel in each atom as a watershed seed. It then remaps all painted voxels and
passes every touched current atom to one native multicut operation. The paint
path does not grid-refine seam pieces or retry the point-seeded fallback.

A successful response keeps the native split body and adds:

| Header | Meaning |
|---|---|
| `X-Cave-Supervoxels-Refined` | Number of atoms refined, including zero. |
| `X-Cave-Paint-Voxels` | Retained source and sink counts, comma-separated. |
| `X-Cave-Paint-Record` | Native operation id associated with durable provenance. |
| `X-Cave-Paint-Elapsed-Seconds` | Total request duration in seconds, including persistence that finishes after the deadline. |

A refusal is HTTP 400 with the following shape:

```json
{"error": "The cut cannot be certified", "paint_refusal": {
  "kind": "not_certified", "locations_nm": [[100, 200, 300]], "supervoxels": []
}}
```

`kind` is one of `not_separated`, `not_certified`, `stray_cut`, `seam`,
`other_object`, `empty_label`, `overlap`, `limit`, `deadline`, `object_changed`
or `disabled`. `locations_nm` contains at most 20 XYZ physical locations for
the client to display. A seam refusal asks the user to paint both colours
inside the same supervoxel on one side of the chunk boundary.

An error after the durable handoff may also include `paint_record_state` in
the JSON response. This diagnostic reports the provenance state, native log
status, affected roots, indefinite-lock owners and the observed painted-atom
partition compared with the pre-cut and expected partitions. A failed graph
read leaves the comparison unknown and reports its error. Equal partitions do
not prove that no graph rows changed: an interrupted multi-row write can retain
partial records. Preserve the record and use the recovery procedure below.
Native write failures keep their native error response; they are not successful
cuts or ordinary pre-cut refusals.

Before applying a cut, the service checks all source/sink components under
native root locks. A closed-side certificate proves separation in the whole
object; if needed, it tries one doubled, bounded solve box. It refuses a cut
that remains uncertified. Locality uses actual face-contact voxels, not nearby
atom bounding boxes. For a fake edge, locality needs the creating merge's
recorded coordinates; unresolved provenance is refused. A refusal applies no
native cut. Refinements already published remain, preserving the object's
voxel set and root. Undo/redo applies to the native cut and retains refinement.

Spelunker itself has no new paint brush in this package. The separate
cave-seg-bio client must record annotation polylines with radius, submit this
body, pass painted requests through its gate without a same-fragment
pre-rejection on enabled volumes, and show refusal locations. Brush controls,
label/radius controls and a Cut button are outside this repository change.

### Paint limits

`supervoxel_split.paint` accepts only the following settings. All numeric
limits must be finite and positive; count limits and box dimensions are
integers. The parent `supervoxel_split.max_atoms` bounds refinements.

| Setting | Default | Unit or scope |
|---|---|---|
| `enabled` | `false` | Paint gate; requires the parent gate too. |
| `max_radius_nm` | 500 | Stroke radius in nm. |
| `max_stroke_points` | 256 | Points per stroke. |
| `max_paint_voxels` | 200000 | Painted voxel count. |
| `max_box_voxels` | `[512,512,128]` | XYZ box dimensions in voxels. |
| `max_cut_distance_nm` | 1000 | Distance from paint to an actual removed-edge contact. |
| `max_painted_atoms` | 256 | Distinct current atoms under both labels combined. |
| `max_removed_edges` | 2000 | Edges checked before applying a cut. |
| `max_contact_voxels` | 2000000 | Total contact-crop voxels read. |
| `max_seconds` | 60 | One request-wide deadline in seconds. |
| `max_request_bytes` | 262144 | Raw request body bytes. |
| `max_strokes` | 128 | Strokes across both labels. |
| `max_total_points` | 2048 | Points across all strokes. |
| `max_raster_work` | 4000000 | Sum of padded segment-box voxel counts, including overlaps. |

Raster work is checked before allocation and processed in batches of at most
65536 voxels. Graph reads, refinement, remapping, solving and checking share
the request deadline. The persistence gate checks immediately before the
first `mutate_rows` of native `_write`, after indefinite-root-lock acquisition
and client lock renewal; the earlier `CREATED` log is outside this gate.
No cut persistence starts after the deadline. Once persistence starts it may
finish after the deadline; successful completion reports success. The native
solver cannot be interrupted, so a late result is discarded before writing.

### Paint provenance and recovery

The service durably saves the paint exactly as sent, user id, start time,
paint counts, terminals and refinement ids under
`data/supervoxel_splits/paint/pending/<request_id>.json` before mutation. The
record is bound to the native operation id before `_write`, then marked
`handed_to_write`. Successful records are finalized as
`paint/<operation_id>.json`. Refused requests without a handoff marker are
retained under `paint/refused/`. These regular files are included in the
existing backup/restore workflow.

Worker/request recovery resolves pending records only by their bound operation
id. A completed PCG operation finalizes its record. `WRITE_STARTED` remains
`awaiting_pcg_repair`; a handoff marker with `CREATED`, `EXCEPTION` or no log
remains `ambiguous`, since multi-row writes are not atomic. An unbound record,
or one without a handoff marker, can become `not_applied` after its deadline
and lock lifetime. Never infer that no graph rows changed merely from a
`CREATED` log when the handoff marker exists.

```bash
bin/cave svsplit repair DEPLOYMENT
bin/cave svsplit repair DEPLOYMENT --pcg-repair OPERATION_ID
```

The first command recovers refinement state and reconciles/reports paint
records, including roots, indefinite locks and the observed partition of
painted atoms. The second explicitly invokes native PCG repair only for one
uniquely bound, handed-off operation whose native status is `CREATED`,
`EXCEPTION` or `WRITE_STARTED`, then reconciles its record. Its native log must
reconstruct the recorded paint terminals, and its journal must retain the exact
validated voxel box and removed edges; repair reuses that box and the original
native edit time. The request's start time plus deadline plus root-lock lifetime
must have passed before any replay. `CREATED` and `EXCEPTION` repairs require
every recorded root to remain current. The command locks those exact roots,
rechecks them under the locks, and refuses a root held by another operation;
it never follows a root into a newer lineage. `WRITE_STARTED` repair requires
every recorded root's indefinite lock to still belong to the selected operation.
Select a confirmed interrupted operation reported as `awaiting_pcg_repair` or
`ambiguous`, not an operation still being written by a live request.

Explicit repair recomputes and finishes the cut. For an `ambiguous` record,
this can apply a cut even if no graph rows were previously written; inspect
the retained record and graph state before selecting that action. The recomputed
removed edges must equal the previously validated cut before native edge removal,
and the successful native log must report that same edge set before locks are
released or provenance becomes `completed`. A post-repair mismatch retains the
indefinite locks and the record as `ambiguous`, even if native status is `SUCCESS`.

Missing native logs, unbound records and records without a handoff marker cannot
be replayed by this command. Neither can a `CREATED`/`EXCEPTION` record whose
partial write or a subsequent edit superseded a recorded root, nor a
`WRITE_STARTED` record without all its own indefinite locks. Preserve these
refusals for investigation; do not bypass them by changing provenance or locks.
Ordinary recovery never invokes native repair and does not discard either
ambiguous or awaiting-repair records. Young unsuccessful requests remain pending until
their deadline plus root-lock lifetime, unless the owning request has already
reported its error. A confirmed native `SUCCESS` can be finalized immediately.
Repair remains available after the
feature is disabled if provenance is retained. Do not delete pending records
or manually clear native locks.
