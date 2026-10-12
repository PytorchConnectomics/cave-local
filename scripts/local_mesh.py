"""On-demand Draco meshes for the local crop using upstream PCG leaves.

Native graph editing and history remain PCG. Production scale should replace
this bounded root mesher with PCG's sharded L2 meshing workers.
"""
from pathlib import Path
from functools import lru_cache
import os,threading,tempfile
import numpy as np
import zmesh,DracoPy
from flask import jsonify
from cloudvolume import CloudVolume
from pychunkedgraph.app.app_utils import get_cg
LOCK=threading.Lock()
@lru_cache(maxsize=1)
def index():return np.load('/data/atom_index.npy')
@lru_cache(maxsize=1)
def bounds():return np.load('/data/atom_bounds.npy')
@lru_cache(maxsize=1)
def geometry():
    import json
    return json.loads(Path('/data/prepared.json').read_text())
def manifest(table_id,node_id):
    name=str(node_id)+'.drc';p=Path('/data/meshes')/name;p.parent.mkdir(exist_ok=True)
    with LOCK:
        if not p.exists():
            cg=get_cg(table_id,skip_cache=True)
            leaves=cg.get_subgraph_leaves(np.uint64(node_id))
            if len(leaves)==0:return jsonify({'fragments':[]})
            ix=index();pos=np.searchsorted(ix[:,0],leaves)
            # Spatial iteration order matches increasing encoded node IDs.
            assert np.all(ix[pos,0]==leaves)
            bb=bounds()[pos].astype(int);lo=np.maximum(bb[:,:3].min(axis=0)-1,0);hi=np.minimum(bb[:,3:].max(axis=0)+1,geometry()['shape_xyz'])
            shape=hi-lo
            if np.prod(shape)>200_000_000: return jsonify({'error':'Selected root exceeds local on-demand meshing limit; use sharded L2 workers.'}),413
            cv=CloudVolume('file:///data/atomic',fill_missing=True)
            arr=cv[tuple(slice(int(x),int(y)) for x,y in zip(lo,hi))][...,0]
            mask=np.asfortranarray(np.isin(arr,leaves).astype(np.uint8))
            resolution=np.array(geometry()['resolution_nm_xyz'])
            mesher=zmesh.Mesher(resolution);mesher.mesh(mask,close=True)
            if not mesher.ids():return jsonify({'fragments':[]})
            mesh=mesher.get(1,reduction_factor=0);vertices=mesh.vertices.astype(np.float32)+(lo*resolution).astype(np.float32)
            payload=DracoPy.encode(vertices,mesh.faces,quantization_bits=16,compression_level=5)
            # Distinct temporary files keep concurrent workers/replicas safe.
            with tempfile.NamedTemporaryFile(dir=p.parent,prefix=p.name+'.',delete=False) as f:
                # nginx runs as a different uid; tempfile otherwise uses 0600.
                os.fchmod(f.fileno(),0o644)
                tmp=Path(f.name);f.write(payload)
            try:os.replace(tmp,p)
            finally:tmp.unlink(missing_ok=True)
    return jsonify({'fragments':[name]})
