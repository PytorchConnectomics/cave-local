# Release and future updates

Use this procedure to publish a tested source revision or ship a later update.
Normal installation follows [the handoff guide](handoff.md); it does not need
Copybara or the release tools.

Before publication, finish the [acceptance checks](acceptance.md), review the
[dependency findings and licenses](dependencies.md), and inspect the exported
tree. A changed runtime dependency or executable behavior requires fresh
installation and browser checks against the final candidate. Keep failures and
the exact tested source revision in the release evidence. Run installations and
browser workflows sequentially on this host, as described in the setup skill.
Publication needs the repository owner's authorization; a local preview does
not provide that authorization.

## Build the release tools

From the source checkout:

```bash
python3 scripts/fetch_export_tools.py
```

`copybara.lock.json` pins Copybara v20261012, Java 25.0.4.1+1, Git 2.51.2,
curl 8.22.0, and OpenSSL 3.5.9 LTS with SHA-256 checksums. Provisioning supports
Linux x86-64 and needs GCC, make, Perl, pkg-config, zlib development headers,
the host CA certificate store, and network access. All downloads, builds and
installed tools stay under `.runtime/tools/`; system tools and Git settings
are unchanged. The Git installation at `.runtime/tools/git/https-install/`
supports HTTPS. curl and OpenSSL are built locally and linked statically.
Build logs are retained there, including preceding logs when a build is rerun.

You can provide compatible tools with `--java /absolute/path/to/java --git
/absolute/path/to/git`. Copybara needs Java 25+ and Git supporting
`git init --object-format=sha1 --ref-format=files` (Git 2.45+). Publication also
checks for Git's HTTPS helper. This host's original Git 2.25.1 is too old, and
the previous preview-only Git build has no HTTPS support.

## Inspect a folder preview

```bash
candidate_commit=$(git rev-parse HEAD)
python3 scripts/export_preview.py "$candidate_commit" /absolute/path/to/new-preview
```

The destination must be a new directory outside the source checkout. Only
committed files are read. This uses `folder.destination()` and publishes
nothing. It records an exact file inventory, hashes, executable modes and a
privacy scan in `.runtime/export-config/<preview-name>/export-report.json`.

The shared allow-list in `scripts/export_policy.py` includes source, the MIT
license and notices, skills, tests, fixture generators, locks, operator docs,
sanitized acceptance evidence, and `.github/workflows/`. It excludes the
private contract, generated data, secrets, databases, backups, vendor trees,
tool binaries, and build outputs. The only tree transformation replaces the
source repository's clone/documentation URL with the public URL in README,
AGENTS, docs and skills. Every exported byte and executable bit is checked
against the exact source revision plus that transformation. Pattern checks for
possible credentials and personal machine paths supplement human review.

## Test the real Git migration locally

```bash
python3 scripts/export_release.py "$candidate_commit" /absolute/path/to/public-preview.git
CAVE_COPYBARA_INTEGRATION=1 python3 -m unittest discover -s tests -p test_release.py -v
```

The first command creates a **bare local Git repository**, using actual
Copybara `git.destination()` and `SQUASH` mode. It is not a GitHub publication.
Clone this resulting repository and run the final installation/browser checks
from its exact commit. A checked folder export alone does not establish that
the Git-exported installation works.

The integration test uses disposable local repositories to verify an initial
migration, a later update, addition/deletion of managed files, executable-mode
changes, preservation of a destination-owned community file, repeat-run
idempotence, and refusal to overwrite a public edit to a managed file. It also
checks that private commit messages, author email and commit ancestry do not
enter the new repository. Failed runs leave their reports/logs under
`.runtime/releases/` and do not count as successful migrations.

## Publish the verified revision

Create the intended **empty** public `PytorchConnectomics/cave-local`
repository after the release gates pass. Do not initialize it with a README or
license: Copybara supplies the reviewed files and starts with one clean root
commit. The publisher needs GitHub permission to push and GitHub CLI available.
Select the authorized account token for this process only, for example:

```bash
GH_TOKEN="$(gh auth token --user akgohain)" \
  python3 scripts/export_release.py "$candidate_commit" \
  https://github.com/PytorchConnectomics/cave-local.git --publish
```

The script allows publication to that exact HTTPS URL only. It checks the
GitHub repository identity, organization ownership and public visibility. It
does not create a repository, change the active GitHub account, write a token,
or change global Git settings. Credentials are read by the GitHub helper from
the process environment, with persistent credential stores disabled.

Publication proceeds as follows:

1. Fetch the current public `main`, if present, into a new isolated bare staging
   repository. No private source Git objects are copied into its ancestry.
2. Verify the current managed public files match the last imported source
   revision. Unrelated destination history or managed-file edits stop the run.
3. Run actual Copybara Git migration into staging, using the generated
   `copy.bara.sky` configuration. The first migration uses `--init-history` and
   `--force` only to create the absent **local staging** branch. Updates use
   neither flag and retain Copybara's `check_last_rev_state` check.
4. Independently verify the complete resulting tree, modes, allowed preserved
   files, privacy scan, source markers, and public ancestry. The initial
   migration must contain exactly one root commit. Generated public commits
   use the verified GitHub noreply identity, and a fixed release message rather
   than private development messages.
5. Push only that verified commit with a normal fast-forward Git push. A
   concurrent public branch change stops the release; the script never force
   pushes. Verify the destination `main` points to the exported commit.

Thus Copybara performs the actual tree/history migration; the wrapper pushes
its already checked Git object. It does not ask Copybara to publish an unchecked
result directly. The private audit report records `source_commit`,
`destination_commit`, repository identity, tool versions, complete file hashes,
preserved files and whether a network publication occurred.

After publication, install from the actual public clone URL and record its
commit. Tag `v0.1.0` as an initial single-server preview only after that check
passes. Keep the release notes clear about supported formats, editing policy,
host/platform limits and any reviewed dependency findings.

## Ship the next update

Develop and review the update in the source repository. Test its exact candidate
and repeat the folder/Git-export checks above. Invoke the same `export_release.py`
command with the new source SHA and the same destination; it fetches the public
state, finds `GitOrigin-RevId`, and makes one new squash commit rather than
re-importing private history. A repeat of an already imported source SHA with
an identical tree succeeds without adding a commit.

Files outside the managed allow-list, such as `community/`, remain byte-for-byte
unchanged. Managed files are maintained from the source repository. If someone
changes them directly in public, migration stops: bring those changes into the
source and deliberately reconcile the baseline before another release. It does
not silently discard them. Changes to the export policy itself may also require
a reviewed baseline reconciliation; do not bypass the check with a force push.

Copybara's [official workflow and Git destination reference](https://github.com/google/copybara/blob/master/docs/reference.md)
describes the underlying squash, managed-file and revision-marker behavior.
