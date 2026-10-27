"""Physical paint geometry, bounded work, and conservative graph certificates."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import time

import numpy as np
import pytest
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import supervoxel_paint as paint
from supervoxel_paint import (
    PAINT_LIMITS, PaintError, check_deadline, closed_side, components, contacts,
    label_atoms, near, parse_paint, rasterize, representative, separation,
)


def stroke(points, radius=.1):
    return {"points_nm": points, "radius_nm": radius}


def body(source=None, sink=None, radius=.1):
    return {"paint": {"version": 1,
                      "sources": [stroke(source or [[2.5, 2.5, 2.5]], radius)],
                      "sinks": [stroke(sink or [[8.5, 8.5, 8.5]], radius)]}}


def run(value, *, resolution=(1, 1, 1), shape=(12, 12, 12), limits=None, **kwargs):
    return rasterize(parse_paint(value, limits), resolution, shape, limits, **kwargs)


def tuples(values):
    return set(map(tuple, values))


def assert_refusal(kind, function, *args, **kwargs):
    with pytest.raises(PaintError) as caught:
        function(*args, **kwargs)
    assert caught.value.kind == kind
    return caught.value


def test_defaults_and_original_provenance_are_independent():
    assert PAINT_LIMITS == {
        "enabled": False, "max_radius_nm": 500, "max_stroke_points": 256,
        "max_paint_voxels": 200000, "max_box_voxels": [512, 512, 128],
        "max_cut_distance_nm": 1000, "max_painted_atoms": 256,
        "max_removed_edges": 2000, "max_contact_voxels": 2000000,
        "max_seconds": 60, "max_request_bytes": 262144, "max_strokes": 128,
        "max_total_points": 2048, "max_raster_work": 4000000,
    }
    value = body()
    parsed = parse_paint(value)
    value["paint"]["sources"][0]["points_nm"][0][0] = 100
    assert parsed.original["sources"][0]["points_nm"][0][0] == 2.5
    assert parsed.sources[0].points_nm[0, 0] == 2.5
    assert not parsed.sources[0].points_nm.flags.writeable


def test_json_raw_size_checked_before_decode(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("oversize request was decoded")
    monkeypatch.setattr(paint.json, "loads", forbidden)
    assert_refusal("limit", parse_paint, b"{" * 40, {"max_request_bytes": 39})
    assert_refusal("limit", parse_paint, "é" * 20, {"max_request_bytes": 39})


@pytest.mark.parametrize("raw", [b"not JSON", "{", b"\xff", "[]", "null"])
def test_invalid_json_is_a_structured_refusal(raw):
    assert_refusal("limit", parse_paint, raw)


def test_raw_valid_json_and_empty_point_lists_are_accepted():
    value = body()
    value.update(sources=[], sinks=[])
    result = parse_paint(json.dumps(value).encode())
    assert result.sources[0].radius_nm == .1


@pytest.mark.parametrize("version", [0, 2, True, 1.0, "1", None])
def test_version_is_an_exact_supported_integer(version):
    value = body()
    value["paint"]["version"] = version
    assert_refusal("limit", parse_paint, value)


@pytest.mark.parametrize("points", [[], [[1, 2]], [[1, 2, 3, 4]], [[True, 2, 3]],
                                  [[1, "2", 3]], [[1, float("nan"), 3]],
                                  [[float("inf"), 2, 3]], [[10**1000, 2, 3]], "xyz"])
def test_point_shapes_and_nonfinite_numbers_reject(points):
    value = body()
    value["paint"]["sources"][0]["points_nm"] = points
    assert_refusal("limit", parse_paint, value)


@pytest.mark.parametrize("radius", [0, -1, 501, True, "1", None, float("inf"), float("nan")])
def test_bad_radii_reject(radius):
    value = body()
    value["paint"]["sources"][0]["radius_nm"] = radius
    assert_refusal("limit", parse_paint, value)


def test_unknown_fields_mixed_seeds_and_missing_labels_reject():
    value = body()
    value["sources"] = [[7, 1, 2, 3]]
    assert_refusal("limit", parse_paint, value)
    value = body()
    value["paint"]["wall"] = []
    assert_refusal("limit", parse_paint, value)
    value = body()
    value["paint"]["sinks"] = []
    assert_refusal("empty_label", parse_paint, value)
    del value["paint"]["sinks"]
    assert_refusal("limit", parse_paint, value)
    value = body()
    value["paint"]["sources"][0]["radius"] = 1
    assert_refusal("limit", parse_paint, value)


@pytest.mark.parametrize("option,limit", [("max_strokes", 1), ("max_total_points", 1),
                                         ("max_stroke_points", 1)])
def test_request_count_limits_are_enforced_before_arrays(monkeypatch, option, limit):
    value = body(source=[[2.5, 2.5, 2.5], [3.5, 2.5, 2.5]])
    def forbidden(*args, **kwargs):
        pytest.fail("excessive paint was converted to arrays")
    monkeypatch.setattr(paint.np, "asarray", forbidden)
    assert_refusal("limit", parse_paint, value, {option: limit})


def test_isotropic_single_sphere_and_capsule_endcaps():
    value = body(source=[[2.5, 2.5, 2.5]], radius=1)
    source, sink = run(value)
    expected = {(2, 2, 2), (1, 2, 2), (3, 2, 2), (2, 1, 2), (2, 3, 2),
                (2, 2, 1), (2, 2, 3)}
    assert tuples(source) == expected
    assert len(sink) == 7
    value["paint"]["sources"][0]["points_nm"].append([4.5, 2.5, 2.5])
    source, _ = run(value)
    assert len(source) == 17
    assert {(1, 2, 2), (5, 2, 2)} <= tuples(source)
    assert (1, 1, 2) not in tuples(source)
    assert source.dtype == np.int64


def test_anisotropic_10_10_12_point_five_uses_physical_distance():
    value = body(source=[[25, 25, 31.25]], sink=[[85, 85, 106.25]], radius=10)
    source, _ = run(value, resolution=(10, 10, 12.5))
    assert tuples(source) == {(2, 2, 2), (1, 2, 2), (3, 2, 2), (2, 1, 2), (2, 3, 2)}
    value["paint"]["sources"][0]["radius_nm"] = 12.5
    source, _ = run(value, resolution=(10, 10, 12.5))
    assert tuples(source) == {(2, 2, 2), (1, 2, 2), (3, 2, 2), (2, 1, 2), (2, 3, 2),
                              (2, 2, 1), (2, 2, 3)}


def test_polyline_bend_and_zero_length_segments_are_unions():
    value = body(source=[[2.5, 2.5, 2.5], [4.5, 2.5, 2.5], [4.5, 4.5, 2.5]], radius=.1)
    source, _ = run(value)
    assert tuples(source) == {(2, 2, 2), (3, 2, 2), (4, 2, 2), (4, 3, 2), (4, 4, 2)}
    value["paint"]["sources"][0]["points_nm"].insert(1, [2.5, 2.5, 2.5])
    repeated, _ = run(value)
    np.testing.assert_array_equal(source, repeated)


def test_clipping_all_volume_faces_and_outside_volume():
    value = body(source=[[.5, .5, .5]], sink=[[3.5, 3.5, 3.5]], radius=1)
    source, sink = run(value, shape=(4, 4, 4))
    assert tuples(source) == {(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)}
    assert tuples(sink) == {(3, 3, 3), (2, 3, 3), (3, 2, 3), (3, 3, 2)}
    value["paint"]["sources"][0]["points_nm"] = [[-.5, .5, .5]]
    source, _ = run(value, shape=(4, 4, 4))
    assert tuples(source) == {(0, 0, 0)}
    value["paint"]["sources"][0]["points_nm"] = [[-3, -3, -3]]
    source, _ = run(value, shape=(4, 4, 4))
    assert source.shape == (0, 3)


def test_duplicate_strokes_use_exact_union_paint_limit():
    value = body(radius=1)
    value["paint"]["sources"] *= 3
    source, sink = run(value, limits={"max_paint_voxels": 14})
    assert len(source) == len(sink) == 7
    assert_refusal("limit", run, value, limits={"max_paint_voxels": 13})


def test_repeated_raster_work_and_global_box_limits_precede_box_allocation(monkeypatch):
    value = body(radius=1)
    value["paint"]["sources"] *= 3
    def forbidden(*args, **kwargs):
        pytest.fail("preflight refusal allocated candidate box voxels")
    monkeypatch.setattr(paint.np, "arange", forbidden)
    assert_refusal("limit", run, value, limits={"max_raster_work": 107})
    assert_refusal("limit", run, body(), limits={"max_box_voxels": [6, 12, 12]})
    assert_refusal("limit", run, body(source=[[1e300, 0, 0]]))


def test_exact_raster_work_boundary_and_large_batches(monkeypatch):
    run(body(radius=1), limits={"max_raster_work": 54})
    assert_refusal("limit", run, body(radius=1), limits={"max_raster_work": 53})
    sizes = []
    native = paint.np.arange
    def measured(start, stop, **kwargs):
        sizes.append(stop - start)
        return native(start, stop, **kwargs)
    monkeypatch.setattr(paint.np, "arange", measured)
    value = body(source=[[24.5, 24.5, 24.5]], sink=[[74.5, 74.5, 74.5]], radius=22)
    source, sink = run(value, shape=(100, 100, 100))
    assert len(source) == len(sink) > 40000
    assert max(sizes) == 65536 and len(sizes) >= 4


def test_finite_extreme_geometry_refuses_without_numeric_overflow():
    value = body(source=[[1e308, 0, 0]], radius=1e308)
    assert_refusal("limit", run, value, limits={"max_radius_nm": 1e308})
    assert_refusal("limit", run, body(), resolution=(1e-308, 1, 1))


def test_deadline_at_entry_and_after_each_batch(monkeypatch):
    assert_refusal("deadline", run, body(), deadline=time.monotonic() - 1)
    clock = [10]
    monkeypatch.setattr(paint.time, "monotonic", lambda: clock[0])
    calls = []
    def delayed():
        calls.append(True)
        clock[0] = 11
    assert_refusal("deadline", run, body(radius=1), deadline=11, checkpoint=delayed)
    assert calls == [True]
    check_deadline(None)
    assert_refusal("deadline", check_deadline, 10)


def test_labels_background_xyz_offsets_and_uint64_ids():
    a, b = 2**63 + 123, 2**64 - 1
    labels = np.zeros((2, 3, 4), np.uint64)
    labels[0, 1, 2] = a
    labels[1, 2, 3] = b
    labels[0, 0, 0] = b
    source = [[12, 21, 30], [10, 20, 30], [11, 20, 30]]
    sink = [[13, 22, 31]]
    src, snk, by_atom = label_atoms(source, sink, labels, [10, 20, 30])
    np.testing.assert_array_equal(src, np.array([a, b], np.uint64))
    np.testing.assert_array_equal(snk, np.array([b], np.uint64))
    assert src.dtype == snk.dtype == np.uint64
    np.testing.assert_array_equal(by_atom[a]["source"], [[12, 21, 30]])
    np.testing.assert_array_equal(by_atom[b]["source"], [[10, 20, 30]])
    np.testing.assert_array_equal(by_atom[b]["sink"], sink)
    assert by_atom[a]["sink"].shape == (0, 3)


def test_overlap_on_foreground_but_not_background_empty_and_bad_crop():
    labels = np.array([[[0, 1, 2]]], np.uint64)
    src, snk, _ = label_atoms([[0, 0, 0], [1, 0, 0]], [[0, 0, 0], [2, 0, 0]], labels, [0, 0, 0])
    assert src.tolist() == [1] and snk.tolist() == [2]
    assert_refusal("overlap", label_atoms, [[1, 0, 0]], [[1, 0, 0]], labels, [0, 0, 0])
    assert_refusal("empty_label", label_atoms, [[0, 0, 0]], [[1, 0, 0]], labels, [0, 0, 0])
    assert_refusal("empty_label", label_atoms, [], [[1, 0, 0]], labels, [0, 0, 0])
    with pytest.raises(ValueError, match="outside"):
        label_atoms([[3, 0, 0]], [[1, 0, 0]], labels, [0, 0, 0])


def test_representative_centroid_tie_xyz_order_and_unique_weight():
    points = [[2, 1, 0], [0, 1, 0], [2, 1, 0]]
    np.testing.assert_array_equal(representative(2**64 - 1, points), [0, 1, 0])
    np.testing.assert_array_equal(representative(1, points[::-1]), [0, 1, 0])
    np.testing.assert_array_equal(representative(1, [[0, 0, 0], [4, 0, 0], [3, 0, 0]]), [3, 0, 0])
    assert_refusal("empty_label", representative, 1, [])


def test_separation_checks_every_terminal_pair_and_alternate_path():
    edges = [[1, 2], [2, 4], [1, 3], [3, 4], [4, 5]]
    assert separation(edges, [[4, 2], [1, 3]], [1], [4, 5]) == []
    assert separation(edges, [[1, 2]], [1], [4, 5]) == [
        (1, 4, [(1, 3), (3, 4)]), (1, 5, [(1, 3), (3, 4), (4, 5)])]
    assert separation(edges, [], [1, 2], [4, 5]) == [
        (1, 4, [(1, 2), (2, 4)]), (1, 5, [(1, 2), (2, 4), (4, 5)]),
        (2, 4, [(2, 4)]), (2, 5, [(2, 4), (4, 5)])]
    assert separation([], [], [1], [1, 2]) == [(1, 1, [])]


def test_components_and_separation_preserve_large_uint64s():
    a, b, c = 2**63 + 10, 2**63 + 11, 2**64 - 1
    assert components([[a, b], [b, a]], terminals=[a, b, c]) == [{a, b}, {c}]
    assert separation([[a, b]], [], [a], [b]) == [(a, b, [(a, b)])]
    assert separation([[a, b]], [[b, a]], [a], [b]) == []
    assert components([], [a, b]) == [{a}, {b}]


def test_closed_side_margin_missing_bounds_and_fake_exit():
    bounds = {1: [1, 1, 1, 9, 9, 9], 2: [[2, 2, 2], [3, 3, 3]]}
    assert closed_side([1], bounds, [0, 0, 0], [10, 10, 10], []) == (True, [])
    assert closed_side([1], bounds, [1, 0, 0], [10, 10, 10], []) == (False, [1])
    assert closed_side([1], bounds, [0, 0, 0], [9, 10, 10], []) == (False, [1])
    assert closed_side([1], bounds, [0, 0, 0], [10, 10, 10], [[1, 2]]) == (True, [])
    assert closed_side([1], bounds, [0, 0, 0], [10, 10, 10], [[3, 1]]) == (False, [1])
    assert closed_side([3], bounds, [0, 0, 0], [10, 10, 10], []) == (False, [3])
    assert closed_side([], bounds, [0, 0, 0], [10, 10, 10], []) == (False, [])


@pytest.mark.parametrize("axis", [0, 1, 2])
def test_contacts_are_six_connected_unsigned_and_respect_zyx(axis):
    a, b = 2**63 + 123, 2**64 - 1
    labels = np.full((3, 4, 5), a, np.uint64)
    selected = [slice(None)] * 3
    selected[axis] = slice(1, None)
    labels[tuple(selected)] = b
    origin = [11, 20, 30]
    actual = contacts(labels, origin, a, b)
    expected = np.argwhere(labels == a)[:, ::-1] + origin
    np.testing.assert_array_equal(actual, np.unique(expected, axis=0))
    reverse = contacts(labels, origin, b, a)
    np.testing.assert_array_equal(reverse, actual + np.eye(3, dtype=int)[2 - axis])


def test_diagonal_neighbors_and_identical_ids_are_not_contacts():
    labels = np.zeros((2, 2, 2), np.uint64)
    labels[0, 0, 0], labels[1, 1, 1] = 1, 2
    assert contacts(labels, [0, 0, 0], 1, 2).shape == (0, 3)
    assert contacts(labels, [0, 0, 0], 1, 1).shape == (0, 3)


def test_distant_real_contact_refuses_despite_overlapping_near_boxes():
    labels = np.zeros((1, 4, 20), np.uint64)
    labels[0, 0, :] = 1
    labels[0, 2, :] = 2
    labels[0, 1, -1] = 1  # Their only contact is at the distant end.
    actual = contacts(labels, [0, 0, 0], 1, 2)
    np.testing.assert_array_equal(actual, [[19, 1, 0]])
    spacing = np.array([10, 10, 12.5])
    tree = cKDTree((np.array([[0, 0, 0], [0, 2, 0]]) + .5) * spacing)
    assert not near(actual, tree, 100, spacing)
    assert near(actual, tree, 200, spacing)
    assert not near([], tree, 1e10, spacing)


def test_near_uses_physical_voxel_centers_and_inclusive_radius():
    spacing = np.array([10, 10, 12.5])
    tree = cKDTree((np.array([[0, 0, 0]]) + .5) * spacing)
    assert near([[0, 0, 0]], tree, 0, spacing)
    assert near([[0, 0, 1]], tree, 12.5, spacing)
    assert not near([[0, 0, 1]], tree, 12.4, spacing)


def test_refusal_payload_caps_locations_and_preserves_ids():
    error = PaintError("stray_cut", "Contact is far away", locations_nm=[[1, 2, 3]] * 25,
                       supervoxels=[np.uint64(2**64 - 1)])
    assert str(error) == "Contact is far away"
    assert len(error.as_dict()["locations_nm"]) == 20
    assert error.as_dict()["supervoxels"] == [2**64 - 1]
    assert PaintError("limit", "bad", locations_nm=None, supervoxels=None).as_dict() == {
        "kind": "limit", "locations_nm": [], "supervoxels": []}
