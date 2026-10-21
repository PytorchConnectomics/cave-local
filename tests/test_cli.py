"""User-facing commands fail clearly and keep operations inside their project.

Docker and package managers are intercepted at the subprocess boundary. Source
fixtures, manifest parsing, HTTP readiness and command-line exits are real.
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cli


def invoke(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["cave", *map(str, arguments)])
    return cli.main()


@pytest.fixture
def saved_deployment(tmp_path):
    target = tmp_path / "deployment with spaces"
    target.mkdir()
    values = {"project_name": "cave-pkg-cli-example", "graph_id": "cli_example",
              "local_url": "http://127.0.0.1:18081", "test_fixture": False}
    (target / "deployment.json").write_text(json.dumps(values))
    (target / "compose.yml").write_text(yaml.safe_dump({"name": values["project_name"], "services": {}}))
    (target / ".env").write_text("unused-disposable-test-env\n")
    (target / "data").mkdir()
    return target


@pytest.fixture
def external_calls(monkeypatch):
    calls = []
    def run(arguments, **kwargs):
        calls.append(([str(argument) for argument in arguments], kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cli, "run", run)
    return calls


@pytest.mark.parametrize("command,tail", [("stop", ["stop"]), ("status", ["ps"]),
                                        ("logs", ["logs", "--tail", "100"])])
def test_scoped_operation_uses_only_its_own_compose_and_env(monkeypatch, saved_deployment, external_calls, command, tail):
    invoke(monkeypatch, command, saved_deployment)
    assert external_calls == [(["docker", "compose", "--env-file", str(saved_deployment / ".env"),
                               "-f", str(saved_deployment / "compose.yml"), *tail], {})]


@pytest.mark.parametrize("command", ["start", "stop", "status", "logs", "validate"])
@pytest.mark.parametrize("identity", ["unrelated-project", "cave-pkg-different"])
def test_identity_mismatch_prevents_every_external_operation(monkeypatch, saved_deployment, external_calls, command, identity):
    (saved_deployment / "compose.yml").write_text(yaml.safe_dump({"name": identity, "services": {}}))
    with pytest.raises(ValueError, match="project identity"):
        invoke(monkeypatch, command, saved_deployment)
    assert external_calls == []


def test_default_validation_is_read_only_and_cannot_edit_real_data(monkeypatch, saved_deployment, external_calls):
    invoke(monkeypatch, "validate", saved_deployment)
    assert external_calls == [([sys.executable, str(ROOT / "scripts/snapshot.py"), str(saved_deployment)], {})]
    external_calls.clear()
    with pytest.raises(ValueError, match="only allowed.*disposable fixtures"):
        invoke(monkeypatch, "validate", saved_deployment, "--edit-fixture", "--browser")
    assert external_calls == []


def test_browser_flag_requires_explicit_fixture_editing_before_reading_a_deployment(monkeypatch, tmp_path, external_calls):
    def forbidden_read(path):
        pytest.fail("ambiguous browser validation must not read or operate on a deployment")
    monkeypatch.setattr(cli, "deployment", forbidden_read)
    missing = tmp_path / "unopened deployment"
    with pytest.raises(ValueError, match="browser edit acceptance requires --edit-fixture"):
        invoke(monkeypatch, "validate", missing, "--browser")
    assert external_calls == []
    # Verify the installed command also exposes the useful error instead of a
    # missing-file traceback or a silently successful snapshot-only check.
    result = subprocess.run([sys.executable, ROOT / "scripts/cli.py", "validate", missing, "--browser"],
                            text=True, capture_output=True)
    assert result.returncode == 1
    assert "CAVE: Automated browser edit acceptance requires --edit-fixture" in result.stderr
    assert "Traceback" not in result.stderr
    assert not missing.exists()


def test_fixture_validation_runs_native_checks_before_browser_checks(monkeypatch, saved_deployment, external_calls):
    path = saved_deployment / "deployment.json"
    values = json.loads(path.read_text())
    values["test_fixture"] = True
    path.write_text(json.dumps(values))
    invoke(monkeypatch, "validate", saved_deployment, "--edit-fixture", "--browser")
    assert [Path(call[0][1]).name for call in external_calls] == ["verify_segmentation_iteration.py", "verify_iteration_browser.py"]
    assert all(call[0][2] == str(saved_deployment) for call in external_calls)


@pytest.mark.parametrize("command,arguments,translated", [
    ("backup", ["/tmp/installation with spaces", "/tmp/backup with spaces", "--include-source"], "backup"),
    ("verify-backup", ["/tmp/backup"], "verify"),
    ("restore", ["/tmp/backup", "/tmp/new installation", "--port", "18082", "--source-dir", "/tmp/input data"], "restore"),
    ("teardown", ["/tmp/installation", "--confirm", "cave-pkg-exact-target"], "teardown"),
    ("rotate-secrets", ["/tmp/installation"], "rotate-secrets"),
])
def test_operations_preserve_arguments_without_shell_interpolation(monkeypatch, external_calls, command, arguments, translated):
    invoke(monkeypatch, command, *arguments)
    assert external_calls == [([sys.executable, str(ROOT / "scripts/operations.py"), translated, *arguments], {})]


def test_create_dispatch_preserves_optional_source_and_project(monkeypatch):
    import create_segmentation_deployment
    calls = []
    monkeypatch.setattr(create_segmentation_deployment, "create", lambda *args: calls.append(args))
    invoke(monkeypatch, "create", "manifest with spaces.json", "deployment with spaces",
           "--source-dir", "input data", "--project-name", "cave-pkg-cli")
    assert calls == [("manifest with spaces.json", "deployment with spaces", "input data", "cave-pkg-cli")]


def test_fixture_dispatch_preserves_variant_and_explicit_editor_basis(monkeypatch, external_calls):
    invoke(monkeypatch, "fixture", "new source", "--variant", "xyz", "--atom-policy", "spatial_tiles",
           "--graph-id", "new_iteration", "--port", "18083")
    assert external_calls == [([sys.executable, str(ROOT / "scripts/make_fixture.py"), "new source",
        "--variant", "xyz", "--atom-policy", "spatial_tiles", "--graph-id", "new_iteration", "--port", "18083"], {})]


def test_start_bootstraps_before_ingesting_and_reports_viewer_after_readiness(monkeypatch, saved_deployment, capsys):
    timeline = []
    monkeypatch.setattr(cli, "run", lambda args, **kwargs: timeline.append(("run", [str(arg) for arg in args])))
    monkeypatch.setattr(cli, "wait_http", lambda url: timeline.append(("ready", url)))
    import configure_iteration_viewer
    monkeypatch.setattr(configure_iteration_viewer, "configure", lambda path: timeline.append(("viewer", path)))
    cli.start(saved_deployment)
    assert timeline[0][1][-2:] == ["up", "-d"]
    assert timeline[1] == ("ready", "http://127.0.0.1:18081/info/health")
    assert timeline[2][1][-1] == "/workspace/scripts/bootstrap_info.py"
    assert timeline[3][1][-1] == "/workspace/scripts/bootstrap_mat.py"
    assert timeline[4][1][-1] == "/workspace/scripts/ingest.py"
    assert timeline[5] == ("ready", "http://127.0.0.1:18081/segmentation/table/cli_example/info")
    assert timeline[6] == ("viewer", saved_deployment)
    assert timeline[7][1][-1] == "ps"
    assert "Viewer: http://127.0.0.1:18081" in capsys.readouterr().out


def test_failed_ingestion_does_not_reconfigure_or_announce_success(monkeypatch, saved_deployment, capsys):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if str(args[-1]) == "/workspace/scripts/ingest.py":
            raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr(cli, "wait_http", lambda url: None)
    import configure_iteration_viewer
    monkeypatch.setattr(configure_iteration_viewer, "configure", lambda path: pytest.fail("failed install announced as ready"))
    with pytest.raises(subprocess.CalledProcessError):
        cli.start(saved_deployment)
    assert calls[-1][-1] == "/workspace/scripts/ingest.py"
    assert "Viewer:" not in capsys.readouterr().out


@pytest.fixture
def healthy_doctor(monkeypatch, external_calls):
    monkeypatch.setattr(cli, "available_cpus", lambda: 4)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/fixture/bin/" + name)
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(cli.subprocess, "check_output", lambda *args, **kwargs: "v22.18.0\n")
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda path: SimpleNamespace(free=25 * 1024**3))
    return external_calls


def test_doctor_confirms_supported_host_and_read_only_docker_access(healthy_doctor):
    report = cli.doctor()
    assert report["node"] == "v22.18.0" and report["free_disk_gib"] == 25
    assert report["docker_cpu_capacity"] == 4
    assert [call[0] for call in healthy_doctor] == [["docker", "info", "--format", "{{.ServerVersion}}"], ["docker", "compose", "version"]]


@pytest.mark.parametrize("problem,reason", [
    ("missing_docker", "Install prerequisites: docker"),
    ("unsupported_os", "Linux x86-64"),
    ("unsupported_arch", "Linux x86-64"),
    ("old_node", "Node >=22.18"),
    ("low_disk", "20 GiB free"),
])
def test_doctor_fails_with_actionable_boundary_and_deps_do_not_install(monkeypatch, healthy_doctor, problem, reason):
    if problem == "missing_docker":
        monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "docker" else "/fixture/" + name)
    elif problem == "unsupported_os":
        monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    elif problem == "unsupported_arch":
        monkeypatch.setattr(cli.platform, "machine", lambda: "arm64")
    elif problem == "old_node":
        monkeypatch.setattr(cli.subprocess, "check_output", lambda *args, **kwargs: "v22.17.0\n")
    else:
        monkeypatch.setattr(cli.shutil, "disk_usage", lambda path: SimpleNamespace(free=19 * 1024**3))
    with pytest.raises(RuntimeError, match=reason):
        invoke(monkeypatch, "deps")
    assert not any("pip" in call[0] or "venv" in call[0] for call in healthy_doctor)


def test_deps_creates_a_repo_local_environment_and_installs_the_exact_lock(monkeypatch, healthy_doctor, tmp_path):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    invoke(monkeypatch, "deps")
    assert healthy_doctor[-2][0] == [sys.executable, "-m", "venv", str(tmp_path / ".venv")]
    assert healthy_doctor[-1][0] == [str(tmp_path / ".venv/bin/python"), "-m", "pip", "install", "-r", str(tmp_path / "requirements.lock.txt")]


def test_deps_reuses_existing_environment_instead_of_replacing_it(monkeypatch, healthy_doctor, tmp_path):
    environment = tmp_path / ".venv"
    environment.mkdir()
    sentinel = environment / "existing-environment-marker"
    sentinel.write_text("preserve")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    invoke(monkeypatch, "deps")
    assert not any("venv" in call[0] for call in healthy_doctor)
    assert healthy_doctor[-1][0][0] == str(environment / "bin/python")
    assert sentinel.read_text() == "preserve"


@pytest.mark.parametrize("arguments,expected", [([], (False, False)),
    (["--no-cache"], (True, False)), (["--skip-viewer"], (False, True)),
    (["--no-cache", "--skip-viewer"], (True, True))])
def test_build_command_retains_explicit_cache_and_viewer_options(monkeypatch, arguments, expected):
    calls = []
    monkeypatch.setattr(cli, "build", lambda *args: calls.append(args))
    invoke(monkeypatch, "build", *arguments)
    assert calls == [expected]


class ReadyHandler(http.server.BaseHTTPRequestHandler):
    statuses = []
    requests = 0

    def do_GET(self):
        type(self).requests += 1
        status = type(self).statuses.pop(0) if len(type(self).statuses) > 1 else type(self).statuses[0]
        self.send_response(status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("statuses,should_pass", [([503, 200], True), ([503], False)])
def test_readiness_requires_actual_http_200_and_has_a_bounded_failure(monkeypatch, statuses, should_pass):
    class Handler(ReadyHandler):
        pass
    Handler.statuses = list(statuses)
    Handler.requests = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    clock = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    try:
        url = f"http://127.0.0.1:{server.server_port}/health"
        if should_pass:
            cli.wait_http(url, timeout=5)
            assert Handler.requests == 2
        else:
            with pytest.raises(RuntimeError, match="did not become ready.*inspect bin/cave logs"):
                cli.wait_http(url, timeout=5)
            assert Handler.requests == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_command_line_exits_nonzero_with_a_clear_message_before_creating_invalid_target(tmp_path):
    manifest = tmp_path / "invalid.json"
    manifest.write_text(json.dumps({"graph_id": "../../wrong"}))
    output = tmp_path / "must-not-exist"
    result = subprocess.run([sys.executable, ROOT / "scripts/cli.py", "create", manifest, output],
                            text=True, capture_output=True)
    assert result.returncode == 1
    assert "CAVE: graph_id must begin" in result.stderr
    assert "Traceback" not in result.stderr
    assert not output.exists()


def test_cli_fixture_command_generates_real_variant_and_editor_metadata(tmp_path):
    output = tmp_path / "new source with spaces"
    result = subprocess.run([sys.executable, ROOT / "scripts/cli.py", "fixture", output, "--variant", "xyz",
        "--atom-policy", "spatial_tiles", "--graph-id", "cli_fixture", "--port", "18084"], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    values = json.loads((output / "manifest.json").read_text())
    assert values["graph_id"] == "cli_fixture" and values["shape_xyz"] == [40, 48, 24]
    assert values["atom_policy"] == "spatial_tiles" and values["atom_tile_xyz"] == [8, 8, 4]
    assert values["local_url"] == "http://127.0.0.1:18084"
    assert (output / "segmentation/info").exists() and (output / "image/0/.zarray").exists()


@pytest.mark.parametrize("arguments", [[], ["unknown-command"], ["fixture", "/tmp/unused", "--variant", "tzyx"]])
def test_argument_errors_exit_before_any_operation(arguments):
    result = subprocess.run([sys.executable, ROOT / "scripts/cli.py", *arguments], text=True, capture_output=True)
    assert result.returncode == 2
    assert "usage:" in result.stderr and "error:" in result.stderr


def test_doctor_failure_has_a_clean_user_facing_exit_without_installing(tmp_path):
    env = dict(os.environ, PATH=str(tmp_path / "no-binaries"))
    result = subprocess.run([sys.executable, ROOT / "scripts/cli.py", "doctor"], env=env, text=True, capture_output=True)
    assert result.returncode == 1
    assert "CAVE: Install prerequisites: docker, git, node, npm" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("has_local_environment", [False, True])
def test_shell_entrypoint_selects_its_environment_and_preserves_argument_boundaries(tmp_path, has_local_environment):
    package = tmp_path / "package with spaces"
    (package / "bin").mkdir(parents=True)
    (package / "scripts").mkdir()
    launcher = package / "bin/cave"
    shutil.copyfile(ROOT / "bin/cave", launcher)
    fallback = tmp_path / "chosen python"
    fallback.write_text("#!/bin/sh\nprintf 'fallback\\n'\nprintf '%s\\n' \"$@\"\n")
    fallback.chmod(0o755)
    if has_local_environment:
        local = package / ".venv/bin/python"
        local.parent.mkdir(parents=True)
        local.write_text("#!/bin/sh\nprintf 'local\\n'\nprintf '%s\\n' \"$@\"\n")
        local.chmod(0o755)
    result = subprocess.run(["bash", launcher, "status", "deployment with spaces"],
        env=dict(os.environ, CAVE_PYTHON=str(fallback)), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["local" if has_local_environment else "fallback",
                                          str(package / "scripts/cli.py"), "status", "deployment with spaces"]


def test_refinement_validation_and_repair_are_deployment_scoped(monkeypatch, saved_deployment, external_calls):
    path = saved_deployment / 'deployment.json'
    meta = json.loads(path.read_text())
    with pytest.raises(ValueError, match='not enabled'):
        invoke(monkeypatch, 'svsplit', 'repair', saved_deployment)
    meta.update(test_fixture=True, supervoxel_split={'enabled': True})
    path.write_text(json.dumps(meta))
    invoke(monkeypatch, 'validate', saved_deployment, '--edit-fixture')
    assert external_calls.pop()[0] == [sys.executable, str(ROOT / 'scripts/verify_supervoxel_split.py'), str(saved_deployment)]
    invoke(monkeypatch, 'svsplit', 'repair', saved_deployment)
    command = external_calls.pop()[0]
    assert command[:6] == ['docker', 'compose', '--env-file', str(saved_deployment / '.env'), '-f', str(saved_deployment / 'compose.yml')]
    assert command[6:] == ['exec', '-T', 'pcg', 'python', '/workspace/scripts/supervoxel_refine.py', '--repair', '--graph-id', meta['graph_id']]


@pytest.mark.parametrize('retained', ['journal', 'generation', 'broken_generation'])
def test_repair_remains_available_after_disabling_new_refinements(monkeypatch, saved_deployment, external_calls, retained):
    if retained == 'journal':
        path = saved_deployment / 'data/supervoxel_splits/journal/12/journal.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"state":"verify_failed"}')
    else:
        tables = saved_deployment / 'data/atom_tables'
        tables.mkdir()
        if retained == 'generation':
            (tables / '1').mkdir()
        (tables / 'current').symlink_to('1', target_is_directory=True)
    invoke(monkeypatch, 'svsplit', 'repair', saved_deployment)
    assert external_calls[0][0][6:] == [
        'exec', '-T', 'pcg', 'python', '/workspace/scripts/supervoxel_refine.py',
        '--repair', '--graph-id', 'cli_example']
