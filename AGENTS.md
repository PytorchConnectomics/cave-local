# CAVE local

This repository packages the local CAVE stack and installable agent skills.
Read README.md and the relevant skill under skills/ before operating it.

Keep implementation, tests, skills and their documented commands consistent.
Use independent agents for bounded implementation and fresh skill evaluations.
Each test deployment must use a project name beginning cave-pkg- and its own
directory, free loopback port, secrets and persistent volumes. Never stop,
edit, remove or reconfigure unrelated deployments. Source data mounts are
read-only. Tests that edit graphs must use their assigned disposable fixtures.

Never commit generated secrets, databases, backups, datasets, personal account
configuration or generated vendor trees. Record failures and distinguish tests
actually run from planned tests. For public releases, follow docs/release.md,
verify the exact exported candidate, and publish only within the release scope
authorized by the user. A release request authorizes its documented publishing
steps after acceptance; routine maintenance does not authorize a new release.
