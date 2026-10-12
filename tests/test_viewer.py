"""Portable viewer configuration and isolation checks for destructive acceptance."""
import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


configure = module("configure_iteration_viewer").configure
browser = module("verify_iteration_browser")
builder = module("build_viewer")


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dest = Path(self.temp.name)
        self.spec = {
            "graph_id": "pkg_viewer",
            "project_name": "cave-pkg-viewer",
            "test_fixture": True,
            "local_url": "http://127.0.0.1:19210",
            "shape_xyz": [96, 64, 32],
            "resolution_nm_xyz": [40, 40, 120],
            "voxel_offset_xyz": [2, 4, 6],
        }
        (self.dest / "deployment.json").write_text(json.dumps(self.spec))
        (self.dest / "compose.yml").write_text("name: cave-pkg-viewer\n")
        self.scenario = {"atoms": ["100", "200"], "positions_xyz": [[20, 30, 12], [40, 30, 12]],
                         "view_position_xyz": [30, 30, 12], "current_roots": ["300", "400"],
                         "initial_relation": "separate"}

    def tearDown(self):
        self.temp.cleanup()

    def test_configuration_is_local_without_dataset_specific_sources(self):
        state = configure(self.dest)
        self.assertEqual(state["position"], [50, 36, 22])
        self.assertEqual(state["layers"][0]["source"], "zarr://http://127.0.0.1:19210/image/")
        self.assertEqual(state["layers"][1]["source"], "graphene://http://127.0.0.1:19210/segmentation/table/pkg_viewer")
        config = json.loads((self.dest / "web/official-viewer-config.json").read_text())
        self.assertEqual(config["viewerOrigin"], "/viewer/")
        self.assertNotIn("spelunker.cave-explorer.org", (self.dest / "web/index.html").read_text())

    def test_scenario_sets_assigned_position_and_selected_roots(self):
        (self.dest / "data").mkdir()
        (self.dest / "data/scenario.json").write_text(json.dumps(self.scenario))
        state = configure(self.dest)
        self.assertEqual(state["position"], self.scenario["view_position_xyz"])
        self.assertEqual(state["layers"][1]["segments"], ["300", "400"])

    def test_browser_acceptance_rejects_live_deployment(self):
        (self.dest / "compose.yml").write_text("name: cave-live-data\n")
        with self.assertRaisesRegex(ValueError, "disposable"):
            browser.validate_target(self.dest, self.spec, self.scenario)

    def test_browser_acceptance_rejects_graph_project_mismatch(self):
        (self.dest / "compose.yml").write_text("name: cave-pkg-other\n")
        with self.assertRaisesRegex(ValueError, "agree"):
            browser.validate_target(self.dest, self.spec, self.scenario)

    def test_browser_acceptance_rejects_non_loopback(self):
        self.spec["local_url"] = "https://remote.example"
        with self.assertRaisesRegex(ValueError, "loopback"):
            browser.validate_target(self.dest, self.spec, self.scenario)

    def test_browser_acceptance_allows_assigned_disposable_target(self):
        self.assertEqual(browser.validate_target(self.dest, self.spec, self.scenario), "cave-pkg-viewer")

    def test_browser_acceptance_rejects_an_ordinary_deployment(self):
        self.spec["test_fixture"] = False
        with self.assertRaisesRegex(ValueError, "test_fixture"):
            browser.validate_target(self.dest, self.spec, self.scenario)

    def test_metadata_404_allowance_does_not_hide_missing_tiles(self):
        origin = "http://127.0.0.1:19210"
        self.assertTrue(browser.metadata_probe(origin + "/image/zarr.json", origin))
        self.assertFalse(browser.metadata_probe(origin + "/image/0/0.0.0", origin))
        self.assertFalse(browser.metadata_probe(origin + "/data/atomic/info", origin))

    def test_browser_cannot_request_another_graph_or_origin(self):
        origin = self.spec["local_url"]
        graph = self.spec["graph_id"]
        self.assertTrue(browser.request_allowed(origin + "/segmentation/api/v1/table/pkg_viewer/merge", origin, graph))
        self.assertFalse(browser.request_allowed(origin + "/segmentation/api/v1/table/pkg_viewer_other/merge", origin, graph))
        self.assertFalse(browser.request_allowed(origin + "/meshing/api/v1/table/brain2/manifest/1", origin, graph))
        self.assertFalse(browser.request_allowed("http://127.0.0.1:8088/segmentation/table/brain2/info", origin, graph))


class DependencyOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name)
        self.commit = json.loads((ROOT / "upstream.lock.json").read_text())["neuroglancer"]["revision"]
        self.overlay = json.loads((ROOT / "config/viewer-dependencies.lock.json").read_text())
        self.base = {"packages": {"node_modules/unrelated": {"version": "1.0.0", "integrity": "unchanged"}}}
        for name, replacement in self.overlay["replacements"].items():
            self.base["packages"]["node_modules/" + name] = {"version": replacement["previous_version"], "integrity": "old"}
        original = (json.dumps(self.base, indent=2) + "\n").encode()
        (self.source / "package-lock.json").write_bytes(original)
        self.overlay["upstream_package_lock_sha256"] = hashlib.sha256(original).hexdigest()
        effective = json.loads(original)
        for name, replacement in self.overlay["replacements"].items():
            effective["packages"]["node_modules/" + name].update({key: replacement[key] for key in ("version", "resolved", "integrity")})
        self.overlay["package_lock_sha256"] = hashlib.sha256((json.dumps(effective, indent=2) + "\n").encode()).hexdigest()
        self.path = self.source / "overlay.json"
        self.write_overlay()

    def write_overlay(self):
        self.path.write_text(json.dumps(self.overlay, indent=2) + "\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_overlay_changes_only_reviewed_artifacts_and_preserves_source(self):
        original = (self.source / "package-lock.json").read_bytes()
        effective, _, overlay_hash = builder.dependency_lock(self.source, self.commit, self.path)
        data = json.loads(effective)
        self.assertEqual(data["packages"]["node_modules/unrelated"], self.base["packages"]["node_modules/unrelated"])
        self.assertEqual((self.source / "package-lock.json").read_bytes(), original)
        self.assertEqual(overlay_hash, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_changed_base_lock_is_rejected(self):
        (self.source / "package-lock.json").write_text('{"packages": {}}')
        with self.assertRaisesRegex(RuntimeError, "Upstream.*hash"):
            builder.dependency_lock(self.source, self.commit, self.path)

    def test_changed_overlay_artifact_is_rejected(self):
        self.overlay["replacements"]["lodash-es"]["integrity"] += "tampered"
        self.write_overlay()
        with self.assertRaisesRegex(RuntimeError, "Effective.*hash"):
            builder.dependency_lock(self.source, self.commit, self.path)

    def test_wrong_revision_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "revision"):
            builder.dependency_lock(self.source, "0" * 40, self.path)

    def test_unreviewed_package_is_rejected(self):
        self.overlay["replacements"]["unrelated"] = self.overlay["replacements"]["lodash-es"]
        self.write_overlay()
        with self.assertRaisesRegex(RuntimeError, "exactly"):
            builder.dependency_lock(self.source, self.commit, self.path)

    def test_dirty_upstream_is_refused_before_running_npm(self):
        with patch.object(builder, "command", side_effect=[self.commit, " M src/viewer.ts"]), patch.object(builder.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "tracked modifications"):
                builder.build(self.source, self.source / "output")
            run.assert_not_called()

    def test_browser_rejects_old_build_without_patched_provenance(self):
        with self.assertRaisesRegex(RuntimeError, "source_modified"):
            browser.validate_viewer_build({"commit": self.commit, "modified": False})

    def test_browser_rejects_wrong_dependency_hash(self):
        import copy
        overlay_bytes = (ROOT / "config/viewer-dependencies.lock.json").read_bytes()
        overlay = json.loads(overlay_bytes)
        build = {"commit": self.commit, "source_modified": False, "dependency_lock_modified": True,
                 "modified": True, "upstream_package_lock_sha256": overlay["upstream_package_lock_sha256"],
                 "package_lock_sha256": overlay["package_lock_sha256"],
                 "dependency_overlay_sha256": hashlib.sha256(overlay_bytes).hexdigest(),
                 "dependency_versions": {name: item["version"] for name, item in overlay["replacements"].items()}}
        browser.validate_viewer_build(build)
        tampered = copy.deepcopy(build)
        tampered["package_lock_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "package_lock_sha256"):
            browser.validate_viewer_build(tampered)


if __name__ == "__main__":
    unittest.main()
