"""``texas2step update``: install a newer release on a server that runs the package.

It takes the steps the install guide's Upgrade section has an operator take by hand:

1. ask the releases API (``update_url``) for the newest release, or for one version;
2. download its .deb and texas2step-setup to CACHE/<version>/, each checked against the
   SHA-256 the API publishes for it;
3. run that release's setup tool with ``--update``. Only it knows its own nginx snippet
   and settings template, so it makes the plan: the package, the settings merge and
   texas2step's own nginx files, nothing else. It writes the plan as text, as
   texas2step-plan.json and as the install script;
4. run the install script: after asking, at once with ``--yes``, or from the update
   timer (``--auto``) only when the plan changes nothing but the package and settings.

Downloads use the system's trust store and honor ``https_proxy``.
"""

from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import __version__
from .config import Settings

CACHE = Path("/var/cache/texas2step/update")
LOCK = Path("/run/lock/texas2step-update.lock")
PYTHON = "/usr/bin/python3"  # the setup tool runs on the system's Python, as by hand
SETUP = "texas2step-setup"
PLAN = "texas2step-plan.json"
SCRIPT = "texas2step-install.sh"
AVAILABLE = 100  # --check's exit status when a newer release is out, as dnf check-update
UNATTENDED = {"package", "settings"}  # what --auto installs with nobody there


class UpdateError(Exception):
    """Stops the update, with a message for the operator."""


@dataclass
class Asset:
    name: str
    url: str
    sha256: str
    size: int


@dataclass
class Release:
    tag: str
    version: str  # the package's, e.g. 1.3.0-1
    date: str  # YYYY-MM-DD
    page: str  # its release notes
    deb: Asset
    setup: Asset


def release_url(base: str, version: str | None) -> str:
    base = base.rstrip("/")
    if not version:
        return base + "/releases/latest"
    return f"{base}/releases/tags/v{version.removeprefix('v')}"


def _asset(entry: dict, tag: str) -> Asset:
    name = str(entry.get("name") or "")
    algo, _, value = str(entry.get("digest") or "").partition(":")
    if algo != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise UpdateError(f"release {tag}: {name} has no SHA-256 to check it against")
    url = str(entry.get("browser_download_url") or "")
    if not url.startswith(("https://", "http://")):
        raise UpdateError(f"release {tag}: {name} has no download URL")
    return Asset(name, url, value, int(entry.get("size") or 0))


def parse_release(data: dict, arch: str) -> Release:
    """The parts of a releases API answer (GitHub's shape) that an update needs."""
    tag = str(data.get("tag_name") or "?")
    assets = [a for a in data.get("assets") or [] if isinstance(a, dict)]
    deb = re.compile(rf"texas2step-([0-9][0-9A-Za-z.+~]*-[0-9A-Za-z.+~]+)-{re.escape(arch)}\.deb")
    debs = [(m.group(1), a) for a in assets if (m := deb.fullmatch(str(a.get("name") or "")))]
    if not debs:
        raise UpdateError(f"release {tag} has no texas2step package for {arch}")
    by_version = functools.cmp_to_key(lambda x, y: {"lt": -1, "eq": 0, "gt": 1}[compare(x, y)])
    version, newest = max(debs, key=lambda d: by_version(d[0]))
    setup = next((a for a in assets if a.get("name") == SETUP), None)
    if setup is None:
        raise UpdateError(f"release {tag} has no {SETUP}")
    return Release(
        tag=tag,
        version=version,
        date=str(data.get("published_at") or "")[:10],
        page=str(data.get("html_url") or ""),
        deb=_asset(newest, tag),
        setup=_asset(setup, tag),
    )


def compare(a: str, b: str) -> str:
    """lt, eq or gt: package version ``a`` against ``b``, the way dpkg orders them."""
    for op in ("lt", "eq", "gt"):
        if subprocess.run(["dpkg", "--compare-versions", a, op, b]).returncode == 0:
            return op
    raise UpdateError(f"can't compare versions {a!r} and {b!r}")


def installed_version() -> str | None:
    """The texas2step package's version, if dpkg has it installed."""
    try:
        r = subprocess.run(
            ["dpkg-query", "-W", "--showformat=${db:Status-Abbrev} ${Version}", "texas2step"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    parts = r.stdout.split()
    return parts[1] if r.returncode == 0 and len(parts) == 2 and parts[0] == "ii" else None


def architecture() -> str:
    return subprocess.run(
        ["dpkg", "--print-architecture"], capture_output=True, text=True, check=True
    ).stdout.strip()


def client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(30, read=60),
        follow_redirects=True,
        # The system's trust store, so a mirror with a certificate from an internal CA works.
        verify=ssl.create_default_context(),
        headers={"User-Agent": f"texas2step/{__version__} (update)"},
    )


def fetch_release(http: httpx.Client, base: str, version: str | None, arch: str) -> Release:
    url = release_url(base, version)
    try:
        r = http.get(url, headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as exc:
        raise UpdateError(f"can't reach {url}: {type(exc).__name__}: {exc}") from exc
    if r.status_code == 404:
        what = f"no release v{version.removeprefix('v')}" if version else "no release"
        raise UpdateError(f"{what} at {url}")
    if r.status_code != 200:
        raise UpdateError(f"{url} answered {r.status_code} {r.reason_phrase}")
    try:
        data = r.json()
    except ValueError as exc:
        raise UpdateError(f"{url} didn't answer with JSON") from exc
    if not isinstance(data, dict):
        raise UpdateError(f"{url} didn't answer with a release")
    return parse_release(data, arch)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(http: httpx.Client, asset: Asset, folder: Path, mode: int) -> Path:
    """``asset`` in ``folder``, checked against its SHA-256; kept from an earlier try."""
    path = folder / asset.name
    if not (path.is_file() and _sha256(path) == asset.sha256):
        part = path.with_name(path.name + ".part")
        digest = hashlib.sha256()
        try:
            with http.stream("GET", asset.url) as r, part.open("wb") as fh:
                if r.status_code != 200:
                    raise UpdateError(f"{asset.url} answered {r.status_code} {r.reason_phrase}")
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
                    digest.update(chunk)
        except httpx.HTTPError as exc:
            part.unlink(missing_ok=True)
            raise UpdateError(f"downloading {asset.name}: {type(exc).__name__}: {exc}") from exc
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        if digest.hexdigest() != asset.sha256:
            part.unlink()
            raise UpdateError(
                f"{asset.name} doesn't match the SHA-256 published with the release; not used"
            )
        part.replace(path)
    path.chmod(mode)
    return path


def lock() -> int:
    """One update at a time: the timer's and a person's could otherwise meet."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise UpdateError("another texas2step update is running") from None
    return fd


def run_setup(setup: Path, allow_downgrade: bool) -> int:
    """The release's setup tool in update mode; its report goes straight to our output."""
    shown = subprocess.run([PYTHON, "-I", str(setup), "--help"], capture_output=True, text=True)
    if "--update" not in shown.stdout:
        raise UpdateError(
            f"this release's {SETUP} predates `texas2step update` and can't plan an update; "
            "install that release by hand, as in docs/install.md"
        )
    cmd = [PYTHON, "-I", str(setup), "--update", "--out", str(setup.parent)]
    if allow_downgrade:
        cmd.append("--allow-downgrade")
    # Without SUDO_UID the files it writes stay root's, like the folder they are in.
    env = {k: v for k, v in os.environ.items() if k not in {"SUDO_UID", "SUDO_GID"}}
    sys.stdout.flush()
    return subprocess.run(cmd, env=env, cwd=setup.parent).returncode


def run_script(script: Path) -> None:
    """Hand over to the install script. This process ends here, before apt replaces the
    files it runs from; the script inherits the lock."""
    sys.stdout.flush()
    os.execv("/bin/bash", ["bash", str(script)])


def _shown(version: str, other: str) -> str:
    """1.3.0-1 as 1.3.0, unless only the package revisions differ."""
    app = version.rsplit("-", 1)[0]
    return version if version != other and app == other.rsplit("-", 1)[0] else app


def main(args: argparse.Namespace, settings: Settings) -> int:
    def say(text: str, warning: bool = False) -> None:
        # From the timer, "<4>" makes journald file the line as a warning.
        print(("<4>" if warning and args.auto else "") + text, flush=True)

    installed = installed_version()
    if installed is None:
        raise UpdateError(
            "texas2step isn't installed from its package here; this updates package installs"
        )
    if not args.check and os.geteuid() != 0:
        raise UpdateError("installing needs root: sudo texas2step update")

    with client() as http:
        release = fetch_release(http, settings.update_url, args.release, architecture())
        old, new = _shown(installed, release.version), _shown(release.version, installed)
        order = compare(installed, release.version)
        if order == "lt":
            say(f"texas2step {old} is installed; {new} is available ({release.date}).")
            if release.page:
                say(f"  Release notes: {release.page}")
        elif not args.release:
            newer = "" if order == "eq" else f", newer than the latest release ({new})"
            say(f"texas2step {old} is installed{newer}: up to date.")
            return 0
        elif order == "eq":
            say(f"texas2step {old} is installed; it would be reinstalled.")
        else:
            say(f"texas2step {old} is installed; {new} ({release.date}) is older.")
            if not args.allow_downgrade:
                if args.check:
                    return 0
                raise UpdateError(f"add --allow-downgrade to install {new}")
        if args.check:
            if order == "lt":
                say("To install it: sudo texas2step update")
            return AVAILABLE if order == "lt" else 0

        fd = lock()
        try:
            return _install(args, http, release, new, say, fd)
        finally:
            os.close(fd)  # unless the install script took it over


def _install(
    args: argparse.Namespace,
    http: httpx.Client,
    release: Release,
    new: str,
    say: Callable[..., None],
    fd: int,
) -> int:
    folder = CACHE / release.version
    folder.mkdir(parents=True, exist_ok=True)
    for kept in CACHE.iterdir():  # earlier downloads
        if kept.is_dir() and kept != folder:
            shutil.rmtree(kept, ignore_errors=True)
    mb = f" ({release.deb.size / 1e6:.0f} MB)" if release.deb.size else ""
    say(f"Downloading {release.deb.name}{mb} and {SETUP} to {folder}/")
    download(http, release.deb, folder, 0o644)
    setup = download(http, release.setup, folder, 0o755)
    say("Both match the SHA-256 published with the release.\n")

    for stale in (PLAN, SCRIPT):
        (folder / stale).unlink(missing_ok=True)
    status = run_setup(setup, args.allow_downgrade)
    if status == 1:
        raise UpdateError("the check above found something to fix first; nothing was installed")
    if status != 0:
        raise UpdateError(f"{SETUP} failed (exit status {status}); nothing was installed")
    try:
        plan = json.loads((folder / PLAN).read_text())
        changes = {name for name, changed in plan["changes"].items() if changed}
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        raise UpdateError(f"{SETUP} left no readable {PLAN}: {exc}") from exc

    if args.auto:
        left = sorted(changes - UNATTENDED)
        if left:
            say(
                f"texas2step {new} is ready in {folder}/, but its plan changes "
                f"{' and '.join(left)}, which an unattended update leaves to you. "
                "To install it: sudo texas2step update",
                warning=True,
            )
            return 0
    elif not args.yes:
        if not sys.stdin.isatty():
            raise UpdateError("no terminal to ask on; add --yes to install without asking")
        answer = input(f"\nInstall texas2step {new} now? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            say(f"Nothing installed. The plan and install script are in {folder}/.")
            return 0
    os.set_inheritable(fd, True)
    run_script(folder / SCRIPT)
    return 0  # not reached, except in tests
