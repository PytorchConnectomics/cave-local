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


@pytest.mark.parametrize('block', [True, {'enabled': 1}, {'enabled': True},
    {'enabled': True, 'grid_xyz': [4, 4, 3]}, {'enabled': True, 'grid_xyz': [True, 4, 4]},
    {'enabled': True, 'grid_xyz': [4, 4, 4], 'max_atoms': 0},
    {'enabled': True, 'grid_xyz': [4, 4, 4], 'max_seconds': float('inf')},
    {'enabled': True, 'grid_xyz': [4, 4, 4], 'unexpected': 1}])
def test_refinement_config_rejects_invalid_limits_and_geometry(block):
    meta = manifest()
    meta.update(atom_policy='agglomerated_supervoxels', agglomeration_https='file:///source/agglomeration.npz',
                supervoxel_split=block)
    with pytest.raises(ValueError, match='supervoxel_split|Unknown'):
        creator.validate_manifest(meta)


def test_refinement_is_operational_configuration_not_data_identity():
    from ingest_segmentation_iteration import data_spec
    meta = manifest()
    meta.update(atom_policy='agglomerated_supervoxels', agglomeration_https='file:///source/agglomeration.npz')
    before = data_spec(meta)
    meta['supervoxel_split'] = {'enabled': True, 'grid_xyz': [4, 4, 4], 'max_atoms': 2, 'max_seconds': .5}
    creator.validate_manifest(meta)
    assert data_spec(meta) == before
    meta['atom_policy'] = 'source_labels'
    with pytest.raises(ValueError, match='requires agglomerated_supervoxels'):
        creator.validate_manifest(meta)


def paint_manifest(paint):
    meta = manifest()
    meta.update(atom_policy='agglomerated_supervoxels',
                agglomeration_https='file:///source/agglomeration.npz',
                supervoxel_split={'enabled': True, 'grid_xyz': [4, 4, 4], 'paint': paint})
    return meta


@pytest.mark.parametrize('paint', [True, [], {'enabled': 1}, {'unknown': 3},
    {'max_radius_nm': 0}, {'max_cut_distance_nm': float('nan')}, {'max_seconds': float('inf')},
    {'max_box_voxels': [4, 4]}, {'max_box_voxels': [4, True, 4]},
    {'max_box_voxels': [4, 0, 4]}, {'max_box_voxels': '512,512,128'}])
def test_invalid_paint_configuration_is_rejected(paint):
    with pytest.raises(ValueError, match='supervoxel_split.paint'):
        creator.validate_manifest(paint_manifest(paint))


@pytest.mark.parametrize('name', ['max_stroke_points', 'max_paint_voxels', 'max_painted_atoms',
    'max_removed_edges', 'max_contact_voxels', 'max_request_bytes', 'max_strokes',
    'max_total_points', 'max_raster_work'])
@pytest.mark.parametrize('value', [0, -1, True, 3.5])
def test_paint_integer_limits_reject_invalid_values(name, value):
    with pytest.raises(ValueError, match=name):
        creator.validate_manifest(paint_manifest({name: value}))


def test_paint_defaults_and_operational_identity():
    expected = {'enabled': False, 'max_radius_nm': 500, 'max_stroke_points': 256,
        'max_paint_voxels': 200000, 'max_box_voxels': [512, 512, 128],
        'max_cut_distance_nm': 1000, 'max_painted_atoms': 256, 'max_removed_edges': 2000,
        'max_contact_voxels': 2000000, 'max_seconds': 60, 'max_request_bytes': 262144,
        'max_strokes': 128, 'max_total_points': 2048, 'max_raster_work': 4000000}
    assert creator.validate_paint_config({}) == expected
    from ingest_segmentation_iteration import data_spec
    meta = paint_manifest({})
    before = data_spec(meta)
    meta['supervoxel_split']['paint'] = {'enabled': True, 'max_radius_nm': 15.5}
    creator.validate_manifest(meta)
    assert data_spec(meta) == before
    # Both gates are runtime conditions; retaining paint config while disabling
    # new refinement must remain a supported recovery configuration.
    meta['supervoxel_split']['enabled'] = False
    creator.validate_manifest(meta)


def test_create_paint_switch_requires_an_explicit_fixture():
    meta = paint_manifest({})
    with pytest.raises(ValueError, match='generated'):
        creator.enable_fixture_paint(meta)
    meta['test_fixture'] = True
    creator.enable_fixture_paint(meta)
    assert meta['supervoxel_split']['enabled']
    assert meta['supervoxel_split']['paint']['enabled']
    assert meta['supervoxel_split']['paint']['max_raster_work'] == 4000000
    meta['atom_policy'] = 'source_labels'
    with pytest.raises(ValueError, match='agglomerated'):
        creator.enable_fixture_paint(meta)
