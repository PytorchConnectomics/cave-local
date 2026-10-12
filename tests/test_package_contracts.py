"""Check discovery, reference integrity and truthful suite evidence."""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_package as package
import test_suite as suite


@pytest.fixture
def minimal(tmp_path):
    skill = tmp_path / 'skills/cave-local'; skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: cave-local\ndescription: Route local CAVE work.\n---\nRead [guide](../../README.md).\n')
    (tmp_path / 'README.md').write_text('# Example\nRead [skill](skills/cave-local/SKILL.md).\n')
    return tmp_path


def test_discoverable_package_with_bidirectional_references(minimal):
    assert package.check(minimal)['passed']


@pytest.mark.parametrize('metadata', ['name: 7\ndescription: test',
                                    'name: different-name\ndescription: test',
                                    'name: cave-local\ndescription: ""',
                                    '- cave-local', 'name: [unclosed'])
def test_invalid_skill_metadata_is_a_reported_package_failure(minimal, metadata):
    (minimal / 'skills/cave-local/SKILL.md').write_text('---\n' + metadata + '\n---\n')
    with pytest.raises(ValueError):
        package.check(minimal)


def test_missing_local_reference_rejected_while_external_links_are_allowed(minimal):
    (minimal / 'README.md').write_text('[Docs](https://example.org/docs)\n[Missing](docs/absent.md)\n')
    with pytest.raises(ValueError, match='missing local reference docs/absent.md'):
        package.check(minimal)


def test_reference_cannot_escape_package_even_through_symlink(minimal, tmp_path_factory):
    external = tmp_path_factory.mktemp('external') / 'outside.md'; external.write_text('private')
    (minimal / 'outside.md').symlink_to(external)
    (minimal / 'README.md').write_text('[Outside](outside.md)\n')
    with pytest.raises(ValueError, match='missing local reference'):
        package.check(minimal)


def test_example_code_links_do_not_break_documentation_validation(minimal):
    (minimal / 'README.md').write_text('```markdown\n[Example](not-a-real-file.md)\n```\n[Skill](skills/cave-local/SKILL.md#installation)\n')
    assert package.check(minimal)['passed']


def test_suite_command_failure_retains_log_and_cannot_mark_success(tmp_path):
    evidence = suite.Evidence(tmp_path / 'evidence', 'fast')
    with pytest.raises(RuntimeError, match='failed'):
        evidence.run('deliberate-failure', [sys.executable, '-c', "print('failure evidence'); raise SystemExit(17)"])
    report = json.loads((evidence.directory / 'suite-report.json').read_text())
    assert report['passed'] is False
    assert report['steps'][0]['exit_code'] == 17
    assert 'failure evidence' in (evidence.directory / report['steps'][0]['log']).read_text()
    assert evidence.directory.stat().st_mode & 0o777 == 0o700


def test_suite_rejects_reused_output_without_overwriting_earlier_evidence(tmp_path):
    target = tmp_path / 'existing'; target.mkdir(); prior = target / 'suite-report.json'; prior.write_text('retained failure')
    with pytest.raises(ValueError, match='new output'):
        suite.Evidence(target, 'full')
    assert prior.read_text() == 'retained failure'


def test_full_suite_requires_exact_revision_before_any_docker_inspection(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError('Invalid revision must be rejected before inspecting Docker')
    monkeypatch.setattr(suite, 'containers', forbidden)
    assert suite.main(['full', '--output', str(tmp_path / 'evidence'), '--commit', 'main']) == 1
    report = json.loads((tmp_path / 'evidence/suite-report.json').read_text())
    assert report['passed'] is False
    assert 'exact 40-character' in report['failure']


def test_collected_logs_redact_fixture_credentials_and_exclude_backups(tmp_path):
    source = tmp_path / 'full'; source.mkdir()
    (source / 'suite-report.json').write_text(json.dumps({'mode': 'full', 'passed': False}))
    (source / 'logs').mkdir()
    log = source / 'logs/failed.log'; log.write_text('Password sensitive-value, session other-secret; retained failure')
    runtime = source / 'zyx-checkout/.runtime/deployment'; runtime.mkdir(parents=True)
    (runtime / '.env').write_text('POSTGRES_PASSWORD=sensitive-value\nCAVE_SESSION_SECRET=other-secret\n')
    private = source / 'zyx-checkout/.runtime/suite-backup'; private.mkdir()
    (private / 'workspace.tar.gz').write_bytes(b'private backup')
    target = tmp_path / 'collected'
    suite.collect_evidence(source, target)
    actual = (target / 'logs/failed.log').read_text()
    assert 'sensitive-value' not in actual and 'other-secret' not in actual
    assert '[REDACTED]' in actual and 'retained failure' in actual
    assert 'sensitive-value' in log.read_text()
    assert not list(target.rglob('.env')) and not list(target.rglob('*.tar.gz'))


def test_protected_inventory_detects_restarts_and_mount_changes_but_not_mount_order(monkeypatch):
    original = {'Id': 'identity', 'Name': '/original', 'Image': 'image', 'RestartCount': 0,
                'State': {'Running': True, 'StartedAt': '2026-10-12T00:00:00Z'},
                'Config': {'Labels': {'com.docker.compose.project': 'original',
                                      'com.docker.compose.service': 'postgres'}},
                'Mounts': [{'Destination': '/data', 'Source': '/original-data', 'RW': True},
                           {'Destination': '/config', 'Source': '/original-config', 'RW': False}],
                'HostConfig': {'PortBindings': {'80/tcp': [{'HostPort': '8088', 'HostIp': '127.0.0.1'}]}}}
    current = copy.deepcopy(original)
    def inspect(command, text=False):
        return 'identity\n' if command[1] == 'ps' else json.dumps([current])
    monkeypatch.setattr(suite.subprocess, 'check_output', inspect)
    before = suite.containers()
    current['Mounts'].reverse()
    assert suite.containers() == before
    current['State']['StartedAt'] = '2026-10-12T01:00:00Z'
    assert suite.containers() != before
    current = copy.deepcopy(original)
    current['Mounts'][0]['Source'] = '/different-data'
    assert suite.containers() != before
    current = copy.deepcopy(original)
    current['HostConfig']['PortBindings']['80/tcp'][0]['HostPort'] = '8089'
    assert suite.containers() != before


def test_fast_revision_assertion_cannot_mislabel_the_observed_checkout(tmp_path, monkeypatch):
    observed = 'a' * 40
    monkeypatch.setattr(suite.subprocess, 'check_output', lambda command, **kwargs: observed + '\n' if command[1] == 'rev-parse' else ' M scripts/test_suite.py\n')
    monkeypatch.setattr(suite, 'fast', lambda *args: (_ for _ in ()).throw(AssertionError('Mismatched revision must not run tests')))
    target = tmp_path / 'evidence'
    assert suite.main(['fast', '--commit', 'b' * 40, '--output', str(target)]) == 1
    report = json.loads((target / 'suite-report.json').read_text())
    assert report['passed'] is False
    assert report['source_commit'] == observed
    assert report['source_worktree_dirty'] is True
    assert report['revision_assertion'] == 'b' * 40


@pytest.fixture
def simulated_suite(tmp_path, monkeypatch):
    observed = {'fresh': [], 'lifecycle': []}
    controls = {'fresh_passed': True, 'failed_lifecycle_gate': None, 'skip_upgrade': False}
    monkeypatch.setattr(suite, 'containers', lambda *args: {})
    monkeypatch.setattr(suite, 'persistent_resources', lambda *args: {'volume': [], 'network': []})
    def command(evidence, name, arguments, **kwargs):
        assert name.endswith('-fresh-install')
        repo = Path(arguments[4])
        (repo / '.runtime').mkdir(parents=True)
        variant = arguments[arguments.index('--variant') + 1]
        policy = arguments[arguments.index('--atom-policy') + 1]
        observed['fresh'].append((variant, policy))
        (repo / '.runtime/fresh-install-report.json').write_text(json.dumps({
            'passed': controls['fresh_passed'], 'source_commit': arguments[3]}))
    def lifecycle(evidence, label, repo, revision, environment, upgrade_backup=None):
        observed['lifecycle'].append((label, upgrade_backup))
        result = {key: True for key in suite.LIFECYCLE_GATES}
        if controls['failed_lifecycle_gate']:
            result[controls['failed_lifecycle_gate']] = False
        result['cross_version_upgrade_tested'] = bool(upgrade_backup) and not controls['skip_upgrade']
        return result
    monkeypatch.setattr(suite.Evidence, 'run', command)
    monkeypatch.setattr(suite, 'lifecycle', lifecycle)
    monkeypatch.setattr(suite, 'retain_diagnostics', lambda *args: None)
    return observed, controls


@pytest.mark.parametrize('variant,expected', [('both', [('zyx', 'source_labels'), ('xyz', 'spatial_tiles')]),
                                            ('zyx', [('zyx', 'source_labels')]),
                                            ('xyz', [('xyz', 'spatial_tiles')])])
def test_full_selection_runs_and_reports_only_requested_scenarios(tmp_path, simulated_suite, variant, expected):
    observed, _ = simulated_suite
    output = tmp_path / 'evidence'
    arguments = ['full', '--commit', 'a' * 40, '--output', str(output)]
    if variant != 'both':
        arguments += ['--variant', variant]
    assert suite.main(arguments) == 0
    report = json.loads((output / 'suite-report.json').read_text())
    assert report['passed'] is True
    assert observed['fresh'] == expected
    assert report['requested_scenarios'] == [{'variant': value, 'atom_policy': policy} for value, policy in expected]
    assert [(item['variant'], item['atom_policy']) for item in report['scenarios']] == expected


@pytest.mark.parametrize('gate', suite.LIFECYCLE_GATES)
def test_incomplete_lifecycle_prevents_overall_success_and_next_scenario(tmp_path, simulated_suite, gate):
    observed, controls = simulated_suite
    controls['failed_lifecycle_gate'] = gate
    output = tmp_path / 'evidence'
    assert suite.main(['full', '--commit', 'a' * 40, '--output', str(output)]) == 1
    report = json.loads((output / 'suite-report.json').read_text())
    assert report['passed'] is False and gate in report['failure']
    assert observed['fresh'] == [('zyx', 'source_labels')]


def test_failed_fresh_install_evidence_prevents_lifecycle_and_success(tmp_path, simulated_suite):
    observed, controls = simulated_suite
    controls['fresh_passed'] = False
    output = tmp_path / 'evidence'
    assert suite.main(['full', '--commit', 'a' * 40, '--output', str(output), '--variant', 'xyz']) == 1
    report = json.loads((output / 'suite-report.json').read_text())
    assert report['passed'] is False and 'successful exact-revision evidence' in report['failure']
    assert observed['lifecycle'] == []


@pytest.mark.parametrize('variant', ['both', 'xyz'])
def test_upgrade_gate_runs_on_first_selected_scenario_including_xyz(tmp_path, simulated_suite, variant):
    observed, _ = simulated_suite
    backup = tmp_path / 'older-backup'; backup.mkdir()
    (backup / 'deployment.json').write_text(json.dumps({'test_fixture': True, 'graph_id': 'pkg_old'}))
    (backup / 'manifest.json').write_text(json.dumps({'observations': {'state': 'saved'}, 'source_included': True, 'repo_commit': 'b' * 40}))
    output = tmp_path / 'evidence'
    assert suite.main(['full', '--commit', 'a' * 40, '--output', str(output), '--variant', variant,
                       '--upgrade-backup', str(backup)]) == 0
    report = json.loads((output / 'suite-report.json').read_text())
    assert report['cross_version_upgrade_tested'] is True
    assert observed['lifecycle'][0][1] == backup
    assert all(value is None for _, value in observed['lifecycle'][1:])


def test_requested_upgrade_cannot_be_reported_as_passed_when_skipped(tmp_path, simulated_suite):
    _, controls = simulated_suite
    controls['skip_upgrade'] = True
    backup = tmp_path / 'older-backup'; backup.mkdir()
    (backup / 'deployment.json').write_text(json.dumps({'test_fixture': True, 'graph_id': 'pkg_old'}))
    (backup / 'manifest.json').write_text(json.dumps({'observations': {'state': 'saved'}, 'source_included': True, 'repo_commit': 'b' * 40}))
    output = tmp_path / 'evidence'
    assert suite.main(['full', '--commit', 'a' * 40, '--output', str(output), '--variant', 'xyz',
                       '--upgrade-backup', str(backup)]) == 1
    report = json.loads((output / 'suite-report.json').read_text())
    assert report['passed'] is False and 'update gate did not run' in report['failure']
