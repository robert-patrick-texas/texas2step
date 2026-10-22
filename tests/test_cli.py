"""The CLI's human-readable report."""

from texas2step import cli

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
