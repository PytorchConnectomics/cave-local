"""Read-only complete graph/annotation/edit evidence for restart and restore.

Run: python scripts/snapshot.py DEPLOYMENT --output snapshot.json
Comparisons intentionally exclude ports, host paths, and the observation time.
This requires the deployed stack and Docker Compose; no edits are performed.
"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import requests
from caveclient import CAVEclient


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


def compose(dest, *args):
    spec = json.loads((dest / "deployment.json").read_text())
    command = ["docker", "compose", "--project-directory", str(dest), "-f", str(dest / "compose.yml")]
    if spec.get("project_name"):
        command += ["--project-name", spec["project_name"]]
    return subprocess.check_output(command + list(args))


def api_roots(spec, atoms):
    result = []
    base = spec["local_url"].rstrip("/") + "/segmentation/api/v1/table/" + spec["graph_id"]
    for start in range(0, len(atoms), 2048):
        batch = [str(int(atom)) for atom in atoms[start:start + 2048]]
        response = requests.post(base + "/roots?int64_as_str=true", json={"node_ids": batch}, timeout=180)
        response.raise_for_status()
        values = response.json()["root_ids"]
        if len(values) != len(batch) or any(int(root) == 0 for root in values):
            raise RuntimeError("Native roots returned an incomplete or zero-root mapping")
        result.extend(int(root) for root in values)
    return np.asarray(result, dtype=np.uint64)


def snapshot(dest):
    spec = json.loads((dest / "deployment.json").read_text())
    mapping = np.load(dest / "data/atom_index.npy")
    atoms = mapping[:, 0]
    roots = api_roots(spec, atoms)
    base = spec["local_url"].rstrip("/") + "/segmentation/api/v1/table/" + spec["graph_id"]
    memberships = []
    for root in np.unique(roots):
        expected = sorted(str(int(atom)) for atom in atoms[roots == root])
        response = requests.get(base + "/node/" + str(root) + "/leaves?int64_as_str=true", timeout=180)
        response.raise_for_status()
        actual = sorted(str(int(atom)) for atom in response.json()["leaf_ids"])
        if actual != expected:
            raise RuntimeError(f"Root {root} leaves disagree with atom-to-root mapping")
        memberships.append({"root_id": str(int(root)), "atom_count": len(actual), "atoms_sha256": sha(actual)})
    client = CAVEclient(spec["datastack"], server_address=spec["local_url"],
                        auth_token="00000000000000000000000000000000", write_server_cache=False)
    annotation = {}
    for table in sorted(client.annotation.get_tables()):
        # SQL goes directly to psql argv, never through a shell. Quote arbitrary
        # table identifiers, and collect invalidated rows as well as valid rows.
        quoted = '"' + table.replace('"', '""') + '"'
        sql = "SELECT coalesce(jsonb_agg(value ORDER BY value::text),'[]'::jsonb) FROM (SELECT to_jsonb(t) AS value FROM " + quoted + " AS t) AS rows;"
        raw = compose(dest, "exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1",
                      "-U", "cave", "-d", spec["aligned_volume"], "-A", "-t", "-c", sql)
        rows = json.loads(raw)
        annotation[table] = {"row_count": len(rows), "rows_sha256": sha(rows)}
        if len(rows) <= 1000:
            annotation[table]["rows"] = rows
    versions = sorted(int(version) for version in client.materialize.get_versions())
    materialization = {}
    for version in versions:
        tables = {}
        for table in sorted(client.materialize.get_tables(version=version)):
            records = []
            offset = 0
            while True:
                frame = client.materialize.query_table(table, materialization_version=version,
                                                       limit=10000, offset=offset)
                records.extend(json.loads(frame.to_json(orient="records")))
                if len(frame) < 10000:
                    break
                offset += len(frame)
            records.sort(key=stable_json)
            tables[table] = {"row_count": len(records), "rows_sha256": sha(records)}
            if len(records) <= 1000:
                tables[table]["rows"] = records
        materialization[str(version)] = tables
    history = compose(dest, "exec", "-T", "redis", "redis-cli", "--raw", "LRANGE", "cave:edits", "0", "-1")
    logs = [json.loads(line) for line in history.decode().splitlines() if line.strip()]
    operations = sorted({int(log["operation_id"]) for log in logs})
    details = {}
    for start in range(0, len(operations), 256):
        response = requests.get(base + "/operation_details", params={"operation_ids": json.dumps(operations[start:start + 256])}, timeout=180)
        response.raise_for_status()
        details.update(response.json())
    pairs = [[str(int(atom)), str(int(root))] for atom, root in zip(atoms, roots)]
    result = {"snapshot_format": 1, "graph_id": spec["graph_id"],
        "atom_count": len(atoms), "root_count": len(memberships),
        "all_atom_to_root_sha256": sha(pairs), "root_memberships_sha256": sha(memberships),
        "all_root_leaves_verified": True, "annotation_tables": annotation,
        "materialization_versions": versions, "materialized_tables": materialization,
        "edit_count": len(logs), "edit_history_sha256": sha(logs),
        "native_operation_count": len(details), "native_operation_details_sha256": sha(details)}
    if len(atoms) <= 10000:
        result["atom_to_root"] = pairs
        result["root_memberships"] = memberships
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = snapshot(args.deployment.resolve())
    output = args.output or args.deployment / "data/snapshot.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("graph_id", "atom_count", "root_count", "edit_count", "materialization_versions")}, indent=2))
