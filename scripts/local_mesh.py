"""On-demand Draco meshes for the local crop using upstream PCG leaves.

Native graph editing and history remain PCG. Production scale should replace
this bounded root mesher with PCG's sharded L2 meshing workers.
"""
from pathlib import Path
from functools import lru_cache
import os,threading,tempfile
import numpy as np
import fastremap,zmesh,DracoPy
from scipy import ndimage
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
@lru_cache(maxsize=1)
def lowres():
    # Optional data/atomic_ds (a coarser copy of the supervoxel layer, info carries stride_xyz): whole-cube roots exceed the
    # full-resolution limit below. Each coarse voxel holds its block's most frequent supervoxel at the time of the copy.
    p=Path('/data/atomic_ds/info')
    if not p.exists():return None
    import json
    i=json.loads(p.read_text());return np.array(i['stride_xyz'],int),np.array(i['scales'][0]['resolution'],float),np.array(i['scales'][0]['size'],int)
def block_mode(a,f):
    # Most frequent non-zero label per f-block (as cave_atomic_downsample.py builds atomic_ds); axes are multiples of f.
    X,Y,Z=(n//s for n,s in zip(a.shape,f))
    b=a.reshape(X,f[0],Y,f[1],Z,f[2]).transpose(0,2,4,1,3,5).reshape(-1,int(np.prod(f)))
    cnt=np.zeros(b.shape,np.int16)
    for j in range(b.shape[1]):cnt+=b==b[:,j:j+1]
    cnt[b==0]=-1
    return b[np.arange(len(b)),cnt.argmax(1)].reshape(X,Y,Z)
def member(a,ids):
    # np.isin sorts the whole volume (~11 s for 137M voxels); fastremap's hash lookup gives the same mask in ~0.4 s.
    return fastremap.mask_except(np.array(a,copy=True),[int(i) for i in ids],in_place=True)!=0
def refresh_refined(arr,lo,f,tables):
    # Refinement relabels one supervoxel's voxels into pieces, so only blocks whose stored mode was a supervoxel retired since
    # the copy can change; recompute those from the current full-resolution labels.
    retired=np.fromiter(tables.retired,np.uint64,len(tables.retired))
    if not len(retired):return arr
    stale=member(arr,retired)
    if not stale.any():return arr
    full=CloudVolume('file:///data/atomic',fill_missing=True,cache=False);shape=np.array(full.bounds.maxpt)
    # One labelling pass finds every stale atom's box (scanning the volume once per atom took ~28 s for 84 atoms).
    atoms=np.unique(arr[stale]);lab=np.zeros(arr.shape,np.int32);lab[stale]=np.searchsorted(atoms,arr[stale])+1
    for atom,sl in zip(atoms,ndimage.find_objects(lab)):
        if sl is None:continue
        a,b=np.array([x.start for x in sl]),np.array([x.stop for x in sl])
        f0,f1=(lo+a)*f,np.minimum((lo+b)*f,shape)
        cut=full[tuple(slice(int(x),int(y)) for x,y in zip(f0,f1))][...,0]
        cut=np.pad(cut,[(0,(-n)%k) for n,k in zip(cut.shape,f)])
        mode=block_mode(cut,f);box=arr[tuple(slice(int(x),int(y)) for x,y in zip(a,b))]
        hit=box==atom;box[hit]=mode[:box.shape[0],:box.shape[1],:box.shape[2]][hit]
    return arr
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
                url,resolution='file:///data/atomic',np.array(geometry()['resolution_nm_xyz'])
                ds=lowres();coarse=ds is not None and np.prod(hi-lo)>20_000_000
                # Small roots stay at full resolution (fast, and tiny pieces do not vanish in the coarse copy).
                if coarse:
                    f,resolution,size=ds;lo=lo//f;hi=np.minimum(-(-hi//f),size);url='file:///data/atomic_ds'
                if np.prod(hi-lo)>200_000_000:return 'too_large'
                cv=CloudVolume(url,fill_missing=True,cache=False)
                arr=cv[tuple(slice(int(x),int(y)) for x,y in zip(lo,hi))][...,0]
                if coarse:arr=refresh_refined(arr,lo,f,tables)
                return lo,resolution,np.asfortranarray(member(arr,leaves).astype(np.uint8))
            observed=snapshot_read(read_mask,recover=lambda: recover_if_idle(lambda: get_cg(table_id,skip_cache=True)))
            if observed is None:return jsonify({'fragments':[]})
            if isinstance(observed,str):return jsonify({'error':'Selected root exceeds local on-demand meshing limit; use sharded L2 workers.'}),413
            lo,resolution,mask=observed
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
