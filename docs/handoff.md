# From a new machine to your own dataset

This is the procedure to give Donglai or another researcher and their local
agent. It ends with a working local viewer, checks on their data, a verified
baseline backup and instructions for maintaining the installation.

## 1. Get the repository and give the agent the inputs

Clone the repository on the Linux x86-64 machine that will run CAVE:

```bash
git clone https://github.com/PytorchConnectomics/cave-local.git
cd cave-local
```

Ask the agent to read `skills/cave-local/SKILL.md` and follow this guide. It can
read the skills directly, or [install all five](skills.md) for later chats.
Keep the whole checkout: the skills use its scripts and documentation.

Provide the locations of your image and segmentation, the segmentation version
you want to edit, and whether its labels are already the smallest pieces you
want to manipulate. The agent should derive axes, shape and spacing from the
actual metadata, then explain any remaining choice it needs from you.

## 2. Check the machine and build

The supported host is Linux x86-64 with Docker/Compose v2 access, Git, Python
>=3.11 with venv, Node >=22.18 and npm. Python 3.11 and 3.14 were tested. Use a
Linux server when your personal computer runs macOS or Windows; the viewer
can be opened on that computer through SSH forwarding. Native macOS/Windows
server installation is outside this preview's tested support.

```bash
bin/cave doctor
bin/cave deps
.venv/bin/python scripts/check_browser.py
bin/cave build
```

`doctor` reports missing prerequisites and requires at least 20 GiB free for
builds and small examples. It is not a capacity estimate for your real data.
It reads CPU capacity from the Docker daemon, including a virtualized daemon's
allocation. New installations and restored copies cap their declared CPU
quotas to that capacity, while preserving defaults on larger machines.
The agent must separately account for ingested data, graph/edge arrays,
database volumes and backups. Graph chunks must fit memory; larger datasets
have not been established by the small-fixture acceptance runs.

The browser check launches an existing Playwright or system Chromium/Chrome
and records its version. If none is available on a supported Playwright host,
install with `.venv/bin/python -m playwright install --with-deps chromium` and
rerun the check. Linux browser libraries may need administrator access; the
pinned installer does not support every Linux version and rejects Ubuntu 20.
An existing working system browser can still be checked and used. When there
is no suitable browser or installation is unsupported,
use a working local/in-app browser with [the exact handoff procedure](browser-agent-test.md).

## 3. Prove the example works first

Choose an unused loopback port. These commands use 8181 and new directories:

```bash
bin/cave fixture .runtime/example-input --port 8181 --graph-id pkg_example
bin/cave create .runtime/example-input/manifest.json .runtime/example \
  --source-dir .runtime/example-input
bin/cave start .runtime/example
bin/cave validate .runtime/example --edit-fixture --browser
```

The fixture is invented data that may safely be edited. Acceptance checks the
graph, mesh generation, annotations and actual browser Merge/Split controls.
It restores the assigned original grouping; native root IDs can change after
edits. Preserve reports and screenshots, including failures.

For the manual-browser path, run the last command without `--browser`, then
run `bin/cave browser-handoff .runtime/example` and give the printed URL to
the browser agent. Complete workflows sequentially; finish other deployment
start/stop/recreate work before browser testing.

Do not move on until the example passes. Stop it when finished with
`bin/cave stop .runtime/example`; stopping preserves its files and volumes.

## 4. Inspect your data and choose how it can be edited

The accepted inputs are:

| Input | Supported form |
| --- | --- |
| Image | OME-Zarr v2, exactly three spatial axes in XYZ or ZYX order, explicit units and scale |
| Segmentation | Neuroglancer precomputed, one integer channel, zero background |
| Alignment | Same XYZ shape and physical spacing, zero origin |
| Location | A local read-only folder, or an unsigned publicly readable HTTP(S) source |

Channel/time axes, nonzero origins, Zarr v3, private cloud credentials and signed
expiring URLs are not supported. Other inputs need a separately validated
preparation step. This repository does not supply a universal converter or
crop command. Keep prepared outputs separate from the originals and verify
that preparation preserves axes, spacing, labels and image/label alignment.

If the full dataset is large, begin with an aligned small crop prepared using
your established data tools. Its zero-origin metadata must describe the crop;
retain the crop's location in the original dataset separately. This validates
the data handoff before committing to full ingestion.

Choose the editing basis deliberately:

- `source_labels` uses each connected supplied label as an indivisible editing
  unit. You can merge these units and split their graph connections; you cannot
  draw a new boundary through one supplied label. Use finer upstream labels
  when that is needed. Disconnected pieces initially form separate components.
- `spatial_tiles` additionally divides labels into connected pieces within
  chosen tiles. This permits splits along those extra boundaries. It is an
  explicit alternative, not a reconstruction of learned affinities. Set
  `atom_tile_xyz`; each dimension must divide its graph chunk dimension.
- `agglomerated_supervoxels` takes over-segmented supervoxels (for example the
  watershed fragments behind an ABISS or other agglomeration) plus a separate
  file mapping each supervoxel to its initial segment, with region-graph
  affinities. Proofreading starts from the agglomeration, and splits follow
  the real supervoxel boundaries, weighted by those affinities. See
  [dataset details](datasets.md#agglomerated-supervoxels).

The agent should report these choices before ingestion and stop at unsupported
metadata rather than silently rebasing, dropping axes or altering source data.
See [dataset details](datasets.md).

## 5. Configure and launch a separate installation

Start with [the manifest example](../templates/segmentation-iteration/manifest.example.json).
Save your own copy and fill in values from your metadata. The agent records
the image/label locations, graph ID, unused local port, XYZ shape, XYZ voxel
spacing in nanometers, graph chunks and editing policy. Do not carry the
example's `test_fixture` marker onto real data.

For a local folder `/srv/donglai-data` containing `image/` and `segmentation/`,
source URLs in the manifest are `file:///source/image/` and
`file:///source/segmentation/`. `/source` is the read-only container mount; it
is not the host folder name. Example commands, with a newly chosen destination:

```bash
bin/cave create /srv/donglai-data/manifest.json /srv/cave/donglai \
  --source-dir /srv/donglai-data
bin/cave start /srv/cave/donglai
bin/cave validate /srv/cave/donglai
```

For public HTTP(S) inputs, omit `--source-dir`. Use URLs reachable from the
containers, not a host `localhost` address. Keep remote sources immutable;
the installation does not archive them automatically.

Original sources are read-only. Graph edits, annotations and derived data live
in this deployment's own files and persistent volumes. A separate directory,
graph ID, port and generated `cave-pkg-*` Docker project keep installations
independent. Reuse `start` for restarts; a new segmentation iteration needs a
new installation, and edits are not automatically migrated between versions.

## 6. Check the actual data, save a backup and hand it over

`validate` without `--edit-fixture` captures a read-only graph/annotation
snapshot. It does not perform a full browser alignment check. Follow the
[read-only browser task](browser-agent-test.md) at representative tissue
locations, several slices and a graph chunk boundary. Check image,
segmentation and meshes, then reload and open a fresh tab. Do not perform
acceptance Merge/Split or annotation edits on the real graph.

Before regular editing begins, create and verify a baseline backup:

```bash
bin/cave backup /srv/cave/donglai /srv/cave-backups/donglai-baseline --include-source
bin/cave verify-backup /srv/cave-backups/donglai-baseline
```

`--include-source` copies local sources and can be large. Without it, retain
the original source location for restoration. For public HTTP inputs, omit
`--include-source`: there is no local source folder to archive. Keep those
sources available and immutable; that backup remains dependent on them.
Backup briefly stops this
installation for a consistent copy and restores its prior running state.
Backups contain credentials and private data; keep them in private storage.

The agent's final report should contain the repository commit, deployment path,
viewer URL, manifest and source provenance, editing policy, test evidence,
baseline backup path and verification result, and any unresolved limitation.
Include these everyday commands:

```bash
bin/cave status /srv/cave/donglai
bin/cave logs /srv/cave/donglai
bin/cave stop /srv/cave/donglai
bin/cave start /srv/cave/donglai
```

For a viewer on a different computer, use [SSH forwarding](operations.md).
The gateway remains loopback-only, with one shared editor identity and no user
authentication. Public hosting is unsupported. Use the operation, backup and
maintenance skills for subsequent work on this installation.
