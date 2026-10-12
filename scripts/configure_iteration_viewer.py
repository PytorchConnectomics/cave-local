"""Configure a deployment to use its pinned, locally built Spelunker client."""
import argparse
import html
import json
from pathlib import Path


def configure(dest, public_url=None):
    dest = Path(dest)
    spec = json.loads((dest / 'deployment.json').read_text())
    graph = spec['graph_id']
    origin = spec['local_url'].rstrip('/')
    scenario_path = dest / 'data/scenario.json'
    scenario = json.loads(scenario_path.read_text()) if scenario_path.exists() else None
    name = spec.get('display_name', graph.replace('_', ' ').title())
    offset = spec.get('voxel_offset_xyz', [0, 0, 0])
    state = {
        'dimensions': {a: [float(r) * 1e-9, 'm'] for a, r in zip('xyz', spec['resolution_nm_xyz'])},
        'position': scenario['view_position_xyz'] if scenario else [o + n / 2 for o, n in zip(offset, spec['shape_xyz'])],
        'crossSectionScale': 0.10 if scenario else 0.5,
        'projectionScale': 120,
        'layers': [
            {'type': 'image', 'name': spec.get('image_name', 'Image'), 'source': 'zarr://' + origin + '/image/',
             'shader': 'void main() { emitRGB(vec3(toNormalized(getDataValue()))); }'},
            {'type': 'segmentation', 'name': name,
             'source': 'graphene://' + origin + '/segmentation/table/' + graph,
             'segments': list(dict.fromkeys(scenario['current_roots'])) if scenario else [],
             'selectedAlpha': 0.45, 'tab': 'graph',
             'toolBindings': {'C': 'grapheneMulticutSegments', 'M': 'grapheneMergeSegments'}},
        ],
        'selectedLayer': {'layer': name, 'visible': True},
        'layout': '4panel',
    }
    web = dest / 'web'
    web.mkdir(parents=True, exist_ok=True)
    (web / 'viewer-state.json').write_text(json.dumps(state, indent=2) + '\n')
    config = {'viewerOrigin': '/viewer/', 'dataOrigin': origin}
    if public_url:
        config['publicDataOrigin'] = public_url.rstrip('/')
    (web / 'official-viewer-config.json').write_text(json.dumps(config, indent=2) + '\n')
    launcher = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Opening CAVE Spelunker</title><p id="status">Loading the proofreading view…</p>
<script>
Promise.all([fetch('/viewer-state.json',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error('Viewer state: '+r.status);return r.json()}),
fetch('/official-viewer-config.json',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error('Viewer config: '+r.status);return r.json()})]).then(([state,config])=>{
  for(const layer of state.layers){
    layer.source=layer.source.replace(config.dataOrigin,location.origin);
  }
  location.replace(config.viewerOrigin+'#!'+encodeURIComponent(JSON.stringify(state)));
}).catch(e=>document.querySelector('#status').textContent=e.message);
</script></html>'''
    (web / 'upstream.html').write_text(launcher)
    (web / 'index.html').write_text(launcher)
    instructions = f'''{name} — browser proofreading

Open {origin}. It opens the pinned Spelunker client with reviewed dependencies, served by
this deployment at /viewer/. Image and graph requests use this deployment's
address. Local installation needs no public tunnel or hosted viewer service.

Navigation: drag to pan, scroll to move through slices, Ctrl-scroll to zoom.
Double-click foreground to select its current graph component and show its mesh.
Double-click again to deselect; selection does not change the graph.

Merge: hover a 2D pane and press Shift+M. Ctrl-click a foreground point in each
component. Turn off auto-submit to place both points before committing.
Click Submit merge, then check that the points have the same root ID.

Split: hover a 2D pane and press Shift+C. Ctrl-click foreground on one side, press
G to switch groups, then Ctrl-click foreground on the other side. Click
Submit multicut, then check that the points have different root IDs.
Split separates existing supervoxels; it does not paint new voxel boundaries.

With source_labels, each supplied source label remains indivisible. Internal
graph chunk fragments of that label are linked with uncuttable edges. Dividing
a source label requires finer labels from upstream. Ordinary proofreading can
merge source labels into an object and later split those labels apart again.

Edits persist in this deployment's graph database; reloading does not undo them.
Save the viewer URL to retain your position and selected segments. Back up the
deployment's databases before making edits you need to preserve. This local
preview uses a shared editor identity and loopback-only access; remote access
requires the documented access setup.
'''
    if scenario:
        instructions += f'''
Disposable acceptance fixture:
A XYZ voxels: {scenario['positions_xyz'][0]}, atomic ID {scenario['atoms'][0]}.
B XYZ voxels: {scenario['positions_xyz'][1]}, atomic ID {scenario['atoms'][1]}.
Initial relationship: {scenario.get('initial_relation', 'joined')}.
Test merge and split using only the assigned points. Verify image, segmentation,
and mesh loading, then reload and open the saved URL in a fresh tab. Compare
component membership rather than old root IDs, which change after edits.
Only run automated edit acceptance on a designated cave-pkg-* test deployment.
'''
    (web / 'proofreader-instructions.txt').write_text(instructions)
    title = html.escape(name)
    (web / 'tools.html').write_text('''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>''' + title + '''</title>
<style>body{font:18px system-ui;background:#10202b;color:#e4f1f7;max-width:850px;margin:70px auto;padding:0 24px}a{color:#9bd9ff}p{line-height:1.6}.button{display:inline-block;background:#14745d;padding:16px 22px;border-radius:8px;color:white;text-decoration:none}</style>
<h1>''' + title + '''</h1><p>Image and editable segmentation.</p>
<p><a class="button" href="/upstream.html">Open Spelunker</a></p>
<p>Double-click a component to select it. Hover a 2D view and press <b>Shift+C</b> for Split or <b>Shift+M</b> for Merge. Place endpoints with Ctrl-click, then Submit. In Split, <b>G</b> switches seed groups.</p>
<p><a href="/proofreader-instructions.txt">Proofreading and test instructions</a></p>
<p>Saved edits belong to this deployment. Split follows the configured supervoxel policy.</p>
</html>''')
    print('Configured local Spelunker for', graph)
    return state


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--public-url', help='Record an optional access address; the client remains local')
    args = parser.parse_args()
    configure(args.destination.resolve(), args.public_url)
