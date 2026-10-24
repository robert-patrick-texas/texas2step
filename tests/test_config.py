import pytest

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
"""
    )
    s = load_settings(path)
    assert (s.brand_name, s.default_theme) == ("Network Check", "federal")
    assert [link.label for link in s.brand_links] == ["Service desk", "Email the NOC"]


def test_branding_rejects_unsafe_or_unknown_values(tmp_path):
    with pytest.raises(ValueError, match="brand_url"):
        Settings(brand_url="javascript:alert(1)")
    with pytest.raises(ValueError, match="brand_links"):
        Settings(brand_links=[Link("Help", "javascript:alert(1)")])
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
    assert Settings().monitor_profile == "path" and Settings().monitor_video is True
    with pytest.raises(ValueError, match="monitor_profile"):
        Settings(monitor_profile="ping")
    with pytest.raises(ValueError, match="monitor_video"):
        Settings(monitor_profile="video", monitor_video=False)
