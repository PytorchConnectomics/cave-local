"""Pinned-PCG interception contracts with isolated native methods and fake I/O.

Each test replaces the exact native bindings before installation and restores all
of them afterward. No graph database, deployment or network connection is used.
"""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sys

import numpy as np
import pytest

pytest.importorskip('pychunkedgraph')
from google.cloud.bigtable.table import Table
from pychunkedgraph.graph import locks, operation
from pychunkedgraph.graph.client.bigtable.client import Client
from pychunkedgraph.graph.exceptions import PreconditionError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import supervoxel_paint_service as service
from supervoxel_paint import PAINT_LIMITS, PaintError


class Journal:
    def __init__(self, events):
        self.data = {}
        self.events = events
        self.bind_error = None
        self.marker_error = None

    def update(self, **values):
        self.events.append(('journal_update', values))
        self.data.update(values)

    def bind(self, value):
        self.events.append(('bind', value))
        if self.bind_error:
            raise self.bind_error
        self.data['operation_id'] = value

    def hand_off(self):
        self.events.append(('hand_off',))
        if self.marker_error:
            raise self.marker_error
        self.data['handed_to_write'] = True


@pytest.fixture
def hooked(monkeypatch):
    events = []
    clock = [0.0]
    table = SimpleNamespace(name='paint')
    client = SimpleNamespace(_table=table)
    cg = SimpleNamespace(client=client, cache=object())
    state = SimpleNamespace(events=events, clock=clock, faults={}, batches=[['WRITE_STARTED', 'graph'], ['SUCCESS']],
                            delegated=[], table_error=None, write_error=None, apply_error=None,
                            apply_result=object(), solve_result=np.asarray([[1, 2]], np.uint64),
                            indefinite=False, apply_calls=0, solve_calls=0, native_write_calls=0,
                            renew_calls=0, box_offsets=[])

    def release(root, op):
        state.indefinite = False
        events.append(('release_indefinite', int(root), int(op)))
    client.unlock_indefinitely_locked_root = release

    def native_apply(self, *, operation_id, timestamp):
        state.apply_calls += 1
        events.append(('native_apply', int(operation_id)))
        if state.apply_error:
            raise state.apply_error
        return state.apply_result

    def native_solve(*args, **kwargs):
        state.solve_calls += 1
        return state.solve_result

    def native_box(source, sink, offset):
        state.box_offsets.append(np.asarray(offset).copy())
        points = np.concatenate([source, sink])
        return np.asarray([points.min(0) - offset, points.max(0) + offset])

    def native_enter(lock):
        Client.renew_locks(lock.cg.client, lock.root_ids, lock.operation_id)
        lock.acquired = True
        state.indefinite = True
        events.append(('acquired_indefinite',))
        return lock

    def native_renew(self, *args, **kwargs):
        state.renew_calls += 1
        events.append(('native_renew',))
        return True

    def native_mutate(self, rows, *args, **kwargs):
        events.append(('native_mutate', self.name, list(rows)))
        state.delegated.append((self, list(rows)))
        if state.table_error:
            raise state.table_error
        return [True] * len(rows)

    def native_write(self, *args, **kwargs):
        state.native_write_calls += 1
        with locks.IndefiniteRootLock(self.cg, np.uint64(42), [np.uint64(100)]):
            Client.renew_locks(self.cg.client, [np.uint64(100)], np.uint64(42))
            for batch in state.batches:
                Table.mutate_rows(self.cg.client._table, batch)
                if state.write_error:
                    raise state.write_error
        return 'native write result'

    def fault(point, context=None, **values):
        events.append(('fault', point))
        callback = state.faults.get(point)
        if callback:
            callback(context)

    monkeypatch.setattr(operation.MulticutOperation, '_apply', native_apply)
    monkeypatch.setattr(operation, 'run_multicut', native_solve)
    monkeypatch.setattr(operation, 'get_bbox', native_box)
    monkeypatch.setattr(operation.GraphEditOperation, '_write', native_write)
    monkeypatch.setattr(Table, 'mutate_rows', native_mutate)
    monkeypatch.setattr(locks.IndefiniteRootLock, '__enter__', native_enter)
    monkeypatch.setattr(Client, 'renew_locks', native_renew)
    monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]))
    monkeypatch.setattr(service, 'check_deadline', lambda deadline: (
        (_ for _ in ()).throw(PaintError('deadline', 'expired')) if clock[0] >= deadline else None))
    monkeypatch.setattr(service, '_fault', fault)
    monkeypatch.setattr(service, '_lookup', lambda *args, **kwargs: (100, [1], [2], {}))
    monkeypatch.setattr(service, '_check_cut', lambda context, edges, removed: context.checked.update(checked=True))
    service.install_hooks()
    journal = Journal(events)
    context = service.PaintContext(
        {'resolution_nm_xyz': [10, 10, 12.5], 'shape_xyz': [100, 100, 100],
         'graph_chunk_xyz': [10, 10, 10], 'test_fixture': False},
        {**PAINT_LIMITS, 'max_box_voxels': [100, 100, 100]},
        100.0, np.asarray([[1, 1, 1]]), np.asarray([[2, 2, 2]]), 100,
        {1}, {2}, np.asarray([10, 10, 10]), journal, cg=cg, operation_id=42)
    state.context, state.cg, state.table, state.journal = context, cg, table, journal
    state.op = SimpleNamespace(cg=cg)
    state.edges = SimpleNamespace(get_pairs=lambda: np.asarray([[1, 2]], np.uint64),
                                  affinities=np.asarray([1.0]))

    @contextmanager
    def armed():
        token = service.active_paint.set(context)
        try:
            yield
        finally:
            service.active_paint.reset(token)
            assert service.writing_paint.get() is None, 'write scope leaked after operation'
    state.armed = armed
    yield state
    assert service.active_paint.get() is None
    assert service.writing_paint.get() is None


def test_created_mutation_is_excluded_even_when_deadline_has_expired(hooked):
    env = hooked
    env.clock[0] = 101
    with env.armed():
        assert Table.mutate_rows(env.table, ['CREATED']) == [True]
        assert env.context.write_calls == 0 and not env.context.begun
        with pytest.raises(PreconditionError, match='expired'):
            operation.GraphEditOperation._write(env.op)
    assert env.delegated == [(env.table, ['CREATED'])]
    assert env.context.write_entries == 1 and env.context.write_calls == 0
    assert env.journal.data['reason'] == 'deadline_at_persistence_gate'
    assert env.cg.cache is None and not env.indefinite


@pytest.mark.parametrize('point', ['indefinite_lock', 'lock_renewal'])
def test_persistence_gate_follows_both_lock_delay_boundaries(hooked, point):
    env = hooked
    env.faults[point] = lambda context: env.clock.__setitem__(0, 101)
    with env.armed(), pytest.raises(PreconditionError):
        operation.GraphEditOperation._write(env.op)
    assert env.renew_calls == 2 and env.context.renewals == 2
    assert env.context.error.kind == 'deadline'
    assert env.context.write_calls == 0 and env.delegated == []
    assert not env.indefinite and env.cg.cache is None
    assert env.journal.data['write_entries'] == 1
    assert ('release_indefinite', 100, 42) in env.events


def test_deadline_after_marker_aborts_apply_before_any_write(hooked):
    env = hooked
    env.faults['marker'] = lambda context: env.clock.__setitem__(0, 101)
    with env.armed(), pytest.raises(PreconditionError):
        operation.MulticutOperation._apply(env.op, operation_id=42, timestamp=None)
    assert env.apply_calls == 1
    assert env.journal.data['operation_id'] == 42 and env.journal.data['handed_to_write'] is True
    assert env.journal.data['reason'] == 'deadline_after_marker'
    assert env.context.error.kind == 'deadline'
    assert env.native_write_calls == 0 and env.context.write_entries == 0 and env.delegated == []
    assert env.journal.data['write_calls'] == 0 and env.journal.data['write_entries'] == 0


def test_marker_deadline_uses_one_clock_read(hooked, monkeypatch):
    env = hooked
    reads = []

    def crossing_clock():
        reads.append(env.clock[0])
        before = env.clock[0]
        env.clock[0] = 101
        return before

    env.clock[0] = 99
    monkeypatch.setattr(service.time, 'monotonic', crossing_clock)
    with env.armed():
        assert operation.MulticutOperation._apply(env.op, operation_id=42, timestamp=None) is env.apply_result
        # Crossing immediately after the one marker check is caught by the
        # independently required first-persistence gate, before delegation.
        with pytest.raises(PreconditionError):
            operation.GraphEditOperation._write(env.op)
    assert reads == [99]
    assert env.context.error.kind == 'deadline'
    assert env.journal.data['reason'] == 'deadline_at_persistence_gate'
    assert env.delegated == []


@pytest.mark.parametrize('boundary', ['marker', 'indefinite_lock'])
def test_failing_deadline_reason_write_preserves_deadline(hooked, monkeypatch, boundary):
    env = hooked
    original_update = env.journal.update

    def update(**values):
        if 'reason' in values:
            raise OSError('reason storage unavailable')
        original_update(**values)

    monkeypatch.setattr(env.journal, 'update', update)
    env.faults[boundary] = lambda context: env.clock.__setitem__(0, 101)
    with env.armed(), pytest.raises(PreconditionError):
        if boundary == 'marker':
            operation.MulticutOperation._apply(env.op, operation_id=42, timestamp=None)
        else:
            operation.GraphEditOperation._write(env.op)
    assert env.context.error.kind == 'deadline'
    assert 'reason storage unavailable' in env.context.error.__notes__[0]
    assert env.journal.data['write_calls'] == 0
    assert env.journal.data['write_entries'] == (boundary == 'indefinite_lock')
    assert env.delegated == []


def test_binding_delay_checks_deadline_before_marker(hooked):
    env = hooked
    env.faults['binding'] = lambda context: env.clock.__setitem__(0, 101)
    with env.armed(), pytest.raises(PreconditionError):
        operation.MulticutOperation._apply(env.op, operation_id=42, timestamp=None)
    assert env.journal.data['operation_id'] == 42
    assert 'handed_to_write' not in env.journal.data
    assert env.native_write_calls == 0


@pytest.mark.parametrize('failure', ['bind_error', 'marker_error'])
def test_provenance_io_failure_becomes_precondition_before_write(hooked, failure):
    env = hooked
    setattr(env.journal, failure, OSError('durability failed'))
    with env.armed(), pytest.raises(PreconditionError) as raised:
        operation.MulticutOperation._apply(env.op, operation_id=42, timestamp=None)
    assert env.context.error.kind == 'limit'
    assert isinstance(raised.value.__cause__, PaintError)
    assert env.native_write_calls == 0 and env.delegated == []


def test_native_apply_error_never_binds_or_marks(hooked):
    env = hooked
    error = PreconditionError('native solver refused')
    env.apply_error = error
    with env.armed(), pytest.raises(PreconditionError) as raised:
        operation.MulticutOperation._apply(env.op, operation_id=42, timestamp=None)
    assert raised.value is error and env.journal.data == {}


def test_successful_apply_binds_and_marks_before_write_is_entered(hooked):
    env = hooked
    with env.armed():
        result = operation.MulticutOperation._apply(env.op, operation_id=42, timestamp=None)
    assert result is env.apply_result and env.context.cg is env.cg
    assert env.journal.data == {'operation_id': 42, 'handed_to_write': True}
    assert env.context.write_entries == 0 and env.native_write_calls == 0
    names = [event[0] for event in env.events]
    assert names.index('native_apply') < names.index('bind') < names.index('hand_off')


@pytest.mark.parametrize('failure', ['table_error', 'write_error'])
def test_write_errors_clear_edit_cache_and_restore_contextvar(hooked, failure):
    env = hooked
    error = OSError('native write failed')
    setattr(env, failure, error)
    with env.armed(), pytest.raises(OSError) as raised:
        operation.GraphEditOperation._write(env.op)
    assert raised.value is error and env.cg.cache is None
    assert not env.indefinite and service.writing_paint.get() is None
    assert env.context.begun and env.context.write_calls == 1


def test_later_write_blocks_and_success_finish_after_deadline(hooked):
    env = hooked
    env.batches = [['WRITE_STARTED', 'graph-1'], ['graph-2'], ['SUCCESS']]
    env.faults['after_mutate'] = lambda context: env.clock.__setitem__(0, 101)
    with env.armed():
        assert operation.GraphEditOperation._write(env.op) == 'native write result'
    assert env.context.begun and env.context.write_calls == 3
    assert [rows for table, rows in env.delegated] == env.batches
    assert env.context.error is None and env.cg.cache is None and not env.indefinite


def test_unarmed_methods_are_identity_and_do_not_touch_provenance(hooked):
    env = hooked
    assert operation.MulticutOperation._apply(env.op, operation_id=42, timestamp=None) is env.apply_result
    assert operation.run_multicut(env.edges, [1], [2]) is env.solve_result
    assert operation.GraphEditOperation._write(env.op) == 'native write result'
    assert env.journal.data == {} and env.context.write_entries == 0 and env.context.write_calls == 0
    # Native fake does not clear its cache. The paint cleanup must be unarmed too.
    assert env.cg.cache is not None


def test_other_table_is_not_counted_or_gated_inside_painted_write(hooked):
    env = hooked
    other = SimpleNamespace(name='other')
    env.clock[0] = 101
    token = service.writing_paint.set(env.context)
    try:
        assert Table.mutate_rows(other, ['unrelated']) == [True]
    finally:
        service.writing_paint.reset(token)
    assert env.context.write_calls == 0 and not env.context.begun


def test_other_graph_write_is_not_armed(hooked):
    env = hooked
    other_table = SimpleNamespace(name='other')
    other_client = SimpleNamespace(_table=other_table, unlock_indefinitely_locked_root=lambda *args: None)
    other_graph = SimpleNamespace(client=other_client, cache=object())
    with env.armed():
        operation.GraphEditOperation._write(SimpleNamespace(cg=other_graph))
    assert env.context.write_calls == 0 and env.context.write_entries == 0
    assert other_graph.cache is not None


def test_doubled_bounding_offset_is_effective_and_request_scoped(hooked):
    env = hooked
    source, sink = np.asarray([[40, 40, 40]]), np.asarray([[45, 45, 45]])
    with env.armed():
        first = operation.get_bbox(source, sink, np.asarray([99, 99, 99]))
        env.context.offset *= 2
        second = operation.get_bbox(source, sink, np.asarray([99, 99, 99]))
    outside = operation.get_bbox(source, sink, np.asarray([3, 3, 3]))
    assert np.array_equal(first, [[30, 30, 30], [55, 55, 55]])
    assert np.array_equal(second, [[20, 20, 20], [65, 65, 65]])
    assert np.array_equal(outside, [[37, 37, 37], [48, 48, 48]])
    assert [offset.tolist() for offset in env.box_offsets] == [[10]*3, [20]*3, [3]*3]


def test_native_box_limit_accounts_for_full_intersecting_chunks(hooked):
    env = hooked
    env.context.offset = np.asarray([1, 1, 1])
    env.context.limits['max_box_voxels'] = [15, 15, 15]
    with env.armed(), pytest.raises(PreconditionError):
        operation.get_bbox(np.asarray([[9, 9, 9]]), np.asarray([[10, 10, 10]]), [1, 1, 1])
    assert env.context.error.kind == 'limit'


def test_solver_refusal_retains_structured_context_through_precondition(hooked, monkeypatch):
    env = hooked
    error = PaintError('object_changed', 'retired terminals', locations_nm=[[10, 20, 30]], supervoxels=[2**63+3])
    monkeypatch.setattr(service, '_lookup', lambda *args, **kwargs: (_ for _ in ()).throw(error))
    with env.armed(), pytest.raises(PreconditionError) as raised:
        operation.run_multicut(env.edges, [1], [2])
    assert env.context.error is error and raised.value.__cause__ is error
    assert env.context.error.as_dict() == {'kind': 'object_changed', 'locations_nm': [[10, 20, 30]],
                                         'supervoxels': [2**63+3]}
    assert env.solve_calls == 0
    assert env.journal.data['write_calls'] == env.context.write_calls
    assert env.journal.data['write_entries'] == env.context.write_entries


def test_refusal_records_observed_write_counts_not_gate_literals(hooked):
    env = hooked
    env.context.write_calls, env.context.write_entries = 3, 2
    env.clock[0] = 101
    with env.armed(), pytest.raises(PreconditionError):
        operation.run_multicut(env.edges, [1], [2])
    assert env.journal.data['write_calls'] == 3
    assert env.journal.data['write_entries'] == 2


def test_seam_diagnostic_selects_contact_nearest_paint(hooked, monkeypatch):
    env = hooked
    env.context.source_xyz = np.asarray([[14, 5, 35]])
    env.context.sink_xyz = np.asarray([[18, 5, 35]])
    env.edges.affinities = np.asarray([np.inf])
    monkeypatch.setattr(service, '_bounds', lambda *args: {})
    monkeypatch.setattr(service, '_edge_contacts', lambda *args: np.asarray([[15, 2, 34], [15, 5, 35]]))
    with env.armed(), pytest.raises(PreconditionError):
        operation.run_multicut(env.edges, [1], [2])
    assert env.context.error.kind == 'seam'
    assert env.context.error.locations_nm == [((np.asarray([15, 5, 35]) + .5) * env.context.resolution).tolist()]
    assert env.solve_calls == 0 and env.delegated == []


def test_late_solver_result_is_discarded_before_cut_checks(hooked):
    env = hooked
    env.faults['solve'] = lambda context: env.clock.__setitem__(0, 101)
    with env.armed(), pytest.raises(PreconditionError):
        operation.run_multicut(env.edges, [1], [2])
    assert env.solve_calls == 1 and env.context.checked == {}
    assert env.context.error.kind == 'deadline'


def test_successful_solver_returns_exact_native_array_and_checks_it(hooked):
    env = hooked
    with env.armed():
        result = operation.run_multicut(env.edges, [1], [2])
    assert result is env.solve_result and env.context.checked == {'checked': True}


def test_hook_installation_is_idempotent(hooked):
    before = (operation.MulticutOperation._apply, operation.run_multicut,
              operation.GraphEditOperation._write, Table.mutate_rows)
    service.install_hooks()
    assert before == (operation.MulticutOperation._apply, operation.run_multicut,
                      operation.GraphEditOperation._write, Table.mutate_rows)
