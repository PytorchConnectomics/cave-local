"""Regress the real two-CPU Docker creation failure without contacting Docker."""
import importlib.util
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import create_segmentation_deployment as creator
import docker_capacity


@pytest.fixture
def package(tmp_path, monkeypatch):
    repo = tmp_path / 'package'; repo.mkdir()
    for directory in ('config', 'scripts', 'templates', 'docker'):
        shutil.copytree(ROOT / directory, repo / directory, ignore=shutil.ignore_patterns('__pycache__'))
    for filename in ('compose.yml', 'images.lock.json', 'upstream.lock.json'):
        shutil.copyfile(ROOT / filename, repo / filename)
    viewer = repo / '.runtime/viewer'; viewer.mkdir(parents=True)
    (viewer / 'index.html').write_text('fixture viewer')
    monkeypatch.setattr(creator, 'ROOT', repo)
    return repo


def manifest(tmp_path):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    values = {'graph_id': 'cpu_fixture', 'local_url': f'http://127.0.0.1:{port}',
              'image_https': 'https://example.org/image/', 'segmentation_https': 'https://example.org/labels/',
              'shape_xyz': [48, 40, 20], 'resolution_nm_xyz': [8, 8, 24], 'graph_chunk_xyz': [16, 16, 8]}
    path = tmp_path / 'manifest.json'; path.write_text(json.dumps(values))
    return path


@pytest.mark.parametrize('capacity', [1, 2, 4, 8])
def test_generated_quotas_fit_actual_daemon_and_keep_higher_host_defaults(package, tmp_path, monkeypatch, capacity):
    calls = []
    def reply(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(capacity), stderr='')
    monkeypatch.setattr(docker_capacity.subprocess, 'run', reply)
    monkeypatch.setattr(creator, 'available_cpus', docker_capacity.available_cpus)
    source = manifest(tmp_path); original_source = source.read_bytes()
    original_template = (package / 'compose.yml').read_bytes()
    template = yaml.safe_load(original_template)
    destination = creator.create(source, tmp_path / 'deployment')
    generated = yaml.safe_load((destination / 'compose.yml').read_text())
    expected = {name: min(float(service['cpus']), capacity)
                for name, service in template['services'].items() if 'cpus' in service}
    actual = {name: service['cpus'] for name, service in generated['services'].items() if 'cpus' in service}
    assert actual == expected
    assert generated['services']['pcg']['cpus'] == min(4, capacity)
    assert all(0.01 <= quota <= capacity for quota in actual.values())
    assert {name: service.get('mem_limit') for name, service in generated['services'].items()} == {
        name: service.get('mem_limit') for name, service in template['services'].items()}
    metadata = json.loads((destination / 'deployment.json').read_text())
    assert metadata['docker_cpu_capacity'] == capacity
    assert metadata['service_cpu_limits'] == actual
    assert calls == [['docker', 'info', '--format', '{{json .NCPU}}']]
    assert (package / 'compose.yml').read_bytes() == original_template
    assert source.read_bytes() == original_source


@pytest.mark.parametrize('reply', ['', 'unknown', 'null', 'true', 'false', '0', '-1', '2.0', '"2"', '{}', '[]'])
def test_invalid_daemon_capacity_creates_no_destination(package, tmp_path, monkeypatch, reply):
    monkeypatch.setattr(docker_capacity.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=reply, stderr=''))
    monkeypatch.setattr(creator, 'available_cpus', docker_capacity.available_cpus)
    source = manifest(tmp_path); before = source.read_bytes(); destination = tmp_path / 'rejected'
    with pytest.raises(RuntimeError, match='positive integer'):
        creator.create(source, destination)
    assert not destination.exists() and source.read_bytes() == before


@pytest.mark.parametrize('failure', ['daemon-error', 'missing-command', 'timeout'])
def test_daemon_failure_is_actionable_and_does_not_start_creation(package, tmp_path, monkeypatch, failure):
    def failed(*args, **kwargs):
        if failure == 'missing-command':
            raise FileNotFoundError('docker')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args[0], 30)
        return SimpleNamespace(returncode=1, stdout='', stderr='private daemon diagnostic')
    monkeypatch.setattr(docker_capacity.subprocess, 'run', failed)
    monkeypatch.setattr(creator, 'available_cpus', docker_capacity.available_cpus)
    target = tmp_path / 'rejected'
    with pytest.raises(RuntimeError, match='check Docker access and context') as caught:
        creator.create(manifest(tmp_path), target)
    assert 'private daemon diagnostic' not in str(caught.value)
    assert not target.exists()


def test_restore_regenerates_quotas_for_the_new_daemon(package, tmp_path, monkeypatch):
    # Reuse archive/Docker/API boundary fixtures, but execute the real generator
    # when restore invokes create. Archive integrity and state handling are real.
    spec = importlib.util.spec_from_file_location('cpu_lifecycle_fixture', ROOT / 'tests/test_operation_contracts.py')
    fixtures = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixtures)
    case = fixtures.LifecycleContracts(methodName='runTest'); case.setUp()
    try:
        case.spec.update(shape_xyz=[48, 40, 20], resolution_nm_xyz=[8, 8, 24], graph_chunk_xyz=[16, 16, 8],
                         docker_cpu_capacity=128, service_cpu_limits={'pcg': 4}, bigtable_project='original-store-namespace')
        for name in ('image', 'labels'):
            (case.source / name).mkdir()
        (case.deployment / 'deployment.json').write_text(json.dumps(case.spec))
        archive, _ = case.backup()
        monkeypatch.setattr(creator, 'available_cpus', lambda: 2)
        calls = []
        original_io = case.command_io
        def process(command, cwd=None):
            args = list(map(str, command))
            if len(args) > 1 and args[1] == 'create':
                calls.append(args)
                creator.create(args[2], args[3], args[args.index('--source-dir') + 1],
                               args[args.index('--project-name') + 1])
                return ''
            return original_io(command, cwd)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        target = case.root / 'restored'
        with case.boundaries(run=process):
            result = fixtures.operations.restore(archive, target, port)
        actual = yaml.safe_load((target / 'compose.yml').read_text())
        metadata = json.loads((target / 'deployment.json').read_text())
        assert len(calls) == 1 and result['state_compared_to_backup']
        assert actual['services']['pcg']['cpus'] == 2
        assert all(service.get('cpus', 2) <= 2 for service in actual['services'].values())
        assert metadata['docker_cpu_capacity'] == 2
        assert metadata['bigtable_project'] == 'original-store-namespace'
        assert json.loads((case.deployment / 'deployment.json').read_text())['docker_cpu_capacity'] == 128
    finally:
        case.doCleanups()
