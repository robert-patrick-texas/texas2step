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
| GET | `/api/v1/down?bytes=N` | Exactly N bytes (≤ `max_download_bytes`) with `Content-Length`: [five founding documents](methodology.md#what-the-tests-send) as text, then random bytes |
| GET | `/api/v1/down?seconds=S` | The documents, then random bytes for S seconds (chunked, ≤ `max_stream_seconds`) |
| GET | `/api/v1/tests/{id}` | Whether the server's CPU held a test's transfers back (below) |
| POST | `/api/v1/up` | Read and discard the body; returns `{"bytes", "rx_ms", "mbps"}` measured server-side |
| WS | `/api/v1/ws` | Echo every message unchanged; closed with code `4001` once `session_max_runtime` is up |
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
command line do this. A server whose host is busy with other work answers the same
way while it refuses tests (`cpu_busy_percent`, [Server CPU](cpu.md#keep-tests-off-a-busy-host)),
with "This server is busy. Try again in a few minutes." and `url` `null`.

The server also times its CPU for each test id: `GET /api/v1/tests/{id}` answers, for
each of the test's transfers that a worker served for a second or more, the busiest
worker's share of a core while it did, and `bound`, whether that was 90 % or more, so
its CPU, not the network, set the rate:

```json
{"test": "k7q2m9xd3p", "download": {"cpu": 0.998, "bound": true}, "upload": {"cpu": 0.42, "bound": false}}
```

The transfers are `download`, `upload` and `one_stream`: add `&transfer=one_stream` to
the one stream's `/down` requests, after the download, to keep it apart. The figures are
kept an hour; `capabilities.test_cpu` in `/api/v1/info` says a server keeps them.

Any request may also carry `?session=<id>` (4 to 32 letters, digits, `-` and `_`) and
`?page=<name>` (`test`, `monitor`, `diag`, `history`, `help`, `api`, `cli` or `server`).
The server's [active clients](clients.md) list tells sessions apart by them; without one,
requests go by address and User-Agent. The page makes a new id each time it loads, and
the command line one for each run. Nothing else changes with them.

### UDP probe protocol

`POST /api/v1/rtc/offer` with `{"sdp": "<offer>", "type": "offer"}` returns
`{"id", "sdp", "type": "answer"}`. ICE is not trickled, so gather all candidates before
sending the offer. The offer must include data channels:

* `probe`: create it with `ordered: false, maxRetransmits: 0`. Every message is echoed,
  verbatim unless stamps are on. texas2step clients send
  `u32 sequence (LE) | f64 send time ms (LE) | padding`, 200 bytes in all on the Test
  page, with random padding, new for each probe.
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
  server withholds that profile (`monitor_voice` or `monitor_video` is `false`, as
  `/api/v1/info`'s `monitor` says), a traffic limit is reached, or, for `"video"`, the
  host is busy with other work (`"server busy"`, with `cpu_busy_percent`); the last two
  come with the `message` and `url` to show. Past a limit while a session runs, the
  server sends `{"op":"limit","message":…,"url":…}` unasked and from then on echoes only
  probes of up to 64 bytes. While the host is busy, it sends a Video call
  `{"op":"limit","to":"voice","message":…,"url":null}` and from then on echoes its probes
  of up to 300 bytes, its voice: the client goes on with the Voice call (a client that
  knows no `to` goes on with path probes, which still come back).

  Once `session_max_runtime` is up, the server sends `{"op":"ended","reason":"runtime","seconds":…}`
  unasked and closes the session a few seconds later. texas2step's pages then stop and ask
  for a reload rather than open another session.

Sessions close when the peer disconnects, after `rtc_session_timeout` seconds idle, or
after `session_max_runtime` seconds in all, where it is set.
The server answers `503` when it already holds `max_rtc_sessions` sessions, or when
every port in its `rtc_port_min`–`rtc_port_max` range is taken.

## Discovery

| Method | Path | Returns |
|---|---|---|
| GET | `/api/v1/info` | name, version (left out for clients outside `internal_networks`), server identity, capabilities (`stamps`: the UDP probe can [stamp](#udp-probe-protocol) its echoes; `test_cpu`: it answers `/api/v1/tests/{id}`), limits (`max_session_seconds`: `session_max_runtime`; `max_monitor_seconds`, `max_diag_seconds`: `page_monitor_max_runtime` and `page_diag_max_runtime`, which pages from other servers keep while they test this one; `0` for none), ICE servers, UI defaults (`one_stream`: whether a test downloads over one stream too, unless asked otherwise), `monitor` (`profile`: Monitor's until a visitor picks; `voice`, `video`: whether the Voice call and Video call profiles are offered); with a [traffic limit](traffic.md), `traffic` (below) |
| GET | `/api/v1/whoami` | client IP, port, family, scheme, HTTP version, user agent |
| GET | `/api/v1/diag/http` | both ends of this HTTP connection, its HTTP version and TLS, the server's TCP RTT and clock (below) |
| GET | `/api/v1/locations` | servers offered in the picker (this one first) |
| GET | `/api/v1/health` | `{"status": "ok", "version": …}`; without the version for clients outside `internal_networks`, or that need an [access code](#access-codes) |
| GET | `/api/v1/clients` | The pages and command-line runs active now, and in `ended` those of the last hour, as `texas2step clients --json` prints them; only for `internal_networks`, and `404` on a server without them. See [Active clients](clients.md#over-the-api). |
| GET | `/api/v1/cpu` | The server's CPU now, the last hour by the minute and each day (`?days=0` to 30), as `texas2step cpu --json` prints it; only for `internal_networks`. See [Server CPU](cpu.md#over-the-api). |
| GET | `/api/v1/metrics` | The same figures in Prometheus's text format; only for `internal_networks` |

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
A target that asks for the access code takes a pass this server issues itself, if their
keys are the same; only targets in `[[locations]]` get one
([Measuring between servers](access.md#measuring-between-servers)).
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
| GET | `/api/v1/access?pass=<pass>` | `{"required", "desk", "codes", "note"}`: whether this client needs a pass, gets the desk's functions, and whether this server gives passes at all (`access_code` is on), and the site's line under the code box (`access_note`, empty for none); with `pass`, also whether that pass is valid and when it expires |
| POST | `/api/v1/access` | `{"code": "maple.river.42"}` (any case and separators, or a standing phrase) returns `{"pass", "expires"}`. A wrong code is `403`. After `access_lockout_after` wrong codes in a row from one /24 or /48, every code from it gets that same `403`, unread, until `access_lockout_minutes` pass without one ([Lockouts and blocks](access.md#lockouts-and-blocks)). A body over 1 KB is `413` |
| GET | `/api/v1/access/code` | The current code, when it changes, and the previous one while it still works; only from `internal_networks` |
| GET | `/api/v1/access/blocks` | `{"lockouts": [{"network", "source", "wrong_codes", "since", "until", "by_hand"}], "counting"}` (`by_hand`: a block, which ends the passes used from the network too); only from `internal_networks` and the server itself |
| POST | `/api/v1/access/blocks` | `{"network": "198.51.100.0/24", "minutes": 120}` locks a network out by hand, or keeps its lockout going, and ends the passes used from it; `minutes` defaults to `access_lockout_minutes`. One that overlaps `internal_networks`, or is wider than a /16 or a /32, is `400` |
| DELETE | `/api/v1/access/blocks?network=<network>` | Ends the lockouts that overlap it (`all`: every one); returns `{"cleared": [...]}` |
| POST | `/api/v1/access/peer` | For [servers that defend together](access.md#servers-that-defend-together): `{"from", "to", "action": "block" \| "clear" \| "check" \| "end-passes" \| "shuffle", "network", "minutes", "kind": "lockout" \| "block", "at", "value"}` (`kind`: a block set by hand, which ends passes there too; `lockout` when left out. `end-passes` ends every pass issued before `at`, in Unix seconds; `shuffle` mixes `value`, 16 hex digits, into the codes from `at`, the latest shuffle staying; neither `at` may be later than the receiving server's clock allows), signed in `X-Texas2step-Time`, `-Nonce` and `-Signature`. Only from `access_peer_networks`, with `access_share_lockouts` on; `404` to everyone else |

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
               "loaded_probes": [17.1, …],
               "rate_limit": {"from_mbps": 938.2, "to_mbps": 312.9, "at_s": 4.2,
                              "before_bytes": 427000000}, …},
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
used one stream or skipped it. Each transfer's `rate_limit` is where its rate stepped down
and stayed down: the rate before (`from_mbps`), when it fell, in seconds from the start of
the transfer (`at_s`), what the transfer had moved by then (`before_bytes`) and the rate
after (`to_mbps`); `null` when it held, and left out of results from before it. See
[Rate limit](methodology.md#rate-limit). Each transfer's `server_cpu` is the busiest
share of a core among the server's workers that served it (`GET /api/v1/tests/{id}`);
at 0.9 or more the server's CPU set the rate. It is left out where the server doesn't
say: before 2.12.0, or a transfer under a second. `config.http` is the HTTP version a browser test
was held to, `"http1"` or `"http3"`, through the server's name for it; it is left out
for Auto ([Streams and HTTP versions](methodology.md#streams-and-http-versions)). `call.mos_rating` is the score's own
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
