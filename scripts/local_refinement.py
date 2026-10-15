"""Bounded OFFLINE refinement journal; never a native PyChunkedGraph writer.

All arrays use ZYX; point coordinates and chunk sizes use XYZ. Native ID bit
layout is mirrored from PyChunkedGraph 5327b224 graph/chunks/utils.py. IDs are
not reserved in native tables. This store must never be imported into a live
CAVE deployment without a separate integration/migration implementation.
"""

import argparse
import ctypes
import errno
from contextlib import contextmanager
import fcntl
import hashlib
import heapq
import io
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

import numpy as np
from scipy import ndimage

MAX_VOXELS = 262144


class StaleVersion(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def array_bytes(array):
    out = io.BytesIO()
    np.save(out, array, allow_pickle=False)
    return out.getvalue()


def write_sync(path, raw):
    with open(path, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rename_new_directory(source, destination):
    """Publish an export without replacing even an empty competing directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOTSUP, "Atomic export requires Linux renameat2")
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def chunk_prefix(xyz, spatial_bits=10, layer_bits=8):
    if not (
        1 <= spatial_bits and 1 <= layer_bits and 64 - layer_bits - 3 * spatial_bits > 0
    ):
        raise ValueError("Invalid ID bit layout")
    if len(xyz) != 3 or any(
        not isinstance(v, (int, np.integer)) or v < 0 or v >= 2**spatial_bits
        for v in xyz
    ):
        raise ValueError("Chunk coordinate out of range")
    layer_offset = 64 - layer_bits
    return (
        (1 << layer_offset)
        | (int(xyz[0]) << (layer_offset - spatial_bits))
        | (int(xyz[1]) << (layer_offset - 2 * spatial_bits))
        | (int(xyz[2]) << (layer_offset - 3 * spatial_bits))
    )


def neighbors(point, shape):
    for axis in range(3):
        for delta in (-1, 1):
            other = list(point)
            other[axis] += delta
            if 0 <= other[axis] < shape[axis]:
                yield tuple(other)


def partition(mask, seeds=None, mode="seeds"):
    if mode == "components":
        labels, _ = ndimage.label(mask)
    elif mode == "seeds":
        if not isinstance(seeds, (list, tuple)) or len(seeds) < 2:
            raise ValueError("At least two seed points required")
        points = []
        for seed in seeds:
            if (
                not isinstance(seed, (list, tuple))
                or len(seed) != 3
                or any(type(v) is not int for v in seed)
            ):
                raise ValueError("Seeds must be integer XYZ points")
            point = tuple(reversed(seed))
            if (
                any(v < 0 or v >= mask.shape[i] for i, v in enumerate(point))
                or not mask[point]
            ):
                raise ValueError("Seed outside selected supervoxel")
            points.append(point)
        points.sort()
        if len(set(points)) != len(points):
            raise ValueError("Duplicate seed")
        # Shape-only watershed: flood from deep interior toward narrow boundaries.
        height = -ndimage.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1, 1:-1]
        labels = np.zeros(mask.shape, dtype=np.int32)
        best_level = np.full(mask.shape, np.inf)
        best_steps = np.full(mask.shape, np.iinfo(np.int32).max, dtype=np.int32)
        finalized = np.zeros(mask.shape, dtype=bool)
        markers = set(points)
        queue = []
        for group, point in enumerate(points, 1):
            labels[point] = group
            best_level[point], best_steps[point] = height[point], 0
            heapq.heappush(queue, (float(height[point]), 0, group, point))
        while queue:
            level, steps, group, point = heapq.heappop(queue)
            if finalized[point] or (level, steps, group) != (
                best_level[point],
                best_steps[point],
                labels[point],
            ):
                continue
            finalized[point] = True
            for other in neighbors(point, mask.shape):
                if not mask[other] or finalized[other] or other in markers:
                    continue
                cost = (max(level, float(height[other])), steps + 1, group)
                previous = (best_level[other], best_steps[other], labels[other])
                if cost < previous:
                    best_level[other], best_steps[other], labels[other] = cost
                    heapq.heappush(queue, (*cost, other))
        if np.any(mask & (labels == 0)):
            raise ValueError("Unseeded disconnected component; mark every component")
    else:
        raise ValueError("Unknown partition mode")
    if len(np.unique(labels[mask])) < 2:
        raise ValueError("Refinement must produce at least two children")
    return labels


def derived(labels, roots, annotations):
    ids = [int(x) for x in np.unique(labels) if x]
    membership = {str(x): int(roots[str(x)]) for x in ids}
    pairs = set()
    for axis in range(3):
        a, b = [slice(None)] * 3, [slice(None)] * 3
        a[axis], b[axis] = slice(None, -1), slice(1, None)
        left, right = labels[tuple(a)], labels[tuple(b)]
        mask = (left != right) & (left != 0) & (right != 0)
        for x, y in zip(left[mask], right[mask]):
            pairs.add(tuple(sorted((int(x), int(y)))))
    mapped = []
    if not isinstance(annotations, list):
        raise ValueError("Annotations must be a list of point records")
    for annotation in annotations:
        if not isinstance(annotation, dict) or "point_xyz" not in annotation:
            raise ValueError("Each annotation must be a record with point_xyz")
        point = annotation["point_xyz"]
        if (
            not isinstance(point, (list, tuple))
            or len(point) != 3
            or any(type(v) is not int for v in point)
        ):
            raise ValueError("Annotation points must be integer XYZ")
        p = tuple(reversed(point))
        if any(v < 0 or v >= labels.shape[i] for i, v in enumerate(p)):
            raise ValueError("Annotation out of bounds")
        atom = int(labels[p])
        mapped.append(
            {**annotation, "atom_id": atom, "root_id": membership.get(str(atom), 0)}
        )
    # A voxel-face mesh represented as oriented unit quads [x,y,z,axis,sign].
    # Root meshes deliberately omit faces between atoms sharing a root.
    root_labels = np.zeros(labels.shape, dtype=np.uint64)
    for atom, root in membership.items():
        root_labels[labels == int(atom)] = root
    faces = {}
    for p in zip(*np.nonzero(root_labels)):
        root = int(root_labels[p])
        for axis in range(3):
            for sign in (-1, 1):
                q = list(p)
                q[axis] += sign
                if (
                    q[axis] < 0
                    or q[axis] >= labels.shape[axis]
                    or int(root_labels[tuple(q)]) != root
                ):
                    faces.setdefault(str(root), []).append(
                        [int(p[2]), int(p[1]), int(p[0]), 2 - axis, sign]
                    )
    meshes = {root: digest(canonical(sorted(quads))) for root, quads in faces.items()}
    snapshot = {
        "offline_prototype": True,
        "native_history_consistent": False,
        "membership": membership,
        "adjacency": [list(p) for p in sorted(pairs)],
        "joined_edges": [
            list(p)
            for p in sorted(pairs)
            if membership[str(p[0])] == membership[str(p[1])]
        ],
        "annotations": mapped,
        "root_mesh_sha256": meshes,
    }
    return snapshot, faces


class Store:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.name.startswith("cave-pkg-"):
            raise ValueError("Offline store directory must start cave-pkg-")

    @contextmanager
    def lock(self):
        with open(self.path / ".lock", "a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def init(
        self,
        labels,
        roots,
        annotations=None,
        chunk_xyz=(64, 64, 64),
        spatial_bits=10,
        layer_bits=8,
    ):
        labels = np.asarray(labels)
        if (
            labels.ndim != 3
            or labels.dtype != np.uint64
            or labels.size > MAX_VOXELS
            or any(x == 0 for x in labels.shape)
        ):
            raise ValueError("Require bounded nonempty 3D uint64 atom labels")
        if len(chunk_xyz) != 3 or any(type(x) is not int or x <= 0 for x in chunk_xyz):
            raise ValueError("Require positive integer XYZ chunk size")
        chunk_prefix((0, 0, 0), spatial_bits, layer_bits)
        if not isinstance(roots, dict) or any(
            type(v) is not int for v in roots.values()
        ):
            raise ValueError("Root IDs must be integers")
        roots = {str(k): v for k, v in roots.items()}
        ids = {str(int(x)) for x in np.unique(labels) if x}
        if set(roots) != ids or any(v <= 0 or v >= 2**64 for v in roots.values()):
            raise ValueError(
                "Exact positive root membership required for every nonzero atom"
            )
        bits = 64 - layer_bits - 3 * spatial_bits
        for atom in ids:
            positions = np.argwhere(labels == int(atom))[:, ::-1] // np.array(chunk_xyz)
            if (
                len(np.unique(positions, axis=0)) != 1
                or int(atom) >> bits
                != chunk_prefix(tuple(positions[0]), spatial_bits, layer_bits) >> bits
                or int(atom) & ((1 << bits) - 1) == 0
            ):
                raise ValueError("Atom IDs must be layer1 and match their unique chunk")
        raw = array_bytes(labels)
        meta = {
            "base_sha256": digest(raw),
            "shape_zyx": list(labels.shape),
            "chunk_xyz": list(chunk_xyz),
            "spatial_bits": spatial_bits,
            "layer_bits": layer_bits,
            "roots": roots,
            "annotations": [] if annotations is None else annotations,
            "max_voxels": MAX_VOXELS,
        }
        derived(labels, roots, meta["annotations"])
        # Exclusive directory creation refuses any existing dataset or deployment.
        self.path.mkdir(parents=False)
        (self.path / "events").mkdir()
        (self.path / "orphans").mkdir()
        write_sync(self.path / "base.npy", raw)
        write_sync(self.path / "meta.json", canonical(meta))
        base = digest(canonical(meta))
        write_sync(self.path / "CURRENT", base.encode())
        sync_dir(self.path)
        return base

    def current(self):
        return (self.path / "CURRENT").read_text()

    def base(self):
        meta = json.loads((self.path / "meta.json").read_bytes())
        raw = (self.path / "base.npy").read_bytes()
        if digest(raw) != meta["base_sha256"]:
            raise ValueError("Base hash mismatch")
        labels = np.load(io.BytesIO(raw), allow_pickle=False)
        if (
            labels.dtype != np.uint64
            or labels.ndim != 3
            or labels.size > MAX_VOXELS
            or list(labels.shape) != meta["shape_zyx"]
        ):
            raise ValueError("Invalid base labels")
        return labels, meta, digest(canonical(meta))

    def event(self, version):
        if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
            raise ValueError("Invalid version")
        path = self.path / "events" / version
        raw = (path / "event.json").read_bytes()
        event = json.loads(raw)
        if digest(raw) != version:
            raise ValueError("Event hash mismatch")
        for name, sha in event["files"].items():
            if (
                name not in ("before.npy", "after.npy", "snapshot.json", "meshes.json")
                or digest((path / name).read_bytes()) != sha
            ):
                raise ValueError("Event artifact hash mismatch")
        if set(event["files"]) != {
            "before.npy",
            "after.npy",
            "snapshot.json",
            "meshes.json",
        }:
            raise ValueError("Incomplete event")
        return event, path

    def read(self, version=None):
        labels, meta, base = self.base()
        if version is not None:
            published = {base}
            cursor = self.current()
            while cursor != base:
                if cursor in published:
                    raise ValueError("History cycle")
                published.add(cursor)
                cursor = self.event(cursor)[0]["parent"]
            if version not in published:
                raise ValueError(
                    "Version is not published on the CURRENT ancestry chain"
                )
        else:
            version = self.current()
        chain, cursor, seen = [], version, set()
        while cursor != base:
            if cursor in seen:
                raise ValueError("History cycle")
            seen.add(cursor)
            event, path = self.event(cursor)
            if event["base"] != base:
                raise ValueError("Wrong base version")
            chain.append((cursor, event, path))
            cursor = event["parent"]
        roots = dict(meta["roots"])
        for _, event, path in reversed(chain):
            slices = tuple(slice(a, b) for a, b in event["bbox_zyx"])
            before = np.load(path / "before.npy", allow_pickle=False)
            after = np.load(path / "after.npy", allow_pickle=False)
            if (
                before.dtype != np.uint64
                or after.dtype != np.uint64
                or after.shape != before.shape
                or not np.array_equal(labels[slices], before)
            ):
                raise ValueError("Patch before-image mismatch")
            labels[slices] = after
            roots = event["roots"]
            snapshot, meshes = derived(labels, roots, meta["annotations"])
            if (
                canonical(snapshot) != (path / "snapshot.json").read_bytes()
                or canonical(meshes) != (path / "meshes.json").read_bytes()
            ):
                raise ValueError("Derived artifacts mismatch")
        return labels, roots, meta, version

    def preview(self, parent, seeds=None, mode="seeds", version=None):
        labels, roots, meta, version = self.read(version)
        mask = labels == parent
        if not np.any(mask) or parent == 0:
            raise ValueError("Selected atom not present")
        groups = partition(mask, seeds, mode)
        bits = 64 - meta["layer_bits"] - 3 * meta["spatial_bits"]
        limit = (1 << bits) - 1
        prefix = parent & ~limit
        # Count all historical allocations, including undone/quarantined events.
        allocated = [int(x) for x in np.unique(labels)] + [
            int(x)
            for x in np.unique(np.load(self.path / "base.npy", allow_pickle=False))
        ]
        for folder in ("events", "orphans"):
            for p in (self.path / folder).glob("*/event.json"):
                try:
                    raw = p.read_bytes()
                    record = json.loads(raw)
                    if digest(raw) != p.parent.name:
                        raise ValueError("Allocation record hash mismatch")
                    allocated.extend(record.get("children", []))
                except (ValueError, OSError, TypeError) as error:
                    raise ValueError(
                        f"Invalid allocation record {p}: {error}"
                    ) from error
        maximum = max([x & limit for x in allocated if x & ~limit == prefix], default=0)
        numbers = sorted(int(x) for x in np.unique(groups[mask]))
        if maximum + len(numbers) > limit:
            raise ValueError("Chunk ID space exhausted")
        after = labels.copy()
        children = []
        for i, group in enumerate(numbers, 1):
            child = prefix | (maximum + i)
            children.append(child)
            after[groups == group] = child
        if not np.array_equal(after[~mask], labels[~mask]) or np.any(
            after[mask] == parent
        ):
            raise ValueError("Partition invariant violated")
        bounds = {}
        for child in children:
            positions = np.argwhere(after == child)[:, ::-1]
            bounds[str(child)] = {
                "min": positions.min(axis=0).tolist(),
                "max_exclusive": (positions.max(axis=0) + 1).tolist(),
            }
        result = {
            "version": version,
            "parent_atom": int(parent),
            "children": children,
            "counts": [int(np.count_nonzero(after == x)) for x in children],
            "child_bounds_xyz": bounds,
            "mode": mode,
            "seeds": sorted(seeds or []) if mode == "seeds" else [],
            "partition_sha256": digest(array_bytes(after)),
            "offline_prototype": True,
            "native_history_consistent": False,
        }
        result["preview_hash"] = digest(canonical(result))
        return result, after

    def unpublished(self):
        _, _, base = self.base()
        reachable = set()
        cursor = self.current()
        while cursor != base:
            reachable.add(cursor)
            cursor = self.event(cursor)[0]["parent"]
        events = [
            str(p.relative_to(self.path))
            for p in (self.path / "events").iterdir()
            if p.is_dir() and p.name not in reachable
        ]
        staging = [p.name for p in self.path.glob(".tmp-*")]
        return sorted(events + staging)

    def recover_locked(self):
        self.read()
        _, _, base = self.base()
        reachable = set()
        cursor = self.current()
        while cursor != base:
            reachable.add(cursor)
            cursor = self.event(cursor)[0]["parent"]
        quarantined = []
        for p in (self.path / "events").iterdir():
            if p.is_dir() and p.name not in reachable:
                if (self.path / "orphans" / p.name).exists():
                    raise ValueError("Duplicate orphan event")
                p.rename(self.path / "orphans" / p.name)
                quarantined.append(p.name)
        for p in self.path.glob(".tmp-*"):
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        sync_dir(self.path / "events")
        sync_dir(self.path / "orphans")
        sync_dir(self.path)
        return quarantined

    def recover(self):
        with self.lock():
            return self.recover_locked()

    def publish(self, expected, before, after, roots, kind, details, fault=None):
        # Caller holds the lock. Only CURRENT is the publication point.
        _, meta, base = self.base()
        changed = np.argwhere(before != after)
        if not len(changed):
            raise ValueError("Empty patch")
        low = changed.min(axis=0)
        high = changed.max(axis=0) + 1
        bbox = [[int(a), int(b)] for a, b in zip(low, high)]
        slices = tuple(slice(a, b) for a, b in bbox)
        snapshot, meshes = derived(after, roots, meta["annotations"])
        data = {
            "before.npy": array_bytes(before[slices]),
            "after.npy": array_bytes(after[slices]),
            "snapshot.json": canonical(snapshot),
            "meshes.json": canonical(meshes),
        }
        event = {
            **details,
            "parent": expected,
            "base": base,
            "kind": kind,
            "bbox_zyx": bbox,
            "roots": roots,
            "files": {k: digest(v) for k, v in data.items()},
            "nonce": uuid.uuid4().hex,
        }
        raw = canonical(event)
        version = digest(raw)
        staging = self.path / (".tmp-" + uuid.uuid4().hex)
        staging.mkdir()
        for name, content in data.items():
            write_sync(staging / name, content)
        write_sync(staging / "event.json", raw)
        sync_dir(staging)
        if fault == "after_staging":
            raise RuntimeError("Injected after_staging")
        staging.rename(self.path / "events" / version)
        sync_dir(self.path / "events")
        if fault == "after_event":
            raise RuntimeError("Injected after_event")
        pointer = self.path / (".tmp-current-" + uuid.uuid4().hex)
        write_sync(pointer, version.encode())
        if fault == "before_current":
            raise RuntimeError("Injected before_current")
        if self.current() != expected:
            raise StaleVersion("stale version")
        pointer.replace(self.path / "CURRENT")
        sync_dir(self.path)
        if fault == "after_current":
            raise RuntimeError("Injected after_current")
        return version

    def apply(
        self, expected, parent, seeds=None, mode="seeds", preview_hash=None, fault=None
    ):
        with self.lock():
            if self.current() != expected:
                raise StaleVersion("stale version")
            self.recover_locked()
            result, after = self.preview(parent, seeds, mode, expected)
            if preview_hash and result["preview_hash"] != preview_hash:
                raise StaleVersion("stale preview")
            before, roots, _, _ = self.read(expected)
            inherited = roots.pop(str(parent))
            roots.update({str(child): inherited for child in result["children"]})
            return self.publish(expected, before, after, roots, "refine", result, fault)

    def undo(self, expected, fault=None):
        # Bounded first release: latest event only; undoing undo acts as redo.
        with self.lock():
            if self.current() != expected:
                raise StaleVersion("stale version")
            self.recover_locked()
            if expected == self.base()[2]:
                raise ValueError("Nothing to undo")
            event, _ = self.event(expected)
            before, _, _, _ = self.read(expected)
            after, roots, _, _ = self.read(event["parent"])
            return self.publish(
                expected,
                before,
                after,
                roots,
                "undo",
                {"undo_of": expected, "children": []},
                fault,
            )

    def export(self, destination, version=None):
        labels, roots, meta, version = self.read(version)
        snapshot, meshes = derived(labels, roots, meta["annotations"])
        destination = Path(destination)
        if not destination.name.startswith("cave-pkg-"):
            raise ValueError("Export directory must start cave-pkg-")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        staging = destination.with_name(
            ".tmp-" + destination.name + "-" + uuid.uuid4().hex
        )
        staging.mkdir(parents=False)
        write_sync(staging / "labels.npy", array_bytes(labels))
        write_sync(
            staging / "snapshot.json",
            canonical(
                {**snapshot, "version": version, "base_sha256": meta["base_sha256"]}
            ),
        )
        write_sync(staging / "meshes.json", canonical(meshes))
        sync_dir(staging)
        rename_new_directory(staging, destination)
        sync_dir(destination.parent)
        return snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.add_argument("--labels", required=True)
    p.add_argument("--roots", required=True)
    p.add_argument("--annotations")
    p.add_argument("--chunk-xyz", type=int, nargs=3, default=[64, 64, 64])
    p.add_argument("--spatial-bits", type=int, default=10)
    p.add_argument("--layer-bits", type=int, default=8)
    for command in ("preview", "apply"):
        p = sub.add_parser(command)
        p.add_argument("--parent", type=int, required=True)
        p.add_argument("--seeds", type=json.loads)
        p.add_argument("--mode", choices=["seeds", "components"], default="seeds")
        if command == "apply":
            p.add_argument("--expected-version", required=True)
            p.add_argument("--preview-hash")
        else:
            p.add_argument("--version")
    p = sub.add_parser("undo")
    p.add_argument("--expected-version", required=True)
    p = sub.add_parser("read")
    p.add_argument("--version")
    p.add_argument("--point-xyz", nargs=3, type=int, required=True)
    p = sub.add_parser("export")
    p.add_argument("--version")
    p.add_argument("--output", required=True)
    sub.add_parser("status")
    sub.add_parser("verify")
    sub.add_parser("recover")
    args = parser.parse_args(argv)
    try:
        store = Store(args.store)
        if args.command == "init":
            result = store.init(
                np.load(args.labels, allow_pickle=False),
                json.loads(Path(args.roots).read_text()),
                json.loads(Path(args.annotations).read_text())
                if args.annotations
                else [],
                args.chunk_xyz,
                args.spatial_bits,
                args.layer_bits,
            )
        elif args.command == "preview":
            result = store.preview(args.parent, args.seeds, args.mode, args.version)[0]
        elif args.command == "apply":
            result = store.apply(
                args.expected_version,
                args.parent,
                args.seeds,
                args.mode,
                args.preview_hash,
            )
        elif args.command == "undo":
            result = store.undo(args.expected_version)
        elif args.command == "export":
            result = store.export(args.output, args.version)
        elif args.command == "recover":
            result = store.recover()
        elif args.command == "read":
            labels, roots, _, version = store.read(args.version)
            point = args.point_xyz
            if any(v < 0 or v >= labels.shape[2 - i] for i, v in enumerate(point)):
                raise ValueError("Point out of bounds")
            atom = int(labels[tuple(reversed(point))])
            result = {
                "version": version,
                "atom_id": atom,
                "root_id": roots.get(str(atom), 0),
            }
        else:
            labels, roots, _, version = store.read()
            leftovers = store.unpublished()
            if args.command == "verify" and leftovers:
                raise ValueError(
                    "Unpublished artifacts; run recover: " + ", ".join(leftovers)
                )
            result = {
                "version": version,
                "atoms": len(roots),
                "voxels": int(labels.size),
                "verified": not leftovers,
                "unpublished_artifacts": leftovers,
                "offline_prototype": True,
                "native_history_consistent": False,
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    except StaleVersion as error:
        print(str(error), file=sys.stderr)
        return 3
    except (ValueError, OSError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
