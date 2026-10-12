---
name: cave-local-maintain
description: Stage a CAVE package update, rotate generated credentials, arrange SSH access, or remove one explicitly identified packaged installation while preserving input data and backups.
---

Read [operations](../../docs/operations.md) for the procedure matching the
request. Work from the appropriate repo containing `bin/cave`.

For an update, record the current commit and make a verified backup. Prepare a
separate clone of the candidate, run `deps`, `fetch` and `build`, then use its
`restore` command on a new directory and port. Require saved-state comparison
and browser checks. Verify the effective repo revision and runtime hashes in
`restore-report.json`: runtime files come from the candidate, while archived
copies remain under `provenance/archived-runtime` for reference. Configuration
must be represented by the manifest. Review any needed hand-edited runtime
settings separately; they are outside automatic migration. Do not point
candidate images at the original volumes.
Database schema migrations, PostgreSQL major-version upgrades and carrying
edits into changed segmentation need separately implemented and tested steps;
stop that part if such a change is required.

Before the candidate receives new edits, recovery is starting the original and
returning access to its URL. After new edits, back up the candidate and plan
compatible recovery; the original alone lacks those edits. Keep the original
stopped and its backup until the user no longer needs that recovery path.

For credential rotation, pause editing/materialization requests and keep a
verified backup, then run `bin/cave rotate-secrets DEPLOYMENT`. It changes the
PostgreSQL role password and application session secret, recreates only this
project's running application services, and compares complete saved-state
snapshots. Require `rotated: true` and `state_unchanged: true` in
`rotation-report.json`. The old environment is retained privately under
`secrets/`; never print it. Failed rotation attempts to restore old credentials
and verify recovery. If the report cannot prove recovery, stop further changes
and restore the verified backup into a separate target. This command does not
enable web authentication.

For remote access, preserve the loopback bind and use an SSH tunnel from the
client: `ssh -N -L PORT:127.0.0.1:PORT USER@SERVER`. Both ports must match
`local_url` for the viewer's URLs to work. Check the browser through that
tunnel. Public access to this authentication-disabled stack is outside the
supported routine setup; do not create an internet tunnel on the user's behalf
as a substitute for authentication.

For removal, identify the exact deployment and `project_name` from its metadata.
The request must cover deleting its edits and annotations. Back up wanted
state, then run:

```bash
bin/cave teardown DEPLOYMENT --confirm EXACT_PROJECT_NAME
```

The confirmation value is the exact `cave-pkg-*` project identifier. The command
checks volume ownership and deletes only that project's containers/network and
persistent volumes. It leaves deployment files, immutable source and backups.
Never use a global Docker prune or a recursive parent-directory delete.

Report which project was removed and what the successful command verified. A
different requested task does not authorize deleting a deployment or publishing
the private repository.
