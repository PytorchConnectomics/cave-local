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
from supervoxel_history import load_atom_tables, snapshot_read
from supervoxel_refine import recover_if_idle
LOCK=threading.Lock()
def index():return load_atom_tables().index
def bounds():return load_atom_tables().bounds
@lru_cache(maxsize=1)
def geometry():
    import json
    return json.loads(Path('/data/prepared.json').read_text())
def manifest(table_id,node_id):
    name=str(node_id)+'.drc';p=Path('/data/meshes')/name;p.parent.mkdir(exist_ok=True)
    with LOCK:
        if not p.exists():
            def read_mask():
                cg=get_cg(table_id,skip_cache=True)
                leaves=cg.get_subgraph_leaves(np.uint64(node_id))
                if len(leaves)==0:return None
                tables=load_atom_tables();ix=tables.index;pos=np.searchsorted(ix[:,0],leaves)
                assert np.all(pos<len(ix)) and np.all(ix[pos,0]==leaves)
                bb=tables.bounds[pos].astype(int);lo=np.maximum(bb[:,:3].min(axis=0)-1,0);hi=np.minimum(bb[:,3:].max(axis=0)+1,geometry()['shape_xyz'])
                if np.prod(hi-lo)>200_000_000:return 'too_large'
                cv=CloudVolume('file:///data/atomic',fill_missing=True,cache=False)
                arr=cv[tuple(slice(int(x),int(y)) for x,y in zip(lo,hi))][...,0]
                return lo,np.asfortranarray(np.isin(arr,leaves).astype(np.uint8))
            observed=snapshot_read(read_mask,recover=lambda: recover_if_idle(lambda: get_cg(table_id,skip_cache=True)))
            if observed is None:return jsonify({'fragments':[]})
            if isinstance(observed,str):return jsonify({'error':'Selected root exceeds local on-demand meshing limit; use sharded L2 workers.'}),413
            lo,mask=observed
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
