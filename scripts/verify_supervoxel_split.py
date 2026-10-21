"""Exercise refinement only on an explicitly disposable cave-pkg fixture.

The host drives HTTP edits and scoped Compose exec probes. Internal probes bypass
the public read barrier deliberately, to inspect each intermediate publication.
Every case is recorded before it runs; an exception leaves failed/pending evidence
and makes the command fail. No case is reported as passed merely because its
supporting unit tests passed.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

import numpy as np
import requests


CASES = {
    "b": "direct refinement preserves every voxel root",
    "e": "atomic writes change only the refined mask",
    "c": "historical export and repeated lineage",
    "a": "same-atom HTTP split and refinement header",
    "d": "native undo restores the refined object's partition",
    "d2": "contact/fake merge and split undo/redo across refinement",
    "d3": "undo/redo retranslate after retirement before locking",
    "f": "both mincut triggers, bounded work and exactly one retry",
    "p1": "cross-chunk neighbour in another root is locked and fenced",
    "g": "prepared and publication crashes roll forward from retained payloads",
    "g2": "publication exceptions recover before returning and public reads succeed",
    "g3": "prepublication exceptions abort without fencing native edits",
    "j": "every publication boundary preserves objects and history",
    "l": "interleaved readers use a validated epoch snapshot",
    "m": "verified crash finalizes once without replay",
    "h": "concurrent refinements in one chunk allocate distinct ids",
    "i": "switch off preserves the existing rejection",
    "k": "fence excludes native edits and repair clears verification failure",
    "n": "expired staging lock aborts and recomputes after native edit",
}


def _digest(array):
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def _canonical(image):
    _, first, inverse = np.unique(image, return_index=True, return_inverse=True)
    ranks = np.empty(len(first), np.int64)
    ranks[np.argsort(first)] = np.arange(len(first))
    return ranks[inverse].reshape(image.shape)


def _table_positions(table_ids, atoms):
    """Do not silently borrow the next row for an unindexed volume ID."""
    table_ids, atoms = np.asarray(table_ids, np.uint64), np.asarray(atoms, np.uint64)
    positions = np.searchsorted(table_ids, atoms)
    assert np.all(positions < len(table_ids)), "volume atom has no dense table row"
    assert np.array_equal(table_ids[positions], atoms), "volume atom has no exact dense table row"
    return positions


def _assert_volume_lineage(data, previous_data, tables):
    ids = np.unique(data[data != 0])
    _table_positions(tables.ids, ids)
    previous = np.unique(previous_data[previous_data != 0])
    added = np.setdiff1d(ids, previous)
    assert np.all(np.isin(added, tables.lineage["new"])), "new volume atom lacks a lineage key"
    return len(added)


def _assert_disjoint_allocations(entries):
    allocated = set()
    for entry in entries:
        ids = list(map(int, entry["new_atoms"]))
        assert ids and len(ids) == len(set(ids)), "journal has empty or duplicate allocated IDs"
        assert allocated.isdisjoint(ids), "concurrent journals allocated overlapping new atom IDs"
        allocated.update(ids)
    return allocated


def _assert_allocated_components(data, allocated, root_leaves):
    from scipy.ndimage import generate_binary_structure, label
    counts = {}
    for atom in allocated:
        mask = data == atom
        assert np.any(mask), f"allocated atom {atom} has no voxels"
        _, components = label(mask, structure=generate_binary_structure(3, 1))
        assert components == 1, f"allocated atom {atom} has {components} disconnected voxel components"
        owners = [int(root) for root, leaves in root_leaves.items()
                  if np.any(np.asarray(leaves, np.uint64) == atom)]
        assert len(owners) == 1, f"allocated atom {atom} belongs to {len(owners)} root leaf sets"
        counts[str(atom)] = {"voxels": int(mask.sum()), "components": components, "root": owners[0]}
    return counts


def _assert_staged_cells_absent(cells_by_step, read_node, attribute):
    checked_cells = 0
    for cells in cells_by_step.values():
        for cell in cells:
            timestamp = datetime.fromisoformat(cell["timestamp"])
            versions = read_node(np.uint64(cell["row"]), properties=attribute(cell),
                                 fake_edges=cell["fake"], end_time=timestamp,
                                 end_time_inclusive=True)
            assert all(version.timestamp != timestamp for version in versions), "aborted publication wrote a staged graph cell"
            checked_cells += 1
    assert checked_cells > 0, "unpublished journal has no staged graph cells to check"
    return checked_cells


def _switch_off_pair(atoms):
    # The largest remaining disposable atom can be in an isolated root after
    # earlier fault cases. Select against this case's complete geometry needs.
    eligible = sorted((atom for atom in atoms if atom["source"] == 2**64 - 1
                       and atom["count"] >= 8), key=lambda atom: (-atom["count"], atom["atom"]))
    for atom in eligible:
        other = next((other for other in atoms if other["source"] == atom["source"]
                      and other["root"] == atom["root"] and other["chunk"] != atom["chunk"]), None)
        if other is not None:
            return atom, other
    raise AssertionError("Switch-off guard needs the same source label across two graph chunks")


def _internal_main(request):
    """Run in the selected PCG container, never through a public fixture API."""
    sys.path.insert(0, "/workspace/scripts")
    from cloudvolume import CloudVolume
    from pychunkedgraph.graph.chunkedgraph import ChunkedGraph
    from pychunkedgraph.graph import attributes
    from supervoxel_history import (ancestors_at, current_descendants, install_history,
                                    load_atom_tables, read_epoch, snapshot_read)
    from supervoxel_refine import (fenced_roots, install_fence, journals, recover,
                                   refine)

    spec = json.loads(Path("/workspace/deployment.json").read_text())
    if (spec.get("test_fixture") is not True
            or spec.get("project_name") != request["project"]
            or not spec["project_name"].startswith("cave-pkg-")):
        raise ValueError("Internal probe requires its assigned disposable fixture")
    install_fence()
    install_history()
    cg = ChunkedGraph(graph_id=spec["graph_id"])
    folder = Path("/data/supervoxel_splits/verification")
    folder.mkdir(parents=True, exist_ok=True)
    volume = CloudVolume("file:///data/atomic", progress=False, cache=False, fill_missing=False)

    def live(timestamp=None):
        data = np.asarray(volume[:][..., 0])
        ids = np.unique(data[data != 0])
        options = {"time_stamp": timestamp} if timestamp is not None else {}
        roots = cg.get_roots(ids, **options)
        image = np.zeros(data.shape, np.uint64)
        mask = data != 0
        image[mask] = roots[np.searchsorted(ids, data[mask])]
        return data, image

    def snapshot(name):
        path = folder / (name + ".npz")
        if path.exists():
            raise ValueError("Snapshot evidence already exists: " + name)
        data, image = live()
        stamp = datetime.now(timezone.utc).isoformat()
        np.savez(path, atomic=data, root_image=image, partition=_canonical(image), timestamp=stamp)
        return {"snapshot": name, "timestamp": stamp, "atomic_sha256": _digest(data),
                "root_image_sha256": _digest(image), "partition_sha256": _digest(_canonical(image)),
                "epoch": read_epoch(), "generation": load_atom_tables().generation,
                "atoms": len(np.unique(data[data != 0]))}

    def saved(name):
        return np.load(folder / (name + ".npz"), allow_pickle=False)

    def check(name, *, history=False, partition=False):
        before = saved(name)
        stamp = datetime.fromisoformat(str(before["timestamp"])) if history else None
        _, image = live(stamp)
        expected = before["partition"] if partition else before["root_image"]
        actual = _canonical(image) if partition else image
        if not np.array_equal(expected, actual):
            raise AssertionError(f"{name}: {'historical ' if history else ''}voxel partition differs")
        return {"snapshot": name, "history": history, "partition": partition,
                "voxel_count": int(image.size), "sha256": _digest(actual)}

    action = request["action"]
    if action == "inventory":
        tables = load_atom_tables()
        data = np.asarray(volume[:][..., 0])
        ids, counts = np.unique(data[data != 0], return_counts=True)
        roots = cg.get_roots(ids)
        parents = cg.get_parents(ids)
        atoms = []
        for atom, count, root, parent in zip(ids, counts, roots, parents):
            pos = int(_table_positions(tables.ids, atom))
            points = np.argwhere(data == atom)
            first = points[0]
            second = points[np.argmax(np.sum((points - first) ** 2, axis=1))]
            atoms.append({"atom": int(atom), "source": int(tables.index[pos, 1]),
                          "count": int(count), "root": int(root), "parent": int(parent),
                          "chunk": list(map(int, cg.get_chunk_coordinates(atom))),
                          "bounds": tables.bounds[pos].tolist(),
                          "points": [first.tolist(), second.tolist()]})
        result = {"atoms": atoms, "epoch": read_epoch(), "generation": tables.generation,
                  "root_lock_expiry": cg.meta.graph_config.ROOT_LOCK_EXPIRY.total_seconds()}
    elif action == "snapshot":
        result = snapshot(request["name"])
    elif action == "compare":
        result = check(request["name"], history=request.get("history", False),
                       partition=request.get("partition", False))
    elif action == "mask_changed":
        before, after = saved(request["before"]), saved(request["after"])
        allowed = before["atomic"] == request["atom"]
        changed = before["atomic"] != after["atomic"]
        assert not np.any(changed & ~allowed), "atomic write changed a neighbor's voxels"
        assert np.any(changed), "refinement did not replace any voxels"
        assert np.array_equal(before["root_image"], after["root_image"])
        result = {"old_atom": request["atom"], "changed_voxels": int(changed.sum()),
                  "old_voxels": int(allowed.sum()), "outside_unchanged": True}
    elif action == "refine":
        output = refine(cg, request["requests"], grid_xyz=request.get("grid", [4, 4, 4]),
                        reason="fixture-verifier")
        result = {"count": output.count, "operation_ids": output.operation_ids,
                  "new_ids": output.new_ids, "elapsed_seconds": output.elapsed_seconds}
    elif action == "recover":
        output = recover(cg, repair=request.get("repair", False))
        result = {"recovered": str(output), "fenced_roots": sorted(fenced_roots()), "epoch": read_epoch()}
    elif action == "boundary":
        before = saved(request["name"])
        tables = load_atom_tables()
        data, image = live()
        assert np.array_equal(image, before["root_image"]), "current voxel roots changed during publication"
        added = _assert_volume_lineage(data, before["atomic"], tables)
        for root in np.unique(before["root_image"]):
            if root:
                leaves = cg.get_subgraph_leaves(root)
                assert np.array_equal(np.isin(data, leaves), before["root_image"] == root), "leaves/voxels have holes"
        check(request["name"], history=True)
        result = {"roots_unchanged": True, "leaves_have_no_holes": True,
                  "historical_voxels_equal": True, "table_rows": len(tables.ids),
                  "lineage_rows": len(tables.lineage), "new_volume_lineage_keys": added,
                  "epoch": read_epoch()}
    elif action == "allocated_components":
        data, _ = live()
        roots = np.unique(cg.get_roots(np.unique(data[data != 0])))
        root_leaves = {int(root): cg.get_subgraph_leaves(root) for root in roots}
        result = _assert_allocated_components(data, request["allocated"], root_leaves)
    elif action == "unpublished":
        from supervoxel_refine import _attribute
        operation = str(request["operation_id"])
        path = Path("/data/supervoxel_splits/journal") / operation / "journal.json"
        if not path.exists():
            path = path.parent.parent / "aborted" / operation / "journal.json"
        entry = json.loads(path.read_text())
        assert entry["state"] in ("prepared", "fenced", "aborted") and not entry.get("markers")
        tables = load_atom_tables()
        assert not np.any(np.isin(np.asarray(entry["new_atoms"], np.uint64), tables.ids)), "unpublished IDs reached atom table"
        checked_cells = _assert_staged_cells_absent(entry["cells"], cg.client.read_node, _attribute)
        files = {item["destination"]: (hashlib.sha256(Path(item["destination"]).read_bytes()).hexdigest()
                  if Path(item["destination"]).exists() else None) for item in entry["files"]}
        result = {"state": entry["state"], "atomic_sha256": _digest(np.asarray(volume[:][..., 0])),
                  "generation": tables.generation, "files": files, "staged_cells_absent": checked_cells,
                  "fenced_roots_now": sorted(fenced_roots()), "epoch": read_epoch()}
    elif action == "operation_log":
        tables = load_atom_tables()
        record, _ = cg.client.read_log_entry(np.uint64(request["operation_id"]))
        result = {}
        for name in ("AddedEdge", "RemovedEdge", "SourceID", "SinkID"):
            column = getattr(attributes.OperationLogs, name)
            if column in record and record[column] is not None:
                values = np.asarray(record[column], dtype=np.uint64)
                assert not tables.retired.intersection(map(int, values.ravel())), f"{name} contains a retired id"
                result[name] = values.tolist()
        assert result, "operation log contains no tested endpoint fields"
    elif action == "no_edges":
        tables = load_atom_tables()
        left = set(map(int, current_descendants(request["left"], tables)))
        right = set(map(int, current_descendants(request["right"], tables)))
        roots = np.unique(cg.get_roots(np.asarray(sorted(left | right), np.uint64)))
        # Query each root separately. A union-of-roots query also includes stored
        # inactive edges whose two endpoints lie in different current objects.
        parts = [np.asarray(cg.get_subgraph_edges(root)).reshape(-1, 2) for root in roots]
        pairs = np.concatenate(parts) if parts else np.empty((0, 2), np.uint64)
        bad = [(int(a), int(b)) for a, b in pairs
               if (int(a) in left and int(b) in right) or (int(a) in right and int(b) in left)]
        assert not bad, f"active edges remain between undone merge components: {bad}"
        result = {"left_descendants": len(left), "right_descendants": len(right), "active_cross_edges": 0}
    elif action == "fake_current":
        tables = load_atom_tables()
        descendants = current_descendants(request["atom"], tables)
        chunks = np.unique(np.asarray([cg.get_chunk_id(layer=2,
            **dict(zip(("x", "y", "z"), map(int, cg.get_chunk_coordinates(atom)))))
            for atom in descendants], np.uint64))
        edge_sets = cg.get_fake_edges(chunks)
        # Stored historical fake edges remain inert. Current fake edges must
        # contain the translated descendants, never only a retired old endpoint.
        pairs = [tuple(map(int, pair)) for edges in edge_sets.values() for pair in edges.get_pairs()]
        current = [pair for pair in pairs if not tables.retired.intersection(pair)]
        before = saved(request["before"])
        left_roots = np.unique(before["root_image"][before["atomic"] == request["atom"]])
        right_roots = np.unique(before["root_image"][before["atomic"] == request["other"]])
        assert len(left_roots) == len(right_roots) == 1 and left_roots[0] != right_roots[0]
        expected = {int(left_roots[0]), int(right_roots[0])}
        bridges = []
        for pair in current:
            points = tables.index[np.searchsorted(tables.ids, np.asarray(pair, np.uint64)), 2:5]
            former = {int(before["root_image"][tuple(point)]) for point in points}
            if former == expected and len(set(map(int, cg.get_roots(np.asarray(pair, np.uint64))))) == 1:
                bridges.append(pair)
        # A native merge may reactivate an existing inactive fake edge elsewhere
        # on these two objects instead of writing its requested representative
        # pair. Verify the actual current bridge and the original partition.
        assert bridges, "redo left no current fake edge connecting the pre-merge objects"
        result = {"current_fake_pairs": current, "bridges_between_premerge_objects": bridges}
    elif action == "journal":
        entries = list(journals())
        chosen = next((j for j in entries if j.data["operation_id"] == str(request.get("operation_id"))), None)
        if chosen is None and request.get("operation_id") is None and entries:
            chosen = max(entries, key=lambda j: (j.path / "journal.json").stat().st_mtime_ns)
        if chosen is None:
            raise ValueError("No matching refinement journal")
        result = dict(chosen.data)
        result["epoch"] = read_epoch()
        result["affected_l2_chunks"] = {
            node: list(map(int, cg.get_chunk_coordinates(np.uint64(node))))
            for node in chosen.data["l2"]}
        result["fenced_roots_now"] = sorted(fenced_roots())
        result["owned_roots_now"] = [root for root in chosen.data["roots"]
                                     if cg.client.get_lock_timestamp(np.uint64(root),
                                         np.uint64(chosen.data["operation_id"])) is not None]
        result["payloads_retained"] = all((chosen.path / entry["source"]).exists()
                                          for entry in chosen.data.get("files", []))
    elif action == "snapshot_busy":
        from supervoxel_history import PublicationBusy
        try:
            snapshot_read(lambda: live(), timeout=.1)
        except PublicationBusy:
            result = {"bounded_error": True}
        else:
            raise AssertionError("reader accepted an odd publication epoch")
    elif action == "log_cursor":
        result = {"operation_id": int(cg.client.get_max_operation_id())}
    elif action == "cross_edges":
        edges = cg.get_atomic_cross_edges(np.asarray([request["node"]], np.uint64))
        result = {str(layer): np.asarray(pairs, np.uint64).tolist()
                  for layer, pairs in edges.get(np.uint64(request["node"]), {}).items()}
    elif action == "no_logs_since":
        maximum = int(cg.client.get_max_operation_id())
        inspected = list(range(int(request["operation_id"]) + 1, maximum + 1))
        for operation in inspected:
            record, _ = cg.client.read_log_entry(np.uint64(operation))
            assert not record, f"fenced native edit wrote operation-log cells for {operation}"
        result = {"operation_ids_checked": inspected, "native_edit_log_cells": 0}
    elif action == "interleaved_reader":
        from supervoxel_refine import atomic_write
        attempts = 0
        expected = saved(request["name"])["root_image"] == request["root"]
        marker, control = Path(request["marker"]), Path(request["control"])

        def read():
            nonlocal attempts
            attempts += 1
            leaves = cg.get_subgraph_leaves(np.uint64(request["root"]))
            if attempts == 1:
                atomic_write(marker, b"ready\n")
                deadline = time.monotonic() + 60
                while control.exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError("interleaved reader control was not released")
                    time.sleep(.025)
            return np.isin(np.asarray(volume[:][..., 0]), leaves)

        actual = snapshot_read(read, timeout=30)
        assert np.array_equal(actual, expected), "snapshot reader returned a mask with holes"
        assert attempts >= 2, "interleaving did not force snapshot retry"
        result = {"attempts": attempts, "holes": 0, "epoch": read_epoch()}
    else:
        raise ValueError("Unknown internal probe action: " + action)
    print("SVSPLIT_RESULT=" + json.dumps(result), flush=True)


class Probe:
    def __init__(self, process, output, path):
        self.process, self.output, self.path = process, output, path

    def finish(self, timeout=300, expected=(0,)):
        code = self.process.wait(timeout=timeout)
        self.output.close()
        text = self.path.read_text()
        if code not in expected:
            raise RuntimeError(f"Internal probe exit {code}; log {self.path}:\n{text[-6000:]}")
        found = [line[len("SVSPLIT_RESULT="):] for line in text.splitlines()
                 if line.startswith("SVSPLIT_RESULT=")]
        return json.loads(found[-1]) if found else {"exit_code": code, "log": str(self.path)}


class Verifier:
    def __init__(self, destination):
        from verify_segmentation_iteration import require_disposable
        self.dest = Path(destination).resolve()
        self.spec = json.loads((self.dest / "deployment.json").read_text())
        require_disposable(self.dest, self.spec)
        if self.spec.get("atom_policy") != "agglomerated_supervoxels":
            raise ValueError("Supervoxel refinement acceptance requires agglomerated_supervoxels")
        if not self.spec.get("supervoxel_split", {}).get("enabled"):
            raise ValueError("Enable supervoxel_split in this disposable fixture before acceptance")
        self.compose = ["docker", "compose", "--env-file", str(self.dest / ".env"),
                        "-f", str(self.dest / "compose.yml")]
        self.base = self.spec["local_url"].rstrip("/") + "/segmentation/api/v1/table/" + self.spec["graph_id"]
        self.folder = self.dest / "data/supervoxel_splits/verification"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"
        self.report_path = self.folder / ("report-" + self.run_id + ".json")
        self.fault = self.dest / "data/supervoxel_splits/fault.json"
        if self.fault.exists():
            raise ValueError("An existing fixture fault control must be cleared by its owner before acceptance")
        self.marker = Path(str(self.fault) + ".reached")
        self.serial = 0
        self.executor = ThreadPoolExecutor(max_workers=6)
        self.report = {"passed": False, "project": self.spec["project_name"], "graph": self.spec["graph_id"],
                       "run_id": self.run_id, "cases": {key: {"description": title, "status": "pending"}
                                                         for key, title in CASES.items()}}
        self.candidate = self.candidate_hashes()
        self.report["candidate_sha256"] = self.candidate
        self.saved = {}

    def candidate_hashes(self):
        source = Path(__file__).resolve().parent
        names = ("supervoxel_split.py", "supervoxel_refine.py", "supervoxel_history.py",
                 "supervoxel_service.py", "source_label_policy.py", "local_mesh.py", "wsgi.py")
        hashes = {}
        for name in names:
            expected = hashlib.sha256((source / name).read_bytes()).hexdigest()
            actual = hashlib.sha256((self.dest / "scripts" / name).read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"Disposable fixture runtime differs from the candidate: {name}")
            hashes[name] = expected
        hashes["verify_supervoxel_split.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        return hashes

    def write_report(self):
        self.report_path.write_text(json.dumps(self.report, indent=2) + "\n")

    def name(self, name):
        self.serial += 1
        return f"{self.run_id}-{self.serial:04d}-{name}"

    def start_probe(self, action, **values):
        request = {"action": action, "project": self.spec["project_name"], **values}
        source = "__name__ = 'svsplit_internal'\n" + Path(__file__).read_text()
        source += "\n_internal_main(" + repr(request) + ")\n"
        path = self.folder / (self.name(action) + ".log")
        output = path.open("w")
        process = subprocess.Popen(self.compose + ["exec", "-T", "pcg", "python", "-"],
                                   stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT)
        process.stdin.write(source.encode())
        process.stdin.close()
        return Probe(process, output, path)

    def probe(self, action, **values):
        return self.start_probe(action, **values).finish()

    def snapshot(self, label):
        return self.probe("snapshot", name=self.name(label))

    def inventory(self):
        return self.probe("inventory")["atoms"]

    def choose(self, *, source=None, exclude=(), min_count=8, root=None, chunk=None):
        options = [atom for atom in self.inventory() if atom["count"] >= min_count
                   and atom["atom"] not in exclude
                   and (source is None or atom["source"] == source)
                   and (root is None or atom["root"] == root)
                   and (chunk is None or atom["chunk"] == chunk)]
        if not options:
            raise RuntimeError("Fixture has no remaining atom matching the requested case geometry")
        return min(options, key=lambda atom: (-atom["count"], atom["atom"]))

    def disposable_atom(self):
        # Reserve the explicit contact, fake and cross-chunk pairs for their cases.
        return self.choose(source=int(np.iinfo(np.uint64).max), min_count=8)

    def refine_request(self, atom):
        low = np.asarray(atom["bounds"][:3])
        points = [(np.asarray(p) - low)[::-1].tolist() for p in atom["points"]]
        return {"atom": atom["atom"], "source_vox": [points[0]], "sink_vox": [points[1]]}

    def refine(self, atom, grid=(4, 4, 4)):
        return self.probe("refine", requests=[self.refine_request(atom)], grid=list(grid))

    def node(self, atom, point=0):
        return [str(atom["atom"]), *(np.asarray(atom["points"][point]) * self.spec["resolution_nm_xyz"]).tolist()]

    def post(self, route, body, *, expected=200, timeout=300):
        response = requests.post(self.base + route + "?int64_as_str=true", json=body, timeout=timeout)
        if expected is not None and response.status_code != expected:
            raise AssertionError(f"{route}: expected {expected}, got {response.status_code}: {response.text[:3000]}")
        return response

    def split(self, a, b=None, *, expected=200):
        bnode = self.node(b) if b is not None else self.node(a, 1)
        return self.post("/split", {"sources": [self.node(a)], "sinks": [bnode]}, expected=expected)

    def undo(self, operation, *, expected=200):
        result = self.post("/undo", {"operation_id": str(operation)}, expected=expected)
        if result.ok:
            self.probe("operation_log", operation_id=result.json()["operation_id"])
        return result

    def redo(self, operation):
        result = self.post("/redo", {"operation_id": str(operation)})
        assert result.json().get("operation_id") is not None, "redo unexpectedly became a no-op"
        self.probe("operation_log", operation_id=result.json()["operation_id"])
        return result

    def set_fault(self, point, action="pause", *, replace=False, **values):
        if self.fault.exists() and not replace:
            raise RuntimeError("Refusing to replace an existing fixture fault control")
        if replace and not self.fault.exists():
            raise RuntimeError("Cannot advance a fixture fault control that is absent")
        self.marker.unlink(missing_ok=True)
        self.fault.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.fault.with_suffix(".tmp")
        temporary.write_text(json.dumps({"point": point, "action": action, **values}))
        os.replace(temporary, self.fault)

    def clear_fault(self):
        self.fault.unlink(missing_ok=True)

    def wait_marker(self, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.marker.exists():
                return json.loads(self.marker.read_text())
            time.sleep(.05)
        raise TimeoutError("Fixture fault point was not reached")

    def paused_refine(self, atom, point, **fault_options):
        self.set_fault(point, **fault_options)
        process = self.start_probe("refine", requests=[self.refine_request(atom)], grid=[4, 4, 4])
        try:
            marker = self.wait_marker()
        except Exception:
            self.clear_fault()
            process.finish()
            raise
        return process, marker

    @contextmanager
    def temporary_options(self, **changes):
        path = self.dest / "deployment.json"
        original = path.read_bytes()
        spec = json.loads(original)
        spec["supervoxel_split"].update(changes)
        path.write_text(json.dumps(spec, indent=2) + "\n")
        try:
            self.restart()
            yield
        finally:
            path.write_bytes(original)
            self.restart()

    def restart(self):
        # The same guarded Compose project and only its PCG service are touched.
        subprocess.run(self.compose + ["restart", "pcg"], check=True, capture_output=True, text=True)
        deadline = time.monotonic() + 120
        info = (self.spec["local_url"].rstrip("/") + "/segmentation/table/"
                + self.spec["graph_id"] + "/info")
        while time.monotonic() < deadline:
            try:
                if requests.get(info, timeout=3).ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(.5)
        raise TimeoutError("Disposable fixture PCG service did not restart")

    def case_b(self):
        atom = self.disposable_atom()
        before = self.snapshot("before-refinement")
        result = self.refine(atom)
        assert result["count"] == 1 and result["new_ids"]
        after = self.snapshot("after-refinement")
        compare = self.probe("compare", name=before["snapshot"])
        self.saved.update(atom=atom, before=before, after=after, refinement=result)
        return {"refinement": result, "all_voxel_roots": compare}

    def case_e(self):
        return self.probe("mask_changed", atom=self.saved["atom"]["atom"],
                          before=self.saved["before"]["snapshot"], after=self.saved["after"]["snapshot"])

    def http_history(self, snapshot):
        from cloudvolume import CloudVolume
        from supervoxel_history import load_atom_tables
        tables = load_atom_tables(self.dest / "data")
        stamp = datetime.fromisoformat(snapshot["timestamp"]).timestamp()
        response = requests.post(self.base + f"/roots?timestamp={stamp}&int64_as_str=true",
                                 json={"node_ids": [str(int(i)) for i in tables.ids]}, timeout=60)
        response.raise_for_status()
        roots = np.asarray(response.json()["root_ids"], np.uint64)
        atomic = np.asarray(CloudVolume(self.spec["local_url"].rstrip("/") + "/data/atomic",
                                      progress=False, cache=False)[:][..., 0])
        reconstructed = np.zeros(atomic.shape, np.uint64)
        mask = atomic != 0
        reconstructed[mask] = roots[np.searchsorted(tables.ids, atomic[mask])]
        expected = np.load(self.folder / (snapshot["snapshot"] + ".npz"))["root_image"]
        assert np.array_equal(reconstructed, expected), "HTTP export-style historical reconstruction differs"
        assert "X-Cave-Svsplit-Epoch" in response.headers
        return {"voxels": int(atomic.size), "sha256": _digest(reconstructed), "epoch": response.headers["X-Cave-Svsplit-Epoch"]}

    def case_c(self):
        before = self.saved["before"]
        first = self.http_history(before)
        descendants = set(map(int, next(iter(self.saved["refinement"]["new_ids"].values()))))
        atom = max((a for a in self.inventory() if a["atom"] in descendants), key=lambda a: a["count"])
        repeated = self.refine(atom, (2, 2, 2))
        assert repeated["count"] == 1
        self.probe("compare", name=before["snapshot"], history=True)
        return {"first_export": first, "repeated_refinement": repeated, "repeated_export": self.http_history(before)}

    def case_a(self):
        atom = self.disposable_atom()
        before = self.snapshot("before-split")
        response = self.split(atom)
        assert int(response.headers.get("X-Cave-Supervoxels-Refined", "0")) >= 1
        point_atoms = self.at_points(atom["points"])
        assert point_atoms[0]["root"] != point_atoms[1]["root"]
        self.saved["same_atom_split"] = (before, response.json())
        return {"operation": response.json(), "refined": response.headers["X-Cave-Supervoxels-Refined"]}

    def at_points(self, points):
        from cloudvolume import CloudVolume
        volume = CloudVolume("file://" + str(self.dest / "data/atomic"), progress=False, cache=False)
        ids = [int(volume[tuple(slice(int(v), int(v) + 1) for v in p)].reshape(-1)[0]) for p in points]
        inventory = {atom["atom"]: atom for atom in self.inventory()}
        return [inventory[atom] for atom in ids]

    def case_d(self):
        before, operation = self.saved["same_atom_split"]
        undone = self.undo(operation["operation_id"])
        return {"undo": undone.json(), "restored": self.probe("compare", name=before["snapshot"], partition=True)}

    def separated_pair(self, fake=False):
        if fake:
            a, b = self.choose(source=7, min_count=2), self.choose(source=17, min_count=2)
        else:
            # A contact-merge case needs actual touching *atoms*, not merely two
            # objects with contacts elsewhere after an earlier refinement.
            from cloudvolume import CloudVolume
            atomic = np.asarray(CloudVolume("file://" + str(self.dest / "data/atomic"),
                                            progress=False, cache=False)[:][..., 0])
            inventory = {atom["atom"]: atom for atom in self.inventory()}
            options = []
            for axis in range(3):
                lo, hi = [slice(None)] * 3, [slice(None)] * 3
                lo[axis], hi[axis] = slice(None, -1), slice(1, None)
                left, right = atomic[tuple(lo)], atomic[tuple(hi)]
                mask = (left != right) & (left != 0) & (right != 0)
                for one, two in np.unique(np.stack((left[mask], right[mask]), axis=1), axis=0):
                    first, second = inventory[int(one)], inventory[int(two)]
                    if first["source"] == 2**63 + 123:
                        first, second = second, first
                    if first["source"] == 7 and second["source"] == 2**63 + 123 and first["count"] >= 2:
                        options.append((first, second))
            if not options:
                raise RuntimeError("No refinable touching atom pair remains for the contact-merge case")
            a, b = min(options, key=lambda pair: (pair[0]["root"] == pair[1]["root"],
                                                  -pair[0]["count"], -pair[1]["count"], pair[0]["atom"]))
        if a["root"] == b["root"]:
            self.split(a, b)
            if not fake:
                # A retry may refine the initial endpoints during this setup
                # split; select their actual touching descendants afresh.
                return self.separated_pair(False)
            a, b = self.at_points([a["points"][0], b["points"][0]])
        assert a["root"] != b["root"]
        return a, b

    def case_d2(self):
        runs = []
        for fake in (False, True):
            for depth in (1, 2):
                a, b = self.separated_pair(fake)
                pre = self.snapshot("pre-merge")
                time.sleep(.15)  # Native merge undo reads connectivity 100 ms before its log time.
                merge = self.post("/merge", [self.node(a), self.node(b)]).json()
                merged = self.snapshot("merged")
                refinements = []
                current = a
                for level in range(depth):
                    assert current["count"] >= 2, "repeated refinement requires a nonsingleton descendant"
                    refinement = self.refine(current, (2, 2, 2))
                    refinements.append(refinement)
                    if level + 1 < depth:
                        descendants = set(map(int, next(iter(refinement["new_ids"].values()))))
                        current = max((item for item in self.inventory() if item["atom"] in descendants),
                                      key=lambda item: item["count"])
                undo = self.undo(merge["operation_id"]).json()
                self.probe("compare", name=pre["snapshot"], partition=True)
                no_edges = self.probe("no_edges", left=a["atom"], right=b["atom"])
                redo = self.redo(merge["operation_id"]).json()
                self.probe("compare", name=merged["snapshot"], partition=True)
                if fake:
                    self.probe("fake_current", atom=a["atom"], other=b["atom"], before=pre["snapshot"])
                self.undo(redo["operation_id"])
                self.probe("compare", name=pre["snapshot"], partition=True)
                runs.append({"kind": "fake" if fake else "contact", "depth": depth,
                             "merge": merge, "refinements": refinements, "undo": undo,
                             "redo": redo, "no_active_edges_after_undo": no_edges})
        # Undo/redo of a split whose cut endpoint is refined afterward.
        a, b = self.separated_pair(False)
        merge = self.post("/merge", [self.node(a), self.node(b)]).json()
        merged = self.snapshot("pre-native-split")
        split = self.split(a, b).json()
        split_image = self.snapshot("post-native-split")
        split_log = self.probe("operation_log", operation_id=split["operation_id"])
        endpoints = set(map(int, np.asarray(split_log["RemovedEdge"]).ravel()))
        cut_atoms = [atom for atom in self.inventory() if atom["atom"] in endpoints and atom["count"] >= 2]
        assert cut_atoms, "split fixture has no refinable endpoint on its cut"
        self.refine(max(cut_atoms, key=lambda atom: atom["count"]), (2, 2, 2))
        undo = self.undo(split["operation_id"]).json()
        self.probe("compare", name=merged["snapshot"], partition=True)
        redo = self.redo(split["operation_id"]).json()
        self.probe("compare", name=split_image["snapshot"], partition=True)
        runs.append({"kind": "split", "split": split, "undo": undo, "redo": redo, "setup_merge": merge})
        return {"runs": runs}

    def case_d3(self):
        outcomes = []
        for kind in ("undo", "redo"):
            a, b = self.separated_pair(True)
            pre = self.snapshot("stale-pre-merge")
            time.sleep(.15)
            merge = self.post("/merge", [self.node(a), self.node(b)]).json()
            merged = self.snapshot("stale-merged")
            operation = merge["operation_id"]
            if kind == "redo":
                self.undo(operation)
            retry_path = self.dest / "data/supervoxel_splits/history-retries.jsonl"
            previous = len(retry_path.read_text().splitlines()) if retry_path.exists() else 0
            self.set_fault("after_translation")
            future = self.executor.submit(self.post, "/" + kind, {"operation_id": str(operation)})
            try:
                marker = self.wait_marker()
                translated = set(map(int, marker["translated_ids"]))
                choices = [atom for atom in self.inventory() if atom["atom"] in translated and atom["count"] >= 2]
                assert choices, "stale-translation fixture has no refinable effective endpoint"
                current = max(choices, key=lambda atom: atom["count"])
                refinement = self.refine(current, (1, 2, 2))
            finally:
                self.clear_fault()
            response = future.result(timeout=300).json()
            entries = ([json.loads(line) for line in retry_path.read_text().splitlines()[previous:]]
                       if retry_path.exists() else [])
            matching = [entry for entry in entries if str(entry["superseded_operation_id"]) == str(operation)]
            assert len(matching) == 1 and matching[0]["attempt"] == 1, matching
            self.probe("operation_log", operation_id=response["operation_id"])
            expected = pre if kind == "undo" else merged
            self.probe("compare", name=expected["snapshot"], partition=True)
            if kind == "undo":
                self.probe("no_edges", left=a["atom"], right=b["atom"])
            else:
                self.undo(response["operation_id"])
            outcomes.append({"kind": kind, "retry": matching, "refinement": refinement, "operation": response})
        return {"interleavings": outcomes}

    def case_f(self):
        outcomes = []
        # The isolated 41/42 object has a known finite cut across its source
        # boundary. Arbitrary points in the large solid seam-connected fixture
        # need not admit a successful native cut after only one refinement.
        # Each error gets a finer grid so both requests have a refinable atom,
        # while max_atoms=1 still exercises the production work bound.
        for error, grid in (("mincut", [4, 4, 4]), ("isolating", [2, 2, 2])):
            with self.temporary_options(max_atoms=1, grid_xyz=grid):
                for calls in (1, 2):
                    a, b = self.choose(source=41), self.choose(source=42)
                    assert a["root"] == b["root"] and a["atom"] != b["atom"]
                    before = self.snapshot("forced-mincut")
                    call_dir = self.dest / "data/supervoxel_splits/native_calls"
                    old_files = set(call_dir.glob("*.json")) if call_dir.exists() else set()
                    self.set_fault("native", "inject", error=error, calls=calls)
                    try:
                        response = self.split(a, b, expected=None)
                    finally:
                        self.clear_fault()
                    new_files = set(call_dir.glob("*.json")) - old_files
                    assert len(new_files) == 1
                    evidence = json.loads(new_files.pop().read_text())
                    assert evidence["calls"] == 2 and evidence["refined"] == 1, evidence
                    coords = [node[1:] for side in ("sources", "sinks") for node in evidence["body"][side]]
                    points = (np.asarray(coords) / self.spec["resolution_nm_xyz"]).astype(int)
                    live = self.at_points(points)
                    remapped = [int(node[0]) for side in ("sources", "sinks") for node in evidence["body"][side]]
                    assert remapped == [node["atom"] for node in live]
                    if calls == 2:
                        assert response.status_code == 400 and "split still failed" in response.text
                        self.probe("compare", name=before["snapshot"], partition=True)
                    else:
                        assert response.ok, response.text
                        self.undo(response.json()["operation_id"])
                        self.probe("compare", name=before["snapshot"], partition=True)
                    outcomes.append({"error": error, "forced_calls": calls, "status": response.status_code,
                                     "grid_xyz": grid, "native_calls": evidence})
        return {"triggers": outcomes}

    def case_p1(self):
        a, b = self.choose(source=31), self.choose(source=32)
        assert a["chunk"] != b["chunk"], "P1 pair must straddle a graph chunk boundary"
        if a["root"] == b["root"]:
            self.split(a, b)
            a, b = self.at_points([a["points"][0], b["points"][0]])
        assert a["root"] != b["root"]
        before = self.snapshot("different-root-neighbor")
        previous_cross = self.probe("cross_edges", node=b["parent"])
        process, marker = self.paused_refine(a, "7c")
        try:
            journal = self.probe("journal", operation_id=marker["operation_id"])
            assert {a["root"], b["root"]}.issubset(set(map(int, journal["roots"])))
            assert journal["roots"] == sorted(set(journal["roots"]))
            assert journal["owned_roots_now"] == journal["roots"]
            assert set(journal["root_timestamps"]) == set(map(str, journal["roots"]))
            assert {a["root"], b["root"]}.issubset(journal["fenced_roots_now"])
            affected = {entry["root"] for entry in journal["l2"].values()}
            assert {a["root"], b["root"]}.issubset(affected)
            assert str(b["parent"]) in journal["l2"], "the actual cross-chunk neighbour L2 was not locked"
            chunks = set(map(tuple, journal["affected_l2_chunks"].values()))
            assert {tuple(a["chunk"]), tuple(b["chunk"])}.issubset(chunks)
            self.probe("boundary", name=before["snapshot"])
        finally:
            self.clear_fault()
        result = process.finish()
        self.probe("compare", name=before["snapshot"])
        completed = self.probe("journal", operation_id=marker["operation_id"])
        assert completed["state"] == "done"
        neighbor_cells = [cell for cell in completed["cells"]["7f"]
                          if cell["attribute"] == "CrossChunkEdge" and int(cell["row"]) == b["parent"]]
        assert all(a["atom"] not in np.asarray(cell["value"]).ravel() for cell in neighbor_cells)
        previous_refs = sum(int(np.count_nonzero(np.asarray(pairs) == a["atom"]))
                            for pairs in previous_cross.values())
        if previous_refs:
            assert neighbor_cells, "an existing different-root CrossChunkEdge reference was not rewritten"
        else:
            # PCG 2.21.1 removes cut CrossChunkEdge cells from both successor L2
            # rows. Their stored inactive edge still contributes both root locks;
            # refinement must not recreate a connectivity cell removed by the cut.
            assert not neighbor_cells
        return {"roots": journal["roots"], "affected_l2": journal["l2"],
                "affected_l2_chunks": journal["affected_l2_chunks"],
                "refinement": result, "unchanged_neighbor_root": b["root"],
                "neighbor_cross_references_before": previous_refs,
                "neighbor_cross_cells_rewritten": len(neighbor_cells)}

    def case_g(self):
        outcomes = []
        for point in ("prepared", "fenced", "7a-symlink", "7a", "7b", "7c", "7d", "7e", "7f"):
            atom = self.disposable_atom()
            before = self.snapshot("crash-" + point)
            self.set_fault(point, "exit")
            try:
                probe = self.start_probe("refine", requests=[self.refine_request(atom)], grid=[4, 4, 4])
                exit_result = probe.finish(expected=(86,))
                marker = self.wait_marker()
                journal = self.probe("journal", operation_id=marker["operation_id"])
                assert journal["payloads_retained"], "recovery payload disappeared before done"
                orphan = None
                if point == "7a-symlink":
                    orphan = self.dest / "data" / Path(marker["temporary_link"]).relative_to("/data")
                    assert orphan.is_symlink(), "symlink fault did not retain the interrupted temporary link"
                installed = {}
                for entry in journal.get("files", []):
                    path = self.dest / "data" / Path(entry["destination"]).relative_to("/data")
                    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]:
                        installed[str(path)] = path.stat().st_mtime_ns
                if point in ("7c", "7d", "7e", "7f"):
                    assert installed, f"{point}: no installed destination exercises hash-skip replay"
            finally:
                self.clear_fault()
            recovered = self.probe("recover")
            if orphan is not None:
                assert not os.path.lexists(orphan), "recovery left an interrupted atom-table link behind"
            self.probe("compare", name=before["snapshot"])
            self.http_history(before)
            if point in ("prepared", "fenced"):
                after = self.snapshot("aborted-" + point)
                assert after["atomic_sha256"] == before["atomic_sha256"]
            else:
                done = self.probe("journal", operation_id=marker["operation_id"])
                assert done["state"] == "done" and set(done["markers"]) == {"7a", "7b", "7c", "7d", "7e", "7f"}
                for entry in done.get("files", []):
                    path = self.dest / "data" / Path(entry["destination"]).relative_to("/data")
                    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
                for path, timestamp in installed.items():
                    assert Path(path).stat().st_mtime_ns == timestamp, "replay rewrote an already matching destination"
                self.probe("boundary", name=before["snapshot"])
            outcomes.append({"point": point, "exit": exit_result, "journal_state_at_crash": journal["state"],
                             "recovery": recovered, "hash_skipped_destinations": len(installed),
                             "interrupted_symlink_removed": str(orphan) if orphan is not None else None})
        return {"crash_points": outcomes}

    def case_g2(self):
        outcomes = []
        for point in ("7a", "7b", "7c", "7d", "7e", "7f"):
            atom = self.disposable_atom()
            before = self.snapshot("exception-" + point)
            self.set_fault(point, "raise_once")
            try:
                failed = self.start_probe("refine", requests=[self.refine_request(atom)], grid=[4, 4, 4])
                failure = failed.finish(expected=(1,))
                marker = self.wait_marker()
            finally:
                self.clear_fault()
            # No explicit recover, subsequent edit or worker restart may mask
            # failure to recover before the failed request returns.
            journal = self.probe("journal", operation_id=marker["operation_id"])
            assert journal["state"] == "done" and not journal["fenced_roots_now"]
            snapshot = self.snapshot("exception-recovered-" + point)
            assert snapshot["epoch"] % 2 == 0 and snapshot["epoch"] > before["epoch"]
            historical = self.http_history(before)
            current = self.at_points([atom["points"][0]])[0]
            roots = self.post("/roots", {"node_ids": [str(current["atom"])]})
            leaves = requests.get(self.base + f"/node/{current['root']}/leaves", timeout=15)
            assert leaves.ok, leaves.text
            self.probe("compare", name=before["snapshot"])
            outcomes.append({"point": point, "failure": failure, "state_before_public_read": journal["state"],
                             "epoch_before_public_read": snapshot["epoch"], "history": historical,
                             "public_roots_status": roots.status_code, "public_leaves_status": leaves.status_code})
        atom = self.disposable_atom()
        before = self.snapshot("replay-exception")
        self.set_fault("7c", "raise")
        try:
            failed = self.start_probe("refine", requests=[self.refine_request(atom)], grid=[4, 4, 4])
            failure = failed.finish(expected=(1,))
            marker = self.wait_marker()
            pending = self.probe("journal", operation_id=marker["operation_id"])
            assert pending["state"] == "publishing" and pending["epoch"] % 2
            blocked = self.post("/roots", {"node_ids": [str(atom["atom"])]}, expected=503, timeout=15)
            assert blocked.headers.get("Retry-After") == "1"
            failed_attempt = self.probe("journal", operation_id=marker["operation_id"])
            throttled_reads = []
            for _ in range(3):
                throttled = self.post("/roots", {"node_ids": [str(atom["atom"])]}, expected=503, timeout=15)
                throttled_reads.append(throttled.status_code)
            throttled_journal = self.probe("journal", operation_id=marker["operation_id"])
            assert throttled_journal["replay_count"] == failed_attempt["replay_count"], "reader traffic bypassed recovery cooldown"
        finally:
            self.clear_fault()
        # This public reader is the only recovery trigger after the transient
        # failure clears. No edit or worker restart may be needed to restore reads.
        # Failed reader recovery has a bounded cooldown shared by all workers.
        # Only public reads retry after that interval; the verifier never repairs.
        deadline = time.monotonic() + 35
        recovery_reads = []
        while True:
            recovered_read = self.post("/roots", {"node_ids": [str(atom["atom"])]}, expected=None, timeout=15)
            recovery_reads.append(recovered_read.status_code)
            if recovered_read.status_code == 200:
                break
            assert recovered_read.status_code == 503, recovered_read.text
            assert time.monotonic() < deadline, "public reader did not recover after the failure cooldown"
            time.sleep(1)
        completed = self.probe("journal", operation_id=marker["operation_id"])
        assert completed["state"] == "done" and completed["epoch"] % 2 == 0
        assert not completed["fenced_roots_now"]
        self.probe("compare", name=before["snapshot"])
        return {"non_lock_exceptions": outcomes,
                "failed_replay_reader_recovery": {"failure": failure, "initial_state": pending["state"],
                    "initial_epoch": pending["epoch"], "blocked_read_status": blocked.status_code,
                    "cooldown_read_statuses": throttled_reads,
                    "replay_count_during_cooldown": throttled_journal["replay_count"],
                    "recovery_read_statuses": recovery_reads,
                    "recovered_read_status": recovered_read.status_code, "final_state": completed["state"],
                    "final_epoch": completed["epoch"]}}

    def case_g3(self):
        outcomes = []
        for point in ("prepared", "validate-preconditions"):
            atom = self.disposable_atom()
            before = self.snapshot("prepublication-exception-" + point)
            self.set_fault(point, "raise_once")
            try:
                failed = self.start_probe("refine", requests=[self.refine_request(atom)], grid=[4, 4, 4])
                failure = failed.finish(expected=(1,))
                marker = self.wait_marker()
            finally:
                self.clear_fault()
            unpublished = self.probe("unpublished", operation_id=marker["operation_id"])
            assert unpublished["state"] == "aborted" and not unpublished["fenced_roots_now"]
            assert unpublished["epoch"] % 2 == 0
            assert unpublished["atomic_sha256"] == before["atomic_sha256"]
            assert unpublished["generation"] == before["generation"]
            outsider = self.outsider(atom)
            merge = self.post("/merge", [self.node(atom), self.node(outsider)]).json()
            self.undo(merge["operation_id"])
            self.probe("compare", name=before["snapshot"], partition=True)
            outcomes.append({"point": point, "failure": failure, "unpublished": unpublished,
                             "native_merge_after_abort": merge})
        return {"prepublication_exceptions": outcomes}

    def barrier_requests(self, snapshot, atom):
        stamp = datetime.fromisoformat(snapshot["timestamp"]).timestamp()
        requests_to_run = [
            ("roots", lambda: self.post("/roots", {"node_ids": [str(atom["atom"])]}, expected=None, timeout=15)),
            ("history", lambda: requests.post(self.base + f"/roots?timestamp={stamp}",
                                              json={"node_ids": [str(atom["atom"])]}, timeout=15)),
            ("leaves", lambda: requests.get(self.base + f"/node/{atom['root']}/leaves", timeout=15)),
        ]
        futures = [(name, self.executor.submit(call)) for name, call in requests_to_run]
        results = []
        for name, future in futures:
            response = future.result()
            assert response.status_code == 503 and response.headers.get("Retry-After") == "1", response.text
            results.append({"handler": name, "status": response.status_code,
                            "epoch": response.headers.get("X-Cave-Svsplit-Epoch")})
        return results

    def case_j(self):
        boundaries = []
        for point in ("7a", "7b", "7c", "7d", "7e", "7f"):
            atom = self.disposable_atom()
            before = self.snapshot("boundary-" + point)
            process, marker = self.paused_refine(atom, point)
            try:
                probe = self.probe("boundary", name=before["snapshot"])
                assert probe["epoch"] % 2
                self.probe("snapshot_busy")
                public = self.barrier_requests(before, atom)
            finally:
                self.clear_fault()
            process.finish()
            historical = self.http_history(before)
            assert int(historical["epoch"]) > before["epoch"] and int(historical["epoch"]) % 2 == 0
            boundaries.append({"point": point, "internal": probe, "public": public, "after": historical})
        return {"boundaries": boundaries}

    def case_l(self):
        atom = self.disposable_atom()
        before = self.snapshot("interleaved-read")
        stem = self.name("reader")
        control, marker = self.folder / (stem + ".control"), self.folder / (stem + ".ready")
        control.write_text("hold\n")
        reader = self.start_probe("interleaved_reader", name=before["snapshot"], root=atom["root"],
                                  control="/data/supervoxel_splits/verification/" + control.name,
                                  marker="/data/supervoxel_splits/verification/" + marker.name)
        deadline = time.monotonic() + 60
        while not marker.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Snapshot reader did not fetch initial leaves")
            time.sleep(.05)
        writer, _ = self.paused_refine(atom, "7e")
        control.unlink()
        # A public request started during the odd epoch waits and succeeds when
        # the publication resumes inside its five-second bound.
        public = self.executor.submit(self.post, "/roots", {"node_ids": [str(atom["atom"])]})
        time.sleep(.25)
        self.clear_fault()
        writer.finish()
        result = reader.finish()
        response = public.result()
        assert int(response.headers["X-Cave-Svsplit-Epoch"]) > before["epoch"]
        return {"snapshot_reader": result, "public_wait_status": response.status_code,
                "public_epoch": response.headers["X-Cave-Svsplit-Epoch"]}

    def outsider(self, atom):
        return next(other for other in self.inventory() if other["root"] != atom["root"]
                    and np.max(np.abs(np.asarray(other["chunk"]) - atom["chunk"])) <= 3)

    def wait_native_expiry(self, seconds, label):
        print(f"{label}: waiting {seconds:.1f}s for this fixture's native root lease", flush=True)
        deadline = time.monotonic() + max(0, seconds)
        while time.monotonic() < deadline:
            time.sleep(min(.5, deadline - time.monotonic()))

    def case_m(self):
        atom = self.disposable_atom()
        before = self.snapshot("verified-crash")
        self.set_fault("verified", "exit", graceful=True)
        try:
            probe = self.start_probe("refine", requests=[self.refine_request(atom)], grid=[4, 4, 4])
            probe.finish(expected=(86,))
            marker = self.wait_marker()
        finally:
            self.clear_fault()
        journal = self.probe("journal", operation_id=marker["operation_id"])
        assert journal["state"] == "verified" and set(journal["roots"]).issubset(journal["fenced_roots_now"])
        current = self.at_points([atom["points"][0]])[0]
        outsider = self.outsider(current)
        refused = self.post("/merge", [self.node(current), self.node(outsider)], expected=503)
        self.probe("recover")
        after = self.probe("journal", operation_id=marker["operation_id"])
        assert after["state"] == "done"
        assert after.get("replay_count", 0) == journal.get("replay_count", 0)
        log = self.dest / "data/supervoxel_splits/log.jsonl"
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        assert sum(str(entry["operation_id"]) == str(marker["operation_id"]) for entry in entries) == 1
        self.probe("compare", name=before["snapshot"])
        merge = self.post("/merge", [self.node(current), self.node(outsider)]).json()
        self.undo(merge["operation_id"])
        self.probe("compare", name=before["snapshot"], partition=True)
        return {"operation_id": marker["operation_id"], "replay_count": after.get("replay_count", 0),
                "log_entries": 1, "final_state": after["state"], "native_merge_refused": refused.status_code,
                "native_merge_after_finalization": merge}

    def case_h(self):
        atoms = self.inventory()
        pair = next(((a, b) for a in atoms for b in atoms if a["atom"] < b["atom"]
                     and a["count"] >= 4 and b["count"] >= 4 and a["chunk"] == b["chunk"]
                     and a["root"] != b["root"]), None)
        assert pair is not None, "Fixture needs two different roots in the same chunk"
        before = self.snapshot("concurrent-refinements")
        old_ids = {atom["atom"] for atom in atoms}
        journal_folder = self.dest / "data/supervoxel_splits/journal"
        previous_journals = set(journal_folder.glob("*/journal.json"))
        futures = [self.executor.submit(self.split, atom) for atom in pair]
        responses = [future.result(timeout=300) for future in futures]
        assert all(int(response.headers.get("X-Cave-Supervoxels-Refined", "0")) >= 1 for response in responses)
        entries = [json.loads(path.read_text()) for path in
                   sorted(set(journal_folder.glob("*/journal.json")) - previous_journals)]
        assert len(entries) == 2 and {entry["old_atom"] for entry in entries} == {atom["atom"] for atom in pair}
        assert all(entry["state"] == "done" for entry in entries)
        allocated = _assert_disjoint_allocations(entries)
        after = self.inventory()
        new_ids = {atom["atom"] for atom in after if atom["atom"] not in old_ids}
        assert allocated == new_ids and len(allocated) >= 4, "allocated IDs differ from current new volume IDs"
        components = self.probe("allocated_components", allocated=sorted(allocated))
        for response in responses:
            self.undo(response.json()["operation_id"])
        self.probe("compare", name=before["snapshot"], partition=True)
        return {"http_splits": [response.json() for response in responses],
                "unique_new_ids": len(new_ids), "chunk": pair[0]["chunk"],
                "disjoint_journal_allocations": {entry["operation_id"]: entry["new_atoms"] for entry in entries},
                "allocated_components_and_roots": components}

    def case_i(self):
        atom, other = _switch_off_pair(self.inventory())
        before = self.snapshot("switch-off")
        with self.temporary_options(enabled=False):
            response = self.split(atom, expected=400)
            assert "indivisible" in response.text
            assert "X-Cave-Supervoxels-Refined" not in response.headers
            cross_chunk = self.split(atom, other, expected=400)
            assert "indivisible" in cross_chunk.text
            assert "X-Cave-Supervoxels-Refined" not in cross_chunk.headers
        self.probe("compare", name=before["snapshot"])
        after = self.snapshot("switch-off-after")
        assert after["atomic_sha256"] == before["atomic_sha256"]
        return {"same_atom_status": response.status_code, "cross_chunk_status": cross_chunk.status_code,
                "message": response.json().get("message"), "unchanged": True}

    def case_k(self):
        atom = self.disposable_atom()
        before = self.snapshot("verify-failed")
        self.set_fault("verify", "raise")
        try:
            failed = self.start_probe("refine", requests=[self.refine_request(atom)], grid=[4, 4, 4])
            failed.finish(expected=(1,))
            marker = self.wait_marker()
        finally:
            self.clear_fault()
        journal = self.probe("journal", operation_id=marker["operation_id"])
        assert journal["state"] == "verify_failed"
        current = self.at_points([atom["points"][0]])[0]
        outsider = self.outsider(current)
        response = self.post("/merge", [self.node(current), self.node(outsider)], expected=503)
        repair_log = self.folder / (self.name("cli-repair") + ".log")
        with repair_log.open("w") as output:
            subprocess.run([str(Path(__file__).resolve().parents[1] / "bin/cave"), "svsplit", "repair", str(self.dest)],
                           check=True, stdout=output, stderr=subprocess.STDOUT)
        self.probe("compare", name=before["snapshot"])
        merge = self.post("/merge", [self.node(current), self.node(outsider)]).json()
        self.undo(merge["operation_id"])
        self.probe("compare", name=before["snapshot"], partition=True)

        atom = self.disposable_atom()
        before_wait = self.snapshot("waiting-merge")
        outsider = self.outsider(atom)
        refining, paused = self.paused_refine(atom, "7c", exit_on_release=86)
        cursor = self.probe("log_cursor")["operation_id"]
        waiting = self.executor.submit(self.post, "/merge", [self.node(atom), self.node(outsider)], expected=None,
                                       timeout=400)
        time.sleep(.3)
        assert not waiting.done(), "native edit did not wait for the refinement lock"
        # Exit the direct probe at the fault point. Its real finally block
        # releases only its own locks; the journal remains fenced for the waiter.
        self.clear_fault()
        refining.finish(expected=(86,))
        response = waiting.result(timeout=400)
        assert response.status_code == 503, response.text
        no_cells = self.probe("no_logs_since", operation_id=cursor)
        self.probe("recover")
        self.probe("compare", name=before_wait["snapshot"])
        current = self.at_points([atom["points"][0]])[0]
        merge = self.post("/merge", [self.node(current), self.node(outsider)]).json()
        self.undo(merge["operation_id"])
        return {"verification_failure_fenced": True, "repair_restored": True, "repair_log": str(repair_log),
                "waiting_merge_status": response.status_code, "waiting_edit_no_log_cells": no_cells}

    def case_n(self):
        atom = self.disposable_atom()
        outsider = self.outsider(atom)
        expiry = self.probe("inventory")["root_lock_expiry"]
        self.set_fault("staging-expiry")
        process = self.start_probe("refine", requests=[self.refine_request(atom)], grid=[4, 4, 4])
        marker = self.wait_marker()
        try:
            staged = self.probe("unpublished", operation_id=marker["operation_id"])
            # Native clients use the fixture's actual expiry; no lock timestamps
            # or other deployments are changed to make this interleaving happen.
            self.wait_native_expiry(expiry + .5, "staging lock expiry")
            merge = self.post("/merge", [self.node(atom), self.node(outsider)]).json()
            merged = self.snapshot("expired-lock-merge")
            merged_root = self.at_points([atom["points"][0]])[0]["root"]
            # Change the existing pause atomically: its owner resumes and then
            # pauses after abort, before any recomputed payload is published.
            self.set_fault("retry-prepared", replace=True)
            retry = self.wait_marker()
            assert retry["point"] == "retry-prepared" and retry["operation_id"] == marker["operation_id"]
            aborted_state = self.probe("unpublished", operation_id=marker["operation_id"])
            assert aborted_state["state"] == "aborted"
            for key in ("atomic_sha256", "generation", "files"):
                assert aborted_state[key] == staged[key], f"expired preparation changed {key} before recompute"
            assert aborted_state["atomic_sha256"] == merged["atomic_sha256"]
            assert aborted_state["generation"] == merged["generation"]
        finally:
            self.clear_fault()
        output = process.finish()
        assert output["count"] == 1 and len(output["operation_ids"]) == 1, "expired-lock retry did not complete refinement"
        completed = self.probe("journal", operation_id=output["operation_ids"][0])
        assert completed["state"] == "done" and completed["root"] == merged_root
        self.probe("compare", name=merged["snapshot"])
        aborted = self.dest / "data/supervoxel_splits/journal/aborted" / str(marker["operation_id"]) / "journal.json"
        assert aborted.exists(), "expired-lock preparation was not retained as aborted"
        rejected = json.loads(aborted.read_text())
        assert rejected["state"] == "aborted" and not rejected.get("markers")
        self.undo(merge["operation_id"])
        return {"native_lock_expiry_seconds": expiry, "aborted_operation": marker["operation_id"],
                "native_merge": merge, "recomputed_refinement": output,
                "unpublished_before_expiry": staged, "unpublished_before_recompute": aborted_state,
                "recomputed_journal_state": completed["state"], "recomputed_root": completed["root"]}

    def run(self, cases=None):
        selected = list(cases) if cases is not None else list(CASES)
        unknown = set(selected) - set(CASES)
        if unknown:
            raise ValueError("Unknown fixture cases: " + ", ".join(sorted(unknown)))
        self.write_report()
        try:
            for case in selected:
                print(f"supervoxel_split case {case}: {CASES[case]}", flush=True)
                began = time.monotonic()
                try:
                    details = getattr(self, "case_" + case)()
                except Exception as error:
                    self.report["cases"][case].update(status="failed", seconds=time.monotonic() - began,
                                                     error=str(error), traceback=traceback.format_exc())
                    self.write_report()
                    raise
                self.report["cases"][case].update(status="passed", seconds=time.monotonic() - began, details=details)
                self.write_report()
            assert self.candidate_hashes() == self.candidate, "candidate changed during fixture verification"
            self.report["passed"] = all(case["status"] == "passed" for case in self.report["cases"].values())
            self.report["selected_cases_passed"] = True
        finally:
            self.clear_fault()
            self.write_report()
            self.executor.shutdown(wait=False, cancel_futures=True)
            print("Supervoxel refinement evidence: " + str(self.report_path), flush=True)
        return self.report


def verify(destination, cases=None):
    return Verifier(destination).run(cases)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--cases", help="Comma-separated diagnostic subset; omitted cases stay pending")
    options = parser.parse_args()
    result = verify(options.destination, options.cases.split(",") if options.cases else None)
    print(json.dumps(result, indent=2))
