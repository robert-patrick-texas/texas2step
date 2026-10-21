# Configuration

Settings come from, in increasing priority:

1. built-in defaults,
2. a TOML file named by `TEXAS2STEP_CONFIG` (or `texas2step serve --config FILE`),
3. environment variables `TEXAS2STEP_<SETTING>` (upper case).

Lists in environment variables are comma separated. Booleans accept `1/0`,
`true/false`, `yes/no` and `on/off`. Unknown keys in the TOML file are an error, so
typos don't fail silently. The package installs a fully commented file listing every
setting and its default as `/etc/texas2step/config.toml` (source:
[`src/texas2step/config_template.toml`](../src/texas2step/config_template.toml)); restart
the service after editing it. After an upgrade, `texas2step config merge` brings your
file up to date with the new version's template and keeps your values; see
[Upgrade](install.md#upgrade).

| Setting | Default | Meaning |
|---|---|---|
| `server_id` | `local` | Short identifier stored with results |
| `server_name` | `texas2step` | Display name |
| `server_location` | `""` | Free text, e.g. `Dallas, TX` |
| `server_url_v4` / `server_url_v6` | `""` | IPv4-only / IPv6-only base URLs for this server; they add the header's IP drop-down. See [IPv4 and IPv6](#ipv4-and-ipv6). |
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
| `page_test` / `page_monitor` / `page_diag` / `page_history` / `page_api` | `true` | Show that tab of the web UI. Any can be hidden, but not all. A hidden page's address opens the first page shown. The API stays on: `results_enabled` and `measure_enabled` turn parts of it off, and `/api/docs` stays reachable without the API tab. |
| `default_streams` / `default_seconds` / `default_packets` | 4 / 10 / 500 | Defaults advertised by `/api/v1/info` |
| `cors_origins` | `["*"]` | Origins allowed to call the API from a browser |
| `trusted_proxies` | `["127.0.0.1", "::1"]` | Peers whose `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Prefix` and `X-Client-Protocol` are believed |
| `debug` | `false` | Also log every request and the UDP probe's WebRTC detail; see [Logging](#logging) |
| `update_url` | GitHub's API for the project | Where `texas2step update` looks for releases: GitHub, GitHub Enterprise or a mirror; see [Without access to GitHub](install.md#without-access-to-github) |
| `brand_name` | `texas2step` | Name in the header and the browser tab |
| `brand_organization` | `""` | Organization shown beside the name and in the footer |
| `brand_url` | `""` | The organization's website; the organization name links to it |
| `brand_logo` | `""` | Path to an SVG, PNG, JPEG or WebP file shown in place of the star |
| `brand_notice` | `""` | A line of text across the top of every page |
| `brand_links` | `[]` | Links in the header and footer, as `[[brand_links]]` tables (TOML only) |
| `brand_credit` | `link` | The footer's "texas2step &lt;version&gt;": `link` to the project's site, plain `text`, or `none` to leave the project's name and version out of the footer |
| `default_theme` | `system` | `system`, `light`, `dark`, `america`, `energy`, `federal`, `military` or `texas` |

## Changing settings from the command line

You can edit `/etc/texas2step/config.toml` by hand. These commands make the common changes
for you and keep the rest of the file as it is, comments included. Each one first shows
the change and writes nothing. Add `--write` to apply it, which keeps a dated backup
beside the file. Then restart the service.

```bash
sudo texas2step config set server_id=houston server_name="houston.example.net" rtc_port_min=61000 rtc_port_max=61999
sudo texas2step config set server_id=houston server_name="houston.example.net" --write
sudo texas2step config add-location https://dallas.example.net/texas2step --write
sudo systemctl restart texas2step
```

* **`config set KEY=VALUE ...`** writes each setting on its own line. If the file has the
  setting commented out, the line is filled in where it stands; otherwise it is added
  above the first `[[table]]`. A value is read as TOML when it is valid TOML (`61000`,
  `true`, `["a", "b"]`), and as text otherwise. Unknown settings, and values the server
  would refuse, are rejected before anything is written. Edit `[[locations]]` and
  `[[brand_links]]` in the file, or use `add-location`.
* **`config add-location URL`** lists another texas2step server in the location picker.
  It asks that server for its id, name and location (`/api/v1/info`), and for its
  IPv4-only and IPv6-only URLs (its own entry in `/api/v1/locations`), using the
  system's trusted certificates. Give `--id`, `--name`, `--location`, `--url-v4` and
  `--url-v6` to set them yourself, or when the server can't be reached. A server that is already listed, by id or by URL, is left
  as it is.
* **`config merge`** brings the whole file up to date after an upgrade; see
  [Upgrade](install.md#upgrade).

`--config FILE` works on another file instead. The
[setup tool](install.md#install-or-upgrade-with-the-setup-tool) uses `config set` and
`add-location` to apply its `--id`, `--name`, `--location`, `--url-v4`, `--url-v6`,
`--udp-ports` and `--peer` options.

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
  Once the site is renamed, the footer reads "Powered by texas2step". The footer's
  "texas2step &lt;version&gt;" links to the project's site; `brand_credit = "text"` keeps the
  words without the link, and `brand_credit = "none"` leaves the project's name, version
  and "Powered by" out of the footer, for sites that carry only their own branding. The
  version is still at `/api/v1/health`.
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
  | America | Cool white with Old Glory blue and red: the window framed by a navy rule across the top and red rules down both sides, and a faint field of stars |
  | Energy | A national lab after hours: near-black blue surfaces, Cherenkov blue into fusion-plasma violet, a glowing beam line under the header and a faint machine-room lattice, for the Department of Energy's supercomputing, fusion and nuclear sites (colours only, no seals or insignia) |
  | Federal | Deep-navy masthead with a gold rule, cool grey-blue surfaces, serif headings and crisp corners, in the style of federal websites such as whitehouse.gov and energy.gov (colours and type only, no seals or insignia) |
  | Military | Ranger-green masthead with a coyote-tan rule, khaki surfaces, olive-drab buttons, condensed capital headings and a faint topographic map (colours and type only, no insignia, branch marks or camouflage) |
  | Texas | Night-sky navy, a red-to-gold horizon and a blue, white and red rule under the header |

  Charts keep the same validated data colours in every theme, so results look the same
  whichever theme took them.

Restart the service after changing any of these: `sudo systemctl restart texas2step`.

## Locations

Every server lists itself first. Add other texas2step servers to offer them in the
picker; browsers then test against them directly (cross-origin), and
`/api/v1/measure/*` may target them. A visitor's choice of server is remembered in
their browser, like the theme, so a return visit tests the same server.

```toml
[[locations]]
id = "houston"
name = "houston.example.net"
url = "https://houston.example.net/texas2step"
location = "Houston, TX"
url_v4 = "https://v4.houston.example.net/texas2step"   # optional: A record only
url_v6 = "https://v6.houston.example.net/texas2step"   # optional: AAAA record only
```

Each listed server must allow the page's origin in its `cors_origins` (the default `*`
does).

With two or more servers listed, **Monitor** can probe two of them at once from the
same browser and compare them on one chart. The browser reaches each server directly,
so each one needs its UDP range open to clients, as for the packet-loss test. The
address of a comparison, such as `/monitor?a=dallas&b=houston` (the servers' ids), opens
it again later.

## IPv4 and IPv6

When a name has both an A and an AAAA record, the browser picks IPv4 or IPv6 by itself,
and a web page can't tell it which. To let visitors choose, give the server two more
names: one with only an A record and one with only an AAAA record. Point both straight
at the server: a name proxied through a CDN, such as Cloudflare's proxied records,
answers with the CDN's own addresses in both families, and the CDN would sit in the
middle of every measurement. The site's certificate must cover both names, and nginx
must answer for them, either by listing them in `server_name` or because the site is
the default server for port 443. Then set:

```toml
server_url_v4 = "https://v4.dallas.example.net/texas2step"
server_url_v6 = "https://v6.dallas.example.net/texas2step"
```

The header then shows an **IP** drop-down with **Auto**, **IPv4 only** and
**IPv6 only**, each offered only when its name is set. The choice holds the HTTP tests
to that name, and holds the UDP probes to the server's addresses of that family.
**Monitor** shows the family each UDP path uses. A server without these names has no
drop-down, and the browser picks the family. The setup tool's `--url-v4` and `--url-v6`
set these and check each name's DNS records. For the other servers in the picker, the
`url_v4` and `url_v6` of their `[[locations]]` entries do the same;
`texas2step config add-location` and the setup tool's `--peer` fill them in from the
other server.

## Logging

The service logs to the journal (`journalctl -u texas2step -f`), and by default only what
needs attention:

* start, stop and worker messages;
* warnings and errors from texas2step and from the WebRTC libraries behind the UDP probe;
* a line for each request that failed (status 400 or above), such as an upload over
  `max_upload_bytes`:

```text
WARNING:  203.0.113.7 - "POST /api/v1/up HTTP/2.0" 413
```

The address is the client's, as `/api/v1/whoami` reports it: behind nginx, it comes from
`X-Forwarded-For` (see `trusted_proxies`), and the HTTP version is the one the client
used with nginx. Successful requests aren't logged, because nginx's access log already
has each of them. The package's nginx snippet leaves out one request even there: an open
**Diag** tab asks for `/api/v1/diag/http` once a second.

To troubleshoot, turn on `debug` and restart:

```bash
sudo texas2step config set debug=true --write
sudo systemctl restart texas2step
journalctl -u texas2step -f
```

With `debug = true`, the journal also gets:

* a line for every request and WebSocket, successful ones included;
* texas2step's own detail, such as addresses the UDP probe couldn't bind and idle probe
  sessions it closed;
* the UDP probe's connection steps: ICE candidate checks and the DTLS, SCTP and data
  channel state changes. Look here when the packet-loss tile ends in **TCP** rather than
  **UDP**. The libraries' per-packet STUN and SCTP messages stay out even now.

That is several lines a second while a **Diag** tab is open, so set `debug=false` and
restart again when you're done. `TEXAS2STEP_DEBUG=1` in `/etc/default/texas2step` does the
same without touching the settings file.
