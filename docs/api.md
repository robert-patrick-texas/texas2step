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

### UDP probe protocol

`POST /api/v1/rtc/offer` with `{"sdp": "<offer>", "type": "offer"}` returns
`{"id", "sdp", "type": "answer"}`. ICE is not trickled, so gather all candidates before
sending the offer. The offer must include data channels:

* `probe`: create it with `ordered: false, maxRetransmits: 0`. Every message is echoed
  verbatim. texas2step clients send `u32 sequence (LE) | f64 send time ms (LE) | padding`.
* `control` (reliable, optional): send `{"op":"stats"}` to receive
  `{"op":"stats","rx":…,"tx":…,"rx_bytes":…}`, or `{"op":"reset"}` to zero the counters.
  `{"op":"addr"}` returns both ends of the UDP path ICE chose:
  `{"op":"addr","ip":"192.0.2.7","port":50123,"server_ip":"192.0.2.1","server_port":61042}`.
  `ip` and `port` are the client as the server sees it, after any NAT. All four are
  `null` until the path is up.

Sessions close when the peer disconnects or after `rtc_session_timeout` seconds idle.
The server answers `503` when it already holds `max_rtc_sessions` sessions, or when
every port in its `rtc_port_min`–`rtc_port_max` range is taken.

## Discovery

| Method | Path | Returns |
|---|---|---|
| GET | `/api/v1/info` | name, version, server identity, capabilities, limits, ICE servers, UI defaults |
| GET | `/api/v1/whoami` | client IP, port, family, scheme, HTTP version, user agent |
| GET | `/api/v1/diag/http` | both ends of this HTTP connection, its HTTP version and TLS, the server's TCP RTT and clock (below) |
| GET | `/api/v1/locations` | servers offered in the picker (this one first) |
| GET | `/api/v1/health` | `{"status": "ok", "version": …}` |

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
At most two run at once per worker; extra requests get `429`.

| Method | Path | Body (defaults) |
|---|---|---|
| POST | `/api/v1/measure/latency` | `{"target", "count": 20}` |
| POST | `/api/v1/measure/download` | `{"target", "seconds": 10, "streams": 4, "loaded_latency": true}` |
| POST | `/api/v1/measure/upload` | same as download |
| POST | `/api/v1/measure/loss` | `{"target", "packets": 500, "interval_ms": 10, "size": 64}` |
| POST | `/api/v1/measure/full` | `{"target", "seconds", "streams", "packets", "interval_ms", "loss": true}` |

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
`internal_networks` sends a pass as `?pass=<pass>` with each request under `/api/v1/`
(except `/health` and `/access`); without a valid one it gets `401`. The WebSocket takes
it the same way. With `internal_networks` set, clients from outside them get `404` for
reading results, `/api/v1/measure/*`, `/api/docs` and `/api/openapi.json`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/access?pass=<pass>` | `{"required", "desk"}`: whether this client needs a pass, and gets the desk's functions; with `pass`, also whether that pass is valid and when it expires |
| POST | `/api/v1/access` | `{"code": "maple.river.42"}` (any case and separators) returns `{"pass", "expires"}`. A wrong code is `403`; 10 in 10 minutes from one address, `429` |
| GET | `/api/v1/access/code` | The current code, when it changes, and the previous one while it still works; only from `internal_networks` |

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
  "config": {"streams": 4, "seconds": 10, "packets": 500, "interval_ms": 10},
  "latency": {"count": 20, "lost": 0, "min": 2.9, "median": 3.2, "p90": 3.8, "jitter": 0.4, "samples": [ … ], …},
  "download": {"mbps": 941.2, "peak_mbps": 948.0, "bytes": …, "timeline": [ …per 100 ms… ],
               "per_stream_mbps": [ … ], "loaded_latency": { …latency stats… }, …},
  "upload": { …same as download… },
  "loss": {"transport": "webrtc", "sent": 500, "received": 499, "loss_pct": 0.2,
           "up_loss_pct": 0.0, "down_loss_pct": 0.2, "out_of_order": 0,
           "rtt": { …latency stats… }, "packets": [3.1, 3.0, null, …]},
  "bufferbloat": {"added_download_ms": 4.1, "added_upload_ms": 11.0, "grade": "A"},
  "readiness": [{"id": "calls", "label": "Video calls", "level": "great", "detail": "…"}, …]
}
```

`readiness` is computed by the browser client only. `client.local_ip` is this computer's
own address: the browser's on the UDP path, where it reveals it, or `texas2step run`'s on
its connection. `client.mac` is the MAC address of the NIC the CLI's connection left by
(`mac_source` `"nic"`, on Linux) or one read from an EUI-64 IPv6 address (`"eui64"`); see
[This computer's address](methodology.md#this-computers-address). Each is left out when
unknown, and `/measure/full` leaves them out. Summaries carry them as `client_local_ip`,
`client_mac` and `client_mac_source`.
