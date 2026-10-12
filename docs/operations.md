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
files, unexpected files, links and archive paths that escape extraction.
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
