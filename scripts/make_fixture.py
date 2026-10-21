"""Generate redistributable synthetic data with explicit expected label topology.

This generates invented arrays, rather than converting microscopy data, so a
custom writer is appropriate. No biological data or credentials are included.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np


def fixture_arrays(variant):
    if variant == "zyx":
        shape, resolution, unit, axes = [48, 40, 20], [8, 8, 24], "nanometer", ["z", "y", "x"]
    elif variant == "xyz":
        shape, resolution, unit, axes = [40, 48, 24], [12, 16, 40], "micrometer", ["x", "y", "z"]
    else:
        raise ValueError("variant must be zyx or xyz")
    labels = np.zeros(shape[::-1], dtype=np.uint64)
    # Two neighboring, single-chunk components for merge/split/undo.
    labels[2:6, 2:10, 2:7] = 7
    labels[2:6, 2:10, 7:12] = np.uint64(2**63 + 123)
    # Exact maximal uint64 ID exercises the label*tile_stride overflow bug.
    labels[5:17, 18:34, 18:36] = np.iinfo(np.uint64).max
    # Same source label, disconnected; it must stay a separate root.
    labels[2:6, 2:6, 32:36] = np.iinfo(np.uint64).max
    labels[2:6, 2:8, 14:20] = 17
    labels[9:13, 12:22, 3:7] = 2**32 + 11
    if variant == "xyz":
        labels[18:23, 36:47, 29:39] = 991
        labels[10:14, 31:40, 2:9] = 991
    zz, yy, xx = np.indices(labels.shape)
    image = ((xx * 5 + yy * 7 + zz * 11) % 256).astype(np.uint8)
    return labels, image, shape, resolution, unit, axes


def add_refinement_geometry(labels):
    """Add independent finite-contact pairs for retry and neighbour-lock tests."""
    for left, right, z, y in ((31, 32, 2, 12), (41, 42, 10, 12)):
        box = np.s_[z:z + 4, y:y + 4, 12:20]
        if np.any(labels[box]):
            raise ValueError("Refinement fixture geometry overlaps an existing object")
        labels[z:z + 4, y:y + 4, 12:16] = left
        labels[z:z + 4, y:y + 4, 16:20] = right


def create(output, variant="zyx", policy="source_labels", graph=None, port=8181, supervoxel_split=False):
    import cc3d
    import zarr
    from cloudvolume import CloudVolume
    from numcodecs import Blosc
    if output.exists():
        raise ValueError("Fixture output already exists; choose a fresh directory")
    labels, image, shape, resolution, unit, axes = fixture_arrays(variant)
    if supervoxel_split:
        if policy != "agglomerated_supervoxels":
            raise ValueError("The refinement fixture requires agglomerated_supervoxels")
        # Pair31/32 stays untouched until the cross-root neighbour-lock test.
        # Pair41/42 supplies a finite cut for the forced mincut-retry scenarios.
        add_refinement_geometry(labels)
    output.mkdir(parents=True)
    segmentation = CloudVolume("file://" + str(output / "segmentation"),
        info=CloudVolume.create_new_info(num_channels=1, layer_type="segmentation", data_type="uint64",
            encoding="compressed_segmentation", resolution=resolution, voxel_offset=[0, 0, 0],
            chunk_size=[16, 16, 8], volume_size=shape, compressed_segmentation_block_size=[8, 8, 8]),
        compress=False, fill_missing=False)
    segmentation.commit_info()
    segmentation[:] = labels.transpose(2, 1, 0)[..., None]
    if not np.array_equal(segmentation[:][..., 0].transpose(2, 1, 0), labels):
        raise RuntimeError("Synthetic precomputed segmentation readback failed")
    group = zarr.open_group(str(output / "image"), mode="w")
    layout = image if axes == ["z", "y", "x"] else image.transpose(2, 1, 0)
    image_array = group.create_dataset("0", data=layout,
        chunks=(8, 16, 16) if axes[0] == "z" else (16, 16, 8),
        compressor=Blosc(cname="zstd", clevel=3, shuffle=Blosc.SHUFFLE))
    factor = 1 if unit == "nanometer" else 1000
    scale_xyz = np.asarray(resolution, dtype=float) / factor
    scale = scale_xyz[::-1] if axes[0] == "z" else scale_xyz
    group.attrs["multiscales"] = [{"version": "0.4", "name": "synthetic-intensity",
        "axes": [{"name": axis, "type": "space", "unit": unit} for axis in axes],
        "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": scale.tolist()}]}]}]
    if not np.array_equal(image_array[:], layout):
        raise RuntimeError("Synthetic image Zarr readback failed")
    expected = []
    for label in np.unique(labels):
        if label:
            components = cc3d.connected_components(labels == label, connectivity=6)
            sizes = np.bincount(components.reshape(-1))[1:]
            expected.append({"source_label": str(int(label)), "connected_components": int(components.max()),
                             "component_voxel_counts": sorted(int(n) for n in sizes),
                             "total_voxels": int(np.count_nonzero(labels == label))})
    manifest = {"graph_id": graph or "pkg_fixture_" + variant, "test_fixture": True,
        "local_url": "http://127.0.0.1:" + str(port), "source_dir": str(output),
        "image_https": "file:///source/image", "segmentation_https": "file:///source/segmentation",
        "shape_xyz": shape, "resolution_nm_xyz": resolution,
        "graph_chunk_xyz": [16, 16, 8], "atom_policy": policy, "voxel_offset_xyz": [0, 0, 0]}
    if policy == "spatial_tiles":
        manifest["atom_tile_xyz"] = [8, 8, 4]
    if policy == "agglomerated_supervoxels":
        # Labels 7 and 2**63+123 touch; they start as one segment joined by a 0.9 region-graph edge.
        # Every other label is ungrouped (segment 0) and starts as its own root.
        fragment = np.unique(labels[labels != 0])
        segment = np.where(np.isin(fragment, np.array([7, 2**63 + 123], np.uint64)), np.uint64(1), np.uint64(0))
        rg_a, rg_b, rg_aff = [7], [2**63 + 123], [0.9]
        if supervoxel_split:
            for group_id, left, right in ((2, 31, 32), (3, 41, 42)):
                segment[np.isin(fragment, np.array([left, right], np.uint64))] = group_id
                rg_a.append(left); rg_b.append(right); rg_aff.append(0.8)
        np.savez(output / "agglomeration.npz", fragment=fragment, segment=segment,
                 rg_a=np.array(rg_a, np.uint64), rg_b=np.array(rg_b, np.uint64), rg_aff=np.array(rg_aff))
        manifest["agglomeration_https"] = "file:///source/agglomeration.npz"
    if supervoxel_split:
        manifest["supervoxel_split"] = {"enabled": True, "grid_xyz": [4, 4, 4],
                                       "max_atoms": 64, "max_seconds": 30}
    for name, data in [("manifest.json", manifest), ("expected.json", {
        "variant": variant, "shape_xyz": shape, "axes": axes, "unit": unit,
        "foreground_voxels": int(np.count_nonzero(labels)), "labels": expected,
        "source_zyx_uint64_sha256": hashlib.sha256(labels.astype("<u8").tobytes()).hexdigest()})]:
        (output / name).write_text(json.dumps(data, indent=2) + "\n")
    print(output / "manifest.json")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--variant", choices=["zyx", "xyz"], default="zyx")
    parser.add_argument("--atom-policy", choices=["source_labels", "spatial_tiles", "agglomerated_supervoxels"], default="source_labels")
    parser.add_argument("--graph-id")
    parser.add_argument("--port", type=int, default=8181)
    parser.add_argument("--supervoxel-split", action="store_true")
    args = parser.parse_args()
    create(args.output.resolve(), args.variant, args.atom_policy, args.graph_id, args.port, args.supervoxel_split)
