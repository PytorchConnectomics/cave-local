"""Durable paint provenance and conservative native-operation recovery.

Paint records are bound to one native operation before its graph write. A durable
hand-off marker means that a partial multi-row write is possible: CREATED alone
can never prove that such a request was not applied. Recovery never guesses an
operation from matching paint, terminals, user, or timestamps.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import functools
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

import numpy as np


_repair_solve_box = ContextVar('cave_paint_repair_solve_box', default=None)
_repair_edges = ContextVar('cave_paint_repair_edges', default=None)
_repair_box_install_lock = threading.Lock()


class PaintHistoryError(RuntimeError):
    """Provenance cannot be persisted or safely reconciled."""


def _json_default(value):
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Cannot serialize paint provenance value {type(value).__name__}")


def _copy(value):
    return json.loads(json.dumps(value, default=_json_default, allow_nan=False))


def _seconds(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('Paint start time must include a timezone')
        value = value.timestamp()
    elif isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return _seconds(value)
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Paint time must be finite')
    return value


def _id(value):
    if isinstance(value, (bool, float, np.floating)):
        raise ValueError('Operation and atom ids must be exact unsigned integers')
    result = int(value)
    if result <= 0 or result >= 2**64:
        raise ValueError('Operation and atom ids must be positive uint64 values')
    return result


def _attributes():
    from pychunkedgraph.graph import attributes
    return attributes


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    """Replace a regular JSON file durably; a failed replace leaves its old bytes."""
    path = Path(path)
    if path.is_symlink():
        raise PaintHistoryError(f'Paint record must be a regular file: {path}')
    content = (json.dumps(value, indent=2, sort_keys=True, default=_json_default,
                          allow_nan=False) + '\n').encode()
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _directories(data_dir):
    root = Path(data_dir) / 'supervoxel_splits' / 'paint'
    # These files are included by the ordinary /data backup. Do not introduce
    # symlink-backed provenance or let an existing link redirect a recovery.
    for directory in (root.parent, root, root / 'pending', root / 'refused'):
        if directory.is_symlink():
            raise PaintHistoryError(f'Paint provenance directory is a symlink: {directory}')
        existed = directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        if not existed:
            _sync_directory(directory.parent)
    return root


@contextmanager
def paint_lock(data_dir='/data'):
    root = _directories(data_dir)
    descriptor = os.open(root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield root
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise PaintHistoryError(f'Paint record must be a regular file: {path}')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('version') != 1:
        raise PaintHistoryError(f'Invalid paint provenance record: {path}')
    return value


def _move(root, path, record, destination):
    if destination.exists():
        existing = _read(destination)
        if existing.get('request_id') != record['request_id']:
            raise PaintHistoryError('Native operation is already bound to another paint request')
        # A completed destination must never be replaced with an older pending
        # version. This also makes a worker/response race idempotent.
        if existing.get('state') == record['state']:
            path.unlink(missing_ok=True)
            _sync_directory(path.parent)
            return existing
        raise PaintHistoryError('Conflicting archived paint record')
    atomic_json(path, record)
    os.replace(path, destination)
    _sync_directory(destination.parent)
    if path.parent != destination.parent:
        _sync_directory(path.parent)
    return record


def _status(cg, operation_id):
    attributes = _attributes()
    log, timestamp = cg.client.read_log_entry(np.uint64(operation_id))
    raw = log.get(attributes.OperationLogs.Status) if log else None
    status = int(raw) if raw is not None else None
    names = {item.value: item.name for item in attributes.OperationLogs.StatusCodes}
    return log, names.get(status, 'UNKNOWN' if status is not None else None), timestamp


def _canonical(values):
    mapping = {}
    return [mapping.setdefault(int(value), len(mapping)) for value in values]


def _report(cg, record):
    """Report uncertain operations without treating read failures as evidence."""
    attributes = _attributes()
    roots = sorted({_id(value) for value in record.get('roots', record.get('old_roots', []))})
    result = {'request_id': record['request_id'], 'operation_id': record.get('operation_id'),
              'state': record.get('state', 'pending'), 'reason': record.get('reason'),
              'roots': roots, 'indefinite_locks': {}, 'partition': None,
              'partition_equals_pre_cut': None, 'partition_equals_expected': None,
              'painted_sides_separated': None}
    for root in roots:
        try:
            cells = cg.client.read_node(np.uint64(root), properties=attributes.Concurrency.IndefiniteLock)
            result['indefinite_locks'][str(root)] = int(cells[0].value) if cells else None
        except Exception as error:
            result.setdefault('lock_errors', {})[str(root)] = str(error)
    sources = sorted({_id(value) for value in record.get('sources', [])})
    sinks = sorted({_id(value) for value in record.get('sinks', [])})
    atoms = sorted({_id(value) for value in record.get('painted_atoms', [])} | set(sources) | set(sinks))
    result['painted_atoms'] = atoms
    if not atoms:
        return result
    try:
        roots_now = list(map(int, cg.get_roots(np.asarray(atoms, np.uint64))))
        if len(roots_now) != len(atoms):
            raise PaintHistoryError('Native root lookup returned the wrong number of painted atoms')
        partition = _canonical(roots_now)
        result['partition'] = partition
        result['current_roots'] = dict(zip(map(str, atoms), roots_now))
        before = record.get('pre_partition', [0] * len(atoms))
        result['partition_equals_pre_cut'] = partition == _canonical(before) if len(before) == len(atoms) else None
        by_atom = dict(zip(atoms, roots_now))
        if sources and sinks:
            result['painted_sides_separated'] = not ({by_atom[atom] for atom in sources}
                                                   & {by_atom[atom] for atom in sinks})
        expected = record.get('expected_partition')
        if expected is not None:
            result['partition_equals_expected'] = (partition == _canonical(expected)
                                                   if len(expected) == len(atoms) else None)
        else:
            result['partition_equals_expected'] = result['painted_sides_separated']
    except Exception as error:
        result['partition_error'] = str(error)
    return result


def _reconcile(cg, root, path, record, now, *, force=False):
    operation_id = record.get('operation_id')
    handed = record.get('handed_to_write') is True
    expires = record['start_time'] + record['deadline_seconds'] + record['lock_lifetime_seconds']
    old_enough = now >= expires
    status = None
    if operation_id is not None and handed:
        _, status, _ = _status(cg, operation_id)
    if record.get('repair_validation_error'):
        record.update(state='ambiguous', native_status=status, reconciled_at=now)
        atomic_json(path, record)
    elif operation_id is not None and handed and status == 'SUCCESS':
        record.update(state='completed', native_status=status, completed_at=now)
        record = _move(root, path, record, root / f'{_id(operation_id)}.json')
    elif not force and not old_enough:
        # Another worker may scan while the owning request is computing/writing.
        # A successful operation can be completed now; other statuses cannot
        # distinguish a live operation from a crashed one until its lease expires.
        pass
    elif not handed:
        record.update(state='not_applied', native_status=status, reconciled_at=now)
        record = _move(root, path, record, root / 'refused' / (record['request_id'] + '.json'))
    else:
        record.update(state='awaiting_pcg_repair' if status == 'WRITE_STARTED' else 'ambiguous',
                      native_status=status, reconciled_at=now)
        atomic_json(path, record)
    report = _report(cg, record)
    report['native_status'] = status
    return record, report


class PaintJournal:
    def __init__(self, data_dir, data):
        self.data_dir = Path(data_dir)
        self.data = data

    @property
    def request_id(self):
        return self.data['request_id']

    @property
    def operation_id(self):
        return self.data.get('operation_id')

    @property
    def path(self):
        root = self.data_dir / 'supervoxel_splits' / 'paint'
        pending = root / 'pending' / (self.request_id + '.json')
        if pending.exists():
            return pending
        if self.operation_id is not None and (root / f'{self.operation_id}.json').exists():
            return root / f'{self.operation_id}.json'
        return root / 'refused' / (self.request_id + '.json')

    @classmethod
    def create(cls, data_dir, paint, user_id, start_time, deadline_seconds, counts, atoms,
               *, lock_lifetime_seconds=180, request_id=None):
        request_id = request_id or uuid.uuid4().hex
        if not isinstance(request_id, str) or not request_id or not all(
                character.isascii() and (character.isalnum() or character in '-_') for character in request_id):
            raise ValueError('Invalid paint request id')
        duration, lifetime = float(deadline_seconds), float(lock_lifetime_seconds)
        if not math.isfinite(duration) or duration <= 0 or not math.isfinite(lifetime) or lifetime < 0:
            raise ValueError('Paint duration and root-lock lifetime must be finite and nonnegative')
        record = _copy({'version': 1, 'request_id': request_id, 'paint': paint,
                        'user_id': str(user_id), 'start_time': _seconds(start_time),
                        'deadline_seconds': duration, 'lock_lifetime_seconds': lifetime,
                        'rasterized_counts': counts, 'painted_atoms': sorted({_id(atom) for atom in atoms}),
                        'refinement_operation_ids': [], 'state': 'pending'})
        with paint_lock(data_dir) as root:
            path = root / 'pending' / (request_id + '.json')
            if path.exists() or (root / 'refused' / path.name).exists():
                raise PaintHistoryError('Paint request id already exists')
            atomic_json(path, record)
        return cls(data_dir, record)

    def _current(self, root):
        path = self.path
        record = _read(path)
        if record['request_id'] != self.request_id:
            raise PaintHistoryError('Paint record belongs to another request')
        return path, record

    def update(self, **changes):
        if set(changes) & {'version', 'request_id', 'operation_id', 'handed_to_write', 'state'}:
            raise PaintHistoryError('Use journal transitions to change protected provenance fields')
        with paint_lock(self.data_dir) as root:
            path, record = self._current(root)
            if path.parent != root / 'pending':
                raise PaintHistoryError('Cannot update an archived paint record')
            record.update(_copy(changes))
            atomic_json(path, record)
            self.data = record
        return self

    def bind(self, operation_id):
        operation_id = _id(operation_id)
        with paint_lock(self.data_dir) as root:
            path, record = self._current(root)
            if path.parent != root / 'pending':
                raise PaintHistoryError('Cannot bind an archived paint request')
            if record.get('operation_id') not in (None, operation_id):
                raise PaintHistoryError('Paint request is already bound to another operation')
            record['operation_id'] = operation_id
            atomic_json(path, record)
            self.data = record
        return self

    def hand_off(self):
        with paint_lock(self.data_dir) as root:
            path, record = self._current(root)
            if record.get('operation_id') is None or path.parent != root / 'pending':
                raise PaintHistoryError('Paint request must be bound before hand-off')
            record['handed_to_write'] = True
            record['handed_to_write_at'] = time.time()
            # replace may succeed before directory fsync fails. Signal the
            # conservative error path in memory even when durability raises;
            # reconciliation re-reads the file to determine actual marker state.
            self.data = record
            atomic_json(path, record)
        return self

    def finish(self, cg=None):
        with paint_lock(self.data_dir) as root:
            path, record = self._current(root)
            if record.get('state') == 'completed':
                self.data = record
                return record
            if record.get('operation_id') is None or record.get('handed_to_write') is not True:
                raise PaintHistoryError('Cannot complete paint without a bound, handed-off operation')
            if cg is not None and _status(cg, record['operation_id'])[1] != 'SUCCESS':
                raise PaintHistoryError('Native paint operation has not reached SUCCESS')
            record.update(state='completed', native_status='SUCCESS', completed_at=time.time())
            self.data = _move(root, path, record, root / f'{record["operation_id"]}.json')
        return self.data

    def refuse(self, error, cg=None):
        with paint_lock(self.data_dir) as root:
            path, record = self._current(root)
            if path.parent != root / 'pending':
                self.data = record
                return record
            record['error'] = str(error)
            details = getattr(error, 'details', None)
            if details is None:
                details = getattr(error, 'paint_refusal', None)
            if details is None and callable(getattr(error, 'as_dict', None)):
                details = error.as_dict()
            if details is not None:
                record['refusal'] = _copy(details)
            if record.get('handed_to_write') is True:
                atomic_json(path, record)
                if cg is not None:
                    record, _ = _reconcile(cg, root, path, record, time.time(), force=True)
            else:
                record.update(state='not_applied', outcome='refused', refused_at=time.time())
                record = _move(root, path, record, root / 'refused' / (self.request_id + '.json'))
            self.data = record
        return self.data

    def reconcile(self, cg, *, force=True, now=None):
        with paint_lock(self.data_dir) as root:
            path, record = self._current(root)
            if path.parent == root / 'pending':
                record, report = _reconcile(cg, root, path, record,
                                            time.time() if now is None else _seconds(now), force=force)
            else:
                report = _report(cg, record)
            self.data = record
        return report


def recover_paint(cg, data_dir='/data', now=None, *, force=False, request_id=None):
    """Reconcile only bound ids; young live records stay pending unless forced."""
    now = time.time() if now is None else _seconds(now)
    reports = []
    with paint_lock(data_dir) as root:
        for path in sorted((root / 'pending').glob('*.json')):
            record = _read(path)
            if request_id is not None and record['request_id'] != request_id:
                continue
            _, report = _reconcile(cg, root, path, record, now, force=force)
            reports.append(report)
    return reports


def _recorded_box(record):
    try:
        box = np.asarray(record.get('box_voxel_xyz'), dtype=object)
    except (TypeError, ValueError) as error:
        raise PaintHistoryError('Native repair requires a recorded integer voxel box') from error
    bounds = np.iinfo(np.int64)
    if (box.shape != (2, 3)
            or any(isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer))
                   or not bounds.min <= int(value) <= bounds.max for value in box.flat)):
        raise PaintHistoryError('Native repair requires a finite int64 voxel box with shape 2 by 3')
    box = box.astype(np.int64)
    if np.any(box[1] <= box[0]):
        raise PaintHistoryError('Native repair requires positive recorded voxel-box extents')
    return box


def _edge_set(values):
    try:
        edges = np.asarray(values, dtype=object)
        if edges.ndim != 2 or edges.shape[1] != 2 or not len(edges):
            raise ValueError('empty or malformed edges')
        return {tuple(sorted((_id(a), _id(b)))) for a, b in edges}
    except (TypeError, ValueError) as error:
        raise PaintHistoryError('Native repair requires valid recorded removed edges') from error


@contextmanager
def _native_repair_context(cg, operation_id, record):
    """Reconstruct only the bound native cut and replay its validated solve box."""
    from pychunkedgraph.graph import operation
    box = _recorded_box(record)
    expected_edges = _edge_set(record.get('removed_edges'))
    try:
        restored = operation.GraphEditOperation.from_operation_id(
            cg, np.uint64(operation_id), multicut_as_split=False, privileged_mode=True)
    except Exception as error:
        raise PaintHistoryError('Bound native paint log cannot be reconstructed') from error
    if not isinstance(restored, operation.MulticutOperation):
        raise PaintHistoryError('Bound native paint log must reconstruct a multicut operation')
    for side in ('source', 'sink'):
        try:
            expected = sorted(_id(value) for value in record[side + 's'])
            actual = sorted(_id(value) for value in getattr(restored, side + '_ids'))
        except (KeyError, AttributeError, TypeError, ValueError) as error:
            raise PaintHistoryError('Bound paint terminals are missing or invalid') from error
        if not expected or actual != expected:
            raise PaintHistoryError('Bound native terminals differ from the paint record')
    # This fork's MulticutOperation reads the graph default instead of its
    # logged bbox_offset. Replay the exact validated box, including a doubled
    # retry. The identity wrapper remains safe for unarmed concurrent requests
    # and composes with the service's own ContextVar wrapper in either order.
    with _repair_box_install_lock:
        if not getattr(operation.get_bbox, '_cave_paint_repair_box', False):
            native_box = operation.get_bbox

            @functools.wraps(native_box)
            def box_for_repair(*args, **kwargs):
                current = _repair_solve_box.get()
                return current.copy() if current is not None else native_box(*args, **kwargs)

            box_for_repair._cave_paint_repair_box = True
            operation.get_bbox = box_for_repair
        if not getattr(operation.run_multicut, '_cave_paint_repair_edges', False):
            native_multicut = operation.run_multicut

            @functools.wraps(native_multicut)
            def checked_multicut(*args, **kwargs):
                edges = native_multicut(*args, **kwargs)
                expected = _repair_edges.get()
                if expected is not None and _edge_set(edges) != expected:
                    raise PaintHistoryError('Repaired removed edges differ from the validated paint cut')
                return edges

            checked_multicut._cave_paint_repair_edges = True
            operation.run_multicut = checked_multicut
    token = _repair_solve_box.set(box)
    edge_token = _repair_edges.set(expected_edges)
    try:
        yield restored
    finally:
        _repair_edges.reset(edge_token)
        _repair_solve_box.reset(token)


@contextmanager
def _repair_root_guard(cg, operation_id, record, status):
    """Fence the exact recorded roots, never a newer lineage, during replay."""
    expires = (_seconds(record['start_time']) + float(record['deadline_seconds'])
               + float(record['lock_lifetime_seconds']))
    if not math.isfinite(expires) or time.time() < expires:
        raise PaintHistoryError('Native repair refuses a request within its deadline and lock lease')
    roots = np.asarray(sorted({_id(value) for value in record.get('roots', [])}), np.uint64)
    if not len(roots):
        raise PaintHistoryError('Native repair requires recorded roots')
    attrs = _attributes()

    def owner(root):
        cells = cg.client.read_node(root, properties=attrs.Concurrency.IndefiniteLock)
        return int(cells[0].value) if cells else None

    owners = {int(root): owner(root) for root in roots}
    if any(value not in (None, operation_id) for value in owners.values()):
        raise PaintHistoryError('A recorded root is indefinitely locked by another operation')
    for root in roots:
        cells = cg.client.read_node(root, properties=attrs.Concurrency.Lock)
        if (cells and int(cells[0].value) != operation_id
                and _seconds(cells[0].timestamp) + float(record['lock_lifetime_seconds']) > time.time()):
            raise PaintHistoryError('A recorded root is temporarily locked by another operation')
    if status == 'WRITE_STARTED' and any(value != operation_id for value in owners.values()):
        raise PaintHistoryError('WRITE_STARTED repair requires every indefinite lock owned by this operation')

    def require_latest():
        current = np.asarray(cg.is_latest_roots(roots), dtype=bool)
        if current.shape != roots.shape or not current.all():
            raise PaintHistoryError('Native repair refuses superseded recorded roots')

    if status != 'WRITE_STARTED':
        require_latest()
    # A previous crash can retain our own indefinite locks. Otherwise acquire
    # normal locks on the exact roots, without following future root ids, then
    # keep indefinite locks through replay and the post-write edge comparison.
    unlocked = np.asarray([root for root in roots if owners[int(root)] is None], np.uint64)
    temporal = []
    acquired = []
    replay_started = False
    try:
        if len(unlocked):
            futures = {root: np.array([], np.uint64) for root in unlocked}
            ok, locked = cg.client.lock_roots(root_ids=unlocked, operation_id=np.uint64(operation_id),
                                              future_root_ids_d=futures, max_tries=1)
            if not ok:
                raise PaintHistoryError('Native repair could not acquire the recorded root locks')
            temporal = list(locked)
            if set(map(int, locked)) != set(map(int, unlocked)):
                raise PaintHistoryError('Native repair cannot follow superseded roots')
            require_latest()
            if not cg.client.renew_locks(unlocked, np.uint64(operation_id)):
                raise PaintHistoryError('Native repair could not renew the recorded root locks')
            ok, locked, _ = cg.client.lock_roots_indefinitely(
                root_ids=unlocked, operation_id=np.uint64(operation_id), future_root_ids_d=futures)
            if not ok:
                raise PaintHistoryError('Native repair could not fence the recorded roots')
            acquired = list(locked)
            if set(map(int, acquired)) != set(map(int, unlocked)):
                raise PaintHistoryError('Native repair cannot fence a different root lineage')
        if status != 'WRITE_STARTED':
            require_latest()
        if any(owner(root) != operation_id for root in roots):
            raise PaintHistoryError('Native repair lost a recorded indefinite root lock')
        # Status must not have changed while the locks were acquired.
        if _status(cg, operation_id)[1] != status:
            raise PaintHistoryError('Native status changed before repair')
        replay_started = True
        yield
    except BaseException:
        # Once replay starts a failed write may be partial. Keep its indefinite
        # locks for explicit recovery; only release newly acquired preflight locks.
        if not replay_started:
            for root in acquired:
                cg.client.unlock_indefinitely_locked_root(root, np.uint64(operation_id))
        raise
    else:
        for root in roots:
            cg.client.unlock_indefinitely_locked_root(root, np.uint64(operation_id))
    finally:
        for root in temporal:
            cg.client.unlock_root(root, np.uint64(operation_id))


def repair_paint(cg, operation_id, data_dir='/data'):
    """Explicitly finish one bound native cut, even if no rows were persisted."""
    operation_id = _id(operation_id)
    with paint_lock(data_dir) as root:
        matches = [(path, _read(path)) for path in sorted((root / 'pending').glob('*.json'))]
        matches = [(path, record) for path, record in matches if record.get('operation_id') == operation_id]
        if len(matches) != 1:
            raise PaintHistoryError('PCG repair requires exactly one bound pending paint record')
        path, record = matches[0]
        status = _status(cg, operation_id)[1]
        if (record.get('handed_to_write') is not True
                or status not in ('CREATED', 'EXCEPTION', 'WRITE_STARTED')):
            raise PaintHistoryError('Native repair requires a handed-off CREATED, EXCEPTION or WRITE_STARTED log')
        from pychunkedgraph.repair.edits import repair_operation
        with _native_repair_context(cg, operation_id, record), _repair_root_guard(cg, operation_id, record, status):
            repair_operation(cg, np.uint64(operation_id), unlock=False, use_preceding_edit_ts=False)
            log, repaired_status, _ = _status(cg, operation_id)
            if repaired_status != 'SUCCESS':
                raise PaintHistoryError('Native repair did not reach SUCCESS; provenance remains pending')
            try:
                if _edge_set(log.get(_attributes().OperationLogs.RemovedEdge)) != _edge_set(record['removed_edges']):
                    raise PaintHistoryError('Repaired removed edges differ from the validated paint cut')
            except PaintHistoryError as error:
                record.update(repair_validation_error=str(error), state='ambiguous')
                atomic_json(path, record)
                raise
        record, report = _reconcile(cg, root, path, record, time.time(), force=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repair', action='store_true', help='Repair refinement journals and report paint state')
    parser.add_argument('--pcg-repair', type=int, metavar='OPERATION_ID',
                        help='Explicitly finish one bound paint operation, including a possibly unapplied cut')
    parser.add_argument('--data-dir', default='/data')
    parser.add_argument('--graph-id')
    args = parser.parse_args(argv)
    from pychunkedgraph.graph.chunkedgraph import ChunkedGraph
    from supervoxel_refine import recover, install_fence
    from supervoxel_history import install_history
    graph_id = args.graph_id or json.loads(Path('/workspace/deployment.json').read_text())['graph_id']
    install_fence(data_dir=args.data_dir)
    install_history(data_dir=args.data_dir)
    cg = ChunkedGraph(graph_id=graph_id)
    refinement = recover(cg, data_dir=args.data_dir, repair=args.repair)
    repaired = repair_paint(cg, args.pcg_repair, data_dir=args.data_dir) if args.pcg_repair is not None else None
    paint = recover_paint(cg, data_dir=args.data_dir)
    print(json.dumps({'recovery': refinement, 'paint': paint, 'pcg_repair': repaired}, indent=2,
                     default=_json_default))


if __name__ == '__main__':
    main()
