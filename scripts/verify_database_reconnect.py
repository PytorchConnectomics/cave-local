"""Prove fixture APIs recover killed idle database connections without retries.

This terminates connections only inside a disposable fixture's own PostgreSQL
container. It never edits graph membership or annotation rows. The legacy
control mode expects the known first-request error and restarts only affected
API/worker services afterward to permit a clean final snapshot comparison.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

import requests

from snapshot import compose, snapshot, stable_json
from verify_iteration_persistence import wait_ready
from verify_segmentation_iteration import require_disposable


def save(dest, report):
    (dest / "data/acceptance-database-reconnect.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")


def terminate_idle(dest, database_name, prefer_held=False):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", database_name):
        raise ValueError("Unsafe fixture database identifier")
    predicate = "state IN ('idle','idle in transaction')"
    if prefer_held:
        # A dead cached checkout can be masked if the separate Engine probe
        # also hits a dead idle connection and resets that Session. Kill held
        # transactions first to exercise the actual cross-request leak. Fixed
        # services release them and instead exercise their idle pooled entries.
        predicate = "(state='idle in transaction' OR (state='idle' AND NOT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname='" + database_name + "' AND state='idle in transaction')))"
    sql = "SELECT coalesce(json_agg(t),'[]'::json) FROM (SELECT pid,state,pg_terminate_backend(pid,5000) AS terminated FROM pg_stat_activity WHERE datname='" + database_name + "' AND pid<>pg_backend_pid() AND client_addr IS NOT NULL AND " + predicate + ") AS t;"
    raw = compose(dest, "exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1",
                  "-U", "cave", "-d", "postgres", "-A", "-t", "-c", sql)
    records = json.loads(raw)
    if not records or not all(record["terminated"] for record in records):
        raise RuntimeError("Connection test did not terminate the expected warmed fixture sessions")
    return records


def read_request(job):
    name, method, url, body = job
    response = requests.request(method, url, json=body, timeout=30)
    result = {"request": name, "status": response.status_code}
    if response.ok:
        payload = response.json()
        result["body_sha256"] = hashlib.sha256(stable_json(payload).encode()).hexdigest()
    else:
        result["error"] = response.text[:3000]
    return result


def reads(jobs):
    with ThreadPoolExecutor(max_workers=4) as executor:
        return list(executor.map(read_request, jobs))


def assert_reads(results, baseline):
    for result in results:
        if result["status"] != 200:
            raise RuntimeError(f"Independent {result['request']} read returned {result['status']}; no retry was attempted")
        if result["body_sha256"] != baseline[result["request"]]:
            raise RuntimeError(f"Independent {result['request']} read returned different data")


def verify(dest, expect_legacy_failure=False):
    spec = json.loads((dest / "deployment.json").read_text())
    require_disposable(dest, spec)
    report = {"graph_id": spec["graph_id"], "expected_legacy_failure": expect_legacy_failure,
              "request_retries": 0, "graph_and_annotation_writes": 0, "passed": False}
    disconnected = False
    phase = "readiness"
    try:
        wait_ready(spec)
        phase = "baseline_snapshot"
        before = snapshot(dest)
        origin = spec["local_url"].rstrip("/")
        anno = ("annotation_tables", "GET", origin + "/annotation/api/v2/aligned_volume/" + spec["aligned_volume"] + "/table", None)
        versions = ("materialization_versions", "GET", origin + "/materialize/api/v2/datastack/" + spec["datastack"] + "/versions", None)
        info = ("datastack_info", "GET", origin + "/info/api/v2/datastack/full/" + spec["datastack"], None)
        jobs = [anno, versions, info]
        query = None
        latest = max(before["materialization_versions"]) if before["materialization_versions"] else None
        if latest is not None and before["materialized_tables"].get(str(latest)):
            table = sorted(before["materialized_tables"][str(latest)])[0]
            query = ("materialized_table_query", "POST", origin + "/materialize/api/v3/datastack/" + spec["datastack"] + "/version/" + str(latest) + "/table/" + table + "/query?return_pyarrow=false&arrow_format=false", {"limit": 10000})
            jobs.append(query)
        else:
            raise ValueError("Reconnect fixture needs an existing materialized annotation table")
        phase = "warm_connections"
        warm = reads(jobs * 4)
        report["warm_requests"] = warm
        baseline = {result["request"]: result.get("body_sha256") for result in warm}
        assert_reads(warm, baseline)
        phase = "disconnect_aligned_volume"
        report["aligned_volume_terminated"] = terminate_idle(dest, spec["aligned_volume"], prefer_held=True)
        report["initial_disconnect_selection"] = "Held idle transactions if present, otherwise idle pool entries"
        disconnected = True
        phase = "first_annotation_read_after_disconnect"
        first = read_request(anno)
        report["first_annotation_read"] = first
        report["independent_reads_after_aligned_disconnect"] = reads(jobs * 4)
        results = [first] + report["independent_reads_after_aligned_disconnect"]
        if expect_legacy_failure:
            failures = [result for result in results if result["status"] == 500 and "OperationalError" in result.get("error", "") and "server closed the connection" in result.get("error", "")]
            if not failures:
                raise RuntimeError("Legacy control did not reproduce the known stale-connection API500")
            report["legacy_failure_reproduced"] = True
            report["legacy_failure_count"] = len(failures)
            phase = "legacy_control_cleanup"
            compose(dest, "restart", "annotation", "materialize", "mat-worker")
            wait_ready(spec)
            report["legacy_cleanup"] = "Restarted annotation/materialize/mat-worker; no request retries"
            disconnected = False
        else:
            assert_reads(results, baseline)
            phase = "disconnect_all_aligned_idle_connections"
            report["all_aligned_volume_idle_terminated"] = terminate_idle(dest, spec["aligned_volume"])
            first_all = read_request(anno)
            report["first_annotation_read_after_all_idle_disconnect"] = first_all
            remaining = reads(jobs * 4)
            report["independent_reads_after_all_aligned_idle_disconnect"] = remaining
            assert_reads([first_all] + remaining, baseline)
            phase = "disconnect_materialized_version"
            database_name = spec["datastack"] + "__mat" + str(latest)
            report["materialized_database"] = database_name
            report["materialized_database_terminated"] = terminate_idle(dest, database_name)
            phase = "first_materialized_query_after_disconnect"
            first_query = read_request(query)
            report["first_materialized_query"] = first_query
            remaining = reads(jobs * 4)
            report["independent_reads_after_materialized_disconnect"] = remaining
            assert_reads([first_query] + remaining, baseline)
            phase = "restart_postgres_only"
            compose(dest, "restart", "postgres")
            deadline = time.monotonic() + 90
            while True:
                try:
                    compose(dest, "exec", "-T", "postgres", "pg_isready", "-U", "cave", "-d", "postgres")
                    break
                except subprocess.CalledProcessError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(1)
            # Only API version/health routes are polled here; the real DB-backed
            # reads below must work on their first request, without warming.
            wait_ready(spec)
            phase = "first_reads_after_postgres_restart"
            first_reads = [read_request(job) for job in (info, anno, query)]
            report["first_reads_after_postgres_restart"] = first_reads
            assert_reads(first_reads, baseline)
            remaining = reads(jobs * 4)
            report["independent_reads_after_postgres_restart"] = remaining
            assert_reads(remaining, baseline)
            report["postgres_only_restart_verified"] = True
        phase = "final_snapshot"
        after = snapshot(dest)
        if after != before:
            report["changed_snapshot_fields"] = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
            raise RuntimeError("Reconnect test changed graph, logs, annotations or materialization data")
        report.update(passed=True, exact_snapshot_unchanged=True, atom_count=before["atom_count"],
                      annotation_table_count=len(before["annotation_tables"]),
                      materialization_versions=before["materialization_versions"])
    except Exception as error:
        report.update(failure_phase=phase, failure=str(error))
        if expect_legacy_failure and disconnected:
            try:
                compose(dest, "restart", "annotation", "materialize", "mat-worker")
                wait_ready(spec)
                report["failure_cleanup"] = "Restarted only the legacy fixture's API/worker services"
            except Exception as cleanup_error:
                report["failure_cleanup_error"] = str(cleanup_error)
        save(dest, report)
        raise
    save(dest, report)
    print(json.dumps({key: report[key] for key in ("graph_id", "passed", "expected_legacy_failure", "request_retries", "exact_snapshot_unchanged")}, indent=2))
    print("First annotation request:", report["first_annotation_read"]["status"])
    if report.get("first_materialized_query"):
        print("First materialized table query:", report["first_materialized_query"]["status"])
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", type=Path)
    parser.add_argument("--expect-legacy-failure", action="store_true")
    args = parser.parse_args()
    verify(args.deployment.resolve(), args.expect_legacy_failure)
