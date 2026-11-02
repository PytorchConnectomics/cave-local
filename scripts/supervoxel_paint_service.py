"""Request-scoped painted split checks on the pinned native multicut path.

All graph changes still pass through native PCG operations or journaled
supervoxel refinement. The solver wrapper returns the native edges unchanged.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import functools
import json
from pathlib import Path
import time

import numpy as np
from scipy.spatial import cKDTree

from supervoxel_paint import (PAINT_LIMITS, PaintError, check_deadline, closed_side,
    components, contacts, label_atoms, near, parse_paint, rasterize,
    representative, separation)


active_paint = ContextVar('cave_active_paint', default=None)
writing_paint = ContextVar('cave_writing_paint', default=None)


def _fault(point, context=None, **values):
    from supervoxel_refine import fixture_fault
    fixture_fault('paint_' + point, data_dir=context.data_dir if context else '/data', **values)


def _pairs(edges):
    return sorted({tuple(sorted(map(int, edge))) for edge in np.asarray(edges, np.uint64).reshape(-1, 2)})


@dataclass
class PaintContext:
    spec: dict
    limits: dict
    deadline: float
    source_xyz: np.ndarray
    sink_xyz: np.ndarray
    root: int
    sources: set
    sinks: set
    offset: np.ndarray
    journal: object
    data_dir: str = '/data'
    cg: object = None
    operation_id: int | None = None
    box: object = None
    error: object = None
    write_calls: int = 0
    write_entries: int = 0
    renewals: int = 0
    begun: bool = False
    checked: dict = field(default_factory=dict)
    whole_box: object = None
    dropped: int = 0
    yielded: dict = field(default_factory=dict)  # atom -> label it gave up on an unsplittable seam

    @property
    def resolution(self):
        return np.asarray(self.spec['resolution_nm_xyz'], float)

    def check(self):
        check_deadline(self.deadline)

    def refuse(self, kind, message, voxels=(), atoms=()):
        locations = ((np.asarray(voxels, float).reshape(-1, 3)[:20] + .5) * self.resolution).tolist()
        raise PaintError(kind, message, locations_nm=locations, supervoxels=list(map(int, atoms))[:20])


def _lookup(context, cg, expected_root=None, *, locked=False):
    from cloudvolume import CloudVolume
    from supervoxel_history import snapshot_read

    def read():
        context.check()
        all_voxels = np.concatenate([context.source_xyz, context.sink_xyz])
        lo = np.maximum(all_voxels.min(0) - 1, 0)
        hi = np.minimum(all_voxels.max(0) + 2, context.spec['shape_xyz'])
        cv = CloudVolume('file://' + str(Path(context.data_dir) / 'atomic'), cache=False, progress=False)
        crop = np.asarray(cv[tuple(slice(int(a), int(b)) for a, b in zip(lo, hi))][..., 0])
        _fault('graph_read', context)
        context.check()
        src, sink, grouped = label_atoms(context.source_xyz, context.sink_xyz, crop.transpose(2, 1, 0), lo)
        ids = np.array(sorted(set(map(int, src)) | set(map(int, sink))), np.uint64)
        if len(ids) > context.limits['max_painted_atoms']:
            context.refuse('limit', 'Paint touches too many supervoxels.', atoms=ids)
        roots = cg.get_roots(ids)
        context.check()
        if expected_root is None:
            # The object being cut is the one most of the paint covers (the mode); paint on other objects is dropped.
            counts = {}
            for atom, root in zip(map(int, ids), map(int, roots)):
                counts[root] = counts.get(root, 0) + sum(len(grouped[atom][label]) for label in ('source', 'sink'))
            wanted = max(counts, key=lambda r: (counts[r], -r))
            keep = {int(a) for a, r in zip(ids, roots) if int(r) == wanted}
            context.dropped = sum(counts.values()) - counts[wanted]
            grouped = {a: v for a, v in grouped.items() if a in keep}
            src = np.array([a for a in src if int(a) in keep], np.uint64)
            sink = np.array([a for a in sink if int(a) in keep], np.uint64)
            if not len(src) or not len(sink):
                context.refuse('empty_label', 'After dropping the paint outside the main object, one colour covers none of it.')
            return wanted, src, sink, grouped
        wanted = int(expected_root)
        other = ids[roots != wanted]
        if len(other):
            positions = [xyz for atom in other for label in ('source', 'sink')
                         for xyz in grouped[int(atom)][label][:20]]
            context.refuse('object_changed' if expected_root is not None else 'other_object',
                           'The painted voxels must belong to the same current object.', positions, other)
        if context.yielded:
            src = np.array([a for a in src if context.yielded.get(int(a)) != 'source'], np.uint64)
            sink = np.array([a for a in sink if context.yielded.get(int(a)) != 'sink'], np.uint64)
            if not len(src) or not len(sink):
                context.refuse('seam', 'Paint both colours away from the chunk boundary: one colour only covers '
                               'supervoxels glued to the other colour across it.', atoms=list(context.yielded))
        if locked and (set(map(int, src)) != context.sources or set(map(int, sink)) != context.sinks):
            context.refuse('object_changed', 'Painted supervoxels changed before the native root lock.', atoms=ids)
        return wanted, src, sink, grouped

    return snapshot_read(read, data_dir=context.data_dir)


def _bounds(context, ids):
    from supervoxel_history import load_atom_tables
    tables = load_atom_tables(context.data_dir)
    ids = np.asarray(sorted(set(map(int, ids))), np.uint64)
    pos = np.searchsorted(tables.ids, ids)
    if np.any(pos >= len(tables.ids)) or not np.array_equal(tables.ids[pos], ids):
        context.refuse('not_certified', 'A local supervoxel has no current bounding box.', atoms=ids)
    return {int(atom): np.asarray(tables.bounds[i], np.int64) for atom, i in zip(ids, pos)}


def _fake_cells(context, ids):
    from pychunkedgraph.graph import attributes
    context.check()
    ids = np.asarray(sorted(set(map(int, ids))), np.uint64)
    parents = context.cg.get_parents(ids)
    chunks = np.unique(context.cg.get_chunk_ids_from_node_ids(parents))
    cells = []
    for chunk in chunks:
        context.check()
        cells.extend(context.cg.client.read_node(chunk, properties=attributes.Connectivity.FakeEdges, fake_edges=True))
    return cells


def _merge_locations(context, edge, cells):
    """Only the timestamp AND exact added pair bind a fake edge to a merge."""
    from pychunkedgraph.graph import attributes
    logs = attributes.OperationLogs
    locations = []
    for cell in cells:
        if edge not in _pairs(cell.value):
            continue
        context.check()
        records = context.cg.client.read_log_entries(start_time=cell.timestamp,
            end_time=cell.timestamp + timedelta(milliseconds=1))
        for record in records.values():
            if record.get('timestamp') != cell.timestamp or logs.AddedEdge not in record:
                continue
            if edge not in _pairs(record[logs.AddedEdge]):
                continue
            # Native operation coordinates are voxel XYZ, despite their docstring.
            for key in (logs.SourceCoordinate, logs.SinkCoordinate):
                if key in record:
                    locations.extend(np.asarray(record[key], float).reshape(-1, 3) * context.resolution)
    return np.asarray(locations, float).reshape(-1, 3)


def _edge_contacts(context, edge, bounds, budget):
    from cloudvolume import CloudVolume
    a, b = (bounds[int(atom)] for atom in edge)
    lo = np.maximum(np.maximum(a[:3], b[:3]) - 1, 0)
    hi = np.minimum(np.minimum(a[3:], b[3:]) + 1, context.spec['shape_xyz'])
    if np.any(hi <= lo):
        return np.empty((0, 3), np.int64)
    budget[0] += int(np.prod(hi - lo))
    if budget[0] > context.limits['max_contact_voxels']:
        context.refuse('limit', 'Cut contact reads exceed the voxel budget.')
    context.check()
    cv = CloudVolume('file://' + str(Path(context.data_dir) / 'atomic'), cache=False, progress=False)
    labels = np.asarray(cv[tuple(slice(int(a), int(b)) for a, b in zip(lo, hi))][..., 0]).transpose(2, 1, 0)
    context.check()
    return contacts(labels, lo, *edge)


def _whole_object_separated(context, cut):
    """Exact fallback when neither side is closed in the local box (typical for whole-cube objects): the same native
    subgraph read the multicut uses, without its box, for the locked root, then components without the cut. Objects
    over max_object_atoms stay uncertified."""
    from functools import reduce
    from pychunkedgraph.graph.edges import Edges
    context.check()
    agglomerations, edges = context.cg.get_subgraph(np.uint64(context.root))
    supervoxels = np.concatenate([agg.supervoxels for agg in agglomerations.values()]) if agglomerations else np.empty(0, np.uint64)
    if len(supervoxels) > context.limits['max_object_atoms']:
        return False
    context.check()
    edges = reduce(lambda x, y: x + y, edges, Edges([], []))
    keep = np.isin(edges.node_ids1, supervoxels) & np.isin(edges.node_ids2, supervoxels)
    whole = set(_pairs(np.stack([edges.node_ids1[keep], edges.node_ids2[keep]], axis=1)))
    removed = set(cut)
    kept = [edge for edge in whole if edge not in removed and edge[::-1] not in removed]
    for group in components(kept, set(map(int, supervoxels)) | set(context.sources) | set(context.sinks)):
        if group & context.sources and group & context.sinks:
            return False
    return True


def _check_cut(context, edges, removed):
    context.check()
    local = _pairs(edges.get_pairs())
    cut = _pairs(removed)
    if len(cut) > context.limits['max_removed_edges']:
        context.refuse('limit', 'The cut removes too many edges.')
    nodes = set(context.sources) | set(context.sinks) | {n for pair in local for n in pair}
    bounds = _bounds(context, nodes)
    remaining = separation(local, cut, context.sources, context.sinks)
    budget = [0]
    if remaining:
        locations = []
        for _, _, path in remaining[:20]:
            for edge in path[:20]:
                locations.extend(_edge_contacts(context, edge, bounds, budget)[:1])
        context.refuse('not_separated', 'The cut leaves source and sink supervoxels connected.', locations,
                       [atom for a, b, _ in remaining for atom in (a, b)])
    cells = _fake_cells(context, nodes)
    fake = [edge for cell in cells for edge in _pairs(cell.value)]
    kept = [edge for edge in local if edge not in set(cut)]
    groups = components(kept, nodes)
    source_side = set().union(*(group for group in groups if group & context.sources))
    sink_side = set().union(*(group for group in groups if group & context.sinks))
    source_closed, source_open = closed_side(source_side, bounds, context.box[0], context.box[1], fake)
    sink_closed, sink_open = closed_side(sink_side, bounds, context.box[0], context.box[1], fake)
    if not source_closed and not sink_closed and not _whole_object_separated(context, cut):
        opened = sorted(set(source_open) | set(sink_open))
        context.refuse('not_certified', 'The local cut cannot certify separation through the whole object.',
                       [bounds[a][:3] for a in opened[:20]], opened)
    paint_xyz = np.concatenate([context.source_xyz, context.sink_xyz])
    tree = cKDTree((paint_xyz + .5) * context.resolution)
    far = []
    for edge in cut:
        context.check()
        contact = _edge_contacts(context, edge, bounds, budget)
        if len(contact):
            if not near(contact, tree, context.limits['max_cut_distance_nm'], context.resolution):
                distances = tree.query((contact + .5) * context.resolution)[0]
                far.append(((contact[np.argmin(distances)] + .5) * context.resolution).tolist())
        else:
            locations = _merge_locations(context, edge, cells)
            if not len(locations) or float(tree.query(locations)[0].min()) > context.limits['max_cut_distance_nm']:
                # A missing merge location stays uncertified; bounding boxes are only diagnostics.
                far.append((locations[0] if len(locations) else (bounds[edge[0]][:3] + .5) * context.resolution).tolist())
    if far:
        raise PaintError('stray_cut', 'The cut contains an edge away from the paint or with unresolved locality.',
                         locations_nm=far[:20], supervoxels=sorted({n for e in cut for n in e})[:20])
    _fault('check', context)
    context.check()
    terminal_groups = {atom: index for index, group in enumerate(groups) for atom in group}
    context.checked = {'removed_edges': [list(e) for e in cut], 'source_closed': source_closed,
                       'expected_partition': [terminal_groups[a] for a in sorted(context.sources | context.sinks)],
                       'sink_closed': sink_closed, 'contact_voxels': budget[0], 'box_voxel_xyz': np.asarray(context.box).tolist()}


def install_hooks():
    """Install identity wrappers once; ContextVars carry all per-request state."""
    from google.cloud.bigtable.table import Table
    from pychunkedgraph.graph import operation, locks
    from pychunkedgraph.graph.client.bigtable.client import Client
    from pychunkedgraph.graph.exceptions import PreconditionError
    if getattr(operation.MulticutOperation._apply, '_cave_paint', False):
        return

    class PaintRefusal(PreconditionError):
        pass

    def refusal(context, error):
        context.error = error
        try:
            context.journal.update(write_calls=context.write_calls, write_entries=context.write_entries)
        except Exception as journal_error:
            error.add_note('Paint write-counter recording failed: ' + str(journal_error))
        raise PaintRefusal(str(error)) from error

    native_apply = operation.MulticutOperation._apply
    native_solve = operation.run_multicut
    native_box = operation.get_bbox
    native_write = operation.GraphEditOperation._write
    native_mutate = Table.mutate_rows
    native_enter = locks.IndefiniteRootLock.__enter__
    native_renew = Client.renew_locks

    @functools.wraps(native_box)
    def box(source, sink, offset):
        context = active_paint.get()
        if context is None or context.cg is None:
            return native_box(source, sink, offset)
        # Objects small enough for the exact whole-object check are cut over their whole extent, so the cut sees every
        # connection between the colours (a box around the paint misses paths that leave it, e.g. only +-12 z voxels).
        result = (np.asarray(context.whole_box, np.int64) if context.whole_box is not None
                  else np.asarray(native_box(source, sink, context.offset), np.int64))
        # PCG reads whole graph chunks intersecting the coordinate box.
        chunks = np.asarray(context.spec['graph_chunk_xyz'])
        clipped_lo = np.maximum(result[0], 0)
        clipped_hi = np.minimum(result[1] + 1, context.spec['shape_xyz'])
        extent = (np.ceil(clipped_hi / chunks).astype(int) - clipped_lo // chunks) * chunks
        if np.any(extent > context.limits['max_box_voxels']):
            refusal(context, PaintError('limit', 'The native solve box exceeds the configured voxel extents.'))
        context.box = result
        return result

    @functools.wraps(native_solve)
    def solve(edges, sources, sinks, **kwargs):
        context = active_paint.get()
        if context is None:
            return native_solve(edges, sources, sinks, **kwargs)
        try:
            context.check()
            _lookup(context, context.cg, context.root, locked=True)
            infinite = edges.get_pairs()[np.isinf(edges.affinities)]
            if separation(infinite, [], context.sources, context.sinks):
                bounds = _bounds(context, np.unique(infinite))
                contact = []
                paint_tree = cKDTree(np.concatenate([context.source_xyz, context.sink_xyz]) * context.resolution)
                for edge in infinite[:20]:
                    candidates = _edge_contacts(context, edge, bounds, [0])
                    if len(candidates):
                        distances = paint_tree.query(candidates * context.resolution)[0]
                        contact.append(candidates[np.argmin(distances)])
                context.refuse('seam', 'Paint both colours inside the same supervoxel on one side of the chunk boundary.',
                               contact, np.unique(infinite))
            result = native_solve(edges, sources, sinks, **kwargs)
            _fault('solve', context)
            context.check()
            _check_cut(context, edges, result)
            return result
        except PaintError as error:
            refusal(context, error)

    @functools.wraps(native_apply)
    def apply(self, *, operation_id, timestamp):
        context = active_paint.get()
        if context is None:
            return native_apply(self, operation_id=operation_id, timestamp=timestamp)
        context.cg, context.operation_id = self.cg, int(operation_id)
        try:
            context.check()
        except PaintError as error:
            refusal(context, error)
        result = native_apply(self, operation_id=operation_id, timestamp=timestamp)
        try:
            context.journal.update(**context.checked)
            _fault('binding', context, operation_id=int(operation_id))
            context.journal.bind(int(operation_id))
            context.check()
            _fault('after_binding', context, operation_id=int(operation_id))
            context.journal.hand_off()
            _fault('marker', context, operation_id=int(operation_id))
            now = time.monotonic()
            if now >= context.deadline:
                error = PaintError('deadline', 'Paint request exceeded its time limit')
                try:
                    context.journal.update(reason='deadline_after_marker')
                except Exception as journal_error:
                    error.add_note('Paint deadline recording failed: ' + str(journal_error))
                raise error
            return result
        except PaintError as error:
            refusal(context, error)
        except PaintRefusal:
            raise
        except Exception as error:
            # Binding/marker failures abort before _write, as PCG PreconditionError.
            refusal(context, PaintError('limit', 'Could not persist the paint provenance: ' + str(error)))

    @functools.wraps(native_write)
    def write(self, *args, **kwargs):
        context = active_paint.get()
        if context is None or self.cg is not context.cg:
            return native_write(self, *args, **kwargs)
        context.write_entries += 1
        token = writing_paint.set(context)
        try:
            result = native_write(self, *args, **kwargs)
            _fault('after_write', context, operation_id=context.operation_id)
            return result
        finally:
            # Native _write only clears this cache on its success path.
            self.cg.cache = None
            writing_paint.reset(token)

    @functools.wraps(native_enter)
    def enter(self):
        result = native_enter(self)
        context = writing_paint.get()
        if context is not None:
            _fault('indefinite_lock', context, operation_id=context.operation_id)
        return result

    @functools.wraps(native_renew)
    def renew(self, *args, **kwargs):
        result = native_renew(self, *args, **kwargs)
        context = writing_paint.get()
        if context is not None:
            context.renewals += 1
            if context.renewals == 2:
                _fault('lock_renewal', context, operation_id=context.operation_id)
        return result

    @functools.wraps(native_mutate)
    def mutate(self, rows, *args, **kwargs):
        context = writing_paint.get()
        if context is None or self is not context.cg.client._table:
            return native_mutate(self, rows, *args, **kwargs)
        _fault('before_mutate', context, operation_id=context.operation_id, calls=context.write_calls)
        if context.write_calls == 1:
            _fault('before_success_mutate', context, operation_id=context.operation_id)
        if not context.begun:
            try:
                context.check()
            except PaintError as error:
                try:
                    context.journal.update(reason='deadline_at_persistence_gate')
                except Exception as journal_error:
                    error.add_note('Paint deadline recording failed: ' + str(journal_error))
                refusal(context, error)
            context.begun = True
        if context.write_calls == 0 and context.spec.get('test_fixture'):
            from supervoxel_service import fixture_configuration
            fault = fixture_configuration(context.data_dir)
            if fault.get('point') == 'paint_partial_mutate':
                # First row is WRITE_STARTED; deliberately persist only some
                # graph rows, without that log row, then fault in this fixture.
                partial = rows[1:max(2, len(rows) // 2)]
                if not partial:
                    raise AssertionError('Partial-write fixture requires graph rows')
                native_mutate(self, partial, *args, **kwargs)
                _fault('partial_mutate', context, operation_id=context.operation_id,
                       rows=len(partial))
                raise RuntimeError('Partial-write fixture fault must terminate or raise')
        context.write_calls += 1
        result = native_mutate(self, rows, *args, **kwargs)
        _fault('after_mutate' if context.write_calls == 1 else 'success_mutate', context,
               operation_id=context.operation_id, calls=context.write_calls)
        return result

    apply._cave_paint = True
    operation.MulticutOperation._apply = apply
    operation.run_multicut = solve
    operation.get_bbox = box
    operation.GraphEditOperation._write = write
    Table.mutate_rows = mutate
    locks.IndefiniteRootLock.__enter__ = enter
    Client.renew_locks = renew


MAX_SEAM_GENERATIONS = 3  # a seam piece split this many times within one request is refused, not split again
MAX_SEAM_YIELDS = 8  # painted ends that may give up their colour on unsplittable seams in one request


def _merge_seeds(seeds):
    """Voxels touching both sides go to neither, unless that empties a side (a seam contact can be one voxel that also
    touches the other neighbour): then they seed the side they would otherwise leave empty."""
    both = {tuple(v) for v in seeds['source']} & {tuple(v) for v in seeds['sink']}
    if not both:
        return seeds
    kept = {k: np.array([v for v in seeds[k] if tuple(v) not in both], np.int64).reshape(-1, 3) for k in seeds}
    for k, other in (('source', 'sink'), ('sink', 'source')):
        if not len(kept[k]) and len(kept[other]):
            kept[k] = np.array(sorted(both), np.int64).reshape(-1, 3)
    return kept


def _seam_targets(context, cg, root, sources, sinks, grouped):
    """A painted cut across a chunk boundary can be blocked by pieces of one ABISS fragment joined by infinite (seam)
    edges. Find every red-to-blue path of seam edges near the paint and, on each, the piece to refine where the
    colours meet: the last red piece before the first blue one (adjacent ends) or the first piece after it. A piece
    chosen by several paths is refined once with all of their seeds: its own paint plus its voxels touching each
    path's red-side neighbour (source) and blue-side neighbour (sink). One subgraph read serves all paths, so a pass
    costs one refinement per chosen piece instead of one per path. Returns [(atom, source_xyz, sink_xyz, chain)]
    sorted by atom (empty when no seam path joins the colours); a path whose pieces cannot seed both sides is
    returned with an empty side, last, so the caller refuses it once the others are refined."""
    from functools import reduce
    from pychunkedgraph.graph.edges import Edges
    context.check()
    paint = np.concatenate([context.source_xyz, context.sink_xyz])
    lo = np.maximum(paint.min(0) - context.offset, 0)
    hi = np.minimum(paint.max(0) + context.offset + 1, context.spec['shape_xyz'])
    agglomerations, edges = cg.get_subgraph(np.uint64(root), bbox=np.array([lo, hi]), bbox_is_coordinate=True)
    context.check()
    if not agglomerations:
        return []
    supervoxels = np.concatenate([agg.supervoxels for agg in agglomerations.values()])
    edges = reduce(lambda x, y: x + y, edges, Edges([], []))
    keep = np.isin(edges.node_ids1, supervoxels) & np.isin(edges.node_ids2, supervoxels) & np.isinf(edges.affinities)
    infinite = _pairs(np.stack([edges.node_ids1[keep], edges.node_ids2[keep]], axis=1))
    paths = separation(infinite, [], set(map(int, sources)), set(map(int, sinks)))
    if not paths:
        return []
    reds, blues = set(map(int, sources)), set(map(int, sinks))
    empty = np.empty((0, 3), np.int64)
    budget, touching_cache, chosen, failed = [0], {}, {}, None

    def touching(target, neighbour):
        if neighbour is None:
            return empty
        key = (target, neighbour)
        if key not in touching_cache:
            bounds = _bounds(context, [target, neighbour])
            touching_cache[key] = np.asarray(_edge_contacts(context, key, bounds, budget), np.int64).reshape(-1, 3)
        return touching_cache[key]

    for _, _, path in paths:
        nodes = [int(path[0][0])] + [int(v) for _, v in path]
        # Split where the colours meet on the path, not next to its red end: j = first blue piece, i = last red piece
        # before it. Adjacent: refine the red piece against its blue neighbour; otherwise the first piece after i,
        # seeded by its contacts with the red piece i and the next piece towards blue.
        j = next(k for k, n in enumerate(nodes) if n in blues)
        i = max(k for k in range(j) if nodes[k] in reds)
        at = lambda k: nodes[k] if 0 <= k < len(nodes) else None
        # Candidates (piece, red-side neighbour, blue-side neighbour); a piece refined small by earlier attempts can
        # have every voxel touch both sides, so the piece on the other side of the meeting point is tried next. A
        # piece another path already chose is preferred, so overlapping paths share one refinement.
        if j == i + 1:
            candidates = [(nodes[i], at(i - 1), nodes[j]), (nodes[j], nodes[i], at(j + 1))]
        else:
            candidates = [(nodes[i + 1], nodes[i], nodes[i + 2]), (nodes[i], at(i - 1), nodes[i + 1])]
        candidates.sort(key=lambda c: c[0] not in chosen)
        for target, red, blue in candidates:
            painted = grouped.get(target, {})
            seeds = {label: np.unique(np.concatenate([np.asarray(painted.get(label, empty), np.int64).reshape(-1, 3),
                                                      touching(target, neighbour)]), axis=0)
                     for label, neighbour in (('source', red), ('sink', blue))}
            seeds = _merge_seeds(seeds)
            if len(seeds['source']) and len(seeds['sink']):
                entry = chosen.setdefault(target, {'source': [], 'sink': [], 'chain': nodes})
                entry['source'].append(seeds['source'])
                entry['sink'].append(seeds['sink'])
                break
        else:
            if failed is None:
                failed = (target, seeds['source'], seeds['sink'], nodes)
    result = []
    for target in sorted(chosen):
        entry = chosen[target]
        seeds = _merge_seeds({label: np.unique(np.concatenate(entry[label]), axis=0) for label in ('source', 'sink')})
        result.append((target, seeds['source'], seeds['sink'], entry['chain']))
    if failed is not None and failed[0] not in chosen:
        result.append(failed)
    return result


def painted_split(native, table_id, spec, body, data_dir='/data', started=None):
    from flask import g, request
    from pychunkedgraph.app import app_utils
    from source_label_policy import remapped_body
    from supervoxel_history import load_atom_tables
    from supervoxel_refine import refine
    from supervoxel_service import fixture_configuration
    from supervoxel_paint_history import PaintJournal, recover_paint
    started = time.monotonic() if started is None else started
    options = spec.get('supervoxel_split', {})
    limits = {**PAINT_LIMITS, **options.get('paint', {})}
    if not options.get('enabled') or not limits['enabled']:
        raise PaintError('disabled', 'Painted splits are disabled for this deployment.')
    if table_id != spec['graph_id']:
        raise PaintError('other_object', 'Paint metadata belongs to another graph.')
    if request.args.get('mincut', 'true').lower() not in ('true', '1'):
        raise PaintError('limit', 'Painted requests require mincut=true.')
    if spec.get('test_fixture'):
        limits.update(fixture_configuration(data_dir).get('paint_limits', {}))
    deadline = started + limits['max_seconds']
    paint = parse_paint(body, limits)
    src_xyz, sink_xyz = rasterize(paint, spec['resolution_nm_xyz'], spec['shape_xyz'], limits,
                                deadline, checkpoint=lambda: _fault('raster'))
    if not len(src_xyz) or not len(sink_xyz):
        raise PaintError('empty_label', 'Each paint label must cover at least one voxel in the volume.')
    # No graph is constructed or read until all request/raster bounds pass.
    cg = app_utils.get_cg(table_id, skip_cache=True)
    recover_paint(cg, data_dir)
    context = PaintContext(spec, limits, deadline, src_xyz, sink_xyz, 0, set(), set(),
                           np.asarray(body.get('bb_offset', [120, 120, 12])), None, data_dir)
    if context.offset.shape != (3,) or context.offset.dtype.kind not in 'iu' or np.any(context.offset < 0):
        raise PaintError('limit', 'bb_offset must contain three nonnegative voxel distances.')
    root, sources, sinks, grouped = _lookup(context, cg)
    context.root = root
    g.paint_dropped = context.dropped
    # Keep only foreground paint; all subsequent read/recheck/trees use these voxels.
    context.source_xyz = np.concatenate([v['source'] for v in grouped.values() if len(v['source'])])
    context.sink_xyz = np.concatenate([v['sink'] for v in grouped.values() if len(v['sink'])])
    counts = [len(context.source_xyz), len(context.sink_xyz)]
    context.journal = PaintJournal.create(data_dir, body['paint'], str(g.auth_user['id']),
        time.time() - (time.monotonic() - started), limits['max_seconds'], counts,
        sorted(set(map(int, sources)) | set(map(int, sinks))),
        lock_lifetime_seconds=cg.meta.graph_config.ROOT_LOCK_EXPIRY.total_seconds())
    refined = 0
    try:
        while set(map(int, sources)) & set(map(int, sinks)):
            context.check()
            if refined >= options.get('max_atoms', 64):
                context.refuse('limit', 'Too many two-label supervoxels require refinement.')
            atom = min(set(map(int, sources)) & set(map(int, sinks)))
            tables = load_atom_tables(data_dir)
            lo = tables.bounds[np.searchsorted(tables.ids, np.uint64(atom)), :3]
            seeds = grouped[atom]
            item = {'atom': atom, 'source_vox': (seeds['source'] - lo)[:, ::-1].tolist(),
                    'sink_vox': (seeds['sink'] - lo)[:, ::-1].tolist()}
            remaining = min(options.get('max_seconds', 30), deadline - time.monotonic())
            result = refine(cg, [item], grid_xyz=options.get('grid_xyz', [16, 16, 16]),
                            reason='painted', data_dir=data_dir, max_seconds=remaining,
                            whole_atom=True)
            if not result.count:
                context.refuse('object_changed', 'Painted supervoxel could not be refined.')
            refined += result.count
            context.journal.update(refinement_operation_ids=
                context.journal.data.get('refinement_operation_ids', []) + result.operation_ids)
            g.svsplit_refined = refined
            root, sources, sinks, grouped = _lookup(context, cg, root)
        # Seams: each pass refines, once each, the unsplittable pieces where seam paths from red meet blue, then reads
        # again. A piece this request has already split MAX_SEAM_GENERATIONS times is refused rather than shaved again.
        generation = {}
        while True:
            found = _seam_targets(context, cg, root, sources, sinks, grouped)
            if not found:
                break
            for atom, red, blue, chain in found:
                if not len(red) or not len(blue):
                    # No piece on this path can be seeded on both sides: typically single-voxel pieces glued across a
                    # chunk face with opposite paint. The painted end holding less paint gives up its colour; the
                    # multicut then places it (its seam edge keeps it with its neighbour).
                    ends = [(int(chain[0]), 'source'), (int(chain[-1]), 'sink')]
                    ends = [(a, label) for a, label in ends
                            if a not in context.yielded and len(grouped.get(a, {}).get(label, ()))]
                    if not ends or len(context.yielded) >= MAX_SEAM_YIELDS:
                        context.refuse('seam', 'Paint both colours inside the same supervoxel on one side of the chunk '
                                       'boundary.', np.concatenate([red, blue])[:20], chain)
                    end, label = min(ends, key=lambda e: (len(grouped[e[0]][e[1]]), e[0]))
                    context.yielded[end] = label
                    context.journal.update(seam_yielded={str(a): l for a, l in context.yielded.items()})
                    continue
                if generation.get(int(atom), 0) >= MAX_SEAM_GENERATIONS:
                    context.refuse('seam', 'A seam-joined supervoxel keeps touching both colours after repeated '
                                   'refinement; paint both colours inside it, away from the chunk boundary.',
                                   np.concatenate([red, blue])[:20], chain)
                if refined >= options.get('max_atoms', 64):
                    context.refuse('limit', 'Too many seam-joined supervoxels require refinement.', atoms=chain)
                tables = load_atom_tables(data_dir)
                lo = tables.bounds[np.searchsorted(tables.ids, np.uint64(atom)), :3]
                item = {'atom': int(atom), 'source_vox': (red - lo)[:, ::-1].tolist(),
                        'sink_vox': (blue - lo)[:, ::-1].tolist()}
                remaining = min(options.get('max_seconds', 30), deadline - time.monotonic())
                result = refine(cg, [item], grid_xyz=options.get('grid_xyz', [16, 16, 16]),
                                reason='painted-seam', data_dir=data_dir, max_seconds=remaining,
                                whole_atom=True)
                if not result.count:
                    context.refuse('seam', 'A seam-joined supervoxel could not be refined.',
                                   np.concatenate([red, blue])[:20], chain)
                refined += result.count
                for child in result.new_ids.get(int(atom), []):
                    generation[int(child)] = generation.get(int(atom), 0) + 1
                context.journal.update(refinement_operation_ids=
                    context.journal.data.get('refinement_operation_ids', []) + result.operation_ids)
                g.svsplit_refined = refined
            root, sources, sinks, grouped = _lookup(context, cg, root)
        _fault('after_refinement', context)
        attempts = 0
        while attempts < 2:
            context.error = None
            context.check()
            root, sources, sinks, grouped = _lookup(context, cg, root)
            context.sources, context.sinks = set(map(int, sources)), set(map(int, sinks))
            if context.sources & context.sinks:
                context.refuse('object_changed', 'The paint labels now share a supervoxel.')
            remapped = {k: v for k, v in body.items() if k != 'paint'}
            for name, label, atoms in (('sources', 'source', sources), ('sinks', 'sink', sinks)):
                remapped[name] = [[str(int(atom)), *((representative(int(atom), grouped[int(atom)][label]) + .5)
                                                   * context.resolution).tolist()] for atom in atoms]
            leaves = np.asarray(cg.get_subgraph_leaves(np.uint64(root)), np.uint64)
            context.whole_box = None
            if 0 < len(leaves) <= context.limits['max_object_atoms']:
                tables = load_atom_tables(data_dir)
                pos = np.searchsorted(tables.ids, leaves)
                known = pos < len(tables.ids)
                known[known] = tables.ids[pos[known]] == leaves[known]
                if np.all(known):
                    b = np.asarray(tables.bounds[pos], np.int64)
                    context.whole_box = np.array([np.maximum(b[:, :3].min(0) - 1, 0),
                                                  np.minimum(b[:, 3:].max(0), context.spec['shape_xyz'])])
            terminals = sorted(context.sources | context.sinks)
            context.journal.update(sources=sorted(context.sources), sinks=sorted(context.sinks),
                roots=[root], painted_atoms=terminals, pre_partition=[0] * len(terminals),
                native_started=time.time(), attempts=attempts + 1)
            _fault('before_native', context, sources=remapped['sources'], sinks=remapped['sinks'])
            attempts += 1
            context.error = None
            token = active_paint.set(context)
            try:
                with remapped_body(remapped):
                    result = native(table_id)
            except Exception:
                if context.error and not context.journal.data.get('handed_to_write') and attempts < 2:
                    if context.error.kind == 'not_certified':
                        context.offset = context.offset * 2
                        continue
                    if context.error.kind == 'object_changed':
                        continue
                raise
            finally:
                active_paint.reset(token)
            _fault('after_native', context, operation_id=int(result.operation_id))
            context.journal.finish(cg)
            g.paint_counts, g.paint_operation = counts, int(result.operation_id)
            g.paint_elapsed = time.monotonic() - started
            g.svsplit_refined = refined
            return result
        context.refuse('object_changed', 'Painted object changed repeatedly.')
    except Exception as caught:
        error = caught
        if (isinstance(error, PaintError) and error.kind == 'deadline' and context.error is None and refined
                and not context.journal.data.get('handed_to_write')):
            # Refinements are published one by one and stay; a resend starts from the smaller supervoxels.
            error = PaintError('deadline', f'{error.message} The {refined} supervoxel refinements already made are kept; '
                               'resend the same paint to continue from them.',
                               locations_nm=error.locations_nm, supervoxels=error.supervoxels)
            error.__cause__ = caught
        try:
            context.journal.update(write_calls=context.write_calls, write_entries=context.write_entries)
        except Exception as journal_error:
            error.add_note('Paint write-counter recording failed: ' + str(journal_error))
        # Re-read the on-disk marker: rename may have succeeded even if its
        # directory fsync raised, leaving the in-memory journal stale.
        try:
            state = context.journal.refuse(context.error or error, cg=context.cg or cg)
            if state.get('handed_to_write'):
                g.paint_record_state = context.journal.reconcile(context.cg or cg, force=True)
        except Exception as journal_error:
            error.add_note('Paint outcome recording failed: ' + str(journal_error))
        if context.error is not None:
            raise context.error from error
        if error is not caught:
            raise error from caught
        raise
