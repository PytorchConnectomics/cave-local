"""Build pinned Spelunker source with a reviewed, exact dependency-lock overlay."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEWER_COMMIT = "d4aeac3b66eb4bd3fa86dbdc0202a7f006439740"
OVERLAY = ROOT / "config/viewer-dependencies.lock.json"


def command(args, cwd=None, env=None):
    return subprocess.check_output(args, cwd=cwd, env=env, text=True).strip()


def dependency_lock(source, commit, overlay_path=OVERLAY):
    """Change only reviewed leaf-package artifacts; reject a different base lock."""
    original = (source / "package-lock.json").read_bytes()
    overlay_bytes = Path(overlay_path).read_bytes()
    overlay = json.loads(overlay_bytes)
    if overlay.get("schema_version") != 1 or overlay.get("upstream_revision") != commit:
        raise RuntimeError("Viewer dependency overlay does not match the pinned revision")
    if hashlib.sha256(original).hexdigest() != overlay["upstream_package_lock_sha256"]:
        raise RuntimeError("Upstream viewer dependency lock hash differs from the reviewed lock")
    data = json.loads(original)
    if set(overlay["replacements"]) != {"lodash-es", "valibot", "fflate"}:
        raise RuntimeError("Viewer dependency overlay must contain exactly the reviewed runtime packages")
    for name, replacement in overlay["replacements"].items():
        package = data["packages"]["node_modules/" + name]
        if package["version"] != replacement["previous_version"]:
            raise RuntimeError(f"Viewer overlay has an unexpected base version for {name}")
        expected_url = f"https://registry.npmjs.org/{name}/-/{name}-{replacement['version']}.tgz"
        if replacement["resolved"] != expected_url or not replacement["integrity"].startswith("sha512-"):
            raise RuntimeError(f"Viewer overlay has an invalid package artifact for {name}")
        package.update({key: replacement[key] for key in ("version", "resolved", "integrity")})
    effective = (json.dumps(data, indent=2) + "\n").encode()
    if hashlib.sha256(effective).hexdigest() != overlay["package_lock_sha256"]:
        raise RuntimeError("Effective viewer dependency lock hash differs from the reviewed lock")
    return effective, overlay, hashlib.sha256(overlay_bytes).hexdigest()


def license_notices(source, output):
    """Retain upstream and npm license texts alongside locally built assets."""
    shutil.copy2(source / "LICENSE", output / "LICENSE.upstream")
    notices = []
    paths = {source / "LICENSE"}
    paths.update(path for path in source.rglob("*") if path.is_file()
                 and "node_modules" not in path.parts
                 and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")))
    packages = json.loads((source / "package-lock.json").read_text())["packages"]
    for name, package in packages.items():
        if name and not package.get("dev"):
            directory = source / name
            paths.update(path for path in directory.glob("*") if path.is_file()
                         and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")))
    for path in sorted(paths):
        notices.append(f"===== {path.relative_to(source)} =====\n{path.read_text(errors='replace')}\n")
    # Some upstream and third-party files carry their attribution inline rather
    # than in a separate notice file. Retain distinct leading license blocks
    # even when the JavaScript minifier removes source comments.
    headers = {}
    for directory in (source / "src", source / "build_tools"):
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix not in (".js", ".ts"):
                continue
            beginning = path.read_text(errors="replace")[:4000]
            match = re.match(r"\s*(/\*.*?\*/)", beginning, re.DOTALL)
            if match and ("@license" in match[1] or "Copyright" in match[1]):
                headers.setdefault(match[1], str(path.relative_to(source)))
    for header, name in headers.items():
        notices.append(f"===== Inline attribution, first occurrence: {name} =====\n{header}\n")
    (output / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(notices))


def build(source=None, output=None):
    source = Path(source or ROOT / "vendor/neuroglancer").resolve()
    output = Path(output or ROOT / ".runtime/viewer").resolve()
    if not (source / "package-lock.json").is_file():
        raise RuntimeError("Pinned viewer source is missing. Run bin/cave fetch first.")
    lock = json.loads((ROOT / "upstream.lock.json").read_text())
    expected = lock.get("neuroglancer", {}).get("revision", VIEWER_COMMIT)
    commit = command(["git", "rev-parse", "HEAD"], source)
    if commit != expected:
        raise RuntimeError(f"Viewer checkout must be {expected}; found {commit}")
    if command(["git", "status", "--porcelain", "--untracked-files=no"], source):
        raise RuntimeError("Viewer source has tracked modifications; restore the pinned upstream checkout first.")
    effective_lock, overlay, overlay_hash = dependency_lock(source, commit)
    node = command(["node", "--version"])
    version = tuple(int(n) for n in re.match(r"v?(\d+)\.(\d+)\.(\d+)", node).groups())
    if version < (22, 18, 0):
        raise RuntimeError(f"Spelunker requires Node >=22.18; found {node}")
    npm = command(["npm", "--version"])
    env = os.environ.copy()
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    # Upstream's lock contains a public GitHub dependency as git+ssh. Scope
    # HTTPS rewrites to this build so a new server needs no personal SSH key.
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    for offset, prefix in enumerate(("ssh://git@github.com/", "git@github.com:")):
        env[f"GIT_CONFIG_KEY_{count + offset}"] = "url.https://github.com/.insteadOf"
        env[f"GIT_CONFIG_VALUE_{count + offset}"] = prefix
    env["GIT_CONFIG_COUNT"] = str(count + 2)
    env["GIT_TERMINAL_PROMPT"] = "0"
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="viewer-stage-", dir=output.parent))
    work = Path(tempfile.mkdtemp(prefix="viewer-source-", dir=output.parent))
    try:
        # A separate checkout preserves upstream symlinks and Git-derived build
        # metadata. A plain archive nested in this repository could make Git
        # commands accidentally discover the packaging repository's HEAD.
        subprocess.run(["git", "clone", "--shared", "--no-checkout", str(source), str(work)], check=True)
        subprocess.run(["git", "checkout", "--detach", commit], cwd=work, check=True)
        (work / "package-lock.json").write_bytes(effective_lock)
        subprocess.run(["npm", "ci"], cwd=work, env=env, check=True)
        if (work / "package-lock.json").read_bytes() != effective_lock:
            raise RuntimeError("npm changed the reviewed viewer dependency lock")
        for name, replacement in overlay["replacements"].items():
            installed = json.loads((work / "node_modules" / name / "package.json").read_text())
            if installed["version"] != replacement["version"]:
                raise RuntimeError(f"Installed viewer dependency {name} differs from the reviewed version")
        subprocess.run(["npm", "run", "build", "--", "--output", str(stage)], cwd=work, env=env, check=True)
        if not (stage / "index.html").is_file():
            raise RuntimeError("Viewer build did not produce index.html")
        provenance = {
            "upstream": "https://github.com/seung-lab/neuroglancer.git",
            "commit": commit,
            "upstream_package_lock_sha256": overlay["upstream_package_lock_sha256"],
            "package_lock_sha256": overlay["package_lock_sha256"],
            "dependency_overlay_sha256": overlay_hash,
            "dependency_versions": {name: item["version"] for name, item in overlay["replacements"].items()},
            "node": node,
            "npm": npm,
            "source_modified": False,
            "dependency_lock_modified": True,
            "modified": True,
        }
        (stage / "build-info.json").write_text(json.dumps(provenance, indent=2) + "\n")
        license_notices(work, stage)
        # Preserve the bind-mounted directory and hashed assets used by open tabs.
        # Publish HTML last, after all assets referenced by the build are present.
        output.mkdir(parents=True, exist_ok=True)
        for item in stage.iterdir():
            if item.name == "index.html":
                continue
            if item.is_dir():
                shutil.copytree(item, output / item.name, dirs_exist_ok=True)
            else:
                shutil.copy2(item, output / item.name)
        temporary_index = output / ".index.html.new"
        shutil.copy2(stage / "index.html", temporary_index)
        temporary_index.replace(output / "index.html")
        print(json.dumps({"output": str(output), **provenance}, indent=2))
        return provenance
    finally:
        shutil.rmtree(stage)
        shutil.rmtree(work)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    build(args.source, args.output)
