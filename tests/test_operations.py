"""Backup integrity and isolation tests; live restore acceptance is separate."""

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import supervoxel_history as history


LOADER = importlib.util.spec_from_file_location("operations", Path(__file__).resolve().parents[1] / "scripts" / "operations.py")
operations = importlib.util.module_from_spec(LOADER)
LOADER.loader.exec_module(operations)


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name)
        self.deployment = self.path / "deployment"
        self.deployment.mkdir()
        (self.deployment / "data").mkdir()
        (self.deployment / "data" / "atom_index.npy").write_bytes(b"test input")
        (self.deployment / ".env").write_text("POSTGRES_PASSWORD=only-in-private-archive\n")
        (self.deployment / "secrets").mkdir()
        self.source = self.path / "original-source"
        self.source.mkdir()
        (self.source / "labels.bin").write_bytes(b"input labels")
        self.spec = {"graph_id": "fixture_one", "datastack": "fixture_one", "aligned_volume": "fixture_one_volume",
                     "local_url": "http://127.0.0.1:19000", "source_dir": str(self.source), "project_name": "cave-pkg-fixture-one"}
        self.project = self.spec["project_name"]
        (self.source / "manifest.json").write_text(json.dumps(self.spec))
        (self.deployment / "deployment.json").write_text(json.dumps(self.spec))
        self.loaded = {"path": self.deployment, "spec": self.spec, "project": self.project,
                       "config": {"name": self.project, "services": {name: {} for name in ("bigtable", "postgres", "redis", "pcg", "gateway")}},
                       "volumes": {name: {"name": self.project + "_" + name, "logical": name} for name in operations.VOLUME_TARGETS}}
        self.calls = []
        self.running = ["bigtable", "postgres", "redis", "pcg"]
        self.observation = {"root_sample_sha256": "roots", "annotations": [{"id": 1, "tag": "test"}], "versions": [1]}

    def fake_run(self, command, cwd=None):
        command = [str(part) for part in command]
        self.calls.append(command)
        if command[:2] == ["docker", "run"]:
            destination = next(part for part in command if part.startswith("type=bind,src="))
            directory = Path(destination.split("src=", 1)[1].split(",dst=", 1)[0])
            archive_name = next(part for part in command if part.startswith("/backup/"))
            with tarfile.open(directory / Path(archive_name).name, "w:gz") as archive:
                info = tarfile.TarInfo("state.db")
                payload = b"persistent state"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        if command[:2] == ["git", "-C"]:
            return "a" * 40
        return ""

    def fake_compose(self, deployment, *arguments):
        self.calls.append(["compose", deployment["project"], *arguments])
        if arguments[0] == "stop":
            self.running = [service for service in self.running if service not in arguments[3:]]
        if arguments[0] == "start":
            self.running = list(arguments[1:])
        if arguments[0] == "up":
            self.running = sorted(set(self.running) | set(arguments[4:]))
        return ""

    def fake_archive(self, command, destination):
        self.calls.append([str(part) for part in command])
        with tarfile.open(destination, "w:gz") as archive:
            info = tarfile.TarInfo("state.db")
            payload = b"persistent state"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    def make_backup(self, offline=False, source=True):
        archive = self.path / "archive"
        with patch.object(operations, "load_deployment", return_value=self.loaded), \
             patch.object(operations, "owned_volumes"), \
             patch.object(operations, "running_services", side_effect=lambda deployment: self.running.copy()), \
             patch.object(operations, "image_exists", return_value=True), \
             patch.object(operations, "run", side_effect=self.fake_run), \
             patch.object(operations, "run_to_file", side_effect=self.fake_archive), \
             patch.object(operations, "compose", side_effect=self.fake_compose), \
             patch.object(operations, "snapshot", return_value=self.observation):
            result = operations.backup(self.deployment, archive, include_source=source, offline=offline)
        return archive, result

    def test_backup_restarts_only_previously_running_services_and_scrubs_machine_paths(self):
        archive, result = self.make_backup()
        self.assertTrue(result["state_checked"])
        self.assertEqual(result["services_restarted"], ["bigtable", "postgres", "redis", "pcg"])
        self.assertEqual(self.running, result["services_restarted"])
        portable = json.loads((archive / "deployment.json").read_text())
        self.assertNotIn("source_dir", portable)
        self.assertNotIn("project_name", portable)
        self.assertNotIn(str(self.source), (archive / "manifest.json").read_text())
        with tarfile.open(archive / "source.tar.gz") as contents:
            included_metadata = contents.extractfile("source/manifest.json").read().decode()
            self.assertNotIn(str(self.source), included_metadata)
        self.assertEqual((archive / "manifest.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o700)
        self.assertTrue(operations.verify(archive)["checksums_verified"])

    def test_backup_restores_prior_service_state_after_copy_failure(self):
        original_running = self.running.copy()
        def failing_run(command, cwd=None):
            if command[:2] == ["docker", "run"]:
                raise RuntimeError("storage full")
            return self.fake_run(command, cwd)
        with patch.object(operations, "load_deployment", return_value=self.loaded), \
             patch.object(operations, "owned_volumes"), \
             patch.object(operations, "running_services", side_effect=lambda deployment: self.running.copy()), \
             patch.object(operations, "image_exists", return_value=True), \
             patch.object(operations, "run", side_effect=failing_run), \
             patch.object(operations, "run_to_file", side_effect=RuntimeError("storage full")), \
             patch.object(operations, "compose", side_effect=self.fake_compose), \
             patch.object(operations, "snapshot", return_value=self.observation):
            with self.assertRaisesRegex(RuntimeError, "storage full"):
                operations.backup(self.deployment, self.path / "incomplete")
        self.assertEqual(self.running, original_running)
        self.assertFalse((self.path / "incomplete" / "manifest.json").exists())

    def test_detects_corruption_before_restore_touches_docker(self):
        archive, _ = self.make_backup()
        with (archive / "postgres.tar.gz").open("ab") as handle:
            handle.write(b"corrupt")
        with patch.object(operations, "volume_info") as inspect, patch.object(operations, "run") as execute:
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                operations.restore(archive, self.path / "restored", 19001)
            inspect.assert_not_called()
            execute.assert_not_called()
        self.assertFalse((self.path / "restored").exists())

    def test_rejects_traversal_even_when_archive_checksum_is_correct(self):
        archive, _ = self.make_backup()
        payload = archive / "workspace.tar.gz"
        with tarfile.open(payload, "w:gz") as handle:
            member = tarfile.TarInfo("../../escape")
            member.size = 1
            handle.addfile(member, io.BytesIO(b"x"))
        manifest = json.loads((archive / "manifest.json").read_text())
        manifest["sha256"][payload.name] = operations.checksum(payload)
        (archive / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            operations.verify(archive)
        self.assertFalse((self.path.parent / "escape").exists())

    def test_rejects_symlink_archive_entries(self):
        archive, _ = self.make_backup()
        payload = archive / "workspace.tar.gz"
        with tarfile.open(payload, "w:gz") as handle:
            member = tarfile.TarInfo("data/outside")
            member.type = tarfile.SYMTYPE
            member.linkname = "/tmp"
            handle.addfile(member)
        manifest = json.loads((archive / "manifest.json").read_text())
        manifest["sha256"][payload.name] = operations.checksum(payload)
        (archive / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            operations.verify(archive)

    def test_backup_restore_preserves_published_refinement_and_history(self):
        data = self.deployment / "data"
        np.save(data / "atom_index.npy", np.array([[100, 1, 0, 0, 0]], np.uint64))
        np.save(data / "atom_bounds.npy", np.array([[0, 0, 0, 2, 1, 1]], np.uint64))
        np.save(data / "initial_roots.npy", np.array([10000], np.uint64))
        staged = history.stage_generation(
            data, new_index_rows=np.array([[101, 1, 0, 0, 0], [102, 1, 1, 0, 0]], np.uint64),
            new_bounds_rows=[[0, 0, 0, 1, 1, 1], [1, 0, 0, 2, 1, 1]],
            new_initial_roots=[10000, 10000],
            new_lineage_rows=[(101, 100, 1000, "refinement"), (102, 100, 1000, "refinement")],
            new_edge_map_rows=[], destination=self.path / "staged-generation")
        published = history.publish_generation(staged, data)
        archive, result = self.make_backup()
        self.assertTrue(result["complete"])
        self.assertTrue(operations.verify(archive)["checksums_verified"])
        workspace = self.path / "extracted-workspace"
        operations.extract_tar(archive / "workspace.tar.gz", workspace, allow_atom_links=True)
        target = self.path / "restored"
        target.mkdir()
        operations.restore_workspace_state(workspace, target)
        restored = history.load_atom_tables(target / "data")
        self.assertEqual(restored.generation, published.generation)
        self.assertEqual(restored.retired, {100})
        np.testing.assert_array_equal(restored.lineage, published.lineage)
        np.testing.assert_array_equal(history.ancestors_at([101, 102], 999, restored), [100, 100])
        np.testing.assert_array_equal(history.ancestors_at([101, 102], 1000, restored), [101, 102])
        for name in ("atom_tables/current", *operations.ATOM_TABLE_ALIASES):
            self.assertTrue((target / "data" / name).is_symlink())
            self.assertEqual(os.readlink(target / "data" / name), os.readlink(data / name))
            self.assertTrue((target / "data" / name).resolve().is_relative_to(target / "data"))
        self.assertTrue((target / "data" / "atom_tables" / "0" / "atom_index.npy").is_file())

    def test_atom_table_links_cannot_escape_data_or_traverse_archive_links(self):
        def member(name, kind=tarfile.REGTYPE, target=""):
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = target
            return info

        cases = {
            "absolute-generation": [member("data/atom_tables/current", tarfile.SYMTYPE, "/tmp")],
            "parent-generation": [member("data/atom_tables/current", tarfile.SYMTYPE, "../secrets")],
            "wrong-alias": [member("data/atom_index.npy", tarfile.SYMTYPE, "atom_tables/current/lineage.npy")],
            "missing-generation": [member("data/atom_tables/current", tarfile.SYMTYPE, "1")],
            "generation-link": [member("data/atom_tables/current", tarfile.SYMTYPE, "1"),
                                member("data/atom_tables/1", tarfile.SYMTYPE, "/tmp")],
            "child-through-current": [member("data/atom_tables/current", tarfile.SYMTYPE, "1"),
                                      member("data/atom_tables/1", tarfile.DIRTYPE),
                                      member("data/atom_tables/current/injected.npy")],
            "duplicate-link-path": [member("data/atom_tables/current", tarfile.SYMTYPE, "1"),
                                    member("data/atom_tables/1", tarfile.DIRTYPE),
                                    member("data/atom_tables/current", tarfile.DIRTYPE)],
            "link-in-source": [member("source/atom_tables/current", tarfile.SYMTYPE, "1")],
            "hard-link": [member("data/atom_index.npy", tarfile.LNKTYPE, "data/atom_tables/1/atom_index.npy")],
        }
        for name, members in cases.items():
            with self.subTest(name=name):
                archive = self.path / (name + ".tar.gz")
                destination = self.path / (name + "-extract")
                with tarfile.open(archive, "w:gz") as handle:
                    for info in members:
                        handle.addfile(info)
                with self.assertRaises(ValueError):
                    operations.extract_tar(archive, destination, allow_atom_links=True)
                self.assertFalse(destination.exists())

    def test_backup_rejects_known_pointer_redirected_outside_data(self):
        data = self.deployment / "data"
        tables = data / "atom_tables"
        tables.mkdir()
        outside = self.path / "outside-generation"
        outside.mkdir()
        (outside / "atom_index.npy").write_bytes(b"must not be included")
        (tables / "1").symlink_to(outside, target_is_directory=True)
        (tables / "current").symlink_to("1", target_is_directory=True)
        (data / "atom_index.npy").unlink()
        (data / "atom_index.npy").symlink_to("atom_tables/current/atom_index.npy")
        with self.assertRaisesRegex(ValueError, "escapes data"):
            self.make_backup()
        self.assertFalse((self.path / "archive" / "manifest.json").exists())

    def test_extraction_rejects_preexisting_target_symlink(self):
        archive = self.path / "safe.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.addfile(tarfile.TarInfo("data/value"))
        destination = self.path / "extract"
        destination.mkdir()
        (destination / "data").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "empty destination"):
            operations.extract_tar(archive, destination)
        self.assertFalse((self.source / "value").exists())
        empty = self.path / "empty"
        empty.mkdir()
        linked_destination = self.path / "linked-extract"
        linked_destination.symlink_to(empty, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "empty destination"):
            operations.extract_tar(archive, linked_destination)
        self.assertEqual(list(empty.iterdir()), [])

    def test_restore_cannot_overwrite_original_or_rename_its_graph(self):
        archive, _ = self.make_backup()
        with patch.object(operations, "run") as execute:
            with self.assertRaisesRegex(ValueError, "new destination"):
                operations.restore(archive, self.deployment, 19001)
            with self.assertRaisesRegex(ValueError, "preserves graph"):
                operations.restore(archive, self.path / "restored", 19001, graph_id="other")
            execute.assert_not_called()

    def test_restore_rejects_database_image_upgrade_before_creating_volumes(self):
        archive, _ = self.make_backup()
        manifest = json.loads((archive / "manifest.json").read_text())
        manifest["persistent_service_images"]["postgres"] = "postgres@sha256:" + "1" * 64
        (archive / "manifest.json").write_text(json.dumps(manifest))
        with patch.object(operations, "volume_info") as inspect, patch.object(operations, "run") as execute:
            with self.assertRaisesRegex(ValueError, "PostgreSQL image differs"):
                operations.restore(archive, self.path / "restored", 19001)
            inspect.assert_not_called()
            execute.assert_not_called()

    def test_unowned_volume_cannot_be_removed(self):
        with patch.object(operations, "load_deployment", return_value=self.loaded), \
             patch.object(operations, "volume_info", return_value={"Labels": {"com.docker.compose.project": "protected-live"}}), \
             patch.object(operations, "compose") as execute:
            with self.assertRaisesRegex(ValueError, "another project"):
                operations.teardown(self.deployment, self.project)
            execute.assert_not_called()

    def test_protected_original_project_rejected_before_docker(self):
        spec = dict(self.spec, project_name="cave-brain2")
        (self.deployment / "deployment.json").write_text(json.dumps(spec))
        with patch.object(operations, "run") as execute:
            with self.assertRaisesRegex(ValueError, "namespace"):
                operations.load_deployment(self.deployment)
            execute.assert_not_called()

    def test_offline_backup_requires_stopped_services_and_records_limited_evidence(self):
        self.running = []
        archive, result = self.make_backup(offline=True)
        self.assertFalse(result["state_checked"])
        self.assertFalse(operations.verify(archive)["state_checked"])
        self.assertFalse(any(call[0] == "compose" for call in self.calls))

    def test_busy_ingestion_container_prevents_a_cold_copy(self):
        raw = json.dumps({"Service": "pcg", "State": "running", "Labels": "com.docker.compose.oneoff=True"})
        with patch.object(operations, "compose", return_value=raw):
            with self.assertRaisesRegex(ValueError, "one-off"):
                operations.running_services(self.loaded)

    def test_secret_rotation_preserves_data_and_recreates_only_its_applications(self):
        original = b"POSTGRES_PASSWORD=old-password\nCAVE_SESSION_SECRET=old-session\n"
        (self.deployment / ".env").write_bytes(original)
        with patch.object(operations, "load_deployment", return_value=self.loaded), \
             patch.object(operations, "owned_volumes"), \
             patch.object(operations, "running_services", side_effect=lambda deployment: self.running.copy()), \
             patch.object(operations, "snapshot", return_value=self.observation), \
             patch.object(operations, "postgres_password") as password, \
             patch.object(operations.secrets, "token_urlsafe", side_effect=["new-password", "new-session"]), \
             patch.object(operations, "compose", side_effect=self.fake_compose):
            result = operations.rotate_secrets(self.deployment)
        self.assertTrue(result["state_unchanged"])
        self.assertEqual(result["services_recreated"], ["pcg"])
        self.assertEqual(set(self.running), {"bigtable", "postgres", "redis", "pcg"})
        password.assert_called_once_with(self.loaded, "new-password")
        self.assertEqual((self.deployment / ".env").read_text(), "POSTGRES_PASSWORD=new-password\nCAVE_SESSION_SECRET=new-session\n")
        self.assertEqual((self.deployment / ".env").stat().st_mode & 0o777, 0o600)
        recovery = Path(result["private_recovery_file"])
        self.assertEqual(recovery.read_bytes(), original)
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("new-password", json.dumps(result))
        self.assertNotIn("old-password", json.dumps(result))

    def test_rotation_failure_restores_credentials_and_checks_old_state(self):
        original = b"POSTGRES_PASSWORD=old-password\nCAVE_SESSION_SECRET=old-session\n"
        (self.deployment / ".env").write_bytes(original)
        failed = False
        def recreate_failure(deployment, *arguments):
            nonlocal failed
            if arguments[0] == "up" and not failed:
                failed = True
                raise RuntimeError("do-not-expose-new-password")
            return self.fake_compose(deployment, *arguments)
        with patch.object(operations, "load_deployment", return_value=self.loaded), \
             patch.object(operations, "owned_volumes"), \
             patch.object(operations, "running_services", side_effect=lambda deployment: self.running.copy()), \
             patch.object(operations, "snapshot", return_value=self.observation), \
             patch.object(operations, "postgres_password") as password, \
             patch.object(operations.secrets, "token_urlsafe", side_effect=["new-password", "new-session"]), \
             patch.object(operations, "compose", side_effect=recreate_failure):
            with self.assertRaisesRegex(RuntimeError, "original credentials and saved state were restored") as failure:
                operations.rotate_secrets(self.deployment)
        self.assertNotIn("do-not-expose", str(failure.exception))
        self.assertEqual((self.deployment / ".env").read_bytes(), original)
        self.assertEqual(password.call_args_list[1].args[1], "old-password")
        report = json.loads((self.deployment / "rotation-report.json").read_text())
        self.assertTrue(report["original_credentials_and_state_restored"])
        self.assertEqual(set(self.running), {"bigtable", "postgres", "redis", "pcg"})

    def test_password_sql_errors_cannot_echo_secret_input(self):
        self.loaded["config"]["services"]["postgres"]["environment"] = {"POSTGRES_USER": "cave"}
        class Failed:
            returncode = 1
            stderr = "ERROR at ALTER ROLE cave PASSWORD 'do-not-leak'"
        with patch.object(operations.subprocess, "run", return_value=Failed()) as execute:
            with self.assertRaises(RuntimeError) as failure:
                operations.postgres_password(self.loaded, "do-not-leak")
        self.assertNotIn("do-not-leak", str(failure.exception))
        self.assertNotIn("do-not-leak", " ".join(execute.call_args.args[0]))
        self.assertIn("do-not-leak", execute.call_args.kwargs["input"])

    def test_restore_runs_candidate_adapter_and_preserves_old_adapter_only_as_provenance(self):
        workspace = self.path / "archived-workspace"
        target = self.path / "candidate"
        for directory in (workspace, target):
            for name in ("config", "scripts", "web", "data", "secrets"):
                (directory / name).mkdir(parents=True)
        (workspace / "scripts" / "wsgi.py").write_text("raise RuntimeError('old adapter must never run')\n")
        (target / "scripts" / "wsgi.py").write_text("print('candidate adapter')\n")
        (workspace / "config" / "common.py").write_text("old config\n")
        (target / "config" / "common.py").write_text("candidate config\n")
        (workspace / "web" / "index.html").write_text("old launcher")
        (target / "web" / "index.html").write_text("candidate launcher")
        (workspace / "data" / "saved-edit.json").write_text('{"operation_id": 123}\n')
        (workspace / ".env").write_text("POSTGRES_PASSWORD=archived-credential\n")
        provenance = operations.restore_workspace_state(workspace, target)
        self.assertEqual((target / "scripts" / "wsgi.py").read_text(), "print('candidate adapter')\n")
        self.assertEqual((target / "config" / "common.py").read_text(), "candidate config\n")
        self.assertEqual((target / "web" / "index.html").read_text(), "candidate launcher")
        self.assertEqual((target / "data" / "saved-edit.json").read_bytes(), (workspace / "data" / "saved-edit.json").read_bytes())
        self.assertEqual((target / ".env").read_bytes(), (workspace / ".env").read_bytes())
        self.assertEqual((provenance / "scripts" / "wsgi.py").read_bytes(), (workspace / "scripts" / "wsgi.py").read_bytes())
        self.assertEqual(provenance.stat().st_mode & 0o777, 0o700)
        self.assertNotEqual(operations.runtime_hashes(target)["scripts/wsgi.py"], operations.runtime_hashes(provenance)["scripts/wsgi.py"])


if __name__ == "__main__":
    unittest.main()
