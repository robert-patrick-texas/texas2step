"""Every component of texas2step at its newest stable release: report them, or move them.

    make outdated    # report only (scripts/upgrade.py --check)
    make upgrade     # move every component, then rebuild the UI

What it covers, and where each is pinned:

* Python, which the server package and the container ship and the tests run on:
  .python-version, the newest stable CPython that uv can install.
* Python packages: uv.lock (uv lock --upgrade).
* npm packages, which build the UI: web/package.json and web/package-lock.json.
* Go modules, which t2s is built with: t2s/go.mod and t2s/go.sum, and go.mod's go line.
* uv's image, which the container is built with: Dockerfile, by version and digest.
* GitHub Actions: .github/workflows/*.yml, by commit (with its tag as a comment) or by tag.
* nfpm, which packages t2s: packaging/build-t2s-deb.sh.
* This machine's uv, Node (the newest LTS) and Go, which build the release. They are
  reported, never changed: update them by hand, uv first, since the Pythons uv offers
  come with it.

Newest means the newest stable release: no alphas, betas, release candidates or other
pre-releases. A component whose newest release failed the tests is held where it is, by
an entry in pyproject.toml's [tool.texas2step.upgrade-hold] saying why; this script
reports it and leaves it be. GitHub's API is reached through the gh command.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
T2S = ROOT / "t2s"
PYTHON_VERSION = ROOT / ".python-version"
DOCKERFILE = ROOT / "Dockerfile"
NFPM_SCRIPT = ROOT / "packaging/build-t2s-deb.sh"
WORKFLOWS = ROOT / ".github/workflows"
UV_IMAGE = re.compile(r"(FROM ghcr\.io/astral-sh/uv:)(\S+?)@(sha256:[0-9a-f]{64})")
NFPM = re.compile(r"(github\.com/goreleaser/nfpm/v(\d+)/cmd/nfpm@)(v[0-9.]+)")
# A pin, and the tag in its comment when it is pinned by commit.
USES = re.compile(r"uses: ([\w.-]+/[\w.-]+)(/[\w./-]+)?@(\S+)(?:[ \t]+#[ \t]*(v?\d[\w.-]*))?")
COMMIT = re.compile(r"[0-9a-f]{40}")
STABLE = re.compile(r"v?(\d+(?:\.\d+)*)")


@dataclass
class Item:
    kind: str  # python, pypi, npm, go, image, action, tool, machine
    name: str
    where: str
    now: str
    newest: str
    held: str | None = None

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    @property
    def behind(self) -> bool:
        return self.now != self.newest


def stable(version: str) -> tuple[int, ...] | None:
    """A stable release's version as numbers, or None for a pre-release or other tag."""
    found = STABLE.fullmatch(version.strip())
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def newest(versions: list[str]) -> str | None:
    ranked = [(stable(v), v) for v in versions]
    ranked = [(key, v) for key, v in ranked if key is not None]
    return max(ranked)[1] if ranked else None


def run(cmd: list[str], cwd: Path = ROOT, ok: tuple[int, ...] = (0,), stderr: bool = False) -> str:
    """A command's output (and its errors too, with stderr=True); exits if it fails."""
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if done.returncode not in ok:
        sys.exit(f"{' '.join(cmd)} failed:\n{done.stderr.strip()}")
    return done.stdout + done.stderr if stderr else done.stdout


def fetch_json(url: str, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "texas2step-upgrade"})
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def gh(path: str) -> Any:
    return json.loads(run(["gh", "api", path]))


def github_newest(repo: str, major: int | None = None) -> str:
    """The newest stable release of a GitHub repository, from its releases or else its tags."""
    releases = gh(f"repos/{repo}/releases?per_page=100")
    tags = [r["tag_name"] for r in releases if not r["draft"] and not r["prerelease"]]
    if not tags:
        tags = [t["name"] for t in gh(f"repos/{repo}/tags?per_page=100")]
    if major is not None:
        tags = [t for t in tags if (stable(t) or (None,))[0] == major]
    found = newest(tags)
    if found is None:
        sys.exit(f"{repo} has no stable release")
    return found


def pypi_newest(name: str) -> str:
    releases = fetch_json(f"https://pypi.org/pypi/{name}/json")["releases"]
    return newest([v for v, files in releases.items() if files]) or "?"


# --- This machine's tools -------------------------------------------------------------


def machine() -> list[Item]:
    items = []
    if shutil.which("uv"):
        now = run(["uv", "--version"]).split()[1]
        items.append(Item("machine", "uv", "this machine (uv self update)", now, pypi_newest("uv")))
    if shutil.which("node"):
        now = run(["node", "--version"]).strip()
        lts = [r["version"] for r in fetch_json("https://nodejs.org/dist/index.json") if r["lts"]]
        items.append(Item("machine", "node", "this machine (newest LTS)", now, newest(lts) or "?"))
    if shutil.which("go"):
        now = run(["go", "env", "GOVERSION"]).strip()
        releases = fetch_json("https://go.dev/dl/?mode=json")
        best = newest([r["version"].removeprefix("go") for r in releases if r["stable"]])
        items.append(Item("machine", "go", "this machine", now, f"go{best}"))
    return items


# --- Python and its packages ----------------------------------------------------------


def python() -> Item:
    now = PYTHON_VERSION.read_text().strip()
    found = json.loads(run(["uv", "python", "list", "--only-downloads", "--output-format", "json"]))
    best = newest(
        [
            p["version"]
            for p in found
            if p["implementation"] == "cpython" and p["variant"] == "default"
        ]
    )
    return Item("python", "cpython", ".python-version", now, best or "?")


def pypi() -> list[Item]:
    out = run(["uv", "lock", "--upgrade", "--dry-run"], stderr=True)  # uv reports there
    items = []
    for name, now, new in re.findall(r"^Update (\S+) (.+?) -> (.+)$", out, re.M):
        items.append(Item("pypi", name, "uv.lock", now, new))
    return items


def upgrade_pypi(held: set[str]) -> None:
    if not held:
        run(["uv", "lock", "--upgrade"])
        return
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    names = {p["name"] for p in lock["package"]} - held - {"texas2step"}
    run(["uv", "lock", *(arg for name in sorted(names) for arg in ("--upgrade-package", name))])


# --- npm --------------------------------------------------------------------------------


def npm() -> list[Item]:
    out = run(["npm", "outdated", "--json"], cwd=WEB, ok=(0, 1))  # 1: something is
    found = json.loads(out) if out.strip() else {}
    return [
        Item("npm", name, "web/package.json", info.get("current", "?"), info["latest"])
        for name, info in found.items()
    ]


def upgrade_npm(held: set[str]) -> None:
    dev = json.loads((WEB / "package.json").read_text())["devDependencies"]
    wanted = [f"{name}@latest" for name in dev if name not in held]
    run(["npm", "install", "--save-dev", "--save-exact", *wanted], cwd=WEB)
    run(["npm", "update"], cwd=WEB)  # what they depend on, within the ranges they allow


# --- Go ---------------------------------------------------------------------------------


def go_modules() -> list[str]:
    out = run(
        [
            "go",
            "list",
            "-deps",
            "-test",
            "-f",
            "{{with .Module}}{{if not .Main}}{{.Path}}{{end}}{{end}}",
            "./...",
        ],
        cwd=T2S,
    )
    return sorted({line for line in out.splitlines() if line})


def go() -> list[Item]:
    items = []
    out = run(["go", "list", "-m", "-u", "-json", *go_modules()], cwd=T2S)
    decoder, at = json.JSONDecoder(), 0
    while at < len(out.rstrip()):
        mod, end = decoder.raw_decode(out, at)
        at = end + len(out[end:]) - len(out[end:].lstrip())
        update = mod.get("Update", {}).get("Version", mod["Version"])
        items.append(Item("go", mod["Path"], "t2s/go.mod", mod["Version"], update))
    directive = re.search(r"^go (\S+)$", (T2S / "go.mod").read_text(), re.M)
    host = re.match(r"go(\d+\.\d+)", run(["go", "env", "GOVERSION"]).strip())
    if directive and host and (stable(host.group(1)) or ()) > (stable(directive.group(1)) or ()):
        items.append(Item("go", "go directive", "t2s/go.mod", directive.group(1), host.group(1)))
    return [item for item in items if item.behind]


def upgrade_go(held: dict[str, str], items: list[Item]) -> None:
    run(["go", "get", "-u", "-t", "./..."], cwd=T2S)
    for item in items:
        if item.name == "go directive" and item.key not in held:
            run(["go", "mod", "edit", f"-go={item.newest}"], cwd=T2S)
        elif item.key in held:
            run(["go", "get", f"{item.name}@{item.now}"], cwd=T2S)
    run(["go", "mod", "tidy"], cwd=T2S)


# --- Pinned in files: uv's image, GitHub Actions, nfpm ---------------------------------


def uv_image() -> Item:
    found = UV_IMAGE.search(DOCKERFILE.read_text())
    if found is None:
        sys.exit("Dockerfile: no ghcr.io/astral-sh/uv image found")
    return Item("image", "astral-sh/uv", "Dockerfile", found.group(2), pypi_newest("uv"))


def ghcr_digest(repo: str, tag: str) -> str:
    token = fetch_json(f"https://ghcr.io/token?scope=repository:{repo}:pull")["token"]
    request = urllib.request.Request(
        f"https://ghcr.io/v2/{repo}/manifests/{tag}",
        method="HEAD",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.oci.image.index.v1+json, "
            "application/vnd.docker.distribution.manifest.list.v2+json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.headers["Docker-Content-Digest"]


def upgrade_uv_image(item: Item) -> None:
    digest = ghcr_digest("astral-sh/uv", item.newest)
    text = UV_IMAGE.sub(lambda m: f"{m.group(1)}{item.newest}@{digest}", DOCKERFILE.read_text())
    DOCKERFILE.write_text(text)


def actions() -> list[Item]:
    """Each action, at the version its pins name: a tag, or a commit with its tag as a comment."""
    pins: dict[str, set[str]] = {}
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for repo, _, ref, tag in USES.findall(workflow.read_text()):
            pins.setdefault(repo, set()).add(tag or ref)
    items = []
    for repo, versions in sorted(pins.items()):
        best = github_newest(repo)
        for version in sorted(versions):
            # A major-version tag (v7) follows the newest major; any other, the newest release.
            new = f"v{(stable(best) or (0,))[0]}" if re.fullmatch(r"v\d+", version) else best
            items.append(Item("action", repo, ".github/workflows", version, new))
    return [item for item in items if item.behind]


def upgrade_actions(items: list[Item]) -> None:
    commits: dict[tuple[str, str], str] = {}
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text()

        def repin(m: re.Match[str]) -> str:
            repo, path, ref, tag = m.group(1), m.group(2) or "", m.group(3), m.group(4)
            item = next((i for i in items if i.name == repo and i.now == (tag or ref)), None)
            if item is None:
                return m.group(0)
            if COMMIT.fullmatch(ref) and tag:  # pinned by commit
                key = (repo, item.newest)
                if key not in commits:
                    commits[key] = gh(f"repos/{repo}/commits/{item.newest}")["sha"]
                return m.group(0).replace(f"@{ref}", f"@{commits[key]}").replace(tag, item.newest)
            return m.group(0).replace(f"{path}@{ref}", f"{path}@{item.newest}")

        new = USES.sub(repin, text)
        if new != text:
            workflow.write_text(new)


def nfpm() -> Item:
    found = NFPM.search(NFPM_SCRIPT.read_text())
    if found is None:
        sys.exit(f"{NFPM_SCRIPT.relative_to(ROOT)}: no nfpm version found")
    best = github_newest("goreleaser/nfpm", major=int(found.group(2)))
    return Item("tool", "nfpm", "packaging/build-t2s-deb.sh", found.group(3), best)


def upgrade_nfpm(item: Item) -> None:
    text = NFPM.sub(lambda m: f"{m.group(1)}{item.newest}", NFPM_SCRIPT.read_text())
    NFPM_SCRIPT.write_text(text)


# --- Report and move ----------------------------------------------------------------------


def holds() -> dict[str, str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return project.get("tool", {}).get("texas2step", {}).get("upgrade-hold", {})


def report(items: list[Item]) -> None:
    width = max((len(i.name) for i in items), default=10)
    for item in items:
        if item.held:
            state = f"held: {item.held}"
        elif item.behind:
            state = "by hand" if item.kind == "machine" else "behind"
        else:
            state = "newest"
        print(f"  {item.name:<{width}}  {item.now:>12} -> {item.newest:<12} {state:8} {item.where}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="report only; change nothing")
    args = parser.parse_args()
    held = holds()
    for command in ("uv", "gh", "npm", "go"):
        if not shutil.which(command):
            sys.exit(f"{command} is needed (docs/development.md)")

    tools = machine()
    py, image, packager = python(), uv_image(), nfpm()
    pinned = [py, *pypi(), *npm(), *go(), image, *actions(), packager]
    for item in pinned:
        item.held = held.get(item.key)
    print("This machine:")
    report(tools)
    print("The repository (behind only, besides Python, uv's image and nfpm):")
    report(pinned)
    uv_now = next((t for t in tools if t.name == "uv"), None)
    if uv_now and uv_now.behind:
        print("Update uv first (uv self update): the Pythons it offers come with it.")
    if args.check:
        return

    def moving(kind: str) -> list[Item]:
        return [i for i in pinned if i.kind == kind and i.behind and not i.held]

    if py.behind and not py.held:
        PYTHON_VERSION.write_text(py.newest + "\n")
    upgrade_pypi({i.name for i in pinned if i.kind == "pypi" and i.held})
    upgrade_npm({i.name for i in pinned if i.kind == "npm" and i.held})
    upgrade_go(held, [i for i in pinned if i.kind == "go"])
    if image.behind and not image.held:
        upgrade_uv_image(image)
    upgrade_actions(moving("action"))
    if packager.behind and not packager.held:
        upgrade_nfpm(packager)
    print("Moved. Next: uv sync, make web, then make check (docs/development.md).")


if __name__ == "__main__":
    main()
