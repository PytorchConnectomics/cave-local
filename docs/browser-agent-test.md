# Repeat the test with your own browser agent

Automated acceptance drives real Chromium controls. For a test in your own
browser, generate instructions from a quiescent disposable fixture:

```bash
bin/cave browser-handoff DEPLOYMENT
```

To use the bundled automated browser check, first launch an available browser
after `bin/cave deps`:

```bash
.venv/bin/python scripts/check_browser.py
bin/cave validate DEPLOYMENT --edit-fixture --browser
```

When no browser is installed on a Playwright-supported OS, provision one with
`.venv/bin/python -m playwright install --with-deps chromium` and rerun the
preflight. Linux browser libraries may need administrator access; the pinned
installer rejects some older distributions, including Ubuntu 20. To select an
existing working browser explicitly, set `CAVE_CHROMIUM_EXECUTABLE` to its
executable/launcher path and run the preflight. Preserve launcher paths such as
`/snap/bin/chromium`; do not substitute their symlink targets.

If provisioning is unsupported or no suitable browser is available, run
`bin/cave validate DEPLOYMENT --edit-fixture` and complete
the generated handoff in a working local or in-app browser instead. Record
which browser was actually used; do not report the automated check as passed.

Run complete acceptance workflows sequentially. Keep other Docker deployment
start/stop/recreate jobs paused while the browser is testing. A development run
recorded `ERR_NETWORK_CHANGED` during another stack's lifecycle work; that run
remained failed. A browser lock alone does not serialize those other jobs.

Give your local agent the printed `/agent-browser-test.txt` URL and this prompt:

> Read the instructions at that URL and use your browser tools to perform the
> assigned CAVE test. Use actual viewer controls for editing, collect the
> requested evidence, and report each check separately. Do not edit another
> graph or infer success merely from the page loading. Finish with one
> self-contained report I can copy-paste to the agent coordinating this setup,
> including screenshots, browser version, errors and any edits left applied.

The instructions name the exact viewer, graph, XYZ test coordinates, atom IDs,
starting relationship, Shift+M/Shift+C controls, a read-only root lookup and
expected results after each step. They require restoring original membership
and checks after reload and opening a fresh tab.

Generate a new handoff after earlier acceptance runs; old root IDs can expire.
The generator refuses a fixture left mid-edit. Do not mark real data disposable.

For a browser on another computer, use the SSH forwarding in
[operations](operations.md). Opening a Codex browser tab alone does not prove
the data or editing controls work. Keep screenshots and the agent's actual
test report with the acceptance evidence.

## Check your real data without editing it

The generated merge/split handoff is restricted to disposable fixtures. For
your own dataset, open the URL from `bin/cave start DEPLOYMENT` and give the
browser agent this task with several representative XYZ coordinates selected
from your metadata:

> Inspect this CAVE viewer without submitting edits. At each supplied XYZ
> coordinate, verify that the image and segmentation refer to the same tissue
> and the selected object's mesh aligns with its segmentation. Check several
> slices and a location near a graph chunk boundary. Reload the viewer and
> open the saved URL in a fresh tab. Save screenshots and a self-contained
> pass/fail report with browser version, requested coordinates, console/network
> errors and any unsupported behavior. Do not use Merge, Split or annotation
> submission, and do not treat a loaded page alone as proof of alignment.
