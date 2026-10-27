"""The cases t2s, the Go client, checks its methodology against still say what this one does."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "methodology_cases.py"


def _script():
    spec = importlib.util.spec_from_file_location("methodology_cases", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cases_are_what_the_python_engine_makes_of_them():
    script = _script()
    assert script.OUT.read_text() == script.render(), (
        "tests/methodology-cases.json is out of date: run "
        "uv run python scripts/methodology_cases.py, then make t2s-test"
    )


def test_the_cases_cover_each_way_and_round_trip_calls():
    cases = json.loads((ROOT / "tests" / "methodology-cases.json").read_text())
    bases = {c["want"]["basis"] for c in cases["call_quality"] if c["want"]}
    assert bases == {"each way", "round trip"}
    assert {c["want"]["basis"] for c in cases["responsiveness"] if c["want"]} == {
        "ippm",
        "latency under load",
    }
