"""Release transaction tests using disposable Git repositories, never GitHub.

Actual Git creates and verifies objects, ancestry, refs and fast-forward pushes.
Only the Copybara Java process and the HTTPS/API transport are simulated here;
the opt-in test_release.py suite independently exercises actual Copybara.
"""

from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import export_policy, export_release


REAL_RUN = subprocess.run


class PublicationContracts(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="cave-publication-contract-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.package = self.root / "package"
        self.package.mkdir()
        self.source = self.root / "private-source"
        self.destination = self.root / "public.git"
        # Tool/version validation belongs to the real Copybara integration.
        # These transaction tests use the host Git without downloading tools.
        self.git = shutil.which("git")
        if not self.git:
            self.skipTest("Git is a package prerequisite")
        self.java = "copybara-process-boundary"
        self.git_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_CONFIG_COUNT": "0", "GIT_TERMINAL_PROMPT": "0"}
        self.git_env.pop("GIT_CONFIG_PARAMETERS", None)
        self.git_command("init", self.source)
        self.git_command("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.source)
        self.git_command("config", "user.name", "Private fixture author", cwd=self.source)
        self.git_command("config", "user.email", "private-author@example.invalid", cwd=self.source)
        self.managed_paths = ("README.md", "LICENSE", "bin/cave", "scripts/operator.py", "docs/change.md")
        self.write(self.source, "README.md", export_policy.PRIVATE_URL + ".git\nRelease fixture\n")
        self.write(self.source, "LICENSE", "Fixture license\n")
        self.write(self.source, "bin/cave", "#!/bin/sh\nexit 0\n")
        (self.source / "bin/cave").chmod(0o755)
        self.write(self.source, "scripts/operator.py", "# supported operator\n")
        self.write(self.source, "docs/contract.md", "PRIVATE CONTRACT MUST NOT LEAVE SOURCE\n")
        self.write(self.source, ".runtime/secret", "PRIVATE GENERATED MATERIAL\n")
        self.private_parent = self.commit(self.source, "Private development history must not be published")
        self.write(self.source, "README.md", export_policy.PRIVATE_URL + ".git\nFirst candidate\n")
        self.candidate = self.commit(self.source, "Another private internal commit message")
        self.calls = []
        self.migrations = 0
        self.ls_remote_calls = 0
        self.before_fetch = None
        self.before_second_read = None
        self.before_push = None
        self.before_final_read = None
        self.migration_fault = None

    @staticmethod
    def write(directory, name, value):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def git_command(self, *arguments, cwd=None, input=None, check=True):
        return REAL_RUN([self.git, *map(str, arguments)], cwd=cwd, env=self.git_env, input=input,
                        check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def commit(self, directory, message):
        self.git_command("add", ".", cwd=directory)
        self.git_command("commit", "-m", message, cwd=directory)
        return self.git_command("rev-parse", "HEAD", cwd=directory).decode().strip()

    def head(self):
        return self.git_command("rev-parse", "main", cwd=self.destination).decode().strip()

    def public_change(self, name="community/notes.txt", content="Destination-owned note\n"):
        with tempfile.TemporaryDirectory(prefix="cave-public-change-", dir=self.root) as scratch:
            work = Path(scratch) / "work"
            self.git_command("clone", self.destination, work)
            self.git_command("symbolic-ref", "HEAD", "refs/heads/main", cwd=work)
            self.git_command("config", "user.name", "Public fixture author", cwd=work)
            self.git_command("config", "user.email", "public-author@example.invalid", cwd=work)
            self.write(work, name, content)
            revision = self.commit(work, "Destination-owned contribution")
            self.git_command("push", "origin", "main", cwd=work)
            return revision

    def source_change(self):
        self.write(self.source, "docs/change.md", "A supported new procedure\n")
        return self.commit(self.source, "Private internal follow-up message")

    def transport_io(self, git, *arguments, env):
        self.calls.append([git, *map(str, arguments)])
        args = list(map(str, arguments))
        if "ls-remote" in args:
            self.ls_remote_calls += 1
            if self.ls_remote_calls == 2 and self.before_second_read:
                callback, self.before_second_read = self.before_second_read, None
                callback()
            if self.ls_remote_calls == 3 and self.before_final_read:
                callback, self.before_final_read = self.before_final_read, None
                callback()
        if "fetch" in args and self.before_fetch:
            callback, self.before_fetch = self.before_fetch, None
            callback()
        if "push" in args and self.before_push:
            callback, self.before_push = self.before_push, None
            callback()
        # HTTPS GitHub transport is represented by a disposable local bare repo.
        # No authentication or remote request leaves this process.
        args = [str(self.destination) if item == export_release.PUBLIC_URL else item for item in args]
        if args[:3] == ["init", "--bare", "--initial-branch=main"]:
            # Equivalent empty-repository initialization for Git 2.25 hosts.
            # The production tool gate requires modern Git; the fast suite
            # does not provision Java/Git merely to test publication decisions.
            initialized = REAL_RUN([git, "init", "--bare", args[3]], env=env, check=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            REAL_RUN([git, "-C", args[3], "symbolic-ref", "HEAD", "refs/heads/main"], env=env,
                     check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return initialized
        return REAL_RUN([git, *args], env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def process_io(self, command, **kwargs):
        if command[0] != self.java:
            return REAL_RUN(command, **kwargs)
        self.migrations += 1
        command = list(map(str, command))
        self.calls.append(command)
        staging = Path(next(part.split("=", 1)[1] for part in command if part.startswith("--git-destination-url=")))
        revision = command[command.index("release") + 1]
        with tempfile.TemporaryDirectory(prefix="cave-simulated-copybara-", dir=self.root) as scratch:
            work = Path(scratch) / "work"
            self.git_command("clone", staging, work)
            self.git_command("symbolic-ref", "HEAD", "refs/heads/main", cwd=work)
            self.git_command("config", "user.name", export_policy.AUTHOR_NAME, cwd=work)
            self.git_command("config", "user.email", export_policy.AUTHOR_EMAIL, cwd=work)
            for name in self.managed_paths:
                source = self.git_command("show", revision + ":" + name, cwd=self.source, check=False)
                path = work / name
                if not source:
                    if path.exists():
                        path.unlink()
                    continue
                if name == "README.md":
                    source = source.replace(export_policy.PRIVATE_URL.encode(),
                                            ("https://github.com/" + export_policy.PUBLIC_REPO).encode())
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source)
                mode = self.git_command("ls-tree", revision, "--", name, cwd=self.source).split()[0]
                path.chmod(0o755 if mode == b"100755" else 0o644)
            if self.migration_fault == "bytes":
                self.write(work, "README.md", "Incorrect exported bytes\n")
            elif self.migration_fault == "mode":
                (work / "bin/cave").chmod(0o644)
            elif self.migration_fault == "extra":
                self.write(work, ".env", "PRIVATE GENERATED MATERIAL\n")
            elif self.migration_fault == "missing":
                (work / "LICENSE").unlink()
            message = "CAVE local release\n\nSource-revision: " + revision + "\nGitOrigin-RevId: " + revision + "\n"
            if self.migration_fault == "marker":
                message = "Missing exact source markers\n"
            if self.migration_fault in {"private-parent", "extra-ancestor"}:
                self.git_command("add", ".", cwd=work)
                tree = self.git_command("write-tree", cwd=work).decode().strip()
                if self.migration_fault == "private-parent":
                    self.git_command("fetch", "--no-tags", self.source, "refs/heads/main", cwd=work)
                    parent = self.private_parent
                else:
                    parent = self.commit(work, "Extra public initial commit")
                commit = self.git_command("commit-tree", tree, "-p", parent, cwd=work,
                                          input=message.encode()).decode().strip()
                self.git_command("update-ref", "refs/heads/main", commit, cwd=work)
            else:
                self.commit(work, message)
            self.git_command("push", "origin", "main", cwd=work)
        return subprocess.CompletedProcess(command, 0)

    def boundaries(self, remote=False):
        stack = ExitStack()
        stack.enter_context(patch.object(export_release, "ROOT", self.package))
        stack.enter_context(patch.object(export_release, "tools", return_value=(
            self.java, self.git, self.root / "copybara.jar", {"fixture": True},
            {"git_version": "real disposable Git", "java_version": "simulated process boundary"})))
        environment = {**self.git_env, "GH_TOKEN": "sentinel-never-log-this"} if remote else self.git_env
        stack.enter_context(patch.object(export_release, "environment", return_value=environment))
        stack.enter_context(patch.object(export_release, "run", side_effect=self.transport_io))
        stack.enter_context(patch.object(export_release.subprocess, "run", side_effect=self.process_io))
        if remote:
            response = json.dumps({"id": 123, "full_name": export_policy.PUBLIC_REPO,
                                   "private": False, "owner": {"type": "Organization"}}).encode()
            stack.enter_context(patch.object(export_release.urllib.request, "urlopen",
                                             side_effect=lambda *_args, **_kwargs: io.BytesIO(response)))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack

    def migrate(self, revision=None, remote=False):
        self.ls_remote_calls = 0
        with self.boundaries(remote):
            return export_release.release(revision or self.candidate,
                                          export_release.PUBLIC_URL if remote else self.destination,
                                          publish=remote, source=self.source)

    def reports(self):
        return [json.loads(path.read_text()) for path in sorted((self.package / ".runtime/releases").glob("*/release-report.json"))]

    def assert_no_push(self):
        self.assertFalse(any("push" in command for command in self.calls))

    def test_first_release_pushes_exact_verified_public_commit_without_private_ancestry(self):
        result = self.migrate()
        self.assertTrue(result["passed"])
        self.assertFalse(result["published"])
        self.assertEqual(self.head(), result["destination_commit"])
        self.assertEqual(self.git_command("rev-list", "--count", "main", cwd=self.destination).strip(), b"1")
        log = self.git_command("log", "--format=%B%n%ae", "main", cwd=self.destination)
        self.assertNotIn(b"Private development", log)
        self.assertNotIn(b"private-author", log)
        self.assertIn(self.candidate.encode(), log)
        self.assertNotIn("docs/contract.md", result["files"])
        self.assertNotIn(".runtime/secret", result["files"])
        self.assertTrue(result["private_source_history_excluded"])
        pushes = [command for command in self.calls if "push" in command]
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0][-1], result["destination_commit"] + ":refs/heads/main")
        self.assertFalse(any("--force" in command for command in pushes))

    def test_exact_source_revision_ignores_dirty_source_files(self):
        self.write(self.source, "README.md", "Uncommitted content must not be shipped\n")
        result = self.migrate()
        actual = self.git_command("show", "main:README.md", cwd=self.destination)
        self.assertNotIn(b"Uncommitted", actual)
        self.assertEqual(result["source_commit"], self.candidate)

    def test_repeat_release_is_idempotent_without_new_objects_or_java(self):
        first = self.migrate()
        count = self.migrations
        second = self.migrate()
        self.assertTrue(second["migration_noop"])
        self.assertEqual(second["destination_commit"], first["destination_commit"])
        self.assertEqual(self.migrations, count)
        self.assertEqual(self.git_command("rev-list", "--count", "main", cwd=self.destination).strip(), b"1")

    def test_repeat_release_preserves_destination_owned_tip_and_locates_actual_import_marker(self):
        first = self.migrate()
        community = self.public_change()
        count = self.migrations
        replay = self.migrate()
        self.assertTrue(replay["migration_noop"])
        self.assertEqual(self.head(), community)
        self.assertEqual(replay["destination_commit"], community)
        self.assertEqual(replay["source_marker_commit"], first["destination_commit"])
        self.assertEqual(self.migrations, count)
        self.assertEqual(replay["preserved_destination_files"], ["community/notes.txt"])
        self.assertEqual(self.git_command("show", "main:community/notes.txt", cwd=self.destination), b"Destination-owned note\n")

    def test_incremental_release_preserves_community_and_accepts_only_source_descendants(self):
        first = self.migrate()
        community = self.public_change()
        second_revision = self.source_change()
        second = self.migrate(second_revision)
        self.assertEqual(second["destination_before"], community)
        self.assertEqual(second["previous_source_commit"], self.candidate)
        self.assertEqual(second["preserved_destination_files"], ["community/notes.txt"])
        self.git_command("merge-base", "--is-ancestor", first["destination_commit"], second["destination_commit"], cwd=self.destination)
        self.calls.clear()
        with self.assertRaises(subprocess.CalledProcessError):
            self.migrate(self.private_parent)
        self.assert_no_push()
        self.assertEqual(self.head(), second["destination_commit"])

    def test_managed_destination_edits_are_never_overwritten(self):
        self.migrate()
        conflict = self.public_change("README.md", "Human managed-path change\n")
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "bytes or executable mode"):
            self.migrate(self.source_change())
        self.assertEqual(self.head(), conflict)
        self.assert_no_push()

    def test_unrelated_destination_history_is_rejected_without_copybara_or_push(self):
        self.git_command("init", "--bare", self.destination)
        self.git_command("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.destination)
        unrelated = self.public_change()
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "no Copybara source marker"):
            self.migrate()
        self.assertEqual(self.migrations, 0)
        self.assertEqual(self.head(), unrelated)
        self.assert_no_push()

    def test_inconsistent_prior_revision_markers_are_rejected(self):
        first = self.migrate()
        with tempfile.TemporaryDirectory(dir=self.root) as scratch:
            work = Path(scratch) / "work"
            self.git_command("clone", self.destination, work)
            self.git_command("config", "user.name", "Public fixture", cwd=work)
            self.git_command("config", "user.email", "public@example.invalid", cwd=work)
            self.write(work, "community/notes.txt", "Contribution with an invalid source claim\n")
            invalid = self.commit(work, "Source-revision: " + "b" * 40 + "\nGitOrigin-RevId: " + self.candidate)
            self.git_command("push", "origin", "main", cwd=work)
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "inconsistent source revision markers"):
            self.migrate()
        self.assertEqual(self.head(), invalid)
        self.assertNotEqual(invalid, first["destination_commit"])
        self.assert_no_push()

    def test_candidate_byte_mode_file_and_marker_failures_never_push(self):
        for fault in ("bytes", "mode", "extra", "missing", "marker", "private-parent", "extra-ancestor"):
            with self.subTest(fault=fault):
                self.destination = self.root / (fault + ".git")
                self.migration_fault = fault
                self.calls.clear()
                with self.assertRaises(ValueError):
                    self.migrate()
                self.assert_no_push()
                self.assertEqual(self.git_command("show-ref", "--heads", cwd=self.destination, check=False), b"")
        self.assertTrue(all(not value["passed"] and not value["published"] for value in self.reports()))

    def test_destination_change_between_listing_and_fetch_aborts(self):
        self.migrate()
        changed = []
        self.before_fetch = lambda: changed.append(self.public_change())
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "changed during staging"):
            self.migrate(self.source_change())
        self.assertEqual(self.head(), changed[0])
        self.assert_no_push()

    def test_destination_change_after_verification_aborts_before_push(self):
        self.migrate()
        changed = []
        self.before_second_read = lambda: changed.append(self.public_change())
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "changed after verification"):
            self.migrate(self.source_change())
        self.assertEqual(self.head(), changed[0])
        self.assert_no_push()
        failed = [report for report in self.reports() if not report["passed"]]
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0]["source_bytes_verified"])

    def test_concurrent_change_after_last_read_is_rejected_by_real_non_force_push(self):
        self.migrate()
        changed = []
        self.before_push = lambda: changed.append(self.public_change())
        self.calls.clear()
        with self.assertRaises(subprocess.CalledProcessError):
            self.migrate(self.source_change())
        self.assertEqual(self.head(), changed[0])
        attempted = [command for command in self.calls if "push" in command]
        self.assertEqual(len(attempted), 1)
        self.assertNotIn("--force", attempted[0])
        self.assertTrue(any(report.get("failure_type") == "CalledProcessError" for report in self.reports()))

    def test_destination_change_after_push_cannot_be_reported_as_verified_success(self):
        self.migrate()
        changed = []
        self.before_final_read = lambda: changed.append(self.public_change())
        with self.assertRaisesRegex(ValueError, "does not point to the verified"):
            self.migrate(self.source_change())
        self.assertEqual(self.head(), changed[0])
        failed = [report for report in self.reports() if not report["passed"]]
        self.assertEqual(len(failed), 1)
        self.assertFalse(failed[0]["published"])
        self.assertNotIn("destination_commit_verified", failed[0])

    def test_publication_destination_identity_is_verified_and_credential_not_reported(self):
        self.git_command("init", "--bare", self.destination)
        self.git_command("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.destination)
        result = self.migrate(remote=True)
        self.assertTrue(result["published"])
        self.assertEqual(result["destination_identity"], {"id": 123, "full_name": export_policy.PUBLIC_REPO, "private": False})
        self.assertEqual(self.head(), result["destination_commit"])
        for path in self.package.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"sentinel-never-log-this", path.read_bytes())

    def test_authenticated_git_failure_report_suppresses_secret_stderr(self):
        self.git_command("init", "--bare", self.destination)
        self.git_command("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.destination)

        def failed_push(git, *arguments, env):
            if "push" in arguments:
                raise subprocess.CalledProcessError(128, [git, "push", export_release.PUBLIC_URL],
                                                    stderr=b"sentinel-never-log-this")
            return self.transport_io(git, *arguments, env=env)

        with self.boundaries(remote=True), patch.object(export_release, "run", side_effect=failed_push):
            with self.assertRaises(subprocess.CalledProcessError):
                export_release.release(self.candidate, export_release.PUBLIC_URL, True, source=self.source)
        failed = self.reports()[0]
        self.assertFalse(failed["passed"])
        self.assertFalse(failed["published"])
        self.assertEqual(failed["failure_type"], "CalledProcessError")
        self.assertNotIn("sentinel-never-log-this", json.dumps(failed))

    def test_private_generated_file_or_credential_in_candidate_fails_before_destination_mutation(self):
        credential = "gh" + "p_" + "A" * 40
        self.write(self.source, "README.md", credential + "\n")
        revision = self.commit(self.source, "Private bad candidate")
        with self.assertRaisesRegex(ValueError, "privacy scan"):
            self.migrate(revision)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.calls, [])

    def test_preserved_destination_files_are_also_privacy_checked(self):
        self.migrate()
        private = self.public_change("community/private.txt", "/home/" + "fixture-user/private-file\n")
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "privacy scan"):
            self.migrate(self.source_change())
        self.assertEqual(self.head(), private)
        self.assert_no_push()

    def test_remote_protocol_and_nested_checkout_guards_precede_tool_or_network_calls(self):
        destinations = [("ssh://git@github.com/elsewhere/repo.git", False),
                        ("git@github.com:elsewhere/repo.git", False),
                        ("http://github.com/elsewhere/repo.git", False),
                        (export_release.PUBLIC_URL, False),
                        ("https://github.com/elsewhere/repo.git", True),
                        (self.source, False), (self.source / "inside.git", False),
                        (self.source.parent, False), (self.destination, True)]
        with patch.object(export_release, "tools") as tools, patch.object(export_release.urllib.request, "urlopen") as network:
            for destination, publish in destinations:
                with self.subTest(destination=str(destination)):
                    with self.assertRaises(ValueError):
                        export_release.release(self.candidate, destination, publish, source=self.source)
            tools.assert_not_called()
            network.assert_not_called()

    def test_nonbare_local_destination_is_rejected_without_staging_or_push(self):
        self.git_command("init", self.destination)
        with self.assertRaisesRegex(ValueError, "bare Git repository"):
            self.migrate()
        self.assertEqual(self.reports(), [])
        self.assert_no_push()


class PublicationIdentityAndEnvironment(unittest.TestCase):
    def test_public_org_identity_rejects_wrong_owner_repo_and_visibility(self):
        valid = {"id": 123, "full_name": export_policy.PUBLIC_REPO, "private": False,
                 "owner": {"type": "Organization"}}
        cases = [{**valid, "private": True}, {**valid, "private": None},
                 {**valid, "full_name": "PytorchConnectomics/other"},
                 {**valid, "owner": {"type": "User"}}, {**valid, "owner": {}}]
        for identity in cases:
            with self.subTest(identity=identity), patch.object(export_release.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(identity).encode())):
                with self.assertRaisesRegex(ValueError, "intended public organization"):
                    export_release.remote_identity({"GH_TOKEN": "not-a-real-token"})

    def test_identity_accepts_canonical_case_and_uses_scoped_api_auth(self):
        identity = {"id": 456, "full_name": export_policy.PUBLIC_REPO.lower(), "private": False,
                    "owner": {"type": "Organization"}}
        with patch.object(export_release.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(identity).encode())) as network:
            result = export_release.remote_identity({"GH_TOKEN": "sentinel-not-a-real-token"})
        self.assertEqual(result["id"], 456)
        request = network.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/" + export_policy.PUBLIC_REPO)
        self.assertEqual(request.get_header("Authorization"), "Bearer sentinel-not-a-real-token")
        self.assertEqual(network.call_args.kwargs["timeout"], 30)
        self.assertNotIn("sentinel", json.dumps(result))

    def test_remote_environment_disables_global_auth_storage_traces_and_tls_override(self):
        provided = {"GH_TOKEN": "sentinel-token", "GIT_TRACE": "1", "GIT_TRACE_CURL": "1",
                    "GIT_CURL_VERBOSE": "1", "GH_DEBUG": "api", "GIT_SSL_NO_VERIFY": "1",
                    "GIT_CONFIG_PARAMETERS": "credential.helper=store", "PATH": os.defpath}
        with patch.dict(os.environ, provided, clear=True), patch.object(export_policy.shutil, "which", return_value="/usr/bin/gh"):
            result = export_policy.environment("/example/bin/git", remote=True)
        self.assertEqual(result["GH_TOKEN"], "sentinel-token")
        self.assertEqual(result["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(result["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(result["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(result["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(result["GIT_CONFIG_VALUE_1"], "!gh auth git-credential")
        self.assertEqual(result["GIT_TERMINAL_PROMPT"], "0")
        for key in provided.keys() - {"GH_TOKEN", "PATH"}:
            self.assertNotIn(key, result)

    def test_remote_environment_requires_both_process_token_and_credential_helper(self):
        for environment, gh in [({}, "/usr/bin/gh"), ({"GH_TOKEN": "sentinel"}, None)]:
            with self.subTest(environment=environment, helper=gh), patch.dict(os.environ, environment, clear=True), patch.object(export_policy.shutil, "which", return_value=gh):
                with self.assertRaisesRegex(ValueError, "GH_TOKEN"):
                    export_policy.environment("/example/git", remote=True)


if __name__ == "__main__":
    unittest.main()
