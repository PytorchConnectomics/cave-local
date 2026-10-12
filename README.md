# CAVE local — v0.1.0 preview

Install a CAVE proofreading stack on your own Linux server and let an agent
set it up for your data and maintain it using the five skills shipped here.
The viewer and data services run on your machine.

The stack uses upstream PyChunkedGraph, CAVE annotation/materialization services,
PostGIS, Redis, a persistent development Bigtable emulator, and a pinned local
Spelunker viewer with a recorded dependency overlay. This is a single-server
preview, with one shared editor identity and a loopback-only gateway.

## Set it up with your agent

Give your agent access to a clone of this repository and this request:

> Read `skills/cave-local/SKILL.md` and follow `docs/handoff.md` to install CAVE
> on this machine. Prove it works with the disposable example first. Then
> inspect my image and segmentation metadata, explain the supported editing
> choices, and prepare a separate installation for my data. Keep my original
> sources read-only and use read-only browser checks on my real dataset.
> Finish with the viewer URL, configuration, test results, backup location,
> maintenance commands and any unresolved limitations.

[The handoff guide](docs/handoff.md) gives the complete sequence and what data
locations to provide. Skills can be read directly, or [installed in your
agent](docs/skills.md) for later use.

## Try the example yourself

Prerequisites: Linux x86-64, Docker with Compose v2 and permission to use it,
Git, Python >=3.11 with venv, Node >=22.18, npm, and network access to GitHub,
Docker Hub, npm and PyPI. Python 3.14 may need a C/C++ compiler for packages
without wheels. Allow at least 20 GiB free for builds and small fixtures; real
dataset ingestion needs additional disk/RAM. Acceptance evidence covers Python
3.11 and 3.14 on Linux x86-64; other host platforms have not been validated.
`doctor` reports CPUs available to the Docker daemon. New installations and
restored copies cap each declared service quota to that capacity; higher-CPU
machines retain the template defaults. This does not size memory or real data.

```bash
git clone https://github.com/PytorchConnectomics/cave-local.git
cd cave-local
bin/cave doctor
bin/cave deps
.venv/bin/python scripts/check_browser.py
bin/cave build
bin/cave fixture .runtime/example-input
bin/cave create .runtime/example-input/manifest.json .runtime/example \
  --source-dir .runtime/example-input
bin/cave start .runtime/example
bin/cave validate .runtime/example --edit-fixture --browser
```

`check_browser.py` launches an existing Playwright or system Chromium/Chrome
and reports its actual version. If none is available on a Playwright-supported
OS, install it with `.venv/bin/python -m playwright install --with-deps chromium`
and rerun the check. That installer may need administrator access and does not
support every Linux distribution; the pinned installer rejects Ubuntu 20.
Use an existing working system browser or the
[browser-agent procedure](docs/browser-agent-test.md) when installation is
unsupported, and run fixture validation without `--browser` before handing off
the browser check. Keep
other deployment start/stop/recreate jobs paused during browser acceptance.

Open the URL printed by `start`. Shift+C activates Split, Shift+M activates Merge
and G switches split groups in the local upstream viewer. Graph editing uses
whole supervoxels. Under `source_labels`, one supplied label cannot be
subdivided; finer upstream labels are needed for that. Labels with disconnected
pieces initially have distinct graph components.

The validation command above makes real edits only on the generated disposable
fixture. It checks graph behavior, mesh generation, annotation snapshots and
browser behavior. For your own data, use read-only `bin/cave validate DEPLOYMENT`.

Run `bin/cave browser-handoff DEPLOYMENT` on a disposable fixture to produce
exact instructions for your own browser agent. See [browser agent testing](docs/browser-agent-test.md).

## Your own dataset

Follow [the handoff guide](docs/handoff.md) and [dataset configuration](docs/datasets.md).
Start from [the manifest format](templates/segmentation-iteration/manifest.example.json).
Set the actual graph ID, unused loopback port, image and label sources, XYZ
shape, XYZ spacing in nanometers and graph chunks using the source metadata.

Image inputs are OME-Zarr v2 with exactly three spatial axes in ZYX or XYZ order,
explicit units and zero origin. Label inputs are Neuroglancer precomputed
segmentation. Both sources must align. Local sources use `file:///source/...`
and are mounted read-only through `--source-dir`. Unsigned public HTTP(S)
sources are supported; credentials and private cloud buckets are outside the
current support envelope. Channel/time axes, nonzero origins and Zarr v3 are
rejected explicitly. Keep source datasets immutable.

Every deployment has its own directory, port, generated secrets and persistent
volumes. Restart preserves edits. Changed data configuration and interrupted
ingestion require inspection rather than silently replacing an existing graph.
Create a new deployment for new label iterations; edits are not automatically
migrated between segmentation versions.

## Agent instructions and operation

Read [skills](docs/skills.md) to install the repo's agent skills. The entry skill
routes requests to setup, operations, backup/restore and maintenance instructions.
They use the same executable commands as the example above.

```bash
bin/cave status DEPLOYMENT
bin/cave logs DEPLOYMENT
bin/cave stop DEPLOYMENT
bin/cave start DEPLOYMENT
bin/cave backup DEPLOYMENT BACKUP_DIRECTORY --include-source
bin/cave verify-backup BACKUP_DIRECTORY
bin/cave restore BACKUP_DIRECTORY NEW_DEPLOYMENT --port NEW_PORT
```

See [operation and recovery](docs/operations.md). Backups contain private data
and credentials. Include local source inputs when you need a self-contained
restore; otherwise supply their location when restoring.

For another computer, forward the loopback gateway over SSH. The current shared
editor setup has no user authentication; direct public access is unsupported.
Large-root meshes are bounded to 200 million voxels and generated locally;
the emulator and meshing adapter are not a production petascale backend.

## Development and acceptance

[Release preparation](docs/release.md) describes the required repeat installs,
skill evaluations and the Copybara publishing path. `upstream.lock.json` pins
fetched sources; `images.lock.json` and the Dockerfiles pin base images. Host
dependencies are pinned in `requirements.lock.txt`. Generated inputs, secrets,
volumes, vendor trees and builds are excluded from Git.

Pull requests run the complete unit/data suite and check skill metadata and
documentation references on Python 3.11 and 3.14. Maintainers can separately
dispatch the disposable full-stack/browser workflow; it needs enough runner
disk/RAM and does not replace release acceptance. Tests and reports distinguish
evidence actually collected from work still planned.

[The test-suite guide](docs/testing.md) gives the fast command and the full
sequential Docker/browser/lifecycle command, their evidence and explicit limits.

Read [the acceptance results](docs/acceptance.md) for successful fresh
installations, independent skill evaluation, retained failures and tested limits.
Read [dependency review](docs/dependencies.md) for the viewer overlay and retained
findings, and [NOTICE](NOTICE.md) for upstream licenses and credits.
