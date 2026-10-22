"""texas2step update, against a releases API on loopback and a stand-in setup tool."""

import hashlib
import io
import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from texas2step import cli, update
from texas2step.config import Settings

pytestmark = pytest.mark.skipif(not shutil.which("dpkg"), reason="compares versions with dpkg")

# Stands in for a release's texas2step-setup: records how it was run, prints a report and
# writes the plan, as the real one does with --update.
FAKE_SETUP = """\
import json, os, sys
from pathlib import Path
if "--help" in sys.argv:
    print("usage: texas2step-setup [--update] [--out OUT]")
    sys.exit(0)
out = Path(sys.argv[sys.argv.index("--out") + 1])
Path(os.environ["SETUP_ARGS"]).write_text(json.dumps(sys.argv[1:]))
print("Will change\\n  - upgrade texas2step")
changes = {k: k in os.environ["SETUP_CHANGES"].split(",") for k in
           ("package", "settings", "nginx", "firewall")}
(out / "texas2step-plan.json").write_text(json.dumps({"changes": changes}))
(out / "texas2step-install.sh").write_text("#!/bin/bash\\n")
sys.exit(int(os.environ.get("SETUP_EXIT", "0")))
"""
DEB = b"not really a package"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Mirror:
    """A releases API in GitHub's shape, with the files behind it."""

    def __init__(self) -> None:
        self.routes: dict[str, bytes] = {}
        self.hits: list[str] = []
        mirror = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                mirror.hits.append(self.path)
                body = mirror.routes.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/repos/x/texas2step"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def release(self, version: str, date: str, *, latest=False, deb=DEB, setup=FAKE_SETUP):
        files = {f"texas2step-{version}-1-amd64.deb": deb, "texas2step-setup": setup.encode()}
        root = self.base.split("/repos/")[0]
        assets = [
            {
                "name": name,
                "browser_download_url": f"{root}/download/v{version}/{name}",
                "digest": digest(DEB if name.endswith(".deb") else data),
                "size": len(data),
            }
            for name, data in files.items()
        ]
        for name, data in files.items():
            self.routes[f"/download/v{version}/{name}"] = data
        data = {
            "tag_name": f"v{version}",
            "published_at": f"{date}T12:00:00Z",
            "html_url": f"https://example.net/releases/v{version}",
            "assets": assets,
        }
        body = json.dumps(data).encode()
        self.routes[f"/repos/x/texas2step/releases/tags/v{version}"] = body
        if latest:
            self.routes["/repos/x/texas2step/releases/latest"] = body


@pytest.fixture
def mirror():
    m = Mirror()
    m.release("1.3.0", "2026-11-02", latest=True)
    m.release("1.1.0", "2026-10-19")
    yield m
    m.server.shutdown()


@pytest.fixture
def host(tmp_path, monkeypatch):
    """This test as a root shell on a server with texas2step 1.2.0-1 installed."""
    ran = {}
    monkeypatch.setattr(update, "installed_version", lambda: "1.2.0-1")
    monkeypatch.setattr(update, "architecture", lambda: "amd64")
    monkeypatch.setattr(update, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(update, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(update, "PYTHON", sys.executable)
    monkeypatch.setattr(update, "run_script", lambda script: ran.setdefault("script", script))
    monkeypatch.setattr(update.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SETUP_ARGS", str(tmp_path / "setup-args"))
    monkeypatch.setenv("SETUP_CHANGES", "package,settings")
    ran["setup"] = lambda: json.loads((tmp_path / "setup-args").read_text())
    return ran


def run(mirror, *argv):
    args = cli.build_parser().parse_args(["update", *argv])
    return update.main(args, Settings(update_url=mirror.base))


def test_check_says_whether_a_newer_release_is_out(mirror, host, monkeypatch, capsys):
    assert run(mirror, "--check") == update.AVAILABLE
    out = capsys.readouterr().out
    assert "texas2step 1.2.0 is installed; 1.3.0 is available (2026-11-02)." in out
    assert "Release notes: https://example.net/releases/v1.3.0" in out
    assert "sudo texas2step update" in out
    assert not (update.CACHE).exists()  # a check downloads nothing

    for installed, said in (
        ("1.3.0-1", "texas2step 1.3.0 is installed: up to date."),
        ("1.4.0-1", "texas2step 1.4.0 is installed, newer than the latest release (1.3.0)"),
        # Only the package revision differs, so it is shown.
        ("1.3.0-2", "texas2step 1.3.0-2 is installed, newer than the latest release (1.3.0-1)"),
    ):
        monkeypatch.setattr(update, "installed_version", lambda v=installed: v)
        assert run(mirror, "--check") == 0
        assert said in capsys.readouterr().out
    # Anyone can check; installing needs root.
    monkeypatch.setattr(update.os, "geteuid", lambda: 1000)
    assert run(mirror, "--check") == 0
    with pytest.raises(update.UpdateError, match="needs root"):
        run(mirror)


def test_an_update_downloads_checks_and_hands_over_to_the_release(mirror, host, capfd):
    stale = update.CACHE / "1.1.0-1"
    stale.mkdir(parents=True)
    assert run(mirror, "--yes") == 0
    folder = update.CACHE / "1.3.0-1"
    deb, setup = folder / "texas2step-1.3.0-1-amd64.deb", folder / "texas2step-setup"
    assert deb.read_bytes() == DEB and deb.stat().st_mode & 0o777 == 0o644
    assert setup.stat().st_mode & 0o777 == 0o755
    assert not stale.exists()  # earlier downloads go
    assert host["setup"]() == ["--update", "--out", str(folder)]
    assert host["script"] == folder / "texas2step-install.sh"
    out = capfd.readouterr().out
    assert "Both match the SHA-256 published with the release." in out
    assert "Will change\n  - upgrade texas2step" in out  # the setup tool's own report

    # Run again, the files already there are used, not fetched again.
    mirror.hits.clear()
    host.pop("script")
    assert run(mirror, "--yes") == 0 and host["script"]
    assert not [hit for hit in mirror.hits if hit.startswith("/download/")]


def test_a_file_that_doesnt_match_its_sha256_is_not_used(mirror, host):
    mirror.release("1.3.0", "2026-11-02", latest=True, deb=b"tampered")
    with pytest.raises(update.UpdateError, match="doesn't match the SHA-256"):
        run(mirror, "--yes")
    assert list((update.CACHE / "1.3.0-1").iterdir()) == []
    assert "script" not in host


def test_a_given_version_and_downgrades(mirror, host, capsys):
    with pytest.raises(update.UpdateError, match=r"add --allow-downgrade to install 1\.1\.0"):
        run(mirror, "--version", "1.1.0", "--yes")
    assert run(mirror, "--version", "1.1.0", "--allow-downgrade", "--yes") == 0
    assert host["setup"]()[-1] == "--allow-downgrade"
    assert "texas2step 1.2.0 is installed; 1.1.0 (2026-10-19) is older." in capsys.readouterr().out
    with pytest.raises(update.UpdateError, match=r"no release v9\.9\.9 at http://127\.0\.0\.1"):
        run(mirror, "--version", "v9.9.9")


def test_unattended_installs_only_package_and_settings_changes(mirror, host, monkeypatch, capsys):
    assert run(mirror, "--auto") == 0 and host.pop("script")
    monkeypatch.setenv("SETUP_CHANGES", "package,settings,nginx")
    assert run(mirror, "--auto") == 0 and "script" not in host
    said = capsys.readouterr().out.splitlines()[-1]
    assert said.startswith("<4>texas2step 1.3.0 is ready in ")  # a warning in the journal
    assert "its plan changes nginx" in said and said.endswith("sudo texas2step update")
    # Something to fix first: the timer's run fails, so it shows in systemctl --failed.
    monkeypatch.setenv("SETUP_EXIT", "1")
    with pytest.raises(update.UpdateError, match="something to fix first"):
        run(mirror, "--auto")


def test_it_asks_first(mirror, host, monkeypatch, capsys):
    class Terminal:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert run(mirror) == 0 and "script" not in host
    assert "Nothing installed. The plan and install script are in" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert run(mirror) == 0 and host["script"]

    monkeypatch.setattr(sys, "stdin", io.StringIO())  # no terminal, as from cron
    with pytest.raises(update.UpdateError, match="add --yes"):
        run(mirror)


def test_a_setup_tool_from_before_update_mode(mirror, host):
    old = FAKE_SETUP.replace("[--update] ", "")
    mirror.release("1.1.0", "2026-10-19", setup=old)
    with pytest.raises(update.UpdateError, match="predates `texas2step update`"):
        run(mirror, "--version", "1.1.0", "--allow-downgrade", "--yes")


def test_only_package_installs_are_updated(mirror, host, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: None)
    with pytest.raises(update.UpdateError, match="isn't installed from its package"):
        run(mirror, "--check")


def test_one_update_at_a_time(mirror, host):
    fd = update.lock()
    try:
        with pytest.raises(update.UpdateError, match="another texas2step update is running"):
            run(mirror, "--yes")
    finally:
        update.os.close(fd)


def test_releases_without_what_an_update_needs(monkeypatch):
    monkeypatch.setattr(update, "compare", lambda a, b: "eq")
    deb = {
        "name": "texas2step-1.3.0-1-amd64.deb",
        "browser_download_url": "https://example.net/d.deb",
        "digest": "sha256:" + "a" * 64,
    }
    setup = deb | {"name": "texas2step-setup", "browser_download_url": "https://example.net/s"}
    release = update.parse_release({"tag_name": "v1.3.0", "assets": [deb, setup]}, "amd64")
    assert (release.version, release.deb.sha256) == ("1.3.0-1", "a" * 64)
    for assets, why in (
        ([setup], "no texas2step package for amd64"),
        ([deb], "no texas2step-setup"),
        ([deb | {"digest": None}, setup], "has no SHA-256"),
    ):
        with pytest.raises(update.UpdateError, match=why):
            update.parse_release({"tag_name": "v1.3.0", "assets": assets}, "amd64")
    with pytest.raises(update.UpdateError, match="no texas2step package for arm64"):
        update.parse_release({"tag_name": "v1.3.0", "assets": [deb, setup]}, "arm64")


def stamped(name: str, deb: bytes) -> str:
    """FAKE_SETUP as a release's setup tool is built: naming its package, with its SHA-256."""
    return f'DEB_NAME = "{name}"\nDEB_SHA256 = "{hashlib.sha256(deb).hexdigest()}"\n' + FAKE_SETUP


@pytest.fixture
def folder(tmp_path):
    """What an operator copies to a server without internet access: one release's pair."""
    path = tmp_path / "from-usb"
    path.mkdir()
    (path / "texas2step-1.3.0-1-amd64.deb").write_bytes(DEB)
    (path / "texas2step-setup").write_text(stamped("texas2step-1.3.0-1-amd64.deb", DEB))
    return path


def run_from(folder, *argv):
    args = cli.build_parser().parse_args(["update", "--from", str(folder), *argv])
    # update_url leads nowhere: nothing --from does may reach for it.
    return update.main(args, Settings(update_url="http://127.0.0.1:9/nowhere"))


def test_from_a_folder_installs_without_downloading(folder, host, capfd):
    assert run_from(folder, "--yes") == 0
    cache = update.CACHE / "1.3.0-1"
    deb, setup = cache / "texas2step-1.3.0-1-amd64.deb", cache / "texas2step-setup"
    assert deb.read_bytes() == DEB and deb.stat().st_mode & 0o777 == 0o644
    assert setup.stat().st_mode & 0o777 == 0o755
    assert host["setup"]() == ["--update", "--out", str(cache)]  # the copy runs, not the original
    assert host["script"] == cache / "texas2step-install.sh"
    out = capfd.readouterr().out
    assert f"texas2step 1.2.0 is installed; 1.3.0 is in {folder}/." in out
    assert "The package matches the SHA-256 its texas2step-setup was built with." in out
    assert "Will change\n  - upgrade texas2step" in out


def test_from_checks_the_two_files_are_a_pair(folder, host, monkeypatch):
    deb, setup = folder / "texas2step-1.3.0-1-amd64.deb", folder / "texas2step-setup"
    for broken, said in (
        (
            lambda: deb.write_bytes(b"another build"),
            "doesn't match the SHA-256 its texas2step-setup was built with",
        ),
        (deb.unlink, r"built with texas2step-1\.3\.0-1-amd64\.deb, which isn't in"),
        (setup.unlink, "no texas2step-setup to read in"),
        (lambda: setup.write_text(FAKE_SETUP), "doesn't name the package it was built with"),
        (
            lambda: setup.write_text(stamped("texas2step-1.3.0-1-arm64.deb", DEB)),
            "is for arm64, not this server's amd64",
        ),
    ):
        broken()
        with pytest.raises(update.UpdateError, match=said):
            run_from(folder, "--yes")
        assert not update.CACHE.exists() and "script" not in host
        deb.write_bytes(DEB)
        setup.write_text(stamped(deb.name, DEB))

    # Changed between its check and the copy that runs: not used.
    monkeypatch.setattr(
        update.shutil, "copyfile", lambda src, dst: Path(dst).write_bytes(b"swapped")
    )
    with pytest.raises(update.UpdateError, match="changed after it was checked"):
        run_from(folder, "--yes")
    assert "script" not in host


def test_from_compares_versions_as_a_chosen_release(folder, host, monkeypatch, capsys):
    monkeypatch.setattr(update.os, "geteuid", lambda: 1000)  # anyone can check
    assert run_from(folder, "--check") == update.AVAILABLE
    out = capsys.readouterr().out
    assert f"To install it: sudo texas2step update --from {folder}" in out
    assert not update.CACHE.exists()  # a check copies nothing

    monkeypatch.setattr(update, "installed_version", lambda: "1.3.0-1")
    assert run_from(folder, "--check") == 0
    assert "texas2step 1.3.0 is installed; it would be reinstalled." in capsys.readouterr().out

    monkeypatch.setattr(update, "installed_version", lambda: "1.4.0-1")
    monkeypatch.setattr(update.os, "geteuid", lambda: 0)
    with pytest.raises(update.UpdateError, match=r"add --allow-downgrade to install 1\.3\.0"):
        run_from(folder, "--yes")
    assert f"1.3.0 in {folder}/ is older." in capsys.readouterr().out
    assert run_from(folder, "--allow-downgrade", "--yes") == 0
    assert host["setup"]()[-1] == "--allow-downgrade"


def test_command_line_checks():
    for argv, said in (
        (["update", "--auto", "--check"], "--auto takes no other options"),
        (["update", "--auto", "--from", "/srv/t2s"], "--auto takes no other options"),
        (["update", "--from", "/srv/t2s", "--version", "1.2.0"], "leave out --version"),
        (["update", "--allow-downgrade"], "--allow-downgrade goes with --version"),
    ):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert said in str(exc.value)
