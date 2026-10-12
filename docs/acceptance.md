# Acceptance evidence

**Release candidate acceptance passed.** Three complete fresh installations
validated the current source: two hosted fixtures and one independent local
setup using all five skills. The exact exported Git tree also installed and
passed its browser and persistence checks. Final publication/public-clone
proof follows separately below. The validated source is
`61f7c915aba2f36f074d048e0408bac19abddfb8`. Its server adapters,
configuration, locks and five skills retain the server core from
`ddf3df1e8708c0284b2c699fdc7a7e3022b961e9`. Three host-side verifier
constructors now disable SDK server-cache writes, and 16 real-SDK isolation
cases were added. The earlier QA runner/workflow changes remain. These host
verifier files differ; we do not claim every runtime file equals the old base.
[The sanitized release record](evidence/public-acceptance.json) separates
confirmed checks, pending work and retained failures.

| Check | Evidence required | Current result |
| --- | --- | --- |
| Current fast suite | Clean current source: 324 main cases and 77 subtests, one opt-in skip; 16 added real-SDK isolation cases | PASS on Python 3.14 |
| Current package CI | Python 3.11 and 3.14 jobs at the validated source | PASS; 402 JUnit cases each, zero failures/errors, one opt-in skip |
| Earlier local server-core installations | Two source-label/spatial-tile installations, viewer and actual helper lifecycle | PASS at older base; 293 main cases plus 77 subtests each; no current host-cache isolation claim |
| Independent fresh setup and skills | New immutable HTTP fixture; all five skills without prior chat context; recovery/maintenance jobs | PASS, clean independent run 004 at current source; 60 shared SDK/profile comparisons from a pre-first baseline, without a hook |
| Two-CPU hosted setup | Each fixture/lifecycle on its own fresh hosted runner, sequential matrix jobs | Both PASS at current source on Ubuntu 24.04.5 LTS, actual two CPUs and Chrome 153 |
| Candidate Git export | All 92 files, exact bytes/modes, privacy, source markers, clean root history, skills and references | PASS |
| Exported-tree installation | Fresh install/API/browser/concurrency/reconnect/restart checks from the exact exported Git commit | PASS at `3ecbd299a98ba8f54099e76197ccf7b640570403`; no-cache build, Chrome 155, shared SDK profile unchanged |
| Actual Copybara updates | First squash, next update, community-tip replay, retained destination files and conflict rejection on both Python versions | PASS; publishing code unchanged from tested version |
| Original-installation preservation | Final comparison of unrelated installations, source inputs, graph partitions and edit history | PASS after exported-tree installation: 199 protected containers, 121 original files, 60 HTTP files and 1,551 sampled roots across three graphs; edit logs unchanged |

The final threshold is **three complete fresh installations of the exact current
source**: both fresh hosted jobs and clean independent run 004 passed,
meeting the **3/3** threshold. The earlier two
local base installations remain evidence of server-core behavior on this
machine. Together with the new local installation they establish three server
installations here, with the host-reader differences disclosed; they are not
three identical complete current-source installations here. The new independent
evaluation captured the real shared SDK cache/profile before any host SDK use
or fast negative control, then passed all 60 per-command comparisons of bytes,
modes, modification times and ownership metadata, without a diagnostic hook.

Each fresh installation needs a new checkout/environment, fetched sources,
local viewer build, synthetic input, credentials, graph and databases. Local
runs share the OS, Docker daemon, installed tools and package/download caches.
A completed setup phase is not a completed lifecycle run; failed or superseded
sources are not counted toward the exact current-source threshold.

Installation checks cover all fixture voxels and graph memberships, native
Merge/Split/Undo, image/segmentation/mesh alignment, annotations and materialized
snapshots, concurrent table creation, first reads after database disconnection,
whole-stack persistence, reload and a fresh browser tab. The local and hosted
full runners perform online backup/integrity checks, separate restoration,
restored-browser checks, credential rotation and scoped teardown with input
and unrelated-resource preservation. A prior-version update is an optional
local runner job when an older backup is supplied; the hosted runner does not
perform it. Independent 004 additionally passed documented stopped-gateway recovery
with one first snapshot and all 28 responses returning 200, plus a separate
update from the historical accepted backup with complete saved-state equality,
eight current adapters and native browser checks.
The exported-tree gate uses the fresh-install runner and does not require
another backup/rotation/update cycle. Only actual reports establish a pass.

The fast suite also tests failure recovery, foreign-volume protection and
service boundaries with controlled unit/contract fixtures. Those cases are
separate evidence from live Docker workflows; the local/hosted full helper
does not separately inject gateway-only failure or a conflicting foreign volume.

Clean independent 004 verified all 46,080 fixture voxels, 4,946 foreground
voxels, 23 atoms, eight connected groups and both adjacency representations.
It passed normal installation, native browser editing on the main, restored
and updated deployments, backup/integrity checks, credential rotation, recovery,
scoped copy removal and the generated handoff. Its 199 pre-existing containers,
69 volumes, 28 networks, 292 runtime/configuration files, input and backup bytes
remained unchanged. An extra private worker-quiescence observer initially
failed because a warning preceded valid Celery JSON; the underlying command
succeeded. The raw failed observation is retained and was resolved by reading
its output without changing the product or rerunning lifecycle work. We do not
claim every private observer command returned zero or that reserved/scheduled
worker-task inspection was performed.

The exact candidate Git export passed the fresh-install runner with a no-cache
backend build, native API/browser checks, source-label cut refusal, concurrent
table creation, database reconnection, an edit held across restart, and repeated
start/snapshot equality. Its separate before/after shared SDK/profile comparison
also passed; this does not replace independent 004's 60 per-command checks.
Final preservation passed after this installation. The only container-removal
exception was the 20 explicitly retired old stopped fixture containers described
below; their six persistent volumes and deployment data stayed intact.

The earlier full local run 002 passed on `a532c585725d379b4802bfcea84e223226bd6028`,
but that candidate and its documentation-only successor were revoked when the
hosted two-CPU daemon rejected a fixed four-CPU graph quota. The server core
then read daemon capacity and capped generated quotas. At the DDF base, both
local full-003 fixtures and their actual helper lifecycle passed. The ZYX
run also passed the configured prior-version backup update. Each fixture kept
inputs/backups and all 199 pre-existing containers/resources unchanged. Final
original-project preservation also passed after the exported-tree installation. Earlier
failures in Python compatibility, browser preparation, restore-volume ownership and community-tip
replay remain recorded.

Local full run 001 reached a working installation but failed during restoration
because Docker's address pools were exhausted. Two verified old stopped test
fixtures were retired to reclaim their networks. All six persistent volumes
and deployment data were retained; no global prune was used. That failed run
remains failed. A preservation observer initially used wrong path and identifier
representations; failed checks and the corrected comparison were retained.
The corrected mid-release check does not replace final preservation.

Hosted run 38300114858 passed its first complete source-label scenario on an
actual two-CPU Docker daemon with browser major version 153. The whole run
still failed: before building the second fixture, free disk fell below the
20 GiB doctor requirement. That is retained server-functional evidence; it
does not establish host-cache isolation. The later QA runner change selects
one scenario per fresh hosted runner and runs two matrix jobs
sequentially. The failed whole run and its partial success are retained; they
are not counted as two successful installations or as exact new-source proof.

Both final hosted scenarios completed at the exact current source on separate
fresh Ubuntu 24.04.5 LTS runners. Each reported an actual two-CPU Docker daemon,
Chrome 153.0.8010.12, zero errors/failures in 402 JUnit cases (one opt-in skip),
and all 14 install/lifecycle command steps returning zero. Their original and
restored native browser checks, backups, restoration, rotation and scoped
teardown passed. These hosted runs did not perform a prior-version update.

Independent run 003 on `707f3d0888f76c8f6ca76fad50796a7b8eb45b6a`
is retained as an overall failure. Shipped host SDK constructors wrote the
shared per-user server cache before an initial cache baseline was captured.
A later task-local diagnostic hook prevented further shared-cache writes, but
that does not prove the pre-first state was untouched or exactly restored and
does not count as clean skill acceptance. Functional/server checks remain
separate evidence. Three extra private browser-helper attempts also failed:
they used unsupported internal layer reconstruction, and their page errors
are retained separately from the shipped native browser checks.

The original-code MIT license, upstream notices and
[dependency review](dependencies.md) are complete for this preview. Reviewed
locks have zero known npm production and host Python findings. The full npm
build/test graph retains 52 entries, including eight critical; their scope and
unreviewed container dependencies are documented.

After factual evidence is committed, export that exact new commit and verify
the complete tree again. Prove runtime code, configuration, locks and skills
still match the accepted current source, including its cache-safe host readers.
Keep comparisons with the earlier server core limited to unchanged components.
Final migration/public-clone reports stay outside Git so recording an exported commit does not change that commit.
Publication and installation from the actual public clone URL follow the
[release procedure](release.md).

## Historical private acceptance and client-browser follow-up

The following evidence applies to the earlier
`dee41c0d429564019bc14d7459a58f4f45d62f31` implementation. Its three server
installs and separate macOS browser-agent report are useful history; they do
not replace the current candidate checks.

Historical private acceptance passed on 12 October 2026. The implementation and skills
tested by all three fresh installations are commit
`dee41c0d429564019bc14d7459a58f4f45d62f31`. Later acceptance documentation does
not change that tested code. [The sanitized JSON report](evidence/private-acceptance.json)
records individual checks, failures and shared host resources.

| Run | Input | Editing basis | Python | Browser | Result |
| --- | --- | --- | --- | --- | --- |
| G | Local ZYX, nanometers | Source labels | 3.11.15 | Chromium 155 | Passed |
| H | Local XYZ, micrometers | Spatial tiles | 3.14.6 | Chromium 155 | Passed |
| I | Unsigned HTTP XYZ, micrometers | Source labels | 3.14.6 | Chrome 122 | Passed |

Each run used a fresh checkout, Python environment, fetched upstream trees,
built viewer, graph, databases and generated credentials. Inputs were invented
fixtures; the HTTP input was generated separately for run I and kept immutable.
The OS, Docker daemon, installed tools and download/package caches were shared.
Run G rebuilt backend images with `--no-cache`. These are repeated installations
on this Linux x86-64 server, not tests on other server platforms or OS reinstalls.

G checked all 38,400 voxels, 19 atoms and six source components. H checked all
46,080 voxels, 74 atoms and eight source components. I checked all 46,080 voxels,
23 atoms and eight source components. Cases include clipped chunks, objects
crossing chunks, disconnected pieces sharing a label, sparse IDs and uint64
extremes. Every run passed all 51 repository tests.

Every run performed native API Merge/Split/both Undo operations, verified
complete memberships and decoded mesh bounds, saved and read an annotation,
and verified its IDs in a materialized snapshot. Real browser controls performed
Merge and Split, then checked complete membership, reload and a fresh tab.
Final browsers recorded no failed requests, blocked requests or page errors;
ordinary missing Zarr metadata probes remain recorded as 404s.

A separate browser agent on macOS followed the generated run G handoff on
12 October 2026. Its user-provided report passed alignment, segmentation,
meshes, native Merge/Split, reload and a fresh Codex in-app browser tab, with
Chrome reporting reduced version `155.0.0.0` and no console errors. A read-only
server check independently confirmed the reported final roots, all 19 atoms
restored to their original six groups, unchanged annotations/materializations,
and exactly two additional native edits. Root IDs changed as expected.
Selection corrections occurred before submission. The browser required the `localhost` hostname for the forwarded gateway. Eight
screenshot filenames were supplied; the images themselves were not available
for inspection here. This follow-up tests the client browser and handoff, not
another fresh server installation.

Every run also passed four simultaneous real linked-table creators, immediate
annotation materialization, deliberately closed database connections, a
PostgreSQL-only restart with first reads succeeding without request retries,
an edit held across a complete stack restart followed by Undo, and repeat-start
snapshot equality.

Run I was performed by an independent agent given the repo, skills and input
data without our chat history. It installed and validated all five skills and
completed these additional workflows:

| Workflow | Evidence of success |
| --- | --- |
| Backup and verify | Complete online archive, checksum verification, unchanged saved state and immutable HTTP inputs |
| Restore separately | Full saved-state equality, current adapter bytes in running containers, and native browser checks |
| Rotate credentials | Both values changed; application services recreated; persistent stores retained; state unchanged |
| Recover stopped gateway | Documented `start` recreated PostgreSQL while other services stayed running; the single first snapshot matched exactly, with all 28 HTTP responses returning 200 |
| Update from older backup | Backup from `648e9dab6aaaeea6866fb78ab3735b041087b405` retained its graph, annotations and snapshot; all eight current adapters remained current; browser checks passed |
| Remove recovery copies | Only the two explicitly confirmed test projects were removed; their sources, evidence and backups remained |
| Browser-agent handoff | Generated instructions matched served bytes and included exact graph, coordinates, controls, string IDs and reload/fresh-tab checks |

The independent preservation check verified 199 pre-existing containers,
121 original runtime/configuration files and all 60 HTTP input files unchanged.
It initially compared unordered Docker mount lists as ordered lists. That
failed observer check and its analysis were retained; a separately named check
normalized list order and verified zero changed values. No product code was
changed to pass that check.

Two earlier candidates were revoked. One retained closed database sessions;
the first annotation read could fail after materialization or store recreation.
The other allowed annotation ingestion and immediate materialization to race
while creating a linked table. Actual negative controls reproduced both bugs.
The final version checks pooled connections, releases request/task sessions,
and serializes both table creators in one PostgreSQL transaction. It does not
replay annotation writes. A separate older browser run recorded
`ERR_NETWORK_CHANGED` during other stack lifecycle work and remained failed.
Final workflows ran sequentially; passing older runs were not substituted for
the three final installations.

Development tests also covered backup-copy failure, credential-rotation failure
with original credentials/state recovered, archive corruption, unsafe archive
paths and foreign-volume protection. They are distinct from the final three
installations. Raw reports, command journals and browser traces remain private
under each test checkout's `.runtime/`; generated data and credentials are not
committed.

The measured host was Ubuntu 20.04.4 LTS, Docker 28.1.1/Compose 2.35.1,
Node 25.8.1 and npm 11.11.0. Small synthetic volumes establish functional
geometry, editing and recovery behavior. They do not establish petascale
throughput, arbitrary platform support or authenticated public hosting.
The supported access mode is one local editor through loopback/SSH.

The historical runs used the original viewer lock, whose audit reported 55
findings. The current preview has an MIT license and a recorded
[dependency review](dependencies.md): zero known npm production and host Python
findings, with 52 build/test entries retained (12 moderate, 32 high, eight
critical). The older browser runs do not validate the changed viewer build.

[Release preparation](release.md) explains the actual local Copybara migration,
source-byte verification and separate fresh-install test of its exported tree.
The final migration and exported-install reports stay outside Git to identify
the exact exported commit without a circular reference. Nothing here publishes
an organization repository.
