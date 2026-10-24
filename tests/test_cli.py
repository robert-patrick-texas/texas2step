"""The command line: its human-readable report, and results clear."""

import io
import sys

import pytest

from texas2step import cli
from texas2step.store import ResultStore

RESULT = {
    "server": {
        "url": "https://www.example.net/texas2step",
        "name": "dallas.example.net",
        "version": "1",
    },
    "client": {"ip": "203.0.113.7", "family": "IPv4", "protocol": "HTTP/1.1"},
}


def test_report_names_this_computer():
    local = {
        **RESULT,
        "client": {**RESULT["client"], "local_ip": "10.0.0.10", "mac": "00:11:22:33:44:55"},
    }
    lines = cli._report(local).splitlines()
    assert lines[1] == "client 203.0.113.7 (IPv4, HTTP/1.1)"
    assert lines[2] == "this computer 10.0.0.10 · MAC 00:11:22:33:44:55"


def test_report_without_this_computer():
    # --no-local, or a run where nothing could be told.
    assert "this computer" not in cli._report(RESULT)
    no_mac = {**RESULT, "client": {**RESULT["client"], "local_ip": "10.8.0.2"}}
    assert "this computer 10.8.0.2" in cli._report(no_mac).splitlines()


def test_report_gives_call_quality_and_responsiveness():
    lines = cli._report(RESULT).splitlines()
    assert "  Call quality   —" in lines
    assert "  Responsiveness —" in lines
    judged = {
        **RESULT,
        "call": {
            "mos": 4.36,
            "rating": "excellent",
            "late_pct": 0.2,
            "longest_gap_ms": 40.0,
            "basis": "round trip",
        },
        "responsiveness": {"rpm": 1500, "download_rpm": 3000, "upload_rpm": 1500},
    }
    lines = cli._report(judged).splitlines()
    assert (
        "  Call quality          4.4 MOS   excellent   (late 0.20 % · longest gap 40 ms"
        " · from round trips)" in lines
    )
    assert "  Responsiveness      1,500 RPM   (↓ 3,000 · ↑ 1,500, under load)" in lines
    gap = {**judged["call"], "mos_rating": "excellent", "rating": "good", "longest_gap_ms": 140.0}
    lines = cli._report({**judged, "call": gap}).splitlines()
    assert any(
        line.startswith(
            "  Call quality          4.4 MOS   good, for its gap (the score alone: excellent)"
        )
        for line in lines
    )


def test_results_clear(tmp_path, monkeypatch, capsys):
    db = tmp_path / "results.db"
    conf = tmp_path / "config.toml"
    conf.write_text(f'results_path = "{db}"\n')
    clear = ["results", "clear", "--config", str(conf)]

    cli.main(clear)
    assert capsys.readouterr().out == f"No saved results: there is no {db}.\n"

    store = ResultStore(db)
    for server in ("dallas", "dallas", "austin"):
        store.add({"version": 1, "server": {"id": server}}, "192.0.2.1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # no terminal to ask on
    with pytest.raises(SystemExit, match="no terminal to ask on; add --yes"):
        cli.main(clear)
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"{db} holds 3 saved results, on 20")
    assert out[1] == "By server id: dallas 2, austin 1"

    answer = io.StringIO("n\n")
    answer.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", answer)
    with pytest.raises(SystemExit, match="Nothing changed"):
        cli.main(clear)
    assert store.tally()[0] == 3

    cli.main([*clear, "--yes"])
    assert capsys.readouterr().out.endswith(
        "Deleted 3 saved results. History starts over with the next test saved.\n"
    )
    assert store.tally()[0] == 0
    cli.main(clear)
    assert capsys.readouterr().out == f"{db} holds no saved results.\n"
    store.close()
