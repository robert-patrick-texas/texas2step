# t2s, the command-line client

**t2s** tests the network between the computer it runs on and a texas2step server, as the
server's pages do: the Test page's full test or any part of it, Monitor's continuous
probing, and Diag's every way of reaching the server. It is one file, with nothing to
install: copy it onto a workstation or a server, run it, and delete it when you're done.
It runs on Linux and Windows, on x86-64 and on ARM.

Its results are the ones `texas2step run` gives (the Python command line in the server's
package): the same methodology, the same report and the same JSON, which History and the
API keep. t2s measures with less of its own overhead, so its latency figures can be a
little lower.

## Download

Each [release](https://github.com/robert-patrick-texas/texas2step/releases) lists t2s's
files beside the server's package:

| File | For |
|---|---|
| `t2s-linux-amd64` | Linux on x86-64 |
| `t2s-linux-arm64` | Linux on ARM, such as a Raspberry Pi 4 or 5 with a 64-bit system |
| `t2s-windows-amd64.exe` | Windows 10 and 11 on x86-64 |
| `t2s-windows-arm64.exe` | Windows 11 on ARM |

`https://github.com/robert-patrick-texas/texas2step/releases/latest/download/<file>`
always gives the newest. On Linux:

```bash
sudo curl -fLo /usr/local/bin/t2s \
    https://github.com/robert-patrick-texas/texas2step/releases/latest/download/t2s-linux-amd64
sudo chmod 755 /usr/local/bin/t2s
t2s version
```

Without `sudo`, put it in `~/.local/bin` instead. On Windows, in PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\t2s" | Out-Null
Invoke-WebRequest -OutFile "$env:LOCALAPPDATA\t2s\t2s.exe" `
    https://github.com/robert-patrick-texas/texas2step/releases/latest/download/t2s-windows-amd64.exe
& "$env:LOCALAPPDATA\t2s\t2s.exe" version
```

To run it as `t2s` from any folder, add that folder to your `Path`:

```powershell
[Environment]::SetEnvironmentVariable("Path",
    [Environment]::GetEnvironmentVariable("Path", "User") + ";$env:LOCALAPPDATA\t2s", "User")
```

### Checking a download

The release notes list each file's SHA-256, and GitHub shows it beside each file on the
release page. Compare it with the file you have:

```bash
sha256sum /usr/local/bin/t2s
```

```powershell
Get-FileHash "$env:LOCALAPPDATA\t2s\t2s.exe"
```

The files aren't code-signed, so Windows may warn about one downloaded with a browser.

## A test

```bash
t2s run https://dallas.example.net/texas2step
```

```
t2s → https://dallas.example.net/texas2step  (Dallas)
client 203.0.113.9 (IPv4, HTTP/1.1)
this computer 192.168.1.20 · MAC 3c:22:fb:12:34:56

  Latency              18.42 ms   jitter 0.61 ms   (min 17.80 · p90 19.35 · 20 samples)
  Download           912.4 Mbps   peak 941.0   (4 streams · loaded +7.9 ms)
  One stream         604.8 Mbps   peak 652.3   (a single file copy · 66 % of 4 streams)
  Upload             874.1 Mbps   peak 902.7   (4 streams · loaded +12.4 ms)
  Loss                   0.00 %   (0/500 lost · ↑ 0.00 % ↓ 0.00 % · UDP RTT 18.97 ms · jitter 0.44 ms)
  Call quality          4.4 MOS   excellent   (you hear 4.4 · they hear 4.4 · longest gap ↓ 0 ↑ 0 ms · late ↓ 0.00 % ↑ 0.00 %)
  Responsiveness      1,720 RPM   (↓ 1,851 · ↑ 1,720 · a new connection 55.1 ms idle, 68.0 ms under load)
  Bufferbloat                 A
```

`client` is the address the server sees; `this computer` is t2s's own on that
connection, before any NAT, with the MAC address of the network adapter it leaves by,
for finding the computer on a switch port or wireless access point. The other lines are
explained in [Methodology](methodology.md) and
[Quality and user experience](experience.md).

Each part of the test runs on its own too:

```bash
t2s latency  https://dallas.example.net/texas2step --count 50
t2s download https://dallas.example.net/texas2step --streams 8 --seconds 15
t2s upload   https://dallas.example.net/texas2step
t2s loss     https://dallas.example.net/texas2step --packets 1000 --interval 5
```

| Option | Commands | What it does |
|---|---|---|
| `--json` | all | print the result as JSON, as History and the API keep it |
| `--code CODE` | all | the access code, for a server that asks for one (default: `$TEXAS2STEP_CODE`) |
| `--seconds N` | run, download, upload | how long each transfer runs (default 10) |
| `--streams N` | run, download, upload | parallel streams (default 4) |
| `--http2` | run, latency, download, upload | run the streams inside one HTTP/2 connection, as a browser does |
| `--packets N` | run, loss | UDP probes in the packet-loss test (default 500) |
| `--interval MS` | run, loss | ms between probes (default 10) |
| `--size BYTES` | loss | probe size (default 200) |
| `--count N` | latency | pings (default 20) |
| `--no-loss` | run | skip the packet-loss test |
| `--one-stream`, `--no-one-stream` | run | download over one stream after the download, or not (default: as the server says) |
| `--save` | run | store the result on the server, for History; t2s prints its address |
| `--no-local` | run | leave this computer's own address and MAC out of the result |

By default each stream is a TCP connection of its own, over HTTP/1.1, as `texas2step run`
does. With `--http2` the streams share one HTTP/2 connection, as the Test page's do through
nginx, and responsiveness is also measured on that connection, as the page measures it.

## Monitor

```bash
t2s monitor https://dallas.example.net/texas2step
t2s monitor https://dallas.example.net/texas2step https://houston.example.net/texas2step
t2s monitor https://dallas.example.net/texas2step --profile voice
```

Monitor sends a steady stream of small UDP probes, ten a second, and the server echoes
them. With two servers it probes both at the same moments and compares them. On a
terminal it shows a panel that redraws in place: the Monitor page's tiles, the last
window as a strip of round trips (the slowest in each column, from zero up to the `max`
shown), a mark for each column with a lost probe, and the latest events:

```
t2s monitor → A Dallas · UDP · WebRTC · IPv4  ·  B Houston · UDP · WebRTC · IPv4   ran 2 min · Ctrl-C stops
Path: 10 probes/s, 64 B, to two servers at the same moments · window 1 min

              Latest     Median     95th pct   Jitter     Loss (1 min)   Probes
  A Dallas    18.6 ms    18.4 ms    19.9 ms    0.52 ms    0 % (0)        1,300
  B Houston   24.9 ms    24.7 ms    26.3 ms    0.61 ms    1.7 % (10)     1,300
  B − A       +6.3 ms    +6.3 ms    +6.4 ms    +0.09 ms   +1.7 pts

  A RTT  ▂▂▂▃▂▂▂▂▃▂▂▂▂▂▂▂▂▂▂▂▂▂▃▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂ max 22.4 ms
    Loss ····························································
  B RTT  ▅▅▅▅▅▆▅▅▅▅▅▅▅▅▅▅▅▅▅▅█▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅ max 31.0 ms
    Loss ····················x·······································

  14:02:51  B (Houston) didn't answer from 14:02:44 to 14:02:51, for 7s.
```

Piped or redirected to a file, it writes a line a second for each server instead, and
the events between them. `--json` writes JSON lines: `figures` each second, `slice` for
each 10 seconds scored, `event`, and `summary` at the end. Ctrl-C stops it; the last
panel stays on the terminal, with the totals since it started.

| Option | What it does |
|---|---|
| `--profile NAME` | `path` (small probes), `voice` (a voice call's packets) or `video` (a video call's, with its voice); the calls are scored every 10 seconds (default: as the server says) |
| `--rate N` | path probes a second (default 10) |
| `--size BYTES` | bytes in a path probe (default 64) |
| `--voice-size BYTES`, `--every MS` | a voice packet's size and spacing (default 200 bytes every 20 ms) |
| `--video KBPS` | the video call's rate (default 1000) |
| `--window SECONDS` | what the figures cover, up to 900 (default 60) |
| `--seconds N` | stop after this long, from the first probe: the last probes get 1.5 seconds to come back, so a call's last 10 seconds are scored too (default: until Ctrl-C) |
| `--code`, `--json` | as for a test |

A server that stops answering is asked over HTTPS every 2 seconds until it answers, then
given a new session, as the [Monitor page](methodology.md#live-monitor) does; the time
it was gone counts as loss, and the panel says for how long it was gone. Where UDP doesn't
get through, the probes go over a WebSocket, and UDP is tried again every 30 seconds. A
server's own time limits stop Monitor as they stop its page: its Monitor limit, the
strictest of the two servers', and its limit on one session.

## Diag

```bash
t2s diag https://dallas.example.net/texas2step
t2s diag https://dallas.example.net/texas2step -6
```

Diag tries every way of reaching the server about once a second: HTTP/1.1, HTTP/2,
HTTP/3, WebSocket and UDP, over IPv4 and over IPv6. A browser can't choose the address
family or the HTTP version, which is why the Diag page needs the server's single-family
and single-version names; t2s chooses them itself, on the server's own name, and uses
those names where the server lists them (`server_url_v4`, `server_url_v6_http3` and the
like, in [Configuration](configuration.md)). HTTP/3 is tried where the server names a
name for it, or its answers over TCP carry `Alt-Svc: h3`.

```
t2s diag → Dallas  https://dallas.example.net/texas2step                          ran 35s · Ctrl-C stops
Your IPv4 203.0.113.9 · Your IPv6 2001:db8:5::9
Clocks: this computer is 3.2 ms behind the server (± 9.2 ms)

IPv4 · server 192.0.2.10
  HTTP/1.1    TCP 443     ····················   Answering   18.6 ms · median 18.4 ms · 0 of 35 lost in 1 min   TLSv1.3 TLS_AES_256_GCM_SHA384 · to 192.0.2.10 · server's TCP RTT 18.1 ms
  HTTP/2      TCP 443     ····················   Answering   18.5 ms · median 18.4 ms · 0 of 35 lost in 1 min   TLSv1.3 TLS_AES_256_GCM_SHA384 · to 192.0.2.10 · server's TCP RTT 18.0 ms
  HTTP/3      UDP 443     ····················   Answering   18.9 ms · median 18.7 ms · 0 of 35 lost in 1 min   TLSv1.3 TLS_AES_128_GCM_SHA256 · to 192.0.2.10
  WebSocket   TCP 443     ····················   Answering   18.3 ms · median 18.2 ms · 0 of 35 lost in 1 min   its own TCP connection, opened by an HTTP/1.1 upgrade
  UDP         UDP 61042   ·················x··   Answering   18.8 ms · median 18.6 ms · 1 of 34 lost in 1 min   DTLS 1.2 TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256 · to 192.0.2.10 · seen from 203.0.113.9 : 51877

IPv6 · server 2001:db8::10
  ...

NAT and egress
  IPv4  This computer 192.168.1.20 · Over HTTPS (TCP) 203.0.113.9 : 50123 · Over HTTP/3 (UDP) 203.0.113.9 : 50998 · Over UDP (WebRTC) 203.0.113.9 : 51877 · Compared Same address, other port · NAT 192.168.1.20 → 203.0.113.9 · MAC 3c:22:fb:12:34:56
  IPv6  This computer 2001:db8:5::9 · Over HTTPS (TCP) 2001:db8:5::9 : 50124 · ... · NAT none: same address

Events
  14:10:31  IPv4 UDP: answering again after 2s, 1 lost
  14:10:29  IPv4 UDP: no answer
  14:09:57  IPv4 HTTP/3: answering, 18.9 ms
```

The dots after each row are its last 20 tries: `·` answered, `x` not answered after it
had answered before. A row is **Answering**, **No answer for** a time, **No answer yet**,
**Not offered** with the reason (such as a name without an address in that family), or
**Restricted** while its address asks for the access code. **NAT and egress** compares how
the server sees this computer over TCP and over UDP: different addresses mean TCP and UDP
leave by different routes or NAT pools.

A window too narrow for a whole row has each row's details on the lines under it, as the
Diag page does, and narrower still its figures too; while Diag runs in a window too short
for those, each row has a line of its own. The table it leaves when it stops has them all.

Piped, Diag writes the events as lines as they happen, and the whole table once it stops.
`--json` writes `event` lines, and a `summary` line with every row at the end.

| Option | What it does |
|---|---|
| `-4`, `-6` | IPv4 or IPv6 only |
| `--seconds N` | stop after this long (default: until Ctrl-C, or the server's Diag limit) |
| `--code`, `--json` | as for a test; an address that asks for the code is given it |

## Access codes

A server that asks for an [access code](access.md) asks t2s too. Give it with `--code`,
or in the `TEXAS2STEP_CODE` environment variable, for a script:

```bash
t2s run https://dallas.example.net/texas2step --code maple.river.42
```

```
t2s: https://dallas.example.net/texas2step asks for an access code: add --code, or set TEXAS2STEP_CODE
```

## Updating t2s

t2s looks for a newer version only when asked:

```bash
t2s update --check
```

```
t2s 2.7.0 (2026-11-20) is available; this is 2.6.0. Install it with: t2s update
```

`t2s update` asks, then installs it over itself:

1. It reads the newest release from GitHub's releases API, or the version given
   (`t2s update 2.6.1`), and finds the file for this computer, such as
   `t2s-windows-amd64.exe`.
2. It downloads it into the folder t2s is in, and checks it against the SHA-256 the
   release gives for it. One that doesn't match is deleted, and nothing changes.
3. It runs the new file once, which must say it is that version.
4. It puts the new file in place of the old. On Windows, where a running program can't
   be replaced, it renames the old one `t2s.exe.old` first; the next run deletes it.

```
Install t2s 2.7.0 (2026-11-20) over 2.6.0 at /usr/local/bin/t2s? [y/N] y
Installed t2s 2.7.0 at /usr/local/bin/t2s, checked against its SHA-256 9c2e…
What's new: https://github.com/robert-patrick-texas/texas2step/releases/tag/v2.7.0
```

It needs to write to that folder: for `/usr/local/bin`, run it with `sudo`. Nothing else
is left on disk. `--yes` installs without asking, for running it on a schedule:

```bash
# /etc/cron.d/t2s: every Monday at 04:00
0 4 * * 1  root  /usr/local/bin/t2s update --yes >> /var/log/t2s-update.log 2>&1
```

```powershell
schtasks /Create /SC WEEKLY /D MON /ST 04:00 /TN "t2s update" `
    /TR "\"$env:LOCALAPPDATA\t2s\t2s.exe\" update --yes"
```

| Option | What it does |
|---|---|
| `VERSION` | install that version, newer or older, instead of the newest |
| `--check` | only say whether a newer release is out |
| `--yes` | install without asking |
| `--proxy URL` | go through this HTTP proxy, such as `http://proxy.example.net:3128` (default: `https_proxy`) |
| `--insecure`, `-k` | check no certificate: for a firewall that inspects TLS with a CA this computer lacks. Nothing then shows the release is genuine, since its SHA-256 comes over the same unchecked connection |
| `--url URL` | ask another releases API, or a mirror that answers as GitHub's does |

## Exit statuses

| Status | When |
|---|---|
| 0 | done; Monitor and Diag stopped with Ctrl-C or at a limit; no newer t2s (`update --check`) |
| 1 | it failed: the reason is on the error output, after `t2s:` |
| 2 | the command or its options weren't understood |
| 100 | `update --check` found a newer t2s |
| 130 | a test was stopped with Ctrl-C |

## Proxies and certificates

Certificates are checked against the computer's own trust store: Windows' certificate
store, and on Linux the system's CA bundle (or `SSL_CERT_FILE` and `SSL_CERT_DIR`). A
firewall that inspects TLS with the organization's own CA works once that CA is installed
there, as browsers need it.

HTTP requests and WebSockets go through the proxy that `https_proxy`, `http_proxy` and
`no_proxy` name, as the Python command line's do. A test through a proxy measures the
path to the proxy and the proxy's own; to measure the network, set `no_proxy` for the
server. UDP, HTTP/3, and the new connections that responsiveness times always go
direct.

## What it sends

Every request names t2s and its version as its User-Agent (`t2s/<version>`), and carries
`?session=` with an id made for the run and `?page=cli`, so the server's
[active clients](clients.md) list shows the run as a command-line one. `run` puts this
computer's own address and its adapter's MAC address in the result; the server keeps them
only with `--save`, and `--no-local` leaves them out.

## Building it

From a clone of the repository, with [Go](https://go.dev/dl/):

```bash
make t2s          # dist/t2s-linux-amd64, dist/t2s-windows-amd64.exe, ...
cd t2s && go build -o t2s .   # this computer's only
```

See [Development](development.md#t2s).
