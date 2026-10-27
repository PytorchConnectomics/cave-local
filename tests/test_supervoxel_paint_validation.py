"""Graph-backed paint checks fail closed before a native cut can be applied.

Only external reads are replaced. Label grouping, unsigned ids, separation,
closed-side certification, contact extraction and physical distances are real.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip('pychunkedgraph')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import supervoxel_paint_service as service
from supervoxel_paint import PAINT_LIMITS, PaintError

A, B, C, D = 2**63 + 7, 2**63 + 9, 2**63 + 11, 2**63 + 13
ROOT = 2**63 + 101


@pytest.fixture
def context(monkeypatch, tmp_path):
    monkeypatch.setattr(service, '_fault', lambda *args, **kwargs: None)
    return service.PaintContext(
        spec={'shape_xyz': [12, 12, 12], 'resolution_nm_xyz': [10, 10, 12.5],
              'graph_chunk_xyz': [4, 4, 4]},
        limits={**PAINT_LIMITS, 'max_cut_distance_nm': 15},
        deadline=time.monotonic() + 60,
        source_xyz=np.array([[2, 2, 2]], np.int64),
        sink_xyz=np.array([[5, 2, 2]], np.int64),
        root=ROOT, sources={A}, sinks={B}, offset=np.array([3, 3, 3]),
        journal=None, data_dir=str(tmp_path), box=np.array([[0, 0, 0], [12, 12, 12]]))


def volume_reader(monkeypatch, data, *, after_read=None, during_read=None):
    """CloudVolume-compatible XYZ read, including its trailing channel axis."""
    import cloudvolume
    calls = []
    class Volume:
        def __getitem__(self, key):
            calls.append(key)
            if during_read:
                during_read()
            result = data[key][..., None]
            if after_read:
                after_read()
            return result
    monkeypatch.setattr(cloudvolume, 'CloudVolume', lambda *args, **kwargs: Volume())
    return calls


def root_reader(roots=None, *, after_read=None):
    calls = []
    def get_roots(ids):
        assert ids.dtype == np.uint64
        calls.append(ids.copy())
        if after_read:
            after_read()
        return np.asarray([ROOT if roots is None else roots[int(atom)] for atom in ids], np.uint64)
    return SimpleNamespace(get_roots=get_roots), calls


def lookup_volume():
    volume = np.zeros((12, 12, 12), np.uint64)
    volume[2, 2, 2], volume[3, 2, 2], volume[5, 2, 2] = A, A, B
    return volume


def test_lookup_groups_all_foreground_voxels_inside_snapshot(monkeypatch, context):
    import supervoxel_history
    inside, snapshots = [False], []
    def snapshot(fn, **kwargs):
        snapshots.append(kwargs)
        inside[0] = True
        try:
            return fn()
        finally:
            inside[0] = False
    monkeypatch.setattr(supervoxel_history, 'snapshot_read', snapshot)
    def check_snapshot():
        assert inside[0]
    volume_reader(monkeypatch, lookup_volume(), during_read=check_snapshot)
    graph, calls = root_reader(after_read=check_snapshot)
    context.source_xyz = np.array([[2, 2, 2], [3, 2, 2], [1, 2, 2]])
    root, sources, sinks, grouped = service._lookup(context, graph)
    assert root == ROOT and sources.tolist() == [A] and sinks.tolist() == [B]
    assert grouped[A]['source'].tolist() == [[2, 2, 2], [3, 2, 2]]
    assert grouped[B]['sink'].tolist() == [[5, 2, 2]]
    assert calls[0].tolist() == [A, B]
    assert snapshots == [{'data_dir': context.data_dir}]


@pytest.mark.parametrize('expected_root,kind', [(None, 'other_object'), (ROOT, 'object_changed')])
def test_lookup_rejects_other_roots_with_physical_locations(monkeypatch, context, expected_root, kind):
    volume_reader(monkeypatch, lookup_volume())
    graph, _ = root_reader({A: ROOT, B: ROOT + 1})
    with pytest.raises(PaintError) as caught:
        service._lookup(context, graph, expected_root)
    assert caught.value.kind == kind
    assert caught.value.supervoxels == [B]
    assert caught.value.locations_nm == [[55., 25., 31.25]]


def test_lookup_atom_limit_precedes_root_access(monkeypatch, context):
    volume_reader(monkeypatch, lookup_volume())
    graph, calls = root_reader()
    context.limits['max_painted_atoms'] = 1
    with pytest.raises(PaintError) as caught:
        service._lookup(context, graph)
    assert caught.value.kind == 'limit' and not calls


@pytest.mark.parametrize('condition', ['other_object', 'limit', 'empty_label', 'overlap'])
def test_invalid_painted_object_never_reaches_journal_or_native_edit(monkeypatch, context, condition):
    from flask import Flask, g
    from pychunkedgraph.app import app_utils
    import supervoxel_paint_history
    data = lookup_volume()
    roots = {A: ROOT, B: ROOT + (condition == 'other_object')}
    if condition == 'empty_label':
        data[5, 2, 2] = 0
    volume_reader(monkeypatch, data)
    graph, _ = root_reader(roots)
    monkeypatch.setattr(app_utils, 'get_cg', lambda *args, **kwargs: graph)
    monkeypatch.setattr(supervoxel_paint_history, 'recover_paint', lambda *args, **kwargs: None)
    monkeypatch.setattr(supervoxel_paint_history.PaintJournal, 'create',
                        lambda *args, **kwargs: pytest.fail('invalid paint created mutation provenance'))
    spec = {**context.spec, 'graph_id': 'paint_validation',
            'supervoxel_split': {'enabled': True, 'paint': {'enabled': True,
                **({'max_painted_atoms': 1} if condition == 'limit' else {})}}}
    source = ((context.source_xyz[0] + .5) * context.resolution).tolist()
    sink = source if condition == 'overlap' else ((context.sink_xyz[0] + .5) * context.resolution).tolist()
    body = {'paint': {'version': 1,
        'sources': [{'points_nm': [source], 'radius_nm': 4}],
        'sinks': [{'points_nm': [sink], 'radius_nm': 4}]}}
    app = Flask(__name__)
    with app.test_request_context('/split', json=body):
        g.auth_user = {'id': 'fixture-user'}
        with pytest.raises(PaintError) as caught:
            service.painted_split(lambda *args: pytest.fail('invalid paint reached native mutation'),
                'paint_validation', spec, body, data_dir=context.data_dir)
    assert caught.value.kind == condition


@pytest.mark.parametrize('change', ['source_replaced', 'labels_swapped', 'extra_source'])
def test_locked_lookup_requires_exact_terminal_sets(monkeypatch, context, change):
    data = lookup_volume()
    if change == 'source_replaced':
        data[2, 2, 2] = C
    elif change == 'labels_swapped':
        data[2, 2, 2], data[5, 2, 2] = B, A
    else:
        context.source_xyz = np.array([[2, 2, 2], [3, 2, 2]])
        data[3, 2, 2] = C
    volume_reader(monkeypatch, data)
    graph, calls = root_reader()
    with pytest.raises(PaintError) as caught:
        service._lookup(context, graph, ROOT, locked=True)
    assert caught.value.kind == 'object_changed' and len(calls) == 1


@pytest.mark.parametrize('phase', ['graph_read', 'roots'])
def test_lookup_discards_reads_finishing_after_deadline(monkeypatch, context, phase):
    def expire():
        context.deadline = time.monotonic() - 1
    volume_reader(monkeypatch, lookup_volume())
    graph, calls = root_reader(after_read=expire if phase == 'roots' else None)
    if phase == 'graph_read':
        monkeypatch.setattr(service, '_fault', lambda name, *args, **kwargs: expire() if name == 'graph_read' else None)
    with pytest.raises(PaintError) as caught:
        service._lookup(context, graph)
    assert caught.value.kind == 'deadline'
    assert len(calls) == (1 if phase == 'roots' else 0)


def cut_setup(monkeypatch, context, *, bounds=None, cells=(), data=None):
    bounds = bounds or {A: np.array([2, 2, 2, 4, 5, 5]), B: np.array([4, 2, 2, 6, 5, 5])}
    monkeypatch.setattr(service, '_bounds', lambda ctx, ids: {atom: bounds[atom] for atom in ids})
    monkeypatch.setattr(service, '_fake_cells', lambda ctx, ids: list(cells))
    if data is None:
        data = np.zeros((12, 12, 12), np.uint64)
        data[2:4, 2:5, 2:5] = A
        data[4:6, 2:5, 2:5] = B
    reads = volume_reader(monkeypatch, data)
    graph = SimpleNamespace(get_pairs=lambda: np.array([[A, B]], np.uint64))
    return graph, bounds, reads


def test_check_accepts_actual_near_contact_and_records_partition(monkeypatch, context):
    edges, _, reads = cut_setup(monkeypatch, context)
    service._check_cut(context, edges, np.array([[B, A], [A, B]], np.uint64))
    assert len(reads) == 1
    assert context.checked['removed_edges'] == [[A, B]]
    assert context.checked['expected_partition'] == [0, 1]
    assert context.checked['contact_voxels'] == 50
    assert context.checked['source_closed'] and context.checked['sink_closed']


def test_local_remaining_path_refuses_before_whole_object_or_fake_checks(monkeypatch, context):
    bounds = {atom: np.array([2, 2, 2, 6, 5, 5]) for atom in (A, B, C)}
    cut_setup(monkeypatch, context, bounds=bounds)
    edges = SimpleNamespace(get_pairs=lambda: np.array([[A, B], [A, C], [C, B]], np.uint64))
    monkeypatch.setattr(service, '_fake_cells', lambda *args: pytest.fail('certificate after failed separation'))
    monkeypatch.setattr(service, '_edge_contacts', lambda *args: np.array([[3, 2, 2]], np.int64))
    with pytest.raises(PaintError) as caught:
        service._check_cut(context, edges, [[A, B]])
    assert caught.value.kind == 'not_separated'
    assert caught.value.locations_nm == [[35., 25., 31.25], [35., 25., 31.25]]
    assert context.checked == {}


def test_certificate_requires_every_terminal_component_on_one_side_closed(monkeypatch, context):
    context.sources, context.sinks = {A, C}, {B, D}
    bounds = {atom: np.array([2, 2, 2, 6, 5, 5]) for atom in (A, B)}
    bounds.update({atom: np.array([0, 2, 2, 6, 5, 5]) for atom in (C, D)})
    cut_setup(monkeypatch, context, bounds=bounds)
    edges = SimpleNamespace(get_pairs=lambda: np.array([[A, B], [C, D]], np.uint64))
    monkeypatch.setattr(service, '_edge_contacts', lambda *args: pytest.fail('locality after failed certificate'))
    with pytest.raises(PaintError) as caught:
        service._check_cut(context, edges, [[A, B], [C, D]])
    assert caught.value.kind == 'not_certified'
    assert caught.value.supervoxels == [C, D]
    assert context.checked == {}


def test_fake_edges_leaving_both_sides_prevent_certificate(monkeypatch, context):
    cells = [SimpleNamespace(value=np.array([[A, C], [B, D]], np.uint64))]
    edges, _, _ = cut_setup(monkeypatch, context, cells=cells)
    with pytest.raises(PaintError) as caught:
        service._check_cut(context, edges, [[A, B]])
    assert caught.value.kind == 'not_certified'
    assert caught.value.supervoxels == [A, B]


def test_one_complete_closed_side_suffices(monkeypatch, context):
    bounds = {A: np.array([0, 2, 2, 4, 5, 5]), B: np.array([4, 2, 2, 6, 5, 5])}
    edges, _, _ = cut_setup(monkeypatch, context, bounds=bounds)
    service._check_cut(context, edges, [[A, B]])
    assert not context.checked['source_closed'] and context.checked['sink_closed']


@pytest.mark.parametrize('known', [[A], [A, C]])
def test_missing_bounds_cannot_borrow_another_atom_row(monkeypatch, context, known):
    import supervoxel_history
    tables = SimpleNamespace(ids=np.asarray(known, np.uint64),
        bounds=np.asarray([[2, 2, 2, 6, 5, 5] for _ in known]))
    monkeypatch.setattr(supervoxel_history, 'load_atom_tables', lambda *args: tables)
    with pytest.raises(PaintError) as caught:
        service._bounds(context, {A, B})
    assert caught.value.kind == 'not_certified'


def test_contact_voxel_budget_refuses_before_volume_read(monkeypatch, context):
    edges, _, reads = cut_setup(monkeypatch, context)
    context.limits['max_contact_voxels'] = 49
    with pytest.raises(PaintError) as caught:
        service._check_cut(context, edges, [[A, B]])
    assert caught.value.kind == 'limit' and not reads and context.checked == {}


def test_removed_edge_budget_precedes_atom_table_read(monkeypatch, context):
    context.limits['max_removed_edges'] = 1
    monkeypatch.setattr(service, '_bounds', lambda *args: pytest.fail('atom bounds after edge limit'))
    edges = SimpleNamespace(get_pairs=lambda: np.array([[A, B], [C, D]], np.uint64))
    with pytest.raises(PaintError) as caught:
        service._check_cut(context, edges, [[A, B], [C, D]])
    assert caught.value.kind == 'limit'


def test_near_bounding_boxes_do_not_hide_distant_physical_contact(monkeypatch, context):
    data = np.zeros((12, 12, 12), np.uint64)
    data[2:4, 8:10, 8:10], data[4:6, 8:10, 8:10] = A, B
    bounds = {A: np.array([2, 2, 2, 4, 10, 10]), B: np.array([4, 2, 2, 6, 10, 10])}
    edges, _, _ = cut_setup(monkeypatch, context, bounds=bounds, data=data)
    with pytest.raises(PaintError) as caught:
        service._check_cut(context, edges, [[A, B]])
    assert caught.value.kind == 'stray_cut'
    assert caught.value.locations_nm == [[35., 85., 106.25]]
    assert context.checked == {}


@pytest.mark.parametrize('reason', ['no_cell', 'wrong_timestamp', 'wrong_edge', 'missing_coordinates', 'far_coordinates'])
def test_fake_locality_needs_matching_merge_and_near_coordinates(monkeypatch, context, reason):
    from pychunkedgraph.graph import attributes
    logs = attributes.OperationLogs
    timestamp = datetime(2026, 10, 21, tzinfo=timezone.utc)
    cells = [] if reason == 'no_cell' else [SimpleNamespace(value=np.array([[A, B]], np.uint64), timestamp=timestamp)]
    record = {'timestamp': timestamp, logs.AddedEdge: np.array([[A, B]], np.uint64),
              logs.SourceCoordinate: np.array([[2.5, 2.5, 2.5]])}
    if reason == 'wrong_timestamp':
        record['timestamp'] += timedelta(milliseconds=1)
    elif reason == 'wrong_edge':
        record[logs.AddedEdge] = np.array([[A, C]], np.uint64)
    elif reason == 'missing_coordinates':
        del record[logs.SourceCoordinate]
    elif reason == 'far_coordinates':
        record[logs.SourceCoordinate] = np.array([[10, 10, 10]])
    calls = []
    def read_log_entries(**kwargs):
        calls.append(kwargs)
        return {1: record}
    context.cg = SimpleNamespace(client=SimpleNamespace(read_log_entries=read_log_entries))
    edges, _, _ = cut_setup(monkeypatch, context, cells=cells, data=np.zeros((12, 12, 12), np.uint64))
    with pytest.raises(PaintError) as caught:
        service._check_cut(context, edges, [[A, B]])
    assert caught.value.kind == 'stray_cut' and context.checked == {}
    assert len(calls) == (0 if reason == 'no_cell' else 1)
    if calls:
        assert calls == [{'start_time': timestamp, 'end_time': timestamp + timedelta(milliseconds=1)}]


def test_fake_edge_accepts_exact_creating_merge_and_near_location(monkeypatch, context):
    from pychunkedgraph.graph import attributes
    logs = attributes.OperationLogs
    timestamp = datetime(2026, 10, 21, tzinfo=timezone.utc)
    cell = SimpleNamespace(value=np.array([[B, A]], np.uint64), timestamp=timestamp)
    record = {'timestamp': timestamp, logs.AddedEdge: np.array([[B, A]], np.uint64),
              logs.SourceCoordinate: np.array([[2.5, 2.5, 2.5]])}
    context.cg = SimpleNamespace(client=SimpleNamespace(read_log_entries=lambda **kwargs: {9: record}))
    edges, _, _ = cut_setup(monkeypatch, context, cells=[cell], data=np.zeros((12, 12, 12), np.uint64))
    service._check_cut(context, edges, [[A, B]])
    assert context.checked['removed_edges'] == [[A, B]]


@pytest.mark.parametrize('phase', ['contact_read', 'check'])
def test_late_contact_or_final_check_never_produces_certificate(monkeypatch, context, phase):
    edges, _, _ = cut_setup(monkeypatch, context)
    def expire():
        context.deadline = time.monotonic() - 1
    if phase == 'contact_read':
        data = np.zeros((12, 12, 12), np.uint64)
        data[2:4, 2:5, 2:5], data[4:6, 2:5, 2:5] = A, B
        volume_reader(monkeypatch, data, after_read=expire)
    else:
        monkeypatch.setattr(service, '_fault', lambda name, *args, **kwargs: expire() if name == 'check' else None)
    with pytest.raises(PaintError) as caught:
        service._check_cut(context, edges, [[A, B]])
    assert caught.value.kind == 'deadline' and context.checked == {}


def test_fake_cell_reads_use_unique_l2_chunk_rows(monkeypatch, context):
    from pychunkedgraph.graph import attributes
    calls = []
    cell = SimpleNamespace(value=np.array([[A, B]], np.uint64))
    def read_node(node, **kwargs):
        calls.append((int(node), kwargs))
        return [cell]
    context.cg = SimpleNamespace(get_parents=lambda ids: np.array([2001, 2002], np.uint64),
        get_chunk_ids_from_node_ids=lambda ids: np.array([2000, 2000], np.uint64),
        client=SimpleNamespace(read_node=read_node))
    assert service._fake_cells(context, {A, B}) == [cell]
    assert calls == [(2000, {'properties': attributes.Connectivity.FakeEdges, 'fake_edges': True})]
