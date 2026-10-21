"""HTTP split/retry contracts; executed in the pinned PCG image (Flask).

Host-only environments may omit Flask. Every graph and voxel access below is a
fake; these tests never connect to a deployment or change a graph database.
"""
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import numpy as np
import pytest

flask = pytest.importorskip('flask')
from flask import Flask, g, jsonify, request
from pychunkedgraph.app import common as native_app_common
from pychunkedgraph.app.app_utils import remap_public as native_remap_public
from pychunkedgraph.app.segmentation.v1.routes import unhandled_exception as native_route_error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_label_policy as policy
import supervoxel_service as service
import supervoxel_history as history
import supervoxel_refine as refinement


def test_public_reader_retries_abandoned_publication_after_transient_recovery_failure(runtime, monkeypatch):
    env = runtime
    monkeypatch.setattr(history, 'install_history', lambda **kwargs: None)
    monkeypatch.setattr(refinement, 'install_fence', lambda **kwargs: None)
    monkeypatch.setattr(refinement, 'install_lock_ownership_fix', lambda: None)
    refinement._set_epoch(env.path, True)
    recoveries, reads = [], []
    now = [1000.0]
    monkeypatch.setattr(refinement.time, 'time', lambda: now[0])

    def recover_locked(graph, data_dir):
        assert graph is env.cg and data_dir == env.path
        recoveries.append(True)
        if len(recoveries) == 1:
            raise OSError('transient replay error')
        refinement._set_epoch(data_dir, False)

    def roots():
        reads.append(True)
        return {'root_ids': ['900']}

    monkeypatch.setattr(refinement, '_recover_locked', recover_locked)
    common = SimpleNamespace(handle_roots=roots)
    service.install(common, env.path)
    env.app.add_url_rule('/roots', view_func=common.handle_roots)
    client = env.app.test_client()
    failed = client.get('/roots')
    assert failed.status_code == 503 and failed.headers['Retry-After'] == '1'
    assert not reads and history.read_epoch(env.path) % 2
    for _ in range(10):
        failed = client.get('/roots')
        assert failed.status_code == 503 and failed.headers['Retry-After'] == '1'
        assert 'cooldown' in failed.json['message']
    assert len(recoveries) == 1 and not reads
    now[0] += refinement.READER_RECOVERY_COOLDOWN_SECONDS
    response = client.get('/roots')
    assert response.status_code == 200 and response.json == {'root_ids': ['900']}
    assert response.headers['X-Cave-Svsplit-Epoch'] == '2'
    assert len(recoveries) == 2 and reads == [True]


class BadRequest(RuntimeError):
    pass


class PreconditionError(RuntimeError):
    pass


class PostconditionError(RuntimeError):
    pass


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv('CAVE_SVSPLIT_FAULT', raising=False)
    data = np.empty((6, 4, 2, 1), np.uint64)
    data[:2] = 11
    data[2:5] = 22
    data[5:] = 33
    env = SimpleNamespace(data=data, calls=[], refinements=[], fault_calls=[], next_id=101,
                          retired=set(), rows={}, boxes={}, root_map={})
    for atom in (11, 22, 33):
        points = np.argwhere(data[..., 0] == atom)
        env.rows[atom] = [atom, atom, *points[0]]
        env.boxes[atom] = [*points.min(0), *(points.max(0) + 1)]

    def tables(*args, **kwargs):
        ids = sorted(env.rows)
        return SimpleNamespace(index=np.array([env.rows[a] for a in ids], np.uint64),
            bounds=np.array([env.boxes[a] for a in ids]), retired=env.retired)
    env.tables = tables

    class Volume:
        def __init__(self, *args, **kwargs):
            pass
        def __getitem__(self, box):
            return env.data[box]
    cg = SimpleNamespace(segmentation_resolution=np.array([2, 3, 4]),
        get_roots=lambda ids: np.array([env.root_map.get(int(atom), 900) for atom in ids], np.uint64))
    env.cg = cg
    utilities = ModuleType('pychunkedgraph.app.app_utils')
    utilities.get_cg = lambda *args, **kwargs: cg
    utilities.handle_supervoxel_id_lookup = lambda cg, coords, ids: np.array([
        env.data[tuple(map(int, xyz))][0] for xyz in coords], np.uint64)
    for name in ('pychunkedgraph', 'pychunkedgraph.app', 'pychunkedgraph.graph'):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules['pychunkedgraph.app'].common = native_app_common
    monkeypatch.setitem(sys.modules, 'pychunkedgraph.app.app_utils', utilities)
    exceptions = ModuleType('pychunkedgraph.graph.exceptions')
    for cls in (BadRequest, PreconditionError, PostconditionError):
        setattr(exceptions, cls.__name__, cls)
    monkeypatch.setitem(sys.modules, exceptions.__name__, exceptions)
    cloudvolume = ModuleType('cloudvolume')
    cloudvolume.CloudVolume = Volume
    monkeypatch.setitem(sys.modules, 'cloudvolume', cloudvolume)
    monkeypatch.setattr(history, 'load_atom_tables', tables)

    def refine(cg, requests, **kwargs):
        env.refinements.append((requests, kwargs))
        old = requests[0]['atom']
        points = np.argwhere(env.data[..., 0] == old)
        low, high = points[:, 0].min(), points[:, 0].max()
        # Preserve singleton shapes in limit tests; the fake still changes ids.
        cut = (int(low) + int(high) + 1) // 2
        atom_ids = (env.next_id, env.next_id + 1)
        env.next_id += 2
        for point in points:
            new = atom_ids[int(point[0] >= cut)]
            env.data[tuple(point)][0] = new
        for atom in atom_ids:
            new_points = np.argwhere(env.data[..., 0] == atom)
            if not len(new_points):
                continue
            env.rows[atom] = [atom, env.rows[old][1], *new_points[0]]
            env.boxes[atom] = [*new_points.min(0), *(new_points.max(0) + 1)]
        env.retired.add(old)
        return SimpleNamespace(count=1)
    env.refine = refine
    monkeypatch.setattr(refinement, 'refine', refine)
    monkeypatch.setattr(refinement, 'fixture_fault', lambda point, **kwargs: env.fault_calls.append(point))
    spec = {'graph_id': 'fixture', 'test_fixture': True, 'shape_xyz': [6, 4, 2],
            'resolution_nm_xyz': [2, 3, 4], 'supervoxel_split': {
                'enabled': True, 'grid_xyz': [1, 1, 1], 'max_atoms': 64, 'max_seconds': 30}}
    env.spec = spec
    monkeypatch.setattr(service, 'configuration', lambda *args, **kwargs: spec)
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_error_handler(BadRequest, lambda error: (jsonify(message=str(error)), 400))
    service.attach(app, tmp_path)
    env.app = app
    env.path = tmp_path
    env.body = {'sources': [[999, 0, 3, 4]], 'sinks': [[999, 6, 3, 4]]}
    return env


def client_for(env, native):
    @env.app.route('/split', methods=['POST'])
    def split():
        return policy.enabled_split(native, 'fixture', env.spec, env.path)
    return env.app.test_client()


def native_spy(env, failures=()):
    def native(table_id):
        env.calls.append(json.loads(request.data))
        if len(env.calls) <= len(failures):
            raise failures[len(env.calls) - 1]
        return {'success': True}
    return native


def test_same_atom_refines_remaps_and_sets_response_header(runtime):
    env = runtime
    body = {'sources': [[999, 0, 3, 4]], 'sinks': [[999, 2, 3, 4]]}
    response = client_for(env, native_spy(env)).post('/split', json=body)
    assert response.status_code == 200
    assert response.headers['X-Cave-Supervoxels-Refined'] == '1'
    assert response.headers['X-Cave-Svsplit-Epoch'] == '0'
    assert len(env.calls) == len(env.refinements) == 1
    request_item, options = env.refinements[0]
    assert request_item == [{'atom': 11, 'source_vox': [[1, 1, 0]], 'sink_vox': [[1, 1, 1]]}]
    assert options['reason'] == 'same-atom'
    assert env.calls[0]['sources'][0] == ['101', 0, 3, 4]
    assert env.calls[0]['sinks'][0] == ['102', 2, 3, 4]


def test_enabled_same_source_label_different_atoms_goes_to_native(runtime):
    env = runtime
    env.rows[22][1] = env.rows[11][1]
    response = client_for(env, native_spy(env)).post('/split', json=env.body)
    assert response.status_code == 200
    assert len(env.calls) == 1 and not env.refinements
    assert 'X-Cave-Supervoxels-Refined' not in response.headers


@pytest.mark.parametrize('error', [
    PostconditionError('Mincut failed. Try with a different set of points.'),
    PreconditionError('Split cut off only the labeled points on the Sink side'),
])
def test_each_recognized_failure_refines_and_retries_exactly_once(runtime, error):
    env = runtime
    env.spec['supervoxel_split']['max_atoms'] = 1
    response = client_for(env, native_spy(env, [error])).post('/split', json=env.body)
    assert response.status_code == 200
    assert len(env.calls) == 2 and len(env.refinements) == 1
    assert env.refinements[0][0][0]['atom'] == 22  # largest current atom first
    assert env.calls[1]['sinks'][0][0] == str(int(env.data[3, 1, 1, 0]))
    assert response.headers['X-Cave-Supervoxels-Refined'] == '1'
    telemetry = [json.loads(path.read_text()) for path in (env.path / 'supervoxel_splits/native_calls').glob('*.json')]
    assert len(telemetry) == 1 and telemetry[0]['calls'] == 2


def test_second_failure_reports_count_and_stops(runtime):
    env = runtime
    env.spec['supervoxel_split']['max_atoms'] = 1
    error = PostconditionError('Mincut failed. Try with a different set of points.')
    response = client_for(env, native_spy(env, [error, error])).post('/split', json=env.body)
    assert response.status_code == 400
    assert 'Refined 1 supervoxels; the split still failed.' in response.json['message']
    assert len(env.calls) == 2 and len(env.refinements) == 1
    assert response.headers['X-Cave-Supervoxels-Refined'] == '1'


def test_refinement_time_limit_stops_further_atoms(runtime, monkeypatch):
    env = runtime
    clock = [0]
    monkeypatch.setattr(policy, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    env.spec['supervoxel_split']['max_seconds'] = 10
    def delayed_refine(*args, **kwargs):
        result = env.refine(*args, **kwargs)
        clock[0] = 11
        return result
    monkeypatch.setattr(refinement, 'refine', delayed_refine)
    response = client_for(env, native_spy(env, [PostconditionError('Mincut failed')])).post('/split', json=env.body)
    assert response.status_code == 200
    assert len(env.refinements) == 1 and len(env.calls) == 2


def test_candidates_exclude_other_roots_and_retired_atoms(runtime):
    env = runtime
    env.root_map[33] = 901
    env.retired.add(22)
    response = client_for(env, native_spy(env, [PostconditionError('Mincut failed')])).post('/split', json=env.body)
    assert response.status_code == 200
    assert [rows[0]['atom'] for rows, _ in env.refinements] == [11]


def test_nonretryable_precondition_is_returned_without_refinement(runtime):
    env = runtime
    client = client_for(env, native_spy(env, [PreconditionError('No local edges found.')]))
    with pytest.raises(PreconditionError, match='No local edges'):
        client.post('/split', json=env.body)
    assert len(env.calls) == 1 and not env.refinements


def test_identical_seed_voxel_is_rejected_before_mutation(runtime):
    env = runtime
    body = {'sources': [[123, 0, 3, 4]], 'sinks': [[456, 0, 3, 4]]}
    response = client_for(env, native_spy(env)).post('/split', json=body)
    assert response.status_code == 400
    assert 'both a source and a sink' in response.json['message']
    assert not env.refinements and not env.calls


def test_remapped_raw_body_is_restored_after_native_error(runtime):
    env = runtime
    with env.app.test_request_context('/split', method='POST', json=env.body):
        original = request.get_data()
        with pytest.raises(ValueError):
            with policy.remapped_body({'sources': []}):
                assert json.loads(request.data) == {'sources': []}
                raise ValueError('native error')
        assert request.data == original


def test_fixture_native_faults_are_ignored_on_nonfixtures(runtime, monkeypatch):
    env = runtime
    env.spec['test_fixture'] = False
    monkeypatch.setenv('CAVE_SVSPLIT_FAULT', json.dumps({'point': 'native', 'calls': 2}))
    response = client_for(env, native_spy(env)).post('/split', json=env.body)
    assert response.status_code == 200 and len(env.calls) == 1
    assert not env.refinements
    assert not (env.path / 'supervoxel_splits/native_calls').exists()


def test_fixture_native_failure_injection_has_one_retry(runtime, monkeypatch):
    env = runtime
    env.spec['supervoxel_split']['max_atoms'] = 1
    monkeypatch.setenv('CAVE_SVSPLIT_FAULT', json.dumps({'point': 'native', 'error': 'isolating', 'calls': 1}))
    response = client_for(env, native_spy(env)).post('/split', json=env.body)
    assert response.status_code == 200 and len(env.calls) == 1
    assert len(env.refinements) == 1
    telemetry = next((env.path / 'supervoxel_splits/native_calls').glob('*.json'))
    assert json.loads(telemetry.read_text())['calls'] == 2


def test_disabled_source_rejection_message_is_preserved(runtime, monkeypatch):
    env = runtime
    env.spec['supervoxel_split']['enabled'] = False
    env.rows[22][1] = env.rows[11][1]
    prepared = env.path / 'prepared.json'
    prepared.write_text(json.dumps({'atom_policy': 'agglomerated_supervoxels', 'spec': {'graph_id': 'fixture'}}))
    monkeypatch.setattr(policy, 'Path', lambda path: prepared if str(path) == '/data/prepared.json' else Path(path))
    np.save(env.path / 'atom_index.npy', np.array(list(env.rows.values()), np.uint64))
    source_index = policy.source_label_index
    monkeypatch.setattr(policy, 'source_label_index', lambda: source_index(env.path))
    assert not (env.path / 'atom_bounds.npy').exists()
    assert not (env.path / 'initial_roots.npy').exists()
    common = SimpleNamespace(handle_split=native_spy(env))
    policy.install(common)
    with env.app.test_request_context('/split', method='POST', json=env.body):
        with pytest.raises(BadRequest) as error:
            common.handle_split('fixture')
    assert str(error.value) == ('Both split groups contain the same source supervoxel. '
        'Source labels are indivisible; choose different source labels to separate a merged segment.')
    assert not env.calls and not env.refinements


def test_disabled_legacy_policy_ignores_unrelated_corrupt_tables(tmp_path, monkeypatch):
    rows = np.array([[11, 7, 0, 0, 0], [22, 8, 1, 0, 0]], np.uint64)
    np.save(tmp_path / 'atom_index.npy', rows)
    for name in ('atom_bounds.npy', 'initial_roots.npy'):
        (tmp_path / name).write_bytes(b'not an array')
    monkeypatch.setattr(history, 'load_atom_tables', lambda *args, **kwargs: pytest.fail('legacy guard loaded all tables'))
    actual = policy.source_label_index(tmp_path)
    assert isinstance(actual, np.memmap)
    np.testing.assert_array_equal(actual, rows)


def test_disabled_policy_retains_generation_loader_when_present(tmp_path, monkeypatch):
    folder = tmp_path / 'atom_tables'
    folder.mkdir()
    (folder / 'current').symlink_to('missing')
    calls = []
    monkeypatch.setattr(history, 'load_atom_tables', lambda path: calls.append(path) or SimpleNamespace(index='generation'))
    assert policy.source_label_index(tmp_path) == 'generation'
    assert calls == [tmp_path]


def test_read_handler_retries_changed_epoch_and_sets_validated_header(runtime, monkeypatch):
    env = runtime
    monkeypatch.setattr(history, 'install_history', lambda **kwargs: None)
    monkeypatch.setattr(refinement, 'install_fence', lambda **kwargs: None)
    directory = env.path / 'supervoxel_splits'
    directory.mkdir()
    epoch = directory / 'epoch'
    epoch.write_text('0')
    calls = []
    def roots():
        calls.append(1)
        if len(calls) == 1:
            epoch.write_text('2')
        return {'call': len(calls)}
    common = SimpleNamespace(handle_roots=roots)
    service.install(common, env.path)
    env.app.add_url_rule('/roots', view_func=common.handle_roots)
    response = env.app.test_client().get('/roots')
    assert response.json == {'call': 2}
    assert response.headers['X-Cave-Svsplit-Epoch'] == '2'


def test_publication_busy_and_converted_fence_are_503(runtime):
    env = runtime
    @env.app.route('/busy')
    def busy():
        raise history.PublicationBusy('supervoxel publication in progress')
    @env.app.route('/fenced')
    def fenced():
        g.svsplit_fenced = True
        return {'code': 500, 'message': 'native InternalServerError'}, 500
    for path in ('/busy', '/fenced'):
        response = env.app.test_client().get(path)
        assert response.status_code == 503
        assert response.headers['Retry-After'] == '1'
        assert response.headers['X-Cave-Svsplit-Epoch'] == '0'


@pytest.mark.parametrize('path', ['read', 'undo_exhausted', 'refinement', 'retried', 'other'])
def test_native_blueprint_errors_preserve_503_without_marking_success(runtime, path):
    """Use PCG's real route wrapper and blueprint handler, which beat app handlers."""
    env = runtime
    blueprint = flask.Blueprint('native_errors', __name__)
    blueprint.before_request(native_app_common.before_request)
    blueprint.register_error_handler(Exception, native_route_error)
    attempts = []

    @blueprint.route('/native/<path>')
    @native_remap_public
    def endpoint(path):
        if path == 'read':
            # Exercise snapshot_read itself, rather than raising a lookalike.
            directory = env.path / 'supervoxel_splits'
            directory.mkdir(exist_ok=True)
            (directory / 'epoch').write_text('1')
            return history.snapshot_read(lambda: {'ok': True}, env.path, timeout=0)
        if path == 'undo_exhausted':
            def stale():
                attempts.append(1)
                raise history.StaleTranslation('retired before lock')
            return history.retry_translated_operation(stale, env.path)
        if path == 'refinement':
            raise refinement.RefinementError('root changed during refinement; retry')
        if path == 'retried':
            try:
                raise refinement.PreconditionsChanged('handled internally')
            except refinement.RefinementError:
                return {'ok': True}
        raise ValueError('unrelated native error')

    env.app.register_blueprint(blueprint)
    response = env.app.test_client().get('/native/' + path)
    if path == 'retried':
        assert response.status_code == 200
        assert response.json == {'ok': True}
        assert 'Retry-After' not in response.headers
    elif path == 'other':
        assert response.status_code == 500
        assert 'Retry-After' not in response.headers
    else:
        assert response.status_code == 503
        assert response.headers['Retry-After'] == '1'
        assert 'supervoxel' in response.json['message'] or 'root changed' in response.json['message']
        assert response.headers['X-Cave-Svsplit-Epoch'] == ('1' if path == 'read' else '0')
    if path == 'undo_exhausted':
        assert len(attempts) == 3


def test_disabling_new_refinement_preserves_existing_lineage_adapters(tmp_path):
    disabled = {'supervoxel_split': {'enabled': False}}
    assert not service.requires_adapters(disabled, tmp_path)
    assert service.requires_adapters({'supervoxel_split': {'enabled': True}}, tmp_path)
    generations = tmp_path / 'atom_tables'
    generations.mkdir()
    (generations / 'current').symlink_to('1')
    # Even a damaged/dangling generation link must not silently disable safety.
    assert service.requires_adapters(disabled, tmp_path)


@pytest.mark.parametrize('relative', ['op/journal.json', 'aborted/op/journal.json'])
def test_disabled_journal_presence_requires_adapters_without_parsing(tmp_path, relative):
    journal = tmp_path / 'supervoxel_splits' / 'journal' / relative
    journal.parent.mkdir(parents=True)
    journal.write_text('{malformed and deliberately not parsed by the gate')
    assert service.requires_adapters({'supervoxel_split': {'enabled': False}}, tmp_path)
