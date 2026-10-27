"""Journaled, root-preserving refinement of local CAVE atomic supervoxels.

PCG imports are deliberately lazy: journal/fence tests run without its container.
Only this module writes refinement payloads. All writers hold the deployment
flock, and every level-2 row touched by a replacement contributes its own root
to the lock and fence set (neighbouring atoms need not belong to the same root).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from typing import Callable

import numpy as np


FENCED_STATES = frozenset(("fenced", "publishing", "published", "verify_failed", "verified"))
TRANSITIONS = {
    "prepared": {"fenced", "aborted"},
    "fenced": {"publishing", "aborted"},
    "publishing": {"published"},
    "published": {"verified", "verify_failed"},
    "verify_failed": {"verified", "verify_failed"},
    "verified": {"done"},
    "done": set(), "aborted": set(),
}
PUBLICATION_STEPS = ("7a", "7b", "7c", "7d", "7e", "7f")
READER_RECOVERY_COOLDOWN_SECONDS = 10.0
_reader_recovery_failures = {}


class RefinementError(RuntimeError):
    status_code = 503


class PreconditionsChanged(RefinementError):
    pass


class LockLost(RefinementError):
    pass


@dataclass
class RefineResult:
    count: int = 0
    operation_ids: list[str] = field(default_factory=list)
    new_ids: dict[int, list[int]] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


def _fsync_directory(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, content: bytes):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        with temporary.open("wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def mutation_lock(data_dir="/data", *, blocking=True):
    directory = Path(data_dir) / "supervoxel_splits"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Journal:
    def __init__(self, path, data=None):
        self.path = Path(path)
        self.data = data if data is not None else json.loads((self.path / "journal.json").read_text())

    @property
    def state(self):
        return self.data["state"]

    def save(self):
        atomic_write(self.path / "journal.json", _json_bytes(self.data))

    def transition(self, state):
        if state not in TRANSITIONS.get(self.state, ()):
            raise ValueError(f"Illegal refinement journal transition {self.state} -> {state}")
        self.data["state"] = state
        self.save()

    def marker(self, step):
        if step not in self.data.setdefault("markers", []):
            self.data["markers"].append(step)
            self.save()

    def abort(self):
        if self.state not in ("prepared", "fenced"):
            raise ValueError(f"Illegal refinement journal transition {self.state} -> aborted")
        self.abort_fence_lifted = False
        errors = []
        try:
            self.transition("aborted")
            self.abort_fence_lifted = True
        except Exception as error:
            errors.append(error)
        # Both actions independently remove the active fence. In particular,
        # ENOSPC while writing the state must not prevent the directory rename,
        # and a failed rename must not undo a persisted aborted transition.
        destination = self.path.parent / "aborted" / self.path.name
        moved = False
        try:
            destination.parent.mkdir(exist_ok=True)
            os.replace(self.path, destination)
            self.path = destination
            moved = True
            self.abort_fence_lifted = True
            _fsync_directory(destination.parent)
            _fsync_directory(destination.parent.parent)
        except Exception as error:
            errors.append(error)
        if moved and errors:
            # Retain an accurate archived state after a transient write error.
            # The archive is already excluded from active fence enumeration.
            self.data["state"] = "aborted"
            try:
                self.save()
            except Exception as error:
                errors.append(error)
        if errors:
            for error in errors[1:]:
                errors[0].add_note(f"Additional abort cleanup failure: {error!r}")
            raise errors[0]


def journals(data_dir="/data"):
    directory = Path(data_dir) / "supervoxel_splits" / "journal"
    if directory.exists():
        for path in sorted(directory.glob("*/journal.json")):
            # A malformed journal must fail closed, not silently remove a fence.
            # Abort moves the whole directory atomically, while native post-lock
            # fence readers intentionally do not take the mutation flock.
            try:
                item = Journal(path.parent)
            except FileNotFoundError:
                continue
            yield item


def fenced_roots(data_dir="/data"):
    return {int(root) for journal in journals(data_dir) if journal.state in FENCED_STATES
            for root in journal.data["roots"]}


def fixture_fault(point, data_dir="/data", **context):
    """Controlled faults are inert unless deployment.json explicitly marks a fixture.

    ``file:/.../fault.json`` permits changing a named pause without restarting
    workers. A pause writes ``<fault file>.reached`` (or marker_file), and waits
    for control_file removal, config removal/change, or a supplied duration.
    """
    setting = os.environ.get("CAVE_SVSPLIT_FAULT", "")
    if not setting:
        return
    deployment = Path(os.environ.get("CAVE_DEPLOYMENT_CONFIG", "/workspace/deployment.json"))
    if not deployment.exists() or json.loads(deployment.read_text()).get("test_fixture") is not True:
        return
    source = Path(setting[5:]) if setting.startswith("file:") else None
    if source is not None:
        if not source.exists():
            return
        config = json.loads(source.read_text())
    else:
        try:
            config = json.loads(setting)
        except json.JSONDecodeError:
            action, _, at = setting.partition(":")
            config = {"point": at, "action": action}
    if config.get("point") != point:
        return
    action = config.get("action", "pause")
    marker = Path(config.get("marker_file", str(source) + ".reached" if source else
                             str(Path(data_dir) / "supervoxel_splits" / f"fault-{point}.reached")))
    atomic_write(marker, _json_bytes({"point": point, "pid": os.getpid(), **context}))
    if action == "exit":
        if config.get("graceful"):
            raise SystemExit(int(config.get("exit_code", 86)))
        os._exit(int(config.get("exit_code", 86)))
    if action == "raise":
        raise RefinementError(f"Fixture fault at {point}")
    if action == "raise_once":
        if source is None:
            raise ValueError("Fixture raise_once requires a file configuration")
        source.unlink()
        raise OSError(f"Transient fixture error at {point}")
    if action != "pause":
        return
    duration = config.get("duration")
    control = Path(config["control_file"]) if config.get("control_file") else None
    start = time.monotonic()
    while duration is None or time.monotonic() - start < float(duration):
        if control is not None and not control.exists():
            break
        if source is not None:
            try:
                # Compare parsed JSON so pretty printing does not release a pause.
                current = json.loads(source.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                break
            if current != config:
                break
        if control is None and source is None and duration is None:
            raise ValueError("Fixture pause needs duration, control_file or file configuration")
        time.sleep(0.05)
    if "exit_on_release" in config:
        raise SystemExit(int(config["exit_on_release"]))


def install_lock_ownership_fix():
    """Fix PCG 2.21.1 predicates that otherwise match a historical lock owner.

    The spike demonstrated that native renew_locks(old_owner) steals a live
    newer owner's lock. Filtering to the newest cell before its value is tested
    makes ownership proof and release atomic inside the Bigtable mutation.
    """
    from pychunkedgraph.graph.client.bigtable import utils
    from google.cloud.bigtable.row_filters import (
        CellsColumnLimitFilter, ColumnRangeFilter, ConditionalRowFilter,
        PassAllFilter, RowFilterChain, TimestampRange, TimestampRangeFilter,
        ValueRangeFilter,
    )
    from pychunkedgraph.graph import attributes
    if getattr(utils, "_cave_svsplit_owner_fix", False):
        return

    def owner_filter(column, operation_id, expiry=None):
        filters = [ColumnRangeFilter(column.family_id, column.key, column.key, True, True),
                   CellsColumnLimitFilter(1)]
        if expiry is not None:
            cutoff = datetime.now(timezone.utc) - expiry
            cutoff = cutoff.replace(microsecond=(cutoff.microsecond // 1000) * 1000)
            filters.append(TimestampRangeFilter(TimestampRange(start=cutoff)))
        value = column.serialize(operation_id)
        filters.append(ValueRangeFilter(value, value, True, True))
        return RowFilterChain(filters)

    def renew(column, operation_id):
        new = attributes.Hierarchy.NewParent
        return ConditionalRowFilter(
            base_filter=owner_filter(column, operation_id),
            true_filter=ColumnRangeFilter(new.family_id, new.key, new.key, True, True),
            false_filter=PassAllFilter(True),
        )

    def unlock(column, expiry, operation_id):
        return owner_filter(column, operation_id, expiry)

    utils.get_renew_lock_filter = renew
    utils.get_unlock_root_filter = unlock
    # Indefinite locks are usually one version, but release must obey the same rule.
    utils.get_indefinite_unlock_root_filter = lambda col, op: owner_filter(col, op)
    utils._cave_svsplit_owner_fix = True


def _fence_error(message):
    from pychunkedgraph.graph.exceptions import LockingError
    try:
        from flask import g, has_request_context
        if has_request_context():
            g.svsplit_fenced = True
    except ImportError:
        pass
    return LockingError(message)


def install_fence(data_dir="/data", client_class=None):
    """Install the post-native-lock check on every newly constructed PCG client."""
    from functools import wraps
    if client_class is None:
        from pychunkedgraph.graph.client.bigtable.client import Client
        client_class = Client
        install_lock_ownership_fix()
    if getattr(client_class, "_cave_svsplit_fence", False):
        return
    for method_name in ("lock_roots", "lock_roots_indefinitely"):
        original = getattr(client_class, method_name, None)
        if original is None:
            continue

        def make_wrapper(native, name):
            @wraps(native)
            def locked(self, *args, **kwargs):
                result = native(self, *args, **kwargs)
                if not result[0]:
                    return result
                roots = result[1]
                operation = kwargs.get("operation_id", args[1] if len(args) > 1 else None)
                try:
                    if set(map(int, roots)) & fenced_roots(data_dir):
                        raise _fence_error("supervoxel refinement in progress or awaiting repair")
                    from supervoxel_history import check_translation_current
                    check_translation_current(data_dir)
                except BaseException:
                    release = self.unlock_indefinitely_locked_root if name.endswith("indefinitely") else self.unlock_root
                    for root in roots:
                        release(root, operation)
                    raise
                return result
            locked._svsplit_unwrapped = native
            return locked
        setattr(client_class, method_name, make_wrapper(original, method_name))
    client_class._cave_svsplit_fence = True


def _unwrapped_lock(client, roots, operation):
    method = client.lock_roots
    native = getattr(method, "_svsplit_unwrapped", None)
    kwargs = dict(root_ids=np.asarray(sorted(map(int, roots)), dtype=np.uint64),
                  operation_id=np.uint64(operation),
                  future_root_ids_d={np.uint64(root): np.array([], np.uint64) for root in roots},
                  max_tries=7)
    return native(client, **kwargs) if native is not None else method(**kwargs)


def _release(client, roots, operation):
    for root in roots:
        client.unlock_root(np.uint64(root), np.uint64(operation))


def _renew(client, roots, operation):
    if not client.renew_locks(np.asarray(roots, np.uint64), np.uint64(operation)):
        raise LockLost("supervoxel refinement lost a root lock; recovery required")
    # Also protect adapters/fakes that have not installed the atomic predicate.
    if any(client.get_lock_timestamp(np.uint64(root), np.uint64(operation)) is None for root in roots):
        raise LockLost("supervoxel refinement no longer owns every root lock")


def _set_epoch(data_dir, publishing):
    from supervoxel_history import read_epoch
    current = read_epoch(data_dir)
    if bool(current % 2) != publishing:
        atomic_write(Path(data_dir) / "supervoxel_splits" / "epoch", f"{current + 1}\n".encode())


def _finalize(journal, data_dir):
    """verified is a commit point: this function never reads graph or payloads."""
    path = Path(data_dir) / "supervoxel_splits" / "log.jsonl"
    lines = path.read_text().splitlines() if path.exists() else []
    operation = journal.data["operation_id"]
    if not any(json.loads(line)["operation_id"] == operation for line in lines if line.strip()):
        entry = {key: journal.data[key] for key in ("operation_id", "roots", "old_atom", "new_atoms", "t1", "t2", "reason")}
        # Atomic replacement survives an interrupted append without a torn JSON line.
        atomic_write(path, ("\n".join(lines + [json.dumps(entry, sort_keys=True)]) + "\n").encode())
    journal.transition("done")


def publish_journal(journal, store, *, renew: Callable, data_dir="/data", fault=fixture_fault):
    """Dependency-ordered replay, with an idempotent store retained in the journal."""
    if journal.state == "verified":
        _finalize(journal, data_dir)
        return
    if journal.state not in ("publishing", "published", "verify_failed"):
        raise ValueError(f"Cannot publish journal in state {journal.state}")
    if journal.state == "publishing":
        _set_epoch(data_dir, True)
        for step in PUBLICATION_STEPS:
            renew()
            # Markers are evidence, not authorization to skip a partially written
            # group. The store independently hash-checks every destination.
            store.apply_step(step)
            journal.marker(step)
            fault(step, data_dir, operation_id=journal.data["operation_id"])
        journal.transition("published")
        _set_epoch(data_dir, False)
    elif journal.state == "published":
        # A crash can occur after persisting published but before the even epoch.
        _set_epoch(data_dir, False)
    renew()
    try:
        store.verify()
    except Exception as error:
        journal.data["verification_error"] = str(error)
        journal.transition("verify_failed")
        raise RefinementError(f"supervoxel verification failed; operation {journal.data['operation_id']} awaits repair") from error
    journal.transition("verified")
    fault("verified", data_dir, operation_id=journal.data["operation_id"])
    _finalize(journal, data_dir)


def _time_text(value):
    return value.isoformat() if value is not None else None


def _root_timestamp(cg, root):
    from pychunkedgraph.graph import attributes
    properties = [attributes.Hierarchy.Child, attributes.Hierarchy.NewParent,
                  attributes.OperationLogs.OperationID]
    row = cg.client.read_node(np.uint64(root), properties=properties)
    stamps = [cell.timestamp for versions in row.values() for cell in versions]
    return _time_text(max(stamps)) if stamps else None


def _volume(data_dir):
    from cloudvolume import CloudVolume
    return CloudVolume("file://" + str(Path(data_dir) / "atomic"),
                       progress=False, cache=False, parallel=1, fill_missing=False)


def _read_xyz(volume, lo, hi):
    return np.asarray(volume[tuple(slice(int(a), int(b)) for a, b in zip(lo, hi))][..., 0])


def _current_pairs(pairs, retired):
    pairs = np.asarray(pairs, np.uint64).reshape(-1, 2)
    if not len(pairs):
        return pairs
    # np.isin's sort path concatenates its inputs. A Python list of IDs becomes
    # int64; concatenating that with uint64 promotes both to float64 and rounds
    # PCG IDs above 2**53, falsely retiring adjacent current atoms.
    retired_ids = np.asarray(list(retired), dtype=np.uint64)
    return pairs[~np.isin(pairs, retired_ids).any(axis=1)]


def _validate_cross_edges(cg, nodes, retired):
    """Check the current, reciprocal leaf endpoints after retirement."""
    nodes = np.asarray(sorted(map(int, nodes)), np.uint64)
    cross = cg.get_atomic_cross_edges(nodes, raw_only=True)
    owners = {int(atom): int(node) for node in nodes for atom in cg.get_children(node)}
    directed = {}
    for node in nodes:
        pairs = {tuple(map(int, pair)) for values in cross.get(node, {}).values()
                 for pair in np.asarray(values, np.uint64).reshape(-1, 2)}
        for a, b in pairs:
            if a in retired or b in retired:
                raise ValueError(f"Current CrossChunkEdge references a retired atom: {(a, b)}")
            if owners.get(a) != int(node):
                raise ValueError(f"CrossChunkEdge source {a} is not a current child of {node}")
        directed[int(node)] = pairs
    for node, pairs in directed.items():
        for a, b in pairs:
            neighbour = owners.get(b)
            if neighbour is not None and (b, a) not in directed[neighbour]:
                raise ValueError(f"Current CrossChunkEdge is not reciprocal: {(a, b)}")


def _logged_edges(cg, atom, root, tables):
    """Find provenance-only representative edges in this object's edit history.

    A contact merge logs the two *requested* representative atoms, while PCG
    may activate other contact edges between their objects. Its requested pair
    consequently need not occur in any stored or fake edge cell. Redo still
    reconstructs that pair, so refinement must retain its translation without
    adding a fictitious physical edge. Walk root ancestry, never all graph logs.
    """
    from pychunkedgraph.graph import attributes
    from supervoxel_history import translate_edges
    from supervoxel_split import Edge
    former = attributes.Hierarchy.FormerParent
    op_column = attributes.OperationLogs.OperationID
    queue, seen, operations = [int(root)], set(), set()
    while queue:
        batch = sorted(set(queue) - seen)
        if not batch:
            break
        seen.update(batch)
        queue = []
        rows = cg.client.read_nodes(node_ids=np.asarray(batch, np.uint64), properties=[former, op_column])
        for row in rows.values():
            for cell in row.get(former, ()):
                queue.extend(map(int, np.asarray(cell.value).ravel()))
            for cell in row.get(op_column, ()):
                operations.update(map(int, np.asarray(cell.value).ravel()))
    if not operations:
        return []
    # Filter logs using the atom's ancestor chain before translation. This both
    # bounds work and avoids consulting unrelated edge-map branches.
    ancestors = {int(atom)}
    parent = {int(row["new"]): int(row["old"]) for row in tables.lineage}
    cursor = int(atom)
    while cursor in parent:
        cursor = parent[cursor]
        ancestors.add(cursor)
    logs = cg.client.read_log_entries(np.asarray(sorted(operations), np.uint64))
    edges = {}
    for record in logs.values():
        for column in (attributes.OperationLogs.AddedEdge, attributes.OperationLogs.RemovedEdge):
            for pair in np.asarray(record.get(column, []), np.uint64).reshape(-1, 2):
                if not ancestors.intersection(map(int, pair)):
                    continue
                for a, b in translate_edges([pair], tables=tables):
                    a, b = int(a), int(b)
                    if int(atom) in (a, b):
                        edge = Edge(a, b, float(np.finfo(np.float32).tiny))
                        edges[(edge.a, edge.b)] = edge
    return list(edges.values())


def _discover(cg, atom, data_dir, tables):
    """Read the complete write footprint before locks, then again under locks.

    Stored files include inactive cut edges. Their endpoints discover the
    neighbour L2 rows even after a native split separates the touching atoms.
    Fake-edge endpoints may be in non-adjacent chunks and are included too.
    """
    from pychunkedgraph.graph import attributes
    from pychunkedgraph.graph.edges import EDGE_TYPES
    from pychunkedgraph.io.edges import get_chunk_edges
    from supervoxel_split import Edge
    atom = int(atom)
    if atom in tables.retired:
        raise PreconditionsChanged(f"atom {atom} was already refined")
    positions = np.searchsorted(tables.index[:, 0], np.uint64(atom))
    if positions == len(tables.index) or int(tables.index[positions, 0]) != atom:
        raise ValueError(f"Unknown atom {atom}")
    lo, hi = np.asarray(tables.bounds[positions], int).reshape(2, 3)
    volume = _volume(data_dir)
    shape = np.asarray(volume.volume_size, int)
    voxels = _read_xyz(volume, lo, hi).transpose(2, 1, 0).copy()
    if not np.any(voxels == atom):
        raise PreconditionsChanged(f"atom {atom} has no current voxels")
    context = np.zeros(tuple(np.asarray(voxels.shape) + 2), np.uint64)
    context_lo, context_hi = np.maximum(0, lo - 1), np.minimum(shape, hi + 1)
    clipped = _read_xyz(volume, context_lo, context_hi).transpose(2, 1, 0)
    offsets = (context_lo - (lo - 1))[::-1]
    context[tuple(slice(int(a), int(a + n)) for a, n in zip(offsets, clipped.shape))] = clipped
    chunk = np.asarray(cg.meta.graph_config.CHUNK_SIZE, int)
    coord = tuple(map(int, cg.get_chunk_coordinates(np.uint64(atom))))
    chunk_lo = np.asarray(coord) * chunk
    chunk_hi = np.minimum(chunk_lo + chunk, shape)
    chunk_voxels = _read_xyz(volume, chunk_lo, chunk_hi).transpose(2, 1, 0).copy()
    coordinates = {coord}
    for axis in range(3):
        for delta in (-1, 1):
            neighbour = np.asarray(coord).copy()
            neighbour[axis] += delta
            if np.all(neighbour >= 0) and np.all(neighbour * chunk < shape):
                coordinates.add(tuple(map(int, neighbour)))
    edge_files, fake_chunks, edges = {}, {}, {}
    processed = set()
    while coordinates - processed:
        at = min(coordinates - processed)
        processed.add(at)
        edge_file = Path(data_dir) / "edges" / ("edges_" + "_".join(map(str, at)) + ".proto.zst")
        if edge_file.exists():
            stored = get_chunk_edges("file://" + str(Path(data_dir) / "edges"), [np.asarray(at)])
            edge_files[at] = stored
            for kind in EDGE_TYPES:
                values = stored[kind]
                for a, b, affinity, area in zip(values.node_ids1, values.node_ids2, values.affinities, values.areas):
                    a, b = int(a), int(b)
                    if atom not in (a, b) or a in tables.retired or b in tables.retired:
                        continue
                    # Ingested edges use the native tiny-float default when no
                    # contact count was stored; treat that as absent area.
                    item = Edge(a, b, float(affinity), max(0, int(round(float(area)))))
                    key = tuple(sorted((a, b)))
                    if key in edges and edges[key].affinity != item.affinity:
                        raise ValueError(f"Conflicting stored affinities for edge {key}")
                    edges[key] = item
        # PCG stores fake edges on L2 *chunk* rows, although their endpoints
        # are layer-1 atomic IDs.
        cid = cg.get_chunk_id(layer=2, x=at[0], y=at[1], z=at[2])
        fake = cg.get_fake_edges(np.asarray([cid], np.uint64))
        pairs = _current_pairs(fake[cid].get_pairs(), tables.retired) if cid in fake else np.empty((0, 2), np.uint64)
        incident = pairs[np.any(pairs == atom, axis=1)]
        if len(incident):
            fake_chunks[int(cid)] = incident
            for a, b in incident:
                a, b = int(a), int(b)
                key = tuple(sorted((a, b)))
                edges.setdefault(key, Edge(a, b, float(np.finfo(np.float32).tiny)))
                neighbour = b if a == atom else a
                coordinates.add(tuple(map(int, cg.get_chunk_coordinates(np.uint64(neighbour)))))
    atom_root = int(cg.get_root(np.uint64(atom)))
    aliases = [edge for edge in _logged_edges(cg, atom, atom_root, tables)
               if (edge.a, edge.b) not in edges]
    endpoints = sorted({atom} | {end for edge in [*edges.values(), *aliases] for end in (edge.a, edge.b)})
    parents = cg.get_parents(np.asarray(endpoints, np.uint64))
    parent_map = dict(zip(endpoints, map(int, parents)))
    L = parent_map[atom]
    candidates = np.asarray(sorted(set(parent_map.values())), np.uint64)
    cross = cg.get_atomic_cross_edges(candidates)
    # Native splits can remove inactive cross edges from both current L2 cells
    # while retaining them in the chunk edge files. Repointing those stored
    # edges still affects both endpoints' future edits: include their current
    # L2s and roots even when there is no active CrossChunkEdge cell to rewrite.
    # Provenance-only log aliases do not add physical writes or extra roots.
    affected = {L} | {parent_map[end] for edge in edges.values() for end in (edge.a, edge.b)}
    for node, layer_edges in cross.items():
        if any(np.any(np.asarray(pairs) == atom) for pairs in layer_edges.values()):
            affected.add(int(node))
    # Fake cells are chunk-wide append-only cells. Include roots of both
    # endpoints whose replacement pairs are added, not unrelated chunk atoms.
    for pairs in fake_chunks.values():
        affected.update(parent_map[int(end)] for end in pairs.ravel())
    l2 = {}
    for node in sorted(affected):
        children = cg.get_children(np.uint64(node))
        if not len(children) or not np.all(cg.get_parents(children) == node):
            raise PreconditionsChanged(f"affected level-2 node {node} is no longer current")
        root = int(cg.get_root(np.uint64(node)))
        if not bool(cg.is_latest_roots(np.asarray([root], np.uint64))[0]):
            raise PreconditionsChanged(f"affected root {root} is no longer current")
        l2[str(node)] = {"root": root, "children": list(map(int, children))}
    if atom not in l2[str(L)]["children"]:
        raise PreconditionsChanged("refined atom is no longer a child of its level-2 node")
    roots = sorted({entry["root"] for entry in l2.values()})
    timestamps = {str(root): _root_timestamp(cg, root) for root in roots}
    return dict(atom=atom, position=int(positions), lo=lo, hi=hi, voxels=voxels,
                context=context, coord=coord, chunk_lo=chunk_lo, chunk_hi=chunk_hi,
                chunk_voxels=chunk_voxels, edge_files=edge_files, fake_chunks=fake_chunks,
                edges=list(edges.values()), alias_edges=sorted(aliases), cross=cross, l2=l2, parent=L,
                root=l2[str(L)]["root"], roots=roots, root_timestamps=timestamps,
                shape=shape, volume_info=volume.info)


def _precondition_equal(first, second):
    return all(first.get(key) == second.get(key)
               for key in ("roots", "root_timestamps", "l2", "parent", "root", "alias_edges"))


def validate_preconditions(cg, precondition, *, publishing=False):
    """Check *all* affected roots and L2 rows, including other-root neighbours."""
    roots = precondition["roots"]
    if not np.all(cg.is_latest_roots(np.asarray(roots, np.uint64))):
        raise PreconditionsChanged("an affected root is no longer current")
    for root in roots:
        if _root_timestamp(cg, root) != precondition["root_timestamps"][str(root)]:
            raise PreconditionsChanged(f"affected root {root} changed operation timestamp")
    for node_text, expected in precondition["l2"].items():
        node = np.uint64(node_text)
        if int(cg.get_root(node)) != expected["root"]:
            raise PreconditionsChanged(f"affected level-2 node {node} changed root")
        actual = set(map(int, cg.get_children(node)))
        previous = set(expected["children"])
        if publishing and int(node) == int(precondition["parent"]):
            new = set(precondition["new_atoms"])
            allowed = (previous, previous | new,
                       (previous - {int(precondition["old_atom"])}) | new)
            if actual not in allowed:
                raise PreconditionsChanged("refined level-2 children no longer match replay payload")
        elif actual != previous:
            raise PreconditionsChanged(f"affected level-2 node {node} changed children")
        # Every currently listed child must still belong to this L2 row, not a
        # successor created by a native edit while a lock was expired.
        if actual and not np.all(cg.get_parents(np.asarray(sorted(actual), np.uint64)) == node):
            raise PreconditionsChanged(f"affected level-2 node {node} is obsolete")


def _compute_pieces(snapshot, request, grid_xyz, spacing):
    from supervoxel_split import grid_pieces, seeded_pieces
    mask = snapshot["voxels"] == snapshot["atom"]
    pieces, count = grid_pieces(mask, snapshot["lo"], grid_xyz)
    sources = [tuple(map(int, p)) for p in request.get("source_vox", ())]
    sinks = [tuple(map(int, p)) for p in request.get("sink_vox", ())]
    for seed in sources + sinks:
        if len(seed) != 3 or any(v < 0 or v >= size for v, size in zip(seed, mask.shape)) or not mask[seed]:
            raise ValueError("Refinement seed lies outside the current atom")
    if set(sources) & set(sinks):
        raise ValueError("A refinement voxel cannot belong to both seed groups")
    shared = {int(pieces[p]) for p in sources} & {int(pieces[p]) for p in sinks}
    for piece in sorted(shared):
        selected = pieces == piece
        result, n, _ = seeded_pieces(selected, [p for p in sources if pieces[p] == piece],
                                     [p for p in sinks if pieces[p] == piece], spacing)
        # Keep all labels dense while replacing just this shared grid piece.
        pieces[selected] = result[selected] + count
        count += n
    labels = np.unique(pieces[mask])
    lookup = np.zeros(int(pieces.max()) + 1, np.int32)
    lookup[labels] = np.arange(1, len(labels) + 1)
    pieces = lookup[pieces]
    assert np.array_equal(pieces > 0, mask)
    return pieces, len(labels)


def _cell(row, name, value, timestamp, *, index=None, fake=False):
    value = np.asarray(value, np.uint64)
    return {"row": int(row), "attribute": name, "index": index, "fake": fake,
            "timestamp": timestamp.isoformat(), "value": value.tolist(),
            "shape": list(value.shape), "sha256": hashlib.sha256(value.tobytes()).hexdigest()}


def _attribute(cell):
    from pychunkedgraph.graph import attributes
    if cell["attribute"] in ("Parent", "Child"):
        return getattr(attributes.Hierarchy, cell["attribute"])
    if cell["attribute"] == "CrossChunkEdge":
        return attributes.Connectivity.CrossChunkEdge[cell["index"]]
    if cell["attribute"] == "FakeEdges":
        return attributes.Connectivity.FakeEdges
    raise ValueError("Unexpected refinement cell attribute")


def _stage_file(journal_dir, source, destination, step):
    return {"source": str(Path(source).relative_to(journal_dir)),
            "destination": str(destination), "sha256": _sha(source), "step": step}


def _fsync_tree(path):
    for source in Path(path).rglob("*"):
        if source.is_file():
            with source.open("rb") as stream:
                os.fsync(stream.fileno())
    for source in sorted(Path(path).rglob("*"), reverse=True):
        if source.is_dir():
            _fsync_directory(source)
    _fsync_directory(path)


def _stage(cg, snapshot, pieces, new, tables, operation, reason, grid_xyz, data_dir):
    from cloudvolume import CloudVolume
    from pychunkedgraph.graph.edges import Edges, EDGE_TYPES
    from pychunkedgraph.io.edges import put_chunk_edges
    from supervoxel_history import stage_generation, LINEAGE_DTYPE, EDGE_MAP_DTYPE
    from supervoxel_split import neighbour_contacts, piece_contacts, rewrite_edges
    path = Path(data_dir) / "supervoxel_splits" / "journal" / str(operation)
    path.mkdir(parents=True, exist_ok=False)
    old, L, root = snapshot["atom"], snapshot["parent"], snapshot["root"]
    new = np.asarray(new, np.uint64)
    now_ms = int(time.time() * 1000)
    if len(tables.lineage):
        now_ms = max(now_ms, int(tables.lineage["t1_ms"].max()) + 1)
    t1 = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    t2 = t1 + timedelta(milliseconds=1)
    piece_ids = {piece: int(atom) for piece, atom in enumerate(new, 1)}
    points, rows, boxes = {}, [], []
    source_label = int(tables.index[snapshot["position"], 1])
    for piece, atom in piece_ids.items():
        pts = np.argwhere(pieces == piece)
        points[piece] = pts.mean(axis=0)
        xyz = pts[:, ::-1] + snapshot["lo"]
        # Store a known inside point, not an arithmetic centroid outside a U.
        rows.append([atom, source_label, *map(int, xyz[len(xyz) // 2])])
        boxes.append([*map(int, xyz.min(axis=0)), *map(int, xyz.max(axis=0) + 1)])
    contact = neighbour_contacts(pieces, snapshot["context"])
    aliases = snapshot.get("alias_edges", [])
    neighbours = {end for edge in [*snapshot["edges"], *aliases] for end in (edge.a, edge.b)} - {old}
    index_lookup = {int(row[0]): row for row in tables.index}
    neighbour_points = {atom: (np.asarray(index_lookup[atom][2:5], float) - snapshot["lo"])[::-1]
                        for atom in neighbours}
    rewritten, provenance = rewrite_edges(old, snapshot["edges"], piece_ids, contact,
                                          points, neighbour_points,
                                          internal_contacts=piece_contacts(pieces),
                                          ref_area=float(min(grid_xyz) ** 2))
    # Representative pairs from merge logs are metadata only. Keep their edge
    # map while deliberately discarding the synthetic edge list, so they cannot
    # add connectivity to storage or change native cutting weights.
    if aliases:
        _, alias_provenance = rewrite_edges(old, aliases, piece_ids, contact, points, neighbour_points)
        provenance.update(alias_provenance)
    by_pair = {(edge.a, edge.b): edge for edge in rewritten}
    lineage = np.asarray([(int(atom), old, now_ms, str(operation)) for atom in new], dtype=LINEAGE_DTYPE)
    edge_map = np.asarray([(a, b, c, d, str(operation), now_ms)
                           for (a, b), replacements in sorted(provenance.items())
                           for c, d in replacements], dtype=EDGE_MAP_DTYPE)
    generation = stage_generation(
        data_dir, new_index_rows=np.asarray(rows, np.uint64),
        new_bounds_rows=np.asarray(boxes, tables.bounds.dtype),
        new_initial_roots=np.full(len(new), tables.initial_roots[snapshot["position"]], np.uint64),
        new_lineage_rows=lineage, new_edge_map_rows=edge_map,
        destination=path / "generation",
    )
    files = []
    # Store all expected table hashes alongside every other destination hash.
    generation_hashes = {p.name: _sha(p) for p in Path(generation).iterdir() if p.is_file()}
    payload = path / "payload"
    payload.mkdir()
    np.save(payload / "before.npy", snapshot["chunk_voxels"])
    chunk_post = snapshot["chunk_voxels"].copy()
    local_lo = (snapshot["lo"] - snapshot["chunk_lo"])[::-1]
    slices = tuple(slice(int(a), int(a + n)) for a, n in zip(local_lo, pieces.shape))
    lut = np.r_[np.uint64(0), new]
    old_box = chunk_post[slices]
    old_box[pieces > 0] = lut[pieces[pieces > 0]]
    np.save(payload / "after.npy", chunk_post)
    chunk_name = "_".join(map(str, snapshot["coord"])) + ".npy"
    files.append(_stage_file(path, payload / "after.npy", Path(data_dir) / "chunks" / chunk_name, "7e"))
    staged_atomic = payload / "atomic"
    staged_volume = CloudVolume("file://" + str(staged_atomic), info=snapshot["volume_info"],
                               compress=True, progress=False, cache=False, parallel=1,
                               fill_missing=False)
    staged_volume.commit_info()
    chunk_slices = tuple(slice(int(a), int(b)) for a, b in zip(snapshot["chunk_lo"], snapshot["chunk_hi"]))
    staged_volume[chunk_slices] = chunk_post.transpose(2, 1, 0)[..., None]
    for source in staged_atomic.rglob("*"):
        if source.is_file() and source.name != "info":
            files.append(_stage_file(path, source, Path(data_dir) / "atomic" / source.relative_to(staged_atomic), "7e"))
    staged_edges = payload / "edges"
    staged_edges.mkdir()
    new_set = set(map(int, new))
    for coordinate, original in snapshot["edge_files"].items():
        modified = {kind: original[kind] for kind in EDGE_TYPES}
        touched = False
        for kind in EDGE_TYPES:
            values = original[kind]
            additions = {}
            for a, b in zip(values.node_ids1, values.node_ids2):
                key = tuple(sorted((int(a), int(b))))
                for pair in provenance.get(key, ()):
                    edge = by_pair[pair]
                    c, d = pair
                    # PCG between/cross arrays store this chunk's endpoint first.
                    if kind != EDGE_TYPES.in_chunk:
                        if tuple(map(int, cg.get_chunk_coordinates(np.uint64(c)))) != coordinate:
                            c, d = d, c
                    additions[(c, d)] = (edge.affinity, edge.area)
            if coordinate == snapshot["coord"] and kind == EDGE_TYPES.in_chunk:
                for edge in rewritten:
                    if edge.a in new_set and edge.b in new_set:
                        additions[(edge.a, edge.b)] = (edge.affinity, edge.area)
            if additions:
                pairs = np.asarray(list(additions), np.uint64)
                weights = np.asarray(list(additions.values()), np.float32)
                modified[kind] = values + Edges(pairs[:, 0], pairs[:, 1],
                                                affinities=weights[:, 0], areas=weights[:, 1])
                touched = True
        if touched:
            put_chunk_edges("file://" + str(staged_edges), np.asarray(coordinate), modified, 3)
    for source in staged_edges.iterdir():
        files.append(_stage_file(path, source, Path(data_dir) / "edges" / source.name, "7c"))
    children = snapshot["l2"][str(L)]["children"]
    cells = {"7b": [_cell(atom, "Parent", L, t1) for atom in new],
             "7d": [_cell(L, "Child", sorted(set(children) | new_set), t1)],
             "7f": [_cell(L, "Child", sorted((set(children) - {old}) | new_set), t2)]}
    for node_text in snapshot["l2"]:
        node = int(node_text)
        for layer, pairs in snapshot["cross"].get(np.uint64(node), {}).items():
            pairs = _current_pairs(pairs, tables.retired)
            if not np.any(pairs == old):
                continue
            replacement = []
            for a, b in pairs:
                a, b = int(a), int(b)
                if old not in (a, b):
                    replacement.append((a, b))
                    continue
                mapped = provenance.get(tuple(sorted((a, b))))
                if not mapped:
                    raise ValueError(f"No edge provenance for affected cross edge {(a, b)}")
                for c, d in mapped:
                    piece = c if c in new_set else d
                    neighbour = b if a == old else a
                    replacement.append((piece, neighbour) if a == old else (neighbour, piece))
            cells["7f"].append(_cell(node, "CrossChunkEdge", np.unique(replacement, axis=0).reshape(-1, 2),
                                      t2, index=int(layer)))
    for chunk_id, pairs in snapshot["fake_chunks"].items():
        replacements = sorted({pair for a, b in pairs
                               for pair in provenance[tuple(sorted((int(a), int(b))))]})
        if replacements:
            # Native fake-edge reads concatenate historical versions. Append only
            # replacements; existing cells and retired edges remain inert.
            cells["7f"].append(_cell(chunk_id, "FakeEdges", replacements, t2, fake=True))
    data = {"version": 1, "state": "prepared", "operation_id": str(operation),
            "old_atom": old, "new_atoms": list(map(int, new)), "parent": L, "root": root,
            "roots": snapshot["roots"], "l2": snapshot["l2"],
            "root_timestamps": snapshot["root_timestamps"], "reason": reason,
            "representative_edge_aliases": [[edge.a, edge.b] for edge in aliases],
            "t1": t1.isoformat(), "t2": t2.isoformat(), "markers": [], "cells": cells,
            "files": files, "generation": str(Path(generation).relative_to(path)),
            "generation_hashes": generation_hashes, "old_count": int(np.count_nonzero(pieces)),
            "chunk_lo": snapshot["chunk_lo"].tolist(), "chunk_hi": snapshot["chunk_hi"].tolist(),
            "replay_count": 0}
    _fsync_tree(path)
    journal = Journal(path, data)
    journal.save()
    return journal


class PublicationStore:
    def __init__(self, cg, journal, data_dir):
        self.cg, self.journal, self.data_dir = cg, journal, Path(data_dir)

    def _file(self, item):
        source = self.journal.path / item["source"]
        destination = Path(item["destination"])
        # Journals are local trusted payloads, but reject destinations escaping
        # this deployment to make repair scope explicit.
        if not destination.resolve().is_relative_to(self.data_dir.resolve()):
            raise RefinementError("Refinement payload destination escapes deployment")
        if destination.exists() and _sha(destination) == item["sha256"]:
            return
        if _sha(source) != item["sha256"]:
            raise RefinementError(f"Refinement payload hash mismatch: {source}")
        atomic_write(destination, source.read_bytes())
        if _sha(destination) != item["sha256"]:
            raise RefinementError(f"Refinement destination readback mismatch: {destination}")

    def _cell_matches(self, item):
        attribute = _attribute(item)
        timestamp = datetime.fromisoformat(item["timestamp"])
        value = np.asarray(item["value"], np.uint64).reshape(item["shape"])
        if hashlib.sha256(value.tobytes()).hexdigest() != item["sha256"]:
            raise RefinementError("Refinement cell payload hash mismatch")
        row = np.uint64(item["row"])
        existing = self.cg.client.read_node(row, properties=attribute, fake_edges=item["fake"],
                                            end_time=timestamp, end_time_inclusive=True)
        return any(cell.timestamp == timestamp and np.array_equal(cell.value, value) for cell in existing)

    def _cell(self, item):
        from pychunkedgraph.graph.utils.serializers import serialize_uint64
        if self._cell_matches(item):
            return
        attribute = _attribute(item)
        timestamp = datetime.fromisoformat(item["timestamp"])
        value = np.asarray(item["value"], np.uint64).reshape(item["shape"])
        row = np.uint64(item["row"])
        write_value = np.uint64(value) if item["attribute"] == "Parent" else value
        mutation = self.cg.client.mutate_row(serialize_uint64(row, fake_edges=item["fake"]),
                                             {attribute: write_value}, timestamp)
        self.cg.client.write([mutation])

    def apply_step(self, step):
        if step == "7a":
            from supervoxel_history import publish_generation
            source = self.journal.path / self.journal.data["generation"]
            for name, expected in self.journal.data["generation_hashes"].items():
                if _sha(source / name) != expected:
                    raise RefinementError("Staged atom-table generation hash mismatch")
            publish_generation(source, self.data_dir, symlink_created=lambda temporary, link:
                fixture_fault("7a-symlink", self.data_dir,
                              operation_id=self.journal.data["operation_id"],
                              temporary_link=str(temporary), link=str(link)))
        for item in self.journal.data["files"]:
            if item["step"] == step:
                self._file(item)
        for item in self.journal.data["cells"].get(step, ()):
            self._cell(item)

    def verify(self):
        from supervoxel_history import load_atom_tables
        data = self.journal.data
        fixture_fault("verify", self.data_dir, operation_id=data["operation_id"])
        tables = load_atom_tables(self.data_dir)
        new, old = np.asarray(data["new_atoms"], np.uint64), int(data["old_atom"])
        if not np.all(self.cg.get_roots(new) == data["root"]):
            raise ValueError("New atoms do not resolve to the original root")
        leaves = self.cg.get_subgraph_leaves(np.uint64(data["root"]))
        if old in leaves or not np.all(np.isin(new, leaves)):
            raise ValueError("Retirement did not preserve correct leaf membership")
        before = np.load(self.journal.path / "payload" / "before.npy")
        after = _read_xyz(_volume(self.data_dir), data["chunk_lo"], data["chunk_hi"]).transpose(2, 1, 0)
        old_mask = before == old
        if not np.array_equal(before[~old_mask], after[~old_mask]):
            raise ValueError("Refinement changed voxels outside the old atom")
        if np.count_nonzero(old_mask) != data["old_count"] or not np.array_equal(np.isin(after, new), old_mask):
            raise ValueError("Refinement pieces do not exactly partition the old atom")
        if np.any(after == old):
            raise ValueError("Retired atom still has voxels")
        if not np.all(np.isin(new, tables.index[:, 0])) or not np.all(np.isin(new, tables.lineage["new"])):
            raise ValueError("New atom table or lineage rows are missing")
        for item in data["files"]:
            if _sha(item["destination"]) != item["sha256"]:
                raise ValueError("Published file differs from its staged payload")
        for items in data["cells"].values():
            for item in items:
                if not self._cell_matches(item):
                    raise ValueError("Published Bigtable cell differs from its staged payload")
        _validate_cross_edges(self.cg, data["l2"], tables.retired)
        validate_preconditions(self.cg, data, publishing=True)


def _recover_locked(cg, data_dir, repair=False):
    from supervoxel_history import cleanup_temporary_symlinks
    cleanup_temporary_symlinks(data_dir)
    results = []
    for journal in journals(data_dir):
        state = journal.state
        if state in ("done", "aborted"):
            continue
        if state in ("prepared", "fenced"):
            try:
                journal.abort()
            finally:
                _release(cg.client, journal.data["roots"], np.uint64(journal.data["operation_id"]))
            results.append({"operation_id": journal.data["operation_id"], "state": "aborted"})
            continue
        if state == "verified":
            _finalize(journal, data_dir)
            _release(cg.client, journal.data["roots"], np.uint64(journal.data["operation_id"]))
            results.append({"operation_id": journal.data["operation_id"], "state": "done"})
            continue
        if state == "verify_failed" and not repair:
            results.append({"operation_id": journal.data["operation_id"], "state": state})
            continue
        # Recovery owns the exclusive mutation flock and the journal fence, so
        # no live refinement can still be using this operation ID. Reuse its
        # retained locks after a process crash instead of waiting 180 seconds
        # for expiry. Missing locks are acquired through the same native path.
        operation = np.uint64(journal.data["operation_id"])
        retained = [root for root in journal.data["roots"]
                    if cg.client.get_lock_timestamp(np.uint64(root), operation) is not None]
        if retained:
            _renew(cg.client, retained, operation)
        missing = sorted(set(journal.data["roots"]) - set(retained))
        locked = list(retained)
        if missing:
            acquired, newly_locked = _unwrapped_lock(cg.client, missing, operation)
            if not acquired:
                _release(cg.client, retained, operation)
                raise RefinementError(f"Cannot lock every root for recovery of {journal.data['operation_id']}")
            locked.extend(map(int, newly_locked))
        try:
            if set(map(int, locked)) != set(journal.data["roots"]):
                raise PreconditionsChanged("Recovery lock set changed")
            _renew(cg.client, locked, operation)
            validate_preconditions(cg, journal.data, publishing=True)
            journal.data["replay_count"] += 1
            journal.save()
            publish_journal(journal, PublicationStore(cg, journal, data_dir),
                            renew=lambda: _renew(cg.client, locked, operation), data_dir=data_dir)
            results.append({"operation_id": journal.data["operation_id"], "state": journal.state})
        finally:
            _release(cg.client, locked, operation)
    return results


def recover(cg, data_dir="/data", repair=False):
    install_lock_ownership_fix()
    with mutation_lock(data_dir):
        return _recover_locked(cg, data_dir, repair)


def recover_if_idle(graph_factory, data_dir="/data"):
    """Let an odd-epoch reader recover an abandoned publisher without waiting.

    A live publisher owns the flock, so readers only wait for its epoch. If an
    exception or interrupted worker released that lock, the first reader can
    replay the journal. Failed reader attempts have a per-operation cooldown,
    shared by workers through a small file under the mutation flock. Explicit
    repair and refinement bypass it. Construct the graph only after checking
    the epoch and cooldown, avoiding graph work for live or failed publishers.
    """
    from supervoxel_history import read_epoch
    with mutation_lock(data_dir, blocking=False) as acquired:
        if not acquired:
            return False
        epoch = read_epoch(data_dir)
        if not epoch % 2:
            return False
        failure_path = Path(data_dir) / "supervoxel_splits" / "reader-recovery-failure.json"
        failure_key = str(failure_path.resolve())
        failure = _reader_recovery_failures.get(failure_key)
        try:
            shared_failure = json.loads(failure_path.read_text())
        except FileNotFoundError:
            shared_failure = None
        except (OSError, ValueError) as error:
            if failure is None or failure["epoch"] != epoch:
                raise RefinementError("Cannot read supervoxel recovery cooldown") from error
            shared_failure = None
        if (shared_failure is not None and shared_failure["epoch"] == epoch
                and (failure is None or failure["epoch"] != epoch
                     or shared_failure["failed_at"] > failure["failed_at"])):
            failure = shared_failure
        if (failure is not None and failure["epoch"] == epoch
                and 0 <= time.time() - failure["failed_at"] < READER_RECOVERY_COOLDOWN_SECONDS):
            operations = ", ".join(failure["operations"]) or "unknown"
            raise RefinementError(
                f"supervoxel publication awaits automatic recovery cooldown for operation {operations}")
        operations = []
        try:
            operations = [str(j.data["operation_id"]) for j in journals(data_dir)
                          if j.state not in ("done", "aborted")]
            install_lock_ownership_fix()
            _recover_locked(graph_factory(), data_dir)
        except Exception as error:
            failed_at = time.time()
            failure = {"epoch": epoch, "failed_at": failed_at,
                       "operations": {operation: failed_at for operation in operations}}
            # Preserve a same-worker cooldown even if failing storage also
            # prevents persisting the cross-worker recovery record.
            _reader_recovery_failures[failure_key] = failure
            try:
                atomic_write(failure_path, _json_bytes(failure))
            except Exception as recording_error:
                error.add_note(f"Could not persist reader recovery cooldown: {recording_error!r}")
            raise RefinementError(
                f"supervoxel publication awaits automatic recovery: {error}"
            ) from error
        _reader_recovery_failures.pop(failure_key, None)
        return True


def _allocate(cg, snapshot, tables, count):
    chunk = cg.get_chunk_id(node_id=np.uint64(snapshot["atom"]))
    same_chunk = cg.get_chunk_ids_from_node_ids(tables.index[:, 0]) == chunk
    maximum = int(tables.index[same_chunk, 0].max())
    current = int(cg.client.get_max_node_id(chunk))
    if max(current, maximum) - int(chunk) + count > int(cg.get_segment_id_limit(chunk)):
        raise ValueError("Refinement would exhaust this graph chunk's atomic ID space")
    if current < maximum:
        cg.client.create_node_ids(chunk, maximum - current)
    new = cg.client.create_node_ids(chunk, count)
    if (len(np.unique(new)) != count or np.any(np.isin(new, tables.index[:, 0]))
            or not np.all(cg.get_chunk_ids_from_node_ids(new) == chunk)):
        raise ValueError("PCG allocated duplicate atom IDs")
    return new


def refine(cg, requests, *, grid_xyz=(16, 16, 16), reason="same_supervoxel", data_dir="/data", max_seconds=None):
    from supervoxel_history import load_atom_tables
    install_lock_ownership_fix()
    if cg.cache is not None:
        raise ValueError("Refinement requires a fresh ChunkedGraph outside a native edit cache")
    started = time.monotonic()
    def check_budget():
        if max_seconds is not None and time.monotonic() >= started + max_seconds:
            from supervoxel_paint import PaintError
            raise PaintError('deadline', 'The paint deadline expired before refinement publication.')
    result = RefineResult()
    with mutation_lock(data_dir):
        check_budget()
        _recover_locked(cg, data_dir)
        # A failed verification retains exact staged chunk/table payloads for
        # explicit repair. A new refinement on another root can share those
        # files and would invalidate the repair preimage, so defer all new
        # refinements until repair completes. Ordinary unfenced native edits
        # remain available; they do not rewrite atomic voxels or table files.
        if any(journal.state == "verify_failed" for journal in journals(data_dir)):
            raise RefinementError("supervoxel refinement awaiting repair; new refinements are deferred")
        for request in requests:
            request = request if isinstance(request, dict) else {"atom": int(request)}
            atom = int(request["atom"])
            for attempt in range(2):
                check_budget()
                tables = load_atom_tables(data_dir)
                if atom in tables.retired:
                    break
                try:
                    before_lock = _discover(cg, atom, data_dir, tables)
                except PreconditionsChanged as error:
                    # Native edits do not take the refinement file lock. A
                    # neighbouring cut can retire an L2 between this probe's
                    # parent and child reads, before any root lock is held.
                    # Spend the same bounded retry without allocating an edit
                    # id, acquiring locks or staging a journal from that view.
                    if attempt == 1:
                        raise RefinementError("root changed during refinement; retry") from error
                    continue
                operation = cg.client.create_operation_id()
                acquired, locked = _unwrapped_lock(cg.client, before_lock["roots"], operation)
                if not acquired:
                    raise RefinementError("Cannot acquire affected root locks for supervoxel refinement")
                journal = None
                try:
                    if set(map(int, locked)) != set(before_lock["roots"]):
                        raise PreconditionsChanged("Affected lock set changed")
                    snapshot = _discover(cg, atom, data_dir, tables)
                    if not _precondition_equal(before_lock, snapshot):
                        raise PreconditionsChanged("Affected level-2 nodes or roots changed before locking")
                    if set(snapshot["roots"]) & fenced_roots(data_dir):
                        raise RefinementError("supervoxel refinement in progress or awaiting repair")
                    _renew(cg.client, locked, operation)
                    check_budget()
                    pieces, count = _compute_pieces(snapshot, request, grid_xyz, cg.segmentation_resolution)
                    _renew(cg.client, locked, operation)
                    if count <= 1:
                        break
                    new = _allocate(cg, snapshot, tables, count)
                    _renew(cg.client, locked, operation)
                    journal = _stage(cg, snapshot, pieces, new, tables, operation, reason, grid_xyz, data_dir)
                    fixture_fault("prepared", data_dir, operation_id=str(operation))
                    fixture_fault("staging-expiry", data_dir, operation_id=str(operation))
                    journal.transition("fenced")
                    fixture_fault("fenced", data_dir, operation_id=str(operation))
                    _renew(cg.client, locked, operation)
                    fixture_fault("validate-preconditions", data_dir, operation_id=str(operation))
                    validate_preconditions(cg, journal.data)
                    # Discover the complete neighbour write footprint again:
                    # checking only the old atom's root misses P1's race.
                    fresh = _discover(cg, atom, data_dir, tables)
                    if not _precondition_equal(snapshot, fresh):
                        raise PreconditionsChanged("Affected root set changed during staging")
                    check_budget()
                    journal.transition("publishing")
                    publish_journal(journal, PublicationStore(cg, journal, data_dir),
                                    renew=lambda: _renew(cg.client, locked, operation), data_dir=data_dir)
                    result.count += 1
                    result.operation_ids.append(str(operation))
                    result.new_ids[atom] = list(map(int, new))
                    break
                except BaseException as error:
                    # Nothing visible has changed before publishing. Any failure,
                    # including process-level exceptions, must remove this fence.
                    if journal is not None and journal.state in ("prepared", "fenced"):
                        try:
                            journal.abort()
                        except Exception as abort_error:
                            error.refinement_abort_error = abort_error
                            error.add_note(f"Refinement abort cleanup failed: {abort_error!r}")
                            if not getattr(journal, "abort_fence_lifted", False):
                                # A persistent failure of both cleanup actions
                                # must stay protected and must not retry staging.
                                raise error from abort_error
                    if (journal is not None and journal.state in ("publishing", "published", "verified")
                            and isinstance(error, Exception)):
                        # Stay inside the mutation flock for one immediate replay.
                        # If replay also fails, odd-epoch readers retry through
                        # recover_if_idle; the original error never exposes a
                        # partially published generation as a successful edit.
                        try:
                            _recover_locked(cg, data_dir)
                        except Exception as recovery_error:
                            raise RefinementError(
                                f"Refinement operation {operation} awaits automatic recovery; "
                                f"publication failed ({error}); replay failed ({recovery_error})"
                            ) from recovery_error
                        raise RefinementError(
                            f"Refinement operation {operation} recovered after publication error: {error}"
                        ) from error
                    if isinstance(error, (PreconditionsChanged, LockLost)):
                        if attempt == 1:
                            raise RefinementError("root changed during refinement; retry") from error
                        fixture_fault("retry-prepared", data_dir, operation_id=str(operation))
                    else:
                        raise
                finally:
                    _release(cg.client, locked, operation)
    result.elapsed_seconds = time.monotonic() - started
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--graph-id")
    args = parser.parse_args()
    from pychunkedgraph.graph.chunkedgraph import ChunkedGraph
    graph_id = args.graph_id
    if not graph_id:
        graph_id = json.loads(Path("/workspace/deployment.json").read_text())["graph_id"]
    result = recover(ChunkedGraph(graph_id=graph_id), data_dir=args.data_dir, repair=args.repair)
    print(json.dumps({"recovery": result}, indent=2))


if __name__ == "__main__":
    main()
