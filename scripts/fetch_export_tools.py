"""Provision checksum-pinned LOCAL export tools without changing system tools."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / '.runtime/tools'


def download(spec, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        partial = destination.with_suffix(destination.suffix + '.partial')
        with urllib.request.urlopen(spec['url'], timeout=60) as source, partial.open('wb') as target:
            while block := source.read(1024 * 1024):
                target.write(block)
        if hashlib.sha256(partial.read_bytes()).hexdigest() != spec['sha256']:
            partial.unlink()
            raise ValueError('Downloaded tool differs from its pinned checksum: ' + destination.name)
        partial.replace(destination)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != spec['sha256']:
        raise ValueError('Cached tool differs from its pinned checksum: ' + destination.name)
    return destination


def extract(archive, destination):
    # Validate both member paths and links before extracting pinned upstream archives.
    base = destination.resolve()
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            target = (base / member.name).resolve()
            if target != base and base not in target.parents:
                raise ValueError('Archive member escapes the tool directory')
            if member.isdev() or member.isfifo():
                raise ValueError('Unsupported archive member type')
            if member.issym() or member.islnk():
                link = ((target.parent if member.issym() else base) / member.linkname).resolve()
                if link != base and base not in link.parents:
                    raise ValueError('Archive link escapes the tool directory')
        if hasattr(tarfile, 'data_filter'):
            source.extractall(base, filter='data')
        else:
            source.extractall(base)


def build(commands, source, name):
    log = TOOLS / (name + '-build.log')
    if log.exists():
        log.rename(log.with_name(name + '-build.previous-' + uuid.uuid4().hex[:8] + '.log'))
    with log.open('w') as output:
        for command in commands:
            result = subprocess.run(command, cwd=source, stdout=output, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError('Local ' + name + ' build failed; inspect ' + str(log))


def build_https_dependencies(lock, jobs):
    openssl_spec = lock['openssl']
    openssl_prefix = TOOLS / 'openssl/install'
    if not (openssl_prefix / 'lib/libssl.a').exists():
        archive = download(openssl_spec, TOOLS / 'openssl' / f"openssl-{openssl_spec['version']}.tar.gz")
        source = archive.parent / f"openssl-{openssl_spec['version']}"
        if not source.exists():
            extract(archive, archive.parent)
        build([['./Configure', 'linux-x86_64', 'no-shared', 'no-tests',
                f'--prefix={openssl_prefix}', '--libdir=lib'],
               ['make', f'-j{jobs}'], ['make', 'install_sw']], source, 'openssl')
    curl_spec = lock['curl']
    curl_prefix = TOOLS / 'curl/install'
    if not (curl_prefix / 'lib/libcurl.a').exists():
        archive = download(curl_spec, TOOLS / 'curl' / f"curl-{curl_spec['version']}.tar.xz")
        source = archive.parent / f"curl-{curl_spec['version']}"
        if not source.exists():
            extract(archive, archive.parent)
        build([['./configure', f'--prefix={curl_prefix}', '--disable-shared', '--enable-static',
                f'--with-openssl={openssl_prefix}', '--with-ca-path=/etc/ssl/certs',
                '--with-ca-bundle=/etc/ssl/certs/ca-certificates.crt', '--without-libpsl',
                '--without-libidn2', '--without-brotli', '--without-zstd', '--without-nghttp2',
                '--without-nghttp3', '--without-ngtcp2', '--without-libssh2', '--without-librtmp',
                '--disable-ldap', '--disable-ldaps'],
               ['make', f'-j{jobs}'], ['make', 'install']], source, 'curl')
    protocols = subprocess.check_output([str(curl_prefix / 'bin/curl-config'), '--protocols'], text=True).split()
    if 'HTTPS' not in protocols:
        raise ValueError('Local libcurl lacks HTTPS support')
    return curl_prefix


def provision(jobs=4):
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        raise ValueError('Pinned Java provisioning supports Linux x86_64; supply compatible tools manually on other hosts')
    lock = json.loads((ROOT / 'copybara.lock.json').read_text())
    jar = download(lock, TOOLS / 'copybara/copybara_deploy.jar')
    java_spec = lock['java']
    java_archive = download(java_spec, TOOLS / 'java25/java25.tar.gz')
    java = TOOLS / 'java25' / java_spec['directory'] / 'bin/java'
    if not java.exists():
        extract(java_archive, java_archive.parent)
    git_spec = lock['git']
    archive = download(git_spec, TOOLS / 'git' / f"git-{git_spec['version']}.tar.xz")
    # Keep an older preview-only build separate; an existing git binary alone
    # does not establish that it supports HTTPS.
    prefix = TOOLS / 'git/https-install'
    git = prefix / 'bin/git'
    helper = prefix / 'libexec/git-core/git-remote-https'
    if not git.exists() or not helper.exists():
        for command in ('make', 'gcc', 'perl', 'pkg-config'):
            if not shutil.which(command):
                raise ValueError('Building local release tools requires ' + command + ' plus zlib development headers')
        curl_prefix = build_https_dependencies(lock, jobs)
        source = archive.parent / f"git-{git_spec['version']}"
        if not source.exists():
            extract(archive, archive.parent)
        build([['make', 'clean'],
               ['make', f'-j{jobs}', f'prefix={prefix}', *git_spec['build_flags'],
                f'CURL_CONFIG={curl_prefix / "bin/curl-config"}',
                'CURL_LDFLAGS=' + subprocess.check_output([str(curl_prefix / 'bin/curl-config'), '--static-libs'], text=True).strip(),
                f'OPENSSLDIR={TOOLS / "openssl/install"}', 'install']], source, 'git')
    version = subprocess.check_output([str(git), '--version'], text=True).strip()
    if version != 'git version ' + git_spec['version']:
        raise ValueError('Local Git version differs from the lock')
    report = {'copybara_jar': str(jar), 'java': str(java), 'git': str(git),
              'git_version': version, 'https_helper_present': helper.exists(),
              'openssl_version': lock['openssl']['version'], 'curl_version': lock['curl']['version'],
              'published': False}
    (TOOLS / 'export-tools.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    provision(args.jobs)
