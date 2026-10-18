import pytest

from texas2step.config import Location, Settings, load_settings


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
server_id = "dal1"
server_name = "Dallas"
ice_servers = ["stun:stun.example:3478"]

[[locations]]
id = "hou1"
name = "Houston"
url = "https://hou.example/"
"""
    )
    monkeypatch.setenv("TEXAS2STEP_CONFIG", str(path))
    monkeypatch.setenv("TEXAS2STEP_SERVER_NAME", "Dallas DC1")
    monkeypatch.setenv("TEXAS2STEP_WEBRTC_ENABLED", "off")
    monkeypatch.setenv("TEXAS2STEP_CORS_ORIGINS", "https://a.example, https://b.example")
    s = load_settings()
    assert s.server_id == "dal1"
    assert s.server_name == "Dallas DC1"
    assert s.webrtc_enabled is False
    assert s.ice_servers == ["stun:stun.example:3478"]
    assert s.cors_origins == ["https://a.example", "https://b.example"]
    assert [loc.id for loc in s.all_locations()] == ["dal1", "hou1"]


def test_rtc_port_range(monkeypatch):
    monkeypatch.setenv("TEXAS2STEP_RTC_PORT_MIN", "50000")
    monkeypatch.setenv("TEXAS2STEP_RTC_PORT_MAX", "50999")
    assert load_settings().rtc_ports == (50000, 50999)
    assert Settings().rtc_ports is None
    for low, high in [(50000, 0), (0, 50999), (50999, 50000), (60000, 70000)]:
        with pytest.raises(ValueError, match="rtc_port_min"):
            Settings(rtc_port_min=low, rtc_port_max=high)


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
