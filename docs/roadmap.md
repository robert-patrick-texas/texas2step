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

### Security

- [ ] **DTLS 1.3 for the UDP probe.** The probe's data channel uses DTLS 1.2
  ([On the wire](security.md#on-the-wire)): the server offers nothing newer, though Chrome
  and Firefox now prefer DTLS 1.3. OpenSSL added DTLS 1.3 in 4.1.0 (October 2026). The
  server's WebRTC library, aiortc, uses the OpenSSL bundled in the `cryptography`
  package, 4.0 for now. Once `cryptography` ships OpenSSL 4.1, a release's `make upgrade`
  brings it in. Then check what a browser negotiates (`tlsVersion` `FEFC` in the
  connection's statistics, not `FEFD`). aiortc may need its DTLS context set to allow
  1.3, and its 1.3 cipher suites set apart from its 1.2 cipher list. DTLS 1.3 would hide
  the throwaway certificates, drop the CBC cipher suites and save a round trip, and later
  allow post-quantum key exchange. The probe carries nothing secret, so this can wait for
  the libraries.
- [ ] **Diag's UDP rows and the access code.** Each UDP row sets up its session through
  the page's own name, then keeps to one address family. A visitor inside
  `internal_networks` over IPv6 but outside over IPv4 sees the IPv4 UDP row answering
  while that family's HTTP and WebSocket rows say Restricted: the setup passed over IPv6,
  and UDP packets have no access check of their own. No one else gains by it, since only
  a browser the server already accepts can set up a session. Setting up each row through
  its family's own name, where the server has one, would check each family on its own, as
  the Test page and Monitor do. The cost: a family whose TCP path is broken couldn't set
  up its UDP row either.
- [ ] **The desk's tools by key, not only by address.** History, saved results, the
  current code, blocks, `/api/v1/measure/*`, `/api/v1/clients` and the API explorer go to
  any address in `internal_networks`. Behind a NAT address shared with people who aren't
  trusted, that is everyone behind it, and a desk member behind such an address gets none
  of them. A key would prove who is asking, from wherever. (Servers measuring each other
  need none: each issues itself a pass, see
  [Measuring between servers](access.md#measuring-between-servers).)
  - **Keys the desk issues:** `texas2step access apikey issue --name Wallboard --scope desk`
    prints one once, such as `t2s_<id>.<scope>.<issued>.<signature>`: when it was issued,
    not when it runs out, signed with a key derived from the shared access key, as requests
    between servers are, so every server with the access key takes it without a database.
    As with passes, how long a key lasts is decided when it is checked, by a setting such
    as `apikey_days`, so shortening it shortens the keys already given. Scopes: `test`
    (what a pass allows), `desk` (reading History, results, the code, the clients),
    `admin` (blocks, ending passes, measurements). The server that issues a key records its
    id and name, for `apikey list`; checking one needs no record.
  - **Ending them:** `texas2step access apikey end` refuses every key issued before now, on
    every server, as `access passes --end` does for passes: one no-earlier-than time, sent
    to the other servers, in place of a list of keys still within their lifetime.
    `apikey revoke <id>` for one that leaked, where the others should keep working. The
    journal names the key at each use of the desk's tools.
  - **Who carries one:** scripts, dashboards and the command line in an
    `Authorization: Bearer` header (`TEXAS2STEP_KEY`); a browser has it entered once, as
    the access code is, and sends it to its own server only, which needs no CORS preflight.
  - **What the desk's tools ask, a setting:** `desk_access = "network"` (as now), `"key"`,
    or `"both"`, where the address narrows who may try and the key proves who it is: the
    fit for mixed networks.
  - **To decide:** keys for the whole cluster (signed, their end time shared) or each
    server's own (stored, hashed); the lifetime's default; whether `test` keys replace
    standing phrases for automation; what the page shows a desk member without a key, and
    entering one on a phone; whether `"both"` becomes the default.

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

[t2s](t2s.md), the Go client for Linux and Windows, could also:

- [ ] **Come with the server's package**, once it has a stable baseline: the package
  would carry every platform's t2s, so each server can offer it for download from its own
  address, and `t2s update` could update from the server it tests, for networks without
  GitHub. Until then the releases list it beside the package.
- [ ] **Be built by GitHub Actions**, with signed attestations of where each file was
  built, as the PyPI release is, so `gh attestation verify` can check a download.
- [ ] **Run on macOS.** It builds for macOS as it is; an unsigned download there needs
  Gatekeeper's quarantine taken off by hand, and Apple's signing and notarization to
  avoid that.

- [ ] **MAC address on macOS and Windows for `texas2step run`.** The Python command line
  reports the MAC address of the NIC its connection leaves by on Linux, from iproute2's
  `ip` (see [This computer's address](methodology.md#this-computers-address)); t2s reports
  it on Linux and Windows, from the interface that holds the local address. The `psutil`
  library could do the same for the Python one: `psutil.net_if_addrs()` lists each
  interface's link-layer address.

### Documentation

- [ ] **A sturdier download command.** The install guide's
  [one-line download](install.md#1-install-the-package) finds the package with
  `grep -o 'https://[^"]*-amd64\.deb'` in GitHub's description of the latest release. A
  URL in the release notes ahead of the package's name would run into it, so release
  notes keep their links after the Files table. Reading the description as JSON, with the
  `python3` the package already needs, wouldn't depend on that.

## Done

Newest first. Each release's notes have the details and its upgrade steps.

### [2.6.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.6.0) (2026-10-27)

- [x] **[t2s](t2s.md), a command-line client in one file** for Linux and Windows, amd64
  and arm64, with nothing to install: the tests of `texas2step run`, with its report and
  results; Monitor as a live panel and Diag as a live table, which tries each address
  family and HTTP version on the server's own name; and `t2s update`, which installs the
  newest from the release, checked against its SHA-256, only when asked. The release
  lists it beside the package. Its methodology is checked against the Python engine's,
  case by case.
- [x] A ping the server refuses, such as for want of the access code, ends a latency
  measurement with the server's answer instead of counting as lost.

### [2.5.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.5.0) (2026-10-27)

- [x] **Active clients**: `texas2step clients` lists each page and command-line run
  using the server, with its page, what it is doing and the bytes it moved, then those
  that ended in the last hour; `/api/v1/clients` for the desk, and a journal line for
  each start and end. Pages are told apart by a random id each page load keeps in
  memory, so two browsers behind one address are two sessions, without a cookie.
  `clients_max` caps the list, and the journal warns when it is full.
- [x] **Founding documents in downloads**: each download starts with the Declaration of
  Independence, the U.S. Constitution, the Texas Declaration of Independence, the
  Constitution of the Republic of Texas and the Texas Constitution, as text, then random
  bytes; `download_documents` turns them off. UDP probes carry random padding.
- [x] **Servers measure each other with a pass** issued with the access key they share,
  so a measuring server needn't be in the other's `internal_networks`.
- [x] Monitor and Diag keep the strictest time limit of their own site and each server
  they test, with its Runtime box.
- [x] History's line fits on one row; a weekly or monthly code's change shows its date.

### [2.4.5](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.4.5) (2026-10-26)

- [x] **A ? tab**: what the tool does, in a one-minute summary for anyone, then more on
  Test, Monitor and Diag. It never names the tool, so it reads right on a renamed site;
  `page_help` hides it.
- [x] **Time limits** for pages left open: `page_monitor_max_runtime` and
  `page_diag_max_runtime` stop Monitor and Diag a set time after each start, and
  `session_max_runtime` has each server end any UDP or WebSocket session that old. A
  stopped page keeps what it shows and starts again once reloaded; a small Runtime box
  counts the time against the page's limit.
- [x] Monitor gives a call's rate as the network carries it, with headers, encryption and
  a VPN's wrapping: about 160 kb/s for voice and 1.5 Mb/s for video, each way per server.
- [x] A finished test says **Saved as Result ID**, with the ID in a box that a click
  copies; History's access code too.
- [x] Durations in seconds read "10s", so the unit can't wrap onto the next line.

### [2.4.4](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.4.4) (2026-10-26)

- [x] **Download JSON** saves a result as `results-<time>.json`, on renamed sites too.

### [2.4.3](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.4.3) (2026-10-26)

- [x] With `brand_logo` set, the logo is the browser tab's icon, in the API explorer too.
- [x] The API explorer takes `brand_name`, in its title and heading.
- [x] The API tab's command-line examples keep the `texas2step` command, which they need
  to work, and install it from PyPI.

### [2.4.2](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.4.2) (2026-10-26)

- [x] The journal files each line at its level, so `journalctl -u texas2step -p warning`
  shows warnings, errors and failed requests.
- [x] History asks for the access code only where the server gives it, which logged a
  warning on servers with codes off.

### [2.4.1](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.4.1) (2026-10-26)

- [x] A UDP session closed while it connects stops its connection checks, which logged
  errors when the service stopped.

### [2.4.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.4.0) (2026-10-26)

- [x] **HTTP/3 (QUIC)** and the other HTTP versions in Diag: with a name for each, rows
  for HTTP/1.1, HTTP/2 and HTTP/3 in each address family, so a network that blocks QUIC
  on UDP 443 shows. Diag lists only what the server offers, and shows the UDP probe's
  DTLS version and cipher.
- [x] `texas2step update --proxy`, and `update_proxy` for the update timer;
  `-k/--insecure` for a proxy that inspects TLS.
- [x] The setup tool names the site's main name where several server blocks publish
  texas2step.

### [2.3.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.3.0) (2026-10-25)

- [x] Passes carry when they were issued: `access_pass_hours` applies to passes already
  given, and `texas2step access passes --end` ends every pass, on every server with
  lockouts shared.
- [x] `texas2step access shuffle` changes the codes at once, without a new key.
- [x] A visitor inside on one address family and outside on the other is asked for the
  code in a box over the page; Diag says which address is restricted.
- [x] The code screen's words are the organization's, with `access_note` under the box.
- [x] `texas2step-setup --port` for servers where another program needs 8080.
- [x] `monitor_voice` withholds Monitor's Voice call profile, as `monitor_video` does
  the Video call one.

### [2.2.4](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.2.4) (2026-10-25)

- [x] The desk's functions refuse requests made for pages on other sites; with
  `internal_networks` set, only those networks see the version.
- [x] API answers are kept in no browser cache.
- [x] A block set by hand ends the passes used from its network, sessions already open
  included.
- [x] No cookie: the pass stays in local storage and goes to the page's own server in a
  header; a Content-Security-Policy on the page and the API explorer.

### [2.2.3](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.2.3) (2026-10-24)

- [x] Spaces around any setting or typed value are ignored; a stray one in
  `internal_networks` no longer breaks every request.

### [2.2.2](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.2.2) (2026-10-24)

- [x] UDP works in Firefox: Monitor, Diag and the Test page's packet loss no longer fall
  back to TCP there.

### [2.2.1](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.2.1) (2026-10-24)

- [x] Monitor gets a server back after an outage: HTTPS checks, then a new session, or
  TCP until UDP returns; the outage counts as loss and is noted beside the chart.
- [x] UDP sessions open about a second sooner: browsers' `.local` candidates are left out
  of the offer, so no multicast DNS lookup holds up the answer.
- [x] [Security](security.md#on-the-wire): what travels encrypted, and what doesn't.

### [2.2.0](https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.2.0) (2026-10-24)

- [x] Each page keeps what it shows across tabs; only the page on screen measures.
- [x] The screen stays on while Monitor or Diag is open and while a test runs, with a
  Keep screen on switch.
- [x] An organization's own footer: `brand_footer` and `[[brand_footer_links]]`.
- [x] Monitor's call tiles put their rating and "ago" under the score on narrow screens.
- [x] Releases publish to PyPI from GitHub, by trusted publishing.

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
