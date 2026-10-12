---
name: cave-local-backup
description: Back up a packaged CAVE deployment, verify backup integrity, or restore its graph edits and annotations into an isolated copy without replacing the original.
---

Work from the repo containing `bin/cave`. Read
[operations](../../docs/operations.md) for full backup/restore behavior. Identify
the original deployment, a new archive directory outside it, and whether
immutable local input should be included. Pause new editing/materialization
requests for the check; the script briefly stops this deployment.

```bash
bin/cave backup DEPLOYMENT NEW_ARCHIVE_DIR --include-source
bin/cave verify-backup NEW_ARCHIVE_DIR
```

Omit `--include-source` for large retained input folders; recovery will require
the unchanged folder. HTTP(S) input must remain available and unchanged.
Backups contain database credentials and secrets: keep them private, outside
Git. Verify must exit successfully. Backup must report `complete: true` and
`state_checked: true` for an online copy. A missing completed manifest means
failure; the script still tries to restart exactly the previously running
services. Check `status` if the restart fails.

`--offline` is allowed only when the whole deployment is stopped. It provides
a consistent copy with hashes, but explicitly lacks an original API snapshot.
Describe that limitation in the handoff.

For recovery, use a trusted backup, the repo version recorded in its manifest,
a new deployment directory and an unused loopback port:

```bash
bin/cave restore ARCHIVE_DIR NEW_DEPLOYMENT --port NEW_PORT
```

Add `--source-dir UNCHANGED_INPUT` if local input was not included. Restore
verifies hashes before creating files or Docker state, generates a separate
project, restores only its fresh volumes, and compares graph/annotation/edit
observations with the saved backup. It preserves graph/datastack identities
and the original Bigtable namespace. `--graph-id` can assert the original
identity; it cannot rename it.

Require `state_compared_to_backup: true` in `restore-report.json` for an online
backup, then inspect the restored image, segmentation and meshes in the
browser. Do not redirect users until those checks pass. Offline copies cannot
claim comparison with an absent original snapshot.

If restoration fails, inspect the new target with `status`/`logs`. Its separate
files and volumes remain. Do not delete or reconfigure the original to fix it.
Remove an unwanted failed copy only through the specific-project teardown in
[maintenance](../cave-local-maintain/SKILL.md).
