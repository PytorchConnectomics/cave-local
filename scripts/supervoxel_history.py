"""Versioned atom tables, refinement lineage, and consistent history readers.

Importing this module does not change files or PCG. Publication helpers are
called by the refinement journal while it holds the deployment mutation lock
and the publication epoch is odd. PCG adapters are installed when refinement is
enabled or persisted refinement history/journals still require them.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Callable
import uuid

import numpy as np


LINEAGE_DTYPE = np.dtype([
    ('new', '<u8'), ('old', '<u8'), ('t1_ms', '<i8'), ('operation_id', 'U64'),
])
EDGE_MAP_DTYPE = np.dtype([
    ('a', '<u8'), ('b', '<u8'), ('c', '<u8'), ('d', '<u8'),
    ('operation_id', 'U64'), ('t1_ms', '<i8'),
])
TABLE_FILES = ('atom_index.npy', 'atom_bounds.npy', 'initial_roots.npy', 'lineage.npy', 'edge_map.npy')
translated_ids: ContextVar[tuple[int, ...]] = ContextVar('svsplit_translated_ids', default=())
translated_retry_count: ContextVar[int] = ContextVar('svsplit_translated_retry_count', default=0)


class PublicationBusy(RuntimeError):
    """The caller must retry rather than consume an inconsistent snapshot."""


class StaleTranslation(RuntimeError):
    """A translated endpoint retired before its native operation acquired locks."""


@dataclass(frozen=True)
class AtomTables:
    index: np.ndarray
    bounds: np.ndarray
    initial_roots: np.ndarray
    lineage: np.ndarray
    edge_map: np.ndarray
    generation: str
    path: Path

    @functools.cached_property
    def retired(self) -> frozenset[int]:
        return frozenset(map(int, self.lineage['old']))

    @functools.cached_property
    def latest_t1_ms(self):
        return int(self.lineage['t1_ms'].max()) if len(self.lineage) else None

    @property
    def ids(self) -> np.ndarray:
        return self.index[:, 0]


_table_cache: dict[tuple, AtomTables] = {}


def _structured(rows, dtype):
    if isinstance(rows, np.ndarray):
        return np.asarray(rows, dtype=dtype)
    return np.array(list(rows), dtype=dtype)


def validate_atom_tables(tables: AtomTables) -> AtomTables:
    """Validate dense alignment and the independent sparse maps."""
    index, bounds, roots = tables.index, tables.bounds, tables.initial_roots
    if index.ndim != 2 or index.shape[1] != 5 or index.dtype.kind != 'u':
        raise ValueError('atom_index must be an unsigned Nx5 array')
    if bounds.shape != (len(index), 6) or roots.shape != (len(index),):
        raise ValueError('dense atom tables must have aligned lengths and shapes')
    ids = index[:, 0]
    if len(ids) > 1 and np.any(ids[1:] <= ids[:-1]):
        raise ValueError('atom_index ids must be sorted and unique')
    if np.any(bounds[:, 3:] <= bounds[:, :3]):
        raise ValueError('atom bounds must have positive extent')
    if np.any(index[:, 2:] < bounds[:, :3]) or np.any(index[:, 2:] >= bounds[:, 3:]):
        raise ValueError('atom inside points must lie in their bounds')
    id_set = set(map(int, ids))
    lineage = tables.lineage
    if lineage.dtype != LINEAGE_DTYPE or lineage.ndim != 1:
        raise ValueError('invalid lineage dtype or shape')
    if len(lineage) > 1 and np.any(lineage['new'][1:] <= lineage['new'][:-1]):
        raise ValueError('lineage keys must be sorted and unique')
    parents = {}
    times = {}
    for row in lineage:
        child, parent = int(row['new']), int(row['old'])
        if child not in id_set or parent not in id_set:
            raise ValueError('lineage endpoints must exist in atom_index')
        if child == parent:
            raise ValueError('lineage cannot map an atom to itself')
        parents[child] = parent
        times[child] = int(row['t1_ms'])
    for child in parents:
        seen = set()
        node = child
        while node in parents:
            if node in seen:
                raise ValueError('lineage cycle')
            seen.add(node)
            parent = parents[node]
            if parent in times and times[node] <= times[parent]:
                raise ValueError('lineage T1 must increase along each refinement chain')
            node = parent
    edge_map = tables.edge_map
    if edge_map.dtype != EDGE_MAP_DTYPE or edge_map.ndim != 1:
        raise ValueError('invalid edge map dtype or shape')
    previous = None
    seen_edges = set()
    for row in edge_map:
        a, b, c, d = (int(row[key]) for key in ('a', 'b', 'c', 'd'))
        if not a < b or not c < d:
            raise ValueError('edge map pairs must be canonical and have distinct endpoints')
        if not {a, b, c, d}.issubset(id_set):
            raise ValueError('edge map endpoints must exist in atom_index')
        key = (a, b, c, d)
        if previous is not None and key < previous:
            raise ValueError('edge map must be sorted by old and new edge')
        if key in seen_edges:
            raise ValueError('duplicate edge map row')
        seen_edges.add(key)
        previous = key
        origins = []
        for new in (c, d):
            origins.append(new if new in (a, b) else parents.get(new))
        if None in origins or sorted(origins) != [a, b] or (a, b) == (c, d):
            raise ValueError('edge map replacements must follow immediate lineage')
    return tables


def _file_key(path):
    stat = path.stat()
    return (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)


def load_atom_tables(data_dir='/data', generation_path=None) -> AtomTables:
    """Resolve current once, then load and validate that immutable generation.

    Legacy arrays remain readable before the feature's first publication. Their
    file identities form the cache key so off-switch readers also notice updates.
    """
    global _table_cache
    data_dir = Path(data_dir)
    current = data_dir / 'atom_tables' / 'current'
    if generation_path is not None:
        directory = Path(generation_path).resolve(strict=True)
        legacy = False
    elif current.exists():
        directory = current.resolve(strict=True)
        legacy = False
    else:
        directory = data_dir.resolve(strict=True)
        legacy = True
    names = TABLE_FILES[:3] if legacy else TABLE_FILES
    key = (legacy, *(_file_key(directory / name) for name in names))
    cached = _table_cache.get(key)
    if cached is not None:
        return cached
    arrays = [np.load(directory / name, mmap_mode='r', allow_pickle=False) for name in names]
    if legacy:
        arrays.extend((np.empty(0, LINEAGE_DTYPE), np.empty(0, EDGE_MAP_DTYPE)))
    tables = validate_atom_tables(AtomTables(*arrays, 'legacy' if legacy else directory.name, directory))
    if len(_table_cache) >= 16:
        # Readers may still hold the previous dict; never clear it in place.
        _table_cache = {}
    _table_cache[key] = tables
    return tables


def merge_atom_tables(tables, new_index_rows, new_bounds_rows, new_initial_roots,
                     new_lineage_rows=(), new_edge_map_rows=()) -> AtomTables:
    """Apply one shared id sort to every dense array; maps have independent keys."""
    rows = np.asarray(new_index_rows, dtype=np.uint64).reshape(-1, 5)
    boxes = np.asarray(new_bounds_rows, dtype=tables.bounds.dtype).reshape(-1, 6)
    roots = np.asarray(new_initial_roots, dtype=np.uint64).reshape(-1)
    if len(rows) != len(boxes) or len(rows) != len(roots):
        raise ValueError('new dense rows must have aligned lengths')
    index = np.concatenate((tables.index, rows))
    order = np.argsort(index[:, 0], kind='stable')
    lineage = np.concatenate((tables.lineage, _structured(new_lineage_rows, LINEAGE_DTYPE)))
    lineage = lineage[np.argsort(lineage['new'], kind='stable')]
    edge_map = np.concatenate((tables.edge_map, _structured(new_edge_map_rows, EDGE_MAP_DTYPE)))
    if len(edge_map):
        edge_map = edge_map[np.lexsort((edge_map['d'], edge_map['c'], edge_map['b'], edge_map['a']))]
    result = AtomTables(index[order], np.concatenate((tables.bounds, boxes))[order],
                        np.concatenate((tables.initial_roots, roots))[order], lineage, edge_map,
                        'staged', tables.path)
    return validate_atom_tables(result)


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _save_array(path, array):
    with open(path, 'wb') as handle:
        np.save(handle, array, allow_pickle=False)
        handle.flush()
        os.fsync(handle.fileno())


def _write_generation(directory, tables, number):
    directory.mkdir(parents=True, exist_ok=False)
    for name, array in zip(TABLE_FILES, (tables.index, tables.bounds, tables.initial_roots,
                                        tables.lineage, tables.edge_map)):
        _save_array(directory / name, array)
    metadata = {'generation': str(number), 'sha256': {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in TABLE_FILES}}
    with open(directory / 'generation.json', 'w') as handle:
        json.dump(metadata, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(directory)
    _fsync_directory(directory.parent)


def stage_generation(data_dir='/data', *, new_index_rows=(), new_bounds_rows=(),
                     new_initial_roots=(), new_lineage_rows=(), new_edge_map_rows=(),
                     destination=None) -> Path:
    """Prepare complete payloads without changing current tables or legacy paths."""
    data_dir = Path(data_dir)
    current = load_atom_tables(data_dir)
    merged = merge_atom_tables(current, new_index_rows, new_bounds_rows, new_initial_roots,
                               new_lineage_rows, new_edge_map_rows)
    parent = data_dir / 'atom_tables'
    existing = [int(path.name) for path in parent.iterdir() if path.name.isdigit()] if parent.exists() else []
    number = max(existing, default=0) + 1
    destination = Path(destination) if destination is not None else data_dir / 'supervoxel_splits' / ('staged-' + uuid.uuid4().hex)
    _write_generation(destination, merged, number)
    return destination


def cleanup_temporary_symlinks(data_dir='/data'):
    """Remove interrupted pointer swaps while holding the mutation lock.

    Only our four pointer names and UUID suffixes are recognized, and only
    symlinks are removed. Never follow a target or remove a matching file or
    directory. Recovery also calls this for journals past the table step.
    """
    data_dir = Path(data_dir)
    removed = []
    for directory, names in ((data_dir, TABLE_FILES[:3]),
                             (data_dir / 'atom_tables', ('current',))):
        if not directory.exists():
            continue
        pattern = re.compile(r'\.(' + '|'.join(map(re.escape, names)) + r')\.[0-9a-f]{32}')
        changed = False
        for path in directory.iterdir():
            if pattern.fullmatch(path.name) and path.is_symlink():
                path.unlink()
                removed.append(path)
                changed = True
        if changed:
            _fsync_directory(directory)
    return removed


def _replace_symlink(target, link, *, after_create=None):
    temp = link.with_name('.' + link.name + '.' + uuid.uuid4().hex)
    temp.symlink_to(target)
    if after_create is not None:
        after_create(temp, link)
    os.replace(temp, link)
    _fsync_directory(link.parent)


def _verify_generation_files(directory, metadata):
    for name in TABLE_FILES:
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != metadata['sha256'][name]:
            raise ValueError('atom-table generation hash mismatch: ' + name)


def publish_generation(staged, data_dir='/data', *, symlink_created=None) -> AtomTables:
    """Idempotently install a journal payload and switch all table readers to it.

    The caller must hold the mutation lock and odd epoch. Generation zero and
    legacy symlinks are created here, never while preparing a journal.
    """
    staged, data_dir = Path(staged), Path(data_dir)
    cleanup_temporary_symlinks(data_dir)
    metadata = json.loads((staged / 'generation.json').read_text())
    number = metadata['generation']
    if not str(number).isdigit():
        raise ValueError('invalid atom-table generation name')
    _verify_generation_files(staged, metadata)
    load_atom_tables(data_dir, generation_path=staged)
    parent = data_dir / 'atom_tables'
    parent.mkdir(exist_ok=True)
    if not (parent / 'current').exists() and not (parent / '0').exists():
        legacy = load_atom_tables(data_dir)
        temporary = parent / ('.zero-' + uuid.uuid4().hex)
        _write_generation(temporary, legacy, 0)
        os.replace(temporary, parent / '0')
        _fsync_directory(parent)
    destination = parent / str(number)
    if destination.exists():
        _verify_generation_files(destination, metadata)
    else:
        temporary = parent / ('.install-' + uuid.uuid4().hex)
        shutil.copytree(staged, temporary)
        for name in (*TABLE_FILES, 'generation.json'):
            with open(temporary / name, 'rb') as handle:
                os.fsync(handle.fileno())
        _fsync_directory(temporary)
        os.replace(temporary, destination)
        _fsync_directory(parent)
    _replace_symlink(str(number), parent / 'current', after_create=symlink_created)
    for name in TABLE_FILES[:3]:
        target = 'atom_tables/current/' + name
        link = data_dir / name
        if not link.is_symlink() or os.readlink(link) != target:
            _replace_symlink(target, link, after_create=symlink_created)
    return load_atom_tables(data_dir)


def initialize_atom_tables(data_dir='/data') -> AtomTables:
    """Explicitly publish an unchanged generation; requires the publication lock."""
    data_dir = Path(data_dir)
    cleanup_temporary_symlinks(data_dir)
    if (data_dir / 'atom_tables' / 'current').exists():
        return load_atom_tables(data_dir)
    legacy = load_atom_tables(data_dir)
    parent = data_dir / 'atom_tables'
    parent.mkdir(exist_ok=True)
    if not (parent / '0').exists():
        temporary = parent / ('.zero-' + uuid.uuid4().hex)
        _write_generation(temporary, legacy, 0)
        os.replace(temporary, parent / '0')
        _fsync_directory(parent)
    _replace_symlink('0', parent / 'current')
    for name in TABLE_FILES[:3]:
        _replace_symlink('atom_tables/current/' + name, data_dir / name)
    return load_atom_tables(data_dir)


def _timestamp_ms(timestamp):
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            # PCG treats naive datetimes as UTC.
            from datetime import timezone
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return int(timestamp.timestamp() * 1000)
    if isinstance(timestamp, np.datetime64):
        return int(timestamp.astype('datetime64[ms]').astype(np.int64))
    return int(timestamp)


def ancestor_at(atom, timestamp, tables=None, data_dir='/data') -> np.uint64:
    """Resolve an atom before its creation; numeric timestamps are milliseconds."""
    if timestamp is None:
        return np.uint64(atom)
    tables = load_atom_tables(data_dir) if tables is None else tables
    ts = _timestamp_ms(timestamp)
    lineage = tables.lineage
    atom = np.uint64(atom)
    while len(lineage):
        pos = int(np.searchsorted(lineage['new'], atom))
        if pos == len(lineage) or lineage['new'][pos] != atom or ts >= lineage['t1_ms'][pos]:
            break
        atom = lineage['old'][pos]
    return np.uint64(atom)


def ancestors_at(atoms, timestamp, tables=None, data_dir='/data') -> np.ndarray:
    """Resolve a batch with one vectorized search per lineage depth."""
    result = np.array(atoms, dtype=np.uint64, copy=True)
    if timestamp is None or not result.size:
        return result
    tables = load_atom_tables(data_dir) if tables is None else tables
    ts = _timestamp_ms(timestamp)
    if tables.latest_t1_ms is None or ts >= tables.latest_t1_ms:
        return result
    flat = result.reshape(-1)
    pending = np.arange(flat.size)
    lineage = tables.lineage
    while len(pending):
        positions = np.searchsorted(lineage['new'], flat[pending])
        valid = positions < len(lineage)
        pending, positions = pending[valid], positions[valid]
        change = ((lineage['new'][positions] == flat[pending])
                  & (ts < lineage['t1_ms'][positions]))
        pending, positions = pending[change], positions[change]
        flat[pending] = lineage['old'][positions]
    return result


def current_descendants(atom, tables=None, data_dir='/data') -> np.ndarray:
    tables = load_atom_tables(data_dir) if tables is None else tables
    children = {}
    for row in tables.lineage:
        children.setdefault(int(row['old']), []).append(int(row['new']))
    pending, result = [int(atom)], []
    while pending:
        node = pending.pop()
        if node in children:
            pending.extend(children[node])
        else:
            result.append(node)
    return np.array(sorted(set(result)), np.uint64)


def translate_edges(edges, now=None, tables=None, data_dir='/data') -> np.ndarray:
    """Follow complete edge provenance until both endpoints are current."""
    if isinstance(now, AtomTables) and tables is None:
        tables = now
    tables = load_atom_tables(data_dir) if tables is None else tables
    maps = {}
    for row in tables.edge_map:
        maps.setdefault((int(row['a']), int(row['b'])), []).append((int(row['c']), int(row['d'])))
    retired = tables.retired
    pending = [tuple(sorted(map(int, row))) for row in np.asarray(edges, np.uint64).reshape(-1, 2)]
    result, expanded = set(), set()
    while pending:
        edge = pending.pop()
        if retired.intersection(edge):
            if edge in expanded:
                continue
            if edge not in maps:
                raise ValueError(f'retired edge has no provenance: {edge}')
            expanded.add(edge)
            pending.extend(maps[edge])
        else:
            result.add(edge)
    return np.array(sorted(result), np.uint64).reshape(-1, 2)


def read_epoch(data_dir='/data') -> int:
    path = Path(data_dir) / 'supervoxel_splits' / 'epoch'
    try:
        value = int(path.read_text().strip())
    except FileNotFoundError:
        return 0
    except ValueError as exc:
        raise PublicationBusy('invalid supervoxel publication epoch') from exc
    if value < 0:
        raise PublicationBusy('invalid supervoxel publication epoch')
    return value


def snapshot_read(fn: Callable, data_dir='/data', *, max_attempts=5, timeout=5.0,
                  poll_interval=0.01, recover=None):
    """Read hierarchy, tables and voxels under one validated publication epoch."""
    deadline = time.monotonic() + timeout
    recovery_attempted = False
    for _ in range(max_attempts):
        before = read_epoch(data_dir)
        while before % 2:
            if recover is not None and not recovery_attempted:
                # Recovery must try the mutation lock non-blockingly: an active
                # publisher should finish on its own. One attempt bounds work
                # on this read; later reads retry any transient replay failure.
                recovery_attempted = True
                recover()
                before = read_epoch(data_dir)
                if not before % 2:
                    break
            if time.monotonic() >= deadline:
                raise PublicationBusy('supervoxel publication in progress')
            time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
            before = read_epoch(data_dir)
        try:
            result = fn()
        except Exception:
            # A reader may encounter a temporary mismatch before it can return.
            # Preserve real failures from a stable epoch; retry publication races.
            if read_epoch(data_dir) == before:
                raise
            continue
        after = read_epoch(data_dir)
        if before == after and not after % 2:
            return result
        if time.monotonic() >= deadline:
            break
    raise PublicationBusy('supervoxel publication changed during read; retry')


def check_translation_current(data_dir='/data'):
    ids = translated_ids.get()
    if ids and load_atom_tables(data_dir).retired.intersection(ids):
        raise StaleTranslation('translated supervoxel retired before root locking')


def refresh_merge_undo(operation, tables):
    """Find actual merge bridges in a graph whose atoms have been refined.

    PCG logs the requested merge pair even when it activates another contact or
    fake edge. Its undo constructor searches historical root leaves with that
    original pair. Refinement of a representative or an actual bridge retires
    those leaves, so the search can miss the bridge and return a representative
    edge that never existed. Reuse PCG's
    bridge selection on current pieces, classified at the original merge time.
    """
    inverse = getattr(operation, 'inverse_superseded_operation', None)
    if inverse is None or not hasattr(operation, 'superseded_operation_id') or not len(tables.lineage):
        return
    from pychunkedgraph.graph import attributes
    from pychunkedgraph.graph.edits import merge_preprocess
    from pychunkedgraph.graph.utils.generic import get_bounding_box

    cg = operation.cg
    record, timestamp = cg.client.read_log_entry(operation.superseded_operation_id)
    requested = record.get(attributes.OperationLogs.AddedEdge)
    if requested is None:
        return
    representatives = translate_edges(requested, tables=tables)
    roots = np.unique(cg.get_roots(representatives.ravel()))
    bbox = get_bounding_box(inverse.source_coords, inverse.sink_coords, inverse.bbox_offset)
    edges = cg.get_subgraph(roots, bbox=bbox, bbox_is_coordinate=True, edges_only=True)
    activated = merge_preprocess(cg, subgraph_edges=edges,
                                 supervoxels=representatives.ravel(),
                                 parent_ts=timestamp - timedelta(milliseconds=100))
    if len(activated):
        inverse.removed_edges = activated
        operation.removed_edges = activated


def _coordinate_atom(coordinate, cg, data_dir):
    from cloudvolume import CloudVolume
    # PCG HTTP handlers convert nanometers to XYZ voxels before constructing
    # operations; OperationLogs therefore already contain voxel coordinates.
    point = np.floor(np.asarray(coordinate)).astype(int)
    if np.any(point < 0):
        return None
    volume = CloudVolume('file://' + str(Path(data_dir).resolve() / 'atomic'), progress=False, cache=False)
    if np.any(point >= np.asarray(volume.shape[:3])):
        return None
    return int(volume[tuple(slice(int(v), int(v) + 1) for v in point)][0, 0, 0, 0])


def translate_operation(operation, tables, data_dir='/data', coordinate_lookup=None):
    """Translate a completed undo/redo factory result before native prechecks."""
    effective = getattr(operation, 'inverse_superseded_operation',
                        getattr(operation, 'superseded_operation', operation))
    endpoints = set()
    for name in ('added_edges', 'removed_edges'):
        if not hasattr(effective, name):
            continue
        before = getattr(effective, name)
        after = translate_edges(before, tables=tables)
        setattr(effective, name, after)
        if operation is not effective:
            setattr(operation, name, after)
        endpoints.update(map(int, after.ravel()))
        if name == 'added_edges' and getattr(effective, 'affinities', None) is not None:
            affinities = {}
            for edge, affinity in zip(before, effective.affinities):
                for pair in translate_edges([edge], tables=tables):
                    affinities[tuple(map(int, pair))] = affinity
            effective.affinities = np.array([affinities[tuple(map(int, edge))] for edge in after], dtype=np.float32)
    for side in ('source', 'sink'):
        name, coordinate_name = side + '_ids', side + '_coords'
        if not hasattr(effective, name):
            continue
        old_ids = getattr(effective, name)
        coordinates = getattr(effective, coordinate_name, None)
        new_ids, new_coordinates = [], []
        for i, atom in enumerate(old_ids):
            replacements = current_descendants(atom, tables)
            coordinate = coordinates[i] if coordinates is not None and i < len(coordinates) else None
            if int(atom) in tables.retired and coordinate is not None:
                try:
                    at_point = (coordinate_lookup(coordinate) if coordinate_lookup is not None
                                else _coordinate_atom(coordinate, effective.cg, data_dir))
                except (IndexError, ValueError):
                    at_point = None
                if at_point in replacements:
                    replacements = np.array([at_point], np.uint64)
            new_ids.extend(map(int, replacements))
            if coordinate is not None:
                new_coordinates.extend([coordinate] * len(replacements))
        setattr(effective, name, np.array(new_ids, np.uint64))
        if coordinates is not None and len(new_coordinates) == len(new_ids):
            setattr(effective, coordinate_name, np.asarray(new_coordinates))
        endpoints.update(new_ids)
    translated_ids.set(tuple(sorted(endpoints)))
    return operation


def retry_translated_operation(call, data_dir='/data', *, attempts=3,
                               precondition_errors=(), on_retry=None):
    """Rebuild from the log after stale locking or a stale pre-lock precheck."""
    token = translated_ids.set(())
    translated_retry_count.set(0)
    try:
        for attempt in range(attempts):
            translated_ids.set(())
            try:
                return call()
            except StaleTranslation:
                pass
            except precondition_errors:
                # Native UndoOperation checks existence before it takes locks.
                # Retry only when refinement explains that failed precondition.
                ids = translated_ids.get()
                if not ids or not load_atom_tables(data_dir).retired.intersection(ids):
                    raise
            if on_retry is not None:
                on_retry(attempt + 1)
            translated_retry_count.set(attempt + 1)
        raise PublicationBusy('supervoxel refinement in progress; retry')
    finally:
        translated_ids.reset(token)


def _record_retry(data_dir, operation, operation_id, attempt):
    path = Path(data_dir) / 'supervoxel_splits' / 'history-retries.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {'operation': operation, 'superseded_operation_id': int(operation_id),
             'attempt': attempt}
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        payload = (json.dumps(entry, sort_keys=True) + '\n').encode()
        if os.write(descriptor, payload) != len(payload):
            raise OSError('incomplete history retry log write')
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install_history(data_dir='/data', *, enabled=True):
    """Install history adapters when enabled by the service's persistence gate."""
    if not enabled:
        return
    from pychunkedgraph.graph.chunkedgraph import ChunkedGraph
    from pychunkedgraph.graph.operation import GraphEditOperation
    from pychunkedgraph.graph.exceptions import PreconditionError

    if getattr(ChunkedGraph, '_cave_svsplit_history_installed', False):
        return
    original_roots, original_root = ChunkedGraph.get_roots, ChunkedGraph.get_root

    @functools.wraps(original_roots)
    def roots(self, node_ids, *args, **kwargs):
        timestamp = kwargs.get('time_stamp')
        if timestamp is not None:
            node_ids = ancestors_at(node_ids, timestamp, data_dir=data_dir)
        return original_roots(self, node_ids, *args, **kwargs)

    @functools.wraps(original_root)
    def root(self, node_id, *args, **kwargs):
        timestamp = kwargs.get('time_stamp')
        if timestamp is not None:
            node_id = ancestor_at(node_id, timestamp, data_dir=data_dir)
        return original_root(self, node_id, *args, **kwargs)

    original_factory = GraphEditOperation._resolve_undo_chain.__func__

    @functools.wraps(original_factory)
    def factory(cls, cg, *args, **kwargs):
        result = original_factory(cls, cg, *args, **kwargs)
        tables = load_atom_tables(data_dir)
        refresh_merge_undo(result, tables)
        translate_operation(result, tables, data_dir)
        if os.environ.get('CAVE_SVSPLIT_FAULT'):
            from supervoxel_refine import fixture_fault
            fixture_fault('after_translation', data_dir=data_dir,
                          operation=type(result).__name__,
                          translated_ids=list(map(int, translated_ids.get())),
                          operation_id=int(getattr(result, 'superseded_operation_id', 0)))
        return result

    def wrap_operation(original):
        @functools.wraps(original)
        def wrapped(self, *args, **kwargs):
            operation_id = kwargs.get('operation_id', args[1] if len(args) > 1 else args[0] if args else 0)
            name = 'undo' if 'undo' in original.__name__ else 'redo'
            return retry_translated_operation(lambda: original(self, *args, **kwargs), data_dir,
                                               precondition_errors=(PreconditionError,),
                                               on_retry=lambda count: _record_retry(data_dir, name, operation_id, count))
        return wrapped

    ChunkedGraph.get_roots = roots
    ChunkedGraph.get_root = root
    GraphEditOperation._resolve_undo_chain = classmethod(factory)
    ChunkedGraph.undo_operation = wrap_operation(ChunkedGraph.undo_operation)
    ChunkedGraph.redo_operation = wrap_operation(ChunkedGraph.redo_operation)
    ChunkedGraph._cave_svsplit_history_installed = True
