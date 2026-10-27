"""HTTP bounds and request isolation; no deployment or graph database used."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip('flask')
pytest.importorskip('pychunkedgraph')
from flask import Flask, g, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_label_policy as policy
import supervoxel_service as service
import supervoxel_paint_service as paint_service
from supervoxel_paint import PaintError


def specimen(enabled=True):
    return {'graph_id': 'paint_test', 'shape_xyz': [16, 16, 8],
            'resolution_nm_xyz': [10, 10, 12.5], 'graph_chunk_xyz': [16, 16, 8],
            'supervoxel_split': {'enabled': enabled, 'paint': {'enabled': enabled}}}


def body():
    return {'paint': {'version': 1,
        'sources': [{'points_nm': [[25, 25, 31.25]], 'radius_nm': 4}],
        'sinks': [{'points_nm': [[115, 25, 31.25]], 'radius_nm': 4}]}}


def app_for(monkeypatch, spec, tmp_path, native=None):
    monkeypatch.setattr(service, 'configuration', lambda *args, **kwargs: spec)
    common = SimpleNamespace(handle_split=native or (lambda table: {'native': table}))
    policy.install(common)
    app = Flask(__name__)
    app.config['TESTING'] = True
    service.attach(app, tmp_path)
    app.add_url_rule('/split', view_func=lambda: common.handle_split('paint_test'), methods=['POST'])
    return app


def no_graph(monkeypatch):
    from pychunkedgraph.app import app_utils
    monkeypatch.setattr(app_utils, 'get_cg', lambda *a, **k: pytest.fail('unexpected graph access'))


def test_disabled_paint_is_structured_and_points_still_native(monkeypatch, tmp_path):
    no_graph(monkeypatch)
    client = app_for(monkeypatch, specimen(False), tmp_path).test_client()
    response = client.post('/split', json=body())
    assert response.status_code == 400
    assert response.json['paint_refusal']['kind'] == 'disabled'
    response = client.post('/split', json={'sources': [], 'sinks': []})
    assert response.status_code == 200 and response.json == {'native': 'paint_test'}


@pytest.mark.parametrize('change,kind', [
    (lambda b: b.update(sources=[[1, 2, 3, 4]]), 'limit'),
    (lambda b: b['paint'].update(version=2), 'limit'),
    (lambda b: b['paint'].update(sources=[]), 'empty_label'),
    (lambda b: b['paint']['sources'][0].update(radius_nm=501), 'limit'),
    (lambda b: b['paint']['sources'][0].update(points_nm=[[float('inf'), 2, 3]]), 'limit'),
])
def test_rejections_precede_graph_access(monkeypatch, tmp_path, change, kind):
    no_graph(monkeypatch)
    value = body()
    change(value)
    response = app_for(monkeypatch, specimen(), tmp_path).test_client().post('/split', json=value)
    assert response.status_code == 400
    assert response.json['paint_refusal']['kind'] == kind


def test_oversized_body_checked_before_decoder(monkeypatch, tmp_path):
    no_graph(monkeypatch)
    spec = specimen()
    spec['supervoxel_split']['paint']['max_request_bytes'] = 16
    response = app_for(monkeypatch, spec, tmp_path).test_client().post('/split', data=b'{"paint":' + b' ' * 20)
    assert response.status_code == 400 and response.json['paint_refusal']['kind'] == 'limit'
    assert 'max_request_bytes' in response.json['error']


@pytest.mark.parametrize('payload', [b'{"paint":', b'{"paint":null}', b'{"paint":[]}'])
def test_malformed_json_precedes_graph_access(monkeypatch, tmp_path, payload):
    no_graph(monkeypatch)
    response = app_for(monkeypatch, specimen(), tmp_path).test_client().post('/split', data=payload)
    assert response.status_code == 400 and response.json['paint_refusal']['kind'] == 'limit'


@pytest.mark.parametrize('mode', ['paint_disabled', 'source_labels', 'spatial_tiles', 'paint_enabled'])
@pytest.mark.parametrize('payload', [
    b'{"sources":[],"sinks":[],"padding":"' + b'x' * 262145 + b'"}',
    b'{"sources":', b'not-json', b'[]', b'null',
    b'{"metadata":{"paint":true},"sources":[],"sinks":[]}',
    b'{"metadata":"paint","sources":[],"sinks":[]}',
])
def test_point_bodies_reach_native_unchanged(monkeypatch, tmp_path, mode, payload):
    no_graph(monkeypatch)
    spec = specimen(mode == 'paint_enabled')
    prepared = tmp_path / 'prepared.json'
    if mode in ('source_labels', 'spatial_tiles'):
        spec.pop('supervoxel_split')
        prepared.write_text(json.dumps({'atom_policy': mode, 'spec': spec}))
    original_path = policy.Path
    monkeypatch.setattr(policy, 'Path', lambda value: prepared if value == '/data/prepared.json' else original_path(value))
    seen = []

    def native(table):
        seen.append((table, request.get_data()))
        return {'native': table}, 202

    response = app_for(monkeypatch, spec, tmp_path, native).test_client().post('/split', data=payload)
    assert response.status_code == 202 and response.json == {'native': 'paint_test'}
    assert seen == [('paint_test', payload)]


@pytest.mark.parametrize('key', [b'"paint"', b'"\\u0070aint"', b'"pa\\u0069\\u006Et"'])
def test_escaped_paint_keys_still_receive_raw_body_limit(monkeypatch, tmp_path, key):
    no_graph(monkeypatch)
    spec = specimen(False)
    spec['supervoxel_split']['paint']['max_request_bytes'] = 16
    response = app_for(monkeypatch, spec, tmp_path).test_client().post('/split', data=b'{' + key + b':' + b' ' * 32)
    assert response.status_code == 400
    assert response.json['paint_refusal']['kind'] == 'limit'
    assert 'max_request_bytes' in response.json['error']


def test_expired_refinement_budget_does_not_read_or_publish_graph(monkeypatch, tmp_path):
    import supervoxel_refine as refinement
    monkeypatch.setattr(refinement, 'install_lock_ownership_fix', lambda: None)
    monkeypatch.setattr(refinement, '_recover_locked', lambda *a: pytest.fail('recovery after deadline'))
    with pytest.raises(PaintError) as caught:
        refinement.refine(SimpleNamespace(cache=None), [{'atom': 7}], data_dir=tmp_path, max_seconds=0)
    assert caught.value.kind == 'deadline'
    assert not list(tmp_path.rglob('journal.json'))


def test_paint_cannot_bypass_check_with_mincut_false(monkeypatch, tmp_path):
    no_graph(monkeypatch)
    response = app_for(monkeypatch, specimen(), tmp_path).test_client().post('/split?mincut=false', json=body())
    assert response.status_code == 400 and response.json['paint_refusal']['kind'] == 'limit'


def test_paint_headers_include_zero_refinements_and_elapsed(tmp_path):
    app = Flask(__name__)
    service.attach(app, tmp_path)
    @app.route('/result')
    def result():
        g.paint_counts, g.paint_operation, g.paint_elapsed = [5, 7], 42, 1.25
        g.svsplit_refined = 0
        return {'operation_id': 42}
    response = app.test_client().get('/result')
    assert response.headers['X-Cave-Supervoxels-Refined'] == '0'
    assert response.headers['X-Cave-Paint-Voxels'] == '5,7'
    assert response.headers['X-Cave-Paint-Record'] == '42'
    assert response.headers['X-Cave-Paint-Elapsed-Seconds'] == '1.25'


def test_unarmed_context_is_empty():
    assert paint_service.active_paint.get() is None
    assert paint_service.writing_paint.get() is None


def test_pending_state_survives_native_error_response(tmp_path):
    app = Flask(__name__)
    service.attach(app, tmp_path)
    @app.route('/failure')
    def failure():
        g.paint_record_state = {'state': 'ambiguous', 'operation_id': 7}
        raise PaintError('deadline', 'late')
    response = app.test_client().get('/failure')
    assert response.status_code == 400
    assert response.json['paint_record_state']['state'] == 'ambiguous'


def test_outer_paint_refusal_persists_observed_write_counters(monkeypatch, tmp_path):
    from datetime import timedelta
    from pychunkedgraph.app import app_utils
    import supervoxel_paint_history as history
    cg = SimpleNamespace(meta=SimpleNamespace(graph_config=SimpleNamespace(ROOT_LOCK_EXPIRY=timedelta(seconds=5))))
    monkeypatch.setattr(app_utils, 'get_cg', lambda *args, **kwargs: cg)
    monkeypatch.setattr(history, 'recover_paint', lambda *args: None)
    monkeypatch.setattr(paint_service, '_fault', lambda *args, **kwargs: None)
    grouped = {1: {'source': np.asarray([[2, 2, 2]]), 'sink': np.empty((0, 3), int)},
               2: {'source': np.empty((0, 3), int), 'sink': np.asarray([[11, 2, 2]])}}
    monkeypatch.setattr(paint_service, '_lookup', lambda *args: (100, [1], [2], grouped))
    recorded = {}
    journal = SimpleNamespace(data=recorded, update=lambda **values: recorded.update(values),
                              refuse=lambda *args, **kwargs: recorded)
    monkeypatch.setattr(history.PaintJournal, 'create', lambda *args, **kwargs: journal)

    def native(table):
        context = paint_service.active_paint.get()
        context.write_calls, context.write_entries = 3, 2
        raise PaintError('deadline', 'refused after native handoff')

    app = app_for(monkeypatch, specimen(), tmp_path, native)

    @app.before_request
    def identity():
        g.auth_user = {'id': 1}

    response = app.test_client().post('/split', json=body())
    assert response.status_code == 400 and response.json['paint_refusal']['kind'] == 'deadline'
    assert recorded['write_calls'] == 3 and recorded['write_entries'] == 2
