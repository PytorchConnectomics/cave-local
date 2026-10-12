---
name: cave-local-operate
description: Start, stop, inspect or diagnose an existing deployment created by this CAVE local package, and check that graph edits and annotations survive a restart.
---

Work from the repo containing `bin/cave`. Read the selected
`DEPLOYMENT/deployment.json` first. Confirm its project begins `cave-pkg-` and
its URL/graph match the user's request. For exact commands and snapshot checks,
read [operations](../../docs/operations.md).

```bash
bin/cave status DEPLOYMENT
bin/cave logs DEPLOYMENT
bin/cave start DEPLOYMENT
bin/cave validate DEPLOYMENT
```

Use `stop` when requested. `start` is safe to repeat on unchanged inputs. Do not
run editing acceptance on a user's dataset; `validate` without
`--edit-fixture` is read-only.

Diagnose the concrete failing part:

- Port unavailable or wrong graph: inspect `deployment.json` and the browser
  URL; do not stop another application to take its port.
- Graph API unhealthy: inspect `bigtable`, `redis` and `pcg` logs. Check
  ingestion reports and source availability; do not delete volumes or rerun
  ingestion against changed source data.
- Annotation/materialization failures: inspect `postgres`, `info`, `schema`,
  `annotation`, `materialize` and `mat-worker` logs. Preserve databases and
  credentials; use a separate restore when recovery is needed.
- Blank viewer or failed image/mesh requests: check the gateway, viewer build,
  source metadata and network requests. Read `docs/viewer.md` for browser checks.

When extra Compose diagnostics are needed, include this deployment's explicit
`--project-name`, `--project-directory` and `-f DEPLOYMENT/compose.yml`.
Never use the repository root's old Compose example or a global prune.

For a persistence check, pause concurrent editing/materialization requests,
capture `scripts/snapshot.py` before and after `stop`/`start`, and compare the
files exactly as in operations.md. Wait for services to become ready before
taking the second snapshot. Graph mappings, edit history, annotations and
materializations must match. Report the failed check if they do not; a restart
without a comparison does not prove persistence.

Stop retrying if it would require modifying original data, deleting databases,
or changing a database major version. Use the backup skill for a verified
separate recovery copy, preserving the failed installation for inspection.
