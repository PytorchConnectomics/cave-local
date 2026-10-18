# Research: what to borrow and what to test

Research date: 2026-10-18. Project-specific synthesis. Literature statements below are source reports, not reproduced results. Architecture recommendations are our proposals. Read primary evidence before using any performance number in a manuscript.

## The decision

Build on graph-based reconstruction, specialist visual evidence and formal edit contracts. Keep VLMs replaceable. The unproven contribution is whether an explicit, inspectable morphology/evidence language enables cheaper reliable decisions and more useful abstention than these components alone.

The best near-term comparison is **specialist scorer vs Qwen vs hybrid contracts**, all using identical candidates and evidence. A comparison only against a slow GUI agent would not establish a research contribution.

## Evidence that determines the architecture

### 1. AlphaProof Nexus: verification is the transferable idea

[Tsoukalas et al., *Advancing Mathematics Research with AI-Driven Formal Proof Search*, arXiv:2605.22763v1](https://arxiv.org/html/2605.22763v1), full HTML including methods/ablations inspected. The system separates problem statements, generated proof attempts and external checking; checks reject incomplete proofs and disallowed axioms. Its basic agent reproduced the nine selected Erdős successes, with higher cost on difficult cases; the full system includes an RL-based prover. Expert scrutiny of whether formalization matches the intended question remains necessary.

**Implication:** start with a simple propose/check loop and immutable specifications. In microscopy, visual premises remain uncertain. Formal success cannot certify those premises. This paper supports external verification and ablations; it does not show that RL is inherently misguided.

Local KB searches by arXiv ID, title and AlphaProof Nexus did not find a corresponding note in this checkout. The supplied primary paper was accessible. Do not infer absence from another machine or the raw PDF collection.

### 2. ConnectomeBench1: task competence is heterogeneous

[Brown et al., *ConnectomeBench: Can LLMs Proofread the Connectome?*, arXiv:2511.05542v1](https://arxiv.org/html/2511.05542v1), full text inspected. Tests mesh-based segment type identification, split-error repair decisions and merge-error detection. Binary split results for o4-mini with heuristics are 75.4%/78.6% for fly/mouse; binary merge detection with descriptions is 62.8%/61.5%. Split experiments use 100 examples and ten calls per example with voting. Small human comparisons and repeated inference limit conclusions about speed or dependable automation.

**Implication:** preserve task-specific results and total call cost. This does not test candidate discovery, actual cut execution or cascading edit damage. Existing [KB entry](/home/donglai/dw-os/kb/papers/wiki/connectomics/connectomebench_llm_proofreading_2025.md) is a starting index, not sufficient evidence for a deployment claim.

### 3. ConnectomeBench2: a strong specialist baseline

[Brown et al., *ConnectomeBench2*, arXiv:2606.21116v1](https://arxiv.org/html/2606.21116v1), full text; [official code](https://github.com/timfarkas/ConnectomeBench2), README inspected. Reports 716,485 decisions across four species, with ViT-B balanced accuracy 97.0% for split repair and 93.0% for merge identification, plus a 2D correction-mask head at 0.696 mIoU. Spatial 80 µm cube splits are not neuron-disjoint. Reversed edits are excluded; control reliability and species coverage vary. Cross-species merge identification is weak, with sparse human/zebrafish junction data.

**Implication:** reproduce its visual specialist before attributing value to a VLM. A 2D mask is not an executable 3D graph edit. Calibration and “human-level” claims are conditional on task/distribution. Pin dataset release and licenses separately from the MIT code. [Existing KB entry](/home/donglai/dw-os/kb/papers/wiki/connectomics/connectomebench2_proofreading_2026.md).

### 4. Recent VLM evidence: adaptation and a leakage trap

[Li et al., *Benchmarking Vision-Language Models on Synapse Detection and Proofreading in Connectomics*, arXiv:2609.36492v1](https://arxiv.org/html/2609.36492v1), full text; DW is a coauthor. Qwen3.5-9B improves from 50.0%/52.9% zero-shot split/merge balanced accuracy to 94.6%/90.2% after full-split LoRA adaptation. Its merge-task input omits an oblique CB2 view whose orientation depends on post-split masks. Human/zebrafish transfer merge tests contain only 60/54 operations. The paper says the project will become public upon acceptance; no available checkpoint is assumed here.

**Implication:** budget for local calibration/adaptation, include specialists, and construct every view from information available before correction. This is evidence about the evaluated models and EM tasks, not measured Qwen3.8-Flash-Next performance on these cerebellum crops.

### 5. NEURD: morphology rules work, but can remove true structure

[Celii et al., *NEURD offers automated proofreading and feature extraction for connectomics*, Nature 640, 487–496 (2025)](https://www.nature.com/articles/s41586-025-08660-5), DOI `10.1038/s41586-025-08660-5`, full text inspected; [official implementation](https://github.com/reimerlab/NEURD). Decomposes meshes into annotated morphology graphs and records parameterized filters behind corrections. Its proofreading addresses merge removal rather than extending false splits. In the reported excitatory-axon analysis, improved synapse precision came with substantial removal of correct synapses; its recall reference excludes manual extensions.

**Implication:** reuse the graph/feature/rule organization as a baseline. Measure retained correct cable and connectivity alongside removed errors. Cortical rules must not be promoted to universal cerebellar laws. [Local KB note](/home/donglai/dw-os/kb/papers/wiki/connectomics/neurd_graph_proofread_2023.md) refers to the earlier work; distinguish the final publication.

### 6. Autoproof: the practical cost comparator

[Huang et al., *Autoproof*, arXiv:2509.26585v1](https://arxiv.org/html/2509.26585v1), full text inspected. A supervised scorer combines local images/masks with optional shape and synaptic features. Uses 40k training and 20k test focused-merge decisions; test merge prevalence is 20%. Its triage calculation recovers 90% of useful merges by human review of roughly 20% of tasks. Separate automatic orphan linking reports 200k attachments; that operating point includes a nonzero error allowance and uncertain manual judgments.

**Implication:** reducing expert workload is a meaningful baseline before full autonomy. Do not interpret triage's retained human accuracy as error-free automated merging. Cross-dataset transfer and this modality remain unestablished. The retrieved v1 HTML has inconsistent displayed dates and a conclusion sentence inconsistent with its 80% reduction calculation; use the methods calculation and pin source version rather than reproducing the inconsistent sentence.

### 7. RoboEM: independent tracing evidence

[Schmidt et al., *RoboEM*, Nature Methods 21, 908–913 (2024)](https://www.nature.com/articles/s41592-024-02226-5), DOI `10.1038/s41592-024-02226-5`, full text inspected. Learns steering through raw 3D EM and checks connections with forward/backward tracing. Reports resolving 76% of ending and 78% of chiasma queries without errors, while also reporting introduced errors for 4%/1% of queries. Its cost comparison is specific to its reconstruction setting.

**Implication:** an image-based local tracer can produce additional continuity evidence independently of mesh appearance. Candidate agreement is useful evidence, not a theorem; transfer to expansion/light microscopy requires testing. Include random axonal sites, not only easy soma-connected structures.

### 8. Biologically constrained graphs: direct prior art

[Matejek et al., CVPR 2019](https://openaccess.thecvf.com/content_CVPR_2019/html/Matejek_Biologically-Constrained_Graphs_for_Global_Connectomics_Reconstruction_CVPR_2019_paper.html), official abstract and first PDF page inspected; [code](https://github.com/Rhoana/biologicalgraphs). DW-coauthored work already combines morphology-informed candidate graphs, learned shape evidence and graph partitioning, reporting average VI improvement across four datasets.

**Implication:** “use biology to constrain graph edits” is not novel. A stronger contribution is reusable typed assumptions, evidence provenance, independently checked edit consequences and lower cost at a fixed harmful-edit budget. Code compatibility and dependencies require a separate bounded survey before reuse.

### 9. Biological counterexamples must shape the contract

[*Exploring the significance of morphological diversity for cerebellar granule cell excitability*, Scientific Reports (2017)](https://www.nature.com/articles/srep46147), full text inspected by the research agent. Describes granule-cell axons emerging from primary dendrites and the ascending-axon T-junction/parallel fibers. Those are essential counterexamples to cortex-derived “axon-on-dendrite” and branch-shape rules; sample-specific frequencies should not become universal priors.

[Neurite autofusion in C. elegans](https://pmc.ncbi.nlm.nih.gov/articles/PMC3057141/), primary study inspected, documents real loops. It refutes a universal tree axiom without establishing cerebellar loops. Computational adjacency cycles and skeleton artifacts must also be distinguished from biological loops.

## RL and the company comparison

“Visual Intuition uses RL to mimic humans” is the user's reported context. Exact and variant searches did not identify a matching primary company source. Identity, architecture, measured performance and latency remain unverified. Do not assign another company's work to that name.

[RLCorrector, arXiv:2106.05487v2](https://arxiv.org/html/2106.05487v2), full text inspected, is a concrete hierarchical RL precedent for error localization and correction action selection; 3D extension is left to future work and coarse actions limit small-fragment handling.

Our recommendation is to defer RL until there is a trustworthy environment and outcome evaluator. API actions are a smaller and more stable action space than browser gestures. Later RL can optimize candidate ranking, view acquisition or edit sequences under the same checker. The central risk is an incomplete reward that favors plausible morphology or fewer fragments over actual tissue identity; behavior cloning and RL are different methods and neither is ruled out by current evidence.

## Exact Qwen target and pricing

The official [Qwen3.8-Flash-Next card](https://huggingface.co/Qwen/Qwen3.8-Flash-Next) confirms vision support and distinguishes the open-weight model from hosted Flash. Metadata inspection via `hf models info Qwen/Qwen3.8-Flash-Next --format json` returned revision `de4b8e4d43b917e7706784d8bb445c9af86a3540`. The card lists `qwen-community-1.0`; do not assume an Apache license. Approximately 360 GB of stored artifacts and the larger total parameter count mean “6B active” is not a 6B-memory deployment. Configurable thinking and smaller responses are candidates to benchmark, not guaranteed speedups.

The official [QwenCloud Flash page](https://www.qwencloud.com/models/qwen3.8-flash), observed 2026-10-18, lists managed `qwen3.8-flash` at **$0.15/M input, $0.47/M output, $0.016/M cached input tokens**. These are sibling hosted-model prices, not a quote for self-hosted Next. Recheck region, billing rules and model version when measuring.

Preserve two separate configuration rows if both are tested: exact `Qwen/Qwen3.8-Flash-Next` weights/runtime; optional managed `qwen3.8-flash`. Never silently substitute the latter for the user target. Log image tokenization and reasoning/output charges from actual usage.

Illustrative arithmetic only: at the hosted rates, 6,000 uncached billed input tokens and 300 output tokens cost `$0.001041` per call, or `$10.41` for 10,000 calls. This excludes extra views, retries, rendering, storage and humans and is **not a measured proofreading cost**. Self-hosting instead requires accelerator-hours, utilization, serving memory and queue time. Compare all approaches by dollars and reviewer minutes per independently validated useful correction.

## Search and evidence log

Read existing KB ConnectomeBench notes before primary lookup. Queries included `2605.22763`, `AlphaProof Nexus`, `ConnectomeBench2`, `connectomics automated proofreading morphology NEURD`, `Visual Intuition connectomics proofreading reinforcement learning`, `Qwen3.8 Flash Next`, and `Biologically-Constrained Graphs`. Searches and primary retrieval occurred 2026-10-18. Source IDs above deduplicate arXiv versions and DOI publications; NEURD preprint/final versions are explicitly distinguished. No papers were ingested into a second library, no paid inference/training was performed, and no paper's results were reproduced here.
