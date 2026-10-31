"""texas2step config set / add-location: change settings in place, keeping the file."""

import stat
import tomllib

import httpx
import pytest

from texas2step import cli
from texas2step.cli import main
from texas2step.config_edit import add_location, parse_value, set_values
from texas2step.config_merge import TEMPLATE, merge

MINE = """# my notes about this server
server_name = "dallas.example.net"   # the name people know
# rtc_port_min = 0
rtc_port_max = 0

[[brand_links]]
label = "Service desk"
url = "https://help.example.net"
"""


def test_set_replaces_lines_in_place_and_keeps_comments():
    edited = set_values(
        MINE, {"server_name": "houston.example.net", "rtc_port_min": 61000, "rtc_port_max": 61999}
    )
    got = tomllib.loads(edited.text)
    assert (got["server_name"], got["rtc_port_min"], got["rtc_port_max"]) == (
        "houston.example.net",
        61000,
        61999,
    )
    assert got["brand_links"] == tomllib.loads(MINE)["brand_links"]
    assert "# my notes about this server" in edited.text
    assert 'server_name = "houston.example.net"   # the name people know' in edited.text
    assert "# rtc_port_min" not in edited.text  # the commented line became the setting
    assert edited.changes == [
        'server_name: "dallas.example.net" -> "houston.example.net"',
        "rtc_port_min: 0 (default) -> 61000",
        "rtc_port_max: 0 -> 61999",
    ]


def test_set_fills_in_the_templates_commented_default():
    edited = set_values(TEMPLATE.read_text(), {"server_id": "houston"})
    assert '\nserver_id = "houston"                   # short id' in edited.text
    assert "# server_id" not in edited.text
    # The line is written as config merge writes it, so a later merge has nothing to do.
    assert merge(edited.text, TEMPLATE.read_text()).text == edited.text


def test_set_adds_a_missing_setting_above_the_tables():
    edited = set_values(MINE, {"server_location": "Houston, TX"})
    assert edited.text.index("server_location") < edited.text.index("[[brand_links]]")
    assert tomllib.loads(edited.text)["server_location"] == "Houston, TX"


def test_set_is_idempotent():
    once = set_values(MINE, {"server_name": "houston.example.net"}).text
    again = set_values(once, {"server_name": "houston.example.net"})
    assert again.text == once and not again.changes


@pytest.mark.parametrize(
    ("name", "raw", "value"),
    [
        ("rtc_port_min", "61000", 61000),
        ("server_name", "houston.example.net", "houston.example.net"),
        ("server_name", '"houston.example.net"', "houston.example.net"),
        ("server_id", "123", "123"),
        ("webrtc_enabled", "no", False),
        ("cors_origins", '["https://a.example"]', ["https://a.example"]),
        (
            "cors_origins",
            "https://a.example,https://b.example",
            ["https://a.example", "https://b.example"],
        ),
    ],
)
def test_values_are_read_like_the_server_reads_them(name, raw, value):
    assert parse_value(name, raw) == value


def test_set_refuses_unknown_tables_and_invalid_values():
    with pytest.raises(ValueError, match="unknown setting"):
        parse_value("server_nmae", "x")
    with pytest.raises(ValueError, match="edit it in the file"):
        parse_value("locations", "[]")
    with pytest.raises(ValueError, match="rtc_port_min"):
        set_values(MINE, {"rtc_port_min": 70000, "rtc_port_max": 70001})


HOU = {
    "id": "houston",
    "name": "houston.example.net",
    "url": "https://houston.example.net/texas2step/",
}


def test_add_location_after_the_templates_example():
    edited = add_location(TEMPLATE.read_text(), {**HOU, "location": "Houston, TX"})
    got = tomllib.loads(edited.text)
    assert got["locations"] == [
        {
            "id": "houston",
            "name": "houston.example.net",
            "url": "https://houston.example.net/texas2step",
            "location": "Houston, TX",
        }
    ]
    assert edited.text.index("# location = ") < edited.text.index("\n[[locations]]")
    assert edited.text.index("\n[[locations]]") < edited.text.index("# [[brand_links]]")


def test_add_location_after_the_last_one_and_above_other_tables():
    first = add_location(MINE, HOU).text
    second = add_location(first, {"id": "aus1", "name": "Austin", "url": "https://aus.example"})
    got = tomllib.loads(second.text)
    assert [loc["id"] for loc in got["locations"]] == ["houston", "aus1"]
    assert got["brand_links"] == tomllib.loads(MINE)["brand_links"]
    assert got["server_name"] == "dallas.example.net"


def test_add_location_never_captures_top_level_settings():
    text = '# [[locations]]\n# id = "x"\n\nserver_name = "Late"\n'
    got = tomllib.loads(add_location(text, HOU).text)
    assert got["server_name"] == "Late" and got["locations"][0]["id"] == "houston"


def test_add_location_skips_a_listed_server_and_refuses_this_one():
    listed = add_location(MINE, HOU).text
    again = add_location(listed, {**HOU, "id": "other"})  # same URL, other id
    assert again.text == listed and "already listed" in again.notes[0]
    with pytest.raises(ValueError, match="own server_id"):
        add_location('server_id = "houston"\n', HOU)
    with pytest.raises(ValueError, match="needs an name"):
        add_location(MINE, {"id": "x", "url": "https://x.example"})


@pytest.fixture
def settings_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(TEMPLATE.read_text())
    path.chmod(0o640)
    return path


def test_cli_set_previews_then_writes_with_a_backup(settings_file, capsys):
    main(["config", "set", "server_name=houston.example.net", "--config", str(settings_file)])
    out = capsys.readouterr().out
    assert '+server_name = "houston.example.net"' in out and "Nothing written" in out
    assert settings_file.read_text() == TEMPLATE.read_text()

    main(
        [
            "config",
            "set",
            "server_name=houston.example.net",
            "--config",
            str(settings_file),
            "--write",
        ]
    )
    main(["config", "set", "server_id=houston", "--config", str(settings_file), "--write"])
    got = tomllib.loads(settings_file.read_text())
    assert (got["server_name"], got["server_id"]) == ("houston.example.net", "houston")
    assert stat.S_IMODE(settings_file.stat().st_mode) == 0o640
    # Two writes in the same second keep both backups.
    backups = sorted(settings_file.parent.glob("config.toml.*.bak"))
    assert len(backups) == 2
    assert TEMPLATE.read_text() in {b.read_text() for b in backups}

    main(["config", "set", "server_id=houston", "--config", str(settings_file), "--write"])
    assert "unchanged" in capsys.readouterr().out


def test_cli_set_in_the_container_says_to_restart_it(settings_file, capsys, monkeypatch):
    from texas2step import update

    marker = settings_file.with_name("container")
    marker.write_text("ghcr.io/robert-patrick-texas/texas2step\n")
    monkeypatch.setattr(update, "CONTAINER", marker)
    main(["config", "set", "server_id=houston", "--config", str(settings_file)])
    assert "To apply: texas2step config set server_id=houston --write" in capsys.readouterr().out
    main(["config", "set", "server_id=houston", "--config", str(settings_file), "--write"])
    out = capsys.readouterr().out
    assert "Apply the settings: restart the container (docker restart texas2step)" in out
    assert "systemctl" not in out


def test_cli_set_rejects_bad_input(settings_file):
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        main(["config", "set", "server_name", "--config", str(settings_file)])
    with pytest.raises(SystemExit, match="unknown setting"):
        main(["config", "set", "nope=1", "--config", str(settings_file)])


def test_cli_add_location_asks_the_server_who_it_is(settings_file, monkeypatch, capsys):
    me = {"id": "houston", "name": "houston.example.net", "location": "Houston, TX"}
    v4, v6 = (
        "https://v4.houston.example.net/texas2step",
        "https://v6.houston.example.net/texas2step",
    )
    h3 = "https://h3-6.houston.example.net/texas2step"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/texas2step/api/v1/locations":
            # Its own entry carries its single-family URLs; the other one isn't it.
            other = {"id": "austin", "url": "https://austin.example.net/texas2step"}
            own = {**me, "url": "", "url_v4": v4, "url_v6": v6, "url_v6_http3": h3}
            return httpx.Response(200, json={"locations": [own, other]})
        assert request.url.path == "/texas2step/api/v1/info"
        return httpx.Response(200, json={"server": me})

    real = httpx.Client
    monkeypatch.setattr(
        cli.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    url = "https://houston.example.net/texas2step"
    main(["config", "add-location", url, "--config", str(settings_file), "--write"])
    got = tomllib.loads(settings_file.read_text())["locations"]
    assert got == [{**me, "url": url, "url_v4": v4, "url_v6": v6, "url_v6_http3": h3}]

    main(["config", "add-location", url, "--config", str(settings_file), "--write"])
    assert "already listed" in capsys.readouterr().out


def test_cli_add_location_needs_a_name_when_the_server_cannot_be_reached(
    settings_file, monkeypatch
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    real = httpx.Client
    monkeypatch.setattr(
        cli.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    args = ["config", "add-location", "https://down.example", "--config", str(settings_file)]
    with pytest.raises(SystemExit, match="give --id and --name"):
        main(args)
    main([*args, "--id", "down1", "--name", "Down", "--write"])
    assert tomllib.loads(settings_file.read_text())["locations"][0]["id"] == "down1"


def test_config_set_writes_values_without_the_spaces_around_them():
    assert parse_value("server_name", "  houston.example.net ") == "houston.example.net"
    assert parse_value("server_name", '" houston.example.net "') == "houston.example.net"
    assert parse_value("internal_networks", " 10.0.0.0/8 , 2001:db8::/32 ") == [
        "10.0.0.0/8",
        "2001:db8::/32",
    ]
    assert parse_value("internal_networks", '[" 10.0.0.0/8 ", "2001:db8::/32 "]') == [
        "10.0.0.0/8",
        "2001:db8::/32",
    ]
