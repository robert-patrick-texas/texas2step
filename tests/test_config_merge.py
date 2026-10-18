"""texas2step config merge: carry an operator's settings into a new release's template."""

import os
import stat
import tomllib

import pytest

from texas2step import config_merge
from texas2step.cli import main
from texas2step.config_merge import TEMPLATE, merge

OLD = """# my notes about this server
server_name = "dallas.example.net"
rtc_port_min = 61000
rtc_port_max = 61999
results_path = "/srv/texas2step/results.db"
cors_origins = ["https://a.example", "https://b.example"]
brand_notice = "Authorized use only. \\"Quotes\\", back\\\\slash, café"
old_setting = 5

[[locations]]
id = "houston"
name = "houston.example.net"
url = "https://houston.example.net/texas2step"
"""


def template() -> str:
    return TEMPLATE.read_text()


def test_values_land_in_the_new_template():
    merged = merge(OLD, template())
    got = tomllib.loads(merged.text)
    old = tomllib.loads(OLD)
    for name in ("server_name", "rtc_port_min", "rtc_port_max", "results_path", "cors_origins"):
        assert got[name] == old[name], name
    assert got["brand_notice"] == 'Authorized use only. "Quotes", back\\slash, café'
    assert got["locations"] == old["locations"]
    # In place of the template's commented default, not appended somewhere.
    assert merged.text.index('server_name = "dallas.example.net"') < merged.text.index(
        "# server_location"
    )
    # Settings this version added arrive as commented defaults.
    assert '# default_theme = "system"' in merged.text
    # Settings it no longer has are kept as comments, out of the server's way.
    assert "old_setting" not in got and "# old_setting = 5" in merged.text
    assert merged.retired == ["old_setting"]
    assert "locations" in merged.kept


def test_merge_is_idempotent_and_the_template_merges_to_itself():
    once = merge(OLD, template()).text
    assert merge(once, template()).text == once
    assert merge(template(), template()).text == template()


def test_renamed_settings_move_to_their_new_name(monkeypatch):
    monkeypatch.setattr(config_merge, "RENAMED", {"site_name": "server_name"})
    merged = merge('site_name = "Austin"\n', template())
    assert tomllib.loads(merged.text)["server_name"] == "Austin"
    assert merged.renamed == ["site_name -> server_name"]


def test_values_this_version_rejects_stop_the_merge():
    with pytest.raises(ValueError, match="default_theme"):
        merge('default_theme = "neon"\n', template())


def test_command_previews_then_writes_with_a_backup(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text(OLD)
    path.chmod(0o640)

    main(["config", "merge", "--config", str(path)])
    out = capsys.readouterr().out
    assert "+server_name" in out and "Nothing written" in out  # a preview only
    assert path.read_text() == OLD

    main(["config", "merge", "--config", str(path), "--write"])
    assert path.read_text() == merge(OLD, template()).text
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    backups = list(tmp_path.glob("config.toml.*.bak"))
    assert len(backups) == 1 and backups[0].read_text() == OLD

    main(["config", "merge", "--config", str(path)])
    assert "up to date" in capsys.readouterr().out


def test_command_takes_values_from_a_dpkg_old_file(tmp_path):
    # The operator let dpkg install the new file; their own is now config.toml.dpkg-old.
    path = tmp_path / "config.toml"
    path.write_text(template())
    (tmp_path / "config.toml.dpkg-old").write_text(OLD)
    main(["config", "merge", "--config", str(path), "--from", str(path) + ".dpkg-old", "--write"])
    assert tomllib.loads(path.read_text())["server_name"] == "dallas.example.net"
    assert os.path.exists(path)
