"""Disposable OFFLINE store tests; no service or real-data writes."""

import hashlib
import importlib.util
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/local_refinement.py"
sys.path.insert(0, str(SCRIPT.parent))
import local_refinement as lr


def fixture(tmp_path, disconnected=False):
    labels = np.zeros((6, 6, 12), dtype=np.uint64)
    atom = lr.chunk_prefix((0, 0, 0)) + 1
    other = lr.chunk_prefix((1, 0, 0)) + 1
    labels[1:5, 1:5, 1:6] = atom
    if disconnected:
        labels[:, :, 3] = 0
    labels[1:5, 1:5, 6:11] = other
    store = lr.Store(tmp_path / "cave-pkg-refine")
    version = store.init(
        labels,
        {str(atom): 777, str(other): 777},
        [{"id": "a", "point_xyz": [1, 2, 2]}, {"id": "b", "point_xyz": [10, 2, 2]}],
        chunk_xyz=[6, 6, 6],
    )
    return store, version, labels, atom, other


def tree(path):
    return [
        (
            str(p.relative_to(path)),
            p.stat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in sorted(path.rglob("*"))
        if p.is_file()
    ]


def test_seed_preview_partition_and_purity(tmp_path):
    store, v, labels, atom, other = fixture(tmp_path)
    before = tree(store.path)
    a, split = store.preview(atom, [[1, 2, 2], [5, 2, 2]])
    b, split2 = store.preview(atom, [[5, 2, 2], [1, 2, 2]])
    assert a == b and np.array_equal(split, split2)
    assert tree(store.path) == before
    assert min(a["counts"]) >= 16
    assert len(a["children"]) == 2 and sum(a["counts"]) == np.count_nonzero(
        labels == atom
    )
    assert np.array_equal(split[labels != atom], labels[labels != atom])
    assert all(x not in np.unique(labels) for x in a["children"])
    for child in a["children"]:
        assert child >> 26 == atom >> 26


def test_apply_history_annotations_roots_mesh_export_undo(tmp_path):
    store, v, labels, atom, other = fixture(tmp_path)
    preview, split = store.preview(atom, [[1, 2, 2], [5, 2, 2]])
    v1 = store.apply(
        v, atom, [[1, 2, 2], [5, 2, 2]], preview_hash=preview["preview_hash"]
    )
    actual, roots, meta, _ = store.read(v1)
    assert np.array_equal(actual, split)
    assert roots[str(other)] == 777 and all(
        roots[str(x)] == 777 for x in preview["children"]
    )
    assert np.array_equal(store.read(v)[0], labels)
    snap = store.export(tmp_path / "cave-pkg-export", v1)
    assert snap["offline_prototype"] and not snap["native_history_consistent"]
    assert snap["annotations"][0]["atom_id"] == int(split[2, 2, 1])
    assert snap["annotations"][1]["atom_id"] == other
    original, _ = lr.derived(labels, meta["roots"], meta["annotations"])
    assert original["root_mesh_sha256"] == snap["root_mesh_sha256"]
    _, faces = lr.derived(actual, roots, meta["annotations"])
    assert len(faces["777"]) == 192
    expected = set()
    for point in zip(*np.nonzero(actual)):
        for other_point in lr.neighbors(point, actual.shape):
            x, y = int(actual[point]), int(actual[other_point])
            if y and x != y:
                expected.add(tuple(sorted((x, y))))
    assert snap["adjacency"] == [list(x) for x in sorted(expected)]
    assert np.array_equal(np.load(tmp_path / "cave-pkg-export/labels.npy"), split)
    for root, quads in json.loads(
        (tmp_path / "cave-pkg-export/meshes.json").read_text()
    ).items():
        assert lr.digest(lr.canonical(sorted(quads))) == snap["root_mesh_sha256"][root]
    v2 = store.undo(v1)
    assert np.array_equal(store.read(v2)[0], labels)
    assert store.read(v2)[1] == meta["roots"]
    assert np.array_equal(store.read(v1)[0], split)
    assert store.event(v2)[0]["kind"] == "undo"
    later, _ = store.preview(atom, [[1, 2, 2], [5, 2, 2]])
    assert min(later["children"]) > max(preview["children"])
    v3 = store.undo(v2)
    assert np.array_equal(store.read(v3)[0], split)


def test_components_and_unreachable_seeds(tmp_path):
    store, v, labels, atom, _ = fixture(tmp_path, True)
    before = tree(store.path)
    result, after = store.preview(atom, mode="components")
    assert len(result["children"]) == 2 and tree(store.path) == before
    ignored, _ = store.preview(atom, seeds=[[1, 2, 2]], mode="components")
    assert ignored == result
    assert len(result["child_bounds_xyz"]) == 2
    store.apply(v, atom, mode="components")
    assert np.array_equal(store.read()[0], after)
    # Seeds all on left leave the right component unassigned.
    with pytest.raises(ValueError, match="Unseeded"):
        lr.partition(labels == atom, [[1, 2, 2], [2, 2, 2]])


def test_stale_version_and_preview(tmp_path):
    store, v, _, atom, _ = fixture(tmp_path)
    v1 = store.apply(v, atom, [[1, 2, 2], [5, 2, 2]])
    before = tree(store.path)
    with pytest.raises(lr.StaleVersion):
        store.apply(v, atom, [[1, 2, 2], [5, 2, 2]])
    assert tree(store.path) == before
    store.undo(v1)
    before = store.current()
    with pytest.raises(lr.StaleVersion):
        store.apply(before, atom, [[1, 2, 2], [5, 2, 2]], preview_hash="wrong")
    assert store.current() == before


@pytest.mark.parametrize("fault", ["after_staging", "after_event", "before_current"])
def test_crash_recovery(tmp_path, fault):
    store, v, labels, atom, _ = fixture(tmp_path)
    with pytest.raises(RuntimeError, match="Injected"):
        store.apply(v, atom, [[1, 2, 2], [5, 2, 2]], fault=fault)
    assert store.current() == v
    assert np.array_equal(store.read()[0], labels)
    allocated = []
    for path in (store.path / "events").glob("*/event.json"):
        allocated.extend(json.loads(path.read_text())["children"])
    orphaned = store.recover()
    assert len(orphaned) == (fault != "after_staging")
    assert not list(store.path.glob(".tmp-*"))
    proposed, _ = store.preview(atom, [[1, 2, 2], [5, 2, 2]])
    if allocated:
        assert min(proposed["children"]) > max(allocated)
    store.apply(v, atom, [[1, 2, 2], [5, 2, 2]])
    store.read()


def race(path, v, atom, queue):
    try:
        value = lr.Store(path).apply(v, atom, [[1, 2, 2], [5, 2, 2]])
        queue.put(("ok", value))
    except lr.StaleVersion:
        queue.put(("stale", None))


def test_concurrent_cas(tmp_path):
    store, v, _, atom, _ = fixture(tmp_path)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [
        context.Process(target=race, args=(str(store.path), v, atom, queue))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0
    results = [queue.get(timeout=2)[0] for _ in workers]
    assert sorted(results) == ["ok", "stale"]
    store.read()


@pytest.mark.parametrize(
    "seeds",
    [
        [[0, 0, 0], [1, 2, 2]],
        [[1, 2, 2], [1, 2, 2]],
        [[1.5, 2, 2], [5, 2, 2]],
        [1, 2],
        "bad",
    ],
)
def test_seed_guards(tmp_path, seeds):
    store, _, _, atom, _ = fixture(tmp_path)
    with pytest.raises(ValueError):
        store.preview(atom, seeds)


def test_input_guards_and_corruption(tmp_path):
    with pytest.raises(ValueError, match="cave-pkg-"):
        lr.Store(tmp_path / "production")
    with pytest.raises(ValueError):
        lr.chunk_prefix((-1, 0, 0))
    with pytest.raises(ValueError):
        lr.chunk_prefix((1024, 0, 0))
    store, v, labels, atom, _ = fixture(tmp_path)
    with pytest.raises(FileExistsError):
        store.init(labels, store.read()[1], chunk_xyz=[6, 6, 6])
    raw = (store.path / "base.npy").read_bytes()
    (store.path / "base.npy").write_bytes(raw + b"x")
    with pytest.raises(ValueError, match="Base hash"):
        store.read()
    (store.path / "base.npy").write_bytes(raw)
    v1 = store.apply(v, atom, [[1, 2, 2], [5, 2, 2]])
    path = store.path / "events" / v1 / "snapshot.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="artifact hash"):
        store.read()


def test_native_encoding_parity():
    # Execute the exact pinned source function without importing its service dependencies.
    source = (
        SCRIPT.parents[1]
        / ".runtime/dependencies/PyChunkedGraph/pychunkedgraph/graph/chunks/utils.py"
    )
    if not source.exists():
        pytest.skip("Exact pinned upstream source not fetched on this host")
    import ast
    from types import SimpleNamespace

    tree = ast.parse(source.read_text())
    function = next(
        x
        for x in tree.body
        if isinstance(x, ast.FunctionDef) and x.name == "_compute_chunk_id"
    )
    namespace = {"np": np}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"),
        namespace,
    )
    meta = SimpleNamespace(
        bitmasks={1: 10}, graph_config=SimpleNamespace(LAYER_ID_BITS=8)
    )
    for point in [(0, 0, 0), (1, 2, 3), (1023, 1023, 1023)]:
        assert int(namespace["_compute_chunk_id"](meta, 1, *point)) == lr.chunk_prefix(
            point
        )


def test_end_to_end_cli(tmp_path):
    store, v, labels, atom, _ = fixture(tmp_path)

    def cli(*args, code=0, path=store.path):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--store", str(path), *args],
            capture_output=True,
            text=True,
        )
        assert result.returncode == code, result.stderr
        return json.loads(result.stdout) if code == 0 else result.stderr

    np.save(tmp_path / "input.npy", labels)
    (tmp_path / "roots.json").write_text(json.dumps(store.read()[1]))
    blank = tmp_path / "cave-pkg-cli"
    cv = cli(
        "init",
        "--labels",
        str(tmp_path / "input.npy"),
        "--roots",
        str(tmp_path / "roots.json"),
        "--chunk-xyz",
        "6",
        "6",
        "6",
        path=blank,
    )
    preview = cli(
        "preview", "--parent", str(atom), "--seeds", "[[1,2,2],[5,2,2]]", path=blank
    )
    assert "stale" in cli(
        "apply",
        "--parent",
        str(atom),
        "--seeds",
        "[[1,2,2],[5,2,2]]",
        "--expected-version",
        cv,
        "--preview-hash",
        "wrong",
        path=blank,
        code=3,
    )
    v1 = cli(
        "apply",
        "--parent",
        str(atom),
        "--seeds",
        "[[1,2,2],[5,2,2]]",
        "--expected-version",
        cv,
        "--preview-hash",
        preview["preview_hash"],
        path=blank,
    )
    assert "stale" in cli(
        "apply",
        "--parent",
        str(atom),
        "--seeds",
        "[[1,2,2],[5,2,2]]",
        "--expected-version",
        cv,
        path=blank,
        code=3,
    )
    assert (
        cli("read", "--point-xyz", "1", "2", "2", path=blank)["atom_id"]
        in preview["children"]
    )
    assert (
        cli("export", "--output", str(tmp_path / "cave-pkg-cli-export"), path=blank)[
            "native_history_consistent"
        ]
        is False
    )
    assert "stale" in cli("undo", "--expected-version", cv, path=blank, code=3)
    cli("undo", "--expected-version", v1, path=blank)
    (blank / ".tmp-interrupted").mkdir()
    assert "Unpublished" in cli("verify", path=blank, code=2)
    assert not cli("status", path=blank)["verified"]
    cli("recover", path=blank)
    assert cli("verify", path=blank)["verified"]
    assert np.array_equal(lr.Store(blank).read()[0], labels)


def test_size_id_and_uint64_guards(tmp_path):
    atom = lr.chunk_prefix((0, 0, 0)) + 1
    with pytest.raises(ValueError, match="bounded"):
        lr.Store(tmp_path / "cave-pkg-big").init(np.zeros((65, 65, 65), np.uint64), {})
    with pytest.raises(ValueError, match="layer1"):
        lr.Store(tmp_path / "cave-pkg-bad-id").init(
            np.ones((2, 2, 2), np.uint64), {"1": 1}
        )
    labels = np.full((2, 2, 2), atom, np.uint64)
    store = lr.Store(tmp_path / "cave-pkg-uint64")
    version = store.init(labels, {str(atom): 2**64 - 1})
    new = store.apply(version, atom, [[0, 0, 0], [1, 1, 1]])
    _, roots, meta, _ = store.read(new)
    assert set(roots.values()) == {2**64 - 1}
    snapshot, _ = lr.derived(store.read(new)[0], roots, meta["annotations"])
    assert str(2**64 - 1) in snapshot["root_mesh_sha256"]


def test_failure_after_publish_and_historical_corruption(tmp_path):
    store, v, _, atom, _ = fixture(tmp_path)
    with pytest.raises(RuntimeError, match="after_current"):
        store.apply(v, atom, [[1, 2, 2], [5, 2, 2]], fault="after_current")
    published = store.current()
    assert published != v
    assert store.recover() == []
    store.read()
    undo = store.undo(published)
    event_path = store.path / "events" / published / "event.json"
    event_path.write_bytes(event_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="Event hash"):
        store.read(undo)


@pytest.mark.parametrize(
    "point,expected",
    [
        ((0, 0, 0), 72057594037927936),
        ((1, 2, 3), 72128100422385664),
        ((1023, 1023, 1023), 144115188008747008),
    ],
)
def test_pinned_native_encoding_constants(point, expected):
    assert lr.chunk_prefix(point) == expected


def test_corrupt_orphan_has_context(tmp_path):
    store, v, _, atom, _ = fixture(tmp_path)
    with pytest.raises(RuntimeError):
        store.apply(v, atom, [[1, 2, 2], [5, 2, 2]], fault="after_event")
    orphan = store.recover()[0]
    record = store.path / "orphans" / orphan / "event.json"
    record.write_text("invalid json")
    with pytest.raises(ValueError, match="Invalid allocation record") as error:
        store.preview(atom, [[1, 2, 2], [5, 2, 2]])
    assert str(record) in str(error.value)


def test_export_stages_before_publication(tmp_path, monkeypatch):
    store, _, _, _, _ = fixture(tmp_path)
    target = tmp_path / "cave-pkg-interrupted-export"
    original = lr.write_sync

    def fail(path, raw):
        if path.name == "snapshot.json":
            raise OSError("Injected export failure")
        original(path, raw)

    monkeypatch.setattr(lr, "write_sync", fail)
    with pytest.raises(OSError, match="Injected"):
        store.export(target)
    assert not target.exists()
    assert list(tmp_path.glob(".tmp-cave-pkg-interrupted-export-*"))


@pytest.mark.parametrize("fault", ["after_event", "before_current"])
def test_unpublished_explicit_version_rejected(tmp_path, fault):
    store, v, _, atom, _ = fixture(tmp_path)
    with pytest.raises(RuntimeError):
        store.apply(v, atom, [[1, 2, 2], [5, 2, 2]], fault=fault)
    unpublished = next((store.path / "events").iterdir()).name
    with pytest.raises(ValueError, match="not published"):
        store.read(unpublished)
    with pytest.raises(ValueError, match="not published"):
        store.preview(atom, [[1, 2, 2], [5, 2, 2]], version=unpublished)
    with pytest.raises(ValueError, match="not published"):
        store.export(tmp_path / "cave-pkg-no-orphan-export", unpublished)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--store",
            str(store.path),
            "read",
            "--version",
            unpublished,
            "--point-xyz",
            "1",
            "2",
            "2",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2 and "not published" in result.stderr


@pytest.mark.parametrize(
    "annotations", [{}, [1], [{"point_xyz": None}], [{}], {"point_xyz": [1, 1, 1]}]
)
def test_invalid_annotation_schema(tmp_path, annotations):
    atom = lr.chunk_prefix((0, 0, 0)) + 1
    store = lr.Store(tmp_path / "cave-pkg-annotation-guard")
    with pytest.raises(ValueError):
        store.init(np.full((2, 2, 2), atom, np.uint64), {str(atom): 1}, annotations)
    assert not store.path.exists()


def test_export_destination_race_and_empty_undo(tmp_path, monkeypatch):
    store, v, _, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="Nothing to undo"):
        store.undo(v)
    target = tmp_path / "cave-pkg-racing-export"
    original = lr.rename_new_directory

    def competing_directory(source, destination):
        destination.mkdir()
        original(source, destination)

    monkeypatch.setattr(lr, "rename_new_directory", competing_directory)
    with pytest.raises(FileExistsError):
        store.export(target)
    assert target.is_dir() and list(target.iterdir()) == []


def test_event_details_cannot_replace_integrity_fields(tmp_path):
    store, v, before, atom, _ = fixture(tmp_path)
    result, after = store.preview(atom, [[1, 2, 2], [5, 2, 2]])
    roots = store.read()[1]
    root = roots.pop(str(atom))
    roots.update({str(child): root for child in result["children"]})
    hostile = {
        **result,
        "parent": "wrong",
        "base": "wrong",
        "files": {},
        "roots": {},
        "kind": "wrong",
        "bbox_zyx": [],
    }
    with store.lock():
        new = store.publish(v, before, after, roots, "refine", hostile)
    event, _ = store.event(new)
    assert event["parent"] == v and event["base"] == v and event["kind"] == "refine"
    assert np.array_equal(store.read(new)[0], after)
