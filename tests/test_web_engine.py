"""The browser's engine (web/src/engine) on the cases t2s is checked against too: what the
Python engine makes of them (tests/methodology-cases.json)."""

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "tests" / "methodology-cases.json"
ESBUILD = ROOT / "web" / "node_modules" / ".bin" / "esbuild"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not (NODE and ESBUILD.exists()), reason="needs Node and web/node_modules (make web)"
)

ENTRY = """
import { readFileSync } from "node:fs";
import { jitter, percentile, summarize, summarizeThroughput } from "ENGINE/stats.ts";
import { callQuality, place, rpm } from "ENGINE/quality.ts";

const cases = JSON.parse(readFileSync(process.argv[2], "utf8"));
const got = {
  percentile: cases.percentile.map((c) => percentile(c.values, c.q)),
  jitter: cases.jitter.map((c) => jitter(c.values)),
  summarize: cases.summarize.map((c) => summarize(c.samples, c.lost, c.keep_samples)),
  summarize_throughput: cases.summarize_throughput.map((c) =>
    summarizeThroughput(c.bins, c.bin_ms, c.seconds),
  ),
  place: cases.place.map((c) => place(c.packets, c.seen, c.upstream)),
  call_quality: cases.call_quality.map((c) => callQuality(c.loss)),
  rpm: cases.rpm.map((c) => rpm(c.samples)),
};
process.stdout.write(JSON.stringify(got));
"""


@pytest.fixture(scope="module")
def browser(tmp_path_factory) -> dict[str, list]:
    """What the browser's engine makes of each case."""
    tmp = tmp_path_factory.mktemp("web-engine")
    entry = tmp / "entry.mjs"
    entry.write_text(ENTRY.replace("ENGINE", (ROOT / "web" / "src" / "engine").as_posix()))
    bundle = tmp / "bundle.mjs"
    subprocess.run(
        [
            str(ESBUILD),
            str(entry),
            "--bundle",
            "--format=esm",
            "--platform=node",
            f"--outfile={bundle}",
        ],
        check=True,
        capture_output=True,
    )
    node = [NODE or "node", str(bundle), str(CASES)]
    out = subprocess.run(node, check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def differences(got, want, at: str = "") -> list[str]:
    """How got differs from want: the same keys, nulls and lists, figures within a part
    in a billion (as t2s's cases.Diff)."""
    if isinstance(want, dict):
        if not isinstance(got, dict):
            return [f"{at}: got {got!r}, want an object"]
        out = []
        for key in sorted(set(want) | set(got)):
            if key not in got:
                out.append(f"{at}.{key}: missing, want {want[key]!r}")
            elif key not in want:
                out.append(f"{at}.{key}: got {got[key]!r}, which the Python engine leaves out")
            else:
                out += differences(got[key], want[key], f"{at}.{key}")
        return out
    if isinstance(want, list):
        if not isinstance(got, list) or len(got) != len(want):
            return [f"{at}: got {got!r}, want {want!r}"]
        return [
            d
            for i, (g, w) in enumerate(zip(got, want, strict=True))
            for d in differences(g, w, f"{at}[{i}]")
        ]
    if isinstance(want, bool) or want is None or isinstance(want, str):
        return [] if got == want else [f"{at}: got {got!r}, want {want!r}"]
    if isinstance(got, bool) or not isinstance(got, int | float):
        return [f"{at}: got {got!r}, want {want!r}"]
    return (
        []
        if math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-9)
        else [f"{at}: got {got}, want {want}"]
    )


@pytest.mark.parametrize(
    "kind",
    ["percentile", "jitter", "summarize", "summarize_throughput", "place", "call_quality", "rpm"],
)
def test_the_browser_engine_does_as_the_python_engine(browser, kind):
    cases = json.loads(CASES.read_text())[kind]
    found = [
        f"case {i}{d}"
        for i, (got, case) in enumerate(zip(browser[kind], cases, strict=True))
        for d in differences(got, case["want"])
    ]
    assert not found, "\n".join(found[:12])
