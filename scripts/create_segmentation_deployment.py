"""Generate an independent loopback CAVE deployment from a checked manifest."""
import argparse
import hashlib
import json
import math
import re
import secrets
import shutil
import socket
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from docker_capacity import available_cpus

ROOT = Path(__file__).resolve().parents[1]


def validate_manifest(meta):
    graph = meta.get('graph_id', '')
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,40}', graph):
        raise ValueError('graph_id must begin with a lowercase letter and contain at most 41 letters, digits or underscores')
    url = urlsplit(meta.get('local_url', ''))
    if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost') or not url.port or url.path or url.query or url.fragment or url.username:
        raise ValueError('local_url must be http://127.0.0.1:PORT or http://localhost:PORT')
    if url.port < 1024:
        raise ValueError('Choose an unprivileged port from 1024 to 65535')
    for field in ('shape_xyz', 'graph_chunk_xyz'):
        values = meta.get(field)
        if not isinstance(values, list) or len(values) != 3 or any(type(v) is not int or v <= 0 for v in values):
            raise ValueError(f'{field} must contain three positive integers in XYZ order')
    resolution = meta.get('resolution_nm_xyz')
    if not isinstance(resolution, list) or len(resolution) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in resolution):
        raise ValueError('resolution_nm_xyz must contain three finite positive spacings in nanometers')
    if meta.get('voxel_offset_xyz', [0, 0, 0]) != [0, 0, 0]:
        raise ValueError('This release supports zero-origin volumes; nonzero origins are not yet supported')
    policy = meta.get('atom_policy', 'source_labels')
    if policy not in ('source_labels', 'spatial_tiles'):
        raise ValueError('atom_policy must be source_labels or spatial_tiles')
    if policy == 'spatial_tiles':
        tiles = meta.get('atom_tile_xyz')
        if not isinstance(tiles, list) or len(tiles) != 3 or any(type(v) is not int or v <= 0 for v in tiles):
            raise ValueError('spatial_tiles needs three positive atom_tile_xyz integers')
        if any(c % t for c, t in zip(meta['graph_chunk_xyz'], tiles)):
            raise ValueError('Every graph chunk dimension must be divisible by the corresponding tile dimension')
    for field in ('image_https', 'segmentation_https'):
        source = urlsplit(meta.get(field, ''))
        if any(character.isspace() or character in '{};"' for character in meta.get(field, '')):
            raise ValueError(f'{field} contains unsupported unescaped URL characters')
        if source.scheme not in ('http', 'https', 'file') or source.query or source.fragment or source.username:
            raise ValueError(f'{field} must be an unsigned public HTTP(S) URL or file:///source/ path')
        if source.scheme == 'file' and (source.netloc or not source.path.startswith('/source/') or '..' in Path(source.path).parts):
            raise ValueError(f'{field}: local data must be below the read-only /source mount')
        if source.scheme != 'file' and not source.hostname:
            raise ValueError(f'{field} needs a hostname')
        if source.scheme != 'file' and source.hostname in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError(f'{field}: container loopback cannot reach the host; use file:///source/ for local inputs')
    return url.port


def create(manifest, dest, source_dir=None, project_name=None):
    meta = json.loads(Path(manifest).read_text())
    port = validate_manifest(meta)
    from ingest_segmentation_iteration import validate_geometry
    validate_geometry(meta)
    dest = Path(dest).resolve()
    if dest.exists():
        raise ValueError(f'Destination already exists: {dest}; use start to preserve its edits')
    graph = meta['graph_id']
    suffix = hashlib.sha256(str(dest).encode()).hexdigest()[:8]
    project = project_name or f'cave-pkg-{graph.replace("_", "-")}-{suffix}'
    if not re.fullmatch(r'cave-pkg-[a-z0-9][a-z0-9-]{0,100}', project):
        raise ValueError('project_name must begin cave-pkg- and contain lowercase letters, digits or hyphens')
    source = Path(source_dir).resolve() if source_dir else None
    local_inputs = [urlsplit(meta[k]).path for k in ('image_https', 'segmentation_https') if meta[k].startswith('file://')]
    if local_inputs:
        if source is None or not source.is_dir():
            raise ValueError('Local inputs require --source-dir pointing to their containing folder')
        for item in local_inputs:
            actual = (source / Path(item).relative_to('/source')).resolve()
            if not actual.is_relative_to(source) or not actual.is_dir():
                raise ValueError(f'Local source directory is missing or escapes the mount: {item}')
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
        except OSError as exc:
            raise ValueError(f'Port {port} is already in use; choose another') from exc
    pins = json.loads((ROOT / 'images.lock.json').read_text())
    build_key = hashlib.sha256((ROOT / 'docker/pcg.Dockerfile').read_bytes() + (ROOT / 'docker/bigtable.Dockerfile').read_bytes() + (ROOT / 'upstream.lock.json').read_bytes()).hexdigest()[:12]
    viewer = ROOT / '.runtime/viewer'
    if not (viewer / 'index.html').exists():
        raise ValueError('Pinned viewer is missing; run bin/cave build first')
    meta.update(datastack=graph, aligned_volume=graph + '_volume', project_name=project,
                source_dir=str(source) if source else None, atom_policy=meta.get('atom_policy', 'source_labels'),
                package_build=build_key, bigtable_project=meta.get('bigtable_project', project))
    compose = yaml.safe_load((ROOT / 'compose.yml').read_text())
    compose['name'] = project
    capacity = available_cpus()
    cpu_limits = {}
    for name, service in compose['services'].items():
        if 'cpus' in service:
            declared = service['cpus']
            try:
                limit = float(declared)
            except (TypeError, ValueError) as error:
                raise ValueError(f'Template service {name} has an invalid CPU quota') from error
            if isinstance(declared, bool) or not math.isfinite(limit) or limit < 0.01:
                raise ValueError(f'Template service {name} has an invalid CPU quota')
            effective = min(limit, float(capacity))
            service['cpus'] = int(effective) if effective.is_integer() else effective
            cpu_limits[name] = service['cpus']
        service.pop('build', None)
        service['image'] = 'cave-local-' + name + ':' + build_key if name in ('bigtable', 'pcg') else pins[name]
        env = service.get('environment', {})
        env.update(CAVE_LOCAL_URL=meta['local_url'], CAVE_DATASTACK=graph, CAVE_ALIGNED_VOLUME=meta['aligned_volume'])
        service['environment'] = env
        if name == 'pcg':
            env.update(BIGTABLE_PROJECT=meta['bigtable_project'], BIGTABLE_INSTANCE=graph, PROJECT_ID=meta['bigtable_project'], PCG_GRAPH_IDS=graph)
            env.pop('CAVE_UI_SANDBOX', None)
            service['volumes'] = ['./config:/workspace/config:ro', './scripts:/workspace/scripts:ro', './data:/data', './deployment.json:/workspace/deployment.json:ro']
            if source:
                service['volumes'].append({'type': 'bind', 'source': str(source), 'target': '/source', 'read_only': True})
            service['healthcheck']['test'][-1] = service['healthcheck']['test'][-1].replace('/table/brain2/', '/table/' + graph + '/')
        elif name == 'gateway':
            service['ports'] = [f'127.0.0.1:{port}:80']
            service['volumes'] = ['./config/nginx.conf:/etc/nginx/conf.d/default.conf:ro', './data:/data:ro', './web:/web:ro', {'type': 'bind', 'source': str(viewer), 'target': '/viewer', 'read_only': True}]
            if source:
                service['volumes'].append({'type': 'bind', 'source': str(source), 'target': '/source', 'read_only': True})
        elif name in ('info', 'schema', 'annotation', 'materialize', 'mat-worker'):
            service['volumes'] = ['./config:/workspace/config:ro', './scripts:/workspace/scripts:ro', './data:/data', './secrets:/root/.cloudvolume/secrets:ro', './deployment.json:/workspace/deployment.json:ro']
    meta.update(docker_cpu_capacity=capacity, service_cpu_limits=cpu_limits)
    for folder in ('config', 'scripts', 'web', 'data', 'secrets'):
        (dest / folder).mkdir(parents=True)
    (dest / 'deployment.json').write_text(json.dumps(meta, indent=2) + '\n')
    (dest / 'compose.yml').write_text(yaml.safe_dump(compose, sort_keys=False))
    envfile = dest / '.env'
    envfile.write_text('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32) + '\nCAVE_SESSION_SECRET=' + secrets.token_urlsafe(32) + '\n')
    envfile.chmod(0o600)
    token = dest / 'secrets/cave-secret.json'
    token.write_text(json.dumps({'token': '00000000000000000000000000000000'}) + '\n')
    token.chmod(0o600)
    for name in ('sitecustomize.py', 'wsgi.py', 'celery_app.py', 'local_mesh.py', 'source_label_policy.py', 'database_connections.py', 'segmentation_metadata.py'):
        shutil.copyfile(ROOT / 'scripts' / name, dest / 'scripts' / name)
    shutil.copyfile(ROOT / 'scripts/ingest_segmentation_iteration.py', dest / 'scripts/ingest.py')
    for name in ('empty.ini', 'pcg.py', 'common.py'):
        shutil.copyfile(ROOT / 'config' / name, dest / 'config' / name)
    init = (ROOT / 'scripts/init-databases.sh').read_text().replace('brain2_volume', meta['aligned_volume'])
    (dest / 'scripts/init-databases.sh').write_text(init)
    template = ROOT / 'templates/segmentation-iteration'
    for name in ('bootstrap_info.py', 'bootstrap_mat.py'):
        shutil.copyfile(template / 'scripts' / name, dest / 'scripts' / name)
    nginx = (template / 'config/nginx.conf').read_text()
    image = urlsplit(meta['image_https'])
    if image.scheme == 'file':
        image_location = f'alias {image.path.rstrip("/")}/;'
    else:
        image_location = f'proxy_pass {meta["image_https"].rstrip("/")}/; proxy_ssl_server_name on; proxy_set_header Host {image.netloc}; proxy_hide_header Access-Control-Allow-Origin;'
    (dest / 'config/nginx.conf').write_text(nginx.replace('__IMAGE_LOCATION__', image_location))
    from configure_iteration_viewer import configure
    configure(dest)
    print('Created', dest, 'project', project, 'graph', graph, flush=True)
    return dest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--source-dir', type=Path)
    parser.add_argument('--project-name')
    args = parser.parse_args()
    create(args.manifest, args.destination, args.source_dir, args.project_name)
