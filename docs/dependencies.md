# Dependency and license review

Reviewed on **12 October 2026** for `v0.1.0` preview. This release supports a
loopback-only, single-editor installation and SSH forwarding to a server.
It is not a reviewed internet-facing CAVE service.

## What changed in the viewer

The pinned Spelunker branch still points to
`d4aeac3b66eb4bd3fa86dbdc0202a7f006439740`; there was no newer supported
upstream revision containing dependency fixes. Its original lock produced
55 npm audit findings: 13 moderate, 34 high and 8 critical. Only three affected
packages are in npm's production dependency graph.

The build replaces those three package artifacts, without changing upstream
TypeScript, shader, worker or viewer UI source:

| Package | Original version | Release build | Reason |
| --- | --- | --- | --- |
| `lodash-es` | 4.17.21 | 4.18.1 | Fix template-import injection and `unset`/`omit` prototype pollution. |
| `valibot` | 1.1.0 | 1.5.0 | Fix emoji-validation ReDoS and malformed record-error flattening. |
| `fflate` | 0.8.2 | 0.8.3 | Fix malformed ZIP64 processing; dependency of the NIfTI reader. |

Primary upstream references: [Lodash template advisory](https://github.com/lodash/lodash/security/advisories/GHSA-r5fr-rjxr-66jc),
[Lodash path advisory](https://github.com/lodash/lodash/security/advisories/GHSA-f23m-r3pf-42rh),
[Valibot emoji advisory](https://github.com/open-circle/valibot/security/advisories/GHSA-vqpr-j7v3-hqw9),
[Valibot record advisory](https://github.com/open-circle/valibot/security/advisories/GHSA-5qjj-4xww-7phc),
and [fflate 0.8.3 release](https://github.com/101arrowz/fflate/releases/tag/v0.8.3).

The existing viewer imports Lodash's debounce, throttle and escapeRegExp,
not its vulnerable template/unset/omit APIs. Its Valibot record/flatten path
does exist in Icechunk metadata handling; the default local installation uses
OME-Zarr and precomputed data. We still update all three production packages
because the built viewer includes additional upstream data-source support.

[The dependency overlay](../config/viewer-dependencies.lock.json) records the
base lock SHA-256, exact replacement versions, npm artifact URLs and SHA-512
integrities, and the effective lock SHA-256. The build refuses a dirty upstream
checkout, a different revision/base lock, an unexpected replacement set, or a
different effective lock. It makes an isolated local Git checkout, applies
only those three lock entries, runs `npm ci`, verifies installed versions and
runs a one-shot build. No automatic `npm audit fix` or dependency resolution
update is applied. The original vendor checkout remains unchanged.

`build-info.json` records `source_modified: false`,
`dependency_lock_modified: true` and `modified: true`, together with both lock
hashes, the overlay hash, dependency versions and Node/npm versions. The browser
acceptance command checks this provenance. The build also retains upstream and
runtime dependency notices beside the generated assets.

## Results and retained findings

| Scope | Original lock | Release build lock |
| --- | --- | --- |
| npm production dependencies (`--omit=dev`) | 3 affected packages | **0 known findings** |
| npm complete build/test graph | 55 affected packages | **52: 12 moderate, 32 high, 8 critical** |
| Host Python `requirements.lock.txt` | 108 pinned distributions reviewed | **0 known findings** in pip-audit 2.10.1 |

The npm totals count affected packages, including packages inheriting another
dependency's advisory; they are not counts of distinct exploitable bugs.
Zero known findings is an advisory database result for these exact locks on
the review date, not a promise that a component has no vulnerabilities.

The eight retained critical package entries are:

| Package entries | Exposure in the supported procedure |
| --- | --- |
| `vitest`, `@vitest/browser`, `@vitest/ui`, `@vitest/web-worker` | Upstream test/UI/Browser Mode servers are not started by this package. Our browser checks use Python Playwright against the installed viewer. |
| `tinypool` | Upstream Vitest worker pool, not invoked by this package. Its worker-option gadget requires a prototype-pollution path; do not treat it as safe for running untrusted upstream tests. |
| `proxy-addr` | Express proxy handling for Node development servers. The deployed viewer is static assets served by nginx. |
| `websocket-driver` | Node development-server WebSocket handling, not a deployed browser/server dependency. |
| `shell-quote` | Node command-line helpers outside the production graph. Build arguments and source come from the reviewed repository, rather than user datasets. |

Upstream descriptions establish the conditions for the
[Vitest Browser API finding](https://github.com/vitest-dev/vitest/security/advisories/GHSA-p63j-vcc4-9vmv),
[Tinypool worker finding](https://github.com/tinylibs/tinypool/security/advisories/GHSA-5gmw-xhrv-c9v3),
[proxy-addr trust finding](https://github.com/jshttp/proxy-addr/security/advisories/GHSA-jqcg-44mw-7w3h)
and [WebSocket compression finding](https://github.com/faye/websocket-driver-node/security/advisories/GHSA-mp7j-qc5w-4988).
The application-specific exposure assessment above follows from this package's
build and deployment commands; it is not an exploit proof.

All 52 retained entries, installed versions and advisory links are in
[the machine-readable review](evidence/dependency-review.json). They remain
installed during `npm ci`: build tooling and package lifecycle scripts execute
on the build machine. Therefore builds still require trusted source and normal
package-install trust. We have not proved each retained finding unreachable
inside every build tool. This preview does not support exposing upstream npm
development servers, Vitest UI or Browser Mode APIs. A future change that uses
those tools needs a new dependency review before it becomes supported.

## Review boundaries

The Python scan covers all 108 pins in the host lock using the PyPI advisory
service. It does **not** cover Python distributions embedded in CAVE container
images, native libraries, package indexes or build backends.

The release retains digest-pinned runtime and builder images. We inventoried
the locally available runtime image digests, architecture and creation times,
and reviewed the named upstream component license sources. We did **not** run
a complete container operating-system CVE scan, generate a complete transitive
SBOM, or review every embedded service dependency. Digest pinning makes the
selection repeatable; it does not establish that those images are secure.
Those reviews remain work for a broader production release. This preview keeps
its existing loopback-only access boundary and explicit single-editor identity.

Original repository work now uses MIT. Upstream source and images retain their
own terms, including Apache-2.0 for the viewer and MPL-2.0 for the pinned
PyChunkedGraph v2 release. [NOTICE.md](../NOTICE.md) identifies components and
retains CAVE attribution for the minimal adapted table-creation setup. The
tracked-source review found no vendored viewer or native graph implementation;
the adapters call upstream interfaces. Redis/PostGIS and other image contents
are not relicensed by this repository's MIT grant.

## Repeat the advisory checks

After `bin/cave fetch`, generate the effective lock in an ignored audit directory:

```bash
.venv/bin/python - <<'PY'
import shutil
from pathlib import Path
from scripts.build_viewer import dependency_lock
import json
source = Path('vendor/neuroglancer')
revision = json.loads(Path('upstream.lock.json').read_text())['neuroglancer']['revision']
effective, _, _ = dependency_lock(source, revision)
audit = Path('.runtime/dependency-audit')
audit.mkdir(parents=True, exist_ok=True)
shutil.copy2(source / 'package.json', audit / 'package.json')
(audit / 'package-lock.json').write_bytes(effective)
PY
npm --prefix .runtime/dependency-audit audit --json
npm --prefix .runtime/dependency-audit audit --omit=dev --json
```

The full audit currently exits nonzero for the retained findings; keep its
output and compare scope/severity to the recorded review rather than ignoring
new results. Install pip-audit in a separate audit environment, then run:

```bash
pip-audit --no-deps --disable-pip -r requirements.lock.txt --format json
```

The full raw outputs and real build/test logs were kept privately under
`.runtime/release-dependencies`; the committed review records their hashes.
Focused viewer guards and a real build passed. Because the compiled viewer
changed, release acceptance must use the new exact candidate and exported tree;
earlier installation reports do not substitute for those runs.
