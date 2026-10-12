"""Exercise production constructor expressions with the real pinned CAVE SDK.

Only HTTP is mocked. The SDK reads and writes real task-local sentinel cache
files, so its default cache overwrite remains a working negative control.
These checks isolate client construction; full acceptance exercises the callers
inside snapshot, backup, restore, rotation and disposable graph validation.
"""
import ast
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests
from caveclient import CAVEclient
from caveclient import auth, datastack_lookup


ROOT = Path(__file__).resolve().parents[1]
CALLERS = ('snapshot.py', 'verify_segmentation_metadata.py', 'verify_segmentation_iteration.py')
SPEC = {'datastack': 'cache_isolation_fixture', 'local_url': 'http://127.0.0.1:29092',
        'aligned_volume': 'cache_isolation_volume'}
ORIGINAL_SERVER = 'http://127.0.0.1:29091'
EXPLICIT_TOKEN = '00000000000000000000000000000000'


def constructor(caller):
    path = ROOT / 'scripts' / caller
    calls = [node for node in ast.walk(ast.parse(path.read_text()))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == 'CAVEclient']
    assert len(calls) == 1, f'{caller} must have exactly one covered client constructor'
    return calls[0]


def construct(caller, *, old_default=False):
    # Evaluate the actual source expression, keeping SDK internals unmodified.
    # Removing just the new keyword recreates the previous production behavior.
    call = constructor(caller)
    if old_default:
        call.keywords = [keyword for keyword in call.keywords if keyword.arg != 'write_server_cache']
    return eval(compile(ast.Expression(call), str(ROOT / 'scripts' / caller), 'eval'),
                {'CAVEclient': CAVEclient, 'spec': dict(SPEC)})


def files(directory):
    return {path.relative_to(directory).as_posix():
            (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns)
            for path in directory.rglob('*') if path.is_file()}


@pytest.fixture
def sdk_boundary(tmp_path, monkeypatch):
    secrets = tmp_path / 'sdk-secrets'; secrets.mkdir(mode=0o700)
    cache = secrets / datastack_lookup.DEFAULT_DATASTACK_FILE
    cache.write_text(json.dumps({SPEC['datastack']: ORIGINAL_SERVER,
                                'unrelated_datastack': 'https://example.org/untouched'}, indent=2) + '\n')
    for filename in ('cave-secret.json', 'chunkedgraph-secret.json', 'unrelated.json',
                     '127.0.0.1:29091-cave-secret.json', '127.0.0.1:29092-cave-secret.json'):
        (secrets / filename).write_text('{"token": "sentinel-file-token", "preserve": true}\n')
    for path in secrets.iterdir():
        path.chmod(0o600)
    # Override SDK-specific paths, never HOME or the runner's actual token files.
    monkeypatch.setattr(datastack_lookup, 'DEFAULT_LOCATION', str(secrets))
    monkeypatch.setattr(auth, 'default_token_location', str(secrets))
    monkeypatch.setattr(auth, 'default_token_file', str(secrets / 'cave-secret.json'))
    monkeypatch.setattr(auth, 'deprecated_token_files', [str(secrets / 'chunkedgraph-secret.json')])
    def forbidden_token_read(*args, **kwargs):
        raise AssertionError('Explicit validation token must not load a personal token')
    monkeypatch.setattr(auth.AuthClient, '_load_token', forbidden_token_read)
    calls = []
    failure = {'phase': None}
    def http(session, method, url, **kwargs):
        calls.append((method, url, dict(session.headers), kwargs))
        assert urlparse(url).netloc == urlparse(SPEC['local_url']).netloc
        assert method.lower() == 'get', 'Client construction must not edit server state'
        if failure['phase'] == 'connection':
            raise requests.ConnectionError('task-local HTTP boundary refused connection')
        response = requests.Response()
        response.url = url
        response.status_code = 200
        response.headers['Content-Type'] = 'application/json'
        if url.endswith('/info/version'):
            value = '4.3.1'
        elif url.endswith('/info/api/v2/datastack/full/' + SPEC['datastack']):
            value = {'aligned_volume': {'name': SPEC['aligned_volume'], 'id': 17},
                     'local_server': SPEC['local_url']}
            if failure['phase'] == 'datastack':
                response.status_code = 500
                value = {'error': 'task-local datastack failure'}
        else:
            raise AssertionError(f'Unexpected construction endpoint: {url}')
        response._content = json.dumps(value).encode()
        return response
    monkeypatch.setattr(requests.Session, 'request', http)
    return secrets, cache, calls, failure


def test_every_host_client_constructor_explicitly_disables_shared_cache_writes():
    covered = []
    for path in (ROOT / 'scripts').glob('*.py'):
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == 'CAVEclient' for node in ast.walk(ast.parse(path.read_text()))):
            covered.append(path.name)
    assert sorted(covered) == sorted(CALLERS), 'A new host SDK constructor needs isolation coverage'
    for caller in CALLERS:
        keywords = {keyword.arg: keyword.value for keyword in constructor(caller).keywords}
        assert isinstance(keywords.get('write_server_cache'), ast.Constant)
        assert keywords['write_server_cache'].value is False


@pytest.mark.parametrize('caller', CALLERS)
def test_real_sdk_routes_to_explicit_restored_server_and_preserves_sentinel_files(sdk_boundary, caller):
    secrets, cache, calls, _ = sdk_boundary
    before = files(secrets); original_home = os.environ.get('HOME')
    client = construct(caller)
    assert client.server_address == SPEC['local_url']
    assert client.local_server == SPEC['local_url']
    assert client.info.get_datastack_info()['aligned_volume']['name'] == SPEC['aligned_volume']
    assert client.auth.token == EXPLICIT_TOKEN
    assert [url for _, url, _, _ in calls] == [SPEC['local_url'] + '/info/version',
        SPEC['local_url'] + '/info/api/v2/datastack/full/' + SPEC['datastack']]
    assert all(headers.get('Authorization') == 'Bearer ' + EXPLICIT_TOKEN for _, _, headers, _ in calls)
    assert json.loads(cache.read_text())[SPEC['datastack']] == ORIGINAL_SERVER
    assert files(secrets) == before
    assert os.environ.get('HOME') == original_home


@pytest.mark.parametrize('caller', CALLERS)
def test_previous_default_really_overwrites_cached_original_server(sdk_boundary, caller):
    secrets, cache, _, _ = sdk_boundary
    before = files(secrets)
    construct(caller, old_default=True)
    assert json.loads(cache.read_text()) == {SPEC['datastack']: SPEC['local_url'],
                                           'unrelated_datastack': 'https://example.org/untouched'}
    after = files(secrets)
    assert after[cache.name][0] != before[cache.name][0]
    assert {name: value for name, value in after.items() if name != cache.name} == {
        name: value for name, value in before.items() if name != cache.name}


@pytest.mark.parametrize('caller', CALLERS)
@pytest.mark.parametrize('phase', ['connection', 'datastack'])
def test_real_sdk_errors_propagate_without_replacing_original_cache(sdk_boundary, caller, phase):
    secrets, _, calls, failure = sdk_boundary
    failure['phase'] = phase; before = files(secrets)
    error = requests.ConnectionError if phase == 'connection' else requests.HTTPError
    with pytest.raises(error):
        construct(caller)
    assert calls and files(secrets) == before


@pytest.mark.parametrize('caller', CALLERS)
def test_absent_cache_is_not_created_by_validation(sdk_boundary, caller):
    secrets, cache, _, _ = sdk_boundary
    cache.unlink(); before = files(secrets)
    construct(caller)
    assert not cache.exists() and files(secrets) == before
