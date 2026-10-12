# Run the test suite

There are two levels. The fast suite runs without Docker or a browser. The full
suite builds two new installations and exercises the actual viewer, APIs,
databases and maintenance commands. A fast pass does not imply a full pass.

## Fast checks

After installing the locked Python dependencies, run:

```bash
.venv/bin/python scripts/test_suite.py fast --output .runtime/tests/fast-001
```

Use a new output directory each time; previous reports are never overwritten.
This checks Python syntax, discoverable skill metadata and documentation
references, then runs every test under `tests/`. Tests cover data geometry and
label identity, invalid CLI/configuration boundaries, browser/handoff isolation,
dependency provenance, archive corruption and traversal, restoration/rotation
failure handling, scoped removal and release/export contracts.

`suite-report.json` starts with `passed: false` and records the observed Git
revision and whether the working checkout has uncommitted changes. Optional
`--commit` asserts that revision; a different revision is rejected. JUnit test
counts include subtests and are separate from the suite phases. Any failed command retains its
exit code and log and leaves the overall report failed. The Python 3.11 and
3.14 GitHub checks run this same command and retain its report/logs/JUnit file.

## Full disposable lifecycle checks

Use a supported Linux x86-64 host with working Docker/Compose, enough disk/RAM
and a real Chromium/Chrome browser. Builds and fixture ingestion require at
least 20 GiB free; prepare enough additional space to retain both fresh clones,
their environments, logs, databases and backups. Pause unrelated deployment
start/stop/recreate work while testing. Never use a real dataset as a fixture.

Pick an exact commit and the branch that contains it. For a local Git checkout:

```bash
REVISION=$(git rev-parse HEAD)
.venv/bin/python scripts/test_suite.py full \
  --source "$PWD" --commit "$REVISION" --branch main \
  --output .runtime/tests/full-001
```

Set `--branch` to the candidate branch when it is not on `main`; a GitHub clone
URL is also accepted as `--source`. Authentication comes from your existing Git
environment. The new installations must report that exact requested revision.

The suite checks an existing Playwright or system browser before the long
build. Use `--chromium /path/to/chromium-or-chrome` to select a working executable
or launcher. `--install-browser` is opt-in for a supported Playwright host.
The pinned Playwright installer rejects Ubuntu 20; use an existing working
system browser there. Chromium, the OS, Docker daemon, tools and download
caches are shared host dependencies, not claimed to be freshly installed.
Browser selection and actual version are recorded by each fresh-install report.

The two scenarios run **sequentially**:

| Scenario | What differs |
| --- | --- |
| ZYX image, nanometer metadata, `source_labels` | Source labels remain indivisible editing units |
| XYZ image, micrometer metadata, `spatial_tiles` | Labels are deliberately subdivided into configured tile units |
| ZYX image, `agglomerated_supervoxels` (`bin/cave fixture --atom-policy agglomerated_supervoxels`) | Two touching supervoxels start as one segment with a 0.9 region-graph edge; split, merge and undo follow the supervoxel boundary |

The default command runs both on one host. For a targeted run, add `--variant
zyx` or `--variant xyz`. The report records the requested scenarios and only
reports success after those scenarios complete. A one-scenario run does not
claim that both were tested.

Each gets its own exact Git checkout, Python environment, synthetic sources,
viewer build, unused loopback ports, graph ID, secrets, Docker project and volumes.
For both, the suite requires:

1. All unit/data/package checks, exhaustive voxel/label/topology checks, native
   graph edits and Undo, decoded meshes, annotation and materialized snapshots.
2. Actual browser Merge/Split, restored component membership, reload and a
   fresh tab; request/console evidence and screenshots. Source-label variants
   additionally verify that prohibited splits leave the graph unchanged.
3. Overlapping linked-table creation, first-request recovery after closed
   database connections/PostgreSQL restart, a real edit across full restart,
   and unchanged snapshots when `start` is repeated.
4. A real online backup, checksum verification and restore into separate
   volumes, with exact saved graph/annotation/edit state and the candidate's
   actual adapter bytes. Native browser edits on the restored copy must leave
   the original untouched.
5. Credential rotation with unchanged complete state, unchanged persistent
   store container IDs, recreated application containers and private `.env`.
6. Exact-project teardown, including removal of only its containers, network
   and volumes, while original sources, backups and deployment files remain.
   Every pre-existing container, volume and network is checked afterward.

Failures leave their disposable installations and private logs in place for
investigation, with `passed: false`. Success tears down only the assigned
projects; their source files, backups and evidence remain. Do not globally
prune Docker to clean up afterward.

## Gates that are separate from the two-fixture run

To test a supported update from a previous release, add `--upgrade-backup`
pointing to a trusted, verified online backup of an **older disposable fixture**
with local source included. The suite restores that backup using the candidate,
requires complete state and candidate revision, checks the real browser and
removes only that new copy. Without this input, the report explicitly says
`cross_version_upgrade_tested: false`: same-version restore is not an update test.
This extra gate runs on the first selected scenario, including a targeted XYZ
run; the report cannot claim it passed when it was not executed.

Actual first and subsequent Copybara migrations, preserving public-side
community files, executable modes and no-op behavior require the separate
opt-in migration suite described in [release preparation](release.md).
The lifecycle report explicitly says it did not run those migrations. Release
acceptance also requires the repeated fresh installations and independent skill
evaluation; a single full run does not erase those gates.

## Hosted runner resources and shareable evidence

The manually dispatched full GitHub workflow runs ZYX and XYZ sequentially as
two matrix jobs, each on a separate fresh Ubuntu 24.04 runner. Each job uses
the same exact-commit command with its explicit variant and runs the full native
API/browser and maintenance lifecycle. The workflow passes only when both jobs
pass; they upload separate named reports. Separate hosts provide each scenario
the required free space rather than retaining the first scenario's images and
builds into the second host's capacity check. Standard runners initially may have less than
20 GiB free. Only on a verified ephemeral GitHub-hosted Ubuntu 24.04 runner,
it can remove the unused preinstalled Android and .NET SDK folders. It never
does this on a local/self-hosted machine and never prunes Docker. The unchanged
20 GiB disk and 6 GiB RAM gate still applies afterward.

The local evidence remains private and includes logs, browser traces and
backups. To collect fixture reports, screenshots and credential-redacted logs
for sharing, use a new directory:

```bash
.venv/bin/python scripts/test_suite.py collect \
  --source .runtime/tests/full-001 --output .runtime/tests/shareable-001
```

Collection excludes `.env`, credential recovery files, databases, source data,
backup archives and raw browser traces. It redacts the generated fixture
passwords/session secrets from copied text and leaves original failure logs
unchanged. The hosted workflow uploads only this collected evidence.
