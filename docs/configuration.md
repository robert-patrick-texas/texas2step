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
| `brand_name` | `texas2step` | Name in the header and the browser tab |
| `brand_organization` | `""` | Organization shown beside the name and in the footer |
| `brand_url` | `""` | The organization's website; the organization name links to it |
| `brand_logo` | `""` | Path to an SVG, PNG, JPEG or WebP file shown in place of the star |
| `brand_notice` | `""` | A line of text across the top of every page |
| `brand_links` | `[]` | Links in the header and footer, as `[[brand_links]]` tables (TOML only) |
| `default_theme` | `system` | `system`, `light`, `dark`, `federal`, `texas` or `america` |

## Branding and themes

texas2step can carry your organization's identity instead of its own. All of it is
optional, and none of it changes what is measured.

```toml
brand_name = "NetCheck"                       # header and browser tab
brand_organization = "Acme IT"
brand_url = "https://www.acme.example"        # "Acme IT" links here
brand_logo = "/etc/texas2step/logo.svg"       # replaces the star mark
brand_notice = "Authorized use only. Activity on this system may be monitored."
default_theme = "federal"

[[brand_links]]
label = "Service desk"
url = "https://help.acme.example"

[[brand_links]]
label = "Email the NOC"
url = "mailto:noc@acme.example"
```

* **Where it shows.** The name and logo lead the header. The organization sits beside them
  and opens its website in a new tab. `brand_links` appear in the header on wide screens
  and in the footer on every screen. The notice runs across the top of every page.
  Once the site is renamed, the footer reads "Powered by texas2step".
* **Logo.** SVG, PNG, JPEG, WebP or GIF, about 30 px tall on screen; wide logos keep their
  shape, up to 180 px wide. The `texas2step` service user must be able to read the file
  (`/etc/texas2step/` is a good place). If the file is missing or unreadable, the server
  logs a warning and keeps the star.
* **Links** must start with `https://`, `http://`, `mailto:` or `tel:`. Anything else stops
  the server at startup with an error naming the setting. `[[brand_links]]` can only be
  set in the TOML file, not through environment variables.
* **Themes.** Visitors choose from the palette button in the header. Their choice is
  remembered in their browser. `default_theme` sets what a first-time visitor sees;
  `system` follows their light or dark OS setting.

  | Theme | Look |
  |---|---|
  | Light, Dark | The original texas2step look: warm neutrals and a Texas-sunset accent |
  | Federal | Deep-navy masthead with a gold rule, cool grey-blue surfaces, serif headings and crisp corners, in the style of federal websites such as whitehouse.gov and energy.gov (colours and type only, no seals or insignia) |
  | Texas | Night-sky navy, a red-to-gold horizon and a blue, white and red rule under the header |
  | America | Crisp white with Old Glory red and blue, a flag-stripe band and a faint field of stars |

  Charts keep the same validated data colours in every theme, so results look the same
  whichever theme took them.

Restart the service after changing any of these: `sudo systemctl restart texas2step`.

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
