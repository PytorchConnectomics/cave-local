"""Create actual installations without running Docker or touching source data."""
import hashlib
import json
import shutil
import socket
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import create_segmentation_deployment as creator


def unused_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def manifest(port=None):
    return {"graph_id": "dataset_iteration_1", "local_url": f"http://127.0.0.1:{port or unused_port()}",
            "image_https": "file:///source/image", "segmentation_https": "file:///source/segmentation",
            "shape_xyz": [40, 48, 24], "resolution_nm_xyz": [12, 16, 40],
            "graph_chunk_xyz": [16, 16, 8], "atom_policy": "source_labels"}


@pytest.fixture
def package(tmp_path, monkeypatch):
    repo = tmp_path / "package"
    repo.mkdir()
    for directory in ("config", "scripts", "templates", "docker"):
        shutil.copytree(ROOT / directory, repo / directory, ignore=shutil.ignore_patterns("__pycache__"))
    for filename in ("compose.yml", "images.lock.json", "upstream.lock.json"):
        shutil.copyfile(ROOT / filename, repo / filename)
    viewer = repo / ".runtime/viewer"
    viewer.mkdir(parents=True)
    (viewer / "index.html").write_text("fixture-built-viewer")
    monkeypatch.setattr(creator, "ROOT", repo)
    monkeypatch.setattr(creator, "available_cpus", lambda: 8)
    return repo


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "input data"
    for directory in ("image", "segmentation"):
        (source / directory).mkdir(parents=True)
        (source / directory / "original-sentinel").write_text("Original inputs are immutable.\n")
    return source


def directory_digest(path):
    return {str(child.relative_to(path)): hashlib.sha256(child.read_bytes()).hexdigest()
            for child in path.rglob("*") if child.is_file()}


def write_manifest(tmp_path, values):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(values))
    return path


def test_generated_config_is_independent_loopback_and_keeps_original_bytes(package, source, tmp_path):
    before = directory_digest(source)
    values = manifest()
    first = creator.create(write_manifest(tmp_path, values), tmp_path / "deployment one", source)
    second_values = dict(values, local_url=f"http://localhost:{unused_port()}")
    second = creator.create(write_manifest(tmp_path, second_values), tmp_path / "deployment two", source)
    deployments = []
    passwords = []
    for target in (first, second):
        spec = json.loads((target / "deployment.json").read_text())
        config = yaml.safe_load((target / "compose.yml").read_text())
        project = spec["project_name"]
        assert config["name"] == project and project.startswith("cave-pkg-")
        assert all("build" not in service for service in config["services"].values())
        ports = [(name, service["ports"]) for name, service in config["services"].items() if "ports" in service]
        port = values["local_url"].rsplit(":", 1)[1] if target == first else second_values["local_url"].rsplit(":", 1)[1]
        assert ports == [("gateway", [f"127.0.0.1:{port}:80"])]
        for name in ("pcg", "gateway"):
            bindings = [entry for entry in config["services"][name]["volumes"] if isinstance(entry, dict)]
            mount = next(entry for entry in bindings if entry["target"] == "/source")
            assert mount == {"type": "bind", "source": str(source.resolve()), "target": "/source", "read_only": True}
        env_lines = (target / ".env").read_text().splitlines()
        assert {line.split("=", 1)[0] for line in env_lines} == {"POSTGRES_PASSWORD", "CAVE_SESSION_SECRET"}
        passwords.append([line.split("=", 1)[1] for line in env_lines])
        assert all(len(value) >= 32 for value in passwords[-1])
        assert (target / ".env").stat().st_mode & 0o777 == 0o600
        assert (target / "secrets/cave-secret.json").stat().st_mode & 0o777 == 0o600
        # An adapter update must ship into each new installation, including the
        # database recovery and linked-table concurrency fixes.
        for adapter in ("database_connections.py", "segmentation_metadata.py", "source_label_policy.py"):
            assert (target / "scripts" / adapter).read_bytes() == (package / "scripts" / adapter).read_bytes()
        deployments.append(project)
    assert deployments[0] != deployments[1]
    assert set(passwords[0]).isdisjoint(passwords[1])
    assert directory_digest(source) == before


def test_explicit_restored_namespace_does_not_change_with_new_project(package, source, tmp_path):
    values = dict(manifest(), bigtable_project="cave-pkg-original-namespace")
    target = creator.create(write_manifest(tmp_path, values), tmp_path / "restored", source,
                            project_name="cave-pkg-restored-copy")
    spec = json.loads((target / "deployment.json").read_text())
    config = yaml.safe_load((target / "compose.yml").read_text())
    assert spec["project_name"] == "cave-pkg-restored-copy"
    assert spec["bigtable_project"] == "cave-pkg-original-namespace"
    assert config["services"]["pcg"]["environment"]["BIGTABLE_PROJECT"] == "cave-pkg-original-namespace"
    assert config["services"]["pcg"]["environment"]["PROJECT_ID"] == "cave-pkg-original-namespace"


def test_public_http_sources_generate_proxy_without_a_host_data_mount(package, tmp_path):
    values = dict(manifest(), image_https="https://example.org/data/image/",
                  segmentation_https="https://example.org/data/labels/")
    target = creator.create(write_manifest(tmp_path, values), tmp_path / "http-deployment")
    config = yaml.safe_load((target / "compose.yml").read_text())
    for name in ("pcg", "gateway"):
        assert not any(isinstance(entry, dict) and entry.get("target") == "/source"
                       for entry in config["services"][name]["volumes"])
    nginx = (target / "config/nginx.conf").read_text()
    assert "proxy_pass https://example.org/data/image/;" in nginx
    assert "proxy_set_header Host example.org;" in nginx
    assert "proxy_ssl_server_name on;" in nginx
    assert "__IMAGE_LOCATION__" not in nginx


@pytest.mark.parametrize("case", ["missing_mount", "missing_image", "escaping_symlink", "invalid_project", "missing_viewer"])
def test_preflight_failure_creates_nothing_and_preserves_sources(package, source, tmp_path, case):
    values = manifest()
    supplied_source = source
    project = None
    if case == "missing_mount":
        supplied_source = None
    elif case == "missing_image":
        values["image_https"] = "file:///source/missing"
    elif case == "escaping_symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "private-data").write_text("do not touch")
        (source / "escaped").symlink_to(outside, target_is_directory=True)
        values["image_https"] = "file:///source/escaped"
    elif case == "invalid_project":
        project = "another-existing-project"
    else:
        (package / ".runtime/viewer/index.html").unlink()
    before = directory_digest(source)
    target = tmp_path / "rejected"
    with pytest.raises(ValueError):
        creator.create(write_manifest(tmp_path, values), target, supplied_source, project)
    assert not target.exists()
    assert directory_digest(source) == before


@pytest.mark.parametrize("field,value", [
    ("local_url", "http://" + "user:" + "password@127.0.0.1:18181"),
    ("local_url", "http://127.0.0.1:18181/path"),
    ("local_url", "http://127.0.0.1:18181?redirect=true"),
    ("local_url", "https://127.0.0.1:18181"),
    ("local_url", "http://127.0.0.1:80"),
    ("graph_chunk_xyz", [16, True, 8]),
    ("shape_xyz", [48, 40.0, 20]),
    ("resolution_nm_xyz", [8, True, 24]),
    ("image_https", "https://example.org/data;proxy_pass_bad"),
    ("image_https", "https://example.org/data\nother-directive"),
    ("image_https", "file://remote/source/image"),
    ("segmentation_https", "http://localhost:18889/labels"),
    ("segmentation_https", "https://" + "user:" + "password@example.org/labels"),
])
def test_manifest_rejects_unsafe_addresses_and_ambiguous_numeric_types(tmp_path, source, field, value):
    values = dict(manifest(), **{field: value})
    before = directory_digest(source)
    with pytest.raises(ValueError):
        creator.create(write_manifest(tmp_path, values), tmp_path / "rejected", source)
    assert not (tmp_path / "rejected").exists()
    assert directory_digest(source) == before


def test_existing_destination_symlink_preserves_its_real_target(package, source, tmp_path):
    real = tmp_path / "existing"
    real.mkdir()
    sentinel = real / "saved-edit"
    sentinel.write_text("preserve me")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="already exists"):
        creator.create(write_manifest(tmp_path, manifest()), alias, source)
    assert sentinel.read_text() == "preserve me"
    assert sorted(path.name for path in real.iterdir()) == ["saved-edit"]
