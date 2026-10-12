"""Run actual annotation/materialization creators concurrently on a fixture.

Independent processes call both real creator entry points. A PostgreSQL table
lock deliberately pauses insertion so concurrent checks overlap; the product
adapter must serialize them and commit exactly one compatible metadata entry.
The host then posts an annotation and materializes it immediately.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def creator(metadata, role, barrier, results, label, adapter=None):
    os.environ["PGAPPNAME"] = label
    try:
        from wsgi import app
        with app.app_context():
            if adapter:
                import importlib.util
                loader = importlib.util.spec_from_file_location("regression_metadata_adapter", adapter)
                module = importlib.util.module_from_spec(loader)
                loader.loader.exec_module(module)
                module.install_dynamic()
                module.install_materialization()
            if role == "dynamic":
                from dynamicannotationdb import DynamicAnnotationInterface
                interface = DynamicAnnotationInterface(app.config["SQLALCHEMY_DATABASE_URI"], metadata["database"])
                client = interface.segmentation
                # Warm schema construction before the shared start barrier.
                client.schema.create_segmentation_model(metadata["annotation_table_name"],
                    metadata["schema"], metadata["pcg_table_name"], None, False)
                def invoke():
                    return client.create_segmentation_table(metadata["annotation_table_name"],
                        metadata["schema"], metadata["pcg_table_name"])
            else:
                from materializationengine.workflows.ingest_new_annotations import create_missing_segmentation_table
                from materializationengine.utils import create_segmentation_model
                create_segmentation_model(metadata)
                def invoke():
                    return create_missing_segmentation_table(metadata)
            barrier.wait(timeout=60)
            value = invoke()
            results.put({"role": role, "passed": True, "result": value})
    except Exception as error:
        results.put({"role": role, "passed": False, "error_type": type(error).__name__, "error": str(error)[:4000]})


def concurrent_creators(metadata, output, expect_failure=False, adapter=None):
    import multiprocessing
    import queue
    from sqlalchemy import text
    from wsgi import app
    from materializationengine.database import db_manager
    from dynamicannotationdb.models import SegmentationMetadata
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(5)
    results = context.Queue()
    label = "cave-metadata-test-" + uuid.uuid4().hex[:12] + "-worker"
    children = [context.Process(target=creator, args=(metadata, role, barrier, results, label + str(i), adapter))
                for i, role in enumerate(("dynamic", "materialization", "dynamic", "materialization"))]
    report = {"creator_count": len(children), "metadata_table": metadata["segmentation_table_name"],
              "expected_legacy_failure": expect_failure, "passed": False}
    collected = []
    try:
        with app.app_context():
            engine = db_manager.get_engine(metadata["database"])
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    # SHARE permits their existence checks but pauses metadata
                    # INSERTs. This stages the real race without sleeps/retries.
                    connection.execute(text("LOCK TABLE segmentation_table_metadata IN SHARE MODE"))
                    existing = connection.execute(text("SELECT count(*) FROM segmentation_table_metadata WHERE table_name=:name"),
                        {"name": metadata["segmentation_table_name"]}).scalar()
                    if existing:
                        raise ValueError("Concurrent regression requires a fresh table identity")
                    for child in children:
                        child.start()
                    barrier.wait(timeout=60)
                    deadline = time.monotonic() + 60
                    while True:
                        while True:
                            try:
                                collected.append(results.get_nowait())
                            except queue.Empty:
                                break
                        connection.execute(text("SELECT pg_stat_clear_snapshot()"))
                        waits = [dict(row) for row in connection.execute(text("SELECT application_name,wait_event FROM pg_stat_activity WHERE datname=:database AND application_name LIKE :label AND wait_event_type='Lock'"),
                            {"database": metadata["database"], "label": label + "%"})]
                        waiting = len({row["application_name"] for row in waits})
                        if waiting + len(collected) >= len(children):
                            report["paused_creator_waits"] = waits
                            report["completed_before_release"] = list(collected)
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Creators did not reach the staged database lock")
                        # Wait for a state change, not for a guessed timing gap.
                        time.sleep(0.05)
                finally:
                    transaction.rollback()
            deadline = time.monotonic() + 60
            while len(collected) < len(children):
                collected.append(results.get(timeout=max(0.1, deadline - time.monotonic())))
            for child in children:
                child.join(timeout=15)
                if child.is_alive():
                    raise RuntimeError("Concurrent creator process did not exit")
            report["creators"] = collected
            with engine.connect() as connection:
                rows = connection.execute(SegmentationMetadata.__table__.select().where(
                    SegmentationMetadata.__table__.c.table_name == metadata["segmentation_table_name"])).fetchall()
                report["metadata_row_count"] = len(rows)
                if len(rows) != 1:
                    raise RuntimeError("Concurrent creators did not commit exactly one metadata row")
                row = rows[0]
                for field, expected in (("annotation_table", metadata["annotation_table_name"]),
                    ("schema_type", metadata["schema"]), ("pcg_table_name", metadata["pcg_table_name"]), ("valid", True)):
                    if row[field] != expected:
                        raise RuntimeError("Concurrent creators produced incorrect " + field)
                report["metadata_identity_verified"] = True
                from materializationengine.utils import create_segmentation_model
                model = create_segmentation_model(metadata)
                report["segmentation_table_readable"] = connection.execute(model.__table__.select()).fetchall() == []
                if not report["segmentation_table_readable"]:
                    raise RuntimeError("Fresh linked table was not empty and readable")
            failures = [result for result in collected if not result["passed"]]
            if expect_failure:
                reproduced = [result for result in failures if metadata["segmentation_table_name"] in result.get("error", "")
                    and any(marker in result.get("error", "") for marker in ("UniqueViolation", "DuplicateTable", "already exists", "duplicate key"))]
                if not reproduced:
                    raise RuntimeError("Legacy concurrent control did not reproduce the creator race")
                report["legacy_race_reproduced"] = True
            elif failures:
                raise RuntimeError("Some concurrent creator failed: " + json.dumps(failures))
            report["passed"] = True
    except Exception as error:
        report["failure"] = str(error)
        raise
    finally:
        for child in children:
            if child.pid and child.is_alive():
                child.terminate()
                child.join(timeout=5)
        output.write_text(json.dumps(report, indent=2) + "\n")
    return report


def verify(dest, expect_failure=False, development_adapter=None):
    import numpy as np
    import requests
    from caveclient import CAVEclient
    from snapshot import compose, snapshot
    from verify_segmentation_iteration import require_disposable
    spec = json.loads((dest / "deployment.json").read_text())
    require_disposable(dest, spec)
    before = snapshot(dest)
    table = "race_" + uuid.uuid4().hex[:8]
    client = CAVEclient(spec["datastack"], server_address=spec["local_url"],
                        auth_token="00000000000000000000000000000000", write_server_cache=False)
    client.annotation.create_table(table, "bound_tag", "Disposable linked-table concurrency regression.",
        spec["resolution_nm_xyz"], write_permission="PUBLIC", read_permission="PUBLIC")
    metadata = {"schema": "bound_tag", "annotation_table_name": table,
        "segmentation_table_name": table + "__" + spec["graph_id"], "database": spec["aligned_volume"],
        "aligned_volume": spec["aligned_volume"], "pcg_table_name": spec["graph_id"],
        "reference_table": None}
    prefix = dest / "data" / table
    metadata_path = prefix.with_suffix(".metadata.json")
    metadata_path.write_text(json.dumps(metadata))
    worker_path = dest / "data/segmentation-metadata-regression.py"
    worker_path.write_text(Path(__file__).read_text())
    arguments = ["exec", "-T", "materialize", "python", "/data/segmentation-metadata-regression.py",
        "--inside", "/data/" + metadata_path.name, "--output", "/data/" + prefix.with_suffix(".creators.json").name]
    if expect_failure:
        arguments.append("--expect-legacy-failure")
    if development_adapter:
        adapter_path = dest / "data/segmentation-metadata-development-adapter.py"
        adapter_path.write_text(development_adapter.read_text())
        arguments += ["--development-adapter", "/data/" + adapter_path.name]
    report = {"graph_id": spec["graph_id"], "annotation_table": table, "passed": False,
              "expected_legacy_failure": expect_failure, "graph_edits": 0}
    try:
        compose(dest, *arguments)
        report["concurrent_creators"] = json.loads(prefix.with_suffix(".creators.json").read_text())
        if not expect_failure:
            scenario = json.loads((dest / "data/scenario.json").read_text())
            tag = "metadata_concurrency_verified"
            ids = client.annotation.post_annotation(table, [{"pt": {"position": scenario["positions_xyz"][0]}, "tag": tag}])
            response = requests.post(spec["local_url"].rstrip("/") + "/materialize/api/v2/materialize/run/complete_workflow/datastack/" + spec["datastack"] + "?days_to_expire=365&merge_tables=true", timeout=60)
            response.raise_for_status()
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                versions = client.materialize.get_versions()
                if versions:
                    try:
                        frame = client.materialize.query_table(table, materialization_version=max(versions), filter_equal_dict={"tag": tag})
                        if len(frame) == 1:
                            row = frame.iloc[0]
                            expected_root = client.chunkedgraph.get_roots(np.array([scenario["atoms"][0]], np.uint64))[0]
                            if int(row["id"]) not in [int(value) for value in ids] or int(row["pt_supervoxel_id"]) != int(scenario["atoms"][0]) or int(row["pt_root_id"]) != int(expected_root):
                                raise RuntimeError("Concurrent-creation annotation materialized with incorrect IDs")
                            report["materialization_version"] = int(max(versions))
                            report["immediate_annotation_materialization_verified"] = True
                            break
                    except requests.HTTPError as error:
                        if error.response.status_code not in (404, 409, 500):
                            raise
                time.sleep(2)
            else:
                raise RuntimeError("Annotation did not materialize after concurrent table creation")
        after = snapshot(dest)
        graph_fields = ("all_atom_to_root_sha256", "root_memberships_sha256", "edit_history_sha256", "native_operation_details_sha256")
        if any(before[field] != after[field] for field in graph_fields):
            raise RuntimeError("Concurrent table creation changed existing graph or edit history")
        for table_name, rows in before["annotation_tables"].items():
            if after["annotation_tables"].get(table_name) != rows:
                raise RuntimeError("Existing annotation rows changed")
        for version, tables in before["materialized_tables"].items():
            if after["materialized_tables"].get(version) != tables:
                raise RuntimeError("Existing materialized rows changed")
        report.update(passed=True, existing_graph_logs_annotations_preserved=True)
    except Exception as error:
        report["failure"] = str(error)
        raise
    finally:
        (dest / "data/acceptance-segmentation-metadata.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", type=Path, nargs="?")
    parser.add_argument("--inside", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expect-legacy-failure", action="store_true")
    parser.add_argument("--development-adapter", type=Path,
        help="Test an exact proposed adapter on a disposable runtime without restarting services")
    args = parser.parse_args()
    if args.inside:
        concurrent_creators(json.loads(args.inside.read_text()), args.output,
            args.expect_legacy_failure, str(args.development_adapter) if args.development_adapter else None)
    elif args.deployment:
        verify(args.deployment.resolve(), args.expect_legacy_failure, args.development_adapter)
    else:
        parser.error("Provide a deployment")
