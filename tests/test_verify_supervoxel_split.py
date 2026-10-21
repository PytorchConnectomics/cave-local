"""Verifier guards must reject corrupted fixture evidence, not pass vacuously."""
from pathlib import Path
from datetime import datetime, timezone
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_supervoxel_split import (
    _assert_allocated_components, _assert_disjoint_allocations,
    _assert_staged_cells_absent, _assert_volume_lineage, _switch_off_pair, _table_positions,
)


@pytest.mark.parametrize("missing", [2**63 + 2, 2**63 + 4])
def test_inventory_requires_an_exact_row_not_a_neighbor(missing):
    ids = np.asarray([2**63 + 1, 2**63 + 3], np.uint64)
    with pytest.raises(AssertionError, match="dense table row"):
        _table_positions(ids, missing)
    assert _table_positions(ids, 2**63 + 3) == 1


def test_boundary_checks_lineage_for_every_new_volume_id():
    old, new = 2**63 + 1, 2**63 + 2
    before = np.full((2, 2, 2), old, np.uint64)
    after = before.copy()
    after[1] = new
    tables = SimpleNamespace(ids=np.asarray([old, new], np.uint64),
                             lineage=np.empty(0, dtype=[("new", "<u8")]))
    with pytest.raises(AssertionError, match="lineage key"):
        _assert_volume_lineage(after, before, tables)
    tables.lineage = np.asarray([(new,)], dtype=[("new", "<u8")])
    assert _assert_volume_lineage(after, before, tables) == 1


@pytest.mark.parametrize("entries", [
    [{"new_atoms": [101, 102]}, {"new_atoms": [102, 103]}],
    [{"new_atoms": [101, 101]}, {"new_atoms": [102, 103]}],
    [{"new_atoms": []}, {"new_atoms": [102, 103]}],
])
def test_concurrent_journal_allocations_must_be_nonempty_unique_and_disjoint(entries):
    with pytest.raises(AssertionError, match="overlapping|empty or duplicate"):
        _assert_disjoint_allocations(entries)


def test_disjoint_journal_allocations_preserve_unsigned_ids():
    atoms = [2**63 + n for n in range(4)]
    assert _assert_disjoint_allocations([
        {"new_atoms": atoms[:2]}, {"new_atoms": atoms[2:]},
    ]) == set(atoms)


def test_new_atom_voxels_must_form_one_six_connected_component():
    data = np.zeros((3, 3, 3), np.uint64)
    data[0, 0, 0] = data[1, 1, 1] = 101  # Diagonal contact is not 6-connectivity.
    with pytest.raises(AssertionError, match="disconnected voxel components"):
        _assert_allocated_components(data, [101], {1000: [101]})


@pytest.mark.parametrize("root_leaves", [{}, {1000: [101], 2000: [101]}])
def test_new_atom_must_belong_to_exactly_one_root_leaf_set(root_leaves):
    data = np.full((2, 2, 2), 101, np.uint64)
    with pytest.raises(AssertionError, match="root leaf sets"):
        _assert_allocated_components(data, [101], root_leaves)


def test_allocated_atom_must_have_voxels():
    with pytest.raises(AssertionError, match="has no voxels"):
        _assert_allocated_components(np.zeros((2, 2, 2), np.uint64), [101], {1000: [101]})


def test_connected_allocated_atoms_report_separate_root_ownership():
    data = np.full((2, 2, 2), 101, np.uint64)
    data[1] = 102
    result = _assert_allocated_components(data, [101, 102], {1000: [101], 2000: [102]})
    assert result == {"101": {"voxels": 4, "components": 1, "root": 1000},
                      "102": {"voxels": 4, "components": 1, "root": 2000}}


@pytest.mark.parametrize("cells", [{}, {"7a": [], "7b": []}])
def test_unpublished_probe_rejects_an_empty_staged_cells_map(cells):
    with pytest.raises(AssertionError, match="no staged graph cells to check"):
        _assert_staged_cells_absent(cells, lambda *args, **kwargs: [], lambda cell: None)


def test_unpublished_probe_checks_every_staged_timestamp():
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cell = {"timestamp": timestamp.isoformat(), "row": 2**63 + 1, "fake": False}
    calls = []

    def read_node(row, **options):
        calls.append((row, options))
        return [SimpleNamespace(timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc))]

    assert _assert_staged_cells_absent({"7a": [cell], "7b": [cell]}, read_node, lambda cell: "test") == 2
    assert len(calls) == 2
    assert all(row == np.uint64(2**63 + 1) and options == {
        "properties": "test", "fake_edges": False, "end_time": timestamp,
        "end_time_inclusive": True,
    } for row, options in calls)
    with pytest.raises(AssertionError, match="wrote a staged graph cell"):
        _assert_staged_cells_absent({"7a": [cell]},
                                   lambda *args, **kwargs: [SimpleNamespace(timestamp=timestamp)],
                                   lambda cell: "test")


def test_switch_off_pair_skips_largest_atom_when_its_root_has_only_one_chunk():
    isolated = {"atom": 101, "source": 2**64 - 1, "root": 1000, "chunk": [0, 0, 0], "count": 64}
    first = {"atom": 102, "source": 2**64 - 1, "root": 2000, "chunk": [1, 0, 0], "count": 32}
    second = {"atom": 103, "source": 2**64 - 1, "root": 2000, "chunk": [2, 0, 0], "count": 16}
    assert _switch_off_pair([isolated, first, second]) == (first, second)


@pytest.mark.parametrize("changed", [{"source": 7}, {"root": 1001}, {"chunk": [0, 0, 0]}])
def test_switch_off_pair_requires_same_source_and_root_in_distinct_chunks(changed):
    first = {"atom": 101, "source": 2**64 - 1, "root": 1000, "chunk": [0, 0, 0], "count": 32}
    second = {**first, "atom": 102, "chunk": [1, 0, 0], **changed}
    with pytest.raises(AssertionError, match="same source label across two graph chunks"):
        _switch_off_pair([first, second])
