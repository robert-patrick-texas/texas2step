# Roadmap

What is being considered, and what each release has shipped. The ideas aren't scheduled
or promised: one goes into a release when it's needed, and moves to
[Done](#done), checked, under that release.

## Ideas

### Measurements

Tests that say how a connection will feel to the people using it, not only how fast it
is. Call quality, responsiveness, one stream and Monitor's call profiles came in
[2.1.0](#210-2026-10-24).

- [ ] **Shapers.** A rate that starts high and settles lower after a few seconds is a
  shaper or policer. The 100 ms throughput timeline already shows it, for several streams
  and for one, and the test could say so.
- [ ] **Where is the problem?** A guided comparison of a nearby or internal server with a
  distant one: if both are bad, it's the local network or Wi-Fi; if only the distant one
  is, it's the WAN or the ISP. It would build on the server picker and Monitor's
  two-server mode.
- [ ] **When the device is the limit.** A busy browser or a slow laptop can't keep up with
  a fast link. Watching the page's own timers and long tasks during a transfer could
  tell that apart from a slow network.
- [ ] **A path trace from the server.** For the service desk only: the server traces the
  path back to the caller's address with `tracepath`, which needs no privileges, and shows
  each hop and the largest packet the path carries. Rate-limited, and only toward the
  caller who asks.
- [ ] **The UDP ports call apps use.** A STUN responder on ports the operator chooses,
  such as Teams' 3478 to 3481, would show whether a network lets calls through on UDP or
  makes them fall back to TCP. It needs a listener and firewall rules for each port.
- [ ] **HTTP/3 (QUIC).** Whether the network passes QUIC on UDP 443, which many corporate
  firewalls block, and how HTTP/3 compares with HTTP/2. nginx would need a `quic` listener,
  an `Alt-Svc` header and a firewall rule; the browser decides whether to use it.
- [ ] **DNS lookup time.** Slow or overloaded resolvers make every new site slow. A browser
  only looks up a name it hasn't cached, so each test would need a fresh name under a
  wildcard DNS record.

### Test page

- [ ] **Choosing HTTP/1.1 for a test.** A web page can't choose the HTTP version of its
  requests: the browser and the server settle it during TLS (ALPN), and a server with
  HTTP/2 enabled gets HTTP/2. Over HTTP/2 the Test page's parallel streams share one TCP
  connection; over HTTP/1.1 each is a connection of its own, closer to many applications
  and to some other speed tests. A design that works with every hostname:

  * a second HTTPS port, such as 8443, in an nginx `server` block without `http2 on`,
    which the setup tool could write;
  * a setting such as `http1_port`, and a firewall rule for it like the one for 443;
  * an "HTTP: Auto / HTTP/1.1" choice in the header beside the IP one, which sends the
    test to that port.

  A second hostname on port 443 wouldn't do: browsers reuse an HTTP/2 connection for any
  name that shares its IP address and is covered by the same certificate, and a wildcard
  certificate makes that likely.

### Branding

The [branding settings](configuration.md#branding-and-themes) replace the name, logo,
links and footer credit. Three places still say texas2step:

- [ ] **The browser tab's icon** is the texas2step star, even with `brand_logo` set. It
  could use that logo.
- [ ] **The API explorer** (`/api/docs`) is titled "texas2step - Swagger UI".
- [ ] **The API tab's command-line examples** name the `texas2step` CLI.

### Updates

[`texas2step update`](install.md#with-texas2step-update) could also:

- [ ] **Roll back when its check fails.** The install script checks that the new version
  answers, directly and through nginx, and stops with an error when it doesn't. It could
  then reinstall the previous version, which means keeping that version's package.
- [ ] **Leave new major versions to an operator.** The update timer installs any newer
  release whose plan changes only the package and the settings. A guard could hold back a
  new major version, such as 2.0.0, for a person to install.
- [ ] **Check signatures.** Downloads are checked against the SHA-256 digests GitHub
  publishes with each release, and `--from` against the digest built into the setup
  tool. Signing each release with a key the servers trust would also guard against a
  tampered mirror or release.

### Command line

- [ ] **MAC address on macOS and Windows.** `texas2step run` reports the MAC address of
  the NIC its connection leaves by on Linux, from iproute2's `ip` (see
  [This computer's address](methodology.md#this-computers-address)). On macOS and Windows
  it reports the local address without a MAC. The `psutil` library could add one there:
  `psutil.net_if_addrs()` lists each interface's link-layer address. It has no route
  lookup, so the NIC would be the one that holds the local address. Not needed while
  Linux is the platform texas2step targets.

### Documentation

- [ ] **A sturdier download command.** The install guide's
  [one-line download](install.md#1-install-the-package) finds the package with
  `grep -o 'https://[^"]*-amd64\.deb'` in GitHub's description of the latest release. A
  URL in the release notes ahead of the package's name would run into it, so release
  notes keep their links after the Files table. Reading the description as JSON, with the
  `python3` the package already needs, wouldn't depend on that.

## Done

Newest first. Each release's notes have the details and its upgrade steps.

### [2.1.1](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.1.1) (2026-10-24)

- [x] Monitor holds still at Stop, to show or take a screenshot of.
- [x] A call's scores sit on Monitor's loss-per-second strip, each over its 10 seconds.
- [x] History without the Local column; Diag's NAT and egress box shows this computer's
  address.

### [2.1.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.1.0) (2026-10-24)

- [x] Call quality each way: a MOS from the ITU-T E-model for what you hear and what the
  other end hears, adjusted for late packets, bursts and gaps a caller would hear
  ([Quality and user experience](experience.md)).
- [x] The UDP probe's stamps, for each direction's delay and losses, and 200-byte probes.
- [x] Responsiveness in round trips per minute, by the IETF draft's method: new
  connections and the loaded connection.
- [x] One stream's download beside the several streams' rate, and `default_one_stream`.
- [x] Monitor's Path, Voice call and Video call profiles, each 10 seconds of a call
  scored, counted toward a traffic limit.

### [2.0.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.0.0) (2026-10-23)

- [x] Servers that defend together: a lockout on one server is a block on every server
  in the picker at once, sent signed (HMAC-SHA-256, with a key derived from the shared
  access key) and taken only from `access_peer_networks`
  ([Servers that defend together](access.md#servers-that-defend-together)).
- [x] `texas2step access peers` checks that each server takes them; `access block` and
  `access lockouts --clear` reach every server.
- [x] `access_lockout_hook` runs a program on each lockout, block and clear.
- [x] Blocks by hand are at most a /16 or a /32.
- [x] [Security](security.md): the security controls in one place, and post-quantum key
  exchange (ML-KEM-768 with X25519) for nginx in the install guide.
- [x] On PyPI: `uv tool install texas2step` or `pipx install texas2step`.

### [1.9.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v1.9.0) (2026-10-23)

- [x] Traffic limits for servers whose traffic is billed: a daily and a monthly cap on
  what the tests move, counting what the server sends, receives or both, with
  `internal_networks` counted or not; past it, new tests get the site's message and an
  optional link elsewhere, and running ones finish ([Traffic limits](traffic.md)).
- [x] `texas2step traffic` and `traffic allow`; History's Traffic column.
- [x] Lockouts: three wrong codes in a row from a /24 or /48 lock it out until it stops
  trying for 30 minutes; `texas2step access lockouts` and `block`, and an API for
  `internal_networks` to carry blocks between servers.
- [x] Standing access phrases beside the day's code, for the service desk away from a PC
  (`texas2step access phrase`).
- [x] Hardening for servers on the Internet: a pass cookie for page loads, request
  bodies capped before they are read, the version hidden from outsiders, and no passes
  in nginx's access log.
- [x] `texas2step results clear` starts History over; [Clone a server](install.md#clone-a-server).

### [1.8.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v1.8.0) (2026-10-22)

- [x] Access codes for servers open to the Internet: the service desk reads callers a
  code that changes daily, weekly or monthly, from words every server works out from a
  shared key, or from the site's own list ([Access codes](access.md)).
- [x] `internal_networks`: no code there, and only they get History, the API tab, saved
  results, the API explorer and the server-side measurements.
- [x] Result IDs that read well over the phone, and History opens one as it is read out.
- [x] `texas2step access show`, `words` and `key`; `--code` for the command line's tests.
- [x] The Test page's link card follows the server picker.

### [1.7.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v1.7.0) (2026-10-22)

- [x] `update_url` can name a mirror in a directory on the server, such as
  `file:///opt/texas2step/mirror`, so the update timer updates a closed network
  unattended, with no web server
  ([From a mirror in a directory](install.md#without-access-to-github)).
- [x] `apt purge` also removes the dated backups of the settings, and with them
  `/etc/texas2step`.

### [1.6.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v1.6.0) (2026-10-22)

- [x] `texas2step update --from <directory>` updates a server without internet access
  from a release's `.deb` and setup tool, checked as a pair
  ([Without access to GitHub](install.md#without-access-to-github)).

### [1.5.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v1.5.0) (2026-10-22)

- [x] `texas2step run` reports this computer's own address on its connection and, on
  Linux, the MAC address of the NIC that connection leaves by. `--save` stores them, and
  `--no-local` leaves them out.
- [x] Results say where a MAC came from: `mac_source` is `nic` or `eui64`.
- [x] Measurements a server makes for `/api/v1/measure/full` leave out its own address
  and MAC.

### [1.4.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v1.4.0) (2026-10-22)

- [x] Diag shows this computer's own address and a NAT comparison where the browser
  reveals it, and a MAC address read from an EUI-64 IPv6 address.
- [x] Test runs save the local address and MAC, shown in History's Local column.
- [x] The Test page groups its boxes into Phases and Results, and a finished phase shows
  a check mark.
- [x] The America theme frames the window in navy and red, under a blue gradient header.
- [x] Phones in portrait: the All measurements table, the header, the packet-loss grid
  and History fit the screen.
- [x] The throughput chart's Upload label no longer overlaps Download's.
- [x] Diag's UDP sessions keep to their address family when a browser reveals its
  addresses.

### [1.3.1](https://github.com/robert-patrick-texas/texas2step/releases/tag/v1.3.1) (2026-10-21)

- [x] On a return visit, the header's server picker shows the remembered server, the one
  the tests use.

### [1.3.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v1.3.0) (2026-10-19)

- [x] `texas2step update` installs a newer release, each file checked against the SHA-256
  GitHub publishes with it.
- [x] Unattended updates with `texas2step-update.timer`, off until turned on.
- [x] `update_url`, for GitHub Enterprise or a mirror on a network without GitHub.
- [x] The install script reloads nginx when an upgrade changes the package's nginx files.
- [x] The setup tool's `--update` option.

### [1.2.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v1.2.0) (2026-10-19)

- [x] A quieter journal: start and stop, warnings, errors and failed requests.
- [x] `debug`, for every request and WebSocket and the UDP probe's WebRTC detail.
- [x] nginx's access log leaves out Diag's once-a-second request.

### [1.1.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v1.1.0) (2026-10-19)

- [x] `brand_credit`: the footer's project credit as a link, as text, or not at all.

### [1.0.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v1.0.0) (2026-10-19)

- [x] **Test**: download and upload over parallel streams, latency and jitter idle and
  under load with a bufferbloat grade, and UDP packet loss split into upstream and
  downstream.
- [x] **Monitor**: continuous UDP probing, to one server or to two at once.
- [x] **Diag**: HTTPS, WebSocket and UDP over IPv4 and IPv6, each tried once a second.
- [x] **History** of saved results, a REST **API** with an OpenAPI explorer, and a
  command-line client.
- [x] **IPv4 and IPv6**: with an IPv4-only and an IPv6-only hostname, tests held to one
  family.
- [x] **Themes and branding**: eight themes, your name, logo, organization, links and
  notice, and tabs that can be hidden.
- [x] **Setup tool and package**: `texas2step-setup` checks a server and writes an install
  script that installs, upgrades, merges settings, publishes through nginx and opens the
  UDP range.
