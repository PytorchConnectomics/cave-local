"""Native Spelunker acceptance. Edits only designated cave-pkg-* fixtures.

Records real browser requests, screenshots and a Playwright trace. The two
assigned points are merged/split through mouse/keyboard controls, then their
root membership is checked independently through the graph API and after reload.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from playwright.sync_api import sync_playwright


def validate_viewer_build(build, root=None):
    root = Path(root or Path(__file__).resolve().parents[1])
    lock = json.loads((root / 'upstream.lock.json').read_text())
    overlay_bytes = (root / 'config/viewer-dependencies.lock.json').read_bytes()
    overlay = json.loads(overlay_bytes)
    expected = {
        'commit': lock['neuroglancer']['revision'],
        'source_modified': False,
        'dependency_lock_modified': True,
        'modified': True,
        'upstream_package_lock_sha256': overlay['upstream_package_lock_sha256'],
        'package_lock_sha256': overlay['package_lock_sha256'],
        'dependency_overlay_sha256': hashlib.sha256(overlay_bytes).hexdigest(),
        'dependency_versions': {name: item['version'] for name, item in overlay['replacements'].items()},
    }
    for key, value in expected.items():
        if build.get(key) != value:
            raise RuntimeError(f'Served viewer {key} differs from the pinned source and reviewed dependency overlay')


def validate_target(dest, spec, scenario):
    compose = yaml.safe_load((dest / 'compose.yml').read_text())
    project = compose.get('name', '')
    if not project.startswith('cave-pkg-'):
        raise ValueError('Browser edit acceptance is restricted to cave-pkg-* disposable deployments')
    if project != spec.get('project_name'):
        raise ValueError('Disposable project name and deployment metadata must agree')
    if not spec['graph_id'].startswith('pkg_'):
        raise ValueError('Browser edits require a pkg_* fixture graph ID')
    if spec.get('test_fixture') is not True:
        raise ValueError('Browser edits require a generated deployment marked test_fixture=true')
    parsed = urlparse(spec['local_url'])
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path not in ('', '/'):
        raise ValueError('Browser edit acceptance requires a loopback HTTP deployment URL')
    if len(scenario['atoms']) != 2 or len(scenario['positions_xyz']) != 2:
        raise ValueError('Acceptance requires exactly two assigned fixture points')
    if scenario.get('initial_relation', 'joined') not in ('joined', 'separate'):
        raise ValueError('Invalid fixture relationship')
    return project


def metadata_probe(url, origin):
    path = urlparse(url).path
    return url.startswith(origin + '/image/') and (
        path.endswith(('/.zattrs', '/.zarray', '/.zgroup', '/zarr.json'))
    )


def request_allowed(url, origin, graph):
    parsed = urlparse(url)
    if parsed.scheme in ('http', 'https') and not url.startswith(origin + '/'):
        return False
    if '/segmentation/' in parsed.path or '/meshing/' in parsed.path:
        return re.search(r'/table/' + re.escape(graph) + r'(?:/|$)', parsed.path) is not None
    return True


def source_url(layer):
    source = layer['source']
    if isinstance(source, str):
        return source
    if isinstance(source, list):
        source = source[0]
    return source['url']


def verify(dest, out, executable=None, headed=False):
    spec = json.loads((dest / 'deployment.json').read_text())
    scenario = json.loads((dest / 'data/scenario.json').read_text())
    project = validate_target(dest, spec, scenario)
    origin = spec['local_url'].rstrip('/')
    graph = spec['graph_id']
    base = origin + '/segmentation/api/v1/table/' + graph
    report = {'passed': False, 'entry_url': origin, 'project': project, 'graph': graph,
              'operations': [], 'page_errors': [], 'request_failures': [], 'http_errors': [],
              'blocked_requests': [], 'graph_requests': []}
    out.mkdir(parents=True, exist_ok=True)

    def roots():
        response = requests.post(base + '/roots?int64_as_str=true', json={'node_ids': scenario['atoms']}, timeout=60)
        response.raise_for_status()
        return response.json()['root_ids']

    initial = roots()
    separate = scenario.get('initial_relation') == 'separate'
    if (initial[0] != initial[1]) != separate:
        raise RuntimeError('Fixture no longer has its assigned initial membership; inspect it before retesting')
    report['initial_roots'] = initial
    # API acceptance may have replaced root IDs while restoring membership.
    # Launch the browser with current roots instead of stale generated state.
    scenario['current_roots'] = initial
    (dest / 'data/scenario.json').write_text(json.dumps(scenario, indent=2) + '\n')
    from configure_iteration_viewer import configure
    configure(dest)
    with sync_playwright() as playwright:
        executable = executable or os.environ.get('CAVE_CHROMIUM_EXECUTABLE')
        if not executable and not Path(playwright.chromium.executable_path).is_file():
            executable = shutil.which('chromium-browser') or shutil.which('chromium') or shutil.which('google-chrome')
        options = {'headless': not headed, 'args': ['--use-angle=swiftshader', '--enable-unsafe-swiftshader']}
        if executable:
            options['executable_path'] = executable
        browser = playwright.chromium.launch(**options)
        report['browser'] = browser.version
        context = browser.new_context(viewport={'width': 1550, 'height': 1050})
        context.set_default_timeout(60000)
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page()
        page.on('pageerror', lambda error: report['page_errors'].append(str(error)))
        context.on('requestfailed', lambda request: report['request_failures'].append({'url': request.url, 'failure': request.failure}))

        def guard(route):
            request = route.request
            if not request_allowed(request.url, origin, graph):
                report['blocked_requests'].append({'method': request.method, 'url': request.url})
                route.abort()
            else:
                route.continue_()

        context.route('**/*', guard)
        images, segmentation, meshes = [], [], []

        def response(response):
            url = response.url
            if any(part in url for part in ('/segmentation/', '/meshing/', '/data/meshes/')):
                report['graph_requests'].append({'url': url, 'status': response.status})
            if response.status >= 400:
                report['http_errors'].append({'url': url, 'status': response.status})
            if response.status == 200:
                if url.endswith('.drc'):
                    meshes.append(url)
                if '/image/' in url and not metadata_probe(url, origin):
                    images.append(url)
                if '/data/atomic/' in url and not url.endswith('/info'):
                    segmentation.append(url)

        context.on('response', response)

        def shot(name):
            page.screenshot(path=str(out / (name + '.png')))
            print('Browser checkpoint:', name, flush=True)

        def point(xyz):
            box = page.locator('.neuroglancer-rendered-data-panel').first.bounding_box()
            state = page.evaluate('viewer.state.toJSON()')
            return (box['x'] + box['width'] / 2 + (xyz[0] - state['position'][0] + 0.5) / state['crossSectionScale'],
                    box['y'] + box['height'] / 2 + (xyz[1] - state['position'][1] + 0.5) / state['crossSectionScale'])

        def anchor(xyz):
            x, y = point(xyz)
            page.mouse.move(x, y)
            page.wait_for_timeout(800)
            page.keyboard.down('Control')
            try:
                page.mouse.click(x, y)
            finally:
                page.keyboard.up('Control')
            page.wait_for_timeout(500)

        def submit(action):
            title = 'Submit multicut' if action == 'split' else 'Submit merge'
            with page.expect_response(lambda r: r.request.method == 'POST' and urlparse(r.url).path == urlparse(base).path + '/' + action,
                                      timeout=120000) as event:
                page.get_by_title(title, exact=True).click()
            response = event.value
            if response.status != 200:
                raise RuntimeError(f'{action} returned {response.status}: {response.text()}')
            result = response.json()
            report['operations'].append({'action': action, 'url': response.url, 'request': response.request.post_data, 'response': result})
            page.wait_for_timeout(2000)
            return result

        try:
            page.goto(origin, wait_until='domcontentloaded')
            page.wait_for_url(origin + '/viewer/**')
            page.wait_for_function('window.viewer && viewer.layerManager.managedLayers.length >= 2')
            report['client_url'] = page.url.split('#')[0]
            build = requests.get(origin + '/viewer/build-info.json', timeout=30)
            build.raise_for_status()
            report['viewer_build'] = build.json()
            validate_viewer_build(report['viewer_build'])
            deadline = time.monotonic() + 60
            while not (images and segmentation and meshes):
                if time.monotonic() > deadline:
                    raise RuntimeError(f'Viewer did not load image/segmentation/mesh: {len(images)}/{len(segmentation)}/{len(meshes)} requests')
                page.wait_for_timeout(500)
            state = page.evaluate('viewer.state.toJSON()')
            assert source_url(state['layers'][0]) == 'zarr://' + origin + '/image/'
            assert source_url(state['layers'][1]) == 'graphene://' + origin + '/segmentation/table/' + graph
            page.get_by_text('Graph', exact=True).click()
            shot('browser-01-loaded')
            actions = ['merge', 'split'] if separate else ['split', 'merge']
            for step, action in enumerate(actions, start=1):
                x, y = point(scenario['positions_xyz'][0])
                page.mouse.move(x, y)
                page.keyboard.press('Shift+c' if action == 'split' else 'Shift+m')
                page.get_by_title('Submit multicut' if action == 'split' else 'Submit merge', exact=True).wait_for()
                if action == 'merge':
                    auto = page.get_by_title('auto-submit merges', exact=True).locator('input')
                    if auto.is_checked():
                        auto.uncheck()
                anchor(scenario['positions_xyz'][0])
                if action == 'split':
                    page.keyboard.press('g')
                anchor(scenario['positions_xyz'][1])
                if action == 'split':
                    points = page.evaluate('viewer.layerManager.managedLayers[1].layer.graphConnection.value.state.multicutState.toJSON()')
                    assert len(points['sources']) == len(points['sinks']) == 1, points
                shot(f'browser-step-{step}-{action}-seeds')
                submit(action)
                current = roots()
                assert (current[0] == current[1]) == (action == 'merge'), current
                shot(f'browser-step-{step}-{action}-complete')
                page.keyboard.press('Escape')
            final = roots()
            assert (final[0] != final[1]) == separate, final
            page.wait_for_timeout(1500)
            saved_url = page.url
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function('window.viewer && viewer.layerManager.managedLayers.length >= 2')
            page.wait_for_timeout(2500)
            assert roots() == final
            reload_state = page.evaluate('viewer.state.toJSON()')
            assert set(final).issubset(set(map(str, reload_state['layers'][1].get('segments', [])))), reload_state
            shot('browser-reloaded')
            fresh = context.new_page()
            fresh.on('pageerror', lambda error: report['page_errors'].append(str(error)))
            fresh.goto(saved_url, wait_until='domcontentloaded')
            fresh.wait_for_function('window.viewer && viewer.layerManager.managedLayers.length >= 2')
            fresh.wait_for_timeout(2500)
            assert roots() == final
            fresh_state = fresh.evaluate('viewer.state.toJSON()')
            assert set(final).issubset(set(map(str, fresh_state['layers'][1].get('segments', [])))), fresh_state
            fresh.close()
            expected = scenario['component_atoms_by_point'] if separate else [scenario['component_atoms']]
            for root, component in zip(dict.fromkeys(final), expected):
                leaves = requests.get(base + '/node/' + root + '/leaves?int64_as_str=true', timeout=60)
                leaves.raise_for_status()
                assert set(leaves.json()['leaf_ids']) == set(component), leaves.json()
            assert not report['page_errors'], report['page_errors']
            assert not report['blocked_requests'], report['blocked_requests']
            unexpected = [e for e in report['http_errors'] if e['status'] != 404 or not metadata_probe(e['url'], origin)]
            assert not unexpected, unexpected
            unexpected_failures = [e for e in report['request_failures'] if e['failure'] != 'net::ERR_ABORTED']
            assert not unexpected_failures, unexpected_failures
            report.update(passed=True, final_roots=final, component_membership_restored=True,
                          image_tiles=images, segmentation_tiles=segmentation, mesh_requests=meshes,
                          reload_verified=True, fresh_tab_verified=True)
            scenario['current_roots'] = final
            (dest / 'data/scenario.json').write_text(json.dumps(scenario, indent=2) + '\n')
            configure(dest)
        except Exception as error:
            report['failure'] = str(error)
            raise
        finally:
            try:
                report['last_state'] = page.evaluate('window.viewer ? viewer.state.toJSON() : null')
                report['last_body'] = page.locator('body').inner_text()[:7000]
                page.screenshot(path=str(out / 'browser-last.png'))
            except Exception as error:
                report['capture_error'] = str(error)
            (out / 'acceptance-browser.json').write_text(json.dumps(report, indent=2) + '\n')
            context.tracing.stop(path=str(out / 'browser-trace.zip'))
            browser.close()
    print('Browser acceptance passed:', out / 'acceptance-browser.json')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('output', type=Path, nargs='?', help='Defaults to DEPLOYMENT/reports/browser')
    parser.add_argument('--chromium', help='Explicit browser path; otherwise Playwright or system Chromium')
    parser.add_argument('--headed', action='store_true')
    args = parser.parse_args()
    dest = args.destination.resolve()
    verify(dest, (args.output or dest / 'reports/browser').resolve(), args.chromium, args.headed)
