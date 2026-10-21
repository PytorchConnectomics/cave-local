"""Failure, publication and complete-root-set contracts for refinement."""
from copy import deepcopy
import errno
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import supervoxel_history as history
import supervoxel_refine as refine


def journal(tmp_path, state="prepared", operation="123"):
    result = refine.Journal(tmp_path / "supervoxel_splits" / "journal" / operation, {
        "state": state, "operation_id": operation, "roots": [1000, 2000],
        "root": 1000, "parent": 10, "old_atom": 100, "new_atoms": [101, 102],
        "root_timestamps": {"1000": "original-a", "2000": "original-b"},
        "l2": {"10": {"root": 1000, "children": [100]},
               "20": {"root": 2000, "children": [200]}},
        "t1": "2026-01-01T00:00:01+00:00", "t2": "2026-01-01T00:00:01.001000+00:00",
        "reason": "unit", "markers": [], "replay_count": 0,
    })
    result.save()
    return result


class RecordingStore:
    def __init__(self):
        self.installed = set()
        self.writes = []
        self.verifications = 0
        self.fail_verify = False

    def apply_step(self, step):
        if step in self.installed:
            return
        if step == "7b":
            assert "7a" in self.installed, "Parent appeared before tables/lineage"
        if step == "7d":
            assert "7b" in self.installed, "Child appeared before Parent"
        if step == "7e":
            assert "7d" in self.installed, "Voxel appeared before Child"
        if step == "7f":
            assert "7e" in self.installed, "Old atom retired before voxel switch"
        self.installed.add(step)
        self.writes.append(step)

    def verify(self):
        self.verifications += 1
        assert self.installed == set(refine.PUBLICATION_STEPS)
        if self.fail_verify:
            raise ValueError("injected voxel mismatch")


def nofault(*args, **kwargs):
    pass


def test_current_pairs_preserve_uint64_precision_on_isin_sort_path():
    base = 72128031568691200
    retired = {1, *(base + offset for offset in range(64))}
    current, neighbour = base + 65, base + 10_000_000
    pairs = np.asarray([[current, neighbour], [base + 63, neighbour],
                        [2**63 + 123, current]], np.uint64)
    assert np.array_equal(refine._current_pairs(pairs, retired), pairs[[0, 2]])
    assert np.array_equal(refine._current_pairs(pairs, set()), pairs)


@pytest.mark.parametrize("defect", [None, "retired", "wrong_parent", "asymmetric"])
def test_current_cross_edges_require_current_reciprocal_leaves(defect):
    a, b, old = 72128031568691265, 72128031635800072, 72128031568691264
    pair = (old if defect == "retired" else a, b)
    cross = {10: {3: np.asarray([pair], np.uint64)},
             20: {3: np.asarray([] if defect == "asymmetric" else [(b, a)], np.uint64)}}
    children = {10: np.asarray([a + 1 if defect == "wrong_parent" else a], np.uint64),
                20: np.asarray([b], np.uint64)}
    graph = SimpleNamespace(get_atomic_cross_edges=lambda nodes, raw_only: cross,
                            get_children=lambda node: children[node])
    if defect is None:
        refine._validate_cross_edges(graph, [10, 20], {old})
    else:
        expected = {"retired": "retired atom", "wrong_parent": "current child", "asymmetric": "reciprocal"}
        with pytest.raises(ValueError, match=expected[defect]):
            refine._validate_cross_edges(graph, [10, 20], {old})


@pytest.mark.parametrize("state", list(refine.TRANSITIONS))
def test_only_prepared_and_fenced_can_abort(tmp_path, state):
    j = journal(tmp_path, state)
    if state in ("prepared", "fenced"):
        j.transition("aborted")
        assert refine.Journal(j.path).state == "aborted"
    else:
        with pytest.raises(ValueError, match="Illegal"):
            j.transition("aborted")


@pytest.mark.parametrize("state", list(refine.TRANSITIONS))
def test_fence_covers_every_affected_root_until_done(tmp_path, state):
    journal(tmp_path, state)
    assert refine.fenced_roots(tmp_path) == ({1000, 2000} if state in refine.FENCED_STATES else set())


@pytest.mark.parametrize("crash_step", refine.PUBLICATION_STEPS)
def test_replay_after_every_publication_boundary_is_idempotent(tmp_path, crash_step):
    j, store = journal(tmp_path, "publishing"), RecordingStore()

    def crash(point, *args, **kwargs):
        assert history.read_epoch(tmp_path) % 2 == 1
        if point == crash_step:
            raise SystemExit(86)

    with pytest.raises(SystemExit):
        refine.publish_journal(j, store, renew=lambda: None, data_dir=tmp_path, fault=crash)
    assert j.state == "publishing"
    assert history.read_epoch(tmp_path) % 2
    refine.publish_journal(refine.Journal(j.path), store, renew=lambda: None, data_dir=tmp_path, fault=nofault)
    assert store.writes == list(refine.PUBLICATION_STEPS)
    assert store.verifications == 1
    assert refine.Journal(j.path).state == "done"
    assert history.read_epoch(tmp_path) % 2 == 0
    assert refine.fenced_roots(tmp_path) == set()


def test_failed_verification_stays_fenced_and_repair_rechecks(tmp_path):
    j, store = journal(tmp_path, "publishing"), RecordingStore()
    store.fail_verify = True
    with pytest.raises(refine.RefinementError, match="awaits repair"):
        refine.publish_journal(j, store, renew=lambda: None, data_dir=tmp_path, fault=nofault)
    assert j.state == "verify_failed"
    assert refine.fenced_roots(tmp_path) == {1000, 2000}
    assert not (tmp_path / "supervoxel_splits" / "log.jsonl").exists()
    assert refine._recover_locked(object(), tmp_path) == [{"operation_id": "123", "state": "verify_failed"}]
    store.fail_verify = False
    refine.publish_journal(j, store, renew=lambda: None, data_dir=tmp_path, fault=nofault)
    assert j.state == "done" and store.verifications == 2


def test_verified_recovery_never_replays_or_reverifies_and_logs_once(tmp_path, monkeypatch):
    j = journal(tmp_path, "verified")
    releases = []
    graph = SimpleNamespace(client=SimpleNamespace(unlock_root=lambda root, op: releases.append((int(root), int(op)))))

    native_transition = refine.Journal.transition
    def interrupted_transition(self, state):
        if state == "done":
            raise SystemExit("crash after log append")
        return native_transition(self, state)
    monkeypatch.setattr(refine.Journal, "transition", interrupted_transition)
    with pytest.raises(SystemExit):
        refine._recover_locked(graph, tmp_path)
    assert refine.Journal(j.path).state == "verified"
    assert refine.fenced_roots(tmp_path) == {1000, 2000}
    monkeypatch.setattr(refine.Journal, "transition", native_transition)
    refine._recover_locked(graph, tmp_path)
    lines = (tmp_path / "supervoxel_splits" / "log.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["operation_id"] == "123"
    assert refine.Journal(j.path).state == "done"
    assert releases == [(1000, 123), (2000, 123)]


@pytest.mark.parametrize("state", ["prepared", "fenced"])
def test_unpublished_recovery_aborts_without_graph_access(tmp_path, state):
    j = journal(tmp_path, state)
    releases = []
    graph = SimpleNamespace(client=SimpleNamespace(unlock_root=lambda root, op: releases.append((int(root), int(op)))))
    refine._recover_locked(graph, tmp_path)
    assert refine.Journal(j.path.parent / "aborted" / j.path.name).state == "aborted"
    assert releases == [(1000, 123), (2000, 123)]


def test_file_replay_hash_skips_installed_destinations_and_keeps_payload(tmp_path, monkeypatch):
    j = journal(tmp_path)
    source = j.path / "payload.bin"
    source.write_bytes(b"replacement chunk")
    destination = tmp_path / "atomic" / "chunk"
    item = {"source": "payload.bin", "destination": str(destination),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "step": "7e"}
    store = refine.PublicationStore(None, j, tmp_path)
    store._file(item)
    assert source.exists() and source.read_bytes() == destination.read_bytes()
    monkeypatch.setattr(refine, "atomic_write", lambda *args: pytest.fail("hash-matched file was rewritten"))
    store._file(item)
    destination.write_bytes(b"damaged")
    source.write_bytes(b"corrupt payload")
    with pytest.raises(refine.RefinementError, match="payload hash"):
        store._file(item)


def test_file_payload_cannot_escape_its_deployment(tmp_path):
    j = journal(tmp_path)
    store = refine.PublicationStore(None, j, tmp_path)
    with pytest.raises(refine.RefinementError, match="escapes deployment"):
        store._file({"source": "missing", "destination": str(tmp_path.parent / "unrelated"), "sha256": "0"})


class Graph:
    def __init__(self):
        self.roots = {10: 1000, 20: 2000}
        self.children = {10: [100], 20: [200]}
        self.parents = {100: 10, 101: 10, 102: 10, 200: 20}
        self.timestamps = {1000: "original-a", 2000: "original-b"}

    def get_root(self, node):
        node = int(node)
        return self.roots[node if node in self.roots else self.parents[node]]

    def get_children(self, node):
        return np.asarray(self.children[int(node)], np.uint64)

    def get_parents(self, ids):
        return np.asarray([self.parents[int(i)] for i in ids], np.uint64)

    def is_latest_roots(self, ids):
        return np.asarray([int(i) in self.timestamps for i in ids])


@pytest.mark.parametrize("change", ["root", "timestamp", "children", "parent"])
def test_revalidation_detects_changes_in_other_root_neighbour(tmp_path, monkeypatch, change):
    cg, j = Graph(), journal(tmp_path)
    monkeypatch.setattr(refine, "_root_timestamp", lambda cg, root: cg.timestamps[root])
    refine.validate_preconditions(cg, j.data)
    if change == "root":
        cg.roots[20] = 3000
    elif change == "timestamp":
        cg.timestamps[2000] = "native-edit-later"
    elif change == "children":
        cg.children[20] = [201]
    else:
        cg.parents[200] = 21
    with pytest.raises(refine.PreconditionsChanged):
        refine.validate_preconditions(cg, j.data)


@pytest.mark.parametrize("children", [[100], [100, 101, 102], [101, 102]])
def test_recovery_accepts_only_valid_old_superset_or_retired_children(tmp_path, monkeypatch, children):
    cg, j = Graph(), journal(tmp_path)
    monkeypatch.setattr(refine, "_root_timestamp", lambda cg, root: cg.timestamps[root])
    cg.children[10] = children
    refine.validate_preconditions(cg, j.data, publishing=True)
    cg.children[10] = [100, 101]
    with pytest.raises(refine.PreconditionsChanged):
        refine.validate_preconditions(cg, j.data, publishing=True)


def test_new_affected_root_in_rediscovery_changes_whole_set():
    before = {"roots": [1000], "root_timestamps": {"1000": "t"}, "l2": {}, "parent": 10, "root": 1000}
    after = deepcopy(before)
    after["roots"] = [1000, 2000]
    assert not refine._precondition_equal(before, after)


def test_unwrapped_locks_use_sorted_complete_root_set():
    class Client:
        def lock_roots(self, **kwargs):
            pytest.fail("native edit fence was not bypassed for refinement")

    def native(client, **kwargs):
        assert kwargs["root_ids"].tolist() == [1000, 2000]
        assert set(kwargs["future_root_ids_d"]) == {1000, 2000}
        return True, kwargs["root_ids"]
    Client.lock_roots._svsplit_unwrapped = native
    acquired, roots = refine._unwrapped_lock(Client(), [2000, 1000], 123)
    assert acquired and roots.tolist() == [1000, 2000]


def fake_client():
    class Client:
        def __init__(self):
            self.released = []
            self.result = (True, np.array([1000, 2000], np.uint64))

        def lock_roots(self, root_ids, operation_id, **kwargs):
            return self.result

        def lock_roots_indefinitely(self, root_ids, operation_id, **kwargs):
            return (*self.result, None)

        def unlock_root(self, root, operation):
            self.released.append(("normal", int(root), int(operation)))

        def unlock_indefinitely_locked_root(self, root, operation):
            self.released.append(("indefinite", int(root), int(operation)))
    return Client


@pytest.mark.parametrize("indefinite", [False, True])
def test_post_native_lock_fence_releases_all_roots_for_other_root_fence(tmp_path, monkeypatch, indefinite):
    j = journal(tmp_path, "publishing")
    j.data["roots"] = [2000]
    j.save()
    Client = fake_client()
    class LockingError(Exception):
        pass
    monkeypatch.setattr(refine, "_fence_error", lambda message: LockingError(message))
    refine.install_fence(tmp_path, client_class=Client)
    client = Client()
    method = client.lock_roots_indefinitely if indefinite else client.lock_roots
    with pytest.raises(LockingError, match="awaiting repair"):
        method(root_ids=[1000, 2000], operation_id=55)
    kind = "indefinite" if indefinite else "normal"
    assert client.released == [(kind, 1000, 55), (kind, 2000, 55)]


def test_unfenced_lock_keeps_native_result_and_stale_translation_releases(tmp_path, monkeypatch):
    Client = fake_client()
    refine.install_fence(tmp_path, client_class=Client)
    client = Client()
    assert client.lock_roots([1000, 2000], 55) is client.result
    assert client.released == []
    def stale(data_dir):
        raise history.StaleTranslation("retired endpoint")
    monkeypatch.setattr(history, "check_translation_current", stale)
    with pytest.raises(history.StaleTranslation):
        client.lock_roots([1000, 2000], 55)
    assert client.released == [("normal", 1000, 55), ("normal", 2000, 55)]


def test_renewal_checks_all_root_owners_even_if_native_claims_success():
    client = SimpleNamespace(renew_locks=lambda ids, op: True,
                             get_lock_timestamp=lambda root, op: "owned" if int(root) == 1000 else None)
    with pytest.raises(refine.LockLost, match="every root"):
        refine._renew(client, [1000, 2000], 123)


def test_lock_loss_during_publication_keeps_fence_and_odd_epoch(tmp_path):
    j, store = journal(tmp_path, "publishing"), RecordingStore()
    calls = 0
    def renew():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise refine.LockLost("injected")
    with pytest.raises(refine.LockLost):
        refine.publish_journal(j, store, renew=renew, data_dir=tmp_path, fault=nofault)
    assert j.state == "publishing" and history.read_epoch(tmp_path) % 2
    assert refine.fenced_roots(tmp_path) == {1000, 2000}
    assert store.writes == ["7a", "7b"]


def test_nonfixture_ignores_fault_configuration(tmp_path, monkeypatch):
    deployment = tmp_path / "deployment.json"
    deployment.write_text(json.dumps({"test_fixture": False}))
    monkeypatch.setenv("CAVE_DEPLOYMENT_CONFIG", str(deployment))
    monkeypatch.setenv("CAVE_SVSPLIT_FAULT", json.dumps({"point": "prepared", "action": "raise"}))
    refine.fixture_fault("prepared", tmp_path)
    deployment.write_text(json.dumps({"test_fixture": True}))
    with pytest.raises(refine.RefinementError, match="Fixture fault"):
        refine.fixture_fault("prepared", tmp_path)


@pytest.mark.parametrize("options", [dict(action="exit", graceful=True),
                                    dict(action="pause", duration=0, exit_on_release=86)])
def test_fixture_graceful_exit_runs_normal_finally_paths(tmp_path, monkeypatch, options):
    deployment = tmp_path / "deployment.json"
    deployment.write_text(json.dumps({"test_fixture": True}))
    monkeypatch.setenv("CAVE_DEPLOYMENT_CONFIG", str(deployment))
    monkeypatch.setenv("CAVE_SVSPLIT_FAULT", json.dumps({"point": "7c", **options}))
    cleanup = []
    with pytest.raises(SystemExit) as error:
        try:
            refine.fixture_fault("7c", tmp_path)
        finally:
            cleanup.append(True)
    assert error.value.code == 86 and cleanup == [True]


def test_grid_then_seeded_same_piece_is_staged_as_one_replacement():
    snapshot = {"atom": 100, "lo": np.zeros(3, int), "voxels": np.full((2, 2, 8), 100, np.uint64)}
    pieces, count = refine._compute_pieces(snapshot, {"source_vox": [[0, 0, 0]], "sink_vox": [[0, 0, 7]]},
                                         (16, 16, 16), (10, 10, 10))
    assert count >= 2 and pieces[0, 0, 0] != pieces[0, 0, 7]
    assert np.all(pieces > 0)


@pytest.mark.parametrize("failure", ["renew", "neighbour_timestamp"])
@pytest.mark.parametrize("abort_write_failure", [False, True])
def test_expired_staging_lock_aborts_without_publication_then_recomputes(
        tmp_path, monkeypatch, failure, abort_write_failure):
    cg = Graph()
    cg.cache = None
    cg.segmentation_resolution = (10, 10, 10)
    locks, released, staged, publications = [], [], [], []
    calls = {"renew": 0}

    class Client:
        def create_operation_id(self):
            return np.uint64(123 + len(locks))

        def lock_roots(self, **kwargs):
            locks.append(kwargs["root_ids"].tolist())
            return True, kwargs["root_ids"]

        def renew_locks(self, roots, operation):
            calls["renew"] += 1
            if calls["renew"] == 4 and failure == "renew":
                return False
            return True

        def get_lock_timestamp(self, root, operation):
            return "owned"

        def unlock_root(self, root, operation):
            released.append((int(root), int(operation)))

    cg.client = Client()
    def discover(*args):
        return {"roots": [1000, 2000], "root_timestamps": dict((str(k), v) for k, v in cg.timestamps.items()),
                "l2": {"10": {"root": 1000, "children": [100]}, "20": {"root": 2000, "children": [200]}},
                "parent": 10, "root": 1000}

    def prepare(cg, snapshot, pieces, new, tables, operation, reason, grid_xyz, data_dir):
        j = journal(tmp_path, operation=str(operation))
        j.data["root_timestamps"] = snapshot["root_timestamps"]
        j.save()
        staged.append(j)
        if len(staged) == 1 and failure == "neighbour_timestamp":
            cg.timestamps[2000] = "edit-on-other-root-during-staging"
        return j

    def publish(j, store, **kwargs):
        publications.append(j.data["operation_id"])
        assert len(staged) == 2 and staged[0].state == "aborted"
        assert staged[0].data["markers"] == []
        j.transition("published")
        j.transition("verified")
        j.transition("done")

    monkeypatch.setattr(refine, "install_lock_ownership_fix", lambda: None)
    monkeypatch.setattr(refine, "_recover_locked", lambda *args: None)
    monkeypatch.setattr(history, "load_atom_tables", lambda *args: SimpleNamespace(retired=frozenset()))
    monkeypatch.setattr(refine, "_discover", discover)
    monkeypatch.setattr(refine, "_root_timestamp", lambda cg, root: cg.timestamps[root])
    monkeypatch.setattr(refine, "_compute_pieces", lambda *args: (np.ones((1, 1, 2), np.int32), 2))
    monkeypatch.setattr(refine, "_allocate", lambda *args: np.array([101, 102], np.uint64))
    monkeypatch.setattr(refine, "_stage", prepare)
    monkeypatch.setattr(refine, "publish_journal", publish)
    if abort_write_failure:
        native_write, failed = refine.atomic_write, []

        def write(path, content):
            if Path(path).name == "journal.json" and json.loads(content)["state"] == "aborted" and not failed:
                failed.append(True)
                raise OSError(errno.ENOSPC, "injected abort transition failure")
            return native_write(path, content)

        monkeypatch.setattr(refine, "atomic_write", write)
    result = refine.refine(cg, [{"atom": 100}], data_dir=tmp_path)
    assert result.count == 1 and publications == ["124"]
    assert locks == [[1000, 2000], [1000, 2000]]
    assert released == [(1000, 123), (2000, 123), (1000, 124), (2000, 124)]
    assert staged[0].path.parent.name == "aborted"
    assert refine.Journal(staged[0].path).state == "aborted"


@pytest.mark.parametrize("active_cross_cell", [True, False])
def test_discovery_includes_cross_chunk_neighbour_after_native_split(tmp_path, monkeypatch, active_cross_cell):
    """A current opposite-face L2 row can belong to a different current root."""
    from collections import namedtuple
    edge_types = namedtuple("EdgeTypes", "in_chunk between_chunk cross_chunk")("in", "between", "cross")

    def edge_values(pairs):
        pairs = np.asarray(pairs, np.uint64).reshape(-1, 2)
        return SimpleNamespace(node_ids1=pairs[:, 0], node_ids2=pairs[:, 1],
                               affinities=np.full(len(pairs), .8), areas=np.zeros(len(pairs)))
    values = {kind: edge_values([[100, 200]] if kind == "between" else []) for kind in edge_types}
    monkeypatch.setitem(sys.modules, "pychunkedgraph.graph", SimpleNamespace(attributes=object()))
    monkeypatch.setitem(sys.modules, "pychunkedgraph.graph.edges", SimpleNamespace(EDGE_TYPES=edge_types))
    monkeypatch.setitem(sys.modules, "pychunkedgraph.io.edges", SimpleNamespace(get_chunk_edges=lambda *args: values))
    (tmp_path / "edges").mkdir()
    for name in ("edges_0_0_0.proto.zst", "edges_1_0_0.proto.zst"):
        (tmp_path / "edges" / name).touch()

    voxels = np.full((4, 2, 2, 1), 100, np.uint64)
    voxels[2:] = 200
    class Volume:
        volume_size = (4, 2, 2)
        info = {}
        def __getitem__(self, slices):
            return voxels[slices]
    monkeypatch.setattr(refine, "_volume", lambda *args: Volume())
    monkeypatch.setattr(refine, "_logged_edges", lambda *args: [])
    cg = Graph()
    cg.meta = SimpleNamespace(graph_config=SimpleNamespace(CHUNK_SIZE=(2, 2, 2)))
    cg.get_chunk_coordinates = lambda atom: np.array([int(atom) == 200, 0, 0], int)
    cg.get_chunk_id = lambda **kwargs: np.uint64(10000 + kwargs["layer"] * 100 + kwargs["x"])
    cg.get_fake_edges = lambda chunks: {}
    cg.get_atomic_cross_edges = lambda ids: {
        np.uint64(10): {2: np.array([[100, 200]], np.uint64)} if active_cross_cell else {},
        np.uint64(20): {2: np.array([[200, 100]], np.uint64)} if active_cross_cell else {}}
    monkeypatch.setattr(refine, "_root_timestamp", lambda cg, root: cg.timestamps[root])
    tables = SimpleNamespace(retired=frozenset(), index=np.array([[100, 7, 0, 0, 0], [200, 8, 2, 0, 0]], np.uint64),
                             bounds=np.array([[0, 0, 0, 2, 2, 2], [2, 0, 0, 4, 2, 2]]))
    result = refine._discover(cg, 100, tmp_path, tables)
    assert result["roots"] == [1000, 2000]
    assert result["l2"] == {"10": {"root": 1000, "children": [100]}, "20": {"root": 2000, "children": [200]}}
    assert result["root_timestamps"] == {"1000": "original-a", "2000": "original-b"}


def test_log_representative_edges_are_discovered_across_root_ancestry(tmp_path, monkeypatch):
    attrs = SimpleNamespace(Hierarchy=SimpleNamespace(FormerParent="former"),
                            OperationLogs=SimpleNamespace(OperationID="operation", AddedEdge="added", RemovedEdge="removed"))
    monkeypatch.setitem(sys.modules, "pychunkedgraph.graph", SimpleNamespace(attributes=attrs))
    cell = lambda value: SimpleNamespace(value=value)
    root_rows = {
        1000: {"former": [cell([900])], "operation": [cell(30)]},
        900: {"former": [cell([800])], "operation": [cell(30), cell(20)]},
        800: {"operation": [cell(20), cell(10)]},
    }
    requested = []
    def logs(ids):
        requested.extend(map(int, ids))
        return {10: {"added": np.array([[100, 200]], np.uint64)},
                20: {"removed": np.array([[101, 300]], np.uint64)},
                30: {"added": np.array([[400, 500]], np.uint64)}}
    cg = SimpleNamespace(client=SimpleNamespace(
        read_nodes=lambda node_ids, properties: {int(node): root_rows[int(node)] for node in node_ids},
        read_log_entries=logs))
    tables = SimpleNamespace(lineage=np.array([(101, 100, 1000, "refine")], history.LINEAGE_DTYPE),
                             edge_map=np.array([(100, 200, 101, 200, "refine", 1000)], history.EDGE_MAP_DTYPE),
                             retired={100})
    result = refine._logged_edges(cg, 101, 1000, tables)
    assert requested == [10, 20, 30]
    assert sorted((edge.a, edge.b) for edge in result) == [(101, 200), (101, 300)]
    assert not any(400 in (edge.a, edge.b) for edge in result)


def test_revalidation_also_checks_new_log_representative_aliases():
    before = {"roots": [1000], "alias_edges": []}
    after = {"roots": [1000], "alias_edges": ["new merge representative"]}
    assert not refine._precondition_equal(before, after)


def test_verify_failed_prevents_another_refinement_invalidating_repair_files(tmp_path, monkeypatch):
    journal(tmp_path, "verify_failed")
    monkeypatch.setattr(refine, "install_lock_ownership_fix", lambda: None)
    monkeypatch.setattr(refine, "_recover_locked", lambda *args: None)
    monkeypatch.setattr(refine, "_discover", lambda *args: pytest.fail("refinement started before repair"))
    with pytest.raises(refine.RefinementError, match="awaiting repair"):
        refine.refine(SimpleNamespace(cache=None), [{"atom": 999}], data_dir=tmp_path)


@pytest.fixture
def publishing_runtime(tmp_path, monkeypatch):
    """Exercise refine/recovery orchestration with dependency-ordered writes."""
    cg, store = Graph(), RecordingStore()
    cg.cache = None
    cg.segmentation_resolution = (10, 10, 10)
    held, released = set(), []

    class Client:
        def create_operation_id(self):
            return np.uint64(123)

        def lock_roots(self, root_ids, operation_id, **kwargs):
            held.update(map(int, root_ids))
            return True, root_ids

        def renew_locks(self, roots, operation):
            return set(map(int, roots)) <= held

        def get_lock_timestamp(self, root, operation):
            return "owned" if int(root) in held else None

        def unlock_root(self, root, operation):
            held.discard(int(root))
            released.append((int(root), int(operation)))

    cg.client = Client()
    state = SimpleNamespace(cg=cg, store=store, held=held, released=released, journal=None)

    def prepare(*args):
        state.journal = journal(tmp_path)
        return state.journal

    monkeypatch.setattr(refine, "install_lock_ownership_fix", lambda: None)
    monkeypatch.setattr(history, "load_atom_tables", lambda *args: SimpleNamespace(retired=frozenset()))
    monkeypatch.setattr(refine, "_discover", lambda *args: {
        "roots": [1000, 2000], "root_timestamps": {str(k): v for k, v in cg.timestamps.items()},
        "l2": {"10": {"root": 1000, "children": [100]}, "20": {"root": 2000, "children": [200]}},
        "parent": 10, "root": 1000})
    monkeypatch.setattr(refine, "_root_timestamp", lambda graph, root: graph.timestamps[root])
    monkeypatch.setattr(refine, "_compute_pieces", lambda *args: (np.ones((1, 1, 2), np.int32), 2))
    monkeypatch.setattr(refine, "_allocate", lambda *args: np.array([101, 102], np.uint64))
    monkeypatch.setattr(refine, "_stage", prepare)
    monkeypatch.setattr(refine, "PublicationStore", lambda *args: store)
    return state


@pytest.mark.parametrize("failure_step", refine.PUBLICATION_STEPS)
def test_transient_publication_error_recovers_before_request_returns(
        tmp_path, monkeypatch, publishing_runtime, failure_step):
    env = publishing_runtime
    native_apply, failed = env.store.apply_step, []

    def transient(step):
        native_apply(step)
        if step == failure_step and not failed:
            failed.append(step)
            raise OSError("transient publication failure")

    monkeypatch.setattr(env.store, "apply_step", transient)
    with pytest.raises(refine.RefinementError, match="recovered after publication error"):
        refine.refine(env.cg, [{"atom": 100}], data_dir=tmp_path)
    saved = refine.Journal(env.journal.path)
    assert failed == [failure_step]
    assert saved.state == "done" and saved.data["replay_count"] == 1
    assert env.store.writes == list(refine.PUBLICATION_STEPS)
    assert not history.read_epoch(tmp_path) % 2
    assert history.snapshot_read(lambda: "available", data_dir=tmp_path) == "available"
    assert refine.fenced_roots(tmp_path) == set() and not env.held


def test_failed_immediate_replay_is_retried_by_next_idle_reader(
        tmp_path, monkeypatch, publishing_runtime):
    env = publishing_runtime
    native_apply, failures = env.store.apply_step, []

    def transient(step):
        native_apply(step)
        if step == "7c" and len(failures) < 2:
            failures.append(step)
            raise OSError("storage temporarily unavailable")

    monkeypatch.setattr(env.store, "apply_step", transient)
    with pytest.raises(refine.RefinementError, match="replay failed"):
        refine.refine(env.cg, [{"atom": 100}], data_dir=tmp_path)
    assert history.read_epoch(tmp_path) % 2 and not env.held
    assert refine.Journal(env.journal.path).state == "publishing"
    calls = []

    def graph_factory():
        calls.append(True)
        return env.cg

    result = history.snapshot_read(lambda: "available", data_dir=tmp_path,
        recover=lambda: refine.recover_if_idle(graph_factory, tmp_path))
    assert result == "available" and calls == [True]
    assert refine.Journal(env.journal.path).state == "done"
    assert refine.fenced_roots(tmp_path) == set() and not env.held
    assert not history.read_epoch(tmp_path) % 2


def test_idle_recovery_does_not_wait_or_construct_graph_for_live_publisher(tmp_path):
    refine._set_epoch(tmp_path, True)
    with refine.mutation_lock(tmp_path):
        assert refine.recover_if_idle(lambda: pytest.fail("graph created while flock busy"), tmp_path) is False
    refine._set_epoch(tmp_path, False)
    assert refine.recover_if_idle(lambda: pytest.fail("graph created for even epoch"), tmp_path) is False


def test_persistent_reader_replay_failure_has_shared_operation_cooldown(
        tmp_path, monkeypatch, publishing_runtime):
    env = publishing_runtime
    native_apply = env.store.apply_step
    now, factories = [1000.0], []
    monkeypatch.setattr(refine.time, "time", lambda: now[0])

    def broken(step):
        native_apply(step)
        if step == "7c":
            raise OSError("persistent Bigtable replay error")

    def graph_factory():
        factories.append(True)
        return env.cg

    monkeypatch.setattr(env.store, "apply_step", broken)
    with pytest.raises(refine.RefinementError, match="replay failed"):
        refine.refine(env.cg, [{"atom": 100}], data_dir=tmp_path)
    with pytest.raises(refine.RefinementError, match="persistent Bigtable"):
        refine.recover_if_idle(graph_factory, tmp_path)
    assert len(factories) == 1
    assert refine.Journal(env.journal.path).data["replay_count"] == 2
    failure_path = tmp_path / "supervoxel_splits" / "reader-recovery-failure.json"
    saved = json.loads(failure_path.read_text())
    assert saved["operations"] == {"123": 1000.0}
    failure_bytes, failure_mtime = failure_path.read_bytes(), failure_path.stat().st_mtime_ns
    for _ in range(30):
        # A fresh worker has no process-local failure cache.
        refine._reader_recovery_failures.clear()
        with pytest.raises(refine.RefinementError, match="cooldown.*123"):
            history.snapshot_read(lambda: pytest.fail("odd-epoch read escaped"), tmp_path,
                recover=lambda: refine.recover_if_idle(graph_factory, tmp_path))
    assert len(factories) == 1
    assert refine.Journal(env.journal.path).data["replay_count"] == 2
    assert failure_path.read_bytes() == failure_bytes and failure_path.stat().st_mtime_ns == failure_mtime
    assert history.read_epoch(tmp_path) % 2 and not env.held

    now[0] += refine.READER_RECOVERY_COOLDOWN_SECONDS
    with pytest.raises(refine.RefinementError, match="persistent Bigtable"):
        refine.recover_if_idle(graph_factory, tmp_path)
    assert len(factories) == 2
    assert refine.Journal(env.journal.path).data["replay_count"] == 3
    # A worker retaining the previous failed attempt must honor another
    # worker's newer persisted cooldown rather than replaying immediately.
    refine._reader_recovery_failures[str(failure_path.resolve())] = saved
    with pytest.raises(refine.RefinementError, match="cooldown"):
        refine.recover_if_idle(graph_factory, tmp_path)
    assert len(factories) == 2

    monkeypatch.setattr(env.store, "apply_step", native_apply)
    now[0] += refine.READER_RECOVERY_COOLDOWN_SECONDS
    assert refine.recover_if_idle(graph_factory, tmp_path)
    assert len(factories) == 3 and refine.Journal(env.journal.path).state == "done"
    assert not history.read_epoch(tmp_path) % 2 and not env.held


def test_explicit_recovery_bypasses_reader_cooldown(tmp_path, monkeypatch, publishing_runtime):
    env = publishing_runtime
    env.journal = journal(tmp_path, "publishing")
    refine._set_epoch(tmp_path, True)
    factory_error = OSError("graph unavailable")

    def broken_factory():
        raise factory_error

    with pytest.raises(refine.RefinementError) as caught:
        refine.recover_if_idle(broken_factory, tmp_path)
    assert caught.value.__cause__ is factory_error
    assert refine.recover(env.cg, tmp_path) == [{"operation_id": "123", "state": "done"}]
    assert not history.read_epoch(tmp_path) % 2


def test_reader_failure_record_error_preserves_primary_error_and_local_cooldown(tmp_path, monkeypatch):
    journal(tmp_path, "publishing")
    refine._set_epoch(tmp_path, True)
    monkeypatch.setattr(refine, "install_lock_ownership_fix", lambda: None)
    primary = OSError("graph connection unavailable")
    calls = []

    def broken_factory():
        calls.append(True)
        raise primary

    def broken_write(*args):
        raise OSError(errno.ENOSPC, "cooldown persistence failed")

    monkeypatch.setattr(refine, "atomic_write", broken_write)
    with pytest.raises(refine.RefinementError) as caught:
        refine.recover_if_idle(broken_factory, tmp_path)
    assert caught.value.__cause__ is primary
    assert "Could not persist reader recovery cooldown" in primary.__notes__[0]
    for _ in range(5):
        with pytest.raises(refine.RefinementError, match="cooldown"):
            refine.recover_if_idle(broken_factory, tmp_path)
    assert calls == [True]


@pytest.mark.parametrize("cleanup_failure", ["transition", "move"])
@pytest.mark.parametrize("error_type", [ValueError, SystemExit, KeyboardInterrupt])
def test_abort_cleanup_failure_preserves_original_and_lifts_fence(
        tmp_path, monkeypatch, publishing_runtime, cleanup_failure, error_type):
    env = publishing_runtime
    original = error_type("original fenced preparation failure")
    abort_error = OSError(errno.ENOSPC, "abort cleanup unavailable")
    native_write, native_replace = refine.atomic_write, refine.os.replace
    failures = []

    def fault(point, *args, **kwargs):
        if point == "fenced":
            raise original

    def write(path, content):
        if (cleanup_failure == "transition" and Path(path).name == "journal.json"
                and json.loads(content)["state"] == "aborted" and not failures):
            failures.append(True)
            raise abort_error
        return native_write(path, content)

    def replace(source, destination):
        if cleanup_failure == "move" and Path(destination).parent.name == "aborted":
            failures.append(True)
            raise abort_error
        return native_replace(source, destination)

    monkeypatch.setattr(refine, "fixture_fault", fault)
    monkeypatch.setattr(refine, "atomic_write", write)
    monkeypatch.setattr(refine.os, "replace", replace)
    with pytest.raises(error_type) as caught:
        refine.refine(env.cg, [{"atom": 100}], data_dir=tmp_path)
    assert caught.value is original and original.refinement_abort_error is abort_error
    assert "Refinement abort cleanup failed" in original.__notes__[0]
    assert failures == [True] and refine.Journal(env.journal.path).state == "aborted"
    assert refine.fenced_roots(tmp_path) == set() and not env.held
    assert not env.store.writes and history.read_epoch(tmp_path) == 0
    Client = fake_client()
    refine.install_fence(tmp_path, client_class=Client)
    client = Client()
    assert client.lock_roots([1000, 2000], 456) is client.result


def test_both_abort_actions_failing_preserves_original_and_does_not_retry(
        tmp_path, monkeypatch, publishing_runtime):
    env = publishing_runtime
    original = refine.PreconditionsChanged("original precondition failure")
    native_write, native_replace = refine.atomic_write, refine.os.replace
    stages = []

    def fault(point, *args, **kwargs):
        if point == "fenced":
            stages.append(True)
            raise original

    def write(path, content):
        if Path(path).name == "journal.json" and json.loads(content)["state"] == "aborted":
            raise OSError(errno.ENOSPC, "state write failed")
        return native_write(path, content)

    def replace(source, destination):
        if Path(destination).parent.name == "aborted":
            raise OSError(errno.EIO, "archive move failed")
        return native_replace(source, destination)

    monkeypatch.setattr(refine, "fixture_fault", fault)
    monkeypatch.setattr(refine, "atomic_write", write)
    monkeypatch.setattr(refine.os, "replace", replace)
    with pytest.raises(refine.PreconditionsChanged) as caught:
        refine.refine(env.cg, [{"atom": 100}], data_dir=tmp_path)
    assert caught.value is original and stages == [True]
    assert original.refinement_abort_error.errno == errno.ENOSPC
    assert "archive move failed" in original.refinement_abort_error.__notes__[0]
    assert refine.fenced_roots(tmp_path) == {1000, 2000}
    assert not env.held and not env.store.writes and history.read_epoch(tmp_path) == 0


def test_recovery_abort_failure_still_releases_locks_and_clears_fence(tmp_path, monkeypatch):
    prepared = journal(tmp_path, "fenced")
    native_write, failures, released = refine.atomic_write, [], []

    def write(path, content):
        if Path(path).name == "journal.json" and json.loads(content)["state"] == "aborted" and not failures:
            failures.append(True)
            raise OSError(errno.ENOSPC, "transient abort transition failure")
        return native_write(path, content)

    monkeypatch.setattr(refine, "atomic_write", write)
    graph = SimpleNamespace(client=SimpleNamespace(unlock_root=lambda root, operation:
                                                   released.append((int(root), int(operation)))))
    with pytest.raises(OSError, match="transient abort transition"):
        refine._recover_locked(graph, tmp_path)
    archived = prepared.path.parent / "aborted" / prepared.path.name
    assert refine.Journal(archived).state == "aborted"
    assert released == [(1000, 123), (2000, 123)] and refine.fenced_roots(tmp_path) == set()


@pytest.mark.parametrize("state", ["publishing", "published", "verify_failed", "verified", "done", "aborted"])
def test_abort_never_archives_a_publication_or_terminal_journal(tmp_path, state):
    prepared = journal(tmp_path, state)
    with pytest.raises(ValueError, match="Illegal"):
        prepared.abort()
    assert refine.Journal(prepared.path).state == state
    assert not (prepared.path.parent / "aborted").exists()


def test_idle_recovery_cleans_temporary_links_without_pending_journals(tmp_path):
    journal(tmp_path, "done")
    temporary = tmp_path / (".atom_index.npy." + "a" * 32)
    temporary.symlink_to("atom_tables/current/atom_index.npy")
    with refine.mutation_lock(tmp_path):
        assert refine._recover_locked(object(), tmp_path) == []
    assert not temporary.is_symlink()


@pytest.mark.parametrize("phase", ["prepared", "fenced", "validate-preconditions"])
@pytest.mark.parametrize("error_type", [OSError, ValueError, KeyError, SystemExit, KeyboardInterrupt])
def test_every_prepublication_exception_aborts_and_native_edits_remain_available(
        tmp_path, monkeypatch, publishing_runtime, phase, error_type):
    env, error = publishing_runtime, error_type("injected preparation error")

    def fault(point, *args, **kwargs):
        if point == phase:
            raise error

    monkeypatch.setattr(refine, "fixture_fault", fault)
    with pytest.raises(error_type) as caught:
        refine.refine(env.cg, [{"atom": 100}], data_dir=tmp_path)
    assert caught.value is error
    assert env.journal.state == "aborted" and env.journal.path.parent.name == "aborted"
    assert refine.Journal(env.journal.path).data["markers"] == []
    assert not env.store.writes and history.read_epoch(tmp_path) == 0
    assert refine.fenced_roots(tmp_path) == set() and not env.held
    Client = fake_client()
    refine.install_fence(tmp_path, client_class=Client)
    client = Client()
    assert client.lock_roots([1000, 2000], 456) is client.result
    assert client.released == []


def test_journal_aborted_between_enumeration_and_read_does_not_refuse_native_edit(tmp_path, monkeypatch):
    prepared = journal(tmp_path, "fenced")
    native_journal, reads = refine.Journal, []

    def move_before_read(path, data=None):
        reads.append(str(path))
        prepared.abort()
        return native_journal(path, data)

    monkeypatch.setattr(refine, "Journal", move_before_read)
    Client = fake_client()
    refine.install_fence(tmp_path, client_class=Client)
    client = Client()
    assert client.lock_roots([1000, 2000], 456) is client.result
    assert len(reads) == 1 and client.released == []
    assert native_journal(prepared.path).state == "aborted"


def test_present_unparseable_journal_still_fails_native_edit_closed(tmp_path):
    prepared = journal(tmp_path, "fenced")
    (prepared.path / "journal.json").write_text("{broken")
    Client = fake_client()
    refine.install_fence(tmp_path, client_class=Client)
    client = Client()
    with pytest.raises(json.JSONDecodeError):
        client.lock_roots([1000, 2000], 456)
    assert client.released == [("normal", 1000, 456), ("normal", 2000, 456)]


def test_fixture_raise_once_removes_control_before_raising(tmp_path, monkeypatch):
    deployment = tmp_path / "deployment.json"
    deployment.write_text(json.dumps({"test_fixture": True}))
    config = tmp_path / "fault.json"
    config.write_text(json.dumps({"point": "7d", "action": "raise_once"}))
    monkeypatch.setenv("CAVE_DEPLOYMENT_CONFIG", str(deployment))
    monkeypatch.setenv("CAVE_SVSPLIT_FAULT", f"file:{config}")
    with pytest.raises(OSError, match="Transient fixture error"):
        refine.fixture_fault("7d", tmp_path)
    assert not config.exists()
    refine.fixture_fault("7d", tmp_path)
