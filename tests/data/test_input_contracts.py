"""Real source readers reject ambiguous data and preserve label semantics.

These checks deliberately do not import the native database client: full graph
construction is exercised by the disposable-stack acceptance tests.
"""
import copy
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
from cloudvolume.exceptions import EmptyVolumeException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from ingest_segmentation_iteration import (component_keys, image_geometry,
                                           source_volume, validate_geometry)
from make_fixture import create


@pytest.fixture
def image_source(tmp_path):
    root = tmp_path / "source"
    manifest = create(root, "xyz")
    spec = dict(manifest, image_https=(root / "image").as_uri(),
                segmentation_https=(root / "segmentation").as_uri())
    return root, spec


def mutate_json(path, change):
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


@pytest.mark.parametrize("unit,factor", [
    ("nm", 1), ("nanometre", 1), ("um", 1_000), ("µm", 1_000),
    ("micrometre", 1_000), ("mm", 1_000_000), ("m", 1_000_000_000),
])
def test_declared_units_are_composed_per_axis_without_changing_source(image_source, unit, factor):
    root, spec = image_source
    attrs = root / "image/.zattrs"
    def change(metadata):
        level = metadata["multiscales"][0]
        for axis in level["axes"]:
            axis["unit"] = unit
        level["datasets"][0]["coordinateTransformations"] = [
            {"type": "scale", "scale": [v / factor for v in spec["resolution_nm_xyz"]]},
            {"type": "translation", "translation": [0, 0, 0]},
        ]
    mutate_json(attrs, change)
    before = {path: path.read_bytes() for path in (root / "image").rglob("*") if path.is_file()}
    geometry = image_geometry(spec)[1]
    assert geometry["shape_xyz"] == [40, 48, 24]
    assert np.allclose(geometry["resolution_nm_xyz"], [12, 16, 40])
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize("case,reason", [
    ("no_multiscales", "exactly one"),
    ("multiple_multiscales", "exactly one"),
    ("time_axis", "exactly ZYX or XYZ"),
    ("nonspatial_axis", "explicit spatial unit"),
    ("unknown_unit", "explicit spatial unit"),
    ("no_levels", "no array datasets"),
    ("absolute_array", "relative path"),
    ("parent_array", "relative path"),
    ("no_scale", "explicit scale"),
    ("bad_scale", "finite positive"),
    ("short_translation", "three finite"),
    ("infinite_translation", "three finite"),
])
def test_ambiguous_ngff_metadata_is_rejected_before_any_source_write(image_source, case, reason):
    root, spec = image_source
    attrs = root / "image/.zattrs"
    def change(value):
        metadata = value["multiscales"][0]
        level = metadata["datasets"][0]
        if case == "no_multiscales":
            value.pop("multiscales")
        elif case == "multiple_multiscales":
            value["multiscales"].append(copy.deepcopy(metadata))
        elif case == "time_axis":
            metadata["axes"].append({"name": "t", "type": "time", "unit": "second"})
        elif case == "nonspatial_axis":
            metadata["axes"][0]["type"] = "time"
        elif case == "unknown_unit":
            metadata["axes"][0]["unit"] = "pixel"
        elif case == "no_levels":
            metadata["datasets"] = []
        elif case == "absolute_array":
            level["path"] = "/outside"
        elif case == "parent_array":
            level["path"] = "sub/../../outside"
        elif case == "no_scale":
            level["coordinateTransformations"] = []
        elif case == "bad_scale":
            level["coordinateTransformations"][0]["scale"] = [0.012, 0, 0.04]
        else:
            level["coordinateTransformations"].append({"type": "translation", "translation":
                [0, 0] if case == "short_translation" else [0, float("inf"), 0]})
    mutate_json(attrs, change)
    before = attrs.read_bytes()
    with pytest.raises(ValueError, match=reason):
        image_geometry(spec)
    assert attrs.read_bytes() == before


@pytest.mark.parametrize("field,value,reason", [
    ("zarr_format", 3, "Zarr v2"),
    ("shape", [48, 40, 24], "differs from shape_xyz"),
    ("shape", [40, 48, 24, 1], "three numbers"),
    ("chunks", [16, 0, 8], "finite positive"),
    ("dtype", "|b1", "Image dtype"),
    ("dtype", "<c8", "Image dtype"),
])
def test_zarr_array_contract_is_checked_not_inferred(image_source, field, value, reason):
    root, spec = image_source
    mutate_json(root / "image/0/.zarray", lambda array: array.update({field: value}))
    with pytest.raises(ValueError, match=reason):
        image_geometry(spec)


@pytest.mark.parametrize("field,value,reason", [
    ("type", "image", "one-channel segmentation"),
    ("num_channels", 2, "one-channel segmentation"),
    ("data_type", "int64", "unsigned integer labels"),
    ("data_type", "float32", "unsigned integer labels"),
    ("size", [41, 48, 24], "size differs"),
    ("resolution", [12, 16, 41], "resolution differs"),
])
def test_precomputed_metadata_must_match_the_declared_editing_volume(image_source, field, value, reason):
    root, spec = image_source
    def change(info):
        if field in ("size", "resolution"):
            info["scales"][0][field] = value
        else:
            info[field] = value
        # Raw storage permits CloudVolume to parse every integer/float type;
        # rejection here must be the adapter's label contract, not its codec.
        info["scales"][0]["encoding"] = "raw"
    mutate_json(root / "segmentation/info", change)
    with pytest.raises(ValueError, match=reason):
        source_volume(spec)


@pytest.mark.parametrize("dtype", ["uint8", "uint16", "uint32", "uint64"])
def test_unsigned_source_labels_roundtrip_including_clipped_chunks(tmp_path, dtype):
    shape = [5, 4, 3]
    root = tmp_path / dtype
    labels = np.zeros(shape, dtype=dtype)
    labels[0:2, 0:2, 0:2] = 1
    labels[3:5, 2:4, 1:3] = np.iinfo(dtype).max
    writer = CloudVolume(root.as_uri(), info=CloudVolume.create_new_info(
        num_channels=1, layer_type="segmentation", data_type=dtype, encoding="raw",
        resolution=[8, 12, 20], voxel_offset=[0, 0, 0], chunk_size=[3, 3, 2], volume_size=shape),
        compress=False, fill_missing=False)
    writer.commit_info()
    writer[:] = labels[..., None]
    volume = source_volume({"segmentation_https": root.as_uri(), "shape_xyz": shape,
                           "resolution_nm_xyz": [8, 12, 20], "graph_chunk_xyz": [3, 3, 2]})
    assert np.array_equal(volume[:][..., 0], labels)
    assert np.array_equal(volume[3:5, 3:4, 2:3][..., 0], labels[3:5, 3:4, 2:3])
    assert np.array_equal(component_keys(labels.transpose(2, 1, 0), "source_labels", [3, 3, 2]),
                          labels.transpose(2, 1, 0))


def test_missing_source_chunks_raise_instead_of_becoming_background(image_source):
    root, spec = image_source
    chunk = next(path for path in (root / "segmentation").rglob("*")
                 if path.is_file() and path.name != "info")
    chunk.unlink()
    volume = source_volume(spec)
    with pytest.raises(EmptyVolumeException) as failure:
        volume[:]
    assert str(chunk.relative_to(root / "segmentation")) in str(failure.value)


@pytest.mark.parametrize("change,reason", [
    ({"shape_xyz": [16, 16, 8]}, "at least two graph chunks"),
    ({"shape_xyz": [16 * 1024 + 1, 16, 8]}, "10-bit chunk"),
    ({"shape_xyz": [2**31, 40, 20]}, "integer dimensions"),
    ({"atom_policy": "spatial_tiles"}, "explicit atom_tile_xyz"),
    ({"atom_policy": "spatial_tiles", "atom_tile_xyz": [7, 8, 4]}, "divide graph_chunk"),
])
def test_native_grid_and_editor_basis_limits_are_explicit(change, reason):
    spec = {"shape_xyz": [48, 40, 20], "graph_chunk_xyz": [16, 16, 8], "resolution_nm_xyz": [8, 8, 24]}
    with pytest.raises(ValueError, match=reason):
        validate_geometry(dict(spec, **change))


def test_largest_supported_grid_and_clipped_dimensions_are_not_rounded_away():
    shape, chunk, _, tile, counts = validate_geometry({"shape_xyz": [16 * 1024, 17, 9],
        "graph_chunk_xyz": [16, 16, 8], "resolution_nm_xyz": [8, 8, 24]})
    assert shape.tolist() == [16 * 1024, 17, 9]
    assert counts.tolist() == [1024, 2, 2]
    assert tile is None


@pytest.mark.parametrize("policy", ["source_labels", "spatial_tiles"])
def test_atom_partition_preserves_each_source_label_and_uses_face_connectivity(policy):
    labels = np.zeros((5, 7, 9), dtype=np.uint64)
    labels[0:3, 0:3, 0:3] = np.iinfo(np.uint64).max
    labels[3:5, 3:5, 3:5] = np.iinfo(np.uint64).max  # only corner contact
    labels[1:4, 5:7, 5:9] = 2**63 + 19
    original = labels.copy()
    keys = component_keys(labels, policy, [12, 8, 8], [3, 4, 4])
    components = cc3d.connected_components(keys, connectivity=6)
    assert np.array_equal(labels, original)
    assert np.array_equal(components == 0, labels == 0)
    for component in np.unique(components)[1:]:
        selection = components == component
        assert np.unique(labels[selection]).size == 1
        if policy == "spatial_tiles":
            z, y, x = np.where(selection)
            assert np.unique(x // 3).size == np.unique(y // 4).size == np.unique(z // 4).size == 1
    # Same maximal label at a corner contact remains two independent objects.
    assert components[2, 2, 2] != components[3, 3, 3]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def test_http_read_failures_are_reported_and_never_fall_back_to_local_metadata(image_source):
    root, spec = image_source
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
        functools.partial(QuietHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/missing-image"
        import requests
        with pytest.raises(requests.HTTPError, match="404"):
            image_geometry(dict(spec, image_https=url))
        # The same server's actual metadata remains readable afterward.
        actual = image_geometry(dict(spec, image_https=f"http://127.0.0.1:{server.server_port}/image"))[1]
        assert actual["shape_xyz"] == spec["shape_xyz"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
