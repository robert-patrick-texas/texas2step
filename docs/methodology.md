# Methodology

texas2step measures the path between two endpoints: a **client** (a web browser, or
the Python engine behind the CLI and `/api/v1/measure/*`) and a **texas2step server**.
Everything is driven over standard web transports, so it works wherever a browser can
reach the server: no agents, plugins or raw sockets.

The browser engine (`web/src/engine/`) and the Python engine (`src/texas2step/engine.py`)
implement the same methods against the same wire endpoints, and share statistical
definitions (`stats.ts` / `stats.py`), so their results are comparable.

## Test sequence

| Step | What | Transport | Default |
|---|---|---|---|
| 1 | Idle latency and jitter | HTTP keep-alive `GET /api/v1/ping` | 20 probes after 1 warm-up |
| 2 | Download, with latency under load | N parallel `GET /api/v1/down` + WebSocket probes | 4 streams × 10 s |
| 3 | Upload, with latency under load | N parallel `POST /api/v1/up` + WebSocket probes | 4 streams × 10 s |
| 4 | Packet loss, UDP RTT and jitter | WebRTC data channel, unordered, no retransmits | 500 probes, 10 ms apart |

## Latency

Each probe is a `GET /api/v1/ping` on an already-open keep-alive connection, so TCP and
TLS setup are excluded (the first request warms the connection and is discarded).

* **Browser:** RTT comes from the Resource Timing API: `responseStart − requestStart`,
  minus the server's own processing time from the `Server-Timing: app;dur=…` header.
  This removes JavaScript scheduling noise. The server sends `Timing-Allow-Origin: *`
  so this also works cross-origin. If timing data is unavailable the engine falls back
  to wall-clock time around `fetch`.
* **Python engine:** wall-clock around the request, minus `Server-Timing`. The HTTP
  client adds a little overhead (tens to hundreds of microseconds).

Reported: median (the headline), min, p25, p75, p90, max, standard deviation and the
number of probes that timed out (3 s).

### Jitter

Jitter is the **mean absolute difference between consecutive RTT samples**:

```
jitter = Σ |rttᵢ − rttᵢ₋₁| / (n − 1)
```

This is the unsmoothed form of the RFC 3550 inter-arrival jitter, and it is what most
web speed tests report. It reflects how much successive packets' delay varies, which is
what real-time media notices. Standard deviation is shown alongside.

## Throughput

Throughput tests are **time-boxed and multi-stream**, in the manner of iperf, rather
than a fixed number of fixed-size transfers:

* *N* streams run concurrently for *T* seconds.
* **Download:** each stream requests a large body (`/down?bytes=<server max>`) and reads
  it with a streaming reader, crediting bytes to 100 ms bins at the moment they arrive.
  Requests are re-issued if they finish early, and aborted at the deadline.
* **Upload:** each stream POSTs random, incompressible bodies. Bytes are credited as
  the browser reports them sent (XHR upload progress). Request size adapts: it starts at
  256 KiB and grows until each request takes about a second, which is large enough for
  TCP to reach steady state while still giving many samples. The Python engine streams
  one chunked body per stream instead.
* Payloads are random bytes generated once and served from memory, so compression
  cannot inflate results and the server never touches disk.

From the 100 ms timeline:

* **Stable rate** (the headline) is the mean rate after a **ramp-up window** of
  `clamp(20 % of T, 1 s, 40 % of T)`, which excludes TCP slow start and window growth.
  With the default 10 s test, the first 2 s are excluded.
* **Peak** is the 95th percentile of the 100 ms bins.
* **Per-stream rate** is each stream's bytes over the test, and **fairness** is
  Jain's index over streams: (Σx)² / (n·Σx²). 100 % means every flow got an equal share.

### Streams and HTTP versions

Browsers open at most **six HTTP/1.1 connections** per server, so more than six streams
queue. Over **HTTP/2** all streams share **one TCP connection** as multiplexed streams;
"parallel streams" then measures one TCP flow. The negotiated protocol is shown in the
connection strip. To measure several TCP flows through a reverse proxy, serve
texas2step over HTTP/1.1 (see [HTTP/2 and parallel streams](install.md#http2-and-parallel-streams)).

## Latency under load (bufferbloat)

While each throughput phase runs, the client sends a small probe every 250 ms over a
**dedicated WebSocket**. That is a separate TCP connection outside the browser's HTTP
connection pool, so it queues behind the transfer in the network, not in the browser.
The increase of the loaded median over the idle median is the latency the transfer
added: **bufferbloat**.

| Grade | Added latency (worst of download / upload) |
|---|---|
| A+ | < 5 ms |
| A | < 30 ms |
| B | < 60 ms |
| C | < 200 ms |
| D | < 400 ms |
| F | ≥ 400 ms |

On very fast links (multi-gigabit, or loopback) the server's own CPU can become the
bottleneck and add queueing delay of its own. Running with `--workers 2` or more
reduces this.

## Packet loss (UDP)

Browsers cannot send raw UDP or ICMP, but WebRTC data channels run over
SCTP/DTLS/**UDP**. A channel opened with `ordered: false, maxRetransmits: 0` never
retransmits, so a packet the network drops stays dropped.

1. The client creates a peer connection with two channels, `probe` (unreliable) and
   `control` (reliable), gathers ICE candidates, and posts the offer to
   `POST /api/v1/rtc/offer`. The server (aiortc) answers.
2. The client sends numbered probes (`u32 sequence | f64 send time | padding`, 64 bytes
   by default) on a fixed schedule. Pacing works against the schedule, not
   `setInterval`, so timer clamping cannot drop probes.
3. The server echoes each probe unchanged and counts what it receives and sends.
4. After the last probe the client waits 2 s for stragglers, then asks over `control`
   for the server's counters.

Reported:

* **Loss** = probes without an echo ÷ probes sent.
* **Upstream loss** = 1 − (server received ÷ client sent).
* **Downstream loss** = 1 − (client received ÷ server sent).
* **Out of order**: echoes arriving with a lower sequence number than the previous one.
* **UDP RTT and jitter**: same definitions as above, over the echoed probes.

When the visitor chooses **IPv4 only** or **IPv6 only**, the client drops the server's
candidates of the other family from the answer before connecting, so ICE can only form
a path in the chosen family. The HTTP tests use the server's single-family hostname for
the same choice (see [IPv4 and IPv6](configuration.md#ipv4-and-ipv6)).

If the UDP path cannot be established (UDP blocked, ICE fails), the client falls back to
the WebSocket and says so. Over TCP, loss appears as delay rather than drops, so
fallback results only count probes that time out.

## Readiness

A simple interpretation layer, defined in `web/src/engine/scores.ts`. Each use case has
three tiers; the result is the highest tier whose every requirement is met. Rates are
floors, the rest are ceilings, and missing measurements are not held against you.

| Use case | Great | Good | Fair |
|---|---|---|---|
| 4K video streaming | ↓ 25 Mbps, loss ≤ 1 %, latency ≤ 150 ms | ↓ 15, loss ≤ 2 % | ↓ 5, loss ≤ 5 % |
| Video calls | ↓ 10, ↑ 5, latency ≤ 50, jitter ≤ 10, loss ≤ 0.5 %, bloat ≤ 60 | ↓ 4, ↑ 2, ≤ 100, ≤ 30, ≤ 1.5 %, ≤ 200 | ↓ 1.5, ↑ 1, ≤ 200, ≤ 50, ≤ 3 % |
| Reaction time, VDI and SSH | ↓ 10, ↑ 3, latency ≤ 20, jitter ≤ 5, loss ≤ 0.1 %, bloat ≤ 30 | ↓ 5, ↑ 1, ≤ 50, ≤ 15, ≤ 1 %, ≤ 100 | ≤ 100, ≤ 30, ≤ 2 % |
| Large file transfers | ↓ 500, ↑ 500 | ↓ 100, ↑ 50 | ↓ 25, ↑ 10 |

Below "fair" is "poor". Latency, jitter and bloat are in milliseconds.

"Reaction time, VDI and SSH" (id `interactive`) covers work where every keystroke or click
waits on a round trip: remote desktops and virtual desktops, SSH and terminal sessions,
and anything else that has to react instantly. Low latency, jitter and bufferbloat matter
far more for these than bandwidth does.

## Live monitor

The monitor sends probes continuously over the UDP channel (or the WebSocket fallback)
at 5–50 per second. A probe that has not returned within one second is counted as lost.
Statistics cover the visible window (1, 5 or 15 minutes).

When the site lists two or more servers, the monitor can probe two of them at once. The
browser opens a separate session to each server directly, over UDP where it can and
TCP where it can't, and sends each probe to both from the same timer. Both paths are
sampled at the same moments from the same client, so a difference between them belongs
to the paths, not to when or where they were measured. The table gives each figure for
both servers and the difference, B − A: milliseconds for times, percentage points for
loss.

Throughput is deliberately not compared this way. Two transfers at once would share
the client's own link, and both would read low.

## Diag

The Diag page tries each way of reaching the server once a second, for as long as the
page is open: HTTPS, WebSocket and UDP, over IPv4 and IPv6.

* **HTTPS** is a small request to `/api/v1/diag/http` on the server's IPv4-only or
  IPv6-only address ([IPv4 and IPv6](configuration.md#ipv4-and-ipv6)). The answer gives
  the client's address and port as the server saw them, the server's own address and
  port, the TLS version and cipher, the server kernel's TCP round trip for that
  connection (nginx's `$tcpinfo_rtt`), and the server's clock. The browser reports the
  HTTP version it used (Resource Timing's `nextHopProtocol`). Browsers keep connections
  open, so each attempt is a request on an existing connection, not a new one.
* **WebSocket** opens its own TCP connection by an HTTP/1.1 upgrade and echoes a probe
  a second. A closed socket is opened again every 2 s.
* **UDP** is a WebRTC data channel held to one family (the server's candidates of the
  other family are dropped) that echoes a probe a second. A session that won't open is
  tried again every 10 s, since opening one takes seconds and uses one of the server's
  ports. The server reports the client's address and port on the chosen path (`addr` on
  the control channel), which is how the NAT and egress table compares UDP with TCP.

An attempt not answered within 2 s (1.5 s for WebSocket and UDP) counts as missed. A
method that has answered before shows a miss as loss (red). One that hasn't answered
yet stays grey and keeps trying. One the server doesn't offer is grey and isn't tried.
The clock offset is the server's time minus the browser's at the middle of the round
trip, ± half the round trip.

## Known limitations

* The UDP test needs UDP from the client to the server. Set `rtc_port_min` and
  `rtc_port_max` to confine it to a range a firewall can open (see
  [Pinning the UDP ports](install.md#pinning-the-udp-ports)). Where UDP is blocked, the client
  falls back to TCP.
* A browser tab in the background is throttled, so keep the tab visible while testing.
* Results describe the path to *this* server. Pick a location close to the service you
  care about.
