"""Verifier reporting and API construction; these do not claim live acceptance."""

import json
from contextlib import contextmanager
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verify_supervoxel_paint import (CASES, PaintProbe, PaintVerifier, paint_body,
                                    require_limit, require_refusal, require_seam_location)


def test_paint_body_converts_every_voxel_to_physical_center():
    geometry = {"sources_vox": [[0, 1, 2], [9, 8, 7]], "sinks_vox": [[3, 4, 5]]}
    value = paint_body(geometry, [10, 10, 12.5])
    assert set(value) == {"paint"}
    assert value["paint"]["version"] == 1
    assert value["paint"]["sources"] == [
        {"points_nm": [[5, 15, 31.25]], "radius_nm": 4},
        {"points_nm": [[95, 85, 93.75]], "radius_nm": 4}]
    assert value["paint"]["sinks"] == [{"points_nm": [[35, 45, 68.75]], "radius_nm": 4}]
    assert paint_body(geometry, [10, 10, 12.5], radius_nm=1)["paint"]["sources"][0]["radius_nm"] == 1


def response(kind="stray_cut", status=400, locations=None, error="refused"):
    value = {"error": error, "paint_refusal": {"kind": kind,
             "locations_nm": locations or [], "supervoxels": [2**64 - 1]}}
    return SimpleNamespace(status_code=status, text=json.dumps(value), json=lambda: value)


def test_refusal_asserts_status_kind_and_bounded_diagnostics():
    assert require_refusal(response(), "stray_cut")["paint_refusal"]["supervoxels"] == [2**64 - 1]
    for bad in (response(status=500), response(kind="limit"), response(locations=[[0, 0, 0]] * 21)):
        with pytest.raises(AssertionError):
            require_refusal(bad, "stray_cut")


def test_limit_refusal_requires_the_specific_exercised_limit():
    assert require_limit(response("limit", error="Paint exceeds max_raster_work"), "max_raster_work")
    with pytest.raises(AssertionError):
        require_limit(response("limit", error="Paint exceeds max_strokes"), "max_raster_work")


def test_seam_location_requires_physical_proximity_to_expected_face():
    face, resolution = [16, 5, 35], [8, 8, 24]
    require_seam_location(response("seam", locations=[[124, 44, 852]]).json(), face, resolution)
    for locations in ([], [[124, 20, 828]], [[800, 44, 852]]):
        with pytest.raises(AssertionError):
            require_seam_location(response("seam", locations=locations).json(), face, resolution)


def deadline_harness(record, marker=None):
    verifier = object.__new__(PaintVerifier)
    verifier.snapshot = lambda name: {"snapshot": name}
    verifier.paint_probe = lambda action: {"graph_sha256": "unchanged"}
    verifier.assert_unchanged = lambda before, graph: None
    refused = response("deadline")
    refused.json()["paint_record_state"] = {"state": "ambiguous"}
    verifier._fault_request = lambda *args, **kwargs: (refused, marker or {}, record)
    verifier.operation = lambda *args: {"status_versions": [1], "indefinite_locks": {}}
    verifier._repair_report = lambda record: {"report": {"partition_equals_pre_cut": True}}
    return verifier


@pytest.mark.parametrize("missing", ["write_calls", "write_entries"])
def test_marked_deadline_requires_real_counter_evidence(missing):
    record = {"handed_to_write": True, "state": "ambiguous", "write_calls": 0,
              "write_entries": 0, "reason": "deadline_after_marker"}
    del record[missing]
    with pytest.raises(AssertionError):
        deadline_harness(record)._deadline("marker", marked=True)


@pytest.mark.parametrize("gate,entries,calls", [(False, 1, 0), (True, 0, 0), (True, 1, 1)])
def test_marked_deadline_rejects_wrong_actual_counters(gate, entries, calls):
    record = {"handed_to_write": True, "state": "ambiguous", "write_calls": calls,
              "write_entries": entries, "reason": "deadline_at_persistence_gate" if gate else "deadline_after_marker"}
    with pytest.raises(AssertionError):
        deadline_harness(record)._deadline("marker", marked=True, gate=gate)


@pytest.mark.parametrize("record,marker", [(None, {"operation_id": 42}), ({"operation_id": 42}, {}),
                                         ({"operation_id": 41}, {"operation_id": 42})])
def test_binding_deadline_cannot_pass_without_bound_record(record, marker):
    with pytest.raises(AssertionError):
        deadline_harness(record, marker)._deadline("binding")


def test_binding_deadline_checks_state_after_recovery():
    pending = {"operation_id": 42, "state": "pending"}
    recovered = {"operation_id": 42, "state": "not_applied"}
    verifier = deadline_harness(pending, {"operation_id": 42})
    calls = []
    verifier._await_recovery = lambda record: (calls.append(record) or recovered, ["recovered"])
    result = verifier._deadline("binding")
    assert calls == [pending]
    assert result["record"] == recovered and result["recovery"] == ["recovered"]


@pytest.mark.parametrize("case,kind", [("case_pd", "seam"), ("case_pd2", "not_certified")])
def test_refusal_cases_compare_graph_cells_as_well_as_partition(case, kind):
    verifier = object.__new__(PaintVerifier)
    verifier.snapshot = lambda name: {"snapshot": name}
    verifier.paint_probe = lambda action: {"graph_sha256": "before"}
    verifier.body = lambda *args: {}
    verifier.split_paint = lambda *args, **kwargs: response(kind, locations=[[124, 44, 852]])
    verifier.geometry = {"seam": {"face_vox": [16, 5, 35]}}
    verifier.spec = {"resolution_nm_xyz": [8, 8, 24]}
    def graph_assertion(snapshot, graph):
        assert graph == {"graph_sha256": "before"}
        raise RuntimeError("observed graph comparison")
    verifier.assert_unchanged = graph_assertion
    with pytest.raises(RuntimeError, match="observed graph comparison"):
        getattr(verifier, case)()


def test_work_limit_proves_unrepeated_request_succeeds_at_same_bound(monkeypatch):
    import verify_supervoxel_paint as module
    verifier = object.__new__(PaintVerifier)
    verifier.snapshot = lambda name: {"snapshot": name}
    verifier.paint_probe = lambda action: {"graph_sha256": "before"}
    verifier.assert_unchanged = lambda *args: None
    verifier.spec = {"supervoxel_split": {"paint": {"max_request_bytes": 100}}}
    verifier.base = "http://fixture"
    verifier.marker = SimpleNamespace(exists=lambda: False)
    verifier.body = lambda: {"paint": {"sources": [{"radius_nm": 1}], "sinks": [{"radius_nm": 1}]}}
    fault = {}
    verifier.set_fault = lambda *args, **kwargs: fault.update(kwargs["paint_limits"])
    verifier.clear_fault = fault.clear
    actual_limits = []
    @contextmanager
    def limits(**values):
        actual_limits.append(values)
        yield
    verifier.limits = limits
    successful = []
    def split_paint(value=None, **kwargs):
        if value is not None:
            if "max_raster_work" in fault:
                assert len(value["paint"]["sources"]) == 16
            return response("limit", error="exceeds " + next(iter(fault)))
        assert actual_limits[-1] == {"max_raster_work": 33}
        successful.append("base")
        return SimpleNamespace(json=lambda: {"operation_id": 42})
    verifier.split_paint = split_paint
    verifier.separated = lambda: successful.append("separated")
    verifier.undo = lambda operation_id: successful.append(("undo", operation_id))
    verifier.probe = lambda *args, **kwargs: successful.append("partition_restored")
    def oversized_post(*args, **kwargs):
        assert b'"paint"' in kwargs["data"] and len(kwargs["data"]) > 100
        return response("limit", error="exceeds max_request_bytes")
    monkeypatch.setattr(module.requests, "post", oversized_post)
    result = verifier.case_pl()
    assert successful == ["base", "separated", ("undo", 42), "partition_restored"]
    assert result[-1]["base_request_at_max_raster_work_33"]["operation_id"] == 42


@pytest.mark.parametrize("stale_success", [False, True])
def test_refinement_race_bridge_links_both_sides_and_checks_success_partition(stale_success):
    verifier = object.__new__(PaintVerifier)
    verifier.geometry = {"multi": {"sources_vox": [[1, 1, 1]]},
                         "fake_edge": {"sources_vox": [[3, 1, 1]], "near_sources_vox": [[4, 1, 1]]}}
    verifier.spec = {"resolution_nm_xyz": [1, 1, 1]}
    atoms = {index: {"atom": index, "root": 10, "points": [[index, 1, 1]]} for index in range(1, 5)}
    atoms[1]["points"].append([2, 1, 1])
    verifier.at_points = lambda points: [atoms[point[0]] for point in points]
    verifier.snapshot = lambda name: {"snapshot": name, "generation": int(name.endswith("published"))}
    verifier.post = lambda *args: SimpleNamespace(json=lambda: {"operation_id": 80})
    verifier.set_fault = lambda *args, **kwargs: None
    verifier.clear_fault = lambda: None
    verifier.wait_marker = lambda: {"point": "after_refinement"}
    verifier.split_paint = lambda *args, **kwargs: None
    verifier.executor = SimpleNamespace(submit=lambda *args, **kwargs: SimpleNamespace(
        result=lambda **kwargs: SimpleNamespace(status_code=200, json=lambda: {"operation_id": 90})))
    def bridge(action, **kwargs):
        assert action == "merge_bridge" and kwargs["atoms"] == [1, 2, 3, 4]
        return {"operation_id": 81, "bridge_edges": [[1, 3], [2, 4]]}
    verifier.paint_probe = bridge
    def separated(source, sink):
        assert source == [[1, 1, 1]] and sink == [[2, 1, 1]]
        assert not stale_success, "stale cut leaves bridge path"
    verifier.separated_points = separated
    undone = []
    verifier.undo = undone.append
    verifier.probe = lambda *args, **kwargs: None
    if stale_success:
        with pytest.raises(AssertionError, match="stale cut leaves bridge"):
            verifier.case_pg()
    else:
        result = verifier.case_pg()
        assert result["concurrent_merge"]["bridge_edges"] == [[1, 3], [2, 4]]
        assert undone == [90, 81, 80]


def harness(tmp_path):
    verifier = object.__new__(PaintVerifier)
    verifier.report_path = tmp_path / "report.json"
    verifier.report = {"passed": False, "cases": {key: {"status": "pending"} for key in CASES}}
    verifier.candidate = {"candidate": "abc"}
    verifier.candidate_hashes = lambda: dict(verifier.candidate)
    verifier.clear_fault = lambda: None
    verifier.executor = SimpleNamespace(shutdown=lambda **kwargs: None)
    verifier.write_report = lambda: verifier.report_path.write_text(json.dumps(verifier.report))
    return verifier


def test_required_case_catalog_has_all_additional_faults():
    assert set(CASES) == {"pa", "pb", "pc", "pd", "pd2", "pc2", "pc3", "pe", "pf", "pg", "ph",
                          "pi", "pi2", "pi3", "pj", "pj2", "pj3", "pj4", "pk", "pl"}
    assert list(CASES)[-1] == "pi3", "partial-write ambiguity must not poison later cases"
    for name in CASES:
        assert callable(getattr(PaintVerifier, "case_" + name))


def test_subset_never_reports_full_acceptance(tmp_path):
    verifier = harness(tmp_path)
    verifier.case_pa = lambda: {"actual": True}
    result = verifier.run(["pa"])
    assert result["selected_cases_passed"] is True
    assert result["passed"] is False
    assert result["cases"]["pa"]["status"] == "passed"
    assert all(value["status"] == "pending" for key, value in result["cases"].items() if key != "pa")
    assert json.loads(verifier.report_path.read_text()) == result


def test_failed_case_is_durable_and_subsequent_case_pending(tmp_path):
    verifier = harness(tmp_path)
    calls = []
    verifier.case_pa = lambda: calls.append("pa")
    def failed():
        raise RuntimeError("real probe failed")
    verifier.case_pb = failed
    verifier.case_pc = lambda: calls.append("pc")
    with pytest.raises(RuntimeError, match="real probe failed"):
        verifier.run(["pa", "pb", "pc"])
    result = json.loads(verifier.report_path.read_text())
    assert result["passed"] is False and calls == ["pa"]
    assert result["cases"]["pb"]["status"] == "failed"
    assert "real probe failed" in result["cases"]["pb"]["traceback"]
    assert result["cases"]["pc"]["status"] == "pending"


def test_complete_run_requires_every_case_and_unchanged_candidate(tmp_path):
    verifier = harness(tmp_path)
    calls = []
    for key in CASES:
        setattr(verifier, "case_" + key, lambda key=key: calls.append(key))
    assert verifier.run()["passed"] is True
    assert calls == list(CASES)
    verifier = harness(tmp_path)
    verifier.case_pa = lambda: None
    verifier.candidate_hashes = lambda: {"candidate": "changed"}
    with pytest.raises(AssertionError, match="candidate changed"):
        verifier.run(["pa"])
    assert json.loads(verifier.report_path.read_text())["passed"] is False


def test_unknown_case_fails_without_writing_report(tmp_path):
    verifier = harness(tmp_path)
    with pytest.raises(ValueError, match="Unknown paint cases"):
        verifier.run(["missing"])
    assert not verifier.report_path.exists()


@pytest.mark.parametrize("code,text,error", [(86, "worker exited", "exit 86"),
                                            (0, "no structured result", "no result")])
def test_probe_does_not_treat_exit_or_missing_evidence_as_pass(tmp_path, code, text, error):
    path = tmp_path / "probe.log"
    path.write_text(text)
    probe = PaintProbe(SimpleNamespace(wait=lambda **kwargs: code), SimpleNamespace(close=lambda: None), path)
    with pytest.raises((AssertionError, RuntimeError), match=error):
        probe.finish()


def test_probe_reads_only_its_result_marker(tmp_path):
    path = tmp_path / "probe.log"
    path.write_text('warning\nPAINT_VERIFY_RESULT={"status": 1}\n')
    probe = PaintProbe(SimpleNamespace(wait=lambda **kwargs: 0), SimpleNamespace(close=lambda: None), path)
    assert probe.finish() == {"status": 1}


def test_new_record_must_be_unambiguously_owned():
    verifier = object.__new__(PaintVerifier)
    verifier.records = lambda: {"old": {"request_id": "old"}, "new": {"request_id": "new"}}
    assert verifier.one_record({"old"}) == {"request_id": "new"}
    with pytest.raises(AssertionError, match="one new paint record"):
        verifier.one_record(set())


def test_separation_checks_every_source_and_sink_root():
    verifier = object.__new__(PaintVerifier)
    verifier.geometry = {"multi": {"sources_vox": [[1], [2]], "sinks_vox": [[3], [4]]}}
    verifier.at_points = lambda points: [{"root": {1: 10, 2: 20, 3: 30, 4: 40}[point[0]]} for point in points]
    assert verifier.separated()["separated"] is True
    verifier.at_points = lambda points: [{"root": {1: 10, 2: 20, 3: 30, 4: 20}[point[0]]} for point in points]
    with pytest.raises(AssertionError):
        verifier.separated()
    assert verifier.separated(expected=False)["separated"] is False


def test_history_case_redoes_original_split_and_undoes_new_redo():
    verifier = object.__new__(PaintVerifier)
    verifier.snapshot = lambda name: {"snapshot": name}
    verifier.split_paint = lambda: SimpleNamespace(json=lambda: {"operation_id": 100})
    calls = []
    def undo(operation_id):
        calls.append(("undo", operation_id))
        return SimpleNamespace(json=lambda: {"operation_id": 200})
    def redo(operation_id):
        calls.append(("redo", operation_id))
        assert operation_id == 100, "Native redo accepts the original operation id"
        return SimpleNamespace(json=lambda: {"operation_id": 300})
    verifier.undo, verifier.redo = undo, redo
    verifier.probe = lambda *args, **kwargs: None
    result = verifier.case_pf()
    assert calls == [("undo", 100), ("redo", 100), ("undo", 300)]
    assert result["redo"]["operation_id"] == 300
