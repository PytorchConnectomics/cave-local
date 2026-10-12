"""Run real acceptance from a new clone, venv, input, graph and volumes.

No existing deployment is modified. Logs and evidence remain in the new clone.
Git authentication, if needed, comes from the caller's normal Git environment.
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', help='Git URL or local Git repository')
    p.add_argument('commit')
    p.add_argument('destination', type=Path)
    p.add_argument('--branch', default='main')
    p.add_argument('--python', default=sys.executable)
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--graph-id', required=True)
    p.add_argument('--variant', choices=('zyx', 'xyz'), default='zyx')
    p.add_argument('--atom-policy', choices=('source_labels', 'spatial_tiles'), default='source_labels')
    p.add_argument('--no-cache', action='store_true')
    p.add_argument('--chromium', help='Existing Chromium executable; no browser download required')
    p.add_argument('--install-browser', action='store_true',
                   help='Download Playwright Chromium on a supported browser-install host')
    p.add_argument('--browser-lock', type=Path, help='Optional shared task lock to serialize browser graphics')
    args = p.parse_args()
    dest = args.destination.resolve()
    if dest.exists():
        raise ValueError('Fresh acceptance requires a nonexistent destination')
    subprocess.run(['git', 'clone', '--branch', args.branch, args.source, str(dest)], check=True)
    subprocess.run(['git', '-C', str(dest), 'checkout', '--detach', args.commit], check=True)
    actual = subprocess.check_output(['git', '-C', str(dest), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != args.commit:
        raise RuntimeError('Fresh checkout differs from requested commit')
    logs = dest / '.runtime/logs';logs.mkdir(parents=True)
    report = {'source_commit': actual, 'passed': False, 'variant': args.variant,
              'atom_policy': args.atom_policy, 'steps': [],
              'fresh_state': ['Git checkout', 'Python venv', 'vendor checkouts', 'viewer output', 'input fixture', 'graph', 'Postgres', 'Redis', 'generated secrets'],
              'shared_host_state': ['OS', 'Docker daemon', 'installed Python/Node/Git', 'global package/download caches', 'Docker layers unless --no-cache'],
              'no_cache_docker_build': args.no_cache}
    env = dict(os.environ, CAVE_PYTHON=args.python)

    def run(name, command):
        began = time.monotonic()
        print(name, flush=True)
        with (logs / (name + '.log')).open('w') as output:
            result = subprocess.run(list(map(str, command)), cwd=dest, env=env, stdout=output, stderr=subprocess.STDOUT)
        report['steps'].append({'step': name, 'exit_code': result.returncode, 'seconds': round(time.monotonic() - began, 2)})
        (dest / '.runtime/fresh-install-report.json').write_text(json.dumps(report, indent=2) + '\n')
        if result.returncode:
            raise RuntimeError(f'{name} failed; see {logs / (name + ".log")}')

    cave = dest / 'bin/cave'
    source = dest / '.runtime/input'
    deployment = dest / '.runtime/deployment'
    python = dest / '.venv/bin/python'
    run('doctor', [cave, 'doctor'])
    run('deps', [cave, 'deps'])
    run('unit-tests', [python, '-m', 'pytest', '-q', 'tests',
                       '--junitxml', dest / '.runtime/unit-results.xml'])
    run('package-checks', [python, 'scripts/check_package.py'])
    if args.install_browser:
        run('browser-install', [python, '-m', 'playwright', 'install', 'chromium'])
    browser_report = dest / '.runtime/browser-preflight.json'
    run('browser-ready', [python, 'scripts/check_browser.py', '--output', browser_report]
        + (['--chromium', args.chromium] if args.chromium else []))
    report['browser_preflight'] = json.loads(browser_report.read_text())
    env['CAVE_CHROMIUM_EXECUTABLE'] = report['browser_preflight']['executable']
    run('build', [cave, 'build'] + (['--no-cache'] if args.no_cache else []))
    run('fixture', [cave, 'fixture', source, '--variant', args.variant, '--atom-policy', args.atom_policy, '--graph-id', args.graph_id, '--port', args.port])
    run('create', [cave, 'create', source / 'manifest.json', deployment, '--source-dir', source])
    run('start', [cave, 'start', deployment])
    if args.browser_lock:
        args.browser_lock.parent.mkdir(parents=True, exist_ok=True)
        with args.browser_lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            run('api-browser', [cave, 'validate', deployment, '--edit-fixture', '--browser'])
    else:
        run('api-browser', [cave, 'validate', deployment, '--edit-fixture', '--browser'])
    if args.atom_policy == 'source_labels':
        run('source-label-policy', [python, 'scripts/verify_source_label_policy.py', deployment])
    run('segmentation-metadata-concurrency', [python, 'scripts/verify_segmentation_metadata.py', deployment])
    run('database-reconnect', [python, 'scripts/verify_database_reconnect.py', deployment])
    run('holding-edit-restart', [python, 'scripts/verify_iteration_persistence.py', deployment, '--holding-edit'])
    run('snapshot-before-rerun', [python, 'scripts/snapshot.py', deployment, '--output', dest / '.runtime/before-rerun.json'])
    run('rerun-preserves-edits', [cave, 'start', deployment])
    run('snapshot', [python, 'scripts/snapshot.py', deployment, '--output', dest / '.runtime/final-snapshot.json'])
    if json.loads((dest / '.runtime/before-rerun.json').read_text()) != json.loads((dest / '.runtime/final-snapshot.json').read_text()):
        raise RuntimeError('Repeating start changed the saved graph/annotation snapshot')
    report['rerun_snapshot_equal'] = True
    report['passed'] = True
    (dest / '.runtime/fresh-install-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
