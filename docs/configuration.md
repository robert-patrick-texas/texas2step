# Configuration

Settings come from, in increasing priority:

1. built-in defaults,
2. a TOML file named by `TEXAS2STEP_CONFIG` (or `texas2step serve --config FILE`),
3. environment variables `TEXAS2STEP_<SETTING>` (upper case).

Lists in environment variables are comma separated. Booleans accept `1/0`,
`true/false`, `yes/no` and `on/off`. Spaces around any value, or around each item of a
list, are ignored, wherever the setting comes from. Unknown keys in the TOML file are an
error, so typos don't fail silently. The package installs a fully commented file listing every
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
| `max_rtc_sessions` | 32 | Concurrent UDP probe sessions per worker. Each costs the server some CPU: about 1 % of a core for Monitor's Path profile at 10 probes a second, 3 % for its Voice call and 8 % for its Video call, measured on a 2019 server CPU. |
| `rtc_session_timeout` | 120 | Seconds of inactivity before a session is closed |
| `rtc_port_min` / `rtc_port_max` | 0 / 0 | Inclusive UDP port range for the probe's server sockets, so a firewall can open just that range. Both 0 lets the OS pick ports. See [Pinning the UDP ports](install.md#pinning-the-udp-ports). |
| `results_enabled` | `true` | Store results and serve History. `sudo texas2step results clear` deletes those saved; see [Clear saved results](install.md#clear-saved-results). |
| `results_path` | `texas2step.db` | SQLite file (relative to the working directory) |
| `results_max_rows` | 10000 | Oldest results are pruned beyond this |
| `measure_enabled` | `true` | Enable `/api/v1/measure/*` |
| `allow_any_target` | `false` | Let `/measure/*` target any URL, not just configured locations. Leave off on shared networks: it turns the server into a request relay. |
| `page_test` / `page_monitor` / `page_diag` / `page_history` / `page_api` | `true` | Show that tab of the web UI. Any can be hidden, but not all. A hidden page's address opens the first page shown. The API stays on: `results_enabled` and `measure_enabled` turn parts of it off, and `/api/docs` stays reachable without the API tab. |
| `default_streams` / `default_seconds` / `default_packets` | 4 / 10 / 500 | Defaults advertised by `/api/v1/info` |
| `monitor_profile` | `path` | Monitor's profile until a visitor picks one: `path` (small probes, to follow the path), `voice` or `video` (a call's packets, scored every 10 seconds); see [Live monitor](methodology.md#live-monitor) |
| `monitor_voice` | `true` | Offer Monitor's Voice call profile, about 150 kb/s each way while a tab runs it. With `false`, the page shows no Voice call button and the server refuses the profile. |
| `monitor_video` | `true` | Offer Monitor's Video call profile, about 1.4 Mb/s each way while a tab runs it. With `false`, the page shows no Video call button and the server refuses the profile. With both `false`, Monitor runs Path only, and shows no profile switch. Voice and video count toward a [traffic limit](traffic.md#monitors-call-profiles). |
| `default_one_stream` | `true` | Whether a test also downloads over [one stream](methodology.md#one-stream), unless the visitor chooses in Test options or the command line with `--one-stream` or `--no-one-stream`. It can add up to half again to a test's traffic, so a server whose traffic is billed may want `false`; see [Traffic limits](traffic.md#what-a-test-moves). |
| `cors_origins` | `["*"]` | Origins allowed to call the API from a browser |
| `trusted_proxies` | `["127.0.0.1", "::1"]` | Peers whose `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Prefix` and `X-Client-Protocol` are believed |
| `debug` | `false` | Also log every request and the UDP probe's WebRTC detail; see [Logging](#logging) |
| `update_url` | GitHub's API for the project | Where `texas2step update` looks for releases: GitHub, GitHub Enterprise, or a mirror on a web server or in a directory (`file:///opt/texas2step/mirror`); see [Without access to GitHub](install.md#without-access-to-github) |
| `update_proxy` | `""` | A proxy for `texas2step update`'s downloads and the update timer's, such as `http://proxy.example.net:3128`; empty uses `https_proxy` from the environment, if set. `--proxy` overrides it for one run. |
| `access_code` | `off` | Ask visitors from outside `internal_networks` for the service desk's code before they test: how often it changes, `daily`, `weekly`, `monthly` or `fixed`; see [Access codes](access.md) |
| `access_timezone` | `America/New_York` | The time zone whose midnight starts a new code |
| `access_words` | `""` | A file of your own words for codes, one entry per line; empty uses the built-in words |
| `access_digits` | 2 | The digits that end a code: 2, 3 or 4 |
| `access_pass_hours` | 12 | How long a pass lasts from when it was issued; applied as passes are used, so a change applies to passes already given. See [Ending every pass](access.md#ending-every-pass). |
| `access_note` | `Contact the service desk for assistance.` | The line under the code box, such as how to reach the desk; `""` for none |
| `access_key_file` | `/etc/texas2step/access.key` | The key codes come from; the same on every server of an organization |
| `access_phrases_file` | `/etc/texas2step/access-phrases.toml` | Standing phrases that work as codes, if the file exists; see [Standing phrases](access.md#standing-phrases) |
| `access_lockout_after` | 3 | Wrong codes in a row from one /24 or /48 that lock it out of entering codes; see [Lockouts and blocks](access.md#lockouts-and-blocks) |
| `access_lockout_minutes` | 30 | How long a lockout lasts after the last code from the network |
| `access_lockouts_file` | `/var/lib/texas2step/access.db` | The counts, lockouts and blocks, shared by the workers and `texas2step access lockouts` |
| `access_share_lockouts` | `false` | `true`: a lockout here, `texas2step access block` and `access lockouts --clear` go to every server in `[[locations]]`; see [Servers that defend together](access.md#servers-that-defend-together) |
| `access_peer_networks` | `[]` | The addresses the other servers' requests come from: their lockouts are taken only from there, and only signed with the shared key |
| `access_lockout_hook` | `""` | The full path of a program run on each lockout, block and clear here, as the service's user; see [A hook for anything else](access.md#a-hook-for-anything-else) |
| `internal_networks` | `[]` | Your own networks, as addresses or CIDR blocks: no code is asked there, and only they get History, the API tab, saved results, the API explorer and `/api/v1/measure/*` |
| `traffic_day_gb` | 0 | Gigabytes (10^9 bytes) the tests may move in a day before new tests are refused until midnight in `access_timezone`; 0 for no limit. See [Traffic limits](traffic.md). |
| `traffic_month_gb` | 0 | The same for a calendar month |
| `traffic_counts` | `both` | What counts: `sent` (the visitors' downloads, which clouds such as AWS, Azure and Google Cloud bill), `received` (their uploads) or `both` |
| `traffic_count_internal` | `true` | `false`: tests from `internal_networks` neither count nor are refused |
| `traffic_file` | `/var/lib/texas2step/traffic.db` | The figures, shared by the workers and `texas2step traffic`; used only while a limit is set |
| `traffic_message` | `Testing temporarily disabled.` | What a visitor refused by a traffic limit reads |
| `traffic_url` | `""` | If set, the message is followed by "Click here to continue.", linking here: another texas2step server, another provider's test, the service desk's page |
| `brand_name` | `texas2step` | Name in the header and the browser tab |
| `brand_organization` | `""` | Organization shown beside the name and in the footer |
| `brand_url` | `""` | The organization's website; the organization name links to it |
| `brand_logo` | `""` | Path to an SVG, PNG, JPEG or WebP file shown in place of the star |
| `brand_notice` | `""` | A line of text across the top of every page |
| `brand_links` | `[]` | Links in the header and footer, as `[[brand_links]]` tables (TOML only) |
| `brand_footer` | `""` | Your own footer's text, near the bottom of every page: each line a line, a blank line a new paragraph, and `[label](url)` a link. See [Branding and themes](#branding-and-themes). |
| `brand_footer_links` | `[]` | Your own footer's links, beside its text, as `[[brand_footer_links]]` tables (TOML only) |
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
  would refuse, are rejected before anything is written. Edit `[[locations]]`,
  `[[brand_links]]` and `[[brand_footer_links]]` in the file, or use `add-location`.
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
# Your own footer, near the bottom of every page (before any [[table]], as TOML needs).
brand_footer = """
© 2026 Acme IT. For official use only.
Questions? Ask the [service desk](https://help.acme.example) or call [555-0100](tel:5550100)."""

[[brand_links]]
label = "Service desk"
url = "https://help.acme.example"

[[brand_links]]
label = "Email the NOC"
url = "mailto:noc@acme.example"

[[brand_footer_links]]
label = "Privacy"
url = "https://www.acme.example/privacy"

[[brand_footer_links]]
label = "Accessibility"
url = "https://www.acme.example/accessibility"
```

* **Where it shows.** The name and logo lead the header. The organization sits beside them
  and opens its website in a new tab. `brand_links` appear in the header on wide screens
  and in the footer on every screen. The notice runs across the top of every page.
  Once the site is renamed, the footer reads "Powered by texas2step". The footer's
  "texas2step &lt;version&gt;" links to the project's site; `brand_credit = "text"` keeps the
  words without the link, and `brand_credit = "none"` leaves the project's name, version
  and "Powered by" out of the footer, for sites that carry only their own branding. The
  version is still at `/api/v1/health`.
* **Your own footer.** `brand_footer` and `[[brand_footer_links]]` add a footer of your
  own near the bottom of every page, the access-code screen included, above the line with
  the organization and the credit. texas2step lays it out the same way on every site and
  in every theme. On wide screens your text is on the left and your links are stacked
  on the right. On phones the links follow the text, and with only one of the two it is
  centered. Each line of `brand_footer` starts a new line, and a blank line starts a
  paragraph. The text is plain, but `[label](url)` makes a link of the label, by the same
  rules as the other links below. A URL ends at the first `)`. HTML isn't allowed, so it
  shows as typed. In TOML, `"""` lets the text run over several lines. The API explorer
  (`/api/docs`) is a separate page and has no footer of yours.
* **Logo.** SVG, PNG, JPEG, WebP or GIF, about 30 px tall on screen; wide logos keep their
  shape, up to 180 px wide. The `texas2step` service user must be able to read the file
  (`/etc/texas2step/` is a good place). If the file is missing or unreadable, the server
  logs a warning and keeps the star.
* **Links** must start with `https://`, `http://`, `mailto:` or `tel:`, in
  `[[brand_links]]`, `[[brand_footer_links]]` and `brand_footer` alike. Anything else
  stops the server at startup with an error naming the setting. `[[brand_links]]` and
  `[[brand_footer_links]]` can only be set in the TOML file, not through environment
  variables.
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
