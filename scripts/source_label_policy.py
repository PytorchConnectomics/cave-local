"""Reject cuts through an immutable source supervoxel before graph editing.

The native graph still performs every valid edit. This guard explains the
source-label policy instead of exposing a native cross-chunk cutting error.
"""
import json
from functools import lru_cache
from pathlib import Path

import numpy as np
from flask import request


def install(common):
    from pychunkedgraph.app import app_utils
    from pychunkedgraph.graph.exceptions import BadRequest

    original_split = common.handle_split

    @lru_cache(maxsize=1)
    def index():
        return np.load('/data/atom_index.npy', mmap_mode='r')

    def split(table_id):
        prepared = Path('/data/prepared.json')
        if prepared.exists() and json.loads(prepared.read_text()).get('atom_policy') in ('source_labels', 'agglomerated_supervoxels'):
            geometry = json.loads(prepared.read_text())
            if geometry['spec']['graph_id'] != table_id:
                raise BadRequest('Source-label metadata belongs to another graph.')
            # Spelunker sends a JSON body without application/json, matching
            # the native handler's raw-body parsing.
            data = json.loads(request.data)
            sources, sinks = data.get('sources', []), data.get('sinks', [])
            if sources and sinks:
                graph = app_utils.get_cg(table_id, skip_cache=True)
                seeds = sources + sinks
                ids = np.array([seed[0] for seed in seeds], dtype=np.uint64)
                coordinates = np.array([seed[1:] for seed in seeds]) / graph.segmentation_resolution
                atoms = app_utils.handle_supervoxel_id_lookup(graph, coordinates, ids)
                mapping = index()
                positions = np.searchsorted(mapping[:, 0], atoms)
                if np.any(positions >= len(mapping)) or np.any(mapping[positions, 0] != atoms):
                    raise BadRequest('Split seeds could not be resolved to source supervoxels.')
                labels = mapping[positions, 1]
                if set(labels[:len(sources)]) & set(labels[len(sources):]):
                    raise BadRequest(
                        'Both split groups contain the same source supervoxel. '
                        'Source labels are indivisible; choose different source labels to separate a merged segment.'
                    )
        return original_split(table_id)

    common.handle_split = split
