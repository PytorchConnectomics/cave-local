# Automatic proofreading: design and research plan

Date: 2026-10-18; review-policy update 2026-10-19. Status: proposed automatic proofreader, with a revised consolidation policy implemented in the companion `cave-seg-bio` repository. No automatic proofreader has been benchmarked.

## Recommendation

Build an API-driven proofreading engine with **typed edit proposals, explicit visual evidence, a small deterministic graph checker, and selective model use**. Treat “Lean for neuro morphology” as a program of making claims and edit consequences checkable. A checker can establish what an edit does to a graph; microscopy evidence and expert adjudication must establish whether that edit matches the tissue.

Use Qwen3.8-Flash-Next as the intended VLM, behind a model adapter. Compare it against a cheap task-specific visual model and geometry-only rules. Spend model calls on unresolved local decisions and additional views; keep graph traversal, candidate generation, coordinates, edit execution and bookkeeping in code.

The first useful deliverable is a reproducible **offline replay and recommendation system** on the two cerebellum crops. Progress to automatic edits on disposable graph copies, then bounded real use only after measurable accuracy and transaction guarantees. This is a plan for that complete system, not a recommendation to stop permanently at a human assistant.

## Read in this order

1. [Research and competing approaches](01-research.md): primary evidence, the Lean analogy, RL, relevant baselines and limits.
2. [System and morphology contracts](02-design.md): representation, candidate search, checker and model roles.
3. [API and CAVE integration](03-api.md): existing code, proposed interfaces, consistency and recovery.
4. [Cerebellum evaluation](04-evaluation.md): known data, leakage controls, experiments and decision gates.
5. [Implementation sequence](05-roadmap.md): bounded milestones, owners, stop conditions and open questions.
6. Inspection evidence: [supplied October 17 x1 bundle](evidence-x1-20261017.json) and [older snapshots/revisions](evidence.json), separate from future experimental results.
7. [Boutons, annotation provenance and review policy](06-error-analysis.md): October 19 feedback, implemented triage changes and evaluation consequences.

## What changes the design

- The desired loop is `propose → collect evidence → check → simulate → apply → inspect`, with an explicit `unknown` outcome. Repeated model agreement is not an independent verifier.
- “Morphology rules” alone are established prior art. The research opportunity is a reusable, scoped language of morphology claims and evidence, checked edit semantics, and a demonstrated accuracy/cost improvement over strong visual and graph baselines.
- The mathematical paper uses RL as one component. The useful distinction is the quality of the objective, feedback and action space. Human mouse imitation is an optional baseline; it is not necessary for an API-native proofreader.
- This checkout edits whole supervoxels. Errors inside one atom require the separate refinement work. Native preview and the robust multicut fork do not currently execute identical algorithms.
- The cerebellum records include a rebuilt x1 graph initialized from prior proofreading. Its original input is located, but voxel/partition reconstruction still needs checking. The October 17 x1 snapshot contains 42 suspicious/todo roots; DW completed that review on October 19 and reported mostly correct large boutons. Preserve the historical snapshot and reconcile per-item outcomes separately. Its NML skeletons also contain inferred gap edges.
- Caliber, branch density and metric disagreement alone are advisory. A co-selected or annotation-linked partner is a separate object until graph membership establishes otherwise. Neither review completion nor an unchanged graph establishes a negative error label.
- ConnectomeBench measures important subproblems. Its scores do not establish proposal recall, executable 3D corrections, or safety over a sequence of edits.

## Scope and provenance

This folder owns implementation planning for `cave-local`; it does not replace scientific project state in `research/projects/` or the paper library in `kb/`. Existing KB entries are linked in the research document. No job cards, cluster jobs, deployments, source data or public interfaces were changed. All new endpoints and theorem names below are proposals.

Inspected baseline: `PytorchConnectomics/cave-local` commit `29645eb79a9f946c09eaab38a17c8ea0858fd41d`, originally on `agglomerated-supervoxels`; notes are on `codex/auto-proofreading-design`. The pre-existing untracked `.agent/` work is preserved. Its recorded deployment claims are not equivalent to a live deployment audit.

Root `git pull --ff-only` failed because `codex/mac-mini-learning-digest` has no tracking branch. `git submodule update --init --no-fetch --merge pm research` completed with both already up to date relative to the root's recorded commits; existing branches and pointer differences were preserved. This is not a claim that every checkout matches its remote. KB revision read: `61f12768c58047b3bdda47862dbc8c656faadd4e`.

## Immediate next action

Validate the located pre-review cerebellum input across the rebuild lineage, reconcile the October 19 review and annotation changes, and build the offline candidate/evidence harness. Both crops now inform development; reserve new independent data for evaluation. Keep excluded bad references, unresolved cases and inferred NML edges visible. Do not start with an RL training run or a broad biological ontology.

Scientific owner: DW. Implementation owner: next assigned CAVE development session. Proposed review: 2026-10-25 or after the first data-manifest milestone, whichever comes first; no scheduled automation or capacity commitment is implied.
