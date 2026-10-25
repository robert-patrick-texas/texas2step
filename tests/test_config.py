import pytest

from texas2step.access import is_internal
from texas2step.config import Link, Location, Settings, load_settings


def test_defaults(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("TEXAS2STEP_"):
            monkeypatch.delenv(key)
    s = load_settings()
    assert s.server_name == "texas2step"
    assert s.webrtc_enabled is True
    assert s.all_locations()[0].id == "local"


def test_toml_and_env_override(tmp_path, monkeypatch):
    path = tmp_path / "t.toml"
    path.write_text(
        """
server_id = "dallas"
server_name = "Dallas"
ice_servers = ["stun:stun.example:3478"]

[[locations]]
id = "houston"
name = "Houston"
url = "https://houston.example.net/"
"""
    )
    monkeypatch.setenv("TEXAS2STEP_CONFIG", str(path))
    monkeypatch.setenv("TEXAS2STEP_SERVER_NAME", "dallas.example.net1")
    monkeypatch.setenv("TEXAS2STEP_WEBRTC_ENABLED", "off")
    monkeypatch.setenv("TEXAS2STEP_CORS_ORIGINS", "https://a.example, https://b.example")
    s = load_settings()
    assert s.server_id == "dallas"
    assert s.server_name == "dallas.example.net1"
    assert s.webrtc_enabled is False
    assert s.ice_servers == ["stun:stun.example:3478"]
    assert s.cors_origins == ["https://a.example", "https://b.example"]
    assert [loc.id for loc in s.all_locations()] == ["dallas", "houston"]


def test_rtc_port_range(monkeypatch):
    monkeypatch.setenv("TEXAS2STEP_RTC_PORT_MIN", "50000")
    monkeypatch.setenv("TEXAS2STEP_RTC_PORT_MAX", "50999")
    assert load_settings().rtc_ports == (50000, 50999)
    assert Settings().rtc_ports is None
    for low, high in [(50000, 0), (0, 50999), (50999, 50000), (60000, 70000)]:
        with pytest.raises(ValueError, match="rtc_port_min"):
            Settings(rtc_port_min=low, rtc_port_max=high)


def test_update_url():
    assert Settings().update_url.startswith("https://api.github.com/repos/")
    assert Settings(update_url="https://mirror.example.net/texas2step").update_url
    assert Settings(update_url="file:///opt/texas2step/mirror").update_url
    for url in ("mirror.example.net/texas2step", "file://mirror/texas2step", "/opt/mirror"):
        with pytest.raises(ValueError, match="update_url"):
            Settings(update_url=url)


def test_unknown_key_rejected(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('sever_name = "typo"\n')
    with pytest.raises(ValueError, match="sever_name"):
        load_settings(path)


def test_target_allow_list():
    s = Settings(
        locations=[
            Location(id="a", name="A", url="https://a.example/", url_v6="https://v6.a.example")
        ]
    )
    assert s.is_allowed_target("https://a.example")
    assert s.is_allowed_target("https://v6.a.example/")
    assert not s.is_allowed_target("http://169.254.169.254")
    assert Settings(allow_any_target=True).is_allowed_target("http://anything")


def test_branding_from_toml(tmp_path):
    path = tmp_path / "brand.toml"
    path.write_text(
        """
brand_name = "Network Check"
brand_organization = "Acme IT"
brand_url = "https://www.acme.example"
default_theme = "federal"

[[brand_links]]
label = "Service desk"
url = "https://help.acme.example"

[[brand_links]]
label = "Email the NOC"
url = "mailto:noc@acme.example"

[[brand_footer_links]]
label = "Privacy"
url = "https://www.acme.example/privacy"
"""
    )
    s = load_settings(path)
    assert (s.brand_name, s.default_theme) == ("Network Check", "federal")
    assert [link.label for link in s.brand_links] == ["Service desk", "Email the NOC"]
    assert [link.label for link in s.brand_footer_links] == ["Privacy"]


def test_branding_rejects_unsafe_or_unknown_values(tmp_path):
    with pytest.raises(ValueError, match="brand_url"):
        Settings(brand_url="javascript:alert(1)")
    with pytest.raises(ValueError, match="brand_links"):
        Settings(brand_links=[Link("Help", "javascript:alert(1)")])
    with pytest.raises(ValueError, match="brand_footer_links: every link needs a label"):
        Settings(brand_footer_links=[Link(" ", "https://acme.example")])
    with pytest.raises(ValueError, match=r"brand_footer \(Help\): URL must start with"):
        Settings(brand_footer="Ask for [Help](javascript:alert(1)) here.")
    assert Settings(brand_footer="[not a link] (here) and [x](y z)").brand_footer
    with pytest.raises(ValueError, match="default_theme"):
        Settings(default_theme="neon")
    with pytest.raises(ValueError, match="brand_credit"):
        Settings(brand_credit="hidden")
    with pytest.raises(ValueError, match="keep one shown"):
        Settings(
            page_test=False,
            page_monitor=False,
            page_diag=False,
            page_history=False,
            page_api=False,
        )
    assert Settings(page_history=False).pages == ["test", "monitor", "diag", "api"]
    path = tmp_path / "typo.toml"
    path.write_text('[[brand_links]]\nlable = "Help"\nurl = "https://help.example"\n')
    with pytest.raises(ValueError, match="brand_links"):
        load_settings(path)


def test_monitor_profile_is_checked():
    s = Settings()
    assert s.monitor_profile == "path" and s.monitor_voice is True and s.monitor_video is True
    with pytest.raises(ValueError, match="monitor_profile"):
        Settings(monitor_profile="ping")
    with pytest.raises(ValueError, match="monitor_video"):
        Settings(monitor_profile="video", monitor_video=False)
    with pytest.raises(ValueError, match="monitor_voice = false withholds it"):
        Settings(monitor_profile="voice", monitor_voice=False)
    # Path only, or path and voice: path is always offered.
    assert Settings(monitor_voice=False, monitor_video=False).monitor_profile == "path"
    assert Settings(monitor_profile="voice", monitor_video=False).monitor_voice


def test_settings_lose_the_spaces_around_their_text(tmp_path, monkeypatch):
    monkeypatch.delenv("TEXAS2STEP_CONFIG", raising=False)
    path = tmp_path / "spaces.toml"
    path.write_text(
        """
server_id = " houston "
internal_networks = [" 10.0.0.0/8", "2001:db8::/32 ", " 203.0.113.7 "]
access_peer_networks = [" 198.51.100.0/24 "]
ice_servers = [" stun:stun.example.net:3478 "]
brand_footer = \"\"\"

  Line one.
Line two.\t
\"\"\"

[[brand_footer_links]]
label = " Privacy "
url = " https://example.net/privacy "

[[locations]]
id = " dallas "
name = " dallas.example.net "
url = " https://dallas.example.net/texas2step "
"""
    )
    s = load_settings(path)
    assert s.server_id == "houston"
    assert s.internal_networks == ["10.0.0.0/8", "2001:db8::/32", "203.0.113.7"]
    assert s.access_peer_networks == ["198.51.100.0/24"]
    assert s.ice_servers == ["stun:stun.example.net:3478"]
    assert s.brand_footer == "Line one.\nLine two."
    assert s.brand_footer_links == [Link("Privacy", "https://example.net/privacy")]
    assert s.locations[0] == Location(
        "dallas", "dallas.example.net", "https://dallas.example.net/texas2step"
    )
    assert s.is_allowed_target("https://dallas.example.net/texas2step/")
    # Every request is checked against these: a stray space used to fail each one.
    assert is_internal("10.1.2.3", s.internal_networks)
    assert is_internal("2001:db8::5", s.internal_networks)
    assert not is_internal("192.0.2.1", s.internal_networks)

    monkeypatch.setenv("TEXAS2STEP_INTERNAL_NETWORKS", " 10.0.0.0/8 , 2001:db8::/32 ")
    monkeypatch.setenv("TEXAS2STEP_SERVER_NAME", "  houston.example.net ")
    s = load_settings()
    assert s.internal_networks == ["10.0.0.0/8", "2001:db8::/32"]
    assert s.server_name == "houston.example.net"
    # However they are made: here, directly.
    assert Settings(internal_networks=[" 10.0.0.0/8 "]).internal_networks == ["10.0.0.0/8"]
