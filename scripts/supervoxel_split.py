"""Deterministic, array-only partitioning of one atomic segmentation mask.

Arrays and local seed coordinates use ZYX order. Grid origins, sizes and voxel
spacing use XYZ order, as in deployment.json. No graph or storage API is used.
"""

from collections import defaultdict
from dataclasses import dataclass
import heapq
import itertools
from typing import Mapping

import cc3d
import numpy as np
from scipy import ndimage


def _mask(value):
    value = np.asarray(value, dtype=bool)
    if value.ndim != 3 or any(size == 0 for size in value.shape):
        raise ValueError("mask must be a nonempty three-dimensional array")
    return value


def _xyz(value, name, *, integral=False, positive=False):
    array = np.asarray(value)
    if array.shape != (3,) or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must have three finite XYZ entries")
    if integral and (np.any(array != np.floor(array)) or array.dtype.kind == "b"):
        raise ValueError(f"{name} must have integer entries")
    if positive and np.any(array <= 0):
        raise ValueError(f"{name} must have positive entries")
    return array.astype(np.int64 if integral else np.float64)


def _components(labels):
    pieces, count = cc3d.connected_components(
        np.ascontiguousarray(labels), connectivity=6, return_N=True,
    )
    return pieces.astype(np.int32, copy=False), int(count)


def grid_pieces(mask_zyx, box_origin_xyz, grid_xyz):
    """Partition a mask into connected parts of a volume-aligned XYZ grid.

``box_origin_xyz`` is the box's voxel offset from the volume origin. A grid
    boundary at a multiple of ``grid_xyz`` therefore remains the same when the
    caller crops a smaller box. Zero denotes outside the mask.
    """
    mask = _mask(mask_zyx)
    origin = _xyz(box_origin_xyz, "box_origin_xyz", integral=True)
    grid = _xyz(grid_xyz, "grid_xyz", integral=True, positive=True)
    blocks = []
    for size, offset, width in zip(mask.shape, origin[::-1], grid[::-1]):
        values = (np.arange(size, dtype=np.int64) + offset) // width
        blocks.append(values - values[0])
    z, y, x = blocks
    labels = ((z[:, None, None] * (y[-1] + 1) + y[None, :, None])
              * (x[-1] + 1) + x[None, None, :] + 1)
    labels[~mask] = 0
    return _components(labels)


def _seeds(values, mask, name):
    values = np.asarray(values)
    if values.size == 0 or values.ndim != 2 or values.shape[1] != 3:
        raise ValueError(f"{name} must contain at least one local ZYX seed")
    if not np.all(np.isfinite(values)) or np.any(values != np.floor(values)):
        raise ValueError(f"{name} must contain integer voxel coordinates")
    values = values.astype(np.int64)
    if np.any(values < 0) or np.any(values >= np.asarray(mask.shape)):
        raise ValueError(f"{name} seed lies outside the mask")
    if not np.all(mask[tuple(values.T)]):
        raise ValueError(f"{name} seed lies outside the mask")
    return sorted(set(map(tuple, values)))


def seeded_pieces(mask_zyx, source_vox, sink_vox, spacing_nm):
    """Watershed of negative physical EDT with two groups of local ZYX seeds.

The small masked piece uses a stable priority flood, avoiding an additional
    image-library dependency in the PCG image. Equal-height arrivals are FIFO;
    seeds and neighbor traversal have fixed ordering. The result relabels each
    disconnected part of either region as its own atom. ``seed_piece`` maps each
    seed's ZYX tuple to its final local piece number.
    """
    mask = _mask(mask_zyx)
    spacing = _xyz(spacing_nm, "spacing_nm", positive=True)
    source = _seeds(source_vox, mask, "source_vox")
    sink = _seeds(sink_vox, mask, "sink_vox")
    if set(source).intersection(sink):
        raise ValueError("a voxel cannot be both a source and a sink seed")
    # A tight bounding box may have foreground on every face. Include explicit
    # outside background so SciPy measures distance to all six exterior faces.
    elevation = -ndimage.distance_transform_edt(
        np.pad(mask, 1), sampling=spacing[::-1],
    )[1:-1, 1:-1, 1:-1]
    regions = np.zeros(mask.shape, dtype=np.uint8)
    queue = []
    sequence = itertools.count()
    for group, seeds in ((1, source), (2, sink)):
        for point in seeds:
            regions[point] = group
            heapq.heappush(queue, (float(elevation[point]), next(sequence), point))
    while queue:
        height, _, point = heapq.heappop(queue)
        region = regions[point]
        for axis in range(3):
            for direction in (-1, 1):
                neighbor = list(point)
                neighbor[axis] += direction
                if not 0 <= neighbor[axis] < mask.shape[axis]:
                    continue
                neighbor = tuple(neighbor)
                if not mask[neighbor] or regions[neighbor]:
                    continue
                regions[neighbor] = region
                heapq.heappush(queue, (
                    max(height, float(elevation[neighbor])), next(sequence), neighbor,
                ))
    if np.any(mask & (regions == 0)):
        raise ValueError("every disconnected mask component must contain a seed")
    pieces, count = _components(regions)
    seed_piece = {point: int(pieces[point]) for point in source + sink}
    if {seed_piece[p] for p in source}.intersection(seed_piece[p] for p in sink):
        raise AssertionError("watershed placed opposing seeds in one piece")
    return pieces, count, seed_piece


def _piece_array(value):
    value = np.asarray(value)
    if value.ndim != 3 or value.dtype.kind not in "iu" or np.any(value < 0):
        raise ValueError("pieces must be a three-dimensional nonnegative integer array")
    return value


def piece_contacts(pieces):
    """Return canonical local piece pairs and the number of shared voxel faces."""
    pieces = _piece_array(pieces)
    pairs = []
    for axis in range(3):
        before, after = [slice(None)] * 3, [slice(None)] * 3
        before[axis], after[axis] = slice(None, -1), slice(1, None)
        a, b = pieces[tuple(before)], pieces[tuple(after)]
        selected = (a > 0) & (b > 0) & (a != b)
        if np.any(selected):
            pairs.append(np.sort(np.column_stack((a[selected], b[selected])), axis=1))
    if not pairs:
        empty = np.empty(0, dtype=np.int64)
        return empty, empty.copy(), empty.copy()
    unique, counts = np.unique(np.concatenate(pairs), axis=0, return_counts=True)
    return unique[:, 0].astype(np.int64), unique[:, 1].astype(np.int64), counts


def neighbour_contacts(pieces, context_zyx):
    """Count contacts to current atoms in the box padded by one voxel per face.

The context is the atomic segmentation before refinement. All positive piece
    voxels must cover exactly one positive old atom. Contacts outside the mask,
    including beyond its chunk face, are grouped by neighbor atom then piece.
    """
    pieces = _piece_array(pieces)
    context = np.asarray(context_zyx)
    if context.shape != tuple(size + 2 for size in pieces.shape) or context.dtype.kind not in "iu":
        raise ValueError("context must be an integer atomic array with shape pieces.shape + 2")
    center = context[1:-1, 1:-1, 1:-1]
    old = np.unique(center[pieces > 0])
    if len(old) == 0:
        return {}
    if len(old) != 1 or old[0] == 0:
        raise ValueError("pieces must cover exactly one positive current atom")
    counts = defaultdict(lambda: defaultdict(int))
    for axis in range(3):
        for direction in (-1, 1):
            slices = [slice(1, -1)] * 3
            slices[axis] = slice(None, -2) if direction == -1 else slice(2, None)
            neighbor = context[tuple(slices)]
            selected = (pieces > 0) & (neighbor > 0) & (neighbor != old[0])
            if not np.any(selected):
                continue
            pairs, areas = np.unique(
                # uint64 + signed int32 would promote to float64 and round PCG
                # node ids above 2**53 before the contact map is constructed.
                np.column_stack((neighbor[selected].astype(np.uint64),
                                 pieces[selected].astype(np.uint64))),
                axis=0, return_counts=True,
            )
            for (atom, piece), area in zip(pairs, areas):
                counts[int(atom)][int(piece)] += int(area)
    return {atom: sorted(counts[atom].items()) for atom in sorted(counts)}


def internal_affinity(area, ref_area=256.0, floor=0.01, ceiling=1.0):
    """Convert face counts to bounded finite affinities (scalar or array)."""
    if not np.isfinite(ref_area) or ref_area <= 0:
        raise ValueError("ref_area must be positive and finite")
    if not (0 < floor <= ceiling < float("inf")):
        raise ValueError("affinity limits must be finite and positive")
    values = np.asarray(area, dtype=np.float64)
    if not np.all(np.isfinite(values)) or np.any(values < 0):
        raise ValueError("contact areas must be finite and nonnegative")
    result = np.clip(values / ref_area, floor, ceiling)
    return float(result) if result.ndim == 0 else result


@dataclass(frozen=True, order=True)
class Edge:
    """A canonical undirected edge; inactive edges retain rewrite provenance."""

    a: int
    b: int
    affinity: float
    area: int = 0
    active: bool = True

    def __post_init__(self):
        a, b = sorted((int(self.a), int(self.b)))
        if a <= 0 or a == b:
            raise ValueError("an edge requires two distinct positive atom ids")
        if np.isnan(self.affinity) or self.affinity < 0:
            raise ValueError("edge affinity must be nonnegative and not NaN")
        if int(self.area) != self.area or self.area < 0:
            raise ValueError("edge area must be a nonnegative integer")
        object.__setattr__(self, "a", a)
        object.__setattr__(self, "b", b)
        object.__setattr__(self, "affinity", float(self.affinity))
        object.__setattr__(self, "area", int(self.area))
        object.__setattr__(self, "active", bool(self.active))


def _edge(value):
    if isinstance(value, Edge):
        return value
    if isinstance(value, Mapping):
        return Edge(**value)
    return Edge(*value)


def rewrite_edges(old_atom, current_edges, piece_ids, contacts, centroids,
                  neighbour_points, *, internal_contacts=None, ref_area=256.0):
    """Replace incident edges and return complete old-edge -> new-edge provenance.

``piece_ids`` maps local labels to newly allocated global atom ids. ``contacts``
    is from :func:`neighbour_contacts`; centroid and neighbor inside-point maps
    use the same coordinate frame and units. Contacting pieces inherit the old
    edge's affinity and activity. A noncontact edge uses the closest centroid,
    breaking ties by the local piece label. Infinite edges require real face
    contact. Unrelated input edges are retained; retired neighbors must already
    have been removed or translated by the caller. Optional ``internal_contacts``
    is the tuple returned by :func:`piece_contacts`.
    """
    old_atom = int(old_atom)
    ids = {int(piece): int(atom) for piece, atom in piece_ids.items()}
    if not ids or any(piece <= 0 or atom <= 0 for piece, atom in ids.items()):
        raise ValueError("piece_ids must map positive pieces to positive atom ids")
    if len(set(ids.values())) != len(ids) or old_atom in ids.values():
        raise ValueError("new atom ids must be unique and exclude the old atom")
    result, provenance = [], defaultdict(set)
    for edge in sorted(map(_edge, current_edges)):
        if old_atom not in (edge.a, edge.b):
            result.append(edge)
            continue
        neighbor = edge.b if edge.a == old_atom else edge.a
        touching = list(contacts.get(neighbor, ()))
        if not touching:
            if np.isinf(edge.affinity):
                raise ValueError("an infinite seam edge must have a real face contact")
            if neighbor not in neighbour_points:
                raise ValueError(f"missing inside point for noncontact neighbor {neighbor}")
            point = _xyz(neighbour_points[neighbor], "neighbor point")
            distances = []
            for piece in sorted(ids):
                if piece not in centroids:
                    raise ValueError(f"missing centroid for piece {piece}")
                delta = _xyz(centroids[piece], "piece centroid") - point
                distances.append((float(np.dot(delta, delta)), piece))
            touching = [(min(distances)[1], edge.area)]
        for piece, area in sorted(touching):
            piece = int(piece)
            if piece not in ids:
                raise ValueError(f"contact references unknown piece {piece}")
            replacement = Edge(ids[piece], neighbor, edge.affinity, area, edge.active)
            result.append(replacement)
            provenance[(edge.a, edge.b)].add((replacement.a, replacement.b))
    if internal_contacts is not None:
        if len(internal_contacts) != 3 or len({len(v) for v in internal_contacts}) != 1:
            raise ValueError("internal_contacts must contain aligned a, b and area arrays")
        for a, b, area in zip(*internal_contacts):
            if int(a) not in ids or int(b) not in ids:
                raise ValueError("internal contact references an unknown piece")
            result.append(Edge(ids[int(a)], ids[int(b)],
                               internal_affinity(area, ref_area), area))
    return sorted(set(result)), {key: sorted(provenance[key]) for key in sorted(provenance)}
