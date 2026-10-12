"""Check portability boundaries before any services or databases are changed."""
import copy
import json
import socket
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import create_segmentation_deployment as creator


def manifest(port=18881):
    return {'graph_id': 'fixture', 'local_url': f'http://127.0.0.1:{port}',
            'image_https': 'file:///source/image/', 'segmentation_https': 'file:///source/segmentation/',
            'shape_xyz': [48, 40, 20], 'resolution_nm_xyz': [100, 120, 300],
            'graph_chunk_xyz': [16, 16, 8], 'atom_policy': 'source_labels'}


@pytest.mark.parametrize('field,value', [
    ('graph_id', '../outside'), ('local_url', 'http://0.0.0.0:18881'),
    ('resolution_nm_xyz', [float('nan'), 100, 300]), ('shape_xyz', [48, 0, 20]),
    ('image_https', 'file:///source/../private/'), ('image_https', 'https://example.org/image?token=secret'),
    ('voxel_offset_xyz', [1, 0, 0]), ('atom_policy', 'silently_relabel')])
def test_invalid_input_rejected_without_creating_target(tmp_path, field, value):
    meta = manifest();meta[field] = value
    path = tmp_path / 'manifest.json';path.write_text(json.dumps(meta))
    target = tmp_path / 'output'
    with pytest.raises(ValueError): creator.create(path, target)
    assert not target.exists()


def test_existing_target_and_port_conflict_preserve_files(tmp_path):
    target = tmp_path / 'existing';target.mkdir();saved = target / 'saved-edit';saved.write_text('keep')
    path = tmp_path / 'manifest.json';path.write_text(json.dumps(manifest()))
    with pytest.raises(ValueError, match='already exists'): creator.create(path, target)
    assert saved.read_text() == 'keep'
    source = tmp_path / 'input';(source / 'image').mkdir(parents=True);(source / 'segmentation').mkdir()
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0));meta = manifest(server.getsockname()[1]);path.write_text(json.dumps(meta))
        with pytest.raises(ValueError, match='already in use'): creator.create(path, tmp_path / 'new', source)
    assert not (tmp_path / 'new').exists()


def test_independent_projects_readonly_inputs_and_private_secrets(tmp_path, monkeypatch):
    # Real generator, templates and config; only the already-built viewer is a
    # fixture because this check does not install or run browser dependencies.
    # Docker capacity is read-only I/O, intercepted to keep the test service-free.
    import shutil
    repo = tmp_path / 'repo';repo.mkdir()
    for name in ('config', 'scripts', 'templates', 'docker'):
        shutil.copytree(ROOT / name, repo / name, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('compose.yml', 'images.lock.json', 'upstream.lock.json'):
        shutil.copy(ROOT / name, repo / name)
    viewer = repo / '.runtime/viewer';viewer.mkdir(parents=True);(viewer / 'index.html').write_text('viewer')
    monkeypatch.setattr(creator, 'ROOT', repo)
    monkeypatch.setattr(creator, 'available_cpus', lambda: 4)
    source = tmp_path / 'input';(source / 'image').mkdir(parents=True);(source / 'segmentation').mkdir()
    deployments = []
    for index in range(2):
        path = tmp_path / f'manifest{index}.json';path.write_text(json.dumps(manifest(18881 + index)))
        dest = tmp_path / f'install{index}';creator.create(path, dest, source)
        spec = json.loads((dest / 'deployment.json').read_text())
        config = yaml.safe_load((dest / 'compose.yml').read_text())
        assert config['name'] == spec['project_name']
        assert spec['bigtable_project'] == spec['project_name']
        assert (dest / '.env').stat().st_mode & 0o777 == 0o600
        assert (dest / 'secrets/cave-secret.json').stat().st_mode & 0o777 == 0o600
        for service in ('pcg', 'gateway'):
            mounts = [v for v in config['services'][service]['volumes'] if isinstance(v, dict) and v.get('target') == '/source']
            assert len(mounts) == 1 and mounts[0]['read_only']
        deployments.append(spec['project_name'])
    assert deployments[0] != deployments[1]
