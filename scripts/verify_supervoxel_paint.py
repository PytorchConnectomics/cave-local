"""Paint acceptance on one explicitly assigned disposable cave-pkg fixture.

Every selected case records its actual outcome. Unselected cases remain pending;
an unavailable fault, missing geometry, or unmet assertion fails verification.
"""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

import numpy as np
import requests

from verify_supervoxel_split import Verifier, Probe


CASES = {
    "pa": "neck paint refines only its two-label atom and records provenance",
    "pb": "multiple painted supervoxels separate with no refinement",
    "pc": "stray cut refuses before graph persistence",
    "pd": "seam refusal and a successful repaint inside one seam piece",
    "pd2": "outside-box bypass is separated or conservatively refused",
    "pc2": "distant real contact refuses despite nearby atom bounds",
    "pc3": "fake edge locality follows the creating merge coordinates",
    "pe": "validation refusals leave graph and refinement unchanged",
    "pf": "paint undo and redo restore partitions and historical roots",
    "pg": "merge between refinement and native call cannot use stale object",
    "ph": "paint disabled refuses while point splits remain available",
    "pj": "one deadline bounds raster, graph read, solve and check",
    "pj2": "binding delay expires before persistence",
    "pj3": "marker delay remains ambiguous with zero write entries",
    "pj4": "both post-lock deadline gates prevent all row mutations",
    "pk": "retirement after remapping is rechecked under the native root lock",
    "pl": "body/stroke/point/work limits precede graph reads",
    "pi": "process crashes preserve bound provenance and native repair",
    "pi2": "ordinary write failures never become not_applied",
    "pi3": "partial graph rows without WRITE_STARTED retain guarded ambiguous recovery",
}


def _internal(request):
    """Diagnostics, recovery, or an explicit native merge in assigned PCG."""
    sys.path.insert(0, "/workspace/scripts")
    from pychunkedgraph.graph import attributes
    from pychunkedgraph.graph.chunkedgraph import ChunkedGraph
    from supervoxel_history import install_history
    from supervoxel_refine import install_fence
    from supervoxel_paint_history import recover_paint
    spec = json.loads(Path("/workspace/deployment.json").read_text())
    assert spec.get("test_fixture") is True
    assert spec["project_name"] == request["project"]
    assert spec["project_name"].startswith("cave-pkg-")
    install_fence()
    install_history()
    cg = ChunkedGraph(graph_id=spec["graph_id"])
    action = request["action"]
    if action == "recover_paint":
        result = recover_paint(cg, request_id=request.get("request_id"))
    elif action == "logs":
        result = {}
        for operation_id, record in cg.client.read_log_entries().items():
            versions = cg.client.read_node(np.uint64(operation_id), properties=attributes.OperationLogs.Status)
            result[str(int(operation_id))] = {
                "status": int(record[attributes.OperationLogs.Status]),
                "status_versions": sorted({int(cell.value) for cell in versions})}
    elif action == "operation":
        operation_id = np.uint64(request["operation_id"])
        record, timestamp = cg.client.read_log_entry(operation_id)
        versions = cg.client.read_node(operation_id, properties=attributes.OperationLogs.Status)
        roots = request.get("roots", [])
        result = {"operation_id": int(operation_id), "status": int(record[attributes.OperationLogs.Status]),
                  "status_versions": sorted({int(cell.value) for cell in versions}),
                  "timestamp": timestamp.isoformat() if timestamp else None,
                  "latest_roots": list(map(bool, cg.is_latest_roots(np.asarray(roots, np.uint64)))) if roots else [],
                  "indefinite_locks": {}}
        for root in roots:
            cells = cg.client.read_node(np.uint64(root), properties=attributes.Concurrency.IndefiniteLock)
            result["indefinite_locks"][str(root)] = [int(cell.value) for cell in cells]
    elif action == "graph_state":
        rows = cg.client._table.read_rows()
        rows.consume_all()
        cells = []
        names = {b"children", b"parents", b"former_parents", b"new_parents", b"fake_edges",
                 b"affinities", b"areas"}
        for key, row in rows.rows.items():
            for family, columns in row.cells.items():
                if family not in ("0", "3"):
                    continue
                for column, versions in columns.items():
                    if column in names or column.startswith(b"atomic_cross_edges_"):
                        cells.extend((key.hex(), family, column.hex(), cell.timestamp.isoformat(), cell.value.hex())
                                     for cell in versions)
        result = {"graph_sha256": hashlib.sha256(json.dumps(sorted(cells)).encode()).hexdigest(),
                  "graph_cell_versions": len(cells)}
    elif action == "merge_bridge":
        # One ordinary locked native merge adds a path through an external
        # component between both painted sides. Its two endpoints occupy
        # separate chunks, so each retains a separate reverse fake-edge cell.
        # The graph assertion below ensures
        # this race would invalidate a cut computed before the merge.
        source, sink, bridge_source, bridge_sink = map(int, request["atoms"])
        assert len({source, sink, bridge_source, bridge_sink}) == 4
        roots = cg.get_roots(np.asarray(request["atoms"], np.uint64))
        assert roots[0] == roots[1] and roots[2] == roots[3] and roots[0] != roots[2], roots
        assert cg.get_chunk_id(bridge_source) != cg.get_chunk_id(bridge_sink)
        edges = np.asarray([[source, bridge_source], [sink, bridge_sink]], np.uint64)
        source_point, sink_point, bridge_source_point, bridge_sink_point = request["points"]
        operation = cg.add_edges(user_id="paint-verifier-bridge", atomic_edges=edges,
            source_coords=[source_point, sink_point], sink_coords=[bridge_source_point, bridge_sink_point])
        current = cg.get_roots(np.asarray(request["atoms"], np.uint64))
        assert len(set(map(int, current))) == 1, current
        active = {tuple(sorted(map(int, edge))) for edge in cg.get_subgraph_edges(current[0])}
        assert all(tuple(sorted(map(int, edge))) in active for edge in edges), (edges, active)
        result = {"operation_id": int(operation.operation_id), "bridge_edges": edges.tolist(),
                  "new_root_ids": list(map(int, operation.new_root_ids))}
    else:
        raise ValueError("Unknown paint internal probe: " + action)
    print("PAINT_VERIFY_RESULT=" + json.dumps(result, default=lambda value: int(value)), flush=True)


class PaintProbe(Probe):
    def finish(self, expected=0, timeout=300):
        code = self.process.wait(timeout=timeout)
        self.output.close()
        text = self.path.read_text()
        if code != expected:
            raise RuntimeError(f"Paint probe exit {code}; log {self.path}:\n{text[-6000:]}")
        found = [line.split("=", 1)[1] for line in text.splitlines() if line.startswith("PAINT_VERIFY_RESULT=")]
        if not found:
            raise AssertionError("Paint probe emitted no result: " + str(self.path))
        return json.loads(found[-1])


def paint_body(geometry, resolution, *, radius_nm=None):
    spacing = np.asarray(resolution, float)
    radius_nm = float(spacing.min() * .4) if radius_nm is None else float(radius_nm)
    return {"paint": {"version": 1, **{
        name: [{"points_nm": [((np.asarray(point) + .5) * spacing).tolist()], "radius_nm": radius_nm}
               for point in geometry[name + "_vox"]]
        for name in ("sources", "sinks")}}}


def require_refusal(response, kind):
    assert response.status_code == 400, (response.status_code, response.text[:3000])
    value = response.json()
    assert value.get("paint_refusal", {}).get("kind") == kind, value
    assert isinstance(value.get("error"), str) and value["error"]
    assert len(value["paint_refusal"]["locations_nm"]) <= 20
    return value


def require_limit(response, name):
    value = require_refusal(response, "limit")
    assert name in value["error"], (name, value)
    return value


def require_seam_location(refusal, face_vox, resolution):
    locations = np.asarray(refusal["paint_refusal"]["locations_nm"], float).reshape(-1, 3)
    offsets = locations / np.asarray(resolution, float) - (np.asarray(face_vox, float) + .5)
    assert np.any(np.all(np.abs(offsets) <= 1, axis=1)), (locations.tolist(), face_vox)


class PaintVerifier(Verifier):
    def __init__(self, destination):
        super().__init__(destination)
        assert self.spec["supervoxel_split"].get("paint", {}).get("enabled") is True
        self.compose = ["docker", "compose", "--project-name", self.spec["project_name"],
                        "--project-directory", str(self.dest), "--env-file", str(self.dest / ".env"),
                        "-f", str(self.dest / "compose.yml")]
        expected = json.loads((Path(self.spec["source_dir"]) / "expected.json").read_text())
        self.geometry = expected["paint_geometry"]
        self.paint_dir = self.dest / "data/supervoxel_splits/paint"
        self.report_path = self.folder / ("paint-report-" + self.run_id + ".json")
        self.report = {"passed": False, "project": self.spec["project_name"], "graph": self.spec["graph_id"],
                       "run_id": self.run_id, "candidate_sha256": self.candidate,
                       "cases": {key: {"description": value, "status": "pending"} for key, value in CASES.items()}}

    def candidate_hashes(self):
        result = super().candidate_hashes()
        source = Path(__file__).resolve().parent
        for name in ("supervoxel_paint.py", "supervoxel_paint_service.py", "supervoxel_paint_history.py"):
            expected = hashlib.sha256((source / name).read_bytes()).hexdigest()
            assert hashlib.sha256((self.dest / "scripts" / name).read_bytes()).hexdigest() == expected, name
            result[name] = expected
        result["verify_supervoxel_paint.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        return result

    def paint_probe(self, action, **values):
        request = {"action": action, "project": self.spec["project_name"], **values}
        # Verifiers run on the host and are deliberately not deployed as service
        # modules. Supply the base helper module in-memory with this probe.
        base_source = Path(__file__).with_name("verify_supervoxel_split.py").read_text()
        source = ("import sys, types\nsys.path.insert(0, '/workspace/scripts')\n"
                  "_base = types.ModuleType('verify_supervoxel_split')\n"
                  "_base.__file__ = '/workspace/scripts/verify_supervoxel_split.py'\n"
                  "exec(" + repr(base_source) + ", _base.__dict__)\n"
                  "sys.modules['verify_supervoxel_split'] = _base\n"
                  "__name__ = 'paint_internal'\n" + Path(__file__).read_text())
        source += "\n_internal(" + repr(request) + ")\n"
        path = self.folder / (self.name("paint-" + action) + ".log")
        output = path.open("w")
        process = subprocess.Popen(self.compose + ["exec", "-T", "pcg", "python", "-"],
                                   stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT)
        process.stdin.write(source.encode())
        process.stdin.close()
        return PaintProbe(process, output, path).finish()

    def body(self, name="multi", **kwargs):
        return paint_body(self.geometry[name], self.spec["resolution_nm_xyz"], **kwargs)

    def records(self):
        return {value["request_id"]: value for path in self.paint_dir.rglob("*.json")
                if path.is_file() and not path.name.startswith(".")
                for value in [json.loads(path.read_text())]}

    def one_record(self, before):
        added = set(self.records()) - set(before)
        assert len(added) == 1, ("Expected one new paint record", added)
        return self.records()[added.pop()]

    @contextmanager
    def limits(self, **values):
        self.set_fault("paint_options", action="none", paint_limits=values)
        try:
            yield
        finally:
            self.clear_fault()

    def split_paint(self, body=None, *, expected=200):
        return self.post("/split", self.body() if body is None else body, expected=expected)

    def separated(self, name="multi", *, expected=True):
        geometry = self.geometry[name]
        return self.separated_points(geometry["sources_vox"], geometry["sinks_vox"], expected=expected)

    def separated_points(self, source_points, sink_points, *, expected=True):
        source = self.at_points(source_points)
        sink = self.at_points(sink_points)
        actual = not ({value["root"] for value in source} & {value["root"] for value in sink})
        assert actual is expected
        return {"sources": source, "sinks": sink, "separated": actual}

    def assert_unchanged(self, snapshot, graph=None):
        result = self.probe("compare", name=snapshot["snapshot"], partition=True)
        after = self.snapshot("paint-unchanged")
        assert after["generation"] == snapshot["generation"]
        assert after["atomic_sha256"] == snapshot["atomic_sha256"]
        if graph is not None:
            assert self.paint_probe("graph_state") == graph, "Refusal persisted graph records"
        return result

    def operation(self, operation_id, roots=()):
        return self.paint_probe("operation", operation_id=int(operation_id), roots=list(roots))

    def _fault_request(self, point, *, action="raise", body=None, **values):
        before = self.records()
        self.set_fault("paint_" + point, action=action, **values)
        future = self.executor.submit(self.split_paint, body, expected=None)
        try:
            marker = self.wait_marker()
            try:
                response = future.result(timeout=300)
            except requests.RequestException as error:
                if action != "exit":
                    raise
                response = {"transport_error": str(error)}
        finally:
            self.clear_fault()
        return response, marker, self.one_record(before) if set(self.records()) - set(before) else None

    def _await_recovery(self, record):
        expires = record["start_time"] + record["deadline_seconds"] + record["lock_lifetime_seconds"] + .2
        while time.time() < expires:
            remaining = expires - time.time()
            print(f"paint recovery {record['request_id']}: waiting {remaining:.1f}s for deadline and lease", flush=True)
            time.sleep(min(remaining, 30))
        reports = self.paint_probe("recover_paint", request_id=record["request_id"])
        return self.records()[record["request_id"]], reports

    def _repair(self, operation_id, *, expected_error=None):
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "bin/cave"), "svsplit", "repair",
                                 str(self.dest), "--pcg-repair", str(operation_id)],
                                capture_output=True, text=True)
        path = self.folder / (self.name("paint-native-repair") + ".log")
        path.write_text(result.stdout + result.stderr)
        if expected_error is not None:
            assert result.returncode != 0, "Unsafe native repair unexpectedly succeeded"
            assert expected_error in path.read_text(), path.read_text()[-6000:]
            return {"operation_id": operation_id, "command_log": str(path),
                    "refused": expected_error, "returncode": result.returncode}
        assert result.returncode == 0, path.read_text()[-6000:]
        assert self.operation(operation_id)["status"] == 0
        return {"operation_id": operation_id, "command_log": str(path)}

    def _repair_report(self, record):
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / "bin/cave"), "svsplit", "repair",
                                 str(self.dest)], capture_output=True, text=True)
        path = self.folder / (self.name("paint-repair-report") + ".log")
        path.write_text(result.stdout + result.stderr)
        assert result.returncode == 0, path.read_text()[-6000:]
        assert record["request_id"] in result.stdout
        reports = self.paint_probe("recover_paint", request_id=record["request_id"])
        assert len(reports) == 1 and reports[0]["state"] == record["state"]
        return {"command_log": str(path), "report": reports[0]}

    def case_pa(self):
        geometry = self.geometry["neck"]
        old_atom = self.at_points(geometry["sources_vox"])[0]["atom"]
        before = self.snapshot("paint-neck-before")
        records = self.records()
        value = self.body("neck", radius_nm=min(self.spec["resolution_nm_xyz"]) * 1.2)
        response = self.split_paint(value)
        assert response.headers["X-Cave-Supervoxels-Refined"] == "1"
        assert len(response.headers["X-Cave-Paint-Voxels"].split(",")) == 2
        operation_id = int(response.json()["operation_id"])
        assert int(response.headers["X-Cave-Paint-Record"]) == operation_id
        record = self.one_record(records)
        assert record["operation_id"] == operation_id and record["paint"] == value["paint"]
        assert record["state"] == "completed"
        assert response.headers["X-Cave-Paint-Voxels"] == ",".join(map(str, record["rasterized_counts"]))
        assert all(int(count) > 1 for count in record["rasterized_counts"]), "Neck case must paint voxel masks"
        assert len(record["refinement_operation_ids"]) == 1
        after = self.snapshot("paint-neck-after")
        previous = np.load(self.folder / (before["snapshot"] + ".npz"))
        current = np.load(self.folder / (after["snapshot"] + ".npz"))
        changed = previous["atomic"] != current["atomic"]
        assert changed.any() and not np.any(changed & (previous["atomic"] != old_atom))
        sides = self.separated("neck")
        self.saved["neck"] = (before, response.json())
        return {"operation": response.json(), "record": record, "sides": sides,
                "changed_voxels": int(changed.sum())}

    def case_pb(self):
        before = self.snapshot("paint-multi-before")
        response = self.split_paint()
        assert response.headers["X-Cave-Supervoxels-Refined"] == "0"
        assert self.snapshot("paint-multi-after")["generation"] == before["generation"]
        result = {"operation": response.json(), "sides": self.separated()}
        self.undo(response.json()["operation_id"])
        self.probe("compare", name=before["snapshot"], partition=True)
        points = self.geometry["multi"]["sources_vox"] + self.geometry["multi"]["sinks_vox"]
        geometry = {"sources_vox": points[::2], "sinks_vox": points[1::2]}
        alternating = self.split_paint(paint_body(geometry, self.spec["resolution_nm_xyz"]))
        sides = self.separated_points(geometry["sources_vox"], geometry["sinks_vox"])
        assert len({atom["root"] for atom in sides["sources"]}) == 2
        assert len({atom["root"] for atom in sides["sinks"]}) == 2
        assert alternating.headers["X-Cave-Supervoxels-Refined"] == "0"
        self.undo(alternating.json()["operation_id"])
        self.probe("compare", name=before["snapshot"], partition=True)
        result["disconnected_labels"] = {"operation": alternating.json(), "sides": sides}
        return result

    def _stray(self, name, distance):
        before, graph, records = self.snapshot("paint-stray-before"), self.paint_probe("graph_state"), self.records()
        old_logs = self.paint_probe("logs")
        with self.limits(max_cut_distance_nm=distance):
            response = self.split_paint(self.body(name), expected=400)
        refusal = require_refusal(response, "stray_cut")
        assert refusal["paint_refusal"]["locations_nm"]
        self.assert_unchanged(before, graph)
        record = self.one_record(records)
        assert record["state"] == "not_applied"
        current_logs = self.paint_probe("logs")
        attempts = {key: value for key, value in current_logs.items() if key not in old_logs}
        assert attempts and all(value["status_versions"] == [1] for value in attempts.values()), attempts
        return {"refusal": refusal, "record": record, "attempt_logs": attempts}

    def case_pc(self):
        return self._stray("multi", 1)

    def case_pc2(self):
        return self._stray("distant_contact", self.geometry["distant_contact"]["max_cut_distance_nm"])

    def case_pd(self):
        before, graph = self.snapshot("paint-seam-before"), self.paint_probe("graph_state")
        response = self.split_paint(self.body("seam"), expected=400)
        refusal = require_refusal(response, "seam")
        require_seam_location(refusal, self.geometry["seam"]["face_vox"], self.spec["resolution_nm_xyz"])
        self.assert_unchanged(before, graph)
        geometry = {name: self.geometry["seam"]["repaint_" + name] for name in ("sources_vox", "sinks_vox")}
        response = self.split_paint(paint_body(geometry, self.spec["resolution_nm_xyz"]))
        assert int(response.headers["X-Cave-Supervoxels-Refined"]) == 1
        atoms = self.at_points(geometry["sources_vox"] + geometry["sinks_vox"])
        assert atoms[0]["root"] != atoms[1]["root"]
        return {"refusal": refusal, "repaint": response.json()}

    def case_pd2(self):
        before, graph = self.snapshot("paint-bypass-before"), self.paint_probe("graph_state")
        value = self.body("bypass")
        value["bb_offset"] = [1, 1, 4]
        response = self.split_paint(value, expected=None)
        if response.status_code == 200:
            result = {"operation": response.json(), "sides": self.separated("bypass")}
            self.undo(response.json()["operation_id"])
        else:
            result = {"refusal": require_refusal(response, "not_certified")}
            self.assert_unchanged(before, graph)
        return result

    def case_pc3(self):
        geometry = self.geometry["fake_edge"]
        results = {}
        for where in ("far", "near"):
            points = geometry[where + "_merge_vox"]
            atoms = self.at_points(points)
            nodes = [[str(atom["atom"]), *((np.asarray(point) + .5) * self.spec["resolution_nm_xyz"]).tolist()]
                     for atom, point in zip(atoms, points)]
            merge = self.post("/merge", nodes).json()
            if where == "far":
                results[where] = self._stray("fake_edge", geometry["max_cut_distance_nm"])
                self.undo(merge["operation_id"])
            else:
                # An inactive fake edge can be reactivated without a new cell;
                # use a pristine, equivalent pair to test a near creating merge.
                near_geometry = {"sources_vox": geometry["near_sources_vox"],
                                 "sinks_vox": geometry["near_sinks_vox"]}
                with self.limits(max_cut_distance_nm=geometry["max_cut_distance_nm"]):
                    response = self.split_paint(paint_body(near_geometry, self.spec["resolution_nm_xyz"]))
                results[where] = {"operation": response.json(),
                                 "sides": self.separated_points(near_geometry["sources_vox"],
                                                                near_geometry["sinks_vox"])}
        return results

    def case_pe(self):
        variants = []
        value = self.body(); value["paint"]["sinks"] = deepcopy(value["paint"]["sources"])
        variants.append(("overlap", value, {}, None))
        value = self.body(); value["paint"]["sources"] = [{"points_nm": [[.5, .5, .5]], "radius_nm": .1}]
        variants.append(("empty_label", value, {}, None))
        value = self.body(); value["paint"]["sinks"] = self.body("neck")["paint"]["sources"]
        variants.append(("other_object", value, {}, None))
        value = self.body(); value["sources"] = [[1, 1, 1, 1]]
        variants.append(("limit", value, {}, "cannot be mixed"))
        for limits in ({"max_paint_voxels": 1}, {"max_box_voxels": [1, 1, 1]},
                       {"max_radius_nm": .01}, {"max_total_points": 1}):
            variants.append(("limit", self.body(), limits, next(iter(limits))))
        before, graph = self.snapshot("paint-validation-before"), self.paint_probe("graph_state")
        results = []
        for kind, value, limits, expected_error in variants:
            with self.limits(**limits):
                response = self.split_paint(value, expected=400)
                results.append(require_limit(response, expected_error) if expected_error
                               else require_refusal(response, kind))
            self.assert_unchanged(before, graph)
        return results

    def case_pf(self):
        before = self.snapshot("paint-history-before")
        response = self.split_paint()
        after = self.snapshot("paint-history-after")
        undo = self.undo(response.json()["operation_id"]).json()
        self.probe("compare", name=before["snapshot"], partition=True)
        redo = self.redo(response.json()["operation_id"]).json()
        self.probe("compare", name=after["snapshot"], partition=True)
        self.probe("compare", name=before["snapshot"], history=True)
        self.undo(redo["operation_id"])
        return {"split": response.json(), "undo": undo, "redo": redo}

    def case_ph(self):
        options = {**self.spec["supervoxel_split"]["paint"], "enabled": False}
        with self.temporary_options(paint=options):
            refusal = require_refusal(self.split_paint(expected=400), "disabled")
            source = self.at_points(self.geometry["multi"]["sources_vox"][:1])[0]
            sink = self.at_points(self.geometry["multi"]["sinks_vox"][-1:])[0]
            response = self.split(source, sink)
            self.undo(response.json()["operation_id"])
        return {"refusal": refusal, "point_split": response.json()}

    def _deadline(self, point, *, marked=False, gate=False):
        before, graph = self.snapshot("paint-deadline-before"), self.paint_probe("graph_state")
        response, marker, record = self._fault_request(point, action="pause", duration=1.2,
                                                      paint_limits={"max_seconds": 1})
        refusal = require_refusal(response, "deadline")
        self.assert_unchanged(before, graph)
        recovery = None
        if point == "binding":
            assert record is not None, "Binding deadline produced no paint record"
            assert marker.get("operation_id") is not None, "Binding deadline produced no operation id"
            assert record.get("operation_id") == marker["operation_id"], (record, marker)
            record, recovery = self._await_recovery(record)
        if marker.get("operation_id"):
            assert record is not None
            operation = self.operation(marker["operation_id"], record.get("roots", []))
            assert operation["status_versions"] == [1]
            assert not any(operation["indefinite_locks"].values())
        if marked:
            assert record is not None
            assert record["handed_to_write"] is True and record["state"] == "ambiguous"
            assert "write_calls" in record and "write_entries" in record, record
            assert record["write_calls"] == 0
            assert record["write_entries"] == int(gate)
            assert record["reason"] == ("deadline_at_persistence_gate" if gate else "deadline_after_marker")
            assert response.json()["paint_record_state"]["state"] == "ambiguous"
            report = self._repair_report(record)
            assert report["report"]["partition_equals_pre_cut"] is True
        elif record:
            assert record["state"] == "not_applied" and not record.get("handed_to_write")
        return {"refusal": refusal, "marker": marker, "record": record, "recovery": recovery}

    def case_pj(self):
        return {point: self._deadline(point) for point in ("raster", "graph_read", "solve", "check")}

    def case_pj2(self):
        return self._deadline("binding")

    def case_pj3(self):
        return self._deadline("marker", marked=True)

    def case_pj4(self):
        results = {point: self._deadline(point, marked=True, gate=True)
                   for point in ("indefinite_lock", "lock_renewal")}
        following = self.split_paint()
        self.undo(following.json()["operation_id"])
        results["following_edit"] = following.json()
        return results

    def case_pl(self):
        before, graph = self.snapshot("paint-request-limits-before"), self.paint_probe("graph_state")
        maximum = self.spec["supervoxel_split"]["paint"]["max_request_bytes"]
        response = requests.post(self.base + "/split", data=b'{"paint":{}}' + b" " * maximum,
                                 headers={"Content-Type": "application/json"}, timeout=30)
        results = [require_limit(response, "max_request_bytes")]
        self.assert_unchanged(before, graph)
        repeated = self.body()
        repeated["paint"]["sources"] *= 16
        for option, limit, value in (("max_strokes", 1, self.body()),
                                      ("max_total_points", 1, self.body()),
                                      ("max_raster_work", 33, repeated)):
            self.set_fault("paint_graph_read", action="raise", paint_limits={option: limit})
            try:
                results.append(require_limit(self.split_paint(value, expected=400), option))
                assert not self.marker.exists(), "Bound refusal reached graph read"
            finally:
                self.clear_fault()
            self.assert_unchanged(before, graph)
        # The unrepeated request must succeed at the exact same work bound;
        # otherwise the repeated-stroke test cannot establish repeat counting.
        with self.limits(max_raster_work=33):
            base = self.split_paint()
        self.separated()
        self.undo(base.json()["operation_id"])
        self.probe("compare", name=before["snapshot"], partition=True)
        results.append({"base_request_at_max_raster_work_33": base.json()})
        return results

    def case_pg(self):
        original = self.at_points(self.geometry["multi"]["sources_vox"][:1])[0]
        geometry = {"sources_vox": original["points"][:1], "sinks_vox": original["points"][1:]}
        before = self.snapshot("paint-race-refinement-before")
        bridge_source = self.at_points(self.geometry["fake_edge"]["sources_vox"][:1])[0]
        bridge_sink = self.at_points(self.geometry["fake_edge"]["near_sources_vox"][:1])[0]
        bridge_setup = self.post("/merge", [self.node(bridge_source), self.node(bridge_sink)]).json()
        self.set_fault("paint_after_refinement", action="pause")
        future = self.executor.submit(self.split_paint, paint_body(geometry, self.spec["resolution_nm_xyz"]),
                                      expected=None)
        try:
            marker = self.wait_marker()
            refined = self.snapshot("paint-race-refinement-published")
            assert refined["generation"] > before["generation"], "Race did not publish a painted refinement"
            source = self.at_points(geometry["sources_vox"])[0]
            sink = self.at_points(geometry["sinks_vox"])[0]
            merge = self.paint_probe("merge_bridge",
                atoms=[source["atom"], sink["atom"], bridge_source["atom"], bridge_sink["atom"]],
                points=[geometry["sources_vox"][0], geometry["sinks_vox"][0],
                        bridge_source["points"][0], bridge_sink["points"][0]])
            assert merge["bridge_edges"] == [[source["atom"], bridge_source["atom"]],
                                             [sink["atom"], bridge_sink["atom"]]]
        finally:
            self.clear_fault()
        response = future.result(timeout=300)
        if response.status_code == 200:
            self.separated_points(geometry["sources_vox"], geometry["sinks_vox"])
            self.undo(response.json()["operation_id"])
            outcome = response.json()
        else:
            outcome = require_refusal(response, "object_changed")
        self.undo(merge["operation_id"])
        self.undo(bridge_setup["operation_id"])
        self.probe("compare", name=before["snapshot"], partition=True)
        return {"marker": marker, "refinement_generation": refined["generation"],
                "bridge_setup": bridge_setup, "concurrent_merge": merge, "outcome": outcome}

    def case_pk(self):
        self.set_fault("paint_before_native", action="pause")
        future = self.executor.submit(self.split_paint, None, expected=None)
        try:
            marker = self.wait_marker()
            atom = self.at_points(self.geometry["multi"]["sources_vox"][:1])[0]
            refinement = self.refine(atom, grid=(2, 2, 2))
        finally:
            self.clear_fault()
        response = future.result(timeout=300)
        if response.status_code == 200:
            self.separated()
            # The base probe also checks every logged endpoint against the
            # current retired-id set, including SourceID/SinkID and edge pairs.
            log = self.probe("operation_log", operation_id=response.json()["operation_id"])
            assert "SourceID" in log and "SinkID" in log
            assert atom["atom"] not in log["SourceID"] + log["SinkID"]
            self.undo(response.json()["operation_id"])
            outcome = {"operation": response.json(), "current_endpoint_log": log}
        else:
            outcome = require_refusal(response, "object_changed")
        return {"marker": marker, "refinement": refinement, "outcome": outcome}

    def case_pi(self):
        results = {}
        baseline = self.split_paint()
        uninterrupted = self.snapshot("paint-uninterrupted-cut")
        self.undo(baseline.json()["operation_id"])
        results["uninterrupted"] = {"operation": baseline.json(), "snapshot": uninterrupted}
        # Identical later requests must never acquire the earlier request's id.
        response, marker, first = self._fault_request("before_native", action="exit",
                                                     paint_limits={"max_seconds": 2})
        self.restart()
        later = self.split_paint()
        second = self.records()[next(key for key, item in self.records().items()
                                     if item.get("operation_id") == int(later.json()["operation_id"]))]
        assert second["request_id"] != first["request_id"] and first.get("operation_id") is None
        self.undo(later.json()["operation_id"])
        results["before_native"] = {"first": first, "second": second}
        for point, expected in (("after_binding", "not_applied"),
                                ("after_mutate", "awaiting_pcg_repair"),
                                ("after_native", "completed")):
            before = self.snapshot("paint-crash-before")
            graph = self.paint_probe("graph_state") if point == "after_binding" else None
            response, marker, record = self._fault_request(point, action="exit",
                                                          paint_limits={"max_seconds": 2})
            self.restart()
            if expected != "completed":
                record, recovery = self._await_recovery(record)
            else:
                recovery = self.paint_probe("recover_paint", request_id=record["request_id"])
                record = self.records()[record["request_id"]]
            assert record["state"] == expected, record
            if expected == "not_applied":
                assert self.operation(record["operation_id"])["status_versions"] == [1]
                self.assert_unchanged(before, graph)
            elif expected == "awaiting_pcg_repair":
                operation = self.operation(record["operation_id"], record["roots"])
                assert operation["status"] == 3 and any(operation["indefinite_locks"].values())
                self._repair(record["operation_id"])
                record = self.records()[record["request_id"]]
                assert record["state"] == "completed", record
                self.separated()
                self.probe("compare", name=uninterrupted["snapshot"], partition=True)
                self.undo(record["operation_id"])
                self.probe("compare", name=before["snapshot"], partition=True)
            else:
                self.separated()
                self.undo(record["operation_id"])
            results[point] = {"marker": marker, "record": record, "recovery": recovery}
        first, _ = self._await_recovery(first)
        assert first["state"] == "not_applied" and first.get("operation_id") is None
        before = self.snapshot("paint-binding-failure-before")
        response, marker, record = self._fault_request("binding", action="raise")
        assert response.status_code == 400 and record["state"] == "not_applied"
        assert self.operation(marker["operation_id"])["status_versions"] == [1]
        self.probe("compare", name=before["snapshot"], partition=True)
        results["binding_failure"] = {"marker": marker, "record": record}
        return results

    def case_pi2(self):
        results = {}
        for point, expected in (("before_mutate", "ambiguous"), ("after_mutate", "awaiting_pcg_repair"),
                                ("before_success_mutate", "awaiting_pcg_repair")):
            before = self.snapshot("paint-write-failure-before")
            response, marker, record = self._fault_request(point, action="raise",
                                                          paint_limits={"max_seconds": 2})
            assert response.status_code >= 400
            assert record["state"] == expected and record["handed_to_write"] is True
            assert (self.paint_dir / "pending" / (record["request_id"] + ".json")).exists()
            assert response.json()["paint_record_state"]["state"] == expected
            repair_report = self._repair_report(record)
            guarded_repair, restore = None, None
            if expected == "awaiting_pcg_repair":
                # A caught native exception releases its indefinite locks.
                # Replaying that WRITE_STARTED log is therefore unsafe even
                # after its lease expires. Restore this fully written fixture
                # cut with a new ordinary locked merge instead.
                assert not any(self.operation(record["operation_id"], record["roots"])["indefinite_locks"].values())
                record, _ = self._await_recovery(record)
                graph = self.paint_probe("graph_state")
                guarded_repair = self._repair(record["operation_id"], expected_error="indefinite lock")
                assert self.paint_probe("graph_state") == graph
                self.separated()
                source = self.at_points(self.geometry["multi"]["sources_vox"][:1])[0]
                sink = self.at_points(self.geometry["multi"]["sinks_vox"][-1:])[0]
                restore = self.post("/merge", [self.node(source), self.node(sink)]).json()
            self.probe("compare", name=before["snapshot"], partition=True)
            results[point] = {"marker": marker, "record": record,
                              "response_status": response.status_code, "response_body": response.text[:3000],
                              "repair_report": repair_report, "guarded_repair": guarded_repair,
                              "restoring_native_merge": restore}
        return results

    def case_pi3(self):
        before = self.snapshot("paint-partial-baseline")
        baseline = self.split_paint()
        expected = self.snapshot("paint-partial-uninterrupted-cut")
        self.undo(baseline.json()["operation_id"])
        self.probe("compare", name=before["snapshot"], partition=True)
        response, marker, record = self._fault_request("partial_mutate", action="exit",
                                                      paint_limits={"max_seconds": 2})
        self.restart()
        record, recovery = self._await_recovery(record)
        assert record["state"] == "ambiguous" and record["handed_to_write"] is True
        operation = self.operation(record["operation_id"], record["roots"])
        assert operation["status_versions"] == [1]
        if not all(operation["latest_roots"]):
            graph = self.paint_probe("graph_state")
            repair = self._repair(record["operation_id"], expected_error="superseded recorded roots")
            assert self.paint_probe("graph_state") == graph
            retained = self.records()[record["request_id"]]
            assert retained["state"] == "ambiguous"
            return {"marker": marker, "record_before_explicit_repair": record, "recovery": recovery,
                    "guarded_repair": repair, "retained": retained, "uninterrupted": expected,
                    "reason": "Partial write superseded a recorded root; stale replay refused and fixture retained."}
        repair = self._repair(record["operation_id"])
        completed = self.records()[record["request_id"]]
        assert completed["state"] == "completed"
        assert not any(self.operation(record["operation_id"], record["roots"])["indefinite_locks"].values())
        self.probe("compare", name=expected["snapshot"], partition=True)
        self.undo(record["operation_id"])
        self.probe("compare", name=before["snapshot"], partition=True)
        return {"marker": marker, "record_before_explicit_repair": record, "recovery": recovery,
                "explicit_repair": repair, "completed": completed, "uninterrupted": expected}

    def run(self, cases=None):
        selected = list(CASES) if cases is None else list(cases)
        unknown = set(selected) - set(CASES)
        if unknown:
            raise ValueError("Unknown paint cases: " + ", ".join(sorted(unknown)))
        self.write_report()
        try:
            for case in selected:
                print(f"paint case {case}: {CASES[case]}", flush=True)
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
            assert self.candidate_hashes() == self.candidate, "candidate changed during paint acceptance"
            self.report["passed"] = all(value["status"] == "passed" for value in self.report["cases"].values())
            self.report["selected_cases_passed"] = True
        finally:
            self.clear_fault()
            self.write_report()
            self.executor.shutdown(wait=False, cancel_futures=True)
            print("Paint evidence: " + str(self.report_path), flush=True)
        return self.report


def verify(destination, cases=None):
    return PaintVerifier(destination).run(cases)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--cases", help="Comma-separated diagnostic subset; omitted cases stay pending")
    args = parser.parse_args()
    print(json.dumps(verify(args.destination, args.cases.split(",") if args.cases else None), indent=2))
