#!/usr/bin/env python3
"""Cold backups, isolated restoration, and scoped retirement for packaged CAVE.

Only deployments whose Compose project starts with ``cave-pkg-`` are accepted.
All Docker calls carry the deployment's explicit project name. No global prune,
host file deletion, graph renaming, or in-place database upgrade is performed.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
FORMAT = 1
ARCHIVE_IMAGE = json.loads((ROOT / "images.lock.json").read_text())["alpine:3.22"]
VOLUME_TARGETS = {"bigtable": "/data", "postgres": "/var/lib/postgresql/data", "redis": "/data"}
WORKSPACE_ITEMS = ("data", "config", "scripts", "web", ".env", "secrets")
PROJECT_RE = re.compile(r"cave-pkg-[a-z0-9][a-z0-9-]*\Z")
ATOM_TABLE_ALIASES = ("atom_index.npy", "atom_bounds.npy", "initial_roots.npy")


def progress(message):
    print(message, file=sys.stderr, flush=True)


def run(command, cwd=None):
    result = subprocess.run([str(arg) for arg in command], cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip()[-4000:]
        raise RuntimeError(f"Command failed ({result.returncode}): {command[0]} {command[1] if len(command) > 1 else ''}\n{detail}")
    return result.stdout


def run_to_file(command, destination):
    # Stream binary tar output through a host-owned file; a root Docker
    # process writing a bind-mounted archive would create unmanageable files.
    with Path(destination).open("xb") as handle:
        os.chmod(destination, 0o600)
        result = subprocess.run([str(arg) for arg in command], stdout=handle, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError("Archive copy failed: " + result.stderr.decode(errors="replace")[-4000:])


def compose(deployment, *arguments):
    return run(["docker", "compose", "--project-name", deployment["project"],
                "--project-directory", deployment["path"], "-f", deployment["path"] / "compose.yml", *arguments])


def checked_project(project):
    if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
        raise ValueError("Refusing a project outside the package's cave-pkg-* namespace.")
    return project


def load_deployment(path):
    path = Path(path).resolve()
    spec = json.loads((path / "deployment.json").read_text())
    project = checked_project(spec.get("project_name"))
    deployment = {"path": path, "spec": spec, "project": project}
    config = json.loads(compose(deployment, "config", "--format", "json"))
    if config.get("name") != project:
        raise ValueError("Resolved Compose project does not match deployment.json.")
    services = config.get("services", {})
    volumes = {}
    for service, target in VOLUME_TARGETS.items():
        mounts = [mount for mount in services.get(service, {}).get("volumes", [])
                  if mount.get("type") == "volume" and mount.get("target") == target]
        if len(mounts) != 1:
            raise ValueError(f"Expected one private persistent {service} volume at {target}.")
        logical = mounts[0]["source"]
        definition = config.get("volumes", {}).get(logical, {})
        name = definition.get("name")
        if definition.get("external") or not name or name != project + "_" + logical:
            raise ValueError(f"Refusing external or unexpectedly named volume for {service}.")
        volumes[service] = {"name": name, "logical": logical}
    if set(config.get("volumes", {})) != {record["logical"] for record in volumes.values()}:
        raise ValueError("Unexpected additional persistent volumes; only the package's three stores are supported.")
    deployment.update(config=config, volumes=volumes)
    return deployment


def volume_info(name):
    result = subprocess.run(["docker", "volume", "inspect", name], capture_output=True, text=True)
    if result.returncode:
        # Distinguish a missing volume from a failed daemon connection.
        if "no such volume" in result.stderr.lower():
            return None
        raise RuntimeError("Cannot inspect Docker volume: " + result.stderr.strip())
    records = json.loads(result.stdout)
    if len(records) != 1:
        raise ValueError("Unexpected Docker volume inspection result.")
    return records[0]


def owned_volumes(deployment):
    for service, record in deployment["volumes"].items():
        info = volume_info(record["name"])
        labels = (info.get("Labels") or {}) if info else {}
        if not info or labels.get("com.docker.compose.project") != deployment["project"] or labels.get("com.docker.compose.volume") != record["logical"]:
            raise ValueError(f"{service} volume is missing or is not owned by this deployment.")


def running_services(deployment):
    raw = compose(deployment, "ps", "--all", "--format", "json").strip()
    if not raw:
        return []
    records = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines()]
    for record in records:
        labels = record.get("Labels", {})
        oneoff = labels.get("com.docker.compose.oneoff", "false") if isinstance(labels, dict) else "true" if "com.docker.compose.oneoff=True" in labels else "false"
        if record.get("State") == "running" and (str(record.get("OneOff", oneoff)).lower() in {"true", "1"}):
            raise ValueError("A one-off setup or editing container is running; finish it before backup or restore.")
    return sorted({record["Service"] for record in records
                   if record.get("State") == "running" and record.get("Service") in deployment["config"]["services"]})


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def private_tree(path):
    for item in [Path(path), *Path(path).rglob("*")]:
        if item.is_symlink():
            raise ValueError(f"Backup cannot contain a symlink: {item.name}")
        os.chmod(item, 0o700 if item.is_dir() else 0o600)


def known_atom_link(member):
    """Only publication's relative, deployment-local table pointers are portable."""
    if not member.issym():
        return False
    if member.name == "data/atom_tables/current":
        return re.fullmatch(r"[0-9]+", member.linkname) is not None
    return any(member.name == "data/" + name
               and member.linkname == "atom_tables/current/" + name
               for name in ATOM_TABLE_ALIASES)


def tar_add(archive, path, arcname, allow_atom_links=False):
    def portable(info):
        if info.issym() and allow_atom_links and known_atom_link(info):
            # Check the actual source as well as the archive spelling: a generation
            # directory must not itself redirect the known pointers outside data/.
            data = Path(path).resolve(strict=True)
            source = data.parent / info.name
            try:
                target = source.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError(f"Invalid atom-table link: {info.name}") from error
            directory = info.name == "data/atom_tables/current"
            if data not in target.parents or not (target.is_dir() if directory else target.is_file()):
                raise ValueError(f"Atom-table link escapes data or has no valid target: {info.name}")
        elif not (info.isfile() or info.isdir()):
            raise ValueError(f"Portable backup refuses links and special files: {info.name}")
        return info
    archive.add(path, arcname=arcname, filter=portable)


def tar_source(archive, source, graph_id):
    directory = archive.gettarinfo(source, "source")
    safe_member(directory)
    archive.addfile(directory)
    for item in sorted(source.iterdir()):
        if item.name == "manifest.json" and item.is_file() and not item.is_symlink():
            try:
                metadata = json.loads(item.read_text())
            except (ValueError, UnicodeDecodeError):
                metadata = None
            if isinstance(metadata, dict) and metadata.get("graph_id") == graph_id:
                # A generated fixture may carry its former host mount location.
                # Preserve dataset choices, while excluding runtime host paths.
                metadata = {key: value for key, value in metadata.items()
                            if key not in {"source_dir", "repo_dir", "deployment_dir"}}
                payload = (json.dumps(metadata, indent=2) + "\n").encode()
                info = archive.gettarinfo(item, "source/manifest.json")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
                continue
        tar_add(archive, item, "source/" + item.name)


def snapshot(deployment):
    helper = ROOT / "scripts" / "snapshot.py"
    if not helper.is_file():
        raise RuntimeError("scripts/snapshot.py is required for a verified online backup.")
    wait_ready(deployment)
    with tempfile.TemporaryDirectory(prefix="cave-observation-") as scratch:
        result_path = Path(scratch) / "snapshot.json"
        run([sys.executable, helper, deployment["path"], "--output", result_path])
        return json.loads(result_path.read_text())


def wait_ready(deployment, timeout=240):
    base = deployment["spec"]["local_url"].rstrip("/")
    paths = ["/info/health", "/schema/versions", "/annotation/api/versions", "/materialize/api/versions",
             "/segmentation/table/" + deployment["spec"]["graph_id"] + "/info"]
    deadline = time.monotonic() + timeout
    while True:
        try:
            for path in paths:
                with urllib.request.urlopen(base + path, timeout=5) as response:
                    if response.status != 200:
                        raise RuntimeError("Unexpected readiness response.")
            return
        except (OSError, urllib.error.URLError, RuntimeError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Deployment did not become ready for read-only state checks; inspect its logs.")
            time.sleep(2)


def normalized_snapshot(observation):
    # The snapshot helper owns stable fields. It may add these display-only fields.
    return {key: value for key, value in observation.items()
            if key not in {"local_url", "deployment", "project_name", "captured_at", "snapshot_time"}}


def backup(deployment_path, archive_path, include_source=False, offline=False):
    deployment = load_deployment(deployment_path)
    archive_path = Path(archive_path).resolve()
    if archive_path.exists():
        raise ValueError("Backup destination already exists; choose a new archive directory.")
    if archive_path == deployment["path"] or deployment["path"] in archive_path.parents:
        raise ValueError("Put backups outside the deployment directory.")
    owned_volumes(deployment)
    running = running_services(deployment)
    if offline and running:
        raise ValueError("--offline requires every service in this deployment to be stopped.")
    if not offline and not running:
        raise ValueError("Start the deployment for state checks, or use --offline for a cold backup without API comparison.")
    source = None
    if include_source:
        source = Path(deployment["spec"].get("source_dir", "")).resolve()
        if not deployment["spec"].get("source_dir") or not source.is_dir():
            raise ValueError("This deployment has no local source directory to include.")
        if source == archive_path or source in archive_path.parents:
            raise ValueError("The archive cannot be inside its source directory.")
    # Pull the archive tool before stopping CAVE, so a failed download causes no downtime.
    run(["docker", "image", "inspect", ARCHIVE_IMAGE]) if image_exists(ARCHIVE_IMAGE) else run(["docker", "pull", ARCHIVE_IMAGE])
    observations = normalized_snapshot(snapshot(deployment)) if not offline else None
    archive_path.mkdir(parents=True, mode=0o700)
    complete = False
    try:
        if running:
            progress("Stopping this deployment's running services for a consistent copy.")
            compose(deployment, "stop", "--timeout", "60", *running)
        if running_services(deployment):
            raise RuntimeError("A service restarted during backup. Stop the other operator and retry.")
        portable_spec = {key: value for key, value in deployment["spec"].items()
                         if key not in {"source_dir", "project_name", "repo_dir", "deployment_dir"}}
        # Compose isolation can change; the database's table namespace cannot.
        portable_spec["bigtable_project"] = deployment["spec"].get("bigtable_project", deployment["project"])
        (archive_path / "deployment.json").write_text(json.dumps(portable_spec, indent=2) + "\n")
        with tarfile.open(archive_path / "workspace.tar.gz", "w:gz") as archive:
            for name in WORKSPACE_ITEMS:
                path = deployment["path"] / name
                if path.exists():
                    tar_add(archive, path, name, allow_atom_links=name == "data")
        if source:
            progress("Copying the local input data; large datasets can take time.")
            with tarfile.open(archive_path / "source.tar.gz", "w:gz") as archive:
                tar_source(archive, source, portable_spec["graph_id"])
        for service, record in deployment["volumes"].items():
            progress(f"Copying {service} persistent storage.")
            run_to_file(["docker", "run", "--rm", "--network", "none", "--read-only",
                         "--mount", f"type=volume,src={record['name']},dst=/volume,readonly",
                         ARCHIVE_IMAGE, "tar", "czf", "-", "-C", "/volume", "."],
                        archive_path / (service + ".tar.gz"))
        complete = True
    finally:
        if running:
            progress("Restarting exactly the services that were running before backup.")
            compose(deployment, "start", *running)
        private_tree(archive_path)
    if complete and observations is not None:
        after = snapshot(deployment)
        if normalized_snapshot(observations) != normalized_snapshot(after):
            raise RuntimeError("Graph or annotation state changed while backing up. No complete backup manifest was written; pause editing and background jobs, then retry in a new directory.")
    manifest = {
        "format": FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "graph_id": portable_spec["graph_id"],
        "source_included": bool(source),
        "archive_image": ARCHIVE_IMAGE,
        "running_services": running,
        "observations": observations,
        "persistent_service_images": {name: deployment["config"]["services"][name].get("image")
                                      for name in VOLUME_TARGETS},
        "sha256": {item.name: checksum(item) for item in sorted(archive_path.iterdir()) if item.is_file()},
    }
    try:
        manifest["repo_commit"] = run(["git", "-C", ROOT, "rev-parse", "HEAD"]).strip()
    except RuntimeError:
        manifest["repo_commit"] = None
    (archive_path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    private_tree(archive_path)
    verify(archive_path)
    return {"backup": str(archive_path), "complete": True, "state_checked": observations is not None,
            "services_restarted": running, "contains_credentials": True}


def image_exists(name):
    return subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode == 0


def safe_member(member, allow_atom_links=False):
    path = PurePosixPath(member.name)
    allowed = member.isfile() or member.isdir() or (allow_atom_links and known_atom_link(member))
    if path.is_absolute() or ".." in path.parts or not allowed:
        raise ValueError(f"Unsafe backup archive member: {member.name}")
    return member


def safe_members(archive, allow_atom_links=False):
    """Validate all entries before extraction, including paths traversing links."""
    members = archive.getmembers()
    by_path = {}
    for member in members:
        safe_member(member, allow_atom_links=allow_atom_links)
        path = PurePosixPath(member.name)
        if path in by_path:
            raise ValueError(f"Duplicate backup archive member: {member.name}")
        by_path[path] = member
    for path, member in by_path.items():
        if any(parent in by_path and not by_path[parent].isdir() for parent in path.parents):
            raise ValueError(f"Unsafe backup archive member beneath a non-directory: {member.name}")
        if not member.issym():
            continue
        target = path.parent / member.linkname
        # An array alias traverses the current-generation pointer. Resolve that
        # one allowed intermediate link against archive entries, never host files.
        current = PurePosixPath("data/atom_tables/current")
        if current in target.parents and current in by_path and by_path[current].issym():
            target = current.parent / by_path[current].linkname / target.name
        resolved = by_path.get(target)
        directory = path == current
        if (not target.is_relative_to(PurePosixPath("data")) or resolved is None
                or not (resolved.isdir() if directory else resolved.isfile())):
            raise ValueError(f"Unsafe atom-table link target: {member.name}")
    return members


def inspect_tar(path, allow_atom_links=False):
    with tarfile.open(path, "r:gz") as archive:
        safe_members(archive, allow_atom_links=allow_atom_links)


def verify(archive_path):
    archive_path = Path(archive_path).resolve()
    if not archive_path.is_dir():
        raise ValueError("Backup directory does not exist.")
    if (archive_path / "manifest.json").is_symlink():
        raise ValueError("Backup manifest cannot be a symlink.")
    manifest = json.loads((archive_path / "manifest.json").read_text())
    if manifest.get("format") != FORMAT:
        raise ValueError("Unsupported backup format.")
    expected = manifest.get("sha256", {})
    required = {"deployment.json", "workspace.tar.gz", *(service + ".tar.gz" for service in VOLUME_TARGETS)}
    if manifest.get("source_included"):
        required.add("source.tar.gz")
    if set(expected) != required:
        raise ValueError("Backup manifest is missing required files or contains unexpected files.")
    actual = {item.name for item in archive_path.iterdir()}
    if actual != required | {"manifest.json"}:
        raise ValueError("Backup contains missing or unrecorded files.")
    for name, digest in expected.items():
        if PurePosixPath(name).name != name or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Malformed backup checksum entry.")
        path = archive_path / name
        if path.is_symlink() or not path.is_file() or checksum(path) != digest:
            raise ValueError(f"Backup checksum mismatch: {name}")
        if name.endswith(".tar.gz"):
            inspect_tar(path, allow_atom_links=name == "workspace.tar.gz")
    spec = json.loads((archive_path / "deployment.json").read_text())
    if spec.get("graph_id") != manifest.get("graph_id"):
        raise ValueError("Backup graph identity does not match its manifest.")
    return {"backup": str(archive_path), "checksums_verified": True, "state_checked": manifest.get("observations") is not None}


def extract_tar(source, destination, allow_atom_links=False):
    with tarfile.open(source, "r:gz") as archive:
        members = safe_members(archive, allow_atom_links=allow_atom_links)
        destination = Path(destination)
        # Restore always extracts into a new private scratch directory. Reject
        # pre-existing entries so an on-disk link cannot bypass archive checks.
        if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
            raise ValueError("Backup extraction requires an empty destination.")
        archive.extractall(destination, members=members)


def runtime_hashes(directory):
    """Identify the effective adapters/viewer configuration without dumping content."""
    directory = Path(directory)
    hashes = {}
    for name in ("config", "scripts", "web"):
        for path in sorted((directory / name).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                hashes[path.relative_to(directory).as_posix()] = checksum(path)
    return hashes


def restore_workspace_state(workspace, destination):
    """Restore state while retaining the candidate's manifest-generated runtime."""
    for name in ("data", ".env", "secrets"):
        source = workspace / name
        target = destination / name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
        elif source.is_file():
            shutil.copy2(source, target)
    if (destination / ".env").is_file():
        os.chmod(destination / ".env", 0o600)
    if (destination / "secrets").is_dir():
        private_tree(destination / "secrets")
    provenance = destination / "provenance" / "archived-runtime"
    provenance.mkdir(parents=True, mode=0o700)
    for name in ("config", "scripts", "web"):
        source = workspace / name
        if source.is_dir():
            shutil.copytree(source, provenance / name)
    private_tree(destination / "provenance")
    return provenance


def restore(archive_path, destination, port, graph_id=None, source_dir=None):
    archive_path, destination = Path(archive_path).resolve(), Path(destination).resolve()
    verify(archive_path)  # No Docker state or destination files change before integrity checks.
    manifest = json.loads((archive_path / "manifest.json").read_text())
    spec = json.loads((archive_path / "deployment.json").read_text())
    if destination.exists():
        raise ValueError("Restore requires a new destination directory, never an existing deployment.")
    if not 1024 <= port <= 65535:
        raise ValueError("Choose an unprivileged port from 1024 through 65535.")
    if graph_id and graph_id != spec["graph_id"]:
        raise ValueError("Restore preserves graph and datastack identities; use a separate dataset installation to create a new graph.")
    original_postgres = manifest.get("persistent_service_images", {}).get("postgres")
    current_postgres = json.loads((ROOT / "images.lock.json").read_text())["postgres"]
    if original_postgres and original_postgres != current_postgres:
        raise ValueError("Backup PostgreSQL image differs from this repo's pin. Use the recorded repo version; database-image upgrades need a separately tested migration.")
    project = checked_project("cave-pkg-" + spec["graph_id"].replace("_", "-") + "-restore-" + hashlib.sha256(str(destination).encode()).hexdigest()[:10])
    # Predict all target volumes before create. Never overwrite even an orphaned volume.
    for service in VOLUME_TARGETS:
        if volume_info(project + "_" + service):
            raise ValueError("A target restore volume already exists; choose another destination.")
    if source_dir:
        source_dir = Path(source_dir).resolve()
        if not source_dir.is_dir():
            raise ValueError("--source-dir must name an existing local input directory.")
    elif not manifest.get("source_included") and any(str(spec.get(key, "")).startswith("file://")
                                                   for key in ("image_https", "segmentation_https")):
        raise ValueError("This backup excludes input data. Supply --source-dir with the original unchanged input directory.")
    spec["local_url"] = f"http://127.0.0.1:{port}"
    created_volumes = []
    with tempfile.TemporaryDirectory(prefix="cave-restore-") as scratch:
        scratch = Path(scratch)
        workspace = scratch / "workspace"
        workspace.mkdir()
        extract_tar(archive_path / "workspace.tar.gz", workspace, allow_atom_links=True)
        if source_dir is None and manifest.get("source_included"):
            # Keep included input beside the restored deployment, so temporary extraction can disappear.
            source_dir = destination.with_name(destination.name + "-source")
            if source_dir.exists():
                raise ValueError("The restore's included-source directory already exists; choose another destination.")
            extracted = scratch / "input"
            extracted.mkdir()
            extract_tar(archive_path / "source.tar.gz", extracted)
            included = extracted / "source"
            if not included.is_dir() or set(extracted.iterdir()) != {included}:
                raise ValueError("Included source archive has an unexpected root.")
            source_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(included, source_dir)
        spec_path = scratch / "deployment.json"
        spec_path.write_text(json.dumps(spec, indent=2) + "\n")
        progress("Generating a new isolated deployment with the original graph identity.")
        create_command = [ROOT / "bin" / "cave", "create", spec_path, destination, "--project-name", project]
        if source_dir:
            create_command += ["--source-dir", source_dir]
        run(create_command)
        deployment = load_deployment(destination)
        provenance = restore_workspace_state(workspace, destination)
        progress("Keeping the current repository's generated runtime; archived runtime is retained privately for reference. Hand-edited runtime settings are outside automatic restoration; supported settings come from the manifest.")
        for record in deployment["volumes"].values():
            if volume_info(record["name"]):
                raise ValueError("A target volume appeared during restore. Nothing was overwritten.")
        # Docker's `volume create` is idempotent: a different operator can create
        # a volume after our inspection, and Docker will return it unchanged.
        # Claim and verify every destination with a fresh attempt label before
        # writing any archive bytes. Project/logical labels alone also match an
        # old orphan from the same deployment, so they are insufficient here.
        restore_attempt = secrets.token_hex(16)
        attempt_label = "org.pytorchconnectomics.cave-local.restore-attempt"
        for service, record in deployment["volumes"].items():
            run(["docker", "volume", "create", "--label", "com.docker.compose.project=" + project,
                 "--label", "com.docker.compose.volume=" + record["logical"],
                 "--label", attempt_label + "=" + restore_attempt, record["name"]])
            info = volume_info(record["name"])
            labels = (info.get("Labels") or {}) if info else {}
            if (labels.get("com.docker.compose.project") != project
                    or labels.get("com.docker.compose.volume") != record["logical"]
                    or labels.get(attempt_label) != restore_attempt):
                raise ValueError("A target restore volume is not owned by this restore attempt. Nothing was overwritten.")
            created_volumes.append(record["name"])
        for service, record in deployment["volumes"].items():
            progress(f"Restoring {service} into its new private volume.")
            run(["docker", "run", "--rm", "--network", "none", "--read-only",
                 "--mount", f"type=volume,src={record['name']},dst=/volume",
                 "--mount", f"type=bind,src={archive_path},dst=/backup,readonly",
                 ARCHIVE_IMAGE, "tar", "xzf", "/backup/" + service + ".tar.gz", "-C", "/volume"])
        progress("Starting the restored copy and checking its saved graph and annotations.")
        run([ROOT / "bin" / "cave", "start", destination])
        observed = snapshot(deployment)
        state_checked = manifest.get("observations") is not None
        if state_checked and normalized_snapshot(observed) != normalized_snapshot(manifest["observations"]):
            raise RuntimeError("Restored graph, edits, or annotation state differs from the backup. The separate restored copy remains for investigation; the original deployment was not changed.")
        current_hashes = runtime_hashes(destination)
        archived_hashes = runtime_hashes(provenance)
        try:
            current_revision = run(["git", "-C", ROOT, "rev-parse", "HEAD"]).strip()
        except RuntimeError:
            current_revision = None
        report = {"backup_checksums_verified": True, "state_compared_to_backup": state_checked,
                  "project_name": project, "graph_id": spec["graph_id"], "local_url": spec["local_url"],
                  "observations": observed, "volumes": created_volumes,
                  "effective_runtime_repo_commit": current_revision, "backup_repo_commit": manifest.get("repo_commit"),
                  "effective_runtime_sha256": current_hashes, "archived_runtime_sha256": archived_hashes,
                  "runtime_files_changed_from_backup": sorted(name for name in set(current_hashes) | set(archived_hashes)
                                                            if current_hashes.get(name) != archived_hashes.get(name)),
                  "archived_runtime_path": "provenance/archived-runtime",
                  "manual_runtime_customizations_supported": False}
        (destination / "restore-report.json").write_text(json.dumps(report, indent=2) + "\n")
        private_tree(destination / "secrets")
        os.chmod(destination / ".env", 0o600)
        return {"restored": str(destination), **report}


def teardown(deployment_path, confirmation):
    deployment = load_deployment(deployment_path)
    if confirmation != deployment["project"]:
        raise ValueError("--confirm must be the exact project_name from deployment.json.")
    # Missing volumes are allowed on a second invocation, foreign volumes are never allowed.
    for record in deployment["volumes"].values():
        info = volume_info(record["name"])
        if info:
            labels = info.get("Labels") or {}
            if labels.get("com.docker.compose.project") != deployment["project"] or labels.get("com.docker.compose.volume") != record["logical"]:
                raise ValueError("Refusing to remove a volume owned by another project.")
    progress("Removing only this project's containers, network, and graph/annotation volumes.")
    compose(deployment, "down", "--volumes", "--timeout", "60")
    return {"removed_project": deployment["project"], "deployment_files_preserved": True,
            "input_data_preserved": True, "backups_preserved": True}


def private_write(path, content):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def postgres_password(deployment, password):
    role = deployment["config"]["services"]["postgres"]["environment"].get("POSTGRES_USER", "cave")
    quoted_role = '"' + role.replace('"', '""') + '"'
    # Feed SQL via stdin, never password argv/env or shell substitutions. Any
    # stderr can echo the SQL, so failure messages intentionally exclude it.
    sql = "ALTER ROLE " + quoted_role + " PASSWORD '" + password.replace("'", "''") + "';\n"
    command = ["docker", "compose", "--project-name", deployment["project"], "--project-directory", deployment["path"],
               "-f", deployment["path"] / "compose.yml", "exec", "-T", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1",
               "-U", role, "-d", "postgres"]
    result = subprocess.run([str(part) for part in command], input=sql, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("PostgreSQL password change failed; secret-bearing diagnostics were suppressed.")


def rotate_secrets(deployment_path):
    deployment = load_deployment(deployment_path)
    owned_volumes(deployment)
    running = running_services(deployment)
    if "postgres" not in running:
        raise ValueError("Start this deployment before rotating its credentials.")
    before = normalized_snapshot(snapshot(deployment))
    envfile = deployment["path"] / ".env"
    if envfile.is_symlink():
        raise ValueError("The deployment .env must be a regular private file before rotation.")
    original = envfile.read_bytes()
    lines = original.decode().splitlines()
    values = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if key in {"POSTGRES_PASSWORD", "CAVE_SESSION_SECRET"} and separator:
            if key in values:
                raise ValueError("Duplicate credential entries in .env; resolve them before rotation.")
            values[key] = value
    if set(values) != {"POSTGRES_PASSWORD", "CAVE_SESSION_SECRET"} or any(not value for value in values.values()):
        raise ValueError(".env must contain both generated credentials before rotation.")
    # The package generates unquoted dotenv values. Refuse ambiguous manually
    # quoted values rather than guessing the password that is actually active.
    if any(value.startswith(("'", '"')) for value in values.values()):
        raise ValueError("Quoted .env credentials need an explicit migration before automatic rotation.")
    replacements = {key: secrets.token_urlsafe(32) for key in values}
    updated = ("\n".join(key + "=" + replacements[key] if (key := line.partition("=")[0]) in replacements else line
                         for line in lines) + "\n").encode()
    recovery = deployment["path"] / "secrets" / ("rotation-before-" + secrets.token_hex(8) + ".env")
    applications = [name for name in running if name not in VOLUME_TARGETS]
    if not applications:
        raise ValueError("Start the application services before rotating their credentials.")
    private_write(recovery, original)
    phase = "stopping applications"
    stopped = False
    try:
        progress("Stopping this deployment's application services for credential rotation.")
        stopped = True
        compose(deployment, "stop", "--timeout", "60", *applications)
        phase = "changing the database password"
        postgres_password(deployment, replacements["POSTGRES_PASSWORD"])
        phase = "updating the private environment file"
        private_write(envfile, updated)
        phase = "recreating applications with the new credentials"
        compose(deployment, "up", "-d", "--no-deps", "--force-recreate", *applications)
        phase = "checking saved state"
        after = normalized_snapshot(snapshot(deployment))
        if after != before:
            raise RuntimeError("Saved state changed during credential rotation.")
    except (Exception, KeyboardInterrupt):
        progress("Credential rotation failed; restoring the original credentials.")
        recovered = False
        try:
            postgres_password(deployment, values["POSTGRES_PASSWORD"])
            private_write(envfile, original)
            if stopped:
                compose(deployment, "up", "-d", "--no-deps", "--force-recreate", *applications)
            recovered = normalized_snapshot(snapshot(deployment)) == before
        except (Exception, KeyboardInterrupt):
            pass
        report = {"project_name": deployment["project"], "rotated": False, "failed_phase": phase,
                  "original_credentials_and_state_restored": recovered, "private_recovery_file": str(recovery)}
        (deployment["path"] / "rotation-report.json").write_text(json.dumps(report, indent=2) + "\n")
        if recovered:
            raise RuntimeError("Credential rotation failed at " + phase + "; original credentials and saved state were restored. See rotation-report.json.") from None
        raise RuntimeError("Credential rotation failed at " + phase + "; automatic recovery could not be verified. The private original .env is saved under secrets/; inspect rotation-report.json before further changes.") from None
    report = {"project_name": deployment["project"], "rotated": True, "database_password_rotated": True,
              "session_secret_rotated": True, "state_unchanged": True, "services_recreated": applications,
              "private_recovery_file": str(recovery)}
    (deployment["path"] / "rotation-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("backup", help="Stop, copy, and restart only the selected deployment.")
    p.add_argument("deployment", type=Path)
    p.add_argument("archive", type=Path)
    p.add_argument("--include-source", action="store_true", help="Include immutable local input data; may be very large.")
    p.add_argument("--offline", action="store_true", help="Back up an already stopped deployment without API state comparison.")
    p = sub.add_parser("verify", help="Check every recorded checksum and reject unsafe archive members.")
    p.add_argument("archive", type=Path)
    p = sub.add_parser("restore", help="Restore into new files and new private volumes, preserving graph identity.")
    p.add_argument("archive", type=Path)
    p.add_argument("destination", type=Path)
    p.add_argument("--graph-id", help="Optional assertion of the original graph identity; renaming is not supported.")
    p.add_argument("--port", required=True, type=int)
    p.add_argument("--source-dir", type=Path)
    p = sub.add_parser("teardown", help="Delete only the chosen project's containers and persistent volumes.")
    p.add_argument("deployment", type=Path)
    p.add_argument("--confirm", required=True, help="Exact project_name; persistent edits and annotations will be deleted.")
    p = sub.add_parser("rotate-secrets", help="Rotate database/session credentials with saved-state checks and rollback.")
    p.add_argument("deployment", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            result = backup(args.deployment, args.archive, args.include_source, args.offline)
        elif args.command == "verify":
            result = verify(args.archive)
        elif args.command == "restore":
            result = restore(args.archive, args.destination, args.port, args.graph_id, args.source_dir)
        elif args.command == "teardown":
            result = teardown(args.deployment, args.confirm)
        else:
            result = rotate_secrets(args.deployment)
        print(json.dumps(result, indent=2))
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError, tarfile.TarError) as error:
        print(f"CAVE operation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
