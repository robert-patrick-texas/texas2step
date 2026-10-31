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


def test_report_says_the_version_only_where_the_server_does():
    head = "texas2step → https://www.example.net/texas2step  (dallas.example.net"
    assert cli._report(RESULT).splitlines()[0] == head + " · v1)"
    hidden = {**RESULT, "server": {**RESULT["server"], "version": None}}
    assert cli._report(hidden).splitlines()[0] == head + ")"


def test_report_without_this_computer():
    # --no-local, or a run where nothing could be told.
    assert "this computer" not in cli._report(RESULT)
    no_mac = {**RESULT, "client": {**RESULT["client"], "local_ip": "10.8.0.2"}}
    assert "this computer 10.8.0.2" in cli._report(no_mac).splitlines()


def test_report_gives_a_rate_limit_under_its_transfer():
    download = {
        "mbps": 485.4,
        "peak_mbps": 990.0,
        "streams": 4,
        "rate_limit": {
            "from_mbps": 938.2,
            "to_mbps": 312.9,
            "at_s": 4.2,
            "before_bytes": 427_000_000,
        },
    }
    upload = {**download, "rate_limit": None}
    lines = cli._report({**RESULT, "download": download, "upload": upload}).splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("  Download"))
    assert lines[i + 1] == (
        "                 rate limit: 938.2 Mbps until 4.2s, 427 MB in, then 312.9 Mbps to the end"
    )
    assert sum("rate limit" in line for line in lines) == 1
    boost = {
        **download,
        "rate_limit": {**download["rate_limit"], "at_s": 12.0, "before_bytes": 1_409_600_000},
    }
    assert "until 12s, 1.4 GB in" in cli._report({**RESULT, "download": boost})


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
    ippm = {
        **judged["responsiveness"],
        "download": {"setup_ms": 40.0},
        "upload": {"setup_ms": 120.25},
        "idle_setup_ms": 25.0,
    }
    assert (
        "  Responsiveness      1,500 RPM   (↓ 3,000 · ↑ 1,500"
        " · a new connection 25.0 ms idle, 120.2 ms under load)"
        in cli._report({**judged, "responsiveness": ippm}).splitlines()
    )
    side = {"mos": 4.4, "rating": "excellent", "longest_gap_ms": 0.0, "late_pct": 0.0}
    each = {**judged["call"], "basis": "each way", "up": side, "down": {**side, "mos": 4.3}}
    assert (
        "  Call quality          4.4 MOS   excellent   (you hear 4.3 · they hear 4.4"
        " · longest gap ↓ 0 ↑ 0 ms · late ↓ 0.00 % ↑ 0.00 %)"
        in cli._report({**judged, "call": each}).splitlines()
    )
    gap = {**judged["call"], "mos_rating": "excellent", "rating": "good", "longest_gap_ms": 140.0}
    lines = cli._report({**judged, "call": gap}).splitlines()
    assert any(
        line.startswith(
            "  Call quality          4.4 MOS   good, for its gap (the score alone: excellent)"
        )
        for line in lines
    )


def test_report_gives_one_stream_beside_the_download():
    multi = {"mbps": 800.0, "peak_mbps": 900.0, "streams": 4}
    one = {"mbps": 200.0, "peak_mbps": 250.0, "streams": 1}
    lines = cli._report({**RESULT, "download": multi, "one_stream": one}).splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("  Download"))
    assert lines[at + 1] == (
        "  One stream         200.0 Mbps   peak 250.0   (a single file copy · 25 % of 4 streams)"
    )
    assert not any(line.startswith("  One stream") for line in cli._report(RESULT).splitlines())


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


def test_arguments_lose_the_spaces_around_them():
    args = cli.trimmed(
        cli.build_parser().parse_args(
            ["run", " https://dallas.example.net/texas2step ", "--code", " maple.river.42 "]
        )
    )
    assert (args.url, args.code) == ("https://dallas.example.net/texas2step", "maple.river.42")
    args = cli.trimmed(cli.build_parser().parse_args(["config", "set", " server_id=houston "]))
    assert args.pairs == ["server_id=houston"]
