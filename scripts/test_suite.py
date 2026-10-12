"""Run service-free checks or sequential disposable CAVE lifecycle acceptance.

Full acceptance requires an exact Git commit, new directories, Docker access
and a working Chromium. Failures retain their installations and evidence.
"""
import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import shutil
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE_GATES = ('backup_state_verified', 'restore_state_equal', 'candidate_adapters_verified',
                   'restored_browser_passed', 'credential_rotation_state_equal',
                   'persistent_store_containers_preserved', 'scoped_teardown_verified',
                   'source_and_backup_unchanged')


class Evidence:
    def __init__(self, directory, mode, revision=None):
        self.directory = Path(directory).resolve()
        if self.directory.exists():
            raise ValueError('Test evidence requires a new output directory')
        self.directory.mkdir(parents=True, mode=0o700)
        (self.directory / 'logs').mkdir(mode=0o700)
        self.report = {'mode': mode, 'source_commit': revision, 'passed': False,
                       'steps': [], 'scenarios': []}
        if mode == 'full':
            self.report['shared_host_state'] = ['OS', 'Docker daemon', 'Python/Node/Git',
                                                 'package/download caches', 'existing or installed Chromium',
                                                 'Docker image layers unless --no-cache']
        self.write()

    def write(self):
        path = self.directory / 'suite-report.json'
        path.write_text(json.dumps(self.report, indent=2) + '\n')
        path.chmod(0o600)

    def run(self, name, command, cwd=ROOT, env=None):
        began = time.monotonic()
        log = self.directory / 'logs' / (name + '.log')
        print('Test:', name, flush=True)
        with log.open('w') as output:
            log.chmod(0o600)
            result = subprocess.run(list(map(str, command)), cwd=cwd, env=env,
                                    stdout=output, stderr=subprocess.STDOUT)
        self.report['steps'].append({'name': name, 'exit_code': result.returncode,
                                     'seconds': round(time.monotonic() - began, 2),
                                     'log': str(log.relative_to(self.directory))})
        self.write()
        if result.returncode:
            raise RuntimeError(f'{name} failed; retained log: {log}')

    def complete(self):
        self.report['passed'] = True
        self.write()


def fingerprint(directory):
    result = {}
    for path in sorted(Path(directory).rglob('*')):
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
            result[path.relative_to(directory).as_posix()] = digest.hexdigest()
    return result


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def containers(project=None):
    command = ['docker', 'ps', '-aq']
    if project:
        command += ['--filter', 'label=com.docker.compose.project=' + project]
    ids = subprocess.check_output(command, text=True).split()
    if not ids:
        return {}
    inspected = json.loads(subprocess.check_output(['docker', 'inspect', *ids]))
    return {item['Id']: {'name': item['Name'], 'image': item['Image'],
                        'running': item['State']['Running'], 'restarts': item['RestartCount'],
                        'started_at': item['State'].get('StartedAt'),
                        'mounts': sorted(item.get('Mounts', []), key=lambda mount: mount.get('Destination', '')),
                        'port_bindings': item.get('HostConfig', {}).get('PortBindings', {}),
                        'project': (item['Config'].get('Labels') or {}).get('com.docker.compose.project'),
                        'service': (item['Config'].get('Labels') or {}).get('com.docker.compose.service')}
            for item in inspected}


def persistent_resources(project=None):
    result = {}
    for kind in ('volume', 'network'):
        command = ['docker', kind, 'ls', '--format', '{{.Name}}']
        if project:
            command += ['--filter', 'label=com.docker.compose.project=' + project]
        result[kind] = sorted(subprocess.check_output(command, text=True).splitlines())
    return result


def expect_same(before, after, label):
    if before != after:
        raise RuntimeError(label + ' changed; preserve evidence and inspect before retrying')


def fast(evidence, python):
    evidence.report['categories'] = ['Python syntax', 'skill discovery and references',
                                   'data geometry and label semantics', 'CLI/deployment isolation',
                                   'native browser and handoff boundaries', 'backup/restore/rotation failures',
                                   'release/export contracts', 'test evidence integrity']
    evidence.write()
    evidence.run('python-syntax', [python, '-m', 'compileall', '-q', 'scripts', 'tests', 'config', 'templates'])
    evidence.run('package-contract', [python, 'scripts/check_package.py'])
    results = evidence.directory / 'unit-results.xml'
    try:
        evidence.run('unit-and-data-contracts', [python, '-m', 'pytest', '-q', 'tests', '--junitxml', results])
    finally:
        if results.is_file():
            evidence.report['unit_tests'] = unit_counts(results)
            evidence.write()


def unit_counts(path):
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    suites = root.findall('testsuite') if root.tag == 'testsuites' else [root]
    result = {key: sum(int(item.get(key, '0')) for item in suites)
              for key in ('tests', 'failures', 'errors', 'skipped')}
    result['count_basis'] = 'JUnit test cases, including subtests'
    return result


def snapshot(evidence, name, repo, deployment):
    path = evidence.directory / (name + '.json')
    evidence.run(name, [repo / '.venv/bin/python', repo / 'scripts/snapshot.py', deployment,
                        '--output', path], cwd=repo)
    path.chmod(0o600)
    return json.loads(path.read_text())


def lifecycle(evidence, label, repo, revision, env, upgrade_backup=None):
    cave = repo / 'bin/cave'
    deployment = repo / '.runtime/deployment'
    spec = json.loads((deployment / 'deployment.json').read_text())
    if spec.get('test_fixture') is not True or not spec['project_name'].startswith('cave-pkg-'):
        raise ValueError('Full suite lifecycle only operates its generated disposable fixture')
    source = Path(spec['source_dir'])
    source_before = fingerprint(source)
    before = snapshot(evidence, label + '-before-lifecycle', repo, deployment)
    backup = repo / '.runtime/suite-backup'
    evidence.run(label + '-backup', [cave, 'backup', deployment, backup, '--include-source'], cwd=repo, env=env)
    evidence.run(label + '-verify-backup', [cave, 'verify-backup', backup], cwd=repo, env=env)
    manifest = json.loads((backup / 'manifest.json').read_text())
    if manifest.get('observations') != before or not manifest.get('source_included'):
        raise RuntimeError('Online backup did not record the complete original state and source')
    backup_before = fingerprint(backup)
    restored = repo / '.runtime/suite-restored'
    evidence.run(label + '-restore', [cave, 'restore', backup, restored, '--port', free_port()], cwd=repo, env=env)
    restoration = json.loads((restored / 'restore-report.json').read_text())
    if not restoration['state_compared_to_backup'] or restoration['effective_runtime_repo_commit'] != revision:
        raise RuntimeError('Restore did not prove saved state and effective candidate revision')
    expect_same(before, snapshot(evidence, label + '-restored-state', repo, restored), 'Restored complete state')
    for name in ('sitecustomize.py', 'wsgi.py', 'celery_app.py', 'local_mesh.py', 'source_label_policy.py',
                 'database_connections.py', 'segmentation_metadata.py'):
        expect_same((repo / 'scripts' / name).read_bytes(), (restored / 'scripts' / name).read_bytes(),
                    'Effective restored adapter ' + name)
    expect_same((repo / 'scripts/ingest_segmentation_iteration.py').read_bytes(),
                (restored / 'scripts/ingest.py').read_bytes(), 'Effective restored ingestion')
    evidence.run(label + '-restored-browser', [repo / '.venv/bin/python', repo / 'scripts/verify_iteration_browser.py', restored], cwd=repo, env=env)
    expect_same(before, snapshot(evidence, label + '-original-after-restored-browser', repo, deployment),
                'Original state while editing its separate restored copy')
    previous_credentials = (deployment / '.env').read_bytes()
    previous_containers = containers(spec['project_name'])
    evidence.run(label + '-rotate', [cave, 'rotate-secrets', deployment], cwd=repo, env=env)
    rotation = json.loads((deployment / 'rotation-report.json').read_text())
    if not rotation.get('rotated') or not rotation.get('state_unchanged'):
        raise RuntimeError('Credential rotation did not verify success')
    if previous_credentials == (deployment / '.env').read_bytes() or (deployment / '.env').stat().st_mode & 0o777 != 0o600:
        raise RuntimeError('Credentials did not change in a private environment file')
    previous_values = dict(line.split('=', 1) for line in previous_credentials.decode().splitlines() if '=' in line)
    current_values = dict(line.split('=', 1) for line in (deployment / '.env').read_text().splitlines() if '=' in line)
    for key in ('POSTGRES_PASSWORD', 'CAVE_SESSION_SECRET'):
        if not current_values.get(key) or current_values[key] == previous_values.get(key):
            raise RuntimeError('Rotation did not independently change ' + key)
    current_containers = containers(spec['project_name'])
    previous_by_service = {item['service']: identity for identity, item in previous_containers.items()}
    current_by_service = {item['service']: identity for identity, item in current_containers.items()}
    applications = set(previous_by_service) - {'postgres', 'redis', 'bigtable'}
    if not applications or set(rotation['services_recreated']) != applications:
        raise RuntimeError('Rotation did not recreate every previously running application')
    for name in ('postgres', 'redis', 'bigtable'):
        if previous_by_service.get(name) != current_by_service.get(name) or name not in current_by_service:
            raise RuntimeError('Rotation replaced a persistent store container: ' + name)
    for name in rotation['services_recreated']:
        if name not in current_by_service or previous_by_service.get(name) == current_by_service[name]:
            raise RuntimeError('Rotation did not recreate application: ' + name)
    expect_same(before, snapshot(evidence, label + '-after-rotation', repo, deployment), 'Rotation complete state')
    restored_project = json.loads((restored / 'deployment.json').read_text())['project_name']
    restored_source = restored.with_name(restored.name + '-source')
    restored_source_before = fingerprint(restored_source)
    evidence.run(label + '-teardown-restored', [cave, 'teardown', restored, '--confirm', restored_project], cwd=repo, env=env)
    if containers(restored_project) or any(persistent_resources(restored_project).values()):
        raise RuntimeError('Teardown retained restored project containers, network or volumes')
    expect_same(restored_source_before, fingerprint(restored_source), 'Restored immutable source after teardown')
    expect_same(before, snapshot(evidence, label + '-original-after-copy-teardown', repo, deployment),
                'Original state after separate copy teardown')
    if upgrade_backup:
        upgraded = repo / '.runtime/suite-upgraded'
        evidence.run(label + '-upgrade-restore', [cave, 'restore', upgrade_backup, upgraded, '--port', free_port()], cwd=repo, env=env)
        upgraded_report = json.loads((upgraded / 'restore-report.json').read_text())
        if not upgraded_report.get('state_compared_to_backup') or upgraded_report['effective_runtime_repo_commit'] != revision:
            raise RuntimeError('Prior-version backup update did not verify effective candidate and state')
        if not upgraded_report.get('backup_repo_commit') or upgraded_report.get('backup_repo_commit') == revision:
            raise RuntimeError('Upgrade gate requires a backup from a different recorded revision')
        upgraded_project = json.loads((upgraded / 'deployment.json').read_text())['project_name']
        evidence.run(label + '-upgrade-browser', [repo / '.venv/bin/python', repo / 'scripts/verify_iteration_browser.py', upgraded], cwd=repo, env=env)
        evidence.run(label + '-teardown-upgraded', [cave, 'teardown', upgraded, '--confirm', upgraded_project], cwd=repo, env=env)
    evidence.run(label + '-browser-handoff', [cave, 'browser-handoff', deployment], cwd=repo, env=env)
    evidence.run(label + '-teardown-original', [cave, 'teardown', deployment, '--confirm', spec['project_name']], cwd=repo, env=env)
    if containers(spec['project_name']) or any(persistent_resources(spec['project_name']).values()):
        raise RuntimeError('Teardown retained original fixture containers, network or volumes')
    expect_same(source_before, fingerprint(source), 'Immutable fixture inputs')
    expect_same(backup_before, fingerprint(backup), 'Verified backup after scoped teardown')
    if not (deployment / 'deployment.json').is_file():
        raise RuntimeError('Teardown removed deployment files')
    return {'backup_state_verified': True, 'restore_state_equal': True, 'candidate_adapters_verified': True,
            'restored_browser_passed': True, 'credential_rotation_state_equal': True,
            'persistent_store_containers_preserved': True, 'scoped_teardown_verified': True,
            'source_and_backup_unchanged': True, 'cross_version_upgrade_tested': bool(upgrade_backup)}


def full(evidence, args):
    if not args.commit or len(args.commit) != 40 or any(c not in '0123456789abcdef' for c in args.commit):
        raise ValueError('Full acceptance requires an exact 40-character Git commit')
    environment = dict(os.environ)
    if args.chromium:
        executable = shutil.which(args.chromium) or os.path.abspath(args.chromium)
        if not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise ValueError('The supplied Chromium executable does not exist')
        environment['CAVE_CHROMIUM_EXECUTABLE'] = executable
    if args.upgrade_backup:
        prior_spec = json.loads((args.upgrade_backup / 'deployment.json').read_text())
        if prior_spec.get('test_fixture') is not True or not prior_spec.get('graph_id', '').startswith('pkg_'):
            raise ValueError('The optional upgrade backup must come from a generated disposable fixture')
        prior_manifest = json.loads((args.upgrade_backup / 'manifest.json').read_text())
        if not prior_manifest.get('observations') or not prior_manifest.get('source_included') or not prior_manifest.get('repo_commit'):
            raise ValueError('The optional upgrade backup requires verified online state, local sources and a recorded prior revision')
    protected = containers()
    protected_resources = persistent_resources()
    run_id = secrets.token_hex(4)
    cases = [('zyx', 'source_labels'), ('xyz', 'spatial_tiles')]
    if args.variant != 'both':
        cases = [case for case in cases if case[0] == args.variant]
    evidence.report['requested_scenarios'] = [{'variant': variant, 'atom_policy': policy}
                                             for variant, policy in cases]
    evidence.write()
    for index, (variant, policy) in enumerate(cases):
        label = variant + '-' + policy
        repo = evidence.directory / (variant + '-checkout')
        command = [args.python, ROOT / 'scripts/fresh_install.py', args.source, args.commit, repo,
                   '--branch', args.branch, '--python', args.python, '--port', free_port(),
                   '--graph-id', 'pkg_suite_' + run_id + '_' + variant, '--variant', variant,
                   '--atom-policy', policy]
        if args.no_cache:
            command += ['--no-cache']
        if args.chromium:
            command += ['--chromium', args.chromium]
        if args.install_browser:
            command += ['--install-browser']
        evidence.run(label + '-fresh-install', command, env=environment)
        fresh = json.loads((repo / '.runtime/fresh-install-report.json').read_text())
        if fresh.get('passed') is not True or fresh.get('source_commit') != args.commit:
            raise RuntimeError('Fresh installation lacks successful exact-revision evidence')
        result = lifecycle(evidence, label, repo, args.commit, environment,
                           args.upgrade_backup if index == 0 else None)
        missing = [key for key in LIFECYCLE_GATES if result.get(key) is not True]
        if missing:
            raise RuntimeError('Lifecycle acceptance gates did not pass: ' + ', '.join(missing))
        if args.upgrade_backup and index == 0 and result.get('cross_version_upgrade_tested') is not True:
            raise RuntimeError('The requested prior-version update gate did not run')
        scenario = {'variant': variant, 'atom_policy': policy, 'fresh_install_passed': True, **result}
        if (repo / '.runtime/unit-results.xml').is_file():
            scenario['unit_tests'] = unit_counts(repo / '.runtime/unit-results.xml')
        evidence.report['scenarios'].append(scenario)
        evidence.write()
        expect_same(protected, containers(), 'Pre-existing containers after fixture lifecycle')
        expect_same(protected_resources, persistent_resources(), 'Pre-existing volumes and networks after fixture lifecycle')
    evidence.report['pre_existing_containers_unchanged'] = len(protected)
    evidence.report['cross_version_upgrade_tested'] = any(item.get('cross_version_upgrade_tested') is True
                                                        for item in evidence.report['scenarios'])
    evidence.report['copybara_actual_migrations_tested'] = False
    evidence.report['categories'] = ['fresh exact-revision installations', 'unit/data/package contracts',
                                   'exhaustive voxel/topology/API/mesh checks', 'native Chromium merge/split/reload/fresh tab',
                                   'linked-table creator concurrency', 'database reconnection without replay',
                                   'real-edit restart persistence', 'idempotent repeat start',
                                   'online backup/integrity/isolated restore', 'credential rotation',
                                   'effective candidate adapters', 'scoped teardown and unrelated-resource preservation']


def retain_diagnostics(evidence):
    for repo in sorted(evidence.directory.glob('*-checkout')):
        for name in ('deployment', 'suite-restored', 'suite-upgraded'):
            deployment = repo / '.runtime' / name
            metadata = deployment / 'deployment.json'
            if not metadata.is_file() or not (deployment / 'compose.yml').is_file():
                continue
            try:
                spec = json.loads(metadata.read_text())
                if spec.get('test_fixture') is not True or not spec.get('project_name', '').startswith('cave-pkg-'):
                    continue
                for operation in ('status', 'logs'):
                    try:
                        evidence.run('failure-' + repo.name + '-' + name + '-' + operation,
                                     [repo / 'bin/cave', operation, deployment], cwd=repo)
                    except Exception:
                        pass
            except Exception:
                pass


def collect_evidence(source, destination):
    """Copy only fixture test evidence, redacting generated credential values."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('Collected evidence requires a new output directory')
    destination.mkdir(parents=True, mode=0o700)
    report_path = source / 'suite-report.json'
    if not report_path.is_file():
        (destination / 'suite-report.json').write_text(json.dumps({'passed': False, 'failure': 'Suite did not start'}) + '\n')
        return
    report = json.loads(report_path.read_text())
    if report.get('mode') != 'full':
        raise ValueError('Only generated full-suite fixture evidence can be collected')
    redactions = set()
    for repo in source.glob('*-checkout'):
        for environment in repo.rglob('*.env'):
            for line in environment.read_text().splitlines():
                key, separator, value = line.partition('=')
                if separator and key in ('POSTGRES_PASSWORD', 'CAVE_SESSION_SECRET') and value:
                    redactions.update((value, quote(value, safe='')))
    def copy_text(path):
        relative = path.relative_to(source)
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(errors='replace')
        for secret in sorted(redactions, key=len, reverse=True):
            text = text.replace(secret, '[REDACTED]')
        output.write_text(text)
    copy_text(report_path)
    for log in (source / 'logs').glob('*.log'):
        copy_text(log)
    for repo in source.glob('*-checkout'):
        report = repo / '.runtime/fresh-install-report.json'
        if report.is_file():
            copy_text(report)
        for log in (repo / '.runtime/logs').glob('*.log'):
            copy_text(log)
        for deployment in ('deployment', 'suite-restored', 'suite-upgraded'):
            browser = repo / '.runtime' / deployment / 'reports/browser'
            report = browser / 'acceptance-browser.json'
            if report.is_file():
                copy_text(report)
            for screenshot in browser.glob('*.png'):
                target = destination / screenshot.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(screenshot, target)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('fast', 'full', 'collect'))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--source', default=str(ROOT))
    parser.add_argument('--commit')
    parser.add_argument('--branch', default='main')
    parser.add_argument('--variant', choices=('both', 'zyx', 'xyz'), default='both',
                        help='Full-suite fixture selection; default runs both sequentially')
    parser.add_argument('--chromium', help='Working system Chromium/Chrome executable; use its version in evidence')
    parser.add_argument('--install-browser', action='store_true', help='Provision Playwright Chromium on a supported OS; default checks an available browser')
    parser.add_argument('--upgrade-backup', type=Path, help='Trusted online backup of an older disposable fixture; optional extra update gate')
    parser.add_argument('--no-cache', action='store_true')
    args = parser.parse_args(argv)
    if args.mode == 'collect':
        collect_evidence(args.source, args.output)
        return 0
    observed = None
    dirty = None
    if args.mode == 'fast':
        try:
            observed = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                               stderr=subprocess.DEVNULL, text=True).strip()
            dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT,
                                                stderr=subprocess.DEVNULL, text=True).strip())
        except subprocess.CalledProcessError:
            pass
    evidence = Evidence(args.output, args.mode, observed if args.mode == 'fast' else args.commit)
    if args.mode == 'fast':
        evidence.report['source_worktree_dirty'] = dirty
        evidence.report['revision_assertion'] = args.commit
        evidence.write()
    try:
        if args.mode == 'fast':
            if args.commit and args.commit != observed:
                raise ValueError('Fast --commit assertion differs from the observed source checkout')
            fast(evidence, args.python)
        else:
            full(evidence, args)
        evidence.complete()
    except (Exception, KeyboardInterrupt) as error:
        evidence.report['failure'] = str(error)
        evidence.write()
        if args.mode == 'full':
            retain_diagnostics(evidence)
        print(error, file=sys.stderr)
        return 1
    print('Suite passed:', evidence.directory / 'suite-report.json')
    return 0


if __name__ == '__main__':
    sys.exit(main())
