# Open and verify the viewer

Each deployment serves pinned Spelunker source with reviewed dependency updates at its own `/viewer/`
address. Opening the deployment's root URL loads its image and graph there.
The default does not use a public hosted viewer or require a tunnel.

The upstream source revision is pinned in `upstream.lock.json`, and the exact
dependency-lock overlay is in `config/viewer-dependencies.lock.json`.
Run `bin/cave fetch`, then `bin/cave build` to fetch dependencies and build the
client. The viewer build requires Node >=22.18 and npm. The build uses `npm ci`
and `npm run build -- --output …` in an isolated checkout; it refuses a modified
upstream source checkout and a lock that differs from the reviewed hashes.
Generated files live under `.runtime/viewer` and include `build-info.json`
with the exact upstream revision, original/effective lock hashes, overlay hash,
dependency versions and Node/npm versions. Viewer code remains upstream;
the dependency-adjusted build is explicitly recorded as modified.
The build fetches public GitHub npm dependencies over HTTPS, including a
dependency recorded upstream as SSH; it changes neither your Git settings nor
the original vendor lockfile, and requires no personal GitHub SSH key.
See [the dependency review](dependencies.md) for fixes and retained findings.

Image requests use `/image/`; the gateway serves the configured local image or
proxies the configured HTTP image source. Graph requests use
`/segmentation/table/<graph_id>`. The launcher changes both addresses to the
address opened in the browser, so the same state works on the deployment's
configured local address. Remote access must follow the access instructions.

## Proofread

Double-click a foreground object to select it and display its mesh. Hover a 2D
pane and press **Shift+M** to merge, or **Shift+C** to split. Use Ctrl-click for the points.
Turn off auto-submit when testing a merge. For a split, press **G** between the
two points to put them in different groups. Click **Submit** and inspect the
result. The deployment also serves `/proofreader-instructions.txt`.

Split separates existing supervoxels. With `source_labels`, it cannot divide
one supplied source label. Labels that were merged together can be separated.
To divide an individual label, prepare finer segmentation labels upstream.
Reloading the viewer preserves committed edits.

## Run actual browser acceptance

Install the Python dependencies and a Chromium browser. Use either the browser
installed by `.venv/bin/python -m playwright install chromium`, or an explicit
system browser path through `--chromium` or `CAVE_CHROMIUM_EXECUTABLE`.

```bash
.venv/bin/python scripts/verify_iteration_browser.py \
  /absolute/path/to/disposable-deployment \
  /absolute/path/to/browser-report
```

This command **edits its assigned fixture**. It refuses projects other than
`cave-pkg-*`, verifies that the project matches its deployment metadata and is
marked as a generated `test_fixture`, and only permits loopback URLs.
All browser HTTP traffic is restricted to that deployment, and
graph requests to its graph. Never rename a working deployment just to bypass
these checks.

The deployment must already have a generated `data/scenario.json` with two
assigned foreground points and their expected component membership. The check
waits for successful image tiles, segmentation tiles, and Draco mesh requests.
It then uses the native keyboard/mouse controls for a merge and split, checks
the resulting roots through the API, restores the initial component membership,
reloads, and opens a fresh tab. Root IDs change during edits; component
membership is the acceptance criterion.

Results include `acceptance-browser.json`, screenshots, and `browser-trace.zip`.
Keep these under `.runtime` or outside the checkout. A passing report records
the actual browser version, viewer build, edit requests/responses, final roots,
and loaded data URLs. JavaScript errors, blocked requests, unexpected HTTP
errors, or missing tiles fail acceptance. Zarr metadata detection can probe
files that do not exist; only metadata 404s are allowed.

If a run fails midway, inspect the report and current graph roots before
rerunning. It may have committed its first edit. Restore the disposable fixture
or recreate that test deployment; do not run blind corrective edits on real
proofreading data.
