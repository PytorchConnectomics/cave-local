"""Release-policy checks plus opt-in real Copybara first/update/failure tests."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.export_policy import (PRIVATE_URL, PUBLIC_REPO, expected_tree, git_tree,
                                    included, privacy_scan, tools, transformed, verify_tree)
from scripts.export_release import release


class ReleasePolicyTests(unittest.TestCase):
    def test_license_workflows_and_skills_are_managed(self):
        for path in ['LICENSE', '.github/workflows/checks.yml', 'skills/cave-local/SKILL.md',
                     'docs/evidence/private-acceptance.json']:
            self.assertTrue(included(path), path)
        for path in ['docs/contract.md', '.env', '.runtime/data/x', 'vendor/x',
                     'scripts/__pycache__/x.pyc']:
            self.assertFalse(included(path), path)

    def test_public_urls_change_in_documents_only(self):
        value = PRIVATE_URL.encode() + b'/tree/main/skills\n'
        self.assertEqual(transformed('docs/skills.md', value),
                         ('https://github.com/' + PUBLIC_REPO + '/tree/main/skills\n').encode())
        self.assertEqual(transformed('scripts/example.py', value), value)

    def test_private_paths_and_possible_secrets_fail_without_echoing_value(self):
        credential = ('gh' + 'p_' + 'A' * 40).encode()
        for content in [credential, b'/home/' + b'person/data/file',
                        b'-----BEGIN ' + b'OPENSSH PRIVATE KEY-----',
                        b'https://user:' + b'password@example.invalid']:
            with self.assertRaises(ValueError) as caught:
                privacy_scan({'README.md': {'mode': '100644', 'content': content}})
            self.assertNotIn(content.decode(), str(caught.exception))
        with self.assertRaises(ValueError):
            privacy_scan({'.env': {'mode': '100644', 'content': b'example'}})

    def test_exact_bytes_and_executable_modes_are_required(self):
        expected = {'bin/cave': {'mode': '100755', 'content': b'executable\n'}}
        for actual in [{}, {'bin/cave': {'mode': '100644', 'content': b'executable\n'}},
                       {'bin/cave': {'mode': '100755', 'content': b'changed\n'}}]:
            with self.assertRaises(ValueError):
                verify_tree(expected, actual)
        self.assertTrue(verify_tree(expected, expected)['passed'])

    def test_remote_url_and_publish_guards_precede_all_network(self):
        for destination, publish in [('https://github.com/PytorchConnectomics/other.git', True),
                                     ('https://github.com/PytorchConnectomics/cave-local.git', False),
                                     ('/tmp/not-public', True)]:
            with self.assertRaises(ValueError):
                release('HEAD', destination, publish)


@unittest.skipUnless(os.environ.get('CAVE_COPYBARA_INTEGRATION') == '1',
                     'Set CAVE_COPYBARA_INTEGRATION=1 after provisioning export tools')
class CopybaraGitIntegrationTests(unittest.TestCase):
    def test_clean_first_migration_incremental_update_and_conflict_rejection(self):
        java, git, _jar, _lock, _versions = tools()
        with tempfile.TemporaryDirectory(prefix='cave-copybara-git-') as directory:
            root = Path(directory)
            source, destination, public_work = root / 'source', root / 'public.git', root / 'public-work'

            def command(*args, cwd=None):
                return subprocess.check_output([git, *map(str, args)], cwd=cwd, stderr=subprocess.STDOUT)

            def commit(directory, message):
                command('add', '.', cwd=directory)
                command('commit', '-m', message, cwd=directory)
                return command('rev-parse', 'HEAD', cwd=directory).decode().strip()

            command('init', '--initial-branch=main', source)
            command('config', 'user.name', 'Private fixture author', cwd=source)
            command('config', 'user.email', 'private@example.invalid', cwd=source)
            files = {'README.md': PRIVATE_URL + '.git\n', 'LICENSE': 'MIT fixture license\n',
                     'bin/cave': '#!/bin/sh\nexit 0\n', 'scripts/removed.py': '# first\n',
                     'docs/contract.md': 'PRIVATE CONTRACT\n', '.runtime/secret': 'PRIVATE DATA\n',
                     '.github/workflows/checks.yml': 'name: Checks\n',
                     'docs/evidence/private-acceptance.json': '{"passed": true}\n'}
            for name, content in files.items():
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            (source / 'bin/cave').chmod(0o755)
            commit(source, 'PRIVATE INTERNAL MESSAGE NEVER EXPORTED')
            (source / 'README.md').write_text(files['README.md'] + 'First release\n')
            first_source = commit(source, 'Another private development message')
            first = release(first_source, destination, java=java, git=git, source=source)
            self.assertTrue(first['passed'])
            self.assertFalse(first['published'])
            self.assertEqual(command('rev-list', '--count', 'main', cwd=destination).strip(), b'1')
            log = command('log', '--format=%B%n%ae', 'main', cwd=destination)
            self.assertNotIn(b'PRIVATE INTERNAL', log)
            self.assertNotIn(b'private@example.invalid', log)
            self.assertNotIn('docs/contract.md', first['files'])
            self.assertNotIn('.runtime/secret', first['files'])
            self.assertEqual(first['files']['bin/cave']['mode'], '100755')
            command('clone', destination, public_work)
            command('config', 'user.name', 'Public fixture author', cwd=public_work)
            command('config', 'user.email', 'public@example.invalid', cwd=public_work)
            (public_work / 'community').mkdir()
            (public_work / 'community/notes.txt').write_text('Destination-owned community note\n')
            destination_only_commit = commit(public_work, 'Public community note')
            command('push', 'origin', 'main', cwd=public_work)
            community_replay = release(first_source, destination, java=java, git=git, source=source)
            self.assertTrue(community_replay['passed'])
            self.assertTrue(community_replay['migration_noop'])
            self.assertFalse(community_replay['published'])
            self.assertEqual(community_replay['destination_commit'], destination_only_commit)
            self.assertEqual(community_replay['source_marker_commit'], first['destination_commit'])
            self.assertEqual(community_replay['preserved_destination_files'], ['community/notes.txt'])
            self.assertEqual(command('rev-parse', 'main', cwd=destination).decode().strip(), destination_only_commit)
            self.assertEqual(command('rev-list', '--count', 'main', cwd=destination).strip(), b'2')
            verify_tree(expected_tree(git, source, first_source), git_tree(git, destination, 'main'),
                        {'community/notes.txt': {'mode': '100644', 'content': b'Destination-owned community note\n'}})
            (source / 'README.md').write_text(files['README.md'] + 'Second release\n')
            (source / 'scripts/removed.py').unlink()
            (source / 'docs/new.md').write_text('New operator documentation\n')
            (source / 'bin/cave').chmod(0o644)
            second_source = commit(source, 'Private second development update')
            second = release(second_source, destination, java=java, git=git, source=source)
            self.assertTrue(second['passed'])
            self.assertEqual(second['destination_before'], destination_only_commit)
            self.assertEqual(second['previous_source_commit'], first_source)
            self.assertEqual(second['preserved_destination_files'], ['community/notes.txt'])
            self.assertNotIn('scripts/removed.py', second['files'])
            self.assertEqual(second['files']['bin/cave']['mode'], '100644')
            self.assertEqual(command('rev-list', '--count', 'main', cwd=destination).strip(), b'3')
            verify_tree(expected_tree(git, source, second_source), git_tree(git, destination, 'main'),
                        {'community/notes.txt': {'mode': '100644', 'content': b'Destination-owned community note\n'}})
            same = release(second_source, destination, java=java, git=git, source=source)
            self.assertTrue(same['migration_noop'])
            self.assertEqual(same['destination_commit'], second['destination_commit'])
            command('pull', '--ff-only', cwd=public_work)
            (public_work / 'README.md').write_text('Public managed-path edit that must not be overwritten\n')
            conflict_commit = commit(public_work, 'Managed path conflict')
            command('push', 'origin', 'main', cwd=public_work)
            (source / 'docs/new.md').write_text('Another source change\n')
            third_source = commit(source, 'Private third development update')
            with self.assertRaises(ValueError):
                release(third_source, destination, java=java, git=git, source=source)
            self.assertEqual(command('rev-parse', 'main', cwd=destination).decode().strip(), conflict_commit)


if __name__ == '__main__':
    unittest.main()
