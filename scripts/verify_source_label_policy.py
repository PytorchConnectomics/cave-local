"""Reject same-source cuts and prove the full graph remains unchanged."""
import argparse
import json
from pathlib import Path

import numpy as np
import requests
from cloudvolume import CloudVolume

from snapshot import api_roots
from verify_segmentation_iteration import require_disposable


def verify(dest):
    spec = json.loads((dest / "deployment.json").read_text())
    require_disposable(dest, spec)
    if spec.get("atom_policy", "source_labels") != "source_labels":
        raise ValueError("Source-label rejection test requires source_labels atom policy")
    base = spec["local_url"].rstrip("/") + "/segmentation/api/v1/table/" + spec["graph_id"]
    mapping = np.load(dest / "data/atom_index.npy")
    edges = np.load(dest / "data/edges.npy")
    if not len(edges):
        raise ValueError("Fixture needs a same-label object crossing graph chunks")
    positions = np.searchsorted(mapping[:, 0], edges)
    chunk = np.asarray(spec["graph_chunk_xyz"])
    rows = mapping[positions]
    cross_chunk = np.any(rows[:, 0, 2:5] // chunk != rows[:, 1, 2:5] // chunk, axis=1)
    if not cross_chunk.any():
        raise ValueError("Fixture needs a source label spanning graph chunks")
    rows = rows[np.flatnonzero(cross_chunk)[0]]
    if rows[0, 1] != rows[1, 1]:
        raise RuntimeError("Same-source test selected different labels")
    nodes = [[str(int(row[0])), *(row[2:5].astype(float) * spec["resolution_nm_xyz"]).tolist()] for row in rows]
    before = api_roots(spec, mapping[:, 0])
    selected = np.searchsorted(mapping[:, 0], rows[:, 0])
    if before[selected[0]] != before[selected[1]]:
        raise RuntimeError("Source fragments are not joined before rejection test")
    results = []
    for name, sink in (("same_atom", nodes[0]), ("cross_chunk_fragments", nodes[1])):
        response = requests.post(base + "/split?int64_as_str=true", json={"sources": [nodes[0]], "sinks": [sink]}, timeout=60)
        if response.status_code != 400 or "indivisible" not in response.json().get("message", ""):
            raise RuntimeError(f"{name} cut was not clearly rejected: {response.status_code} {response.text}")
        if not np.array_equal(api_roots(spec, mapping[:, 0]), before):
            raise RuntimeError("Rejected split changed some graph roots")
        results.append({"case": name, "status": response.status_code, "message": response.json()["message"]})
    storage = {}
    for name, url in [("gateway", spec["local_url"].rstrip("/") + "/data/atomic")]:
        volume = CloudVolume(url, progress=False, cache=False, fill_missing=False)
        actual = [str(int(volume[int(x):int(x)+1, int(y):int(y)+1, int(z):int(z)+1][0, 0, 0, 0])) for x, y, z in rows[:, 2:5]]
        if actual != [node[0] for node in nodes]:
            raise RuntimeError("Atomic IDs at rejection-test seed coordinates are incorrect")
        storage[name] = actual
    if spec.get("atomic_public_url"):
        volume = CloudVolume(spec["atomic_public_url"], progress=False, cache=False, fill_missing=False)
        actual = [str(int(volume[int(x):int(x)+1, int(y):int(y)+1, int(z):int(z)+1][0, 0, 0, 0])) for x, y, z in rows[:, 2:5]]
        if actual != [node[0] for node in nodes]:
            raise RuntimeError("Optional public atomic source has different seed IDs")
        storage["public"] = actual
    report = {"passed": True, "source_label": str(int(rows[0, 1])),
        "atoms": [node[0] for node in nodes], "rejected_cuts": results,
        "all_atom_roots_unchanged": len(mapping), "atomic_readback": storage}
    (dest / "data/indivisible-source-label.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    verify(parser.parse_args().destination.resolve())
