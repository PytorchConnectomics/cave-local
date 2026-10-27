"""Source-label guard and opt-in, bounded local supervoxel refinement."""
import contextlib
import json
import re
import time
import uuid
from pathlib import Path

import numpy as np
from flask import g, request


_PAINT_KEY = re.compile(rb'"(?:p|\\u0070)(?:a|\\u0061)(?:i|\\u0069)(?:n|\\u006[eE])(?:t|\\u0074)"')


def _has_paint_key(raw):
    """Recognize a top-level paint key without decoding an unbounded body.

    Ignore nested keys and string values. JSON's escaped spelling of the key
    must still receive the byte limit before the full JSON decoder runs.
    """
    if b'"paint"' not in raw and b'\\u' not in raw:
        return False
    depth, start, escaped, key = 0, None, False, False
    for index, value in enumerate(raw):
        if start is not None:
            if escaped:
                escaped = False
            elif value == 92:
                escaped = True
            elif value == 34:
                if depth == 1 and key and index - start <= 31 and _PAINT_KEY.fullmatch(raw[start:index + 1]):
                    return True
                start = None
            continue
        if value == 34:
            start = index
        elif value in (123, 91):
            if depth == 0 and value != 123:
                return False
            depth += 1
            if depth == 1:
                key = True
        elif value in (125, 93):
            depth -= 1
            if depth <= 0:
                return False
        elif depth == 1 and value in (44, 58):
            key = value == 44
    return False


@contextlib.contextmanager
def remapped_body(body):
    """PCG 2.21.1 parses request.data directly; override only this request's bytes.

    Flask does not expose a public setter for the consumed raw body. Restore its
    cached bytes in finally; no application-global parser or Flask class is patched.
    """
    req = request._get_current_object()
    previous = req.get_data()
    # Werkzeug in the pinned image also caches the data property separately.
    # Invalidate it for the override and restore its prior presence on exit.
    missing = object()
    previous_property = req.__dict__.pop('data', missing)
    req._cached_data = json.dumps(body).encode()
    try:
        yield
    finally:
        req._cached_data = previous
        if previous_property is missing:
            req.__dict__.pop('data', None)
        else:
            req.__dict__['data'] = previous_property


def retryable(error):
    message = str(error).lower()
    return 'mincut failed' in message or 'cut off only the labeled points' in message


def source_label_index(data_dir='/data'):
    """Keep the disabled legacy guard independent of unrelated dense tables."""
    directory = Path(data_dir)
    current = directory / 'atom_tables' / 'current'
    if current.exists() or current.is_symlink():
        from supervoxel_history import load_atom_tables
        return load_atom_tables(directory).index
    return np.load(directory / 'atom_index.npy', mmap_mode='r', allow_pickle=False)


def enabled_split(native, table_id, spec, data_dir='/data'):
    from cloudvolume import CloudVolume
    from pychunkedgraph.app import app_utils
    from pychunkedgraph.graph.exceptions import BadRequest, PostconditionError, PreconditionError
    from supervoxel_history import load_atom_tables, snapshot_read
    from supervoxel_refine import refine, fixture_fault
    from supervoxel_service import fixture_configuration

    if table_id != spec['graph_id']:
        raise BadRequest('Source-label metadata belongs to another graph.')
    options = spec['supervoxel_split']
    grid = options.get('grid_xyz', [16, 16, 16])
    limit = options.get('max_atoms', 64)
    deadline = time.monotonic() + options.get('max_seconds', 30)
    body = json.loads(request.data)
    sources, sinks = body.get('sources', []), body.get('sinks', [])
    if not sources or not sinks:
        return native(table_id)
    coords = np.asarray([seed[1:] for seed in sources + sinks], float)
    coords = np.floor(coords / np.asarray(spec['resolution_nm_xyz'])).astype(np.int64)
    if coords.shape[1:] != (3,) or np.any(coords < 0) or np.any(coords >= spec['shape_xyz']):
        raise BadRequest('Split seeds must lie inside the atomic volume.')
    source_count = len(sources)
    if set(map(tuple, coords[:source_count])) & set(map(tuple, coords[source_count:])):
        raise BadRequest('A voxel cannot be both a source and a sink seed.')
    cg = app_utils.get_cg(table_id, skip_cache=True)
    count = 0
    attempted = set()
    request_id = uuid.uuid4().hex
    call_count = 0

    def lookup():
        cv = CloudVolume('file://' + str(Path(data_dir) / 'atomic'), cache=False, progress=False)
        ids = np.array([cv[tuple(slice(int(v), int(v) + 1) for v in xyz)].reshape(-1)[0]
                        for xyz in coords], np.uint64)
        if np.any(ids == 0):
            raise BadRequest('Split seeds must lie inside foreground voxels.')
        return ids

    def atoms():
        return snapshot_read(lookup, data_dir=data_dir)

    def apply(atom, ids, seeded=False):
        nonlocal count
        if count >= limit or time.monotonic() >= deadline or int(atom) in attempted:
            return
        attempted.add(int(atom))
        item = {'atom': int(atom)}
        if seeded:
            tables = load_atom_tables(data_dir)
            pos = np.searchsorted(tables.index[:, 0], atom)
            low = tables.bounds[pos, :3]
            item['source_vox'] = (coords[:source_count][ids[:source_count] == atom] - low)[:, ::-1].tolist()
            item['sink_vox'] = (coords[source_count:][ids[source_count:] == atom] - low)[:, ::-1].tolist()
        result = refine(cg, [item], grid_xyz=grid, reason='same-atom' if seeded else 'mincut-failed', data_dir=data_dir)
        count += result.count
        g.svsplit_refined = count

    ids = atoms()
    for atom in sorted(set(ids[:source_count]) & set(ids[source_count:])):
        apply(atom, ids, seeded=True)
    ids = atoms()

    def run_native():
        nonlocal call_count
        call_count += 1
        current = atoms()
        remapped = dict(body)
        remapped['sources'] = [[str(int(atom)), *seed[1:]] for atom, seed in zip(current[:source_count], sources)]
        remapped['sinks'] = [[str(int(atom)), *seed[1:]] for atom, seed in zip(current[source_count:], sinks)]
        fault = fixture_configuration(data_dir)
        if spec.get('test_fixture'):
            folder = Path(data_dir) / 'supervoxel_splits' / 'native_calls'
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (request_id + '.json')).write_text(json.dumps({'calls': call_count, 'refined': count, 'body': remapped}))
        fixture_fault('before_native', data_dir=data_dir, call=call_count)
        if fault.get('point') == 'native' and call_count <= fault.get('calls', 1):
            if fault.get('error', 'mincut') == 'isolating':
                raise PreconditionError('Split cut off only the labeled points on the Sink side')
            raise PostconditionError('Mincut failed. Try with a different set of points.')
        with remapped_body(remapped):
            return native(table_id)

    try:
        return run_native()
    except Exception as error:
        if not retryable(error):
            raise
        # Candidates are current atoms of the seed root intersecting PCG's box.
        ids = atoms()
        roots = np.unique(cg.get_roots(ids))
        tables = load_atom_tables(data_dir)
        offset = np.asarray(body.get('bb_offset', (120, 120, 12)), int)
        if offset.shape != (3,) or np.any(offset < 0):
            raise BadRequest('bb_offset must contain three nonnegative voxel distances.')
        low, high = coords.min(0) - offset, coords.max(0) + offset + 1
        mask = np.all(tables.bounds[:, 3:] > low, axis=1) & np.all(tables.bounds[:, :3] < high, axis=1)
        candidate = tables.index[mask, 0]
        candidate = np.array([a for a in candidate if int(a) not in tables.retired and int(a) not in attempted], np.uint64)
        if len(candidate):
            candidate = candidate[np.isin(cg.get_roots(candidate), roots)]
            # Counts from the current atomic labels, not bounding-box estimates.
            cv = CloudVolume('file://' + str(Path(data_dir) / 'atomic'), cache=False, progress=False)
            sizes = []
            for atom in candidate:
                if time.monotonic() >= deadline:
                    break
                pos = np.searchsorted(tables.index[:, 0], atom)
                box = tables.bounds[pos]
                cut = cv[tuple(slice(int(a), int(b)) for a, b in zip(box[:3], box[3:]))]
                sizes.append((int(np.count_nonzero(cut == atom)), int(atom)))
            for _, atom in sorted(sizes, key=lambda pair: (-pair[0], pair[1])):
                if count >= limit or time.monotonic() >= deadline:
                    break
                apply(atom, ids)
        try:
            return run_native()
        except Exception as retried:
            if retryable(retried):
                message = getattr(retried, 'message', str(retried))
                raise BadRequest(f'{message} Refined {count} supervoxels; the split still failed.') from retried
            raise


def install(common):
    from pychunkedgraph.app import app_utils
    from pychunkedgraph.graph.exceptions import BadRequest
    from supervoxel_service import configuration
    spec = configuration()
    original_split = common.handle_split

    def split(table_id):
        # Paint bounds apply only to paint bodies. Leave point requests and
        # their native decoding/error behavior independent of this feature.
        from supervoxel_paint import PAINT_LIMITS, PaintError
        started = time.monotonic()
        raw = request.get_data()
        paint_body = _has_paint_key(raw)
        if paint_body:
            maximum = spec.get('supervoxel_split', {}).get('paint', {}).get(
                'max_request_bytes', PAINT_LIMITS['max_request_bytes'])
            if len(raw) > maximum:
                raise PaintError('limit', 'Paint request exceeds max_request_bytes.')
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            if paint_body:
                raise PaintError('limit', 'Paint request must be valid JSON.') from error
            return original_split(table_id)
        if not isinstance(body, dict):
            return original_split(table_id)
        if 'paint' in body:
            from supervoxel_paint_service import painted_split
            return painted_split(original_split, table_id, spec, body, started=started)
        if spec.get('supervoxel_split', {}).get('enabled', False):
            return enabled_split(original_split, table_id, spec)
        prepared = Path('/data/prepared.json')
        if prepared.exists() and json.loads(prepared.read_text()).get('atom_policy') in ('source_labels', 'agglomerated_supervoxels'):
            geometry = json.loads(prepared.read_text())
            if geometry['spec']['graph_id'] != table_id:
                raise BadRequest('Source-label metadata belongs to another graph.')
            data = json.loads(request.data)
            sources, sinks = data.get('sources', []), data.get('sinks', [])
            if sources and sinks:
                graph = app_utils.get_cg(table_id, skip_cache=True)
                seeds = sources + sinks
                ids = np.array([seed[0] for seed in seeds], dtype=np.uint64)
                coordinates = np.array([seed[1:] for seed in seeds]) / graph.segmentation_resolution
                atoms = app_utils.handle_supervoxel_id_lookup(graph, coordinates, ids)
                mapping = source_label_index()
                positions = np.searchsorted(mapping[:, 0], atoms)
                if np.any(positions >= len(mapping)) or np.any(mapping[positions, 0] != atoms):
                    raise BadRequest('Split seeds could not be resolved to source supervoxels.')
                labels = mapping[positions, 1]
                if set(labels[:len(sources)]) & set(labels[len(sources):]):
                    raise BadRequest(
                        'Both split groups contain the same source supervoxel. '
                        'Source labels are indivisible; choose different source labels to separate a merged segment.'
                    )
        return original_split(table_id)

    common.handle_split = split
