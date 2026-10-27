"""Paint journal crash ambiguity and native-id recovery contracts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from enum import Enum
import functools
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from threading import Barrier
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import supervoxel_paint_history as history


class Status(Enum):
    SUCCESS = 0
    CREATED = 1
    EXCEPTION = 2
    WRITE_STARTED = 3
    WRITE_FAILED = 4


ATTRS = SimpleNamespace(OperationLogs=SimpleNamespace(Status='status', StatusCodes=Status, RemovedEdge='removed'),
                        Concurrency=SimpleNamespace(IndefiniteLock='indefinite', Lock='temporal'))


class Graph:
    def __init__(self):
        self.statuses = {}
        self.roots = {1: 100, 2: 100}
        self.locks = {}
        self.log_calls = []
        self.latest = True
        self.removed = [[1, 2]]
        self.lock_ok = True
        self.temporal = set()
        self.client = self

    def read_log_entry(self, operation):
        operation = int(operation)
        self.log_calls.append(operation)
        return ({'status': self.statuses[operation], 'removed': self.removed} if operation in self.statuses else {}), None

    def read_node(self, root, properties=None):
        if properties == 'temporal':
            return []
        return [SimpleNamespace(value=self.locks[int(root)])] if int(root) in self.locks else []

    def is_latest_roots(self, roots):
        return np.full(len(roots), self.latest)

    def lock_roots(self, *, root_ids, **kwargs):
        if self.lock_ok:
            self.temporal.update(map(int, root_ids))
        return self.lock_ok, root_ids

    def renew_locks(self, *args):
        return True

    def lock_roots_indefinitely(self, *, root_ids, operation_id, **kwargs):
        self.locks.update({int(root): int(operation_id) for root in root_ids})
        return True, root_ids, None

    def unlock_indefinitely_locked_root(self, root, operation):
        if self.locks.get(int(root)) == int(operation):
            del self.locks[int(root)]

    def unlock_root(self, root, operation):
        self.temporal.discard(int(root))

    def get_roots(self, atoms):
        assert atoms.dtype == np.uint64
        return np.asarray([self.roots[int(atom)] for atom in atoms], np.uint64)


@pytest.fixture(autouse=True)
def attributes(monkeypatch):
    monkeypatch.setattr(history, '_attributes', lambda: ATTRS)


@pytest.fixture
def graph():
    return Graph()


def journal(tmp_path, name='first', **kwargs):
    settings = dict(data_dir=tmp_path, paint={'version': 1, 'sources': [{'points_nm': [[1.5, 2, 3]], 'radius_nm': 5}],
                                            'sinks': [{'points_nm': [[4, 5, 6]], 'radius_nm': 5}]},
                    user_id='proofreader', start_time=1000, deadline_seconds=60, counts=[3, 4], atoms=[1, 2],
                    lock_lifetime_seconds=180, request_id=name)
    settings.update(kwargs)
    result = history.PaintJournal.create(**settings)
    result.update(roots=[100], sources=[1], sinks=[2], pre_partition=[100, 100],
                  box_voxel_xyz=[[-8, -8, -8], [16, 16, 16]], removed_edges=[[1, 2]])
    return result


def pending(tmp_path, name='first'):
    return tmp_path / 'supervoxel_splits' / 'paint' / 'pending' / (name + '.json')


def test_create_preserves_paint_as_sent_and_integer_ids(tmp_path):
    huge = 2**63 + 123
    paint = {'version': 1, 'sources': [{'points_nm': [[1.125, 2, 3]], 'radius_nm': 8}], 'sinks': []}
    record = history.PaintJournal.create(tmp_path, paint, 7, datetime(2026, 1, 1, tzinfo=timezone.utc),
                                         10, [1, 2], [huge], request_id='exact')
    paint['sources'][0]['radius_nm'] = 99
    disk = json.loads(record.path.read_text())
    assert disk['paint']['sources'][0]['radius_nm'] == 8
    assert disk['painted_atoms'] == [huge] and isinstance(disk['painted_atoms'][0], int)
    assert disk['state'] == 'pending' and 'operation_id' not in disk
    assert record.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('bad', [True, 1.0, -1, 0, 2**64])
def test_operation_ids_reject_lossy_or_invalid_values(tmp_path, bad):
    record = journal(tmp_path)
    with pytest.raises(ValueError):
        record.bind(bad)
    assert 'operation_id' not in json.loads(record.path.read_text())


def test_binding_is_single_operation_and_marker_requires_binding(tmp_path):
    record = journal(tmp_path)
    with pytest.raises(history.PaintHistoryError, match='bound'):
        record.hand_off()
    record.bind(23).bind(23)
    with pytest.raises(history.PaintHistoryError, match='another operation'):
        record.bind(24)
    record.hand_off()
    assert json.loads(record.path.read_text())['handed_to_write'] is True
    with pytest.raises(history.PaintHistoryError, match='protected'):
        record.update(handed_to_write=False)


def test_journal_updates_reread_under_flock_and_do_not_lose_another_writer(tmp_path):
    record = journal(tmp_path)
    another = history.PaintJournal(tmp_path, dict(record.data))
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(record.update, first=1), executor.submit(another.update, second=2)]
        for future in futures:
            future.result()
    disk = json.loads(record.path.read_text())
    assert disk['first'] == 1 and disk['second'] == 2


@pytest.mark.parametrize('bound', [False, True])
def test_unmarked_record_waits_until_deadline_plus_lock_lifetime(tmp_path, graph, bound):
    record = journal(tmp_path)
    if bound:
        record.bind(23)
        graph.statuses[23] = Status.CREATED.value
    report = history.recover_paint(graph, tmp_path, now=1239)[0]
    assert report['state'] == 'pending' and pending(tmp_path).exists()
    report = history.recover_paint(graph, tmp_path, now=1240)[0]
    assert report['state'] == 'not_applied' and not pending(tmp_path).exists()
    assert record.path.parent.name == 'refused'
    assert not graph.log_calls, 'unmarked records must not be matched to later native edits'


@pytest.mark.parametrize('status', [None, 1, 2, 3, 4])
def test_young_handed_record_is_not_misclassified_while_live(tmp_path, graph, status):
    record = journal(tmp_path).bind(23).hand_off()
    if status is not None:
        graph.statuses[23] = status
    report = history.recover_paint(graph, tmp_path, now=1010)[0]
    assert report['state'] == 'pending' and record.path.parent.name == 'pending'


@pytest.mark.parametrize(('status', 'state'), [(None, 'ambiguous'), (1, 'ambiguous'),
                                              (2, 'ambiguous'), (3, 'awaiting_pcg_repair'),
                                              (4, 'ambiguous')])
def test_handoff_status_table_never_infers_unapplied_from_created(tmp_path, graph, status, state):
    record = journal(tmp_path).bind(23).hand_off()
    if status is not None:
        graph.statuses[23] = status
    graph.roots[2] = 200  # Partial graph writes may exist with CREATED or no log.
    report = history.recover_paint(graph, tmp_path, now=1240)[0]
    assert report['state'] == state
    assert report['partition_equals_pre_cut'] is False
    assert report['painted_sides_separated'] is True
    assert record.path.parent.name == 'pending'


def test_success_recovers_immediately_and_finish_race_is_idempotent(tmp_path, graph):
    record = journal(tmp_path).bind(23).hand_off()
    graph.statuses[23] = 0
    graph.roots[2] = 200
    report = history.recover_paint(graph, tmp_path, now=1001)[0]
    assert report['state'] == 'completed' and record.path.name == '23.json'
    assert record.finish(graph)['state'] == 'completed'
    assert history.recover_paint(graph, tmp_path, now=2000) == []


def test_identical_later_request_can_only_complete_its_own_bound_operation(tmp_path, graph):
    first = journal(tmp_path, 'first')
    second = journal(tmp_path, 'second').bind(25).hand_off()
    graph.statuses[25] = 0
    reports = history.recover_paint(graph, tmp_path, now=1300)
    assert {report['request_id']: report['state'] for report in reports} == {
        'first': 'not_applied', 'second': 'completed'}
    assert first.path.parent.name == 'refused' and second.path.name == '25.json'
    assert set(graph.log_calls) == {25}


def test_recovery_uses_exact_bound_id_not_another_success_with_same_terminals(tmp_path, graph):
    record = journal(tmp_path).bind(23).hand_off()
    graph.statuses.update({23: 1, 25: 0})
    report = history.recover_paint(graph, tmp_path, now=1300)[0]
    assert report['state'] == 'ambiguous' and graph.log_calls == [23]
    assert record.path.parent.name == 'pending'


def test_refusal_without_marker_is_certain_and_with_marker_remains_pending(tmp_path, graph):
    before = journal(tmp_path, 'before').bind(23)
    before.refuse(RuntimeError('binding deadline'))
    assert before.data['state'] == 'not_applied' and before.path.parent.name == 'refused'
    after = journal(tmp_path, 'after').bind(24).hand_off()
    graph.statuses[24] = 1
    after.update(reason='deadline_after_marker')
    after.refuse(RuntimeError('deadline'), cg=graph)
    assert after.data['state'] == 'ambiguous' and after.path.parent.name == 'pending'
    assert after.data['reason'] == 'deadline_after_marker'


def test_refusal_retains_structured_kind_and_locations(tmp_path):
    class Refusal(ValueError):
        def as_dict(self):
            return {'kind': 'stray_cut', 'locations_nm': [[1, 2, 3]], 'supervoxels': [2**63 + 7]}
    record = journal(tmp_path)
    record.refuse(Refusal('far contact'))
    assert json.loads(record.path.read_text())['refusal'] == Refusal().as_dict()


def test_forced_reconcile_is_scoped_to_one_failed_request(tmp_path, graph):
    failed = journal(tmp_path, 'failed').bind(23).hand_off()
    active = journal(tmp_path, 'active').bind(24).hand_off()
    graph.statuses.update({23: 1, 24: 1})
    assert failed.reconcile(graph, force=True, now=1001)['state'] == 'ambiguous'
    assert json.loads(active.path.read_text())['state'] == 'pending'


def test_partial_write_report_exposes_lock_owner_and_exact_partitions(tmp_path, graph):
    record = journal(tmp_path).bind(23).hand_off()
    record.update(expected_partition=[0, 1])
    graph.statuses[23] = 3
    graph.locks[100] = 23
    graph.roots[2] = 200
    report = record.reconcile(graph, force=True)
    assert report['state'] == 'awaiting_pcg_repair'
    assert report['roots'] == [100] and report['indefinite_locks'] == {'100': 23}
    assert report['partition'] == [0, 1]
    assert report['partition_equals_pre_cut'] is False and report['partition_equals_expected'] is True


def test_read_failures_do_not_turn_ambiguity_into_partition_evidence(tmp_path, graph, monkeypatch):
    record = journal(tmp_path).bind(23).hand_off()
    graph.statuses[23] = 1
    monkeypatch.setattr(graph, 'get_roots', lambda atoms: (_ for _ in ()).throw(RuntimeError('partial hierarchy')))
    report = record.reconcile(graph)
    assert report['state'] == 'ambiguous'
    assert report['partition_equals_pre_cut'] is None
    assert report['partition_error'] == 'partial hierarchy'


def test_failed_bind_replace_leaves_no_bound_id_or_marker(tmp_path, monkeypatch):
    record = journal(tmp_path)
    original = record.path.read_bytes()
    monkeypatch.setattr(history.os, 'replace', lambda source, dest: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        record.bind(23)
    assert record.path.read_bytes() == original
    assert not list(record.path.parent.glob('*.tmp'))


def test_marker_directory_fsync_failure_is_conservatively_visible(tmp_path, graph, monkeypatch):
    record = journal(tmp_path).bind(23)
    real_sync = history._sync_directory
    def fail_pending(path):
        if Path(path).name == 'pending':
            raise OSError('directory fsync failed')
        real_sync(path)
    monkeypatch.setattr(history, '_sync_directory', fail_pending)
    with pytest.raises(OSError, match='fsync'):
        record.hand_off()
    assert record.data['handed_to_write'] is True
    assert json.loads(record.path.read_text())['handed_to_write'] is True
    monkeypatch.setattr(history, '_sync_directory', real_sync)
    graph.statuses[23] = 1
    record.refuse(OSError('marker failure'), cg=graph)
    assert record.data['state'] == 'ambiguous'


def test_success_archive_rename_failure_recovers_from_pending_bound_id(tmp_path, graph, monkeypatch):
    record = journal(tmp_path).bind(23).hand_off()
    graph.statuses[23] = 0
    native_replace = history.os.replace
    def fail_archive(source, dest):
        if Path(dest).name == '23.json':
            raise OSError('archive rename failed')
        native_replace(source, dest)
    monkeypatch.setattr(history.os, 'replace', fail_archive)
    with pytest.raises(OSError, match='rename'):
        record.finish(graph)
    assert pending(tmp_path).exists()
    monkeypatch.setattr(history.os, 'replace', native_replace)
    assert history.recover_paint(graph, tmp_path, now=1001)[0]['state'] == 'completed'


def test_completed_operation_cannot_be_overwritten_by_other_request(tmp_path, graph):
    first = journal(tmp_path, 'first').bind(23).hand_off()
    graph.statuses[23] = 0
    first.finish(graph)
    before = first.path.read_bytes()
    second = journal(tmp_path, 'second').bind(23).hand_off()
    with pytest.raises(history.PaintHistoryError, match='another paint request'):
        second.finish(graph)
    assert first.path.read_bytes() == before and second.path.parent.name == 'pending'


def test_symlink_records_and_directories_are_rejected(tmp_path, graph):
    record = journal(tmp_path)
    outside = tmp_path / 'outside.json'
    outside.write_text(record.path.read_text())
    original_path = record.path
    original_path.unlink()
    original_path.symlink_to(outside)
    with pytest.raises(history.PaintHistoryError, match='regular file'):
        history.recover_paint(graph, tmp_path, now=1300)
    record.path.unlink()
    directory = pending(tmp_path).parent
    directory.rmdir()
    directory.symlink_to(tmp_path)
    with pytest.raises(history.PaintHistoryError, match='symlink'):
        journal(tmp_path, 'other')


def install_repair(monkeypatch, callback):
    package = ModuleType('pychunkedgraph')
    graph_package = ModuleType('pychunkedgraph.graph')
    operation = ModuleType('pychunkedgraph.graph.operation')
    class MulticutOperation:
        source_ids = np.array([1], np.uint64)
        sink_ids = np.array([2], np.uint64)
    operation.MulticutOperation = MulticutOperation
    operation.GraphEditOperation = SimpleNamespace(from_operation_id=lambda *args, **kwargs: MulticutOperation())
    operation.get_bbox = lambda *args, **kwargs: 'native-box'
    operation.run_multicut = lambda *args, **kwargs: np.array([[1, 2]], np.uint64)
    graph_package.operation = operation
    repair = ModuleType('pychunkedgraph.repair')
    edits = ModuleType('pychunkedgraph.repair.edits')
    edits.repair_operation = callback
    monkeypatch.setitem(sys.modules, 'pychunkedgraph', package)
    monkeypatch.setitem(sys.modules, 'pychunkedgraph.graph', graph_package)
    monkeypatch.setitem(sys.modules, 'pychunkedgraph.graph.operation', operation)
    monkeypatch.setitem(sys.modules, 'pychunkedgraph.repair', repair)
    monkeypatch.setitem(sys.modules, 'pychunkedgraph.repair.edits', edits)
    return operation


@pytest.mark.parametrize('status', [1, 2, 3])
def test_explicit_native_repair_uses_qualified_options_and_finishes_record(tmp_path, graph, monkeypatch, status):
    record = journal(tmp_path).bind(23).hand_off()
    graph.statuses[23] = status
    if status == 3:
        graph.locks[100] = 23
    calls = []
    def repair(cg, operation, **kwargs):
        calls.append((cg, int(operation), kwargs))
        np.testing.assert_array_equal(native.get_bbox([], [], [99, 99, 99]), record.data['box_voxel_xyz'])
        cg.statuses[int(operation)] = 0
        cg.roots[2] = 200
    native = install_repair(monkeypatch, repair)
    report = history.repair_paint(graph, 23, tmp_path)
    assert report['state'] == 'completed' and record.path.name == '23.json'
    assert calls == [(graph, 23, {'unlock': False, 'use_preceding_edit_ts': False})]
    assert native.get_bbox([], [], []) == 'native-box'


@pytest.mark.parametrize('status', [None, 0, 4])
def test_repair_rejects_missing_completed_or_unsupported_log(tmp_path, graph, monkeypatch, status):
    journal(tmp_path).bind(23).hand_off()
    if status is not None:
        graph.statuses[23] = status
    install_repair(monkeypatch, lambda *args, **kwargs: pytest.fail('must not replay native cut'))
    with pytest.raises(history.PaintHistoryError, match='WRITE_STARTED'):
        history.repair_paint(graph, 23, tmp_path)


def test_repair_requires_unique_bound_request_and_keeps_pending_on_failure(tmp_path, graph, monkeypatch):
    first = journal(tmp_path, 'first').bind(23).hand_off()
    second = journal(tmp_path, 'second').bind(23).hand_off()
    graph.statuses[23] = 3
    with pytest.raises(history.PaintHistoryError, match='exactly one'):
        history.repair_paint(graph, 23, tmp_path)
    second.path.unlink()
    graph.locks[100] = 23
    install_repair(monkeypatch, lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('repair failed')))
    with pytest.raises(RuntimeError, match='repair failed'):
        history.repair_paint(graph, 23, tmp_path)
    assert first.path.parent.name == 'pending'


def test_repair_requires_marker_even_with_reconstructible_created_log(tmp_path, graph, monkeypatch):
    journal(tmp_path).bind(23)
    graph.statuses[23] = 1
    install_repair(monkeypatch, lambda *args, **kwargs: pytest.fail('markerless replay'))
    with pytest.raises(history.PaintHistoryError, match='handed-off'):
        history.repair_paint(graph, 23, tmp_path)


@pytest.mark.parametrize('failure', ['malformed_log', 'wrong_type', 'wrong_terminals', 'missing_box'])
def test_repair_requires_reconstructible_matching_native_cut(tmp_path, graph, monkeypatch, failure):
    record = journal(tmp_path).bind(23).hand_off()
    graph.statuses[23] = 1
    native = install_repair(monkeypatch, lambda *args, **kwargs: pytest.fail('invalid replay'))
    if failure == 'malformed_log':
        def fail(*args, **kwargs):
            raise KeyError('SourceID')
        native.GraphEditOperation.from_operation_id = fail
    elif failure == 'wrong_type':
        native.GraphEditOperation.from_operation_id = lambda *args, **kwargs: object()
    elif failure == 'wrong_terminals':
        native.MulticutOperation.sink_ids = np.array([3], np.uint64)
    else:
        record.update(box_voxel_xyz=None)
    with pytest.raises(history.PaintHistoryError):
        history.repair_paint(graph, 23, tmp_path)
    assert record.path.parent.name == 'pending'


@pytest.mark.parametrize('box', [None, [[1, 2, 3]], [[1., 2, 3], [4, 5, 6]],
                                [[False, 2, 3], [4, 5, 6]], [[float('inf'), 2, 3], [4, 5, 6]],
                                [[0, 0, 0], [0, 1, 1]], [[0, 0, 0], [-1, 1, 1]],
                                [[0, 0, 0], [2**64, 1, 1]]])
def test_repair_box_rejects_malformed_nonintegral_or_empty_bounds(box):
    with pytest.raises(history.PaintHistoryError):
        history._recorded_box({'box_voxel_xyz': box})


@pytest.mark.parametrize('service_installed_first', [True, False])
def test_repair_box_context_composes_with_service_wrapper_and_resets(graph, monkeypatch, service_installed_first):
    native = install_repair(monkeypatch, lambda *args, **kwargs: None)
    record = {'sources': [1], 'sinks': [2], 'removed_edges': [[1, 2]], 'box_voxel_xyz': [[-1, -2, -3], [4, 5, 6]]}
    def install_service_identity():
        original = native.get_bbox
        @functools.wraps(original)
        def service_box(*args, **kwargs):
            return original(*args, **kwargs)
        native.get_bbox = service_box
    if service_installed_first:
        install_service_identity()
    with history._native_repair_context(graph, 23, record):
        if not service_installed_first:
            install_service_identity()
        np.testing.assert_array_equal(native.get_bbox([], [], []), record['box_voxel_xyz'])
    assert native.get_bbox([], [], []) == 'native-box'
    wrapper = native.get_bbox
    with pytest.raises(RuntimeError, match='failed'):
        with history._native_repair_context(graph, 23, record):
            assert native.get_bbox is wrapper
            raise RuntimeError('failed')
    assert native.get_bbox([], [], []) == 'native-box'


def test_repair_boxes_are_isolated_between_concurrent_contexts(graph, monkeypatch):
    native = install_repair(monkeypatch, lambda *args, **kwargs: None)
    barrier = Barrier(2)
    def repair_context(offset):
        record = {'sources': [1], 'sinks': [2], 'removed_edges': [[1, 2]], 'box_voxel_xyz':
                  [[offset, offset, offset], [offset + 10, offset + 20, offset + 30]]}
        with history._native_repair_context(graph, 23, record):
            barrier.wait(timeout=10)
            actual = native.get_bbox([], [], [])
            np.testing.assert_array_equal(actual, record['box_voxel_xyz'])
            actual[0, 0] = -999
            np.testing.assert_array_equal(native.get_bbox([], [], []), record['box_voxel_xyz'])
        assert native.get_bbox([], [], []) == 'native-box'
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(repair_context, offset) for offset in (0, 100)]
        for future in futures:
            future.result(timeout=20)


def test_recovery_never_calls_native_repair_without_explicit_request(tmp_path, graph, monkeypatch):
    journal(tmp_path).bind(23).hand_off()
    graph.statuses[23] = 3
    install_repair(monkeypatch, lambda *args, **kwargs: pytest.fail('implicit repair'))
    assert history.recover_paint(graph, tmp_path, now=1300)[0]['state'] == 'awaiting_pcg_repair'


@pytest.mark.parametrize('status', [1, 2, 3])
def test_repair_refuses_request_within_lease(tmp_path, graph, monkeypatch, status):
    journal(tmp_path).bind(23).hand_off()
    graph.statuses[23] = status
    graph.locks[100] = 23
    install_repair(monkeypatch, lambda *a, **k: pytest.fail('live replay'))
    monkeypatch.setattr(history.time, 'time', lambda: 1239.9)
    with pytest.raises(history.PaintHistoryError, match='lease'):
        history.repair_paint(graph, 23, tmp_path)
    assert graph.locks[100] == 23


@pytest.mark.parametrize('status', [1, 2])
def test_gate_refused_repair_rejects_superseded_root(tmp_path, graph, monkeypatch, status):
    record = journal(tmp_path).bind(23).hand_off()
    record.update(reason='deadline_at_persistence_gate')
    graph.statuses[23] = status
    graph.latest = False
    install_repair(monkeypatch, lambda *a, **k: pytest.fail('stale replay'))
    with pytest.raises(history.PaintHistoryError, match='superseded'):
        history.repair_paint(graph, 23, tmp_path)
    assert not graph.locks and not graph.temporal


@pytest.mark.parametrize('owner', [None, 24])
def test_write_started_repair_requires_own_indefinite_lock(tmp_path, graph, monkeypatch, owner):
    journal(tmp_path).bind(23).hand_off()
    graph.statuses[23] = 3
    if owner is not None:
        graph.locks[100] = owner
    install_repair(monkeypatch, lambda *a, **k: pytest.fail('unowned replay'))
    with pytest.raises(history.PaintHistoryError, match='indefinite'):
        history.repair_paint(graph, 23, tmp_path)
    assert graph.locks.get(100) == owner


def test_created_repair_cannot_race_concurrent_root_lock(tmp_path, graph, monkeypatch):
    journal(tmp_path).bind(23).hand_off()
    graph.statuses[23] = 1
    graph.lock_ok = False
    install_repair(monkeypatch, lambda *a, **k: pytest.fail('unlocked replay'))
    with pytest.raises(history.PaintHistoryError, match='acquire'):
        history.repair_paint(graph, 23, tmp_path)
    assert not graph.locks


def test_created_repair_rechecks_latest_root_under_lock(tmp_path, graph, monkeypatch):
    journal(tmp_path).bind(23).hand_off()
    graph.statuses[23] = 1
    native_lock = graph.lock_roots
    def lock(**kwargs):
        graph.latest = False
        return native_lock(**kwargs)
    monkeypatch.setattr(graph, 'lock_roots', lock)
    install_repair(monkeypatch, lambda *a, **k: pytest.fail('stale replay'))
    with pytest.raises(history.PaintHistoryError, match='superseded'):
        history.repair_paint(graph, 23, tmp_path)
    assert not graph.temporal and not graph.locks


def test_repair_rejects_solver_edge_mismatch_before_graph_removal(tmp_path, graph, monkeypatch):
    record = journal(tmp_path).bind(23).hand_off()
    graph.statuses[23] = 1
    def repair(*args, **kwargs):
        native.run_multicut()
        pytest.fail('must refuse before remove_edges or writing graph rows')
    native = install_repair(monkeypatch, repair)
    native.run_multicut = lambda: np.array([[1, 3]], np.uint64)
    with pytest.raises(history.PaintHistoryError, match='removed edges differ'):
        history.repair_paint(graph, 23, tmp_path)
    assert record.path.parent.name == 'pending'
    assert history._repair_edges.get() is None


def test_repair_checks_logged_edges_before_unlock_or_completion(tmp_path, graph, monkeypatch):
    record = journal(tmp_path).bind(23).hand_off()
    graph.statuses[23] = 1
    def repair(cg, *args, **kwargs):
        assert cg.locks[100] == 23
        cg.statuses[23] = 0
        cg.removed = [[1, 3]]
    install_repair(monkeypatch, repair)
    with pytest.raises(history.PaintHistoryError, match='removed edges differ'):
        history.repair_paint(graph, 23, tmp_path)
    assert graph.locks[100] == 23
    assert record.path.parent.name == 'pending'
    assert history.recover_paint(graph, tmp_path, now=1300)[0]['state'] == 'ambiguous'
