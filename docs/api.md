# API

Interactive reference: `/api/docs` (OpenAPI explorer) and `/api/openapi.json` on any
server. The **API** tab in the web UI has copy-ready examples against that server.

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

Sessions close when the peer disconnects or after `rtc_session_timeout` seconds idle.
The server answers `503` when it already holds `max_rtc_sessions` sessions, or when
every port in its `rtc_port_min`–`rtc_port_max` range is taken.

## Discovery

| Method | Path | Returns |
|---|---|---|
| GET | `/api/v1/info` | name, version, server identity, capabilities, limits, ICE servers, UI defaults |
| GET | `/api/v1/whoami` | client IP, port, family, scheme, HTTP version, user agent |
| GET | `/api/v1/locations` | servers offered in the picker (this one first) |
| GET | `/api/v1/health` | `{"status": "ok", "version": …}` |

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
curl -s -X POST https://dal.example.net/texas2step/api/v1/measure/full \
  -H 'content-type: application/json' \
  -d '{"target": "https://hou.example.net/texas2step", "seconds": 10}'
```

## Results

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/results` | Store a result object (`"version": 1`, ≤ 2 MB). Returns its summary and `id` |
| GET | `/api/v1/results?limit=50&before=<epoch>` | Summaries, newest first |
| GET | `/api/v1/results/{id}` | One result in full; also viewable at `/r/{id}` |

## Result object

Browser runs, `texas2step run --json` and `/measure/full` share one shape (snake_case):

```jsonc
{
  "version": 1,
  "tool": "texas2step-web/<version>",
  "started_at": "2026-10-18T07:24:00Z",
  "server": {"id": "dal1", "name": "Dallas DC", "location": "Dallas, TX", "url": "https://…", "version": "<version>"},
  "client": {"ip": "10.1.2.3", "family": "IPv4", "protocol": "h2", "user_agent": "…"},
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

`readiness` is computed by the browser client only.
