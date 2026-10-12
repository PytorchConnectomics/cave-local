"""Acquire only locked upstream sources, never a preexisting workstation build."""
import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fetch(names=None):
    lock = json.loads((ROOT / 'upstream.lock.json').read_text())
    for name in names or lock:
        entry = lock[name]
        dest = ROOT / 'vendor' / name
        dest.parent.mkdir(exist_ok=True)
        if not dest.exists():
            subprocess.run(['git', 'init', str(dest)], check=True)
            subprocess.run(['git', '-C', str(dest), 'remote', 'add', 'origin', entry['remote']], check=True)
        actual_remote = subprocess.check_output(['git', '-C', str(dest), 'remote', 'get-url', 'origin'], text=True).strip()
        if actual_remote != entry['remote']:
            raise RuntimeError(f'{name}: checkout remote differs from upstream.lock.json')
        dirty = subprocess.check_output(['git', '-C', str(dest), 'status', '--porcelain'], text=True)
        if dirty:
            raise RuntimeError(f'{name}: checkout contains local changes; inspect before replacing')
        subprocess.run(['git', '-C', str(dest), 'fetch', '--depth', '1', 'origin', entry['revision']], check=True)
        subprocess.run(['git', '-C', str(dest), 'checkout', '--detach', 'FETCH_HEAD'], check=True)
        actual = subprocess.check_output(['git', '-C', str(dest), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != entry['revision']:
            raise RuntimeError(f'{name}: fetched unexpected revision')
        print(f'{name}: {actual}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('names', nargs='*')
    fetch(parser.parse_args().names or None)
