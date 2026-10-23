# Security

The security controls texas2step has, what each guards against, and what is left to the
operator. The other guides have the details and settings; this page links to them.

texas2step is built to sit on the Internet, so employees can test the path to your network
from home, while keeping the service desk's tools and callers' details on your own
networks. A server for internal networks only needs few of these controls; most are off
until set, and none gets in the way of a test from inside.

## At a glance

| Area | Control |
|---|---|
| Who may test | [Access codes](#who-may-test) that change daily, weekly or monthly; passes that expire; `internal_networks` need none |
| Guessing | [Lockouts](#guessing) of a /24 or /48 after three wrong codes in a row, shared between servers if wanted |
| The desk's tools | History, saved results, the current code, measurements and blocks [only for `internal_networks`](#the-desks-tools) |
| Abuse of the service | [Size and time caps](#abuse-of-the-service) on every request, a daily and monthly traffic limit, nginx connection limits |
| Data | [Saved results](#data-and-privacy) readable only inside, deleted securely; no passes or phrases in any log |
| Cryptography | [HMAC-SHA-256 with 256-bit keys](#cryptography); TLS 1.3 with post-quantum key exchange recommended |
| The host | A [sandboxed service](#the-host) as its own unprivileged user, behind nginx, with only the needed ports open |
| Updates | [Releases checked](#updates-and-supply-chain) against their SHA-256 before anything is installed; no unattended change to nginx or the firewall |

## Who may test

With [access codes](access.md) on, a visitor from outside `internal_networks` gets only
the page that asks for the service desk's code. Every API request needs a pass, refused
with `401` without one, except the health check and the request that trades a code for a
pass.

* **Codes.** Two words and two to four digits (`maple.river.42`), worked out with
  HMAC-SHA-256 from a 256-bit key that every server of the organization shares. Each key
  draws its own words, so no two installs give the same codes. A code changes at midnight
  in `access_timezone`, daily, weekly or monthly; yesterday's works for 24 hours more.
* **The key.** `/etc/texas2step/access.key`, made at install, readable only by root and
  the service (`0640 root:texas2step`). Anyone holding it can work out every code, so it
  is kept out of the world-readable `config.toml`. `texas2step access show` prints a
  fingerprint to compare servers by, which reveals nothing about the key.
* **Passes.** A right code buys a pass for `access_pass_hours` (12 by default): an expiry
  time signed with the key, checked by every server that holds it. The browser sends it
  as `?pass=`; this server's own pages get it as a cookie that is `HttpOnly`,
  `SameSite=Strict`, and `Secure` over HTTPS.
* **Standing phrases.** For the desk away from a PC, in their own `0640` file
  (`access-phrases.toml`), with an optional last day. Each use is a journal warning with
  the phrase's name, never the phrase. The examples published in the docs are refused.
* **Comparisons** of codes, phrases and passes are constant-time.
* **What an outsider sees before the code:** the code page, without the version number;
  Monitor, Diag, History and the API pages send them back to it. The health check gives
  `{"status": "ok"}` and no version.

## Guessing

* **Lockouts.** Wrong codes count against the visitor's IPv4 /24 or IPv6 /48, not its
  address, since one host can take a new IPv6 address for every guess. Three in a row
  (`access_lockout_after`) lock the network out: every code from it is refused unread,
  the right one too, with the same answer as a wrong code, until 30 minutes
  (`access_lockout_minutes`) pass without one. Each try moves the end.
* **No races.** Checking a code and counting it are one transaction, shared by the
  service's workers, so a burst of guesses sent at once gets three read.
* **Blocks by hand,** from the command line or an API for `internal_networks`, at most a
  /16 of IPv4 or a /32 of IPv6, and never over `internal_networks` or loopback.
  See [Lockouts and blocks](access.md#lockouts-and-blocks).
* **Servers that defend together.** With `access_share_lockouts`, a lockout on one server
  is a block on every other at once, renewed while the attacker keeps trying.
  See [below](#between-servers) and
  [Servers that defend together](access.md#servers-that-defend-together).
* **A hook** (`access_lockout_hook`) can tell a SIEM or a firewall of each lockout.
* **How strong a code is.** With lockouts, an attacker with 10,000 networks has about a
  0.1% chance a day against a code with four digits (`access_digits = 4`), which is what
  a server on the Internet should use. See
  [How strong is a code, then?](access.md#lockouts-and-blocks)

### Between servers

Lockouts, blocks and clears sent between servers are taken only when both of these hold:

* the request comes from an address in `access_peer_networks`, a list that grants nothing
  else; and
* it is signed with HMAC-SHA-256, with a key derived (HKDF-SHA-256) from the shared access
  key, over the time, a one-time value and the body, which names the server it is for.

A request more than 5 minutes old, seen before, or meant for another server is refused.
What one server can make another do is bounded, in case one is ever misconfigured or
compromised: a block covers at most a /24 or a /48, never `internal_networks` or loopback,
lasts at most a week, and isn't passed on; a server holds at most 1,000 of them.
`texas2step access peers` checks each server with a signed request that changes nothing.

## The desk's tools

With `internal_networks` set, these answer only clients from those networks (and the
server itself), and `404` to everyone else, as if they weren't there:

* History, saved results and the result pages, which hold callers' addresses;
* the current access code;
* lockouts and blocks (`/api/v1/access/blocks`);
* the API explorer (`/api/docs`, `/api/openapi.json`);
* the server-side measurements (`/api/v1/measure/*`).

Server-side measurements only reach the servers in `[[locations]]`, unless
`allow_any_target` is set, so the server can't be used to make requests to other hosts.
At most two run at once on each worker.

## Abuse of the service

A speed test exists to move a lot of data, so each part has a cap:

* **Request bodies** are capped before they are read: 1 KB for a code, 2 MB for a saved
  result, 64 KB for anything else, refused with `413`. Only the upload test takes any
  size, and with access codes on it needs a pass.
* **Each transfer:** a download of at most `max_download_bytes` or `max_stream_seconds`,
  an upload of at most `max_upload_bytes`.
* **The UDP probe:** at most `max_rtc_sessions` sessions per worker, each closed after
  `rtc_session_timeout` idle. Nothing listens on its UDP ports between tests: a port
  opens when a pass-holding browser asks for a test, takes only packets with that test's
  credentials, inside DTLS, and closes when it ends.
* **Traffic limits.** Where traffic is billed, `traffic_day_gb` and `traffic_month_gb`
  refuse new tests once the day's or month's allowance is used, with the site's message
  and no word of when tests start again. See [Traffic limits](traffic.md).
* **Connections.** nginx can cap the connections each visitor and each server holds:
  [Other limits](access.md#other-limits).
* **Every input is checked:** lengths, ranges and formats, refused with `400` or `422`.

## Data and privacy

* **Saved results** hold the address the caller tested from, and where the client could
  tell, their computer's own address and a MAC address. With `internal_networks` set,
  only the desk reads them. Only the newest `results_max_rows` are kept, and
  `texas2step results clear` deletes all of them: the rows are overwritten, the file
  shrunk and its journal emptied, so no address stays on disk.
* **Logs.** The journal shows `pass=*` in place of a pass, and names standing phrases,
  never their text. nginx logs texas2step's requests without their query strings, so its
  access log never holds a pass. Wrong codes, lockouts, blocks and each use of a phrase
  are warnings in the journal, with the address they came from.
* **Secrets apart from settings.** The access key and standing phrases are in their own
  files, readable by root and the service only; `config.toml` holds no secret.
* **The service's files** under `/var/lib/texas2step` (saved results, lockouts, traffic
  figures) belong to the service user. The traffic figures hold no addresses.
* **No third parties.** The web page loads nothing from other sites: no CDN, fonts,
  analytics or telemetry. No public STUN server is used unless listed in `ice_servers`.

## Cryptography

| Use | How |
|---|---|
| Access codes, passes | HMAC-SHA-256, 256-bit shared key |
| Requests between servers | HMAC-SHA-256 with a key derived by HKDF-SHA-256; time and one-time value against replay |
| Connections to nginx | TLS 1.3 (your site's settings); `X25519MLKEM768`, ML-KEM-768 with X25519, recommended |
| UDP probe | DTLS, as WebRTC requires |
| Releases | SHA-256 of each file, checked against GitHub's record before install |

Quantum computers would break public-key cryptography, such as the elliptic-curve key
exchange of a TLS connection, but not keyed hashes such as HMAC-SHA-256: against them a
256-bit key keeps about 128 bits of strength. So the codes, passes and signed requests
hold as they are. For connections, nginx with OpenSSL 3.5 (Ubuntu 26.04) can agree keys
with ML-KEM-768 and X25519 together, so traffic recorded today can't be decrypted later;
a site that sets `ssl_ecdh_curve` must list it. See the
[install guide's security notes](install.md#security-notes). The certificates that
authenticate a connection are still elliptic-curve or RSA; that risk is to a connection
at the moment it is made, not to recorded traffic.

## The host

* **Behind nginx.** The service listens on `127.0.0.1:8080`; nginx ends TLS in front of
  it. `X-Forwarded-For` and the other forwarded headers are believed only from
  `trusted_proxies`, nginx on the same host by default, and the package's snippet
  replaces any `X-Forwarded-For` a client sends, so a visitor can't claim an address
  inside. A proxy that sends no visitor address makes the request an outsider's.
* **Only the needed ports.** TCP 443 (and 80, to redirect), and the UDP probe's range,
  pinned with `rtc_port_min`/`rtc_port_max` so a firewall can allow just that. The setup
  tool finds the host's firewall and plans the rule.
* **A sandboxed service.** It runs as the unprivileged `texas2step` user with
  `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
  `PrivateDevices`, kernel and control-group protection, and only the address families it
  needs. It can write only to `/var/lib/texas2step`. The lockout hook runs as that user,
  in that sandbox, even when `sudo texas2step access block` sets it off.
* **Changes to nginx are checked.** The setup tool backs up a site before it adds the
  `include` line, and every change is tested with `nginx -t` and put back if it fails.

## Updates and supply chain

* **Releases** are a `.deb` and the setup tool, attached to a GitHub release. GitHub
  records each file's SHA-256; `texas2step update` downloads both, checks them against
  it, and stops on any difference. The setup tool carries its package's SHA-256 and
  refuses any other. On a closed network, `--from` and mirrors are checked the same way.
* **Nothing installs by surprise.** The update timer is off until turned on, and even then
  installs a release by itself only if it changes nothing but the package and the
  settings, never nginx or the firewall. Settings are merged into the new template with
  every value kept, checked before writing, and backed up.
* **Dependencies** are pinned in `uv.lock` and installed exactly into the package's own
  environment. Each release moves every dependency to its newest version first, for its
  security fixes. The web page has no runtime dependencies at all.
* **No code from elsewhere at run time.** The page's scripts are part of the package.

## Left to the operator

texas2step can't do these for you:

* Keep Ubuntu, nginx and OpenSSL patched (`unattended-upgrades` does this by default).
* Floods that fill the link before they reach the server: that is for your provider or a
  scrubbing service.
* Use `access_digits = 4`, standing phrases of several unrelated words, and keep the
  access key as you would a password.
* Set `internal_networks` on every server open to the Internet, the same everywhere.
* Callers who share a /24, such as a mobile carrier's pool, can be locked out together by
  one guesser; `texas2step access lockouts --clear` lets them back in.
* The page's own scripts are public, so a determined attacker can tell the release.
* A visitor with a pass can test as often as they like: use nginx's connection limits,
  and traffic limits where traffic is billed.

## Reporting a vulnerability

Please don't put the details of a security problem in a public issue.
[Open an issue](https://github.com/robert-patrick-texas/texas2step/issues/new) that asks
for a private way to send them, and one will be arranged.
