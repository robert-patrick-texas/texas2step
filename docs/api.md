# API

Interactive reference: `/api/docs` (OpenAPI explorer) and `/api/openapi.json` on any
server. The explorer is served by texas2step itself, so it works without internet access.
The **API** tab in the web UI has copy-ready examples against that server.

Paths below are relative to the server's base URL. That is
`https://www.example.net/texas2step` for a server published at `/texas2step/` (see
[Install](install.md)), or just the origin, such as `http://127.0.0.1:8080`, when
reaching the service directly.

All responses carry `Cache-Control: no-store`, `Timing-Allow-Origin: *` and CORS
headers. Measurement endpoints add `Server-Timing` so clients can subtract server time.

## Measurement primitives

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/ping` | Minimal response for round-trip timing. `Server-Timing: app;dur=…` |
| GET | `/api/v1/down?bytes=N` | Exactly N random bytes (≤ `max_download_bytes`) with `Content-Length` |
| GET | `/api/v1/down?seconds=S` | Stream random bytes for S seconds (chunked, ≤ `max_stream_seconds`) |
| POST | `/api/v1/up` | Read and discard the body; returns `{"bytes", "rx_ms", "mbps"}` measured server-side |
| WS | `/api/v1/ws` | Echo every message unchanged |
| POST | `/api/v1/rtc/offer` | WebRTC signalling for the UDP probe (see below) |

```bash
# 1 GB download at full speed
curl -o /dev/null -w '%{speed_download} B/s\n' 'https://www.example.net/texas2step/api/v1/down?bytes=1000000000'

# Saturate for 30 s (useful with -P in a loop, or alongside other traffic)
curl -o /dev/null 'https://www.example.net/texas2step/api/v1/down?seconds=30'

# 500 MB upload; the server reports what it measured
head -c 500000000 /dev/urandom | curl -s -X POST --data-binary @- https://www.example.net/texas2step/api/v1/up
```

On a server with a [traffic limit](traffic.md), `/down` and `/up` answer `503` once the
limit is reached, with the site's `traffic_message` and `traffic_url` (`null` when not
set). The Test page shows the message, then "Click here to continue." linking to the URL;
`detail` puts both in plain text, for clients that can't show a link:

```json
{
  "detail": "Testing temporarily disabled. Click here to continue: https://houston.example.net/texas2step/",
  "message": "Testing temporarily disabled.",
  "url": "https://houston.example.net/texas2step/"
}
```

They don't say when tests start again. Add `?test=<id>` (up to 64 letters, digits, `-`
and `_`) to every request of one test: once the server has let a test in, it lets the
rest of it in for up to 10 minutes, so a running test finishes. The browser and the
command line do this.

### UDP probe protocol

`POST /api/v1/rtc/offer` with `{"sdp": "<offer>", "type": "offer"}` returns
`{"id", "sdp", "type": "answer"}`. ICE is not trickled, so gather all candidates before
sending the offer. The offer must include data channels:

* `probe`: create it with `ordered: false, maxRetransmits: 0`. Every message is echoed,
  verbatim unless stamps are on. texas2step clients send
  `u32 sequence (LE) | f64 send time ms (LE) | padding`, 200 bytes in all.
* `control` (reliable, optional): send `{"op":"stats"}` to receive
  `{"op":"stats","rx":…,"tx":…,"rx_bytes":…}`, or `{"op":"reset"}` to zero the counters.
  `{"op":"addr"}` returns both ends of the UDP path ICE chose:
  `{"op":"addr","ip":"192.0.2.7","port":50123,"server_ip":"192.0.2.1","server_port":61042}`.
  `ip` and `port` are the client as the server sees it, after any NAT. All four are
  `null` until the path is up. `{"op":"stamp"}`, answered with `{"op":"stamp"}`, turns on
  **stamps** for the session: from then on the server writes into each echo at least 24
  bytes long, at bytes 12 to 23, its own clock when the probe arrived (`f64` ms, LE) and
  how many probes it had received by then, this one included (`u32`, LE). The client's
  sequence number and send time, and the rest of the padding, come back unchanged. The
  server's clock is its own: compare its stamps with each other, not with the client's
  clock. Servers that can stamp say so with `capabilities.stamps` in `/api/v1/info`.

  `{"op":"profile","name":"voice"}` (or `"video"`, or `"path"`) says which of Monitor's
  profiles the session runs. A voice or video session counts toward a
  [traffic limit](traffic.md#monitors-call-profiles). The answer is
  `{"op":"profile","ok":true,"name":"voice"}`, or `"ok": false` with a `reason`: the
  server withholds video (`monitor_video = false`), or a traffic limit is reached, when
  the site's `message` and `url` come with it. Past a limit while a session runs, the
  server sends `{"op":"limit","message":…,"url":…}` unasked and from then on echoes only
  probes of up to 64 bytes.

Sessions close when the peer disconnects or after `rtc_session_timeout` seconds idle.
The server answers `503` when it already holds `max_rtc_sessions` sessions, or when
every port in its `rtc_port_min`–`rtc_port_max` range is taken.

## Discovery

| Method | Path | Returns |
|---|---|---|
| GET | `/api/v1/info` | name, version (left out for clients outside `internal_networks`), server identity, capabilities (`stamps`: the UDP probe can [stamp](#udp-probe-protocol) its echoes), limits, ICE servers, UI defaults (`one_stream`: whether a test downloads over one stream too, unless asked otherwise), `monitor` (`profile`: Monitor's until a visitor picks; `video`: whether the Video call profile is offered); with a [traffic limit](traffic.md), `traffic` (below) |
| GET | `/api/v1/whoami` | client IP, port, family, scheme, HTTP version, user agent |
| GET | `/api/v1/diag/http` | both ends of this HTTP connection, its HTTP version and TLS, the server's TCP RTT and clock (below) |
| GET | `/api/v1/locations` | servers offered in the picker (this one first) |
| GET | `/api/v1/health` | `{"status": "ok", "version": …}`; without the version for clients outside `internal_networks`, or that need an [access code](#access-codes) |

With a traffic limit, `traffic` says whether this client may start a test, and if not,
what it is told:

```json
{"open": false, "message": "Testing temporarily disabled.", "url": null}
```

Clients in `internal_networks` also get when tests start again and why, and the figures
in bytes (`allowance` is `null` without that period's limit). With
`traffic_count_internal = false`, `open` is `true` for them even past the limit:

```json
{
  "open": false,
  "message": "Testing temporarily disabled.",
  "url": null,
  "until": "2026-10-24T00:00:00-04:00",
  "reason": "Today's allowance is used up; tests start again at midnight (America/New_York)",
  "counts": "sent",
  "count_internal": true,
  "today": {"counted": 50210000000, "allowance": 50000000000},
  "month": {"counted": 539100000000, "allowance": 1000000000000}
}
```

`GET /api/v1/diag/http` answers once per request, uncached, for the Diag tab:

```json
{
  "client": {"ip": "2001:db8::7", "port": 51234, "family": "IPv6"},
  "server": {"ip": "2001:db8::1", "port": 443, "family": "IPv6"},
  "http_version": "HTTP/2.0",
  "tls": {"protocol": "TLSv1.3", "cipher": "TLS_AES_256_GCM_SHA384"},
  "tcp_rtt_ms": 1.234,
  "tcp_rttvar_ms": 0.567,
  "time_ms": 1792385060799.1
}
```

Behind nginx, the client port, server address and port, TLS and the kernel's TCP
round-trip time (`$tcpinfo_rtt`) come from headers the package's snippet sets. They're
believed only from `trusted_proxies`, and a proxy that doesn't send them leaves those
fields `null`. Without a proxy, `server` is the socket's own address, and `tls` and the
TCP times are `null`. `time_ms` is the server's clock, in milliseconds since the epoch.

## Server-side measurements

The server runs the Python engine **toward another texas2step server** and returns the
result. `target` must be a configured location URL unless `allow_any_target` is set.
At most two run at once per worker; extra requests get `429`. A
[traffic limit](traffic.md) refuses download, upload and full ones with `503`, and counts
this server's side of them.

| Method | Path | Body (defaults) |
|---|---|---|
| POST | `/api/v1/measure/latency` | `{"target", "count": 20}` |
| POST | `/api/v1/measure/download` | `{"target", "seconds": 10, "streams": 4, "loaded_latency": true}` |
| POST | `/api/v1/measure/upload` | same as download |
| POST | `/api/v1/measure/loss` | `{"target", "packets": 500, "interval_ms": 10, "size": 200}` |
| POST | `/api/v1/measure/full` | `{"target", "seconds", "streams", "packets", "interval_ms", "loss": true, "one_stream"}`; without `one_stream`, as the target's `default_one_stream` says |

```bash
curl -s -X POST https://dallas.example.net/texas2step/api/v1/measure/full \
  -H 'content-type: application/json' \
  -d '{"target": "https://houston.example.net/texas2step", "seconds": 10}'
```

## Results

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/results` | Store a result object (`"version": 1`, ≤ 2 MB). Returns its summary and `id` |
| GET | `/api/v1/results?limit=50&before=<epoch>` | Summaries, newest first |
| GET | `/api/v1/results/{id}` | One result in full; also viewable at `/r/{id}`. Case and separators in the ID don't matter |

## Access codes

On a server with `access_code` on ([Access codes](access.md)), a client from outside
`internal_networks` sends a pass with each request under `/api/v1/` (except `/health`
and `/access`); without a valid one it gets `401`. It goes as `?pass=<pass>`, or in an
`Authorization: Bearer <pass>` header; a WebSocket can also carry it in its subprotocol,
offering `texas2step` and `texas2step.pass.<pass>`, and is answered with `texas2step`. With `internal_networks` set, clients from outside them get `404` for
reading results, `/api/v1/measure/*`, `/api/docs`, `/api/openapi.json`, the current code and
the lockouts, and the browser's requests to these made for a page on another site get
`403`. With `internal_networks` set, `/api/v1/info` and `/api/v1/health` leave out the
version for clients outside them; without them, `/api/v1/health` leaves it out for
clients that would need a pass.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/access?pass=<pass>` | `{"required", "desk", "codes"}`: whether this client needs a pass, gets the desk's functions, and whether this server gives passes at all (`access_code` is on); with `pass`, also whether that pass is valid and when it expires |
| POST | `/api/v1/access` | `{"code": "maple.river.42"}` (any case and separators, or a standing phrase) returns `{"pass", "expires"}`. A wrong code is `403`. After `access_lockout_after` wrong codes in a row from one /24 or /48, every code from it gets that same `403`, unread, until `access_lockout_minutes` pass without one ([Lockouts and blocks](access.md#lockouts-and-blocks)). A body over 1 KB is `413` |
| GET | `/api/v1/access/code` | The current code, when it changes, and the previous one while it still works; only from `internal_networks` |
| GET | `/api/v1/access/blocks` | `{"lockouts": [{"network", "source", "wrong_codes", "since", "until", "by_hand"}], "counting"}` (`by_hand`: a block, which ends the passes used from the network too); only from `internal_networks` and the server itself |
| POST | `/api/v1/access/blocks` | `{"network": "198.51.100.0/24", "minutes": 120}` locks a network out by hand, or keeps its lockout going, and ends the passes used from it; `minutes` defaults to `access_lockout_minutes`. One that overlaps `internal_networks`, or is wider than a /16 or a /32, is `400` |
| DELETE | `/api/v1/access/blocks?network=<network>` | Ends the lockouts that overlap it (`all`: every one); returns `{"cleared": [...]}` |
| POST | `/api/v1/access/peer` | For [servers that defend together](access.md#servers-that-defend-together): `{"from", "to", "action": "block" \| "clear" \| "check", "network", "minutes", "kind": "lockout" \| "block"}` (`kind`: a block set by hand, which ends passes there too; `lockout` when left out), signed in `X-Texas2step-Time`, `-Nonce` and `-Signature`. Only from `access_peer_networks`, with `access_share_lockouts` on; `404` to everyone else |

```bash
pass=$(curl -s -X POST https://speedtest.example.net/texas2step/api/v1/access \
  -H 'content-type: application/json' -d '{"code": "maple.river.42"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["pass"])')
curl -s "https://speedtest.example.net/texas2step/api/v1/info?pass=$pass"
```

The command line does this with `--code`: `texas2step run <url> --code maple.river.42`.

## Result object

Browser runs, `texas2step run --json` and `/measure/full` share one shape (snake_case):

```jsonc
{
  "version": 1,
  "tool": "texas2step-web/<version>",
  "started_at": "2026-10-18T07:24:00Z",
  "server": {"id": "dallas", "name": "dallas.example.net", "location": "Dallas, TX", "url": "https://…", "version": "<version>"},
  "client": {"ip": "203.0.113.7", "family": "IPv4", "protocol": "h2", "user_agent": "…",
             "local_ip": "10.0.0.10", "mac": "00:11:22:33:44:55", "mac_source": "nic"},
  "config": {"streams": 4, "seconds": 10, "packets": 500, "interval_ms": 10, "one_stream": true},
  "latency": {"count": 20, "lost": 0, "min": 2.9, "median": 3.2, "p90": 3.8, "jitter": 0.4,
              "samples": [ … ], "setup_ms": 11.7, …},
  "download": {"mbps": 941.2, "peak_mbps": 948.0, "bytes": …, "timeline": [ …per 100 ms… ],
               "per_stream_mbps": [ … ], "loaded_latency": { …latency stats… },
               "connections": {"setup_ms": [30.3, …], "lost": 0, "tls": true},
               "loaded_probes": [17.1, …], …},
  "one_stream": { …same as download, with one stream and no loaded latency… },
  "upload": { …same as download… },
  "loss": {"transport": "webrtc", "sent": 500, "received": 499, "loss_pct": 0.2,
           "up_loss_pct": 0.0, "down_loss_pct": 0.2, "out_of_order": 0, "size": 200,
           "rtt": { …latency stats… }, "packets": [3.1, 3.0, null, …],
           "up_ms": [0.4, 0.0, null, …], "down_ms": [0.1, 0.3, null, …], "lost_upstream": []},
  "bufferbloat": {"added_download_ms": 4.1, "added_upload_ms": 11.0, "grade": "A"},
  "call": {"basis": "each way", "interval_ms": 20, "delay_ms": 81.6, "worse": "down",
           "packets": 250, "late": 0, "late_pct": 0.0, "impaired_pct": 0.2, "burst_ratio": 1.0,
           "longest_gap_ms": 20, "r": 90.5, "mos": 4.35, "mos_rating": "excellent",
           "rating": "excellent", "down": { …the same figures, for what you hear… },
           "up": { …and for what the other end hears… }},
  "responsiveness": {"rpm": 1850, "download_rpm": 1910, "upload_rpm": 1850, "basis": "ippm",
                     "download": {"rpm": 1910, "foreign_rpm": 1640, "loaded_rpm": 2180,
                                  "setup_ms": 109.8, "loaded_ms": 27.5,
                                  "loaded_on": "loaded connection"},
                     "upload": { … }, "idle_setup_ms": 11.7},
  "readiness": [{"id": "calls", "label": "Video calls", "level": "great", "detail": "…"}, …]
}
```

`call` is a G.711 call's quality over the path the packet-loss test measured, and
`responsiveness` is round trips per minute under load: see
[Call quality](methodology.md#call-quality) and
[Responsiveness](methodology.md#responsiveness). With a server that stamps its echoes,
`loss.up_ms` and `loss.down_ms` give each probe's delay above the fastest, each way, and
`loss.lost_upstream` the probes lost on the way to the server; `call.basis` is then
`"each way"`, with `down` and `up` scored apart and the worse one's figures at the top.
Without stamps, `call.basis` is `"round trip"` and there are no `up` and `down`.
`responsiveness.basis` is `"ippm"` when new connections were timed during the transfers
(`connections`; from the command line also `tcp_ms`, `tls_ms` and `http_ms`), and
`"latency under load"` for results from before. `one_stream` is left out when the test
used one stream or skipped it. `call.mos_rating` is the score's own
rating, and `call.rating` the one shown: lower when a gap is long enough for a caller to
hear ([Quality and user experience](experience.md#adjustments-for-what-callers-hear)). Each is left out without the figures it
comes from: `call` without echoed UDP probes, `responsiveness` without latency-under-load
probes. Both clients work them out the same way, and the Test page works them out for a
result saved before they existed. `readiness` is computed by the browser client only. `client.local_ip` is this computer's
own address: the browser's on the UDP path, where it reveals it, or `texas2step run`'s on
its connection. `client.mac` is the MAC address of the NIC the CLI's connection left by
(`mac_source` `"nic"`, on Linux) or one read from an EUI-64 IPv6 address (`"eui64"`); see
[This computer's address](methodology.md#this-computers-address). Each is left out when
unknown, and `/measure/full` leaves them out. Summaries carry them as `client_local_ip`,
`client_mac` and `client_mac_source`, and `traffic_bytes`: `download.bytes`,
`one_stream.bytes` and `upload.bytes` added up, what the test moved as the client counted
it (`null` without any of them).
