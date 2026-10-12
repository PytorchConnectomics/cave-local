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

Choose graph chunks that fit available memory. The native grid must contain
at least two chunks along one axis and fit within the supported graph ID
coordinate limits. Derived storage chunks follow this grid, including clipped
chunks at the volume edge. Initial ingestion builds geometric face adjacency;
it does not recreate learned affinity scores or the original model hierarchy.

## Another segmentation iteration

Use a new manifest, graph ID, directory and port. Keep the earlier installation
and edits until you deliberately retire it. New source labels define a new
editing basis; this package does not automatically transfer proofreading edits
between changed label volumes. Source files should remain immutable throughout
installation, operation and backup.
