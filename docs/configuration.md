# Configuration

Settings come from, in increasing priority:

1. built-in defaults,
2. a TOML file named by `TEXAS2STEP_CONFIG` (or `texas2step serve --config FILE`),
3. environment variables `TEXAS2STEP_<SETTING>` (upper case).

Lists in environment variables are comma separated. Booleans accept `1/0`,
`true/false`, `yes/no` and `on/off`. Unknown keys in the TOML file are an error, so
typos don't fail silently. The package installs a fully commented file listing every
setting and its default as `/etc/texas2step/config.toml` (source:
[`deploy/config.toml`](../deploy/config.toml)); restart the service after editing it.

| Setting | Default | Meaning |
|---|---|---|
| `server_id` | `local` | Short identifier stored with results |
| `server_name` | `texas2step` | Display name |
| `server_location` | `""` | Free text, e.g. `Dallas, TX` |
| `server_url_v4` / `server_url_v6` | `""` | IPv4-only / IPv6-only hostnames for this server; enables the address-family switch |
| `max_download_bytes` | 1 GiB | Largest single `/down` response |
| `max_upload_bytes` | 1 GiB | Largest single `/up` body |
| `max_stream_seconds` | 60 | Cap for `/down?seconds=` |
| `webrtc_enabled` | `true` | Offer the UDP probe |
| `ice_servers` | `[]` | STUN/TURN URLs given to both peers, e.g. `["stun:stun.example:3478"]`. No public STUN server is used unless listed here. |
| `max_rtc_sessions` | 32 | Concurrent UDP probe sessions per worker |
| `rtc_session_timeout` | 120 | Seconds of inactivity before a session is closed |
| `rtc_port_min` / `rtc_port_max` | 0 / 0 | Inclusive UDP port range for the probe's server sockets, so a firewall can open just that range. Both 0 lets the OS pick ports. See [Pinning the UDP ports](install.md#pinning-the-udp-ports). |
| `results_enabled` | `true` | Store results and serve History |
| `results_path` | `texas2step.db` | SQLite file (relative to the working directory) |
| `results_max_rows` | 10000 | Oldest results are pruned beyond this |
| `measure_enabled` | `true` | Enable `/api/v1/measure/*` |
| `allow_any_target` | `false` | Let `/measure/*` target any URL, not just configured locations. Leave off on shared networks: it turns the server into a request relay. |
| `default_streams` / `default_seconds` / `default_packets` | 4 / 10 / 500 | Defaults advertised by `/api/v1/info` |
| `cors_origins` | `["*"]` | Origins allowed to call the API from a browser |
| `trusted_proxies` | `["127.0.0.1", "::1"]` | Peers whose `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Prefix` and `X-Client-Protocol` are believed |

## Locations

Every server lists itself first. Add other texas2step servers to offer them in the
picker; browsers then test against them directly (cross-origin), and
`/api/v1/measure/*` may target them.

```toml
[[locations]]
id = "hou1"
name = "Houston DC"
url = "https://hou.example.net/texas2step"
location = "Houston, TX"
url_v4 = "https://v4.hou.example.net/texas2step"   # optional: A record only
url_v6 = "https://v6.hou.example.net/texas2step"   # optional: AAAA record only
```

Each listed server must allow the page's origin in its `cors_origins` (the default `*`
does).
