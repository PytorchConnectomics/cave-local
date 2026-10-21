"""Array-level refinement contracts; no deployment or PCG process is needed."""

import sys
import time
from pathlib import Path

import cc3d
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from supervoxel_split import (  # noqa: E402
    Edge, grid_pieces, internal_affinity, neighbour_contacts, piece_contacts,
    rewrite_edges, seeded_pieces,
)


@pytest.mark.parametrize("variant", ["zyx", "xyz"])
def test_refinement_fixture_pairs_are_isolated_connected_boundary_contacts(variant):
    from make_fixture import add_refinement_geometry, fixture_arrays

    labels, *_ = fixture_arrays(variant)
    original = labels.copy()
    add_refinement_geometry(labels)
    np.testing.assert_array_equal(labels[original != 0], original[original != 0])
    for left, right in ((31, 32), (41, 42)):
        for source in (left, right):
            mask = labels == source
            assert mask.sum() == 64
            assert cc3d.connected_components(mask, connectivity=6).max() == 1
        assert np.count_nonzero((labels[:, :, 15] == left) & (labels[:, :, 16] == right)) == 16
        assert cc3d.connected_components(np.isin(labels, np.array([left, right], np.uint64)),
                                         connectivity=6).max() == 1
    with pytest.raises(ValueError, match="overlap"):
        add_refinement_geometry(labels)


def assert_partition(mask, pieces, count):
    assert pieces.dtype == np.int32
    np.testing.assert_array_equal(pieces > 0, mask)
    np.testing.assert_array_equal(np.unique(pieces[mask]), np.arange(1, count + 1))
    for piece in range(1, count + 1):
        _, connected = cc3d.connected_components(pieces == piece, connectivity=6, return_N=True)
        assert connected == 1


def centroids(pieces):
    return {int(piece): np.argwhere(pieces == piece).mean(axis=0)
            for piece in np.unique(pieces) if piece}


def test_grid_dumbbell_tube_and_disconnected_parts_are_partitioned():
    dumbbell = np.zeros((10, 10, 40), bool)
    dumbbell[1:9, 1:9, 1:11] = True
    dumbbell[1:9, 1:9, 29:39] = True
    dumbbell[5, 5, 11:29] = True
    tube = np.zeros((3, 3, 40), bool)
    tube[1, 1, :] = True
    disconnected = np.zeros((4, 4, 4), bool)
    disconnected[0, 0, :2] = True
    disconnected[3, 3, 2:] = True
    for mask in (dumbbell, tube, disconnected):
        pieces, count = grid_pieces(mask, (0, 0, 0), (16, 16, 16))
        assert count > 1
        assert_partition(mask, pieces, count)
        np.testing.assert_array_equal(pieces, grid_pieces(mask, (0, 0, 0), (16, 16, 16))[0])


def test_grid_alignment_survives_box_crop_and_non_cubic_axes():
    mask = np.ones((3, 5, 10), bool)
    pieces, count = grid_pieces(mask, (13, 5, 3), (16, 8, 4))
    assert count == 8
    assert_partition(mask, pieces, count)
    assert pieces[0, 0, 2] != pieces[0, 0, 3]  # Global X=15 versus X=16.
    assert pieces[0, 2, 0] != pieces[0, 3, 0]  # Global Y=7 versus Y=8.
    assert pieces[0, 0, 0] != pieces[1, 0, 0]  # Global Z=3 versus Z=4.
    negative, count = grid_pieces(np.ones((1, 1, 3), bool), (-1, 0, 0), (2, 2, 2))
    assert count == 2
    assert negative[0, 0, 0] != negative[0, 0, 1] == negative[0, 0, 2]


@pytest.mark.parametrize("grid", [(0, 16, 16), (-1, 16, 16), (1.5, 16, 16), (16, 16)])
def test_invalid_grid_is_rejected(grid):
    with pytest.raises(ValueError):
        grid_pieces(np.ones((2, 2, 2), bool), (0, 0, 0), grid)


def test_empty_mask_produces_no_atoms():
    pieces, count = grid_pieces(np.zeros((2, 2, 2), bool), (0, 0, 0), (16, 16, 16))
    assert count == 0
    assert not pieces.any()


def test_tier_two_splits_u_shape_inside_one_grid_piece():
    mask = np.zeros((3, 12, 12), bool)
    mask[1, 1:10, 1:3] = True
    mask[1, 1:10, 9:11] = True
    mask[1, 8:10, 1:11] = True
    assert grid_pieces(mask, (0, 0, 0), (16, 16, 16))[1] == 1
    source = [(1, 1, 1), (1, 2, 2)]
    sink = [(1, 1, 9), (1, 2, 10)]
    pieces, count, seeds = seeded_pieces(mask, source, sink, (10, 10, 12.5))
    assert count >= 2
    assert_partition(mask, pieces, count)
    assert not {seeds[p] for p in source}.intersection(seeds[p] for p in sink)
    repeated = seeded_pieces(mask, source[::-1], sink[::-1], (10, 10, 12.5))
    np.testing.assert_array_equal(pieces, repeated[0])
    assert repeated[2] == seeds


def test_disconnected_watershed_regions_become_distinct_atoms():
    mask = np.ones((1, 1, 9), bool)
    pieces, count, seeds = seeded_pieces(
        mask, [(0, 0, 0), (0, 0, 8)], [(0, 0, 4)], (10, 11, 12),
    )
    assert count == 3  # The source group has two disconnected regions.
    assert seeds[(0, 0, 0)] != seeds[(0, 0, 8)]
    assert_partition(mask, pieces, count)


@pytest.mark.parametrize("sources,sinks", [
    ([(0, 0, 0)], [(0, 0, 0)]),
    ([(3, 0, 0)], [(0, 0, 1)]),
    ([(0.5, 0, 0)], [(0, 0, 1)]),
    ([], [(0, 0, 1)]),
])
def test_seed_errors_reject_without_an_output_partition(sources, sinks):
    with pytest.raises(ValueError):
        seeded_pieces(np.ones((2, 2, 2), bool), sources, sinks, (1, 1, 1))


def test_seed_outside_mask_and_unseeded_component_rejected():
    mask = np.zeros((1, 1, 8), bool)
    mask[0, 0, :3] = True
    mask[0, 0, 6:] = True
    with pytest.raises(ValueError, match="outside"):
        seeded_pieces(mask, [(0, 0, 3)], [(0, 0, 6)], (1, 1, 1))
    with pytest.raises(ValueError, match="component"):
        seeded_pieces(mask, [(0, 0, 0)], [(0, 0, 2)], (1, 1, 1))


def test_piece_contact_areas_and_internal_affinity():
    pieces = np.ones((2, 3, 4), np.int32)
    pieces[:, :, 2:] = 2
    a, b, area = piece_contacts(pieces)
    np.testing.assert_array_equal(a, [1])
    np.testing.assert_array_equal(b, [2])
    np.testing.assert_array_equal(area, [6])
    np.testing.assert_allclose(internal_affinity([0, 1, 128, 1024]), [.01, .01, .5, 1])
    assert internal_affinity(3, ref_area=6) == .5
    assert all(len(v) == 0 for v in piece_contacts(np.ones((2, 2, 2), np.int32)))


def test_neighbor_contact_counts_include_cross_chunk_face_only():
    pieces, _ = grid_pieces(np.ones((2, 2, 4), bool), (124, 0, 0), (2, 4, 4))
    context = np.zeros((4, 4, 6), np.uint64)
    context[1:-1, 1:-1, 1:-1] = 100
    context[1:-1, 1:-1, -1] = 200  # Chunk face at global X=128.
    context[1:-1, 0, 1:-1] = 300
    contacts = neighbour_contacts(pieces, context)
    assert contacts == {200: [(2, 4)], 300: [(1, 4), (2, 4)]}
    edges, provenance = rewrite_edges(
        100, [Edge(100, 200, float("inf"), 4)], {1: 101, 2: 102},
        contacts, centroids(pieces), {},
    )
    assert edges == [Edge(102, 200, float("inf"), 4)]
    assert provenance == {(100, 200): [(102, 200)]}


def test_neighbor_contacts_preserve_full_uint64_pcg_ids():
    pieces = np.ones((2, 2, 2), np.int32)
    context = np.zeros((4, 4, 4), np.uint64)
    context[1:-1, 1:-1, 1:-1] = np.uint64(72057662824513535)
    context[1:-1, 1:-1, -1] = np.uint64(72057662824513537)
    context[1:-1, 0, 1:-1] = np.uint64(2**63 + 123)
    assert neighbour_contacts(pieces, context) == {
        72057662824513537: [(1, 4)], 2**63 + 123: [(1, 4)],
    }


def test_tight_mask_edt_uses_background_at_both_ends():
    pieces, count, seeds = seeded_pieces(
        np.ones((3, 3, 9), bool), [(1, 1, 0)], [(1, 1, 8)], (1, 1, 1),
    )
    assert count == 2
    # Symmetric EDT leaves only the central voxel plane as a tie. An unpadded
    # all-foreground EDT favors one end as though exterior background were absent.
    sizes = [np.count_nonzero(pieces == seeds[p]) for p in ((1, 1, 0), (1, 1, 8))]
    assert abs(sizes[0] - sizes[1]) <= 9


def test_rewrite_contact_fake_inactive_and_internal_edges_keep_provenance():
    edges, provenance = rewrite_edges(
        100,
        [Edge(100, 200, .7, 50), Edge(100, 300, .9, 2, False),
         (400, 100, .4, 3), {"a": 500, "b": 600, "affinity": .8}],
        {1: 101, 2: 102}, {200: [(1, 3), (2, 7)], 300: [(2, 2)]},
        {1: (0, 0, 0), 2: (0, 0, 2)}, {400: (0, 0, 1)},
        internal_contacts=([1], [2], [4]), ref_area=16,
    )
    assert edges == sorted([
        Edge(101, 200, .7, 3), Edge(102, 200, .7, 7),
        Edge(102, 300, .9, 2, False), Edge(101, 400, .4, 3),
        Edge(500, 600, .8), Edge(101, 102, .25, 4),
    ])
    assert provenance == {
        (100, 200): [(101, 200), (102, 200)],
        (100, 300): [(102, 300)], (100, 400): [(101, 400)],
    }
    assert all(edge.a != 100 and edge.b != 100 for edge in edges)


def test_seam_without_contact_and_missing_fake_point_rejected():
    for affinity, error in [(float("inf"), "face contact"), (.5, "inside point")]:
        with pytest.raises(ValueError, match=error):
            rewrite_edges(100, [Edge(100, 200, affinity)], {1: 101}, {}, {1: (0, 0, 0)}, {})


@pytest.mark.parametrize("affinity", [.7, float("inf")])
def test_adjacent_refinements_rewrite_both_current_endpoints(affinity):
    # The interface lies at a chunk face; its cells may belong to different roots.
    # This pure test covers contact/provenance semantics independently of locking.
    atomic = np.zeros((4, 4, 6), np.uint64)
    atomic[1:-1, 1:-1, 1:3] = 100
    atomic[1:-1, 1:-1, 3:5] = 200
    a_pieces, _ = grid_pieces(np.ones((2, 2, 2), bool), (126, 0, 0), (1, 2, 2))
    a_ids = {1: 101, 2: 102}
    a_edges, a_map = rewrite_edges(
        100, [Edge(100, 200, affinity, 4)], a_ids,
        neighbour_contacts(a_pieces, atomic[:, :, :4]), centroids(a_pieces), {},
        internal_contacts=piece_contacts(a_pieces),
    )
    for piece, atom in a_ids.items():
        atomic[1:-1, 1:-1, 1:3][a_pieces == piece] = atom
    b_pieces, _ = grid_pieces(np.ones((2, 2, 2), bool), (128, 0, 0), (2, 1, 2))
    b_edges, b_map = rewrite_edges(
        200, a_edges, {1: 201, 2: 202},
        neighbour_contacts(b_pieces, atomic[:, :, 2:]), centroids(b_pieces), {},
        internal_contacts=piece_contacts(b_pieces),
    )
    boundary = [edge for edge in b_edges if edge.a == 102 and edge.b in (201, 202)]
    assert boundary == [Edge(102, 201, affinity, 2), Edge(102, 202, affinity, 2)]
    assert all(not {edge.a, edge.b}.intersection({100, 200}) for edge in b_edges)
    assert a_map[(100, 200)] == [(102, 200)]
    assert b_map[(102, 200)] == [(102, 201), (102, 202)]


def test_complete_128_cube_tier_one_timing(capsys):
    mask = np.ones((128, 128, 128), bool)
    started = time.perf_counter()
    pieces, count = grid_pieces(mask, (0, 0, 0), (16, 16, 16))
    duration = time.perf_counter() - started
    assert count == 512
    labels, counts = np.unique(pieces, return_counts=True)
    np.testing.assert_array_equal(labels, np.arange(1, 513))
    np.testing.assert_array_equal(counts, np.repeat(16 ** 3, 512))
    with capsys.disabled():
        print(f"\nsvsplit tier-1 128^3: {duration:.6f}s; {count} pieces", flush=True)
