# API and CAVE integration

This is an interface design, not an implemented API. Retain native CAVE services behind a small coordinator; do not automate the browser as the primary execution path.

## Current implementation boundary

Baseline source: `29645eb79a9f946c09eaab38a17c8ea0858fd41d`, [repository](https://github.com/PytorchConnectomics/cave-local). Local files below are relative to the repository root.

| Component | Existing evidence | Reuse and missing work |
|---|---|---|
| Input contract and ingestion | `scripts/segmentation_metadata.py`, `scripts/source_label_policy.py`, `scripts/ingest_segmentation_iteration.py`; `docs/datasets.md` | Aligned image/label sources, explicit axes/spacing; retain immutable originals and atom mapping |
| Graph reads and edit calls | `scripts/verify_segmentation_iteration.py`, `scripts/verify_iteration_persistence.py` | Reuse native merge/split/undo and root resolution; build typed client and exact-effect verification |
| Viewer and mesh adapter | `scripts/local_mesh.py`, `scripts/configure_iteration_viewer.py`, `docs/viewer.md` | Human inspection and graph-derived visualization exist; reproducible VLM evidence renderer is new |
| Restore and persistence | `scripts/snapshot.py`, `scripts/operations.py`, `docs/operations.md` | Use isolated disposable copies and operation receipts; backup restore is not per-edit rollback |
| Reviewed exports and accounts | Sibling `coding/cave-seg-bio/export.py`, `README.md`, revision `dc679dfde9126e8e2b89958689ef3e182ba16dc7` | Timestamped labels, atom map and edit log exist; labels must be reconstructed using status/undo lineage |
| Robust multi-piece split | `coding/PyChunkedGraph` fork and `.agent/split_multicut/task.md` | Recorded separate implementation; native preview does not use fallback, so no preview/commit equivalence yet |
| Split inside one atom | `.agent/split/task.md` and plans | Separate refinement proposal, not a supported automatic action here |

Current README envelope: a single server, shared native editor identity, loopback gateway, source images OME-Zarr v2 (three spatial axes, zero origin) and precomputed labels. Large-root meshes are capped at 200 million voxels. Do not assume arbitrary deployment size or production multi-user isolation from this package.

Local wrappers and fixtures are evidence of code paths, not evidence that the current live deployment passes all desired contracts. Record the running image digest, PCG commit, source/atom generation and graph ID during future preflight; a stale `deployment.json` build label alone is insufficient.

### Concrete native calls to adapt

Prefix below: `/segmentation/api/v1/table/{graph}`. Contracts were traced in the local PCG fork `26c7c74f87a7d5af125970c1e5a623e0c19de425` and CAVE fixture callers. The fork integration worktree is `6844daac0595a87153bb757a8624399074349228`; it is not the main inspected checkout's Docker build.

| Native endpoint | Input/output and source |
|---|---|
| `POST /roots?int64_as_str=true&timestamp={epoch}` | `node_ids` strings → `root_ids` strings; `scripts/snapshot.py:34`, PCG `app/segmentation/common.py:240` |
| `POST /merge` | Two `[id,x_nm,y_nm,z_nm]` rows → operation/new roots/L2 IDs; fixture `scripts/verify_segmentation_iteration.py:148`, PCG `common.py:348` |
| `POST /split` | `sources`/`sinks` arrays of those physical XYZ rows; PCG `common.py:411` |
| `POST /undo`, `/redo` | `operation_id` → newly assigned roots; PCG `common.py:478,511` |
| `GET /node/{root}/leaves`, `/node/{root}/subgraph` | Leaves or edge pairs/affinities/areas; PCG `v1/routes.py:290,343` |
| `POST /graph/split_preview` | Seed arrays → components and `illegal_split`; PCG `common.py:1031`; fork limitation below |

Ingested `region_adjacency.npy` and aligned `region_affinity.npy` include joined and unjoined atom contacts (`scripts/ingest_segmentation_iteration.py:392`). `atom_index.npy` is sorted uint64 Nx5 `[atom,source_label,x,y,z]`; `atom_bounds.npy` is aligned Nx6 XYZ bounds (`:335`). These are useful candidate primitives, not calibrated biological probabilities.

Two visibility pitfalls: `scripts/local_mesh.py` closes mesh surfaces, so apparent closure at a crop boundary is not raw-image evidence; `scripts/snapshot.py` assumes writers are paused and does not give all its reads one timestamp. The new snapshot API must enforce consistency rather than wrap this helper unchanged. Sibling gate account attribution, done-item protection and any seed rewriting must remain explicit in the adapter; direct native requests must not bypass existing review policies.

## Two adapters

`CaveAdapter` owns images, graph reads, candidate anchors, simulation, commits, receipts and history. `VisionAdapter` owns image payloads, model invocation, schema parsing, usage and latency. Both are versioned; neither exposes graph credentials to a model. Start the coordinator as a local Python library/CLI with the contracts below, then expose HTTP for independent workers. No new distributed framework is needed initially.

All IDs that may be uint64 travel as **decimal strings** in JSON. All coordinates declare axis order, space and units. Separate image snapshot, atom generation, mutable graph state, rule version and model revision; a root ID alone is not a version.

Each proposal/certificate binds a dependency fingerprint: graph state, atom basis, active claim and protected-constraint versions, policy, evidence hashes, renderer, and checker version. Recheck the entire fingerprint at the commit boundary. A human may retract a premise without changing the graph; such a certificate is stale even if all root IDs still match. Superseding a premise invalidates every dependent obligation and proposal.

## Proposed public service contract

| Method and proposed route | Request | Response/semantics |
|---|---|---|
| `GET /auto/v1/capabilities` | Deployment identifier | Native/fork revision, supported edit kinds, exact simulation support, concurrency mode and limits |
| `POST /auto/v1/snapshots` | Deployment, optional historical timestamp | Immutable image/segmentation/atom references, transform and graph-state token; no graph edit |
| `POST /auto/v1/candidates` | Snapshot, ROI/roots, task kind, bounded budget | Async candidate batch, discovery version and coverage counts |
| `POST /auto/v1/evidence` | Candidate, requested view specification | Content-addressed images, geometry, raw source references, render transform and hashes |
| `POST /auto/v1/proposals` | Candidate/evidence IDs, model/rule result, anchors | Validated proposal ID; no edit; schema errors never coerce to an action |
| `POST /auto/v1/proposals/{id}/check` | Proposal, kernel/policy version | Structural certificate, explicit empirical claims, unresolved obligations or rejection |
| `POST /auto/v1/proposals/{id}/simulate` | Proposal, expected state | Exact edge delta and affected partition digest, or `unsupported_exact_preview` |
| `POST /auto/v1/proposals/{id}/commit` | Certificate, expected state, simulation digest, idempotency key, execution policy | Operation ID/receipt or explicit conflict; initially disposable copies only |
| `GET /auto/v1/operations/{id}` | Operation ID | Pending/applied/failed/unknown state, before/after roots, actual delta, audit and verification status |
| `POST /auto/v1/operations/{id}/undo` | Expected current state, dependency review, idempotency key | Native undo receipt after compatibility checks; do not promise exact byte restoration |
| `POST /auto/v1/reviews` | Proposal, accept/reject/uncertain, reason and evidence | Append-only adjudication; never implicitly executes the edit |

Async jobs expose status and cancellation of *pending work*. Cancellation after a graph edit starts requires reconciliation, not an assumption that the edit did not happen.

State machine: `discovered → evidenced → proposed → checked → simulated → eligible → applying → applied → postchecked`. Side exits: `no_change`, `needs_review`, `unsupported`, `stale`, `failed`, `unknown_commit`. Only the coordinator advances `eligible`; a VLM cannot set this status.

## Example proposal (illustrative IDs, not a real operation)

```json
{
  "schema_version": "auto.proposal/1",
  "candidate_id": "candidate-example-1",
  "snapshot_id": "snapshot-example-1",
  "graph_id": "fixture_graph",
  "atom_generation": "fixture-atoms-v1",
  "expected_state": "opaque-server-issued-token",
  "action": "split",
  "source_anchors": ["anchor-A"],
  "sink_anchors": ["anchor-B", "anchor-C"],
  "evidence_ids": ["raw-xyz-1", "overlay-xyz-1", "skeleton-1"],
  "claims": [{
    "predicate": "visual_boundary_support",
    "value": "likely",
    "evidence_ids": ["raw-xyz-1"],
    "authority": "model_observation"
  }],
  "constraints_version": "fixture-constraints-v1",
  "proposer": {"model": "Qwen/Qwen3.8-Flash-Next", "prompt_version": "pilot-v1"}
}
```

The server resolves `anchor-A` to an atom plus coordinate, e.g. `{space: "image_voxel", axes: "xyz", unit: "voxel", value: [x,y,z]}`, using a named transform. It rejects stale/ambiguous mappings. A certificate is generated by the checker, never accepted as a model-written assertion. Keep observation confidence separate from `structural_checks_passed`.

## Native write semantics: the main implementation risk

The native API already supports graph edits, but the desired caller expected-state and durable idempotency semantics are not established. An external `get_roots()` followed by a write has a race even if native root locks serialize writes: another editor may change the graph between those calls.

For the offline prototype, use one writer on an isolated copy. For eventual shared deployment, either route **every** writer, including the human viewer and claim/policy editors, through one enforced coordinator, or implement expected-state checks under native edit locks with coordinated claim-version validation. Merely adding a mutex to the agent process is insufficient. For the first implementation prefer a deployment-wide serialized edit lane; optimize to component locks only when the dependency model is demonstrated.

Exact simulation must use the same cut implementation, parameters, boundary context and edge data as commit. The inspected robust fork disables its fallback during preview. Therefore current native preview must not issue an auto-commit certificate. Options are to share the exact planner between preview and commit or commit a checked explicit edge delta with all native preconditions; both need fixture validation. Never rerun a potentially different optimizer after certifying one result.

Reserve an idempotency key with the intent before writing. Bind it to the proposal and expected state; conflicting reuse is rejected. Ideally the native operation ID is atomically linked to the key. Until that guarantee exists, a timeout after write is `unknown_commit`: reconcile native operation logs and graph state; never blindly retry. Hash matching alone may be ambiguous after concurrent writes.

After success, resolve anchors again, verify all declared postconditions and compare actual vs certified partitions. Mesh regeneration is an observable downstream stage; stale meshes cannot validate graph correctness. Failed postcheck stops the queue and preserves evidence. Undo is a new conditional operation, and dependent later edits may prevent safe reversal. Snapshot restore happens only on the assigned disposable copy.

## Failure categories and tests to implement

Use explicit errors for `stale_state`, `same_supervoxel`, `uncuttable_seam`, `incomplete_graph`, `unsupported_exact_preview`, `constraint_conflict`, `invalid_coordinates`, `model_schema_error`, `budget_exhausted`, and `unknown_commit`. A representation failure must not trigger repeated model calls.

Meaningful contract fixtures: uint64 round trip; XYZ/ZYX and non-isotropic scale; same-atom refusal; multi-piece split; infinite seam edges; external path leaving crop; conflicting protected anchors; stale simulation after human edit; duplicate idempotency key; crash before/after native write; postcheck mismatch; undo after dependent edit; cache invalidation after refinement. Existing CAVE full-stack fixture tests must still pass when implementation changes land. No implementation tests were run for this documentation-only proposal.
