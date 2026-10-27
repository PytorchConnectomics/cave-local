"""HTTP integration for the optional local supervoxel refinement adapter."""
import functools
import json
import os
from pathlib import Path

from flask import g, has_request_context, jsonify


def configuration(path='/workspace/deployment.json'):
    file = Path(path)
    return json.loads(file.read_text()) if file.exists() else {}


def requires_adapters(spec, data_dir='/data'):
    """Disabling new refinement must preserve existing history and edit fences.

    The presence of a journal is sufficient: parsing happens in recovery, where
    malformed state fails closed instead of allowing unfenced native edits.
    """
    if spec.get('supervoxel_split', {}).get('enabled', False):
        return True
    directory = Path(data_dir)
    current = directory / 'atom_tables' / 'current'
    return (current.exists() or current.is_symlink()
            or any((directory / 'supervoxel_splits' / 'journal').rglob('journal.json')))


def fixture_configuration(data_dir='/data'):
    """Faults are inert unless both the fixture flag and environment opt-in exist."""
    spec = configuration()
    value = os.environ.get('CAVE_SVSPLIT_FAULT', '')
    if not spec.get('test_fixture') or not value:
        return {}
    if value.startswith('file:'):
        path = Path(value[5:])
        return json.loads(path.read_text()) if path.exists() else {}
    try:
        return json.loads(value)
    except ValueError:
        return {'point': value, 'action': 'exit'}


def install(common, data_dir='/data'):
    """Install before app construction, so routes retain wrapped common handlers."""
    from supervoxel_history import install_history, snapshot_read, read_epoch
    from supervoxel_refine import install_fence, recover_if_idle
    install_fence(data_dir=data_dir)
    install_history(data_dir=data_dir)
    from supervoxel_paint_service import install_hooks
    install_hooks()

    def recover_reader():
        from pychunkedgraph.app.app_utils import get_cg
        return recover_if_idle(
            lambda: get_cg(configuration()['graph_id'], skip_cache=True), data_dir=data_dir)
    # Both JSON and binary routes call these common handlers in PCG 2.21.1.
    # Graph-level history mapping also covers internal coordinate/root lookups.
    names = ('handle_root', 'handle_roots', 'handle_children', 'handle_leaves',
             'handle_leaves_many', 'handle_leaves_from_leave', 'handle_subgraph',
             'handle_l2_chunk_children', 'handle_find_minimal_covering_nodes',
             'handle_contact_sites', 'handle_pairwise_contact_sites',
             'handle_get_layer2_graph', 'handle_root_timestamps', 'handle_is_latest_roots')
    for name in names:
        native = getattr(common, name, None)
        if native is None:
            continue

        @functools.wraps(native)
        def read(*args, _native=native, **kwargs):
            result, epoch = snapshot_read(
                lambda: (_native(*args, **kwargs), read_epoch(data_dir)),
                data_dir=data_dir, recover=recover_reader)
            if has_request_context():
                g.svsplit_read_epoch = epoch
            return result
        setattr(common, name, read)


def attach(app, data_dir='/data'):
    from supervoxel_history import PublicationBusy, read_epoch
    from supervoxel_refine import RefinementError
    from pychunkedgraph.app import common as app_common
    from supervoxel_paint import PaintError

    def paint_error(error):
        return jsonify({'error': str(error), 'paint_refusal': error.as_dict()}), 400

    def unavailable(error):
        response = jsonify({'code': 503, 'message': str(error)})
        response.status_code = 503
        response.headers['Retry-After'] = '1'
        return response

    app.register_error_handler(PublicationBusy, unavailable)
    app.register_error_handler(RefinementError, unavailable)
    app.register_error_handler(PaintError, paint_error)
    # PCG's v1 and legacy blueprints register their own Exception handler. It
    # wins over Flask's app handlers and calls this shared function, otherwise
    # converting every non-PCG exception to 500. Handle only errors that escape
    # an operation: intermediate lock/precondition errors may be retried inside
    # refinement and must not mark a later successful response unavailable.
    native_unhandled = app_common.unhandled_exception
    if not getattr(native_unhandled, '_cave_svsplit_unavailable', False):
        @functools.wraps(native_unhandled)
        def unhandled(error):
            if isinstance(error, PaintError):
                return paint_error(error)
            if isinstance(error, (PublicationBusy, RefinementError)):
                return unavailable(error)
            return native_unhandled(error)
        unhandled._cave_svsplit_unavailable = True
        app_common.unhandled_exception = unhandled

    @app.after_request
    def refinement_headers(response):
        # Native common handlers convert LockingError into InternalServerError.
        # The post-lock wrapper marks this request so the HTTP status remains 503.
        if getattr(g, 'svsplit_fenced', False):
            response = unavailable('supervoxel refinement in progress or awaiting repair')
        response.headers['X-Cave-Svsplit-Epoch'] = str(getattr(g, 'svsplit_read_epoch', read_epoch(data_dir)))
        count = getattr(g, 'svsplit_refined', 0)
        if count:
            response.headers['X-Cave-Supervoxels-Refined'] = str(count)
        if hasattr(g, 'paint_operation'):
            response.headers['X-Cave-Supervoxels-Refined'] = str(count)
            response.headers['X-Cave-Paint-Voxels'] = ','.join(map(str, g.paint_counts))
            response.headers['X-Cave-Paint-Record'] = str(g.paint_operation)
            response.headers['X-Cave-Paint-Elapsed-Seconds'] = str(round(g.paint_elapsed, 6))
        if hasattr(g, 'paint_record_state') and response.is_json:
            value = response.get_json()
            value['paint_record_state'] = g.paint_record_state
            response.set_data(json.dumps(value))
        return response
