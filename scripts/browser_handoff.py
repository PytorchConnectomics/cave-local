"""Write exact browser-only agent instructions for a disposable installation."""
import argparse
import json
from pathlib import Path
import numpy as np
from snapshot import api_roots
from verify_segmentation_iteration import require_disposable


def write(dest):
    dest = Path(dest).resolve()
    spec = json.loads((dest / 'deployment.json').read_text())
    require_disposable(dest, spec)
    scenario = json.loads((dest / 'data/scenario.json').read_text())
    roots = api_roots(spec, np.asarray(scenario['atoms'], np.uint64)).astype(str).tolist()
    separate = scenario.get('initial_relation') == 'separate'
    if (roots[0] != roots[1]) != separate:
        raise ValueError('Assigned fixture is mid-edit; inspect before issuing new browser test instructions')
    scenario['current_roots'] = roots
    (dest / 'data/scenario.json').write_text(json.dumps(scenario, indent=2) + '\n')
    from configure_iteration_viewer import configure
    configure(dest)
    origin = spec['local_url'].rstrip('/')
    order = 'Merge A and B, then Split them apart' if separate else 'Split A from B, then Merge them back together'
    expected = 'different root IDs' if separate else 'the same root ID'
    text = f'''Use your browser tools to test this disposable CAVE installation.

Viewer: {origin}/
Graph: {spec['graph_id']}
A XYZ voxels: {scenario['positions_xyz'][0]}; atomic ID {scenario['atoms'][0]}
B XYZ voxels: {scenario['positions_xyz'][1]}; atomic ID {scenario['atoms'][1]}
XYZ voxel spacing, nanometers: {spec['resolution_nm_xyz']}
Initial relationship: {scenario.get('initial_relation', 'joined')}
Root IDs when instructions were generated: {roots}

Edit only these assigned points in this synthetic test graph. If the viewer,
graph or starting relationship differs, stop and report it. Do not change a
real proofreading installation, SSH settings or databases.

1. Open the viewer. Wait for image and colored segmentation. Inspect the
   selected objects' 3D meshes and alignment. Save a screenshot, browser version
   and any console errors.
2. Confirm the assigned atoms have {expected}. If browser evaluation is
   available, this same-origin lookup is read-only:

   await fetch('{origin}/segmentation/api/v1/table/{spec['graph_id']}/roots?int64_as_str=true', {{
     method: 'POST', headers: {{'Content-Type': 'application/json'}},
     body: JSON.stringify({{'node_ids': {scenario['atoms']}}})
   }}).then(r => {{if (!r.ok) throw Error(r.status); return r.json()}})

   Keep IDs as strings; JavaScript numbers lose their precision.
3. {order}, using actual mouse/keyboard controls for both:
   MERGE: hover a 2D pane; Shift+M; disable auto-submit; Ctrl-click A and B;
   click Submit merge. Repeat the read-only lookup and require one root.
   SPLIT: select the joined object; hover a 2D pane; Shift+C; Ctrl-click A;
   G; Ctrl-click B; click Submit multicut. Require two roots.
   The listed XYZ points are voxel coordinates, not nanometers. Do not replace
   browser editing with direct API edit calls. Inspect mesh changes and save
   screenshots/results after each edit. On failure, inspect current state;
   do not blindly repeat submissions.
4. Finish with the original relationship ({scenario.get('initial_relation', 'joined')}).
   Reload, then open the saved viewer URL in a fresh tab. Require the atoms
   still have {expected}, with image, segmentation and meshes loaded.
   Root numbers change after edits; compare membership, not old root numbers.
5. Report PASS/FAIL separately for alignment, segmentation, meshes, native
   Merge, native Split, reload and fresh tab. Include browser version,
   before/intermediate/final IDs, screenshots and failures. Identify any edit
   left applied. Metadata detection may return 404 for absent Zarr metadata
   probes; other errors need investigation.
'''
    output = dest / 'web/agent-browser-test.txt'
    output.write_text(text)
    print('Browser agent prompt:', output)
    print('Browser-readable prompt:', origin + '/agent-browser-test.txt')
    return output


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('deployment', type=Path)
    write(p.parse_args().deployment)
