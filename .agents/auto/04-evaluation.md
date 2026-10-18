# Cerebellum evaluation protocol

Status: proposed protocol plus read-only artifact inspection. No model inference, graph correction, training or benchmark scoring was performed in this planning task.

## Primary reference and supporting snapshots

DW supplied the newer x1 reference during this session:

- Bundle: `/data/runs/msi-liconn-moe20um-2608/results/gt_cereb32x1_mt0700/20261017T032104Z/`
- Corrected labels: `export/seg.h5`
- Review: `review.json`
- Skeletons: `skeleton_nml_v2/`

Both supplied blocks were the same x1 path. No newer x2 reference was supplied. The newer bundle was inspected: all nine data files declared by its bundle, export and NML manifests matched hashes and sizes. Exact checks, HDF5 headers, XML counts and lineage are saved in [evidence-x1-20261017.json](evidence-x1-20261017.json). The older x1 artifacts below are supporting provenance, not the primary reference.

### Findings in the supplied x1 bundle

| Finding | Consequence for evaluation |
|---|---|
| 2,593 objects; 198 labeled; 134 IDs in supplied reviewed-axon list | Partial reviewed reference, not complete volume truth |
| All 42 roots in `suspicious axons 2026-10-16` with `todo` also occur in that 134-root cohort | The exporter chooses latest non-todo status. Preserve the supplied list and mark this pending subset; adjudicate or report separately |
| 96 operation rows: 89 SUCCESS, 6 CREATED, 1 EXCEPTION; successful attribution is 50 rebuild + 39 human-account operations | Neither 96 nor 89 equals the count of positive human correction labels |
| Graph export at `2026-10-17T03:21:04.925540Z`; point export roughly eight seconds later | Graph and semantic metadata are not one established atomic snapshot; check anchor-to-root consistency before use |
| Reviewed NML: 133 objects, 153 trees, 1,789 nodes, 1,636 edges | Object count differs from tree count; retain root identity across disconnected trees |
| Missing reviewed root `288230376151797134` has 22 voxels, below generator `min_voxels=200` | Record exclusion; do not silently call skeleton coverage complete |
| NML nodes are full-cube XYZ voxels, despite zero XML offset | Subtract XYZ `[320,320,365]` before sampling crop-local labels; use XYZ scale `[10.15625,10.15625,12.5]` nm |

The full NML has 345 annotation nodes; the reviewed subset has 341. The v2 generator chains grouped points in annotation-ID order and attaches chain ends to nearby same-object skeleton nodes up to 2,000 nm away. Stats record 96 attachment edges, two unattached ends and 96 unresolved points excluded. In reviewed NML, 377 edges touch annotation nodes (288 point–point, 89 point–skeleton); XML does not type every edge's construction provenance. These are **derived gap assumptions**, not solely image-supported tracing.

For strict ERL, reconstruct edge provenance and use segmentation-supported paths; evaluate curated identity constraints separately. A secondary gap-inclusive metric requires adjudication and its own fixed denominator. Do not silently change the user's NML or treat all inferred gap lengths as observed cable. NML topology checks found unique node IDs, valid endpoints, acyclic connected trees, coordinates within the crop after offset conversion, and point IDs present in the review. These checks establish representation validity, not biological correctness.

The exact generator is recoverable at research commit `d1e3dbf52ef055f5f14de56d8ab44c233797b321`, path `projects/msi-liconn-moe20um-2608/scripts/proof_skeleton_nml.py`; its SHA256 matches the recorded `a3cbbdecada6db08c5f6b1daef036c365d4141759fe21dc18e3f96f6ba51525f`. Current source has a later update. Configured simplification tolerance is 40 nm but recorded maximum removed-vertex deviation is 74.47 nm; do not promote the parameter to a guaranteed geometric error bound.

### Older supporting snapshots

An existing comparison config at [configs/cereb32.json](/home/donglai/dw-os/coding/focus/cereb-nerl/em_erl/configs/cereb32.json) identifies two older exports and review snapshots. The checksum inspection in [evidence.json](evidence.json) recomputed hashes of all six exported data files and both review files; all matched their declared hashes. This verifies file integrity relative to those manifests, not correctness of annotations.

| Field | Older x1 | Older x2 |
|---|---|---|
| Export folder suffix | `cereb32x1_u10_mt0700_v2/exports/20261015T141608Z` | `cereb32x2_u10_mt0700_crop/exports/20261015T141615Z` |
| Common parent | `/data/runs/msi-datasim-26/results/q5-cave/deployments/` | Same |
| Review basename under `gt_score_cereb32/preparation-20261015/` | `cereb32x1-u10-mt0700-v2.json` | `cereb32x2-u10-mt0700.json` |
| Crop shape ZYX | `[256,512,512]` | `[256,512,512]` |
| Export spacing ZYX nm | `[12.5,10.15625,10.15625]` | Same |
| Supervoxels | 268,522 | 163,725 |
| Operation rows | 75 | 169 |
| Native statuses | 71 SUCCESS, 3 CREATED, 1 EXCEPTION | 159 SUCCESS, 10 CREATED |
| Config preflight cohort, not recomputed | 112 axons; 30 nonaxon roots | 119 axons; 40 nonaxon roots |

Both names contain `ExPID99`; treat them as potentially dependent crops from one specimen until provenance establishes otherwise. The recorded prediction origins are ZYX `[365,320,320]` and `[400,320,320]`. Those offsets alone do not establish cross-volume overlap or independence.

The older comparison report prints “9 nm,” but the inspected original x1 decode/input provenance declares the same anisotropic spacing as the export and a model field `9`. Do not use the report label or model name as physical metadata. Existing evaluation validation checks extents/hashes, not full physical registration. Resolve transforms and expansion/native-tissue units explicitly for both crops. Never derive voxel spacing, optical resolution or magnification from `32x` in a name. Confirm modality and channels from source metadata; EM results cannot be assumed to transfer.

## Required data manifest

Before claiming end-to-end improvement, freeze:

| Required record | Why it matters |
|---|---|
| Raw image URI/hash, axes, units, origin, spacing and registration/expansion transforms | Models must see the same tissue as the edit graph and reference |
| Original pre-review segmentation and atom basis | Starting from already corrected roots invalidates improvement claims |
| Graph/image/source revision, native image digest and PCG fork revision | Replay must use the actual implementation |
| Final corrected labels, review status, skeleton/NML conversion and identity map | Separate reviewed truth, partial truth and unknown |
| Successful operation history, reversals, timestamps and migration maps | Reconstruct each pre-action state and effective human decisions |
| Candidate generation, rendering and reference hashes | Prevent hindsight and leakage |
| Spatial/neuron/specimen grouping and train/calibration/test assignment | Control correlated examples and cross-crop reuse |
| Cell classes, uncertainty flags, exclusions and sampled negatives | Avoid treating unreviewed tissue as correct |

**x1 baseline trap, confirmed:** v2 provenance starts from export `20261015T100654Z` after 97 logged operations. Its `initial_root` is initial *v2* state, not pre-human segmentation. The original input is located at `/data/runs/msi-datasim-26/results/q5-cave/inputs/cereb32x1_u10_mt0700_crop`; provenance points to `/data/runs/msi-liconn-moe20um-2608/results/cubes/ExPID99-32x_1_cerebellum/seg.h5:mt0700`, sibling `img_u8.zarr`, ABISS fragments and crop origin ZYX `[365,320,320]`. Those provenance files were inspected and hashed, but original voxel content and cross-basis partition reconstruction were not revalidated. That validation is the next gate. Using human-informed atom refinements can improve repairability in hindsight; report that condition separately. If reconstruction cannot be established, limit the experiment to curated decision cases.

Do not equate logged operations with correct labels. Resolve status and undo/redo references; remove failed/incomplete requests from positive labels, distinguish later corrections and refinement/migration actions, preserve uncertain cases, and compare effective graph states. Operation-ID attribution is stronger than approximate timestamp attribution. Equivalent valid edit sequences need not match the human action order.

## Dataset construction and leakage prevention

Proposed default: develop on x1; use x2 as a locked within-specimen test only after review coverage, physical overlap, shared neuron lineage and input-context buffers are resolved. If separation cannot be established, call it a separate-crop evaluation rather than held-out performance. Partition x1 training and calibration by neuron lineage where possible and spatial blocks with buffers otherwise. Exclude shared parent identities and duplicate locations across splits. With only two crops, report per-crop results and do not estimate broad population generalization from an artificial two-sample statistic.

Maintain two distinct evaluations:

1. **Oracle-location decision evaluation:** historical/curated error sites are supplied to every method. This measures perception and edit planning conditional on localization; identify it as such.
2. **End-to-end reconstruction evaluation:** candidates, anchor choices, camera orientations and context come only from the pre-review input. References score the resulting graph, never feed the candidate generator. Include random-site review to measure undiscovered errors.

Construct explicit `correct_merge`, `correct_split`, `no_change`, `ambiguous`, `outside_reviewed_scope` and `unrepairable_in_current_atoms` categories. An unchanged location is not automatically a negative. Adjudicate a blinded mixture of proposed changes, disagreements, unchanged hard negatives and random tissue; preserve unresolved examples rather than forcing consensus.

The 42 pending suspicious x1 roots cannot supply trusted negatives without further adjudication. Preserve their membership in the user's reference and report them as a separate stratum. Final labels/NML also cannot become operational must-link/cannot-link constraints: only pre-baseline knowledge or explicitly budgeted new human annotations may guide the agent.

For NML skeletons: inspect how trees, nodes, edges, identities, comments and gap links were generated. Do not interpolate missing edges merely to increase ERL. If a skeleton connects disconnected segments through curator-declared identity, separate identity constraints from image-supported cable. Fix the denominator and inclusion mask before scoring any method.

Freeze preprocessing, prompts, policy thresholds and rule versions before test access. Never use final masks to orient inputs; never retrieve test cases as few-shot examples. Permuting colors/IDs, hiding filenames/status, and removing overlays are useful leakage audits. Repeated tuning after test inspection requires a newly held-out test or an explicitly exploratory report.

## Experiments that isolate value

| Arm | Purpose |
|---|---|
| A: no edits | Reconstruction and human-work baseline |
| B: geometry/rule ranking plus deterministic edit planner | Value of morphology without a VLM |
| C: specialist CNN/ViT scorer using the same images/geometry | Strong cheap visual baseline informed by CB2/Autoproof |
| D: Qwen decision with bounded fixed views | Exact requested model's local competence and cost |
| E: Qwen + morphology claims/checker + selective extra views | Proposed system |
| F, optional: stronger teacher or the available lab app | Cost/quality reference, only if its interface/version is accessible |

All executing arms retain basic graph-integrity checks. To isolate the added morphology/policy checker, compare acceptance decisions offline against a version omitting those extra obligations, and count damaging edits it would have admitted. Do not remove data integrity controls from a real deployment for an ablation.

Use the same candidates for B–E decision comparisons, then give each a separately reported end-to-end run. The principal mandatory ablation is a 2×2: fixed/adaptive evidence acquisition × flat scoring/typed morphology obligations, at matched total budgets and with identical permitted human constraints. This separates better views from reusable reasoning. Additional ablations: raw images vs geometry vs both; direct Qwen vs locally adapted model; cached vs uncached; per-edit vs sequential rollout. Start with a small fixed replay to expose failures before any training request. A specialist may win; retain that result and restrict the VLM to the cases where it adds value.

For the speed/workload claim, run a randomized matched-task or crossover comparison of standard CAVE review and assisted review, using disjoint comparable tasks to avoid memory effects. Time inspection, decision, verification and recovery. Counterbalance ordering, record reviewer identity and stratify task difficulty. A reduction in candidate count alone does not demonstrate minutes saved.

## Metrics and uncertainty

Primary outcomes are **new damaging edits at a stated automation coverage** and **remaining human work at a stated reconstruction quality**. Define candidate automation coverage as automatically applied candidates divided by all technically actionable candidates from a fixed generator, counted before confidence or policy filtering. Report unrepairable candidates separately. Define error-repair coverage as independently verified corrected errors divided by all adjudicated errors in a predeclared reviewed region. Report repairable and all-error denominators separately; do not count multiple actions on one error as multiple recovered errors.

- Discovery: recall of reviewed errors, candidates per useful error, missed-error audit, and repairability ceiling imposed by current atoms.
- Decisions: precision/recall separately for merge and split actions, abstention/coverage, calibration and risk–coverage curves. Report class prevalence and uncertainty, not balanced accuracy alone.
- Execution: intended-vs-actual partition, introduced false joins/cuts, collateral cable affected, stale proposal rejection, ambiguous commits and undo outcomes.
- Reconstruction: split and merge VI separately only on a genuinely reviewed dense domain; ERL/nERL on fixed reviewed skeletons; retained correct cable; identity constraints and synaptic partners only where annotated. A lower total VI can hide a harmful join.
- Cost: end-to-end wall time including I/O, renderer, queue, model, retries, checker, edit and remeshing; p50/p95 latency; billed tokens and dollars; human adjudication and recovery minutes; useful correct corrections per dollar and per reviewer hour.

Reuse [tools/cave_compare.py](/home/donglai/dw-os/coding/focus/cereb-nerl/em_erl/tools/cave_compare.py) at `be1787acb4b334bbde203b23a2d4058b066139db` as a reference implementation to adapt, not an unexamined turnkey evaluator. It selects reviewed conflict-free axons, adds gap points without fabricated cable, holds the reference graph fixed, and uses strict merge tolerance zero. Its existing CLI expects particular prediction arms and its scoring run is restricted to an approved rdash allocation. This task did not run it. Its report's `merge_threshold=0.7` describes segmentation agglomeration, whereas ERL merge tolerance is zero; future schema must name these separately.

Bootstrap by reviewed neuron or spatial block, not individual neighboring pixels/edits. With zero errors among `n` approximately independent accepted edits, the one-sided 95% binomial upper error bound is `1 - 0.05^(1/n)` (approximately `3/n`). About 2,995 error-free independent cases would be needed to place it below 0.1%; correlated edits provide less evidence. Hundreds of log rows across these crops cannot justify a universal 99.9% correctness claim.

## Proposed decision gates, not measured achievements

1. **Data gate:** hashes, transforms, pre-review state, review coverage, operation semantics and split assignment are resolved. Otherwise limit conclusions to the specific available reference and task.
2. **Replay gate:** all graph invariants and lifecycle fixtures pass; every simulated effect matches the applied effect on disposable graphs. No unresolved commit outcomes or unchecked preview fallback.
3. **Recommendation gate:** in the timed study, improve useful-error yield per reviewer minute relative to the best cheap baseline, with explicit uncertainty and no loss hidden by excluding difficult cases. A provisional target is 2× yield; DW can revise it before the test is frozen.
4. **Bounded autonomy gate:** declare separate merge/split risk budgets, maximum affected cable/components and cohort scope. Provisional study targets are at least 99% observed accepted-edit precision and at least 20% error-repair coverage within the fixed adjudicated region, plus a one-sided 95% lower precision bound above 99% for each automated action class. This per-action check is necessary but not sufficient; the rollout harm gate below must also pass. If sample size cannot support the bounds, remain in recommendation mode.
5. **Expansion gate:** independent new tissue and sequential rollouts show benefit at the chosen risk/cost level. Within-specimen crop success alone does not authorize broad claims.

These provisional numbers are proposed utility thresholds, not a universal acceptable neuroscience error rate. The evaluator must include all harmful automatic edits, even if subsequently undone. Choose stricter false-merge tolerances where scientific impact demands them, before inspecting the locked test.

**Rollout harm gate:** freeze a maximum per-action affected cable/identity size, a maximum count of newly damaging operations per rollout, and a cumulative damaged-cable or partner-identity budget. Set concrete values from the intended scientific use before automatic execution; no autonomy gate is executable while these are unset. Evaluate final retained correct cable and partner correctness against baseline with a predeclared noninferiority margin, alongside recovery gains. Estimate uncertainty by complete neuron/block rollouts with buffers, and report catastrophic events individually. Do not assume per-edit independence or infer a case-specific confidence bound from a global calibration curve. Even 99% per-edit precision can accumulate unacceptable harm over a long run. If these two crops cannot support an impact bound, the next stage remains a limited human-reviewed pilot.

## Reproducibility record for every later run

Save run ID, exact command/script, code/config/prompt/rule/model hashes, dataset and split manifests, source geometry, reference artifacts, operation sequence, errors and abstentions, evidence images, cost/latency logs, evaluator revision and machine/runtime. Store metrics and outputs in the code/data artifact location and promote scientific conclusions through the owning research project. Any cluster execution follows the rdash skill and project contract; this document is not a job submission.
