"""Restart a disposable stack with a real edit still applied, then Undo it.

The complete graph, native operation log, live annotations and all snapshots
must survive restart exactly. Failure evidence and any outstanding operation
are saved before raising. This never runs on a non-fixture deployment.
"""
import argparse
import json
import time
from pathlib import Path

import numpy as np
import requests

from snapshot import api_roots, compose, snapshot
from verify_segmentation_iteration import require_disposable


def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def partition(atoms, roots):
    return sorted([sorted(str(int(atom)) for atom in atoms[roots == root]) for root in np.unique(roots)])


def wait_ready(spec, deadline_seconds=240):
    deadline = time.monotonic() + deadline_seconds
    routes = ["/segmentation/table/" + spec["graph_id"] + "/info", "/info/health",
              "/schema/versions", "/annotation/api/versions", "/materialize/api/versions"]
    while True:
        try:
            for route in routes:
                requests.get(spec["local_url"].rstrip("/") + route, timeout=10).raise_for_status()
            return
        except requests.RequestException:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def verify(dest, holding_edit=True):
    spec = json.loads((dest / "deployment.json").read_text())
    require_disposable(dest, spec)
    base = spec["local_url"].rstrip("/") + "/segmentation/api/v1/table/" + spec["graph_id"]
    report = {"graph_id": spec["graph_id"], "holding_edit": holding_edit, "passed": False}
    operation = None
    undone = False
    phase = "readiness"

    def post(path, payload):
        response = requests.post(base + path + "?int64_as_str=true", json=payload, timeout=180)
        if not response.ok:
            raise RuntimeError(path + ": " + response.text[:2000])
        return response.json()

    try:
        wait_ready(spec)
        phase = "original_roots"
        atoms = np.load(dest / "data/atom_index.npy")[:, 0]
        original_roots = api_roots(spec, atoms)
        phase = "original_snapshot"
        original = snapshot(dest)
        save(dest / "data/persistence-original.json", original)
        if holding_edit:
            phase = "holding_edit"
            scenario = json.loads((dest / "data/scenario.json").read_text())
            nodes = [[atom, *(np.asarray(point) * spec["resolution_nm_xyz"]).tolist()]
                     for atom, point in zip(scenario["atoms"], scenario["positions_xyz"])]
            separate = scenario.get("initial_relation") == "separate"
            report["edit_kind"] = "merge" if separate else "split"
            operation = post("/merge", nodes) if separate else post("/split", {"sources": [nodes[0]], "sinks": [nodes[1]]})
            report["holding_operation"] = operation
            current = api_roots(spec, np.asarray(scenario["atoms"], np.uint64))
            if (current[0] == current[1]) != separate:
                raise RuntimeError("Temporary persistence edit did not change the selected component relation")
        phase = "snapshot_before_restart"
        before = snapshot(dest)
        save(dest / "data/persistence-before.json", before)
        report["before_edit_count"] = before["edit_count"]
        # stop/start all services, retaining their persistent volumes. This does
        # not ingest again or replace any database.
        phase = "full_stack_restart"
        compose(dest, "stop")
        compose(dest, "up", "-d")
        wait_ready(spec)
        phase = "snapshot_after_restart"
        after = snapshot(dest)
        save(dest / "data/persistence-after.json", after)
        if after != before:
            report["changed_fields"] = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
            raise RuntimeError("Graph, logs, annotations or materializations changed across full restart")
        report["exact_full_snapshot_equal"] = True
        if operation:
            phase = "undo_holding_edit"
            report["undo_holding_operation"] = post("/undo", {"operation_id": operation["operation_id"]})
            undone = True
            if partition(atoms, api_roots(spec, atoms)) != partition(atoms, original_roots):
                raise RuntimeError("Undo failed to restore all original atom memberships")
            phase = "final_snapshot"
            final = snapshot(dest)
            save(dest / "data/persistence-final.json", final)
            scenario["current_roots"] = [str(root) for root in api_roots(spec, np.asarray(scenario["atoms"], np.uint64))]
            save(dest / "data/scenario.json", scenario)
            report["original_membership_restored"] = True
            report["final_edit_count"] = final["edit_count"]
        report.update(passed=True, full_stack_restart_verified=True, atom_count=len(atoms))
    except Exception as error:
        report["failure"] = str(error)
        report["failure_phase"] = phase
        if isinstance(error, requests.HTTPError) and error.response is not None:
            report["failed_http_status"] = error.response.status_code
            report["failed_http_url"] = error.response.url
        if operation and not undone:
            try:
                wait_ready(spec, deadline_seconds=30)
                report["failure_cleanup_undo"] = post("/undo", {"operation_id": operation["operation_id"]})
                report["failure_cleanup_membership_restored"] = partition(atoms, api_roots(spec, atoms)) == partition(atoms, original_roots)
            except Exception as cleanup_error:
                report["outstanding_operation_id"] = operation["operation_id"]
                report["failure_cleanup_error"] = str(cleanup_error)
        save(dest / "data/acceptance-persistence.json", report)
        raise
    save(dest / "data/acceptance-persistence.json", report)
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--holding-edit", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    verify(args.destination.resolve(), args.holding_edit)
