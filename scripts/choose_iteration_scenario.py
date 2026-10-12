"""Choose disposable neighboring components for native and browser acceptance."""
import argparse
import json
from pathlib import Path

import numpy as np
import requests
from scipy.ndimage import distance_transform_edt


def choose(dest):
    spec = json.loads((dest / "deployment.json").read_text())
    data = dest / "data"
    index = np.load(data / "atom_index.npy")
    roots = np.load(data / "initial_roots.npy")
    source_labels = spec.get("atom_policy", "source_labels") == "source_labels"
    pairs = np.load(data / ("region_adjacency.npy" if source_labels else "edges.npy"))
    if not len(pairs):
        raise ValueError("No neighboring test components; use a disposable generated fixture")
    positions = np.searchsorted(index[:, 0], pairs)
    _, inverse, counts = np.unique(roots, return_inverse=True, return_counts=True)
    rows = index[positions]
    valid = ((rows[:, 0, 1] != rows[:, 1, 1]) if source_labels else (roots[positions[:, 0]] == roots[positions[:, 1]]))
    valid &= np.all(counts[inverse[positions]] <= 128, axis=1)
    chunk = np.array(spec["graph_chunk_xyz"])
    coordinates = rows[:, :, 2:5].astype(int) // chunk
    valid &= np.all(coordinates[:, 0] == coordinates[:, 1], axis=1)
    pairs, positions, coordinates = pairs[valid], positions[valid], coordinates[valid, 0]
    best = None
    for pair, position, coordinate in zip(pairs[:1000], positions[:1000], coordinates[:1000]):
        volume = np.load(data / "chunks" / ("_".join(map(str, coordinate)) + ".npy"), mmap_mode="r")
        for z in range(volume.shape[0]):
            masks = [volume[z] == atom for atom in pair]
            if min(mask.sum() for mask in masks) < 4:
                continue
            distances = [distance_transform_edt(mask) for mask in masks]
            quality = min(distance.max() for distance in distances)
            if best is None or quality > best[0]:
                points = []
                for distance in distances:
                    y, x = np.unravel_index(np.argmax(distance), distance.shape)
                    points.append((np.array([x, y, z]) + coordinate * chunk).tolist())
                groups = [index[roots == roots[p], 0].astype(str).tolist() for p in position]
                scenario = {"atoms": pair.astype(str).tolist(), "positions_xyz": points,
                    "source_labels": index[position, 1].astype(str).tolist(),
                    "interior_radius_voxels": float(quality), "component_atoms_by_point": groups,
                    "component_atoms": sorted(set(groups[0] + groups[1]), key=int),
                    "component_atom_count": len(set(groups[0] + groups[1])),
                    "initial_relation": "separate" if source_labels else "joined",
                    "test_order": "merge_split" if source_labels else "split_merge"}
                if not source_labels:
                    scenario["source_label"] = str(int(index[position[0], 1]))
                best = (quality, scenario)
    if best is None:
        raise ValueError("No suitable small browser scenario; use a disposable generated fixture")
    scenario = best[1]
    response = requests.post(spec["local_url"].rstrip("/") + "/segmentation/api/v1/table/" + spec["graph_id"] + "/roots?int64_as_str=true",
        json={"node_ids": scenario["atoms"]}, timeout=60)
    response.raise_for_status()
    scenario["current_roots"] = [str(root) for root in response.json()["root_ids"]]
    if (scenario["current_roots"][0] != scenario["current_roots"][1]) != source_labels:
        raise ValueError("Selected fixture has already been edited; use a fresh disposable deployment")
    scenario["view_position_xyz"] = np.mean(scenario["positions_xyz"], axis=0).tolist()
    (data / "scenario.json").write_text(json.dumps(scenario, indent=2) + "\n")
    print(json.dumps(scenario, indent=2))
    return scenario


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    choose(parser.parse_args().destination.resolve())
