"""Run pinned Copybara into a new local folder, with the release tree policy."""
import argparse
import json
import re
import subprocess
from pathlib import Path

try:
    from .export_policy import (ROOT, PUBLIC_REPO, FILES, configuration, environment,
                                expected_tree, manifest, privacy_scan, tools, verify_tree)
except ImportError:
    from export_policy import (ROOT, PUBLIC_REPO, FILES, configuration, environment,
                               expected_tree, manifest, privacy_scan, tools, verify_tree)


def export(commit, output, java=None, public_repo=PUBLIC_REPO, git=None):
    output = Path(output).resolve()
    if output.exists() or output == ROOT or output in ROOT.parents or ROOT in output.parents:
        raise ValueError('Copybara preview requires a new output directory outside the source checkout')
    if not re.fullmatch(r'PytorchConnectomics/[A-Za-z0-9_-]+', public_repo):
        raise ValueError('Expected a proposed PytorchConnectomics repository name')
    java, git, jar, lock, versions = tools(java, git)
    env = environment(git)
    resolved = subprocess.check_output([git, '-C', str(ROOT), 'rev-parse', '--verify', commit + '^{commit}'],
                                       env=env, text=True).strip()
    expected = expected_tree(git, ROOT, resolved, public_repo, env)
    privacy_scan(expected)
    state = ROOT / '.runtime/export-config' / output.name
    state.mkdir(parents=True, exist_ok=True)
    config = state / 'copy.bara.sky'
    config.write_text(configuration(ROOT, resolved, output, public_repo, folder=True))
    subprocess.run([java, '-jar', str(jar), 'migrate', str(config), 'release', resolved,
                    '--folder-dir=' + str(output), '--output-root=' + str(state / 'copybara-work'), '--force'],
                   cwd=ROOT, env=env, check=True)
    actual = {}
    for path in output.rglob('*'):
        if path.is_symlink():
            raise ValueError('Export unexpectedly contains a symlink')
        if path.is_file():
            actual[str(path.relative_to(output))] = {'content': path.read_bytes(),
                                                    'mode': '100755' if path.stat().st_mode & 0o111 else '100644'}
    scan = verify_tree(expected, actual)
    report = {'source_commit': resolved, 'copybara': lock, 'destination_type': 'local folder',
              'published': False, 'proposed_public_repo': public_repo,
              'files_sha256': {path: entry['sha256'] for path, entry in manifest(actual).items()},
              'files': manifest(actual), 'source_bytes_verified': True,
              'executable_modes_verified': True, 'privacy_scan': scan, **versions}
    (state / 'export-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'source_commit': resolved, 'exported_files': len(actual), 'published': False}, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('commit')
    parser.add_argument('output', type=Path)
    parser.add_argument('--java')
    parser.add_argument('--git')
    parser.add_argument('--public-repo', default=PUBLIC_REPO)
    args = parser.parse_args()
    export(args.commit, args.output, args.java, args.public_repo, args.git)
