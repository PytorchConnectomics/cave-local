"""Exercise browser and handoff boundaries without submitting graph edits."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import browser_handoff as handoff
import check_browser as readiness
import verify_iteration_browser as browser


@pytest.fixture
def assigned(tmp_path):
    spec = {'graph_id': 'pkg_contract', 'project_name': 'cave-pkg-contract', 'test_fixture': True,
            'local_url': 'http://127.0.0.1:18182', 'shape_xyz': [48, 40, 20],
            'resolution_nm_xyz': [8, 8, 24], 'voxel_offset_xyz': [0, 0, 0]}
    scenario = {'atoms': ['72057594037927937', '72057594037927938'],
                'positions_xyz': [[4, 5, 2], [9, 5, 2]], 'initial_relation': 'separate',
                'view_position_xyz': [6, 5, 2], 'current_roots': ['288230376151711900', '288230376151711901']}
    (tmp_path / 'data').mkdir()
    (tmp_path / 'deployment.json').write_text(json.dumps(spec))
    (tmp_path / 'compose.yml').write_text('name: cave-pkg-contract\n')
    (tmp_path / 'data/scenario.json').write_text(json.dumps(scenario))
    return tmp_path, spec, scenario


@pytest.mark.parametrize('url', [
    'https://127.0.0.1:18182', 'http://0.0.0.0:18182', 'http://localhost.attacker.test:18182',
    'http://127.0.0.1:18182/another-deployment', 'http://192.0.2.1:18182'])
def test_edit_browser_rejects_remote_or_relocated_target(assigned, url):
    dest, spec, scenario = assigned
    spec['local_url'] = url
    with pytest.raises(ValueError, match='loopback'):
        browser.validate_target(dest, spec, scenario)


@pytest.mark.parametrize('scenario_field,value', [('atoms', ['1']), ('positions_xyz', [[1, 2, 3]]),
                                                   ('initial_relation', 'unknown')])
def test_browser_refuses_incomplete_or_ambiguous_assignment(assigned, scenario_field, value):
    dest, spec, scenario = assigned
    scenario[scenario_field] = value
    with pytest.raises(ValueError):
        browser.validate_target(dest, spec, scenario)


@pytest.mark.parametrize('path', ['/segmentation/api/v1/table/pkg_contract_other/merge',
                                 '/meshing/api/v1/table/another_graph/manifest/1',
                                 '/segmentation/table/real_data/info'])
def test_graph_request_guard_distinguishes_exact_graph_names(assigned, path):
    _, spec, _ = assigned
    assert not browser.request_allowed(spec['local_url'] + path, spec['local_url'], spec['graph_id'])
    assert browser.request_allowed(spec['local_url'] + '/segmentation/api/v1/table/pkg_contract/merge',
                                   spec['local_url'], spec['graph_id'])


def test_handoff_refreshes_uint64_roots_and_keeps_atom_ids_as_strings(assigned, monkeypatch):
    dest, spec, scenario = assigned
    roots = np.array([288230376151712010, 288230376151712121], dtype=np.uint64)
    observed = []
    def read_roots(actual_spec, atoms):
        observed.append((actual_spec['graph_id'], atoms.tolist()))
        return roots
    monkeypatch.setattr(handoff, 'api_roots', read_roots)
    path = handoff.write(dest)
    actual = json.loads((dest / 'data/scenario.json').read_text())
    assert actual['current_roots'] == [str(int(root)) for root in roots]
    assert actual['atoms'] == scenario['atoms']
    assert observed == [(spec['graph_id'], list(map(int, scenario['atoms'])))]
    text = path.read_text()
    assert "'node_ids': ['72057594037927937', '72057594037927938']" in text
    assert '/table/pkg_contract/roots?int64_as_str=true' in text
    viewer = json.loads((dest / 'web/viewer-state.json').read_text())
    assert viewer['layers'][1]['segments'] == actual['current_roots']


def test_mid_edit_handoff_does_not_overwrite_previous_instructions(assigned, monkeypatch):
    dest, _, _ = assigned
    (dest / 'web').mkdir()
    output = dest / 'web/agent-browser-test.txt'; output.write_text('previous instructions')
    scenario_before = (dest / 'data/scenario.json').read_bytes()
    monkeypatch.setattr(handoff, 'api_roots', lambda *args: np.array([9, 9], dtype=np.uint64))
    with pytest.raises(ValueError, match='mid-edit'):
        handoff.write(dest)
    assert output.read_text() == 'previous instructions'
    assert (dest / 'data/scenario.json').read_bytes() == scenario_before


def test_real_data_handoff_rejected_before_network_lookup(assigned, monkeypatch):
    dest, spec, _ = assigned
    spec['test_fixture'] = False
    (dest / 'deployment.json').write_text(json.dumps(spec))
    def forbidden(*args):
        raise AssertionError('A real deployment must be rejected before a graph request')
    monkeypatch.setattr(handoff, 'api_roots', forbidden)
    with pytest.raises(ValueError, match='Edit acceptance'):
        handoff.write(dest)
    assert not (dest / 'web/agent-browser-test.txt').exists()


@pytest.fixture
def executables(tmp_path, monkeypatch):
    monkeypatch.delenv('CAVE_CHROMIUM_EXECUTABLE', raising=False)
    cache = tmp_path / 'cached-chromium'; cache.write_text('browser'); cache.chmod(0o700)
    system = tmp_path / 'system-chromium'; system.write_text('browser'); system.chmod(0o700)
    return cache, system


def test_explicit_browser_failure_does_not_silently_use_cache_or_system(executables, monkeypatch):
    cache, system = executables
    monkeypatch.setattr(readiness.shutil, 'which', lambda name: str(system) if name == 'chromium' else None)
    with pytest.raises(ValueError, match='supplied Chromium executable'):
        readiness.choose_executable(str(cache), '/missing/explicit-browser')


def test_browser_environment_override_precedes_cache(executables, monkeypatch):
    cache, system = executables
    monkeypatch.setenv('CAVE_CHROMIUM_EXECUTABLE', str(system))
    assert readiness.choose_executable(str(cache)) == (str(system), 'supplied executable')


def test_browser_cache_precedes_system_and_missing_cache_uses_system(executables, monkeypatch):
    cache, system = executables
    monkeypatch.setattr(readiness.shutil, 'which', lambda name: str(system) if name == 'chromium' else None)
    assert readiness.choose_executable(str(cache)) == (str(cache), 'shared Playwright browser cache')
    cache.unlink()
    assert readiness.choose_executable(str(cache)) == (str(system), 'installed system browser')


def test_launcher_symlink_is_not_replaced_by_its_target(executables, monkeypatch):
    cache, system = executables
    launcher = system.with_name('chromium-launcher'); launcher.symlink_to(system)
    monkeypatch.setattr(readiness.shutil, 'which', lambda name: None)
    selected, _ = readiness.choose_executable(str(cache), str(launcher))
    assert selected == str(launcher)
    assert Path(selected).is_symlink()


def test_non_executable_explicit_browser_rejected(executables, monkeypatch):
    cache, system = executables
    system.chmod(0o600)
    monkeypatch.setattr(readiness.shutil, 'which', lambda name: None)
    with pytest.raises(ValueError, match='not executable'):
        readiness.choose_executable(str(cache), str(system))
