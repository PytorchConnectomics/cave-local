# Bring your own image and labels

The image and segmentation must describe the same physical volume. Obtain
shape, axes, origin and spacing from their metadata; a filename is not evidence
of voxel spacing. Installation reads the original sources without writing them.

## Accepted data

- Image: OME-Zarr v2, exactly three spatial axes in XYZ or ZYX order. Each axis
  has an explicit spatial unit and the first declared level has a scale.
  Nanometers, micrometers, millimeters and meters are normalized to nanometers.
- Labels: Neuroglancer precomputed segmentation, one integer channel, zero as
  background. Nonconsecutive IDs and full uint64 IDs are preserved in the
  recorded mapping. Native graph atom/root IDs differ from the original IDs.
- Both: same XYZ shape, zero origin and matching XYZ spacing. Nonzero origins,
  channel/time axes, unknown units, Zarr v3 and missing/misaligned data are
  rejected. This release does not silently convert or rebase your sources.

Use a supported upstream data converter for other inputs, keep the converted
output separate, and verify its metadata before bringing it here. Native graph
ID assignment and construction are performed by this repository's adapter.

## Manifest and local folders

```json
{
  "graph_id": "my_dataset_001",
  "local_url": "http://127.0.0.1:8181",
  "display_name": "My dataset",
  "image_https": "file:///source/image/",
  "segmentation_https": "file:///source/segmentation/",
  "shape_xyz": [48, 40, 20],
  "resolution_nm_xyz": [100, 100, 300],
  "graph_chunk_xyz": [16, 16, 8],
  "atom_policy": "source_labels",
  "voxel_offset_xyz": [0, 0, 0]
}
```

These geometry values are examples; replace them with the real metadata.
Despite their historical names, `image_https` and `segmentation_https` accept
local `file://` and public `http://` as well as `https://` sources.

For a host folder `/srv/my-data` containing `image/` and `segmentation/`, pass
`--source-dir /srv/my-data`. The containers see these as `/source/image/` and
`/source/segmentation/`; the host path does not belong in the source URLs.

```bash
bin/cave create /srv/my-data/manifest.json /srv/cave/iteration-001 \
  --source-dir /srv/my-data
bin/cave start /srv/cave/iteration-001
bin/cave validate /srv/cave/iteration-001
```

The gateway serves the image at `/image/`. Original labels are converted into
immutable native atoms and an editable graph in the deployment's `data/` and
database volumes. Do not mark real data as `test_fixture` to run edit tests.

## HTTP and cloud-hosted data

Use unsigned, publicly readable HTTP(S) directory URLs that end at the image
Zarr group and segmentation precomputed root. The gateway proxies the image;
ingestion reads labels. A readable public GCS HTTPS URL is one such source.
Private buckets, signed expiring URLs and mounting personal cloud credentials
inside these containers are not implemented by this release.

For development, container `127.0.0.1` refers to the container itself. Use a
read-only local source mount or a network address reachable from the deployment,
rather than a host loopback HTTP URL.

## Editing policy and chunk size

`source_labels` treats supplied labels as immutable supervoxels. Connected
fragments crossing graph chunk boundaries are joined with uncuttable edges.
Disconnected pieces of the same label initially remain separate components.
The split guard forbids a source ID in both split groups, even if its
disconnected pieces were subsequently merged. Use finer upstream labels if
you need to split within a supplied label.

`spatial_tiles` is an explicit alternative that subdivides labels into connected
pieces bounded by `atom_tile_xyz`. It preserves label identity and foreground,
but permits splits along those additional internal boundaries. Every tile
dimension must divide its graph chunk dimension. Set the policy deliberately.

### Agglomerated supervoxels

`agglomerated_supervoxels` is for a segmentation produced by agglomerating
finer supervoxels, such as watershed fragments and their region graph. Supply
the supervoxels as the precomputed segmentation, and the agglomeration as one
NumPy `.npz` file named by `agglomeration_https` (a `file:///source/...` path or
public URL):

| Array | Meaning |
| --- | --- |
| `fragment` | every supervoxel ID in the segmentation, unique, nonzero |
| `segment` | its initial segment; `0` = ungrouped (starts as its own root) |
| `rg_a`, `rg_b`, `rg_aff` | region-graph affinity in [0, 1] between supervoxel pairs |

```json
{"atom_policy": "agglomerated_supervoxels",
 "agglomeration_https": "file:///source/agglomeration.npz",
 "agglomeration_default_affinity": 0.001}
```

Supervoxels are indivisible by default (as with `source_labels`; the split guard
rejects one supervoxel in both seed groups). The optional
[on-demand refinement switch](operations.md#on-demand-supervoxel-refinement-opt-in)
allows an involved atom to be subdivided during a split. Touching supervoxels of the same initial
segment are joined with their region-graph affinity; joined contacts absent
from the region graph get `agglomeration_default_affinity` (default 0.001), so
the split tool cuts weak boundaries first. Contacts between different segments
start unjoined; `data/region_affinity.npy` records the affinity of every
contact, aligned with `data/region_adjacency.npy`, as a candidate-merge list.
The separate [paint switch](operations.md#painted-cuts-opt-in) refines only
current atoms touched by both painted labels, then cuts between all painted
source and sink atoms. Both switches are opt-in operational settings; original
source labels remain read-only. A refused paint cut can retain a refinement
that preserves the object. This API does not add a brush to Spelunker.
Ingestion rejects supervoxels missing from the mapping and records how many
segments start as several roots because their supervoxels do not touch inside
the volume (common at crop edges). Supervoxel and segment IDs may use the full
uint64 range.

Choose graph chunks that fit available memory. The native grid must contain
at least two chunks along one axis and fit within the supported graph ID
coordinate limits. Derived storage chunks follow this grid, including clipped
chunks at the volume edge. Initial ingestion builds geometric face adjacency.
Only `agglomerated_supervoxels` carries learned affinities and an initial
grouping into the graph; the other policies do not recreate them.

## Another segmentation iteration

Use a new manifest, graph ID, directory and port. Keep the earlier installation
and edits until you deliberately retire it. New source labels define a new
editing basis; this package does not automatically transfer proofreading edits
between changed label volumes. Source files should remain immutable throughout
installation, operation and backup.
