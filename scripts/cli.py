"""Repository entrypoint: portable installation and deployment operations."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from docker_capacity import available_cpus

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    print('+', ' '.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def deployment(path):
    path = Path(path).resolve()
    meta = json.loads((path / 'deployment.json').read_text())
    import yaml
    compose = yaml.safe_load((path / 'compose.yml').read_text())
    if not compose['name'].startswith('cave-pkg-') or compose['name'] != meta['project_name']:
        raise ValueError('Deployment project identity differs from metadata or is outside cave-pkg- scope')
    return path, meta, ['docker', 'compose', '--env-file', str(path / '.env'), '-f', str(path / 'compose.yml')]


def wait_http(url, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError(f'Service did not become ready: {url}; inspect bin/cave logs DEPLOYMENT')


def doctor():
    missing = [name for name in ('docker', 'git', 'node', 'npm') if not shutil.which(name)]
    if missing:
        raise RuntimeError('Install prerequisites: ' + ', '.join(missing))
    if sys.version_info < (3, 11):
        raise RuntimeError('Host Python >=3.11 required; select it with CAVE_PYTHON=/path/to/python')
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise RuntimeError('Initial supported host is Linux x86-64')
    run(['docker', 'info', '--format', '{{.ServerVersion}}'])
    run(['docker', 'compose', 'version'])
    capacity = available_cpus()
    node = subprocess.check_output(['node', '--version'], text=True).strip()
    parts = tuple(map(int, node.lstrip('v').split('.')[:2]))
    if parts < (22, 18):
        raise RuntimeError('Node >=22.18 required by pinned upstream viewer')
    disk = shutil.disk_usage(ROOT)
    report = {'platform': platform.platform(), 'python': platform.python_version(), 'node': node,
              'docker_cpu_capacity': capacity,
              'free_disk_gib': round(disk.free / 1024**3, 2)}
    if disk.free < 20 * 1024**3:
        raise RuntimeError('At least 20 GiB free required for dependencies and fixture installs; real datasets need more')
    print(json.dumps(report, indent=2))
    return report


def build(no_cache=False, skip_viewer=False):
    from fetch_upstream import fetch
    fetch()
    key = hashlib.sha256((ROOT / 'docker/pcg.Dockerfile').read_bytes() + (ROOT / 'docker/bigtable.Dockerfile').read_bytes() + (ROOT / 'upstream.lock.json').read_bytes()).hexdigest()[:12]
    for name in ('bigtable', 'pcg'):
        args = ['docker', 'build', '--pull', '-t', f'cave-local-{name}:{key}', '-f', ROOT / f'docker/{name}.Dockerfile']
        if no_cache:
            args.append('--no-cache')
        run(args + [ROOT], cwd=ROOT)
    if not skip_viewer:
        run([sys.executable, ROOT / 'scripts/build_viewer.py'], cwd=ROOT)
    print('Runtime build:', key)


def start(path):
    dest, meta, cmd = deployment(path)
    run(cmd + ['up', '-d'], cwd=dest)
    wait_http(meta['local_url'] + '/info/health')
    run(cmd + ['exec', '-T', 'info', 'python', '/workspace/scripts/bootstrap_info.py'])
    run(cmd + ['exec', '-T', 'materialize', 'python', '/workspace/scripts/bootstrap_mat.py'])
    run(cmd + ['run', '--rm', '--no-deps', '--entrypoint', 'python', 'pcg', '/workspace/scripts/ingest.py'])
    wait_http(meta['local_url'] + '/segmentation/table/' + meta['graph_id'] + '/info')
    if meta.get('test_fixture') and not (dest / 'data/scenario.json').exists():
        run([sys.executable, ROOT / 'scripts/choose_iteration_scenario.py', dest])
    from configure_iteration_viewer import configure
    configure(dest)
    run(cmd + ['ps'])
    print('Viewer:', meta['local_url'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    sub.add_parser('deps')
    sub.add_parser('fetch')
    p = sub.add_parser('build'); p.add_argument('--no-cache', action='store_true'); p.add_argument('--skip-viewer', action='store_true')
    p = sub.add_parser('fixture'); p.add_argument('output'); p.add_argument('--variant', default='zyx', choices=['zyx', 'xyz']); p.add_argument('--atom-policy', default='source_labels', choices=['source_labels', 'spatial_tiles', 'agglomerated_supervoxels']); p.add_argument('--graph-id', default='pkg_fixture'); p.add_argument('--port', default=8181, type=int); p.add_argument('--supervoxel-split', action='store_true'); p.add_argument('--paint', action='store_true', help='Add paint geometry; implies agglomerated_supervoxels and --supervoxel-split')
    p = sub.add_parser('svsplit'); q = p.add_subparsers(dest='svsplit_command', required=True); p = q.add_parser('repair'); p.add_argument('deployment'); p.add_argument('--pcg-repair', type=int, metavar='OPERATION_ID')
    p = sub.add_parser('create'); p.add_argument('manifest'); p.add_argument('destination'); p.add_argument('--source-dir'); p.add_argument('--project-name'); p.add_argument('--paint', action='store_true', help='Enable paint defaults on a generated agglomerated fixture')
    for name in ('start', 'stop', 'status', 'logs', 'snapshot', 'browser-handoff'):
        p = sub.add_parser(name); p.add_argument('deployment')
    p = sub.add_parser('validate'); p.add_argument('deployment'); p.add_argument('--edit-fixture', action='store_true'); p.add_argument('--browser', action='store_true')
    for name in ('backup', 'verify-backup', 'restore', 'teardown', 'rotate-secrets'):
        p = sub.add_parser(name); p.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == 'doctor': doctor()
    elif args.command == 'deps':
        doctor()
        target = ROOT / '.venv'
        if not target.exists(): run([sys.executable, '-m', 'venv', target])
        run([target / 'bin/python', '-m', 'pip', 'install', '-r', ROOT / 'requirements.lock.txt'])
    elif args.command == 'fetch':
        from fetch_upstream import fetch
        fetch()
    elif args.command == 'build': build(args.no_cache, args.skip_viewer)
    elif args.command == 'fixture': run([sys.executable, ROOT / 'scripts/make_fixture.py', args.output, '--variant', args.variant, '--atom-policy', args.atom_policy, '--graph-id', args.graph_id, '--port', args.port] + (['--supervoxel-split'] if args.supervoxel_split else []) + (['--paint'] if args.paint else []))
    elif args.command == 'svsplit':
        dest, meta, cmd = deployment(args.deployment)
        current = dest / 'data/atom_tables/current'
        paint_state = dest / 'data/supervoxel_splits/paint'
        has_paint = (bool(meta.get('supervoxel_split', {}).get('paint'))
                     or any(paint_state.rglob('*.json')))
        retained = (current.exists() or current.is_symlink()
                    or any((dest / 'data/supervoxel_splits/journal').rglob('journal.json')) or has_paint)
        if not meta.get('supervoxel_split', {}).get('enabled') and not retained:
            raise ValueError('Supervoxel refinement is not enabled and has no retained state for this deployment')
        if args.pcg_repair is not None and (args.pcg_repair <= 0 or not has_paint):
            raise ValueError('--pcg-repair needs a positive painted operation id and retained paint state')
        script = 'supervoxel_paint_history.py' if has_paint else 'supervoxel_refine.py'
        run(cmd + ['exec', '-T', 'pcg', 'python', '/workspace/scripts/' + script,
                   '--repair', '--graph-id', meta['graph_id']]
            + (['--pcg-repair', str(args.pcg_repair)] if args.pcg_repair is not None else []))
    elif args.command == 'create':
        from create_segmentation_deployment import create
        create(args.manifest, args.destination, args.source_dir, args.project_name,
               **({'paint': True} if args.paint else {}))
    elif args.command == 'start': start(args.deployment)
    elif args.command in ('stop', 'status', 'logs'):
        dest, meta, cmd = deployment(args.deployment)
        extra = {'stop': ['stop'], 'status': ['ps'], 'logs': ['logs', '--tail', '100']}[args.command]
        run(cmd + extra)
    elif args.command == 'snapshot': run([sys.executable, ROOT / 'scripts/snapshot.py', args.deployment])
    elif args.command == 'browser-handoff': run([sys.executable, ROOT / 'scripts/browser_handoff.py', args.deployment])
    elif args.command == 'validate':
        if args.browser and not args.edit_fixture:
            raise ValueError('Automated browser edit acceptance requires --edit-fixture '
                             'on a generated disposable fixture. Use the documented '
                             'read-only browser check for real data.')
        dest, meta, cmd = deployment(args.deployment)
        if not args.edit_fixture:
            run([sys.executable, ROOT / 'scripts/snapshot.py', dest])
        else:
            if not meta.get('test_fixture'):
                raise ValueError('Editing acceptance is only allowed on generated disposable fixtures')
            if meta.get('supervoxel_split', {}).get('enabled'):
                run([sys.executable, ROOT / 'scripts/verify_supervoxel_split.py', dest])
            else:
                run([sys.executable, ROOT / 'scripts/verify_segmentation_iteration.py', dest])
            if args.browser: run([sys.executable, ROOT / 'scripts/verify_iteration_browser.py', dest])
    else:
        command = 'verify' if args.command == 'verify-backup' else args.command
        run([sys.executable, ROOT / 'scripts/operations.py', command] + args.arguments)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print('CAVE:', exc, file=sys.stderr)
        sys.exit(1)
