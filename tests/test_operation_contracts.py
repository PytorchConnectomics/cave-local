"""Lifecycle contracts with real files/archives and simulated Docker/API I/O.

These tests never contact Docker. They exercise complete backup/restore/rotation
transactions, including interruption and races, rather than only helper output.
The live acceptance runner remains responsible for validating real containers.
"""

from contextlib import ExitStack
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import operations


class LifecycleContracts(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="cave-operation-contract-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.deployment = self.root / "original"
        self.source = self.root / "immutable-source"
        self.source.mkdir()
        (self.source / "labels.bin").write_bytes(b"immutable input, never rewritten")
        self.spec = {
            "graph_id": "contract_fixture", "datastack": "contract_fixture",
            "aligned_volume": "contract_fixture_volume",
            "local_url": "http://127.0.0.1:19201",
            "source_dir": str(self.source), "project_name": "cave-pkg-contract-fixture",
            "image_https": "file:///source/image", "segmentation_https": "file:///source/labels",
        }
        self.original_environment = b"POSTGRES_PASSWORD=old-private-password\nCAVE_SESSION_SECRET=old-private-session\nEXTRA_SETTING=preserved\n"
        self.paths = {}
        self.install_workspace(self.deployment, self.spec, "original")
        self.running = ["bigtable", "postgres", "redis", "pcg", "gateway"]
        self.calls = []
        self.volume_labels = {}
        self.register_volumes(self.paths[self.deployment])
        self.observation = {
            "roots": {"72057594037927937": "288230376151712022"},
            "memberships": {"288230376151712022": ["72057594037927937"]},
            "annotations": [{"id": 1, "tag": "preserve"}],
            "materialization_versions": [1, 2], "native_edit_history": [{"operation_id": 5}],
        }
        self.images = json.loads((operations.ROOT / "images.lock.json").read_text())

    def install_workspace(self, path, spec, adapter):
        path.mkdir(parents=True)
        for name in ("data", "config", "scripts", "web", "secrets"):
            (path / name).mkdir()
        (path / "deployment.json").write_text(json.dumps(spec))
        (path / "compose.yml").write_text("name: " + spec["project_name"] + "\n")
        (path / ".env").write_bytes(self.original_environment)
        (path / "secrets" / "private.json").write_text('{"credential":"private-recovery-material"}')
        (path / "data" / "saved-state.json").write_text('{"operation_id":5}')
        for name in ("config", "scripts", "web"):
            (path / name / "runtime.txt").write_text(adapter + " " + name)
        self.paths[path] = {"path": path, "spec": spec, "project": spec["project_name"]}

    def config(self, deployment):
        project = deployment["project"]
        return {
            "name": project,
            "services": {
                **{name: {"image": self.images.get(name, "cave-local-bigtable:fixture-build"),
                           "environment": {"POSTGRES_USER": "cave"},
                           "volumes": [{"type": "volume", "source": name, "target": target}]}
                   for name, target in operations.VOLUME_TARGETS.items()},
                "pcg": {}, "gateway": {},
            },
            "volumes": {name: {"name": project + "_" + name} for name in operations.VOLUME_TARGETS},
        }

    def register_volumes(self, deployment):
        for name in operations.VOLUME_TARGETS:
            self.volume_labels[deployment["project"] + "_" + name] = {
                "Labels": {"com.docker.compose.project": deployment["project"],
                           "com.docker.compose.volume": name}}

    def compose_io(self, deployment, *arguments):
        self.calls.append(("compose", deployment["project"], arguments))
        if arguments[0] == "config":
            return json.dumps(self.config(deployment))
        if arguments[0] == "ps":
            return json.dumps([{"Service": service, "State": "running", "Labels": {}}
                               for service in self.running])
        if arguments[0] == "stop":
            self.running = [name for name in self.running if name not in arguments[3:]]
        if arguments[0] == "start":
            self.running = sorted(set(self.running) | set(arguments[1:]))
        if arguments[0] == "up":
            self.running = sorted(set(self.running) | set(arguments[4:]))
        if arguments[0] == "down":
            self.running = []
            for name in operations.VOLUME_TARGETS:
                self.volume_labels.pop(deployment["project"] + "_" + name, None)
        return ""

    def command_io(self, command, cwd=None):
        args = [str(part) for part in command]
        self.calls.append(("process", args))
        if args[:2] == ["docker", "volume"] and args[2] == "create":
            labels = {args[index + 1].split("=", 1)[0]: args[index + 1].split("=", 1)[1]
                      for index, item in enumerate(args) if item == "--label"}
            self.volume_labels.setdefault(args[-1], {"Labels": labels})
            return args[-1]
        if args[:2] == ["git", "-C"]:
            return "a" * 40
        if len(args) > 1 and args[1] == "create":
            spec = json.loads(Path(args[2]).read_text())
            spec["project_name"] = args[args.index("--project-name") + 1]
            if "--source-dir" in args:
                spec["source_dir"] = args[args.index("--source-dir") + 1]
            self.install_workspace(Path(args[3]), spec, "candidate")
        return ""

    def archive_io(self, command, destination):
        args = [str(part) for part in command]
        self.calls.append(("archive", args))
        self.write_tar(destination, "state.db", b"persistent store bytes")

    @staticmethod
    def write_tar(path, name, data=b"x", kind=tarfile.REGTYPE, linkname=""):
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.type, member.linkname = kind, linkname
            if member.isfile():
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            else:
                archive.addfile(member)

    def boundaries(self, **overrides):
        stack = ExitStack()
        defaults = {
            "compose": self.compose_io,
            "run": self.command_io,
            "run_to_file": self.archive_io,
            "volume_info": lambda name: copy.deepcopy(self.volume_labels.get(name)),
            "image_exists": lambda _name: True,
            "snapshot": lambda _deployment: copy.deepcopy(self.observation),
        }
        defaults.update(overrides)
        for name, implementation in defaults.items():
            stack.enter_context(patch.object(operations, name, side_effect=implementation))
        return stack

    def backup(self, include_source=True, offline=False):
        archive = self.root / "backup"
        with self.boundaries():
            result = operations.backup(self.deployment, archive, include_source, offline)
        return archive, result

    def update_checksum(self, archive, name):
        manifest = json.loads((archive / "manifest.json").read_text())
        manifest["sha256"][name] = operations.checksum(archive / name)
        (archive / "manifest.json").write_text(json.dumps(manifest))

    def mutations(self):
        return [call for call in self.calls if call[0] == "compose" and call[2][0] in {"stop", "start", "up", "down"}]

    def test_backup_compares_graph_annotations_materializations_and_native_history(self):
        for key, changed in [("roots", {"atom": "changed-root"}), ("annotations", []),
                             ("materialization_versions", [1]), ("native_edit_history", [])]:
            with self.subTest(state=key):
                archive = self.root / ("changed-" + key)
                later = copy.deepcopy(self.observation)
                later[key] = changed
                snapshots = iter([self.observation, later])
                with self.boundaries(snapshot=lambda _deployment: next(snapshots)):
                    with self.assertRaisesRegex(RuntimeError, "state changed"):
                        operations.backup(self.deployment, archive)
                self.assertFalse((archive / "manifest.json").exists())
                self.assertEqual(set(self.running), {"bigtable", "postgres", "redis", "pcg", "gateway"})

    def test_backup_ignores_only_display_metadata_in_state_comparison(self):
        before = {**self.observation, "captured_at": "before", "local_url": "old", "project_name": "old"}
        after = {**self.observation, "captured_at": "after", "local_url": "new", "project_name": "new"}
        snapshots = iter([before, after])
        with self.boundaries(snapshot=lambda _deployment: next(snapshots)):
            result = operations.backup(self.deployment, self.root / "display-only")
        self.assertTrue(result["complete"])
        self.assertTrue(result["state_checked"])

    def test_interrupted_or_partially_failed_stop_restarts_original_services(self):
        for failure in (KeyboardInterrupt, OSError):
            with self.subTest(failure=failure.__name__):
                self.running = ["bigtable", "postgres", "redis", "pcg"]
                failed = False

                def interrupted(deployment, *arguments):
                    nonlocal failed
                    result = self.compose_io(deployment, *arguments)
                    if arguments[0] == "stop" and not failed:
                        failed = True
                        raise failure("interrupted stop")
                    return result

                archive = self.root / failure.__name__
                with self.boundaries(compose=interrupted):
                    with self.assertRaises(failure):
                        operations.backup(self.deployment, archive)
                self.assertEqual(set(self.running), {"bigtable", "postgres", "redis", "pcg"})
                self.assertFalse((archive / "manifest.json").exists())
                self.assertEqual(archive.stat().st_mode & 0o777, 0o700)

    def test_archive_interrupt_retains_private_incomplete_evidence_and_restarts(self):
        def interrupted(command, destination):
            self.archive_io(command, destination)
            raise KeyboardInterrupt("operator cancellation")

        archive = self.root / "interrupted-copy"
        with self.boundaries(run_to_file=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                operations.backup(self.deployment, archive)
        self.assertFalse((archive / "manifest.json").exists())
        self.assertEqual(set(self.running), {"bigtable", "postgres", "redis", "pcg", "gateway"})
        for item in archive.rglob("*"):
            self.assertEqual(item.stat().st_mode & 0o777, 0o700 if item.is_dir() else 0o600)

    def test_restart_failure_never_writes_success_manifest(self):
        def failed_restart(deployment, *arguments):
            if arguments[0] == "start":
                raise RuntimeError("failed restart")
            return self.compose_io(deployment, *arguments)

        archive = self.root / "restart-failed"
        with self.boundaries(compose=failed_restart):
            with self.assertRaisesRegex(RuntimeError, "failed restart"):
                operations.backup(self.deployment, archive)
        self.assertFalse((archive / "manifest.json").exists())

    def test_download_and_initial_observation_failures_cause_no_downtime(self):
        def pull_failure(command, cwd=None):
            if list(command)[:2] == ["docker", "pull"]:
                raise RuntimeError("image download failed")
            return self.command_io(command, cwd)

        for label, overrides in [("pull", {"image_exists": lambda _name: False, "run": pull_failure}),
                                 ("snapshot", {"snapshot": lambda _deployment: (_ for _ in ()).throw(RuntimeError("API unavailable"))})]:
            with self.subTest(stage=label):
                self.calls.clear()
                destination = self.root / ("preflight-" + label)
                with self.boundaries(**overrides):
                    with self.assertRaises(RuntimeError):
                        operations.backup(self.deployment, destination)
                self.assertFalse(destination.exists())
                self.assertEqual(self.mutations(), [])

    def test_restarted_service_during_cold_copy_aborts_before_archiving(self):
        def concurrent_operator(deployment, *arguments):
            result = self.compose_io(deployment, *arguments)
            if arguments[0] == "stop":
                self.running = ["pcg"]
            return result

        archive = self.root / "concurrent-operator"
        with self.boundaries(compose=concurrent_operator):
            with self.assertRaisesRegex(RuntimeError, "service restarted"):
                operations.backup(self.deployment, archive)
        self.assertFalse(any(call[0] == "archive" for call in self.calls))
        self.assertFalse((archive / "manifest.json").exists())

    def test_offline_backup_rejects_any_running_service(self):
        with self.boundaries():
            with self.assertRaisesRegex(ValueError, "every service"):
                operations.backup(self.deployment, self.root / "offline", offline=True)
        self.assertEqual(self.mutations(), [])
        self.assertFalse((self.root / "offline").exists())

    def test_backup_reads_exact_private_stores_without_network_or_write_mounts(self):
        self.backup()
        commands = [call[1] for call in self.calls if call[0] == "archive"]
        self.assertEqual(len(commands), 3)
        for command in commands:
            self.assertEqual(command[:6], ["docker", "run", "--rm", "--network", "none", "--read-only"])
            mounts = [part for part in command if part.startswith("type=volume")]
            self.assertEqual(len(mounts), 1)
            self.assertTrue(mounts[0].startswith("type=volume,src=" + self.spec["project_name"] + "_"))
            self.assertTrue(mounts[0].endswith(",readonly"))
            self.assertFalse(any(part.startswith("type=bind") for part in command))
        self.assertEqual((self.source / "labels.bin").read_bytes(), b"immutable input, never rewritten")

    def test_backup_destinations_cannot_embed_or_overwrite_deployment_or_input(self):
        for destination in (self.deployment, self.deployment / "backups", self.source / "backup"):
            with self.subTest(destination=destination.name), self.boundaries():
                with self.assertRaises(ValueError):
                    operations.backup(self.deployment, destination, include_source=True)
        self.assertEqual(self.mutations(), [])

    def test_tar_verification_rejects_absolute_paths_links_and_special_files(self):
        archive, _ = self.backup()
        cases = [("/outside", tarfile.REGTYPE, ""), ("data/../../outside", tarfile.REGTYPE, ""),
                 ("hard", tarfile.LNKTYPE, "data/file"), ("symbol", tarfile.SYMTYPE, "data/file"),
                 ("fifo", tarfile.FIFOTYPE, ""), ("device", tarfile.CHRTYPE, ""),
                 ("block", tarfile.BLKTYPE, "")]
        for name, kind, target in cases:
            with self.subTest(member=name):
                self.write_tar(archive / "postgres.tar.gz", name, kind=kind, linkname=target)
                self.update_checksum(archive, "postgres.tar.gz")
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    operations.verify(archive)

    def test_manifest_requires_exact_complete_files_and_valid_checksums(self):
        archive, _ = self.backup()
        original = json.loads((archive / "manifest.json").read_text())
        cases = [lambda value: value.update(format=2),
                 lambda value: value["sha256"].pop("redis.tar.gz"),
                 lambda value: value["sha256"].update({"unrecorded": "a" * 64}),
                 lambda value: value["sha256"].update({"redis.tar.gz": "A" * 64}),
                 lambda value: value["sha256"].update({"redis.tar.gz": 123}),
                 lambda value: value.update(graph_id="different_graph")]
        for number, mutate in enumerate(cases):
            with self.subTest(case=number):
                changed = copy.deepcopy(original)
                mutate(changed)
                (archive / "manifest.json").write_text(json.dumps(changed))
                with patch.object(operations, "run") as process:
                    with self.assertRaises(ValueError):
                        operations.verify(archive)
                    process.assert_not_called()
        (archive / "manifest.json").write_text(json.dumps(original))
        (archive / "unrecorded").write_bytes(b"not recorded")
        with self.assertRaisesRegex(ValueError, "unrecorded"):
            operations.verify(archive)

    def test_symlinked_manifest_or_payload_never_passes_integrity_verification(self):
        archive, _ = self.backup()
        for name in ("manifest.json", "workspace.tar.gz"):
            with self.subTest(name=name):
                original = (archive / name).read_bytes()
                outside = self.root / ("outside-" + name)
                outside.write_bytes(original)
                (archive / name).unlink()
                (archive / name).symlink_to(outside)
                with self.assertRaises(ValueError):
                    operations.verify(archive)
                (archive / name).unlink()
                (archive / name).write_bytes(original)

    def test_valid_checksum_does_not_make_a_truncated_tar_restorable(self):
        archive, _ = self.backup()
        (archive / "redis.tar.gz").write_bytes(b"not a gzip archive")
        self.update_checksum(archive, "redis.tar.gz")
        with self.boundaries():
            self.calls.clear()
            with self.assertRaises(tarfile.TarError):
                operations.restore(archive, self.root / "invalid-tar", 19202)
        self.assertEqual(self.calls, [])

    def test_restore_rejects_invalid_ports_before_inspecting_or_creating_volumes(self):
        archive, _ = self.backup()
        for port in (-1, 0, 80, 1023, 65536):
            with self.subTest(port=port), patch.object(operations, "volume_info") as inspect, patch.object(operations, "run") as process:
                with self.assertRaisesRegex(ValueError, "unprivileged"):
                    operations.restore(archive, self.root / "invalid-port", port)
                inspect.assert_not_called()
                process.assert_not_called()

    def test_restore_refuses_even_an_orphan_target_volume(self):
        archive, _ = self.backup()
        destination = self.root / "target"
        with self.boundaries(volume_info=lambda _name: {"Labels": {}}):
            self.calls.clear()
            with self.assertRaisesRegex(ValueError, "target restore volume already exists"):
                operations.restore(archive, destination, 19202)
        self.assertFalse(destination.exists())
        self.assertFalse(any(call[0] == "process" for call in self.calls))

    def test_restore_detects_volume_appearing_after_create_before_extracting(self):
        archive, _ = self.backup()
        destination = self.root / "late-volume"

        def occupied_after_create(name):
            if destination.exists() and "restore" in name:
                return {"Labels": {"com.docker.compose.project": "protected-live"}}
            return copy.deepcopy(self.volume_labels.get(name))

        with self.boundaries(volume_info=occupied_after_create):
            self.calls.clear()
            with self.assertRaisesRegex(ValueError, "appeared during restore"):
                operations.restore(archive, destination, 19202)
        self.assertTrue(destination.exists())
        self.assertFalse(any(call[0] == "process" and call[1][:3] == ["docker", "volume", "create"] for call in self.calls))
        self.assertEqual((self.deployment / ".env").read_bytes(), self.original_environment)

    def test_restore_rechecks_ownership_after_volume_create_race(self):
        archive, _ = self.backup()
        destination = self.root / "raced-volume"

        def foreign_at_create(command, cwd=None):
            args = list(map(str, command))
            if args[:3] == ["docker", "volume", "create"]:
                self.volume_labels[args[-1]] = {"Labels": {
                    "com.docker.compose.project": "protected-live",
                    "com.docker.compose.volume": "postgres"}}
            return self.command_io(command, cwd)

        with self.boundaries(run=foreign_at_create):
            self.calls.clear()
            with self.assertRaisesRegex(ValueError, "owned|ownership|another project"):
                operations.restore(archive, destination, 19202)
        self.assertFalse(any(call[0] == "process" and call[1][:2] == ["docker", "run"] for call in self.calls))
        self.assertEqual((self.source / "labels.bin").read_bytes(), b"immutable input, never rewritten")

    def test_restore_reserves_all_stores_before_extraction_and_rejects_same_project_orphan(self):
        archive, _ = self.backup()
        destination = self.root / "same-project-orphan"

        def orphan_at_create(command, cwd=None):
            args = list(map(str, command))
            if args[:3] == ["docker", "volume", "create"] and args[-1].endswith("_postgres"):
                # Docker accepts a pre-existing volume unchanged, even if its
                # project/logical labels happen to match this restore's names.
                project = next(item.split("=", 1)[1] for item in args if item.startswith("com.docker.compose.project="))
                self.volume_labels[args[-1]] = {"Labels": {
                    "com.docker.compose.project": project, "com.docker.compose.volume": "postgres"}}
            return self.command_io(command, cwd)

        with self.boundaries(run=orphan_at_create):
            self.calls.clear()
            with self.assertRaisesRegex(ValueError, "restore attempt"):
                operations.restore(archive, destination, 19202)
        self.assertFalse(any(call[0] == "process" and call[1][:2] == ["docker", "run"] for call in self.calls))
        creations = [call for call in self.calls if call[0] == "process" and call[1][:3] == ["docker", "volume", "create"]]
        self.assertEqual(len(creations), 2)
        self.assertTrue(all("--label" in call[1] for call in creations))

    def test_restore_preserves_graph_namespace_current_runtime_and_exact_saved_state(self):
        archive, _ = self.backup()
        destination = self.root / "restored"
        original_files = {path.relative_to(self.deployment): path.read_bytes()
                          for path in self.deployment.rglob("*") if path.is_file()}
        with self.boundaries():
            self.calls.clear()
            result = operations.restore(archive, destination, 19202)
        generated = json.loads((destination / "deployment.json").read_text())
        self.assertEqual(generated["graph_id"], self.spec["graph_id"])
        self.assertEqual(generated["datastack"], self.spec["datastack"])
        self.assertEqual(generated["bigtable_project"], self.spec["project_name"])
        self.assertNotEqual(generated["project_name"], self.spec["project_name"])
        self.assertEqual(generated["local_url"], "http://127.0.0.1:19202")
        self.assertTrue(result["state_compared_to_backup"])
        self.assertEqual(result["observations"], self.observation)
        self.assertEqual((destination / "scripts/runtime.txt").read_text(), "candidate scripts")
        self.assertEqual((destination / "provenance/archived-runtime/scripts/runtime.txt").read_text(), "original scripts")
        self.assertEqual((destination / "data/saved-state.json").read_bytes(), (self.deployment / "data/saved-state.json").read_bytes())
        self.assertEqual((destination / ".env").read_bytes(), self.original_environment)
        self.assertEqual((destination / ".env").stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(name.startswith(generated["project_name"] + "_") for name in result["volumes"]))
        self.assertEqual(len(result["volumes"]), 3)
        commands = [call[1] for call in self.calls if call[0] == "process"]
        creation_positions = [index for index, command in enumerate(commands) if command[:3] == ["docker", "volume", "create"]]
        extraction_positions = [index for index, command in enumerate(commands) if command[:2] == ["docker", "run"]]
        self.assertEqual(len(creation_positions), 3)
        self.assertEqual(len(extraction_positions), 3)
        self.assertLess(max(creation_positions), min(extraction_positions))
        self.assertEqual(original_files, {path.relative_to(self.deployment): path.read_bytes()
                                        for path in self.deployment.rglob("*") if path.is_file()})
        self.assertEqual((destination.with_name(destination.name + "-source") / "labels.bin").read_bytes(), (self.source / "labels.bin").read_bytes())

    def test_restore_state_mismatch_retains_only_separate_copy_for_investigation(self):
        archive, _ = self.backup()
        destination = self.root / "wrong-state"
        changed = {**self.observation, "annotations": []}
        with self.boundaries(snapshot=lambda _deployment: changed):
            self.calls.clear()
            with self.assertRaisesRegex(RuntimeError, "separate restored copy"):
                operations.restore(archive, destination, 19202)
        self.assertTrue(destination.exists())
        self.assertFalse((destination / "restore-report.json").exists())
        self.assertEqual((self.deployment / ".env").read_bytes(), self.original_environment)
        self.assertFalse(any(call[0] == "compose" and call[1] == self.spec["project_name"] for call in self.calls))

    def test_offline_restore_reports_no_comparison_to_unobserved_backup(self):
        self.running = []
        archive, _ = self.backup(offline=True)
        with self.boundaries():
            result = operations.restore(archive, self.root / "offline-restored", 19202)
        self.assertFalse(result["state_compared_to_backup"])
        self.assertTrue(result["backup_checksums_verified"])

    def test_restore_excluded_local_inputs_require_existing_unchanged_directory(self):
        archive, _ = self.backup(include_source=False)
        for source_dir in (None, self.root / "missing"):
            with self.subTest(source=source_dir), self.boundaries():
                self.calls.clear()
                with self.assertRaisesRegex(ValueError, "source-dir"):
                    operations.restore(archive, self.root / "no-input", 19202, source_dir=source_dir)
            self.assertFalse((self.root / "no-input").exists())
            self.assertFalse(any(call[0] == "process" for call in self.calls))

    def test_restore_cannot_replace_existing_included_source_directory(self):
        archive, _ = self.backup()
        destination = self.root / "occupied-input"
        neighbor = destination.with_name(destination.name + "-source")
        neighbor.mkdir()
        (neighbor / "keep").write_bytes(b"foreign input")
        with self.boundaries():
            self.calls.clear()
            with self.assertRaisesRegex(ValueError, "included-source directory already exists"):
                operations.restore(archive, destination, 19202)
        self.assertFalse(destination.exists())
        self.assertEqual((neighbor / "keep").read_bytes(), b"foreign input")

    def test_load_rejects_external_extra_duplicate_and_foreign_named_volumes(self):
        mutations = [lambda cfg: cfg["volumes"]["postgres"].update(external=True),
                     lambda cfg: cfg["volumes"]["postgres"].update(name="protected_postgres"),
                     lambda cfg: cfg["volumes"].update(extra={"name": "extra"}),
                     lambda cfg: cfg["services"]["postgres"]["volumes"].append(copy.deepcopy(cfg["services"]["postgres"]["volumes"][0])),
                     lambda cfg: cfg.update(name="protected-live")]
        for number, mutate in enumerate(mutations):
            with self.subTest(case=number):
                config = self.config(self.paths[self.deployment])
                mutate(config)
                with patch.object(operations, "compose", return_value=json.dumps(config)):
                    with self.assertRaises(ValueError):
                        operations.load_deployment(self.deployment)

    def test_docker_volume_daemon_error_is_not_treated_as_missing_volume(self):
        for stderr, expected in [("Cannot connect to daemon", RuntimeError), ("permission denied", RuntimeError),
                                 ("no such volume", None)]:
            with self.subTest(stderr=stderr), patch.object(operations.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr=stderr)):
                if expected:
                    with self.assertRaises(expected):
                        operations.volume_info("cave-pkg-owned_postgres")
                else:
                    self.assertIsNone(operations.volume_info("cave-pkg-owned_postgres"))

    def test_retirement_confirmation_is_exact_and_precedes_volume_inspection(self):
        with self.boundaries():
            for confirmation in ("", "cave-pkg-", self.spec["project_name"] + " ", self.spec["project_name"].upper()):
                with self.subTest(confirmation=confirmation), patch.object(operations, "volume_info") as inspect:
                    with self.assertRaisesRegex(ValueError, "exact project_name"):
                        operations.teardown(self.deployment, confirmation)
                    inspect.assert_not_called()
        self.assertEqual(self.mutations(), [])

    def test_retirement_checks_volume_logical_label_even_for_same_project(self):
        self.volume_labels[self.spec["project_name"] + "_redis"]["Labels"]["com.docker.compose.volume"] = "postgres"
        with self.boundaries():
            with self.assertRaisesRegex(ValueError, "another project"):
                operations.teardown(self.deployment, self.spec["project_name"])
        self.assertEqual(self.mutations(), [])

    def test_repeat_retirement_preserves_input_backup_and_deployment_files(self):
        archive, _ = self.backup()
        original = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.calls.clear()
        with self.boundaries():
            first = operations.teardown(self.deployment, self.spec["project_name"])
            second = operations.teardown(self.deployment, self.spec["project_name"])
        self.assertEqual(first, second)
        self.assertEqual(original, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        self.assertTrue(operations.verify(archive)["checksums_verified"])
        self.assertEqual(self.mutations(), [("compose", self.spec["project_name"], ("down", "--volumes", "--timeout", "60"))] * 2)
        self.assertFalse(any(call[0] == "process" for call in self.calls))

    def test_rotation_rejects_ambiguous_credentials_without_mutation(self):
        cases = [b"POSTGRES_PASSWORD=one\n", b"POSTGRES_PASSWORD=\nCAVE_SESSION_SECRET=x\n",
                 b"POSTGRES_PASSWORD=one\nPOSTGRES_PASSWORD=two\nCAVE_SESSION_SECRET=x\n",
                 b"POSTGRES_PASSWORD='quoted'\nCAVE_SESSION_SECRET=x\n",
                 b'POSTGRES_PASSWORD=one\nCAVE_SESSION_SECRET="quoted"\n']
        for number, content in enumerate(cases):
            with self.subTest(case=number), self.boundaries(), patch.object(operations, "postgres_password") as password:
                (self.deployment / ".env").write_bytes(content)
                with self.assertRaises(ValueError):
                    operations.rotate_secrets(self.deployment)
                password.assert_not_called()
                self.assertEqual((self.deployment / ".env").read_bytes(), content)
                self.assertEqual(self.mutations(), [])
                self.assertEqual(list((self.deployment / "secrets").glob("rotation-before-*")), [])

    def test_rotation_symlink_does_not_modify_external_environment(self):
        external = self.root / "foreign.env"
        external.write_bytes(self.original_environment)
        (self.deployment / ".env").unlink()
        (self.deployment / ".env").symlink_to(external)
        with self.boundaries(), patch.object(operations, "postgres_password") as password:
            with self.assertRaisesRegex(ValueError, "regular private file"):
                operations.rotate_secrets(self.deployment)
            password.assert_not_called()
        self.assertEqual(external.read_bytes(), self.original_environment)
        self.assertEqual(self.mutations(), [])

    def test_rotation_requires_database_and_application_services_running(self):
        for running in (["pcg"], ["postgres", "bigtable", "redis"]):
            with self.subTest(running=running), self.boundaries(), patch.object(operations, "postgres_password") as password:
                self.running = running
                with self.assertRaises(ValueError):
                    operations.rotate_secrets(self.deployment)
                password.assert_not_called()
        self.assertEqual(self.mutations(), [])

    def test_rotation_rolls_back_when_saved_state_changes(self):
        changed = {**self.observation, "native_edit_history": []}
        snapshots = iter([self.observation, changed, self.observation])
        with self.boundaries(snapshot=lambda _deployment: next(snapshots)), patch.object(operations, "postgres_password") as password:
            with self.assertRaisesRegex(RuntimeError, "original credentials and saved state were restored"):
                operations.rotate_secrets(self.deployment)
        self.assertEqual(password.call_args_list[-1].args[1], "old-private-password")
        self.assertEqual((self.deployment / ".env").read_bytes(), self.original_environment)
        report = json.loads((self.deployment / "rotation-report.json").read_text())
        self.assertFalse(report["rotated"])
        self.assertEqual(report["failed_phase"], "checking saved state")
        self.assertTrue(report["original_credentials_and_state_restored"])
        self.assertNotIn("old-private-password", json.dumps(report))

    def test_rotation_partial_stop_and_database_failures_restore_state_without_secret_errors(self):
        for stage in ("stop", "password"):
            with self.subTest(stage=stage):
                self.running = ["bigtable", "postgres", "redis", "pcg", "gateway"]
                (self.deployment / ".env").write_bytes(self.original_environment)
                failed = False

                def partial_stop(deployment, *arguments):
                    nonlocal failed
                    result = self.compose_io(deployment, *arguments)
                    if stage == "stop" and arguments[0] == "stop" and not failed:
                        failed = True
                        raise RuntimeError("sensitive old-private-password diagnostic")
                    return result

                def password_io(_deployment, _password):
                    nonlocal failed
                    if stage == "password" and not failed:
                        failed = True
                        raise RuntimeError("sensitive old-private-password diagnostic")

                with self.boundaries(compose=partial_stop), patch.object(operations, "postgres_password", side_effect=password_io):
                    with self.assertRaises(RuntimeError) as error:
                        operations.rotate_secrets(self.deployment)
                self.assertNotIn("old-private-password", str(error.exception))
                self.assertEqual((self.deployment / ".env").read_bytes(), self.original_environment)
                self.assertEqual(set(self.running), {"bigtable", "postgres", "redis", "pcg", "gateway"})
                report = json.loads((self.deployment / "rotation-report.json").read_text())
                self.assertTrue(report["original_credentials_and_state_restored"])

    def test_rotation_interruption_restores_credentials_instead_of_leaving_half_change(self):
        failed = False

        def interrupted_recreate(deployment, *arguments):
            nonlocal failed
            if arguments[0] == "up" and not failed:
                failed = True
                raise KeyboardInterrupt("operator cancelled")
            return self.compose_io(deployment, *arguments)

        with self.boundaries(compose=interrupted_recreate), patch.object(operations, "postgres_password"):
            with self.assertRaisesRegex(RuntimeError, "original credentials and saved state were restored"):
                operations.rotate_secrets(self.deployment)
        self.assertEqual((self.deployment / ".env").read_bytes(), self.original_environment)
        self.assertEqual(set(self.running), {"bigtable", "postgres", "redis", "pcg", "gateway"})

    def test_rotation_atomic_environment_failure_restores_database_and_old_file(self):
        real_replace = operations.os.replace
        failed = False

        def fail_environment_once(source, target):
            nonlocal failed
            if Path(target) == self.deployment / ".env" and not failed:
                failed = True
                raise OSError("private replacement failed")
            return real_replace(source, target)

        with self.boundaries(), patch.object(operations, "postgres_password") as password, patch.object(operations.os, "replace", side_effect=fail_environment_once):
            with self.assertRaisesRegex(RuntimeError, "original credentials and saved state were restored"):
                operations.rotate_secrets(self.deployment)
        self.assertEqual(password.call_args_list[-1].args[1], "old-private-password")
        self.assertEqual((self.deployment / ".env").read_bytes(), self.original_environment)
        self.assertEqual(list(self.deployment.glob("..env-*")), [])
        report = json.loads((self.deployment / "rotation-report.json").read_text())
        self.assertEqual(report["failed_phase"], "updating the private environment file")
        self.assertTrue(report["original_credentials_and_state_restored"])

    def test_rotation_reports_unverified_recovery_and_retains_private_original(self):
        def failed_password(_deployment, _password):
            raise RuntimeError("secret-bearing password diagnostics")

        with self.boundaries(), patch.object(operations, "postgres_password", side_effect=failed_password):
            with self.assertRaisesRegex(RuntimeError, "automatic recovery could not be verified") as error:
                operations.rotate_secrets(self.deployment)
        self.assertNotIn("secret-bearing", str(error.exception))
        report = json.loads((self.deployment / "rotation-report.json").read_text())
        self.assertFalse(report["original_credentials_and_state_restored"])
        self.assertFalse(report["rotated"])
        recovery = Path(report["private_recovery_file"])
        self.assertEqual(recovery.read_bytes(), self.original_environment)
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("secret-bearing", json.dumps(report))

    def test_atomic_environment_replace_failure_cleans_temporary_and_preserves_original(self):
        with patch.object(operations.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                operations.private_write(self.deployment / ".env", b"new private contents")
        self.assertEqual((self.deployment / ".env").read_bytes(), self.original_environment)
        self.assertEqual(list(self.deployment.glob("..env-*")), [])

    def test_password_sql_quotes_role_and_password_and_never_uses_secret_argv(self):
        with self.boundaries():
            deployment = operations.load_deployment(self.deployment)
        deployment["config"]["services"]["postgres"]["environment"]["POSTGRES_USER"] = 'role"name'
        password = "quoted'password"
        with patch.object(operations.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as process:
            operations.postgres_password(deployment, password)
        self.assertEqual(process.call_args.kwargs["input"], 'ALTER ROLE "role""name" PASSWORD \'quoted\'\'password\';\n')
        self.assertNotIn(password, " ".join(process.call_args.args[0]))
        self.assertTrue(process.call_args.kwargs["capture_output"])


if __name__ == "__main__":
    unittest.main()
