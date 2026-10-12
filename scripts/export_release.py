"""Stage an actual Copybara Git migration, verify it, then push its exact commit."""
import argparse
import json
import re
import subprocess
import urllib.request
import uuid
from pathlib import Path

try:
    from .export_policy import (ROOT, PUBLIC_REPO, AUTHOR_NAME, AUTHOR_EMAIL, configuration,
                                environment, expected_tree, git_output, git_tree, included,
                                manifest, privacy_scan, tools, verify_tree)
except ImportError:
    from export_policy import (ROOT, PUBLIC_REPO, AUTHOR_NAME, AUTHOR_EMAIL, configuration,
                               environment, expected_tree, git_output, git_tree, included,
                               manifest, privacy_scan, tools, verify_tree)

PUBLIC_URL = 'https://github.com/' + PUBLIC_REPO + '.git'


def run(git, *args, env):
    return subprocess.run([git, *map(str, args)], env=env, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def remote_identity(env):
    request = urllib.request.Request('https://api.github.com/repos/' + PUBLIC_REPO,
        headers={'Authorization': 'Bearer ' + env['GH_TOKEN'], 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(request, timeout=30) as response:
        identity = json.load(response)
    if (identity.get('full_name', '').lower() != PUBLIC_REPO.lower()
            or identity.get('private') is not False or identity.get('owner', {}).get('type') != 'Organization'):
        raise ValueError('Publication destination must be the intended public organization repository')
    return {'id': identity['id'], 'full_name': identity['full_name'], 'private': identity['private']}


def release(commit, destination, publish=False, java=None, git=None, source=ROOT):
    source = Path(source).resolve()
    remote = str(destination).startswith('https://')
    if remote:
        if str(destination) != PUBLIC_URL or not publish:
            raise ValueError('Remote destination must be ' + PUBLIC_URL + ' with --publish after release acceptance')
    elif publish:
        raise ValueError('--publish is only valid for the approved organization URL')
    else:
        if '://' in str(destination) or re.match(r'[^/]+@[^/]+:', str(destination)):
            raise ValueError('Use an absolute local bare-repository path or the approved HTTPS URL')
        destination = Path(destination).resolve()
        if (destination == source or destination in source.parents or source in destination.parents
                or destination == ROOT or destination in ROOT.parents or ROOT in destination.parents):
            raise ValueError('Local Git destination must be outside the source and packaging checkouts')
    java, git, jar, lock, versions = tools(java, git)
    env = environment(git, remote=remote)
    if remote:
        exec_path = subprocess.check_output([git, '--exec-path'], env=env, text=True).strip()
        if not (Path(exec_path) / 'git-remote-https').exists():
            raise ValueError('The selected Git lacks HTTPS support')
    resolved = git_output(git, source, 'rev-parse', '--verify', commit + '^{commit}', env=env).decode().strip()
    expected = expected_tree(git, source, resolved, env=env)
    privacy_scan(expected)
    identity = remote_identity(env) if remote else None
    if not remote:
        if not destination.exists():
            run(git, 'init', '--bare', '--initial-branch=main', destination, env=env)
        bare = git_output(git, destination, 'rev-parse', '--is-bare-repository', env=env).decode().strip()
        if bare != 'true':
            raise ValueError('Local migration destination must be a bare Git repository')
    state = ROOT / '.runtime/releases' / (resolved[:12] + '-' + uuid.uuid4().hex[:8])
    state.mkdir(parents=True, mode=0o700)
    report_path = state / 'release-report.json'
    report = {'passed': False, 'published': False, 'source_commit': resolved,
              'destination': str(destination), 'destination_identity': identity,
              'copybara': lock, **versions}
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    try:
        # No private source objects or previous private commit messages enter
        # this bare repo: its only initial objects are from the public target.
        staging = state / 'staging.git'
        run(git, 'init', '--bare', '--initial-branch=main', staging, env=env)
        refs = run(git, 'ls-remote', '--heads', destination, 'refs/heads/main', env=env).stdout.decode().strip()
        previous = refs.split()[0] if refs else None
        preserved = {}
        prior_origin = None
        prior_import_commit = None
        if previous:
            run(git, '-C', staging, 'fetch', '--no-tags', destination, 'refs/heads/main:refs/heads/main', env=env)
            if git_output(git, staging, 'rev-parse', 'main', env=env).decode().strip() != previous:
                raise ValueError('Destination changed during staging; start a new release check')
            before = git_tree(git, staging, previous, env)
            privacy_scan(before)
            preserved = {path: item for path, item in before.items() if not included(path)}
            # Destination-owned commits can sit above the last import. Recover
            # the actual imported commit together with its source marker rather
            # than requiring those markers on an unrelated community tip.
            records = git_output(git, staging, 'log', '--format=%H%x00%B%x00', 'main', env=env).decode().split('\0')
            for index in range(0, len(records) - 1, 2):
                public_commit, message = records[index].strip(), records[index + 1]
                origin = re.search(r'^GitOrigin-RevId: ([a-f0-9]{40})$', message, re.MULTILINE)
                if origin:
                    prior_origin = origin.group(1)
                    if not re.search(r'^Source-revision: ' + prior_origin + r'$', message, re.MULTILINE):
                        raise ValueError('Existing import has inconsistent source revision markers')
                    prior_import_commit = public_commit
                    break
            if not prior_origin:
                raise ValueError('Existing destination has no Copybara source marker; refusing unrelated history')
            run(git, '-C', source, 'merge-base', '--is-ancestor', prior_origin, resolved, env=env)
            # Independently check the last imported managed tree before running
            # Copybara. This also catches destination edits when Copybara's
            # own force/init behavior would otherwise accept them.
            verify_tree(expected_tree(git, source, prior_origin, env=env), before, preserved)
        config = state / 'copy.bara.sky'
        config.write_text(configuration(source, resolved, destination))
        command = [java, '-jar', str(jar), 'migrate', str(config), 'release', resolved,
                   '--git-destination-url=' + str(staging), '--output-root=' + str(state / 'copybara-work'),
                   '--git-committer-name=' + AUTHOR_NAME, '--git-committer-email=' + AUTHOR_EMAIL]
        if not previous:
            # Copybara requires --force to create an absent destination branch.
            # It is confined to this verified-empty local staging repository;
            # later migrations retain check_last_rev_state and never use it.
            command.extend(['--init-history', '--force'])
        same_revision = prior_origin == resolved
        if same_revision:
            verify_tree(expected, before, preserved)
            (state / 'copybara.log').write_text('Exact source revision and destination tree already match; no migration needed.\n')
        else:
            with (state / 'copybara.log').open('w') as output:
                result = subprocess.run(command, cwd=source, env=env, stdout=output, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError('Copybara Git migration failed; inspect ' + str(state / 'copybara.log'))
        migrated = git_output(git, staging, 'rev-parse', 'main', env=env).decode().strip()
        actual = git_tree(git, staging, migrated, env)
        scan = verify_tree(expected, actual, preserved)
        marker_commit = prior_import_commit if same_revision else migrated
        message = git_output(git, staging, 'show', '-s', '--format=%B', marker_commit, env=env).decode()
        if 'GitOrigin-RevId: ' + resolved not in message or 'Source-revision: ' + resolved not in message:
            raise ValueError('Migration did not record its exact source revision')
        source_history = set(git_output(git, source, 'rev-list', resolved, env=env).decode().split())
        destination_history = set(git_output(git, staging, 'rev-list', migrated, env=env).decode().split())
        if source_history & destination_history:
            raise ValueError('Private source commits entered the public ancestry')
        if not previous and len(destination_history) != 1:
            raise ValueError('Initial migration must have exactly one clean squash commit')
        if previous:
            run(git, '-C', staging, 'merge-base', '--is-ancestor', previous, migrated, env=env)
        report.update({'destination_before': previous, 'previous_source_commit': prior_origin,
                       'destination_commit': migrated, 'staging_repo': str(staging),
                       'source_marker_commit': marker_commit,
                       'files': manifest(actual), 'managed_files': sorted(expected),
                       'preserved_destination_files': sorted(preserved), 'privacy_scan': scan,
                       'source_bytes_verified': True, 'executable_modes_verified': True,
                       'private_source_history_excluded': True, 'fast_forward_verified': True,
                       'migration_noop': same_revision})
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        # Publish only the verified object/ancestry. Refuse both concurrent
        # destination changes and any non-fast-forward push; no force is used.
        current = run(git, 'ls-remote', '--heads', destination, 'refs/heads/main', env=env).stdout.decode().strip()
        if (current.split()[0] if current else None) != previous:
            raise ValueError('Destination changed after verification; nothing was pushed')
        run(git, '-C', staging, 'push', destination, migrated + ':refs/heads/main', env=env)
        final = run(git, 'ls-remote', '--heads', destination, 'refs/heads/main', env=env).stdout.decode().split()[0]
        if final != migrated:
            raise ValueError('Destination does not point to the verified migration commit')
        report.update({'passed': True, 'published': remote, 'destination_commit_verified': True})
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'passed': True, 'published': remote, 'source_commit': resolved,
                          'destination_commit': migrated, 'report': str(report_path)}, indent=2))
        return report
    except Exception as error:
        # Preserve the failed report and logs. Never include stderr from an
        # authenticated Git command or a possible credential in this report.
        report['failure_type'] = type(error).__name__
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('commit')
    parser.add_argument('destination')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--java')
    parser.add_argument('--git')
    args = parser.parse_args()
    release(args.commit, args.destination, args.publish, args.java, args.git)
