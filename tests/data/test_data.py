"""Regression coverage for input semantics and actual on-disk source roundtrips."""
import functools
import http.server
import json
import sys
import threading
from pathlib import Path

import cc3d
import numpy as np
import pytest
from cloudvolume import CloudVolume

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from ingest_segmentation_iteration import (component_keys, data_spec, image_geometry,
    source_volume, validate_geometry)
from make_fixture import create, fixture_arrays


def host_spec(manifest, output):
    spec = dict(manifest)
    spec["image_https"] = (output / "image").as_uri()
    spec["segmentation_https"] = (output / "segmentation").as_uri()
    return spec


@pytest.mark.parametrize("variant", ["zyx", "xyz"])
@pytest.mark.parametrize("policy", ["source_labels", "spatial_tiles"])
def test_real_fixture_roundtrip_and_physical_metadata(tmp_path, variant, policy):
    output = tmp_path / "source"
    manifest = create(output, variant, policy)
    expected, image, shape, resolution, unit, axes = fixture_arrays(variant)
    spec = host_spec(manifest, output)
    _, geometry = image_geometry(spec)
    assert geometry["axes"] == axes
    assert geometry["shape_xyz"] == shape
    assert geometry["resolution_nm_xyz"] == resolution
    volume = source_volume(spec)
    actual = volume[:][..., 0].transpose(2, 1, 0)
    assert np.array_equal(actual, expected)
    assert actual.max() == np.iinfo(np.uint64).max
    declared = json.loads((output / "expected.json").read_text())
    max_label = next(row for row in declared["labels"] if row["source_label"] == str(2**64 - 1))
    assert max_label["connected_components"] == 2
    assert manifest["test_fixture"] is True


def test_public_http_metadata_and_segmentation_roundtrip(tmp_path):
    output = tmp_path / "source"
    manifest = create(output, "xyz")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(output))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        spec = dict(manifest, image_https=f"http://127.0.0.1:{server.server_port}/image/",
                    segmentation_https=f"http://127.0.0.1:{server.server_port}/segmentation")
        _, geometry = image_geometry(spec)
        assert geometry["resolution_nm_xyz"] == [12, 16, 40]
        actual = source_volume(spec)[:][..., 0].transpose(2, 1, 0)
        assert np.array_equal(actual, fixture_arrays("xyz")[0])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_spatial_keys_do_not_merge_uint64_overflow_collision():
    chunk = np.array([16, 16, 8])
    tile = np.array([8, 8, 4])
    stride = 9
    inverse = pow(stride, -1, 2**64)
    collision_label = (8 * inverse) % 2**64
    labels = np.ones((8, 16, 16), dtype=np.uint64)
    labels[:, :, 8:] = collision_label
    keys = component_keys(labels, "spatial_tiles", chunk, tile)
    assert keys[0, 0, 7] != keys[0, 0, 8]
    # The old arithmetic collides at the touching x tile boundary.
    assert (1 * stride + 1) % 2**64 == (collision_label * stride + 2) % 2**64
    components = cc3d.connected_components(keys, connectivity=6)
    for component in np.unique(components):
        assert len(np.unique(labels[components == component])) == 1
    assert components.max() == 8


def test_maximal_label_can_be_tiled_without_background_or_dtype_loss():
    labels = np.full((8, 16, 16), np.iinfo(np.uint64).max, dtype=np.uint64)
    labels[0, 0, 0] = 0
    keys = component_keys(labels, "spatial_tiles", [16, 16, 8], [8, 8, 4])
    assert np.array_equal(keys == 0, labels == 0)
    assert cc3d.connected_components(keys, connectivity=6).max() == 8
    assert np.array_equal(component_keys(labels, "source_labels", [16, 16, 8]), labels)


def test_nonzero_origins_and_signed_labels_rejected(tmp_path):
    output = tmp_path / "source"
    manifest = create(output)
    with pytest.raises(ValueError, match="Nonzero voxel origins"):
        validate_geometry(dict(manifest, voxel_offset_xyz=[1, 0, 0]))
    with pytest.raises(ValueError, match="unsigned"):
        component_keys(np.array([[[-1]]], dtype=np.int64), "source_labels", [16, 16, 8])
    info = json.loads((output / "segmentation/info").read_text())
    info["scales"][0]["voxel_offset"] = [0, 0, 2]
    (output / "segmentation/info").write_text(json.dumps(info))
    with pytest.raises(ValueError, match="voxel offsets"):
        source_volume(host_spec(manifest, output))


@pytest.mark.parametrize("mutation,reason", [
    ("missing_unit", "explicit spatial unit"),
    ("bad_axes", "exactly ZYX or XYZ"),
    ("translation", "Nonzero image translations"),
    ("wrong_scale", "resolution"),
    ("unknown_transform", "Unsupported image coordinate transform"),
])
def test_image_metadata_errors_are_explicit(tmp_path, mutation, reason):
    output = tmp_path / "source"
    manifest = create(output)
    attrs_path = output / "image/.zattrs"
    attrs = json.loads(attrs_path.read_text())
    metadata = attrs["multiscales"][0]
    if mutation == "missing_unit":
        metadata["axes"][0].pop("unit")
    elif mutation == "bad_axes":
        metadata["axes"][0]["name"] = "c"
    elif mutation == "translation":
        metadata["datasets"][0]["coordinateTransformations"].append({"type": "translation", "translation": [0, 1, 0]})
    elif mutation == "wrong_scale":
        metadata["datasets"][0]["coordinateTransformations"][0]["scale"][0] *= 2
    else:
        metadata["datasets"][0]["coordinateTransformations"].append({"type": "rotation"})
    attrs_path.write_text(json.dumps(attrs))
    with pytest.raises(ValueError, match=reason):
        image_geometry(host_spec(manifest, output))


def test_global_scale_composes_and_nanometer_units_are_normalized(tmp_path):
    output = tmp_path / "source"
    manifest = create(output, "xyz")
    attrs_path = output / "image/.zattrs"
    attrs = json.loads(attrs_path.read_text())
    metadata = attrs["multiscales"][0]
    metadata["coordinateTransformations"] = [{"type": "scale", "scale": [2, 2, 2]}]
    metadata["datasets"][0]["coordinateTransformations"][0]["scale"] = [0.006, 0.008, 0.02]
    attrs_path.write_text(json.dumps(attrs))
    assert image_geometry(host_spec(manifest, output))[1]["resolution_nm_xyz"] == [12, 16, 40]


def test_data_identity_allows_runtime_relocation_but_rejects_changed_atom_semantics():
    common = {"graph_id": "fixture", "shape_xyz": [48, 40, 20],
              "graph_chunk_xyz": [16, 16, 8], "resolution_nm_xyz": [8, 8, 24]}
    assert data_spec(common) == data_spec(dict(common, local_url="http://localhost:8182", source_dir="/new/path", project_name="cave-pkg-restored"))
    assert data_spec(common) != data_spec(dict(common, atom_policy="spatial_tiles", atom_tile_xyz=[8, 8, 4]))


def test_fresh_storage_chunks_accept_each_graph_chunk_and_clipped_edges(tmp_path):
    # A real compressed-precomputed writer catches alignment errors that mocks
    # missed: graph-sized writes must align with their storage-sized chunks.
    shape, chunk = np.array([40, 48, 24]), np.array([16, 16, 8])
    volume = CloudVolume((tmp_path / "atomic").as_uri(), info=CloudVolume.create_new_info(
        num_channels=1, layer_type="segmentation", data_type="uint64", encoding="compressed_segmentation",
        resolution=[12, 16, 40], voxel_offset=[0, 0, 0], chunk_size=chunk.tolist(),
        volume_size=shape.tolist(), compressed_segmentation_block_size=[8, 8, 8]),
        compress=True, fill_missing=False)
    volume.commit_info()
    expected = np.zeros(tuple(shape), dtype=np.uint64)
    for x in range(0, shape[0], chunk[0]):
        for y in range(0, shape[1], chunk[1]):
            for z in range(0, shape[2], chunk[2]):
                low = np.array([x, y, z])
                high = np.minimum(low + chunk, shape)
                slices = tuple(slice(int(a), int(b)) for a, b in zip(low, high))
                value = np.uint64(2**63 + x * 100 + y * 10 + z)
                block = np.full(tuple(high - low), value, dtype=np.uint64)
                volume[slices] = block[..., None]
                expected[slices] = block
    assert np.array_equal(volume[:][..., 0], expected)


def test_fixture_output_never_overwrites_existing_data(tmp_path):
    create(tmp_path / "fixture")
    with pytest.raises(ValueError, match="already exists"):
        create(tmp_path / "fixture")
