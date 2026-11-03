# Error-analysis revision after cerebellum review

Date: 2026-10-19. Scope: candidate triage and evidence requirements; no automatic graph edits. This is an engineering correction to a weak heuristic, not a demonstrated improvement in biological error detection.

## Feedback and interpretation

DW completed the 32x_1 and 32x_2 rounds, reported mostly correct large boutons and approximately 3–4 actual problems, and requested discarding two already falsely merged segments. He subsequently clarified that this round contained no graph edits: some points were added and some irrelevant extra segments were deselected. Therefore an apparent extra long axon in a view must not automatically become a false-merge label. The exact per-case biological labels remain distinct from workflow status and display changes.

The prior consolidation rule promoted a labelled axon when `caliber_radius_um > 0.15` **or** `branch_per_um > 0.5`. The upstream metrics also use related geometry for cell classification, so caliber, branching and classifier disagreement are correlated observations. The exported metric drops the upstream `branch_density_usable` safeguard. These features cannot distinguish a bouton's skeleton branches from an erroneous join. Raising their thresholds to fit this round would not establish a reliable discriminator.

There was also an endpoint vocabulary error: the producer emits `bouton_head`, while consolidation excluded only `bouton`. Both spellings now receive the same handling. This avoids an unexplained-end warning for a recognized bouton-shaped endpoint; it does not certify correct connectivity or a biological termination.

## Implemented triage policy

Implementation: [review_policy.py](/home/donglai/dw-os/coding/cave-seg-bio/review_policy.py), integrated into [consolidate.py](/home/donglai/dw-os/coding/cave-seg-bio/consolidate.py).

| Evidence | Treatment |
|---|---|
| Large caliber, high branch density, metric class disagreement, missing/undersized measurement | Advisory; preserve raw metrics; no automatic review-queue promotion |
| Unexplained interior endpoint | Advisory; exclude spur and recognized bouton-shaped endpoints; no automatic queue promotion |
| Unresolved annotation target, orphaned split piece, unlabelled candidate, explicit pending/flagged item | Workflow review obligation, not a confirmed error or authorization to edit |
| Advisory and workflow obligation on the same root | Preserve the workflow obligation; never suppress the entire object because it looks like a bouton |
| Selected or annotation-linked second root | Context only; not evidence that the roots are connected |
| Partner deselected and submitted | Persisted per-item `queue_apart` association; retain/export it and apply only to that root pair, never suppress all work on either root |
| Unknown measurement or invalid coordinate | Remain unknown; do not silently infer a valid origin or correct anatomy |

Structured findings retain reason, code and tier; output includes untouched metric records and policy version `review-triage/3`. Existing waiting items retain the explicit `--include-waiting` behavior. The policy does not overwrite labels, annotations, queue status or segmentation. Tests exercise the actual decision policy without importing the live gate.

## Required evidence before an error recommendation

1. **Membership first.** Resolve source atoms and targets at the same timestamp. Diagnose a false merge only within a single root; a display containing two roots is not one object. Keep contextual roots visually and semantically separate.
2. **Point provenance.** Keep annotation ID, exact coordinate/frame, sampled atom/root, intended target atoms/roots and time. A point on background represents missing tissue; a point on a third root or with multiple targets requires clarification of identity. Neither circumstance proves an edit is needed. Merely sharing a point group or viewer selection is insufficient.
3. **Localized tissue evidence.** For a suspected merge, inspect the putative junction and continuation in raw orthogonal/adjacent slices. Compare the bouton explanation against an unrelated cable joining the object. For a suspected split, verify a specific compatible continuation; do not join a long partner solely because an annotation names it.
4. **Independent uncertainty.** Several correlated morphology warnings do not satisfy missing image or identity evidence. Missing evidence requests more context or yields abstention.

Steps 1–4 specify the automatic engine's evidence contract. Consolidation now checks point origins/targets against exported atom membership and preserves pair-specific kept-apart decisions; it remains a review-report tool, not a local image discriminator. Its live labels/points/queue metadata can differ from a frozen graph export. Snapshot disagreement is explicit uncertainty, not evidence that a completed join should be repeated. Transactionally consistent biological adjudication remains necessary before automatic graph decisions.

## Evaluation consequences

Both crops are development data after this feedback. Preserve accepted bouton examples only when their individual verdict is established; `done` alone is not a negative label, and the approximate 3–4 errors are not an exact labelled set. Keep the two bad references out of trusted GT only with explicit identity/provenance, while retaining them in an unresolved-error/exclusion ledger and the original cohort accounting. Do not delete their graph data or count their omission as a successful correction.

The relevant comparison is useful-error yield and missed errors under the same audit budget, including a sample of advisory sites. Fewer queued items by construction is not improved precision or recall. Freeze the revised policy before an independent evaluation and retain all raw warnings for that audit.

## Comparison of the new exports with the previous references

Fresh exports for both volumes were created at `20261019T055035Z` using the existing `cave-seg-bio/export.py` at one shared graph timestamp. Compared with x1 `20261017T032104Z` and x2 `20261016T184543Z`, every native operation record is identical (96 and 192 respectively), every atom mapping is identical, and all 67,108,864 voxel labels in each volume are identical. Declared file hashes were checked against each manifest; HDF5 file hashes differ because the export timestamp is metadata, so the comparison read the arrays directly.

Local comparison artifacts: [export comparison](review-feedback/2026-10-19/export-comparison.json), [review changes](review-feedback/2026-10-19/review-changes.json), [kept-apart decisions](review-feedback/2026-10-19/kept-apart.json) and [reference-exclusion ledger](review-feedback/2026-10-19/reference-exclusions.json). These generated local artifacts are separate from the immutable old exports.

| Recorded change | Interpretation |
|---|---|
| x1 suspicious queue #19: points 493/494 on root `288230376151745934`, with stored targets `288230376151712328` and `288230376151712208` | Annotation relationship involving a third root; not a graph merge or demonstrated biological identity |
| x1 suspicious queue #25: root `288230376151752258` labelled dendrite | Recorded semantic correction from the older axon label |
| x2 final queue #31 follows `288230376151712058` instead of `288230376151712801`; 11 new background points (696–706) target `712058` | Changed review anchor plus evidence for unsegmented tissue; the old anchor still belongs to `712801` |
| x2 root `288230376151712801` receives an axon label | Semantic annotation; no connectivity change |

No existing annotation content, deletion or settled-point decision changed in the comparison. This does **not** mean deselections were forgotten: they are persisted separately in SQLite `queue_apart`. The initial comparison omitted that table. It is absent from native `edits.json` and from the old `gather_gt.py` review export, so comparing those files alone misses this part of the user's proofreading.

The viewer selects the main root plus point-linked partners. Submit sends `hidden`/`visible` IDs; `_set_apart` stores a supervoxel anchor for each hidden partner, scoped to the queue item; `_item_payload` resolves those anchors and removes the partners on revisit. `point_settled` is a different, annotation-level record. Neither mechanism edits the segmentation. The exporter now includes `queue_apart` and a live metadata observation window in `review.json`.

Current kept-apart records occur on x1 suspicious items #4, #6, #17, #19, #25 and #40 (16 anchors). For example, #4/root `288230376151770730` excludes partner `288230376151726658`. x2 final item #31 now follows `288230376151712058` and excludes its former followed root `288230376151712801`. These are recoverable per-item review decisions, not necessarily six segmentation errors. The table has no per-record time/account and the baseline omitted it, so the exact insertion times cannot be recovered from these exports.

The revised exporter helper was also run read-only against the current registry and fresh graph exports. Complete queue metadata is saved as [x1](review-feedback/2026-10-19/queue-review-32x_1.json) and [x2](review-feedback/2026-10-19/queue-review-32x_2.json): 48 kept-apart records on 20 items for x1, 45 on 27 items for x2, with zero unresolved atom mappings. These are queue-only sidecars, not newly generated full GT/NML bundles.

The two outstanding x2 final-queue entries are #25, root `288230376151790682` (flag), and #30, root `288230376151712092` (now skip). The local exclusion ledger quarantines them from a future trusted reference because review remains unresolved. Matching these IDs to DW's two reported bad merges is an **inference**, not a confirmed per-item error label. No live segment was deleted, and no existing GT export or scoring configuration was rewritten.

## Validation performed

From `/home/donglai/dw-os/coding/cave-seg-bio`, `.venv/bin/python -m unittest discover -s tests -v` passed all **22** tests. These cover large-bouton advisories, explicit work surviving advisory cues, both endpoint spellings, malformed/missing measurements, coordinate quarantine, pair-scoped and multi-target annotations, snapshot disagreement, exported string IDs, unresolved anchors and a concurrent SQLite writer. Consolidation/export integration fixtures use temporary data and fake gate modules. `git diff --check` passed. This is software validation, not a biological precision/recall benchmark or a live graph-edit test.
