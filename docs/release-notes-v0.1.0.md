# CAVE local v0.1.0 preview

Install a local CAVE proofreading stack from a fresh checkout and configure it
for your image and segmentation data. The package includes five agent skills
for setup, operation, backup/restore and maintenance, with executable commands
and checks that establish whether each job succeeded.

The initial release supports Linux x86-64, Docker Compose, three-dimensional
OME-Zarr v2 images and Neuroglancer precomputed integer segmentation. Local
inputs are mounted read-only; unsigned public HTTP(S) inputs are also supported.
Source metadata must establish aligned shape, spacing and zero origin. See
[the setup handoff](handoff.md) and [dataset configuration](datasets.md).

The local Spelunker viewer supports native merge/split controls, images,
segmentation and meshes. The source revision is pinned; a reviewed dependency
overlay updates three runtime packages and records exact lock hashes. Upstream
source code is retained unchanged, and locally built viewer assets include
upstream and third-party license notices.

Completed ingestion and edits survive restart. The package provides verified
backups, restoration into a separate installation, credential rotation, staged
updates and removal of an explicitly identified installation. New segmentation
versions use new installations; proofreading edits do not automatically migrate
between changed label volumes.

Default editing treats supplied source labels as indivisible units. Finer
upstream labels are needed to split within an object, or the operator must
explicitly choose the documented spatial subdivision policy. The graph uses
geometric adjacency and does not reconstruct learned affinity scores.

This preview uses a persistent Bigtable emulator, one shared editor identity,
loopback/SSH access and bounded local meshing. It does not provide authenticated
public hosting, petascale performance guarantees, private/signed cloud inputs,
arbitrary microscopy format conversion or automatic database upgrades.

Read [acceptance evidence](acceptance.md) for tests actually run and retained
failures. [Dependency review](dependencies.md) describes fixed and retained
findings and the limits of the review. See [upstream notices](../NOTICE.md) and
[the original-code license](../LICENSE) before redistributing components.

[The test guide](testing.md) provides the fast suite and complete disposable
stack/lifecycle suite. Tests cover supported data contracts, configuration and
source preservation, native editing and persistence, browser checks, backup and
recovery failures, and guarded Copybara publication. Full integration runs
create their own synthetic installations and retain failures for inspection.
