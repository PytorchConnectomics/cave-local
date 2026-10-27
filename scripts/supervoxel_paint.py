"""Bounded, deterministic paint geometry and cut checks, without graph access.

Paint and voxel coordinates use XYZ; label crops use ZYX. Bounding boxes are
half-open voxel XYZ. Supervoxel identifiers remain integers throughout.
"""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import time

import numpy as np


PAINT_LIMITS = {
    "enabled": False,
    "max_radius_nm": 500,
    "max_stroke_points": 256,
    "max_paint_voxels": 200000,
    "max_box_voxels": [512, 512, 128],
    "max_cut_distance_nm": 1000,
    "max_painted_atoms": 256,
    "max_removed_edges": 2000,
    "max_contact_voxels": 2000000,
    "max_seconds": 60,
    "max_request_bytes": 262144,
    "max_strokes": 128,
    "max_total_points": 2048,
    "max_raster_work": 4000000,
}
RASTER_BATCH_VOXELS = 65536


class PaintError(ValueError):
    def __init__(self, kind, message, *, locations_nm=None, supervoxels=None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        locations = () if locations_nm is None else locations_nm
        atoms = () if supervoxels is None else supervoxels
        self.locations_nm = [[float(v) for v in point] for point in list(locations)[:20]]
        self.supervoxels = [int(atom) for atom in atoms]

    def as_dict(self):
        return {"kind": self.kind, "locations_nm": self.locations_nm,
                "supervoxels": self.supervoxels}


@dataclass(frozen=True)
class Stroke:
    points_nm: np.ndarray
    radius_nm: float


@dataclass(frozen=True)
class Paint:
    sources: tuple
    sinks: tuple
    original: dict


def check_deadline(deadline):
    """The caller supplies one absolute time.monotonic deadline per request."""
    if deadline is not None and time.monotonic() >= deadline:
        raise PaintError("deadline", "Paint request exceeded its time limit")


def _limits(limits):
    return {**PAINT_LIMITS, **(limits or {})}


def _number(value):
    try:
        return (isinstance(value, (int, float, np.integer, np.floating))
                and not isinstance(value, (bool, np.bool_)) and math.isfinite(value))
    except OverflowError:
        return False


def parse_paint(body, limits=None):
    """Validate a split body (or raw JSON), including bounded stroke counts.

The HTTP adapter checks raw body length before its JSON decoder. Raw JSON is
also supported here so callers can preserve that ordering without Flask.
"""
    limits = _limits(limits)
    if isinstance(body, (bytes, bytearray, str)):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        if len(raw) > limits["max_request_bytes"]:
            raise PaintError("limit", "Paint request body exceeds max_request_bytes")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise PaintError("limit", "Paint request must be valid JSON") from exc
    if not isinstance(body, dict) or not isinstance(body.get("paint"), dict):
        raise PaintError("limit", "Split request must contain a paint object")
    for label in ("sources", "sinks"):
        if label in body and body[label] != []:
            raise PaintError("limit", "Paint cannot be mixed with point sources or sinks")
    paint = body["paint"]
    if type(paint.get("version")) is not int or paint["version"] != 1:
        raise PaintError("limit", "Paint version must be 1")
    if set(paint) - {"version", "sources", "sinks"}:
        raise PaintError("limit", "Unknown paint field")
    groups = []
    total_strokes = total_points = 0
    for name in ("sources", "sinks"):
        strokes = paint.get(name)
        if not isinstance(strokes, list):
            raise PaintError("limit", f"Paint {name} must be a list of strokes")
        if not strokes:
            raise PaintError("empty_label", f"Paint {name} must contain at least one stroke")
        total_strokes += len(strokes)
        if total_strokes > limits["max_strokes"]:
            raise PaintError("limit", "Paint exceeds max_strokes")
        groups.append(strokes)
        for stroke in strokes:
            if not isinstance(stroke, dict) or set(stroke) != {"points_nm", "radius_nm"}:
                raise PaintError("limit", "Each stroke needs only points_nm and radius_nm")
            points = stroke["points_nm"]
            if not isinstance(points, list) or not points:
                raise PaintError("limit", "Each stroke needs at least one XYZ point")
            if len(points) > limits["max_stroke_points"]:
                raise PaintError("limit", "Stroke exceeds max_stroke_points")
            total_points += len(points)
            if total_points > limits["max_total_points"]:
                raise PaintError("limit", "Paint exceeds max_total_points")
            radius = stroke["radius_nm"]
            if not _number(radius) or not 0 < radius <= limits["max_radius_nm"]:
                raise PaintError("limit", "Stroke radius_nm must be positive and at most max_radius_nm")
            for point in points:
                if (not isinstance(point, (list, tuple)) or len(point) != 3
                        or not all(_number(v) for v in point)):
                    raise PaintError("limit", "Stroke points must be finite numeric XYZ triples")
    parsed = []
    for strokes in groups:
        group = []
        for stroke in strokes:
            points = np.asarray(stroke["points_nm"], dtype=np.float64)
            points.setflags(write=False)
            group.append(Stroke(points, float(stroke["radius_nm"])))
        parsed.append(tuple(group))
    return Paint(*parsed, deepcopy(paint))


def _xyz(value, name, *, positive=False, integral=False):
    result = np.asarray(value)
    if (result.shape != (3,) or result.dtype.kind not in "iuf"
            or not np.all(np.isfinite(result))):
        raise ValueError(f"{name} must be three finite XYZ numbers")
    if positive and np.any(result <= 0):
        raise ValueError(f"{name} must be positive")
    if integral and (np.any(result != np.floor(result))
                     or np.any(np.abs(result.astype(float)) >= 2**63)):
        raise ValueError(f"{name} must be int64 XYZ coordinates")
    return result.astype(np.int64 if integral else np.float64)


def _voxels(values, name):
    values = np.asarray(values)
    if values.size == 0:
        return np.empty((0, 3), np.int64)
    if (values.ndim != 2 or values.shape[1] != 3 or values.dtype.kind not in "iuf"
            or not np.all(np.isfinite(values)) or np.any(values != np.floor(values))
            or np.any(np.abs(values.astype(float)) >= 2**63)):
        raise ValueError(f"{name} must be integer XYZ voxel triples")
    return values.astype(np.int64)


def rasterize(paint, resolution_nm_xyz, shape_xyz, limits=None, deadline=None, *, checkpoint=None):
    """Rasterize capsules at physical voxel centers into sorted unique XYZ sets.

Preflight counts every segment's clipped padded box exactly, including repeated
strokes, before allocating any box array. Candidate arrays are bounded batches;
the union is capped before inserting a voxel that would exceed its limit. This
avoids estimating a sparse capsule's voxel count from its much larger box.
"""
    check_deadline(deadline)
    limits = _limits(limits)
    resolution = _xyz(resolution_nm_xyz, "resolution_nm_xyz", positive=True)
    shape = _xyz(shape_xyz, "shape_xyz", positive=True, integral=True)
    max_box = _xyz(limits["max_box_voxels"], "max_box_voxels", positive=True, integral=True)
    segments = []
    work = 0
    all_lo, all_hi = None, None
    for label, strokes in enumerate((paint.sources, paint.sinks)):
        for stroke in strokes:
            starts = stroke.points_nm[:-1] if len(stroke.points_nm) > 1 else stroke.points_nm
            ends = stroke.points_nm[1:] if len(stroke.points_nm) > 1 else stroke.points_nm
            for a, b in zip(starts, ends):
                check_deadline(deadline)
                # Python integer arithmetic prevents overflow on hostile finite
                # coordinates before the coordinates are clipped to the volume.
                try:
                    lo = [math.ceil((min(float(x), float(y)) - stroke.radius_nm) / float(r) - .5)
                          for x, y, r in zip(a, b, resolution)]
                    hi = [math.floor((max(float(x), float(y)) + stroke.radius_nm) / float(r) - .5) + 1
                          for x, y, r in zip(a, b, resolution)]
                except OverflowError as exc:
                    raise PaintError("limit", "Paint bounding box exceeds numeric limits") from exc
                all_lo = lo if all_lo is None else [min(x, y) for x, y in zip(all_lo, lo)]
                all_hi = hi if all_hi is None else [max(x, y) for x, y in zip(all_hi, hi)]
                if any(h - l > int(m) for l, h, m in zip(all_lo, all_hi, max_box)):
                    raise PaintError("limit", "Paint bounding box exceeds max_box_voxels")
                lo = np.asarray([max(0, min(int(n), v)) for v, n in zip(lo, shape)], np.int64)
                hi = np.asarray([max(0, min(int(n), v)) for v, n in zip(hi, shape)], np.int64)
                size = np.maximum(0, hi - lo)
                count = math.prod(map(int, size))
                work += count
                if work > limits["max_raster_work"]:
                    raise PaintError("limit", "Paint exceeds max_raster_work")
                segments.append((label, a, b, stroke.radius_nm, lo, size, count))
    painted = [set(), set()]
    total = 0
    for label, a, b, radius, lo, size, count in segments:
        direction = b - a
        squared_length = float(np.dot(direction, direction))
        for begin in range(0, count, RASTER_BATCH_VOXELS):
            check_deadline(deadline)
            flat = np.arange(begin, min(count, begin + RASTER_BATCH_VOXELS), dtype=np.int64)
            xyz = np.column_stack((flat // (size[1] * size[2]),
                                   flat // size[2] % size[1], flat % size[2])) + lo
            physical = (xyz + .5) * resolution
            if squared_length:
                position = np.clip((physical - a) @ direction / squared_length, 0, 1)
                nearest = a + position[:, None] * direction
            else:
                nearest = a
            squared_distance = np.sum((physical - nearest) ** 2, axis=1)
            selected = xyz[squared_distance <= radius * radius]
            for point in map(tuple, selected):
                if point not in painted[label]:
                    if total >= limits["max_paint_voxels"]:
                        raise PaintError("limit", "Paint exceeds max_paint_voxels")
                    painted[label].add(point)
                    total += 1
            if checkpoint is not None:
                checkpoint()
            check_deadline(deadline)
    check_deadline(deadline)
    return tuple(np.asarray(sorted(group), dtype=np.int64).reshape(-1, 3) for group in painted)


def label_atoms(source_xyz, sink_xyz, labels_box, box_origin):
    """Drop background and group painted XYZ voxels by exact uint64 atom id."""
    labels = np.asarray(labels_box)
    if labels.ndim != 3 or labels.dtype.kind not in "iu" or np.any(labels < 0):
        raise ValueError("labels_box must be a nonnegative integer ZYX label crop")
    origin = _xyz(box_origin, "box_origin", integral=True)
    groups = []
    for name, values in (("source", source_xyz), ("sink", sink_xyz)):
        voxels = np.unique(_voxels(values, name), axis=0)
        offsets = voxels - origin
        if np.any(offsets < 0) or np.any(offsets >= np.array(labels.shape[::-1])):
            raise ValueError("Paint voxel is outside labels_box")
        atoms = labels[tuple(offsets[:, ::-1].T)].astype(np.uint64)
        keep = atoms != 0
        voxels, atoms = voxels[keep], atoms[keep]
        if not len(voxels):
            raise PaintError("empty_label", f"Paint {name} has no foreground voxels")
        groups.append((voxels, atoms))
    if set(map(tuple, groups[0][0])).intersection(map(tuple, groups[1][0])):
        raise PaintError("overlap", "Source and sink paint overlap on foreground voxels")
    per_atom = {}
    for name, (voxels, atoms) in zip(("source", "sink"), groups):
        for atom in np.unique(atoms):
            entry = per_atom.setdefault(int(atom), {"source": np.empty((0, 3), np.int64),
                                                    "sink": np.empty((0, 3), np.int64)})
            entry[name] = voxels[atoms == atom]
    return np.unique(groups[0][1]), np.unique(groups[1][1]), per_atom


def representative(atom, voxels_xyz):
    """Choose the painted voxel nearest its centroid; break ties by XYZ order."""
    int(atom)  # Keep the public per-atom contract without coercing any IDs to float.
    voxels = np.unique(_voxels(voxels_xyz, "voxels_xyz"), axis=0)
    if not len(voxels):
        raise PaintError("empty_label", "Cannot select a seed from empty paint")
    # Shift before converting to float, avoiding loss for a large voxel origin.
    shifted = (voxels - voxels[0]).astype(float)
    distance = np.sum((shifted - shifted.mean(axis=0)) ** 2, axis=1)
    return voxels[int(np.argmin(distance))].copy()


def _pairs(edges):
    values = np.asarray(edges, dtype=np.uint64)
    if not values.size:
        return []
    if values.ndim != 2 or values.shape[1] != 2:
        raise ValueError("Edges must be pairs of uint64 ids")
    return [tuple(map(int, pair)) for pair in values]


def _adjacency(edges, terminals=()):
    adjacency = {int(atom): set() for atom in terminals}
    for a, b in _pairs(edges):
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    return adjacency


def components(edges, terminals=()):
    """Return deterministic connected components, including isolated terminals."""
    adjacency = _adjacency(edges, terminals)
    remaining = set(adjacency)
    result = []
    while remaining:
        start = min(remaining)
        component, queue = {start}, [start]
        remaining.remove(start)
        while queue:
            for neighbor in sorted(adjacency[queue.pop()]):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    queue.append(neighbor)
        result.append(component)
    return result


def separation(local_edges, removed, sources, sinks):
    """Report each still-connected source/sink pair and one deterministic path."""
    removed = {tuple(sorted(pair)) for pair in _pairs(removed)}
    edges = [pair for pair in _pairs(local_edges) if tuple(sorted(pair)) not in removed]
    sources, sinks = sorted(set(map(int, sources))), sorted(set(map(int, sinks)))
    adjacency = _adjacency(edges, sources + sinks)
    result = []
    for source in sources:
        previous, queue = {source: None}, deque([source])
        while queue:
            current = queue.popleft()
            for neighbor in sorted(adjacency[current]):
                if neighbor not in previous:
                    previous[neighbor] = current
                    queue.append(neighbor)
        for sink in sinks:
            if sink not in previous:
                continue
            path, current = [], sink
            while previous[current] is not None:
                parent = previous[current]
                path.append((parent, current))
                current = parent
            result.append((source, sink, path[::-1]))
    return result


def closed_side(component_atoms, atom_bounds, box_lo, box_hi, fake_edges):
    """Certify no physical or fake edge can leave the selected local graph.

atom_bounds must contain all local graph nodes, with half-open XYZ bounds as
flattened six-vectors or two triples. Missing bounds and outgoing fake edges
make a component open. The one-voxel margin is deliberately conservative.
"""
    side = set(map(int, component_atoms))
    bounds = {int(atom): np.asarray(box).reshape(2, 3) for atom, box in atom_bounds.items()}
    low = _xyz(box_lo, "box_lo", integral=True) + 1
    high = _xyz(box_hi, "box_hi", integral=True) - 1
    opened = set()
    for atom in side:
        if atom not in bounds or np.any(bounds[atom][0] < low) or np.any(bounds[atom][1] > high):
            opened.add(atom)
    for a, b in _pairs(fake_edges):
        if a in side and b not in bounds:
            opened.add(a)
        if b in side and a not in bounds:
            opened.add(b)
    return bool(side) and not opened, sorted(opened)


def contacts(labels_zyx, origin_xyz, a, b):
    """Return voxels of a having a six-connected face contact with b, in XYZ."""
    labels = np.asarray(labels_zyx)
    if labels.ndim != 3 or labels.dtype.kind not in "iu":
        raise ValueError("labels_zyx must be an integer ZYX label crop")
    origin = _xyz(origin_xyz, "origin_xyz", integral=True)
    a, b = np.uint64(a), np.uint64(b)
    if a == b:
        return np.empty((0, 3), np.int64)
    found = []
    for axis in range(3):
        before, after = [slice(None)] * 3, [slice(None)] * 3
        before[axis], after[axis] = slice(None, -1), slice(1, None)
        left, right = labels[tuple(before)], labels[tuple(after)]
        points = np.argwhere((left == a) & (right == b))
        if len(points):
            found.append(points)
        points = np.argwhere((left == b) & (right == a))
        if len(points):
            points[:, axis] += 1
            found.append(points)
    if not found:
        return np.empty((0, 3), np.int64)
    return np.unique(np.concatenate(found)[:, ::-1] + origin, axis=0)


def near(contacts_xyz, paint_tree, max_cut_distance_nm, resolution):
    """True if a contact center is near a paint center in physical units.

paint_tree is a scipy.spatial.cKDTree built from physical paint voxel centers.
An empty contact set cannot establish locality (including fake edges).
"""
    points = _voxels(contacts_xyz, "contacts_xyz")
    spacing = _xyz(resolution, "resolution", positive=True)
    if not len(points):
        return False
    distance, _ = paint_tree.query((points + .5) * spacing, k=1)
    return bool(np.min(distance) <= max_cut_distance_nm)
