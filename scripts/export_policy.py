"""One explicit, independently verified public tree policy for both exports."""
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    from .fetch_export_tools import download
except ImportError:
    from fetch_export_tools import download

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_REPO = 'PytorchConnectomics/cave-local'
FILES = ['.dockerignore', '.gitignore', '.github/workflows/**', 'AGENTS.md',
         'LICENSE', 'NOTICE.md', 'README.md', 'bin/**', 'compose.yml', 'config/**',
         'docker/**', 'docs/**', 'images.lock.json', 'requirements*.txt',
         'scripts/**', 'skills/**', 'templates/**', 'tests/**',
         'upstream.lock.json', 'copybara.lock.json']
EXCLUDED = ['docs/contract.md', '**/__pycache__/**', '**/*.pyc']
DOCUMENTS = ['README.md', 'AGENTS.md', 'docs/**', 'skills/**']
PRIVATE_URL = 'https://github.com/akgohain/cave-local'
AUTHOR_NAME = 'Adam Gohain'
AUTHOR_EMAIL = '68021524+akgohain@users.noreply.github.com'


def included(path):
    return (any(fnmatch.fnmatchcase(path, pattern) for pattern in FILES)
            and not any(fnmatch.fnmatchcase(path, pattern) for pattern in EXCLUDED))


def transformed(path, content, public_repo=PUBLIC_REPO):
    if any(fnmatch.fnmatchcase(path, pattern) for pattern in DOCUMENTS):
        content = content.replace(PRIVATE_URL.encode(), ('https://github.com/' + public_repo).encode())
    return content


def git_output(git, directory, *args, env=None):
    return subprocess.check_output([git, '-C', str(directory), *args], env=env)


def git_tree(git, directory, revision, env=None):
    result = {}
    for item in git_output(git, directory, 'ls-tree', '-rz', revision, env=env).split(b'\0'):
        if not item:
            continue
        info, raw_path = item.split(b'\t', 1)
        mode, kind, oid = info.decode().split()
        path = raw_path.decode()
        if mode not in ('100644', '100755') or kind != 'blob':
            raise ValueError('Public export accepts regular files only: ' + path)
        result[path] = {'mode': mode, 'content': git_output(git, directory, 'cat-file', 'blob', oid, env=env)}
    return result


def expected_tree(git, source, revision, public_repo=PUBLIC_REPO, env=None):
    return {path: {'mode': item['mode'], 'content': transformed(path, item['content'], public_repo)}
            for path, item in git_tree(git, source, revision, env).items() if included(path)}


def privacy_scan(tree):
    # Only paths/categories are reported; never copy a possible credential to a log.
    patterns = {'private-key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                'github-token': rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})',
                'aws-access-key': rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
                'personal-home': rb'/(?:home|Users)/[A-Za-z0-9_.-]+/',
                'credential-url': rb'https?://[^\s/@:]+:[^\s/@]+@'}
    findings = []
    for path, item in tree.items():
        if (path.startswith(('.runtime/', '.venv/', 'vendor/', 'deployments/', 'backups/', '.git/'))
                or Path(path).name in ('.env', 'cave-secret.json') or path == 'docs/contract.md'):
            findings.append({'path': path, 'category': 'private-generated-file'})
        for category, pattern in patterns.items():
            if re.search(pattern, item['content']):
                findings.append({'path': path, 'category': category})
    if findings:
        raise ValueError('Public tree privacy scan failed: ' + json.dumps(findings))
    return {'passed': True, 'checked_files': len(tree), 'categories': list(patterns),
            'limitation': 'Pattern and path checks supplement human review; they do not detect every possible secret.'}


def manifest(tree):
    return {path: {'sha256': hashlib.sha256(item['content']).hexdigest(), 'mode': item['mode']}
            for path, item in sorted(tree.items())}


def verify_tree(expected, actual, preserved=None):
    combined = dict(preserved or {})
    combined.update(expected)
    if set(actual) != set(combined):
        raise ValueError('Export file list differs from the managed source and preserved destination files')
    for path, item in combined.items():
        if actual[path] != item:
            raise ValueError('Export bytes or executable mode differ: ' + path)
    return privacy_scan(actual)


def tools(java=None, git=None):
    lock = json.loads((ROOT / 'copybara.lock.json').read_text())
    local_java = ROOT / '.runtime/tools/java25' / lock['java']['directory'] / 'bin/java'
    local_git = ROOT / '.runtime/tools/git/https-install/bin/git'
    java = shutil.which(java or (str(local_java) if local_java.exists() else 'java'))
    git = shutil.which(git or (str(local_git) if local_git.exists() else 'git'))
    if not java or not git:
        raise ValueError('Java 25+ and modern HTTPS Git are required; run python3 scripts/fetch_export_tools.py')
    java_version = subprocess.check_output([java, '-version'], stderr=subprocess.STDOUT, text=True)
    major = re.search(r'version "(\d+)', java_version)
    if not major or int(major.group(1)) < lock['java_major']:
        raise ValueError('Copybara requires Java 25+')
    git_version = subprocess.check_output([git, '--version'], text=True).strip()
    state = ROOT / '.runtime/export-config'
    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='git-prerequisite-', dir=state) as directory:
        check = subprocess.run([git, 'init', '--object-format=sha1', '--ref-format=files', directory],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if check.returncode:
            raise ValueError('Copybara needs Git 2.45+ supporting --ref-format=files')
    jar = download(lock, ROOT / '.runtime/tools/copybara/copybara_deploy.jar')
    return java, git, jar, lock, {'git_version': git_version, 'java_version': java_version.strip()}


def environment(git, remote=False):
    result = os.environ.copy()
    result['PATH'] = str(Path(git).parent) + os.pathsep + result.get('PATH', '')
    result['GIT_TERMINAL_PROMPT'] = '0'
    for key in list(result):
        if key.startswith('GIT_TRACE') or key == 'GIT_CURL_VERBOSE':
            result.pop(key)
    result.pop('GH_DEBUG', None)
    if remote:
        if not result.get('GH_TOKEN') or not shutil.which('gh'):
            raise ValueError('Publication requires GH_TOKEN in the process environment and GitHub CLI; no token is saved')
        # Ignore global credential storage and URL rewrites. The helper reads the
        # existing process token and never stores it in a config or token file.
        result.update({'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                       'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'credential.helper',
                       'GIT_CONFIG_VALUE_0': '', 'GIT_CONFIG_KEY_1': 'credential.helper',
                       'GIT_CONFIG_VALUE_1': '!gh auth git-credential'})
        result.pop('GIT_CONFIG_PARAMETERS', None)
        result.pop('GIT_SSL_NO_VERIFY', None)
    return result


def configuration(source, revision, destination, public_repo=PUBLIC_REPO, folder=False):
    q = json.dumps
    release_message = 'CAVE local release\n\nSource-revision: ' + revision + '\n'
    destination_line = ('folder.destination()' if folder else
                        f'git.destination(url = {q(str(destination))}, push = "main", fetch = "main")')
    return ('core.workflow(\n    name = "release",\n'
            f'    origin = git.origin(url = {q(str(source))}, ref = {q(revision)}),\n'
            f'    destination = {destination_line},\n'
            f'    authoring = authoring.overwrite({q(AUTHOR_NAME + " <" + AUTHOR_EMAIL + ">")}),\n'
            f'    origin_files = glob({q(FILES)}, exclude = {q(EXCLUDED)}),\n'
            f'    destination_files = glob({q(FILES)}, exclude = {q(EXCLUDED)}),\n'
            '    transformations = [\n'
            f'        core.transform([core.replace({q(PRIVATE_URL)}, {q("https://github.com/" + public_repo)},\n'
            f'                     paths = glob({q(DOCUMENTS)}))], noop_behavior = "IGNORE_NOOP"),\n'
            f'        metadata.replace_message({q(release_message)}),\n'
            '    ],\n    mode = "SQUASH",\n    check_last_rev_state = True,\n'
            '    migrate_noop_changes = True,\n)\n')
