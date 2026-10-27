"""Paint fixture topology and public CLI routing without operating deployments."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import cli
import make_fixture


@pytest.mark.parametrize('variant', ['zyx', 'xyz'])
def test_paint_geometry_preserves_old_targets_and_has_the_intended_contacts(variant):
    from scipy.ndimage import label
    original = make_fixture.fixture_arrays(variant)[0]
    make_fixture.add_refinement_geometry(original)
    painted, geometry = make_fixture.add_paint_geometry(original)
    assert np.array_equal(painted[tuple(slice(n) for n in original.shape)], original)
    assert painted.shape[0] == 64
    for name in ('neck', 'multi', 'seam', 'bypass', 'distant_contact', 'fake_edge'):
        record = geometry[name]
        for point in record['sources_vox'] + record['sinks_vox']:
            assert int(painted[tuple(point[::-1])]) in record['labels']
    for name in ('neck', 'multi', 'seam', 'bypass', 'distant_contact'):
        mask = np.isin(painted, np.asarray(geometry[name]['labels'], np.uint64))
        assert label(mask)[1] == 1
    neck = painted == 1001
    neck[:, :, 7:9] = False
    assert label(neck)[1] == 2
    assert label(np.isin(painted, [1501, 1502]))[1] == 2
    assert label(np.isin(painted, [1503, 1504]))[1] == 2
    fake = geometry['fake_edge']
    for point, fragment in zip(fake['near_sources_vox'] + fake['near_sinks_vox'], fake['near_labels']):
        assert int(painted[tuple(point[::-1])]) == fragment
    contacts = (painted[:, :-1] == 1402) & (painted[:, 1:] == 1401)
    positions = np.argwhere(contacts)
    assert len(positions) > 0 and np.all(positions[:, 1] == 11)
    assert not np.any((painted[:, :, :-1] == 1401) & (painted[:, :, 1:] == 1402))


def test_generated_paint_manifest_and_expected_geometry_are_self_contained(tmp_path):
    from cloudvolume import CloudVolume
    from create_segmentation_deployment import validate_manifest
    directory = tmp_path / 'paint-input'
    manifest = make_fixture.create(directory, paint=True)
    validate_manifest(manifest)
    assert manifest['atom_policy'] == 'agglomerated_supervoxels'
    assert manifest['supervoxel_split']['enabled']
    assert manifest['supervoxel_split']['paint']['enabled']
    expected = json.loads((directory / 'expected.json').read_text())
    geometry = expected['paint_geometry']
    with np.load(directory / 'agglomeration.npz') as data:
        lookup = dict(zip(map(int, data['fragment']), map(int, data['segment'])))
        for group in geometry['segment_groups']:
            assert len({lookup[fragment] for fragment in group}) == 1
            assert lookup[group[0]] > 0
        assert lookup[1501] == lookup[1502] == 0
        assert lookup[1503] == lookup[1504] == 0
    volume = CloudVolume('file://' + str(directory / 'segmentation'))
    assert list(volume.volume_size) == manifest['shape_xyz']


def invoke(monkeypatch, *arguments):
    monkeypatch.setattr(sys, 'argv', ['cave', *map(str, arguments)])
    cli.main()


def test_fixture_paint_cli_forwarding(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, 'run', lambda args, **kw: calls.append(list(map(str, args))))
    invoke(monkeypatch, 'fixture', tmp_path / 'input', '--paint')
    assert len(calls) == 1 and calls[0][-1] == '--paint'


def test_paint_repair_requires_explicit_id_and_stays_on_its_deployment(monkeypatch, tmp_path):
    path = tmp_path / 'deployment'
    path.mkdir()
    meta = {'project_name': 'cave-pkg-paint-cli', 'graph_id': 'paint_cli',
            'supervoxel_split': {'enabled': False}}
    (path / 'deployment.json').write_text(json.dumps(meta))
    (path / 'compose.yml').write_text(yaml.safe_dump({'name': meta['project_name']}))
    pending = path / 'data/supervoxel_splits/paint/pending/request.json'
    pending.parent.mkdir(parents=True)
    pending.write_text('{"operation_id": 14, "handed_to_write": true}')
    calls = []
    monkeypatch.setattr(cli, 'run', lambda args, **kw: calls.append(list(map(str, args))))
    invoke(monkeypatch, 'svsplit', 'repair', path)
    assert calls[-1] == ['docker', 'compose', '--env-file', str(path / '.env'),
        '-f', str(path / 'compose.yml'), 'exec', '-T', 'pcg', 'python',
        '/workspace/scripts/supervoxel_paint_history.py', '--repair', '--graph-id', 'paint_cli']
    invoke(monkeypatch, 'svsplit', 'repair', path, '--pcg-repair', '14')
    assert calls[-1][-2:] == ['--pcg-repair', '14']
    before = len(calls)
    with pytest.raises(ValueError, match='positive'):
        invoke(monkeypatch, 'svsplit', 'repair', path, '--pcg-repair', '0')
    assert len(calls) == before
