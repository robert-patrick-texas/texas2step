"""t2s, the Go client, against a real server: its results look as the Python engine's do.

Skipped where Go isn't installed. The methodology itself is checked case by case on both
sides (tests/methodology-cases.json); these check the whole run, end to end.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from texas2step import engine

ROOT = Path(__file__).resolve().parent.parent
GO = shutil.which("go")
pytestmark = pytest.mark.skipif(GO is None, reason="needs Go to build t2s")


@pytest.fixture(scope="session")
def t2s(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("t2s") / "t2s"
    subprocess.run(
        [str(GO), "build", "-o", str(out), "."],
        cwd=ROOT / "t2s",
        check=True,
        env={**os.environ, "CGO_ENABLED": "0"},
    )
    return out


def run(t2s: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(t2s), *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def shape(value, at: str = "") -> dict[str, str]:
    """Every key's path and kind of value; a list of objects by its first item."""
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for key, v in value.items():
            number = isinstance(v, int | float) and not isinstance(v, bool)
            out[f"{at}.{key}"] = "number" if number else type(v).__name__
            out |= shape(v, f"{at}.{key}")
    elif isinstance(value, list) and value and isinstance(value[0], dict):
        out |= shape(value[0], f"{at}[]")
    return out


async def test_a_t2s_run_has_the_python_engines_result(t2s, live_server):
    want = await engine.run_all(live_server, seconds=2, streams=2, packets=60, local=True)
    r = run(
        t2s, "run", live_server, "--json", "--seconds", "2", "--streams", "2", "--packets", "60"
    )
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert list(got) == list(want)
    assert shape(got) == shape(want)
    assert got["tool"].startswith("t2s/")
    assert got["loss"]["transport"] == "webrtc"
    assert got["loss"]["received"] > 0


def test_a_saved_t2s_result_is_in_history(t2s, live_server):
    r = run(t2s, "run", live_server, "--json", "--save", "--seconds", "1", "--no-loss")
    assert r.returncode == 0, r.stderr
    saved = json.loads(r.stdout)
    back = httpx.get(f"{live_server}/api/v1/results/{saved['id']}").json()
    assert back["tool"] == saved["tool"]
    assert back["download"]["mbps"] == pytest.approx(saved["download"]["mbps"])


def test_the_report_reads_as_texas2step_runs(t2s, live_server):
    r = run(t2s, "loss", live_server, "--packets", "40")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0].startswith("  Loss ")
    assert lines[1].startswith("  Call quality ")


def test_monitor_probes_and_sums_up(t2s, live_server):
    r = run(t2s, "monitor", live_server, "--json", "--seconds", "3", "--rate", "20")
    assert r.returncode == 0, r.stderr
    lines = [json.loads(line) for line in r.stdout.splitlines()]
    kinds = {line["type"] for line in lines}
    assert {"figures", "summary"} <= kinds
    total = lines[-1]["servers"][0]
    assert total["sent"] >= 40
    assert total["lost"] <= 2


def test_diag_answers_on_the_servers_own_address(t2s, live_server):
    r = run(t2s, "diag", live_server, "--json", "--seconds", "3", "-4")
    assert r.returncode == 0, r.stderr
    summary = json.loads(r.stdout.splitlines()[-1])["diag"]
    rows = {(row["family"], row["method"]): row for row in summary["rows"]}
    assert rows[("IPv4", "HTTP/1.1")]["state"] == "Answering"
    assert rows[("IPv4", "HTTP/2")]["state"] == "Not offered"  # plain http://
    assert summary["you"]["IPv4"] == "127.0.0.1"


def test_an_unreachable_server_is_said_plainly(t2s):
    r = run(t2s, "latency", "http://127.0.0.1:9", timeout=30)
    assert r.returncode == 1
    assert r.stderr.startswith("t2s: can't reach http://127.0.0.1:9")
    assert "pass=" not in r.stderr  # the request's URL, which carries the pass, isn't said


def test_usage_and_version(t2s):
    assert run(t2s, "version").stdout.strip() == "t2s dev"
    r = run(t2s, "run")
    assert r.returncode == 2
    assert "usage: t2s run URL" in r.stderr
