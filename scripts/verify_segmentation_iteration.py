"""Exhaustive storage/topology checks, native edit round-trip and annotations.

Edits are restricted to generated disposable fixture deployments whose Compose
project name starts cave-pkg-. Original component membership is restored with
native Undo; immutable source data remain read-only. Edit logs remain as evidence.
"""
import argparse
import hashlib
import json
import time
import uuid
from pathlib import Path

import numpy as np
import requests
from caveclient import CAVEclient
from cloudvolume import CloudVolume

from ingest_segmentation_iteration import (chunk_coordinates, component_keys,
    image_geometry, source_volume, validate_geometry)
from snapshot import api_roots


def require_disposable(dest, spec):
    import yaml
    compose = yaml.safe_load((dest / "compose.yml").read_text())
    name = spec.get("project_name", compose.get("name", ""))
    if not spec.get("test_fixture") or not name.startswith("cave-pkg-") or compose.get("name") != name:
        raise ValueError("Edit acceptance requires test_fixture:true and an isolated cave-pkg- Compose project")


def host_source_spec(spec):
    result = dict(spec)
    for name in ("image_https", "segmentation_https"):
        source = result[name]
        if source.startswith("file:///source/"):
            if not spec.get("source_dir"):
                raise ValueError("Host verification of file:///source needs deployment source_dir")
            path = Path(spec["source_dir"]) / source.removeprefix("file:///source/")
            result[name] = path.resolve().as_uri()
    return result


def verify_storage(dest, spec):
    """Re-read every original and derived voxel; independently verify topology."""
    import cc3d
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    shape, chunk, _, tile, counts = validate_geometry(spec)
    host_spec = host_source_spec(spec)
    _, image = image_geometry(host_spec)
    source = source_volume(host_spec)
    atomic = CloudVolume(spec["local_url"].rstrip("/") + "/data/atomic", progress=False, cache=False, fill_missing=False)
    index = np.load(dest / "data/atom_index.npy")
    bounds = np.load(dest / "data/atom_bounds.npy")
    prepared = json.loads((dest / "data/prepared.json").read_text())
    mode = spec.get("atom_policy", "source_labels")
    digest = hashlib.sha256()
    foreground_count, voxel_count = 0, 0
    seen = set()
    for coordinate in chunk_coordinates(counts):
        lo = np.asarray(coordinate) * chunk
        hi = np.minimum(lo + chunk, shape)
        slices = tuple(slice(int(a), int(b)) for a, b in zip(lo, hi))
        labels = np.ascontiguousarray(source[slices][..., 0].transpose(2, 1, 0))
        digest.update(labels.astype("<u8").tobytes())
        cached = np.load(dest / "data/chunks" / ("_".join(map(str, coordinate)) + ".npy"))
        actual = atomic[slices][..., 0].transpose(2, 1, 0)
        if not np.array_equal(actual, cached):
            raise RuntimeError(f"Served atomic voxels differ from prepared chunk {coordinate}")
        foreground = actual != 0
        if not np.array_equal(foreground, labels != 0):
            raise RuntimeError(f"Background/foreground changed in chunk {coordinate}")
        positions = np.searchsorted(index[:, 0], actual[foreground])
        if np.any(positions >= len(index)) or not np.array_equal(index[positions, 0], actual[foreground]):
            raise RuntimeError("Served volume contains unknown atomic IDs")
        if not np.array_equal(index[positions, 1], labels[foreground]):
            raise RuntimeError("Source-label mapping changed at a voxel")
        components = cc3d.connected_components(component_keys(labels, mode, chunk, tile), connectivity=6, out_dtype=np.uint32)
        relations = np.unique(np.stack([components[foreground].astype(np.uint64), actual[foreground]], axis=1), axis=0)
        if len(relations) != len(np.unique(relations[:, 0])) or len(relations) != len(np.unique(relations[:, 1])):
            raise RuntimeError("Prepared atoms differ from expected six-connected label/tile components")
        for atom in np.unique(actual[foreground]):
            position = int(np.searchsorted(index[:, 0], atom))
            xyz = np.argwhere(actual == atom)[:, ::-1] + lo
            expected_box = np.r_[xyz.min(axis=0), xyz.max(axis=0) + 1]
            if not np.array_equal(expected_box, bounds[position]):
                raise RuntimeError(f"Atom {atom} bounding box does not match its voxels")
            point = index[position, 2:5].astype(int) - lo
            if actual[tuple(point[::-1])] != atom:
                raise RuntimeError("Atom representative point is not inside that atom")
            seen.add(int(atom))
        foreground_count += int(np.count_nonzero(foreground))
        voxel_count += labels.size
    if len(seen) != len(index) or digest.hexdigest() != prepared["source_chunk_order_sha256"]:
        raise RuntimeError("Missing atomic IDs or original source fingerprint changed")
    # Independently derive face adjacency including cross-chunk boundaries.
    region_pairs = []
    for coordinate in chunk_coordinates(counts):
        volume = np.load(dest / "data/chunks" / ("_".join(map(str, coordinate)) + ".npy"), mmap_mode="r")
        for axis in range(3):
            left, right = [slice(None)] * 3, [slice(None)] * 3
            left[axis], right[axis] = slice(None, -1), slice(1, None)
            a, b = volume[tuple(left)], volume[tuple(right)]
            mask = (a != 0) & (b != 0) & (a != b)
            if mask.any():
                region_pairs.append(np.stack([a[mask], b[mask]], axis=1))
            neighbor = list(coordinate)
            neighbor[2 - axis] += 1
            if neighbor[2 - axis] < counts[2 - axis]:
                other = np.load(dest / "data/chunks" / ("_".join(map(str, neighbor)) + ".npy"), mmap_mode="r")
                left[axis], right[axis] = -1, 0
                a, b = volume[tuple(left)], other[tuple(right)]
                mask = (a != 0) & (b != 0) & (a != b)
                if mask.any():
                    region_pairs.append(np.stack([a[mask], b[mask]], axis=1))
    regions = np.unique(np.sort(np.concatenate(region_pairs), axis=1), axis=0) if region_pairs else np.empty((0, 2), np.uint64)
    if not np.array_equal(regions, np.load(dest / "data/region_adjacency.npy")):
        raise RuntimeError("Stored region adjacency differs from voxel face adjacency")
    pos = np.searchsorted(index[:, 0], regions)
    edges = regions[index[pos[:, 0], 1] == index[pos[:, 1], 1]]
    if not np.array_equal(edges, np.load(dest / "data/edges.npy")):
        raise RuntimeError("Initial graph edges differ from same-label face adjacency")
    pos = np.searchsorted(index[:, 0], edges)
    matrix = coo_matrix((np.ones(len(pos)), (pos[:, 0], pos[:, 1])), shape=(len(index), len(index)))
    expected_count, expected_components = connected_components(matrix, directed=False)
    roots = api_roots(spec, index[:, 0])
    relations = np.unique(np.stack([expected_components.astype(np.uint64), roots], axis=1), axis=0)
    if len(relations) != expected_count or len(relations) != len(np.unique(roots)):
        raise RuntimeError("Current graph roots differ from the source connected-component partition")
    return {"all_voxels_verified": voxel_count, "foreground_voxels_verified": foreground_count,
            "all_atoms_verified": len(index), "expected_connected_components": int(expected_count),
            "source_fingerprint_verified": digest.hexdigest(), "image_geometry": image,
            "same_label_edges_verified": len(edges), "face_adjacency_edges_verified": len(regions)}


def verify(dest):
    spec = json.loads((dest / "deployment.json").read_text())
    require_disposable(dest, spec)
    scenario = json.loads((dest / "data/scenario.json").read_text())
    base = spec["local_url"].rstrip("/") + "/segmentation/api/v1/table/" + spec["graph_id"]
    resolution = np.asarray(spec["resolution_nm_xyz"])
    nodes = [[atom, *(np.asarray(point) * resolution).tolist()] for atom, point in zip(scenario["atoms"], scenario["positions_xyz"])]
    report = {"graph": spec["graph_id"], "scenario": scenario, "operations": {}, "storage_before": verify_storage(dest, spec)}

    def post(path, body):
        response = requests.post(base + path + "?int64_as_str=true", json=body, timeout=180)
        if not response.ok:
            raise RuntimeError(path + ": " + response.text[:2000])
        return response.json()

    def roots():
        return [str(root) for root in post("/roots", {"node_ids": scenario["atoms"]})["root_ids"]]

    def relation(separate):
        current = roots()
        if (current[0] != current[1]) != separate:
            raise RuntimeError("Native edit did not produce the expected component relation")
        return current

    separate = scenario.get("initial_relation") == "separate"
    relation(separate)
    if separate:
        merged = post("/merge", nodes)
        relation(False)
        split = post("/split", {"sources": [nodes[0]], "sinks": [nodes[1]]})
        relation(True)
        report["operations"]["undo_split"] = post("/undo", {"operation_id": split["operation_id"]})
        relation(False)
        report["operations"]["undo_merge"] = post("/undo", {"operation_id": merged["operation_id"]})
    else:
        split = post("/split", {"sources": [nodes[0]], "sinks": [nodes[1]]})
        relation(True)
        merged = post("/merge", nodes)
        relation(False)
        report["operations"]["undo_merge"] = post("/undo", {"operation_id": merged["operation_id"]})
        relation(True)
        report["operations"]["undo_split"] = post("/undo", {"operation_id": split["operation_id"]})
    report["operations"].update(merge=merged, split=split)
    final = relation(separate)
    expected = scenario["component_atoms_by_point"] if separate else [scenario["component_atoms"]]
    import DracoPy
    index = np.load(dest / "data/atom_index.npy")
    bounds = np.load(dest / "data/atom_bounds.npy")
    mesh_reports = []
    for root, component in zip(dict.fromkeys(final), expected):
        leaves = requests.get(base + "/node/" + root + "/leaves?int64_as_str=true", timeout=60)
        leaves.raise_for_status()
        if {str(value) for value in leaves.json()["leaf_ids"]} != set(component):
            raise RuntimeError("Undo did not restore the complete component membership")
        mesh = requests.get(spec["local_url"].rstrip("/") + "/meshing/api/v1/table/" + spec["graph_id"] + "/manifest/" + root + ":0", timeout=180)
        mesh.raise_for_status()
        if not mesh.json()["fragments"]:
            raise RuntimeError("Restored root has no mesh")
        selected = np.searchsorted(index[:, 0], np.asarray(component, np.uint64))
        low, high = bounds[selected, :3].min(axis=0) * resolution, bounds[selected, 3:].max(axis=0) * resolution
        for fragment in mesh.json()["fragments"]:
            response = requests.get(spec["local_url"].rstrip("/") + "/data/meshes/" + fragment, timeout=60)
            response.raise_for_status()
            decoded = DracoPy.decode(response.content)
            vertices = np.asarray(decoded.points)
            faces = np.asarray(decoded.faces)
            if not len(vertices) or not len(faces) or not np.all(np.isfinite(vertices)):
                raise RuntimeError("Mesh has no valid vertices/triangles")
            if np.any(vertices.min(axis=0) < low - resolution) or np.any(vertices.max(axis=0) > high + resolution):
                raise RuntimeError("Mesh coordinates do not align with segmentation bounds")
            mesh_reports.append({"root": root, "vertices": len(vertices), "triangles": len(faces), "bounds_nm": [vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()]})
    report["meshes"] = mesh_reports
    report["storage_after"] = verify_storage(dest, spec)
    client = CAVEclient(spec["datastack"], server_address=spec["local_url"],
                        auth_token="00000000000000000000000000000000", write_server_cache=False)
    if client.info.get_datastack_info()["aligned_volume"]["name"] != spec["aligned_volume"]:
        raise RuntimeError("CAVEclient resolved the wrong aligned volume")
    if [str(root) for root in client.chunkedgraph.get_roots(np.asarray(scenario["atoms"], np.uint64))] != final:
        raise RuntimeError("Official client disagrees with native roots API")
    table, tag = "setup_validation", "fixture_" + uuid.uuid4().hex
    if table not in client.annotation.get_tables():
        client.annotation.create_table(table, "bound_tag", "Disposable setup acceptance annotations.", resolution.tolist(), write_permission="PUBLIC", read_permission="PUBLIC")
    ids = client.annotation.post_annotation(table, [{"pt": {"position": scenario["positions_xyz"][0]}, "tag": tag}])
    report["annotation_ids"] = ids
    live = client.annotation.get_annotation(table, ids)
    if not live or live[0]["tag"] != tag:
        raise RuntimeError("Inserted annotation could not be read back")
    response = requests.post(spec["local_url"].rstrip("/") + "/materialize/api/v2/materialize/run/complete_workflow/datastack/" + spec["datastack"] + "?days_to_expire=365&merge_tables=true", timeout=60)
    response.raise_for_status()
    report["snapshot_trigger_status"] = response.status_code
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        versions = client.materialize.get_versions()
        if versions:
            try:
                frame = client.materialize.query_table(table, materialization_version=max(versions), filter_equal_dict={"tag": tag})
                if len(frame) == 1:
                    row = frame.iloc[0]
                    if int(row["pt_supervoxel_id"]) != int(scenario["atoms"][0]) or int(row["pt_root_id"]) != int(final[0]):
                        raise RuntimeError("Materialized annotation has incorrect segmentation IDs")
                    if int(row["id"]) not in [int(value) for value in ids]:
                        raise RuntimeError("Materialized row is not the inserted annotation")
                    report.update(materialization_version=int(max(versions)), materialized_annotation_verified=True)
                    break
            except requests.HTTPError as error:
                if error.response.status_code not in (404, 409, 500):
                    raise
        print("Waiting for annotation materialization…", flush=True)
        time.sleep(3)
    else:
        raise RuntimeError("Inserted annotation was not materialized before the timeout")
    report.update(passed=True, component_membership_restored=True, final_roots=final)
    scenario["current_roots"] = final
    (dest / "data/scenario.json").write_text(json.dumps(scenario, indent=2) + "\n")
    (dest / "data/acceptance-api.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--storage-only", action="store_true")
    args = parser.parse_args()
    destination = args.destination.resolve()
    if args.storage_only:
        print(json.dumps(verify_storage(destination, json.loads((destination / "deployment.json").read_text())), indent=2))
    else:
        verify(destination)
