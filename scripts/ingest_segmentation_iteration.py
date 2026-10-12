"""Prepare immutable source labels, then ingest an isolated native graph.

TensorSwitch cannot generate native ChunkedGraph IDs/hierarchies. This saved
adapter validates metadata and preserves every source voxel while doing that
graph-specific work. Sources are precomputed segmentation and OME-Zarr v2
image, either file:///source/... or public HTTP(S). Origins must be zero.
"""
import hashlib
import itertools
import json
import os
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np
import requests

DATA_FIELDS = ("graph_id", "datastack", "aligned_volume", "image_https",
               "segmentation_https", "shape_xyz", "resolution_nm_xyz",
               "graph_chunk_xyz", "atom_policy", "atom_tile_xyz", "voxel_offset_xyz",
               "agglomeration_https", "agglomeration_default_affinity")
POLICIES = ("source_labels", "spatial_tiles", "agglomerated_supervoxels")
# Policies whose supplied labels are indivisible supervoxels (only spatial_tiles subdivides them).
INDIVISIBLE = ("source_labels", "agglomerated_supervoxels")
UNIT_TO_NM = {"nanometer": 1, "nanometre": 1, "nm": 1,
              "micrometer": 1000, "micrometre": 1000, "um": 1000, "µm": 1000,
              "millimeter": 1e6, "millimetre": 1e6, "mm": 1e6,
              "meter": 1e9, "metre": 1e9, "m": 1e9}


def data_spec(spec):
    """Data identity is independent of host path, port, or Compose project."""
    result = {key: spec[key] for key in DATA_FIELDS if key in spec}
    result.setdefault("atom_policy", "source_labels")
    result.setdefault("voxel_offset_xyz", [0, 0, 0])
    if result["atom_policy"] != "spatial_tiles":
        result.pop("atom_tile_xyz", None)
    if result["atom_policy"] != "agglomerated_supervoxels":
        result.pop("agglomeration_https", None)
        result.pop("agglomeration_default_affinity", None)
    return result


def positive_triple(value, name, integer=False):
    array = np.asarray(value)
    if array.shape != (3,) or not np.issubdtype(array.dtype, np.number):
        raise ValueError(f"{name} must contain three numbers in XYZ order")
    if not np.all(np.isfinite(array)) or np.any(array <= 0):
        raise ValueError(f"{name} must contain finite positive values")
    if integer and (not np.all(array == np.floor(array)) or np.any(array > 2**31 - 1)):
        raise ValueError(f"{name} must contain positive integer dimensions")
    return array.astype(np.int64 if integer else np.float64)


def validate_geometry(spec):
    shape = positive_triple(spec["shape_xyz"], "shape_xyz", integer=True)
    chunk = positive_triple(spec["graph_chunk_xyz"], "graph_chunk_xyz", integer=True)
    resolution = positive_triple(spec["resolution_nm_xyz"], "resolution_nm_xyz")
    offset = np.asarray(spec.get("voxel_offset_xyz", [0, 0, 0]))
    if offset.shape != (3,) or np.any(offset != 0):
        raise ValueError("Nonzero voxel origins are not supported; supply an explicitly rebased dataset")
    counts = (shape + chunk - 1) // chunk
    if np.max(counts) < 2:
        raise ValueError("Native graph requires at least two graph chunks along one axis")
    if np.any(counts > 1024):
        raise ValueError("Graph grid exceeds native 10-bit chunk coordinates (1024 per axis)")
    mode = spec.get("atom_policy", "source_labels")
    if mode not in POLICIES:
        raise ValueError("atom_policy must be source_labels, spatial_tiles or agglomerated_supervoxels")
    if mode == "agglomerated_supervoxels" and not spec.get("agglomeration_https"):
        raise ValueError("agglomerated_supervoxels requires agglomeration_https (an .npz of fragment/segment/rg arrays)")
    tile = None
    if mode == "spatial_tiles":
        if "atom_tile_xyz" not in spec:
            raise ValueError("spatial_tiles requires explicit atom_tile_xyz")
        tile = positive_triple(spec["atom_tile_xyz"], "atom_tile_xyz", integer=True)
        if np.any(chunk % tile != 0):
            raise ValueError("atom_tile_xyz must divide graph_chunk_xyz along every axis")
    return shape, chunk, resolution, tile, counts


def read_json(source, relative):
    url = source.rstrip("/") + "/" + relative
    parsed = urlparse(url)
    if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
        return json.loads(Path(unquote(parsed.path)).read_text())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Sources must use file://, http://, or https://")
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def read_bytes(source):
    parsed = urlparse(source)
    if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
        return Path(unquote(parsed.path)).read_bytes()
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Sources must use file://, http://, or https://")
    response = requests.get(source, timeout=300)
    response.raise_for_status()
    return response.content


def load_agglomeration(spec):
    """Supervoxel grouping and region graph for agglomerated_supervoxels.

    The .npz holds `fragment`/`segment` (each supplied label's initial segment; 0 = ungrouped, its own root) and
    `rg_a`/`rg_b`/`rg_aff` (region-graph affinities between labels, e.g. ABISS dend). Full uint64 labels are
    supported: pairs are keyed by their dense positions in the sorted fragment table.
    """
    import io
    raw = read_bytes(spec["agglomeration_https"])
    with np.load(io.BytesIO(raw), allow_pickle=False) as z:
        fragment, segment = z["fragment"].astype(np.uint64), z["segment"].astype(np.uint64)
        rg_a, rg_b, rg_aff = z["rg_a"].astype(np.uint64), z["rg_b"].astype(np.uint64), z["rg_aff"].astype(np.float64)
    if fragment.ndim != 1 or fragment.shape != segment.shape or len(np.unique(fragment)) != len(fragment) or np.any(fragment == 0):
        raise ValueError("Agglomeration needs unique nonzero fragment ids with one segment each")
    if not (rg_a.shape == rg_b.shape == rg_aff.shape) or rg_a.ndim != 1:
        raise ValueError("Agglomeration region graph arrays must be one-dimensional and equal in length")
    if np.any(~np.isfinite(rg_aff)) or np.any(rg_aff < 0) or np.any(rg_aff > 1):
        raise ValueError("Region-graph affinities must lie in [0, 1]")
    order = np.argsort(fragment)
    agg = {"fragment": fragment[order], "segment": segment[order], "sha256": hashlib.sha256(raw).hexdigest()}
    keys = pair_keys(agg, rg_a, rg_b)
    korder = np.argsort(keys)
    agg.update(rg_key=keys[korder], rg_aff=rg_aff[korder])
    return agg


def fragment_positions(agg, labels):
    pos = np.searchsorted(agg["fragment"], labels)
    if np.any(pos >= len(agg["fragment"])) or np.any(agg["fragment"][np.minimum(pos, len(agg["fragment"]) - 1)] != labels):
        raise ValueError("Labels missing from the agglomeration mapping (segmentation or region graph)")
    return pos.astype(np.uint64)


def pair_keys(agg, a_labels, b_labels):
    a, b = fragment_positions(agg, a_labels), fragment_positions(agg, b_labels)
    return np.minimum(a, b) * np.uint64(len(agg["fragment"])) + np.maximum(a, b)


def label_groups(agg, labels):
    """Dense initial-group index per supplied label: segments first, then one group per ungrouped (segment 0) label."""
    pos = fragment_positions(agg, labels)
    segments = agg["segment"][pos]
    table = np.unique(agg["segment"][agg["segment"] != 0])
    return np.where(segments == 0, np.uint64(len(table)) + pos, np.searchsorted(table, segments).astype(np.uint64))


def ungrouped_from(agg):
    """Group indices at or above this value are single ungrouped supervoxels."""
    return np.uint64(len(np.unique(agg["segment"][agg["segment"] != 0])))


def pair_affinity(agg, a_labels, b_labels, default):
    """Region-graph affinity for each label pair; same label = uncuttable (inf); absent edge = default."""
    keys = pair_keys(agg, a_labels, b_labels)
    affinity = np.full(len(keys), default, np.float64)
    if len(agg["rg_key"]):
        pos = np.minimum(np.searchsorted(agg["rg_key"], keys), len(agg["rg_key"]) - 1)
        found = agg["rg_key"][pos] == keys
        affinity[found] = agg["rg_aff"][pos[found]]
    return np.where(a_labels == b_labels, np.inf, affinity).astype(np.float32)


def image_geometry(spec):
    """Read axes and compose declared transforms without guessing units."""
    shape, _, resolution, _, _ = validate_geometry(spec)
    attrs = read_json(spec["image_https"], ".zattrs")
    multiscales = attrs.get("multiscales", [])
    if len(multiscales) != 1:
        raise ValueError("Image must declare exactly one OME-NGFF multiscale")
    metadata = multiscales[0]
    axes = metadata.get("axes", [])
    names = [axis.get("name") if isinstance(axis, dict) else None for axis in axes]
    if names not in (["z", "y", "x"], ["x", "y", "z"]):
        raise ValueError("Image axes must be exactly ZYX or XYZ; channel/time axes are not supported")
    factors = []
    for axis in axes:
        if axis.get("type", "space") != "space" or axis.get("unit") not in UNIT_TO_NM:
            raise ValueError("Each image axis needs a supported explicit spatial unit")
        factors.append(UNIT_TO_NM[axis["unit"]])
    datasets = metadata.get("datasets", [])
    if not datasets:
        raise ValueError("Image metadata has no array datasets")
    level = datasets[0]
    path = level.get("path", "")
    if not path or path.startswith("/") or ".." in Path(path).parts:
        raise ValueError("Image dataset path must be a relative path within its group")
    array = read_json(spec["image_https"], path.rstrip("/") + "/.zarray")
    if array.get("zarr_format") != 2:
        raise ValueError("Only Zarr v2 image arrays are supported")
    order = [names.index(name) for name in ("x", "y", "z")]
    image_shape = positive_triple(array.get("shape"), "image array shape", integer=True)[order]
    positive_triple(array.get("chunks"), "image array chunks", integer=True)
    if not np.array_equal(image_shape, shape):
        raise ValueError(f"Image shape {image_shape.tolist()} differs from shape_xyz {shape.tolist()}")
    if np.dtype(array["dtype"]).kind not in "uif":
        raise ValueError("Image dtype must be an unsigned/signed integer or float")
    scale, translation = np.ones(3), np.zeros(3)
    transformations = level.get("coordinateTransformations", []) + metadata.get("coordinateTransformations", [])
    if not any(t.get("type") == "scale" for t in transformations):
        raise ValueError("Image needs an explicit scale transform")
    for transform in transformations:
        kind = transform.get("type")
        if kind == "scale":
            factor = positive_triple(transform.get("scale"), "image scale")
            scale *= factor
            translation *= factor
        elif kind == "translation":
            value = np.asarray(transform.get("translation"), dtype=float)
            if value.shape != (3,) or not np.all(np.isfinite(value)):
                raise ValueError("Image translation must contain three finite values")
            translation += value
        else:
            raise ValueError(f"Unsupported image coordinate transform: {kind}")
    nm = (scale * factors)[order]
    if not np.allclose(nm, resolution, rtol=1e-9, atol=1e-9):
        raise ValueError(f"Image resolution {nm.tolist()} nm differs from resolution_nm_xyz")
    if np.any(translation != 0):
        raise ValueError("Nonzero image translations are not supported; supply an explicitly rebased dataset")
    return attrs, {"axes": names, "shape_xyz": image_shape.tolist(),
                   "resolution_nm_xyz": nm.tolist(), "array_path": path}


def component_keys(labels_zyx, mode, chunk_xyz, tile_xyz=None):
    """Categorical compaction avoids arithmetic on potentially maximal uint64 IDs."""
    if np.asarray(labels_zyx).dtype.kind != "u":
        raise ValueError("Segmentation labels must have an unsigned integer dtype")
    if mode in INDIVISIBLE:
        return labels_zyx
    if mode != "spatial_tiles" or tile_xyz is None:
        raise ValueError("Spatial atom creation requires explicit tile dimensions")
    tile_xyz, chunk_xyz = np.asarray(tile_xyz), np.asarray(chunk_xyz)
    tile_count = chunk_xyz // tile_xyz
    stride = int(np.prod(tile_count)) + 1
    unique, inverse = np.unique(labels_zyx, return_inverse=True)
    if len(unique) * stride > np.iinfo(np.uint64).max:
        raise ValueError("Too many tile/label combinations for uint64 categorical keys")
    compact = inverse.reshape(labels_zyx.shape).astype(np.uint64)
    zz, yy, xx = np.indices(labels_zyx.shape, sparse=True)
    tile = (zz // tile_xyz[2]) * tile_count[1] * tile_count[0] + (yy // tile_xyz[1]) * tile_count[0] + xx // tile_xyz[0]
    keys = compact * np.uint64(stride) + tile.astype(np.uint64) + 1
    keys[labels_zyx == 0] = 0
    return keys


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def graph_meta(spec, watershed):
    from pychunkedgraph.graph.meta import ChunkedGraphMeta, DataSource, GraphConfig
    return ChunkedGraphMeta(GraphConfig(ID=spec["graph_id"], CHUNK_SIZE=spec["graph_chunk_xyz"], FANOUT=2),
                            DataSource(WATERSHED=watershed, EDGES="file:///data/edges", DATA_VERSION=2))


def source_volume(spec):
    from cloudvolume import CloudVolume
    shape, _, resolution, _, _ = validate_geometry(spec)
    volume = CloudVolume(spec["segmentation_https"], fill_missing=False, progress=False, parallel=1)
    if volume.info["type"] != "segmentation" or volume.num_channels != 1:
        raise ValueError("Precomputed source must be a one-channel segmentation")
    if np.dtype(volume.dtype).kind != "u":
        raise ValueError("Segmentation source must use unsigned integer labels")
    if not np.array_equal(volume.volume_size, shape):
        raise ValueError("Segmentation size differs from shape_xyz")
    if np.any(volume.voxel_offset != 0):
        raise ValueError("Nonzero segmentation voxel offsets are not supported")
    if not np.allclose(volume.resolution, resolution, rtol=1e-9, atol=1e-9):
        raise ValueError("Segmentation resolution differs from resolution_nm_xyz")
    return volume


def chunk_coordinates(counts):
    return itertools.product(*(range(int(n)) for n in counts))


def prepare(spec, root=Path("/data")):
    import cc3d
    from cloudvolume import CloudVolume
    from pychunkedgraph.graph.chunkedgraph import ChunkedGraph
    shape, chunk, resolution, tile, counts = validate_geometry(spec)
    identity = data_spec(spec)
    if (root / "prepared.json").exists():
        previous = json.loads((root / "prepared.json").read_text())
        if data_spec(previous["spec"]) != identity:
            raise RuntimeError("Data configuration changed after preparation; create a separate deployment")
        print("Source already prepared; preserving graph and edit state.", flush=True)
        return
    if (root / "preparing.json").exists() or (root / "atomic/info").exists():
        raise RuntimeError("Partial preparation exists; inspect it before deleting derived data")
    attrs, image = image_geometry(spec)
    source = source_volume(spec)
    mode = identity["atom_policy"]
    agg = load_agglomeration(spec) if mode == "agglomerated_supervoxels" else None
    save(root / "preparing.json", {"spec": identity, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    save(root / "source-image-attrs.json", attrs)
    save(root / "source-segmentation-info.json", source.info)
    info = CloudVolume.create_new_info(num_channels=1, layer_type="segmentation", data_type="uint64",
        encoding="compressed_segmentation", resolution=resolution.tolist(), voxel_offset=[0, 0, 0],
        chunk_size=chunk.tolist(), volume_size=shape.tolist(), compressed_segmentation_block_size=[8, 8, 8])
    cv = CloudVolume("file://" + str(root / "atomic"), info=info, compress=True, fill_missing=False)
    cv.commit_info()
    cg = ChunkedGraph(meta=graph_meta(spec, "file://" + str(root / "atomic")))
    index, bounds = [], []
    digest = hashlib.sha256()
    (root / "chunks").mkdir(exist_ok=True)
    for coordinate in chunk_coordinates(counts):
        lo = np.array(coordinate) * chunk
        hi = np.minimum(lo + chunk, shape)
        slices = tuple(slice(int(x), int(y)) for x, y in zip(lo, hi))
        labels = np.ascontiguousarray(source[slices][..., 0].transpose(2, 1, 0))
        digest.update(labels.astype("<u8").tobytes())
        components = cc3d.connected_components(component_keys(labels, mode, chunk, tile), connectivity=6, out_dtype=np.uint32)
        stats = cc3d.statistics(components)
        unique, first = np.unique(components, return_index=True)
        unique, first = unique[unique != 0], first[unique != 0]
        cid = cg.get_chunk_id(layer=1, x=coordinate[0], y=coordinate[1], z=coordinate[2])
        if len(unique) > int(cg.get_segment_id_limit(cid)):
            raise ValueError(f"Chunk {coordinate} has too many atoms for native graph IDs")
        ids = cid + np.arange(1, len(unique) + 1, dtype=np.uint64)
        lut = np.zeros(int(components.max()) + 1, dtype=np.uint64)
        lut[unique] = ids
        out = lut[components]
        source_lut = np.zeros_like(lut, dtype=np.uint64)
        source_lut[unique] = labels.flat[first]
        if not np.array_equal(source_lut[components], labels):
            raise RuntimeError("An atom mixed source labels")
        xyz = np.array(np.unravel_index(first, labels.shape))[::-1].T + lo
        for atom, component, point, label in zip(ids, unique, xyz, labels.flat[first]):
            index.append([int(atom), int(label), *map(int, point)])
            box = stats["bounding_boxes"][component]
            bounds.append([*[int(s.start + base) for s, base in zip(box[::-1], lo)],
                           *[int(s.stop + base) for s, base in zip(box[::-1], lo)]])
        np.save(root / "chunks" / ("_".join(map(str, coordinate)) + ".npy"), out)
        cv[slices] = out.transpose(2, 1, 0)[..., None]
        if not np.array_equal(cv[slices][..., 0].transpose(2, 1, 0), out):
            raise RuntimeError(f"Atomic storage readback mismatch in chunk {coordinate}")
        print("PREPARED", coordinate, "atoms", len(ids), flush=True)
    if not index:
        raise ValueError("Segmentation contains only background; no graph can be ingested")
    index = np.asarray(index, dtype=np.uint64).reshape(-1, 5)
    bounds = np.asarray(bounds, dtype=np.uint32).reshape(-1, 6)
    order = np.argsort(index[:, 0])
    if agg is not None:
        label_groups(agg, np.unique(index[:, 1]))  # every supplied label must have a mapping row
    np.save(root / "atom_index.npy", index[order])
    np.save(root / "atom_bounds.npy", bounds[order])
    description = {
        "source_labels": "Each 6-connected source-label component is indivisible; touching fragments across graph chunks use infinite-affinity edges. Disconnected regions sharing a label ID remain separate roots.",
        "spatial_tiles": "6-connected source-label components inside spatial tiles; touching same-label atoms use cuttable edges.",
        "agglomerated_supervoxels": "Each 6-connected supervoxel component is indivisible; touching pieces of one supervoxel use infinite-affinity edges. Touching supervoxels of the same initial segment are joined with their region-graph affinity (default when absent); other contacts start unjoined."}[mode]
    save(root / "prepared.json", {"spec": identity, "shape_xyz": shape.tolist(),
        "resolution_nm_xyz": resolution.tolist(), "graph_chunk_xyz": chunk.tolist(),
        "atom_tile_xyz": tile.tolist() if tile is not None else None, "atom_count": len(index),
        "source_chunk_order_sha256": digest.hexdigest(), "source_digest_dtype": "little-endian uint64",
        "readback": "Every prepared voxel read back and compared; every atom maps to exactly one source label.",
        "atom_policy": mode, "atom_policy_description": description, "image_geometry": image, "zero_is_background": True,
        **({"agglomeration_sha256": agg["sha256"], "agglomeration_segments": int(len(np.unique(agg["segment"][agg["segment"] != 0]))),
            "agglomeration_rg_edges": int(len(agg["rg_key"]))} if agg is not None else {})})
    (root / "preparing.json").unlink()


def ingest(spec, root=Path("/data")):
    from pychunkedgraph.graph.chunkedgraph import ChunkedGraph
    from pychunkedgraph.graph.edges import EDGE_TYPES, Edges
    from pychunkedgraph.ingest.create.abstract_layers import add_layer
    from pychunkedgraph.ingest.create.atomic_layer import add_atomic_edges
    from pychunkedgraph.io.edges import put_chunk_edges
    shape, chunk, _, _, counts = validate_geometry(spec)
    if (root / "ingested.json").exists():
        print("Graph already ingested; preserving its edits.", flush=True)
        return
    if (root / "ingesting.json").exists():
        raise RuntimeError("Partial graph ingestion exists; inspect it before rerunning")
    save(root / "ingesting.json", {"graph": spec["graph_id"]})
    mode = spec.get("atom_policy", "source_labels")
    agg = None
    if mode == "agglomerated_supervoxels":
        agg = load_agglomeration(spec)
        if agg["sha256"] != json.loads((root / "prepared.json").read_text()).get("agglomeration_sha256"):
            raise RuntimeError("Agglomeration file changed after preparation; create a separate deployment")
        default = float(spec.get("agglomeration_default_affinity", 0.001))
    cg = ChunkedGraph(meta=graph_meta(spec, "http://gateway/data/atomic"))
    cg.create()
    index = np.load(root / "atom_index.npy")
    allpairs, all_region_pairs = [], []
    for coordinate in chunk_coordinates(counts):
        a = np.load(root / "chunks" / ("_".join(map(str, coordinate)) + ".npy"), mmap_mode="r")
        pairs = []
        for axis in range(3):
            left, right = [slice(None)] * 3, [slice(None)] * 3
            left[axis], right[axis] = slice(None, -1), slice(1, None)
            x, y = a[tuple(left)], a[tuple(right)]
            mask = (x != 0) & (y != 0) & (x != y)
            if mask.any():
                pairs.append(np.stack([x[mask], y[mask]], axis=1))
            neighbor = list(coordinate)
            neighbor[2 - axis] += 1
            if neighbor[2 - axis] < counts[2 - axis]:
                b = np.load(root / "chunks" / ("_".join(map(str, neighbor)) + ".npy"), mmap_mode="r")
                left[axis], right[axis] = -1, 0
                x, y = a[tuple(left)], b[tuple(right)]
                mask = (x != 0) & (y != 0) & (x != y)
                if mask.any():
                    pairs.append(np.stack([x[mask], y[mask]], axis=1))
        if pairs:
            p = np.unique(np.sort(np.concatenate(pairs), axis=1), axis=0)
            all_region_pairs.append(p)
            labels = index[np.searchsorted(index[:, 0], p), 1]
            if agg is not None:
                groups = label_groups(agg, labels.ravel()).reshape(labels.shape)
                p = p[groups[:, 0] == groups[:, 1]]
            else:
                p = p[labels[:, 0] == labels[:, 1]]
            if len(p):
                allpairs.append(p)
        print("ADJACENCY", coordinate, flush=True)
    pairs = np.unique(np.concatenate(allpairs), axis=0) if allpairs else np.empty((0, 2), dtype=np.uint64)
    region_pairs = np.unique(np.concatenate(all_region_pairs), axis=0) if all_region_pairs else np.empty((0, 2), dtype=np.uint64)
    np.save(root / "edges.npy", pairs)
    np.save(root / "region_adjacency.npy", region_pairs)
    pair_aff = None
    if agg is not None:
        def labels_of(atoms):
            return index[np.searchsorted(index[:, 0], atoms), 1]
        pair_aff = pair_affinity(agg, labels_of(pairs[:, 0]), labels_of(pairs[:, 1]), default)
        # Affinity of every contact, joined or not: the candidate-merge list for proofreading.
        np.save(root / "region_affinity.npy",
                pair_affinity(agg, labels_of(region_pairs[:, 0]), labels_of(region_pairs[:, 1]), default))
    atomic_chunks = cg.get_chunk_ids_from_node_ids(index[:, 0])
    left_chunks = cg.get_chunk_ids_from_node_ids(pairs[:, 0])
    right_chunks = cg.get_chunk_ids_from_node_ids(pairs[:, 1])
    for coordinate in chunk_coordinates(counts):
        cid = cg.get_chunk_id(layer=1, x=coordinate[0], y=coordinate[1], z=coordinate[2])
        ids = index[atomic_chunks == cid, 0]
        in_mask = (left_chunks == cid) & (right_chunks == cid)
        out_l, out_r = (left_chunks == cid) & (right_chunks != cid), (right_chunks == cid) & (left_chunks != cid)
        internal = pairs[in_mask]
        cross = np.concatenate([pairs[out_l], pairs[out_r][:, ::-1]])
        edges = {kind: Edges([], []) for kind in EDGE_TYPES}
        if pair_aff is not None:
            in_aff, cross_aff = pair_aff[in_mask], np.concatenate([pair_aff[out_l], pair_aff[out_r]])
        else:
            affinity = np.inf if mode == "source_labels" else 1.0
            in_aff, cross_aff = np.full(len(internal), affinity, np.float32), np.full(len(cross), affinity, np.float32)
        edges[EDGE_TYPES.in_chunk] = Edges(internal[:, 0], internal[:, 1], affinities=in_aff)
        edges[EDGE_TYPES.between_chunk] = Edges(cross[:, 0], cross[:, 1], affinities=cross_aff)
        put_chunk_edges("file:///data/edges", np.array(coordinate), edges, compression_level=3)
        add_atomic_edges(cg, np.array(coordinate), edges, isolated=ids)
        print("INGESTED", coordinate, "nodes", len(ids), flush=True)
    for layer in range(3, cg.meta.layer_count + 1):
        previous = np.ceil(counts / (2 ** (layer - 3))).astype(int)
        current = np.ceil(counts / (2 ** (layer - 2))).astype(int)
        for coordinate in chunk_coordinates(current):
            children = np.array([p for p in itertools.product(*(range(v * 2, v * 2 + 2) for v in coordinate)) if np.all(np.array(p) < previous)])
            add_layer(cg, layer, np.array(coordinate), children_coords=children, n_threads=1)
            print("PARENT", layer, coordinate, flush=True)
    roots = cg.get_roots(index[:, 0])
    if not np.all(roots > 0):
        raise RuntimeError("Some atoms have no native root")
    extra = {}
    if agg is not None:
        groups = label_groups(agg, index[:, 1])
        mapping = np.unique(np.stack([roots, groups], axis=1), axis=0)
        if len(mapping) != len(np.unique(roots)):
            raise RuntimeError("Initial ingestion joined different agglomeration segments")
        grouped = mapping[mapping[:, 1] < ungrouped_from(agg)]
        _, per_segment = np.unique(grouped[:, 1], return_counts=True)
        # A segment whose supervoxels do not touch inside this volume starts as several roots (e.g. at a crop edge).
        extra = {"initial_segments": int(len(per_segment)), "segments_split_into_several_roots": int((per_segment > 1).sum()),
                 "ungrouped_supervoxel_roots": int((mapping[:, 1] >= ungrouped_from(agg)).sum()),
                 "joined_edges_without_rg_affinity": int(np.sum(pair_aff == np.float32(default)))}
        np.save(root / "initial_root_to_segment.npy", mapping)
    else:
        mapping = np.unique(np.stack([roots, index[:, 1]], axis=1), axis=0)
        if len(mapping) != len(np.unique(roots)):
            raise RuntimeError("Initial ingestion joined different source labels")
        np.save(root / "initial_root_to_source_label.npy", mapping)
    np.save(root / "initial_roots.npy", roots)
    save(root / "meshes/info", {"@type": "neuroglancer_legacy_mesh"})
    save(root / "ingested.json", {"graph": spec["graph_id"], "layers": cg.meta.layer_count,
        "atoms": len(index), "edges": len(pairs), "region_adjacency_edges": len(region_pairs),
        "atom_policy": spec.get("atom_policy", "source_labels"), "original_labels": len(np.unique(index[:, 1])),
        "initial_roots": len(mapping), "initial_root_label_consistency_verified": True, **extra,
        "completed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    (root / "ingesting.json").unlink()


if __name__ == "__main__":
    manifest = json.loads(Path("/workspace/deployment.json").read_text())
    if os.environ.get("PCG_GRAPH_IDS") != manifest["graph_id"]:
        raise RuntimeError("PCG_GRAPH_IDS does not match this deployment's graph")
    prepare(manifest)
    ingest(manifest)
