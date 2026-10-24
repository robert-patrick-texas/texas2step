# Methodology

texas2step measures the path between two endpoints: a **client** (a web browser, or
the Python engine behind the CLI and `/api/v1/measure/*`) and a **texas2step server**.
Everything is driven over standard web transports, so it works wherever a browser can
reach the server: no agents, plugins or raw sockets.

The browser engine (`web/src/engine/`) and the Python engine (`src/texas2step/engine.py`)
implement the same methods against the same wire endpoints, and share statistical
definitions (`stats.ts` / `stats.py`, and `quality.ts` / `quality.py` for call quality and
responsiveness), so their results are comparable.

## Test sequence

| Step | What | Transport | Default |
|---|---|---|---|
| 1 | Idle latency and jitter | HTTP keep-alive `GET /api/v1/ping` | 20 probes after 1 warm-up |
| 2 | Download, with latency under load and responsiveness | N parallel `GET /api/v1/down`, WebSocket probes, a new connection a second | 4 streams × 10 s |
| 3 | One stream's download | One `GET /api/v1/down` | 1 stream × 10 s, skipped when the test uses one |
| 4 | Upload, with latency under load and responsiveness | N parallel `POST /api/v1/up`, WebSocket probes, a new connection a second | 4 streams × 10 s |
| 5 | Packet loss, UDP RTT and jitter, call quality | WebRTC data channel, unordered, no retransmits | 500 probes of 200 bytes, 10 ms apart |

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

### One stream

After the download, the test downloads over **one stream** for the same time, measured in
the same way: what a single file copy, backup or remote-desktop session gets, as each of
those uses one connection. It is shown beside the multi-stream rate, as a share of it.
It is skipped when the test itself uses one stream. Whether it runs otherwise is the
server's `default_one_stream`, unless the visitor chooses in Test options or the command
line with `--one-stream` or `--no-one-stream`. Why one stream can get less than several is in
[Quality and user experience](experience.md#one-stream).

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

## Responsiveness

Responsiveness is the number of round trips a connection manages per minute while it's
busy, in **RPM**: 60,000 divided by the round trip in milliseconds, so higher is better.
1,000 RPM is 60 ms a round trip. It is the measure of the IETF draft
[Responsiveness under Working Conditions](https://datatracker.ietf.org/doc/draft-ietf-ippm-responsiveness/),
which macOS's `networkQuality` also reports.

texas2step measures it as the draft does, during each transfer:

* **New connections.** One a second, each timed from the start of its TCP connect to the
  answer to its first request. The browser opens a WebSocket for this, as a WebSocket
  always gets a connection of its own, and can only time it as a whole: that is three
  round trips with TLS (the TCP connect, the TLS handshake and the request) and two
  without, and the draft's mean of the steps is the open time divided by them. The
  command line times each step on its own.
* **Round trips on the loaded connection.** Over HTTP/2 or HTTP/3 a request shares the
  transfer's connection, so the browser sends a `GET /api/v1/ping` on it every 250 ms.
  Over HTTP/1.1 a request can't, and the latency-under-load probes, on a connection of
  their own beside the transfer, stand in. The command line uses HTTP/1.1.

Each kind is taken as the mean of its fastest 95 %, leaving out the slowest 5 %, and
turned into RPM. A direction's responsiveness is the mean of the two RPM figures, and the
Results tile shows the lower direction's. A new connection is also timed before the
load: what each new connection costs, idle.

Results saved before new connections were timed get their responsiveness from the
latency-under-load probes alone.

## Packet loss (UDP)

Browsers cannot send raw UDP or ICMP, but WebRTC data channels run over
SCTP/DTLS/**UDP**. A channel opened with `ordered: false, maxRetransmits: 0` never
retransmits, so a packet the network drops stays dropped.

1. The client creates a peer connection with two channels, `probe` (unreliable) and
   `control` (reliable), gathers ICE candidates, and posts the offer to
   `POST /api/v1/rtc/offer`. The server (aiortc) answers.
2. The client asks the server to **stamp** its echoes, where the server can.
3. The client sends numbered probes (`u32 sequence | f64 send time | padding`) on a fixed
   schedule. They are 200 bytes, at least the size of a voice call's packets, so a path
   that can't carry them can't carry calls. Pacing works against the schedule, not
   `setInterval`, so timer clamping cannot drop probes.
4. The server echoes each probe and counts what it receives and sends. A stamped echo
   also carries the server's clock when the probe arrived, and how many probes had
   arrived by then ([UDP probe protocol](api.md#udp-probe-protocol)).
5. After the last probe the client waits 2 s for stragglers, then asks over `control`
   for the server's counters.

Reported:

* **Loss** = probes without an echo ÷ probes sent.
* **Upstream loss** = 1 − (server received ÷ client sent).
* **Downstream loss** = 1 − (client received ÷ server sent).
* **Out of order**: echoes arriving with a lower sequence number than the previous one.
* **UDP RTT and jitter**: same definitions as above, over the echoed probes.
* **Each direction's delay**, from the stamps: each probe's one-way delay above the
  fastest probe's in that direction. Upstream is the server's arrival time less the
  client's send time, downstream the client's receipt less the server's arrival. The two
  computers' clocks needn't agree: their difference is the same for every probe and drops
  out.
* **Which way each loss went**, from the stamps: between two echoes, the server's counts
  say how many of the probes in between reached it, and the rest were lost on the way
  there. The count each way is exact. Where one gap lost probes both ways, which went
  which way can't be told, and those lost on the way there are taken to be the first.

When the visitor chooses **IPv4 only** or **IPv6 only**, the client drops the server's
candidates of the other family from the answer before connecting, so ICE can only form
a path in the chosen family. The HTTP tests use the server's single-family hostname for
the same choice (see [IPv4 and IPv6](configuration.md#ipv4-and-ipv6)).

If the UDP path cannot be established (UDP blocked, ICE fails), the client falls back to
the WebSocket and says so. Over TCP, loss appears as delay rather than drops, so
fallback results only count probes that time out.

## Call quality

The packet-loss test's probes also tell how a voice call over the same path would sound.
The score is a **MOS** (mean opinion score, 1 to 5) from the ITU-T G.107 **E-model**, for
a G.711 call with packet-loss concealment. The definitions are in
`web/src/engine/quality.ts` and `src/texas2step/quality.py`.

1. **Late packets.** A call plays its packets out of a jitter buffer, 60 ms here. A probe
   that arrived more than 60 ms behind the fastest one would have missed its turn, so it
   counts as lost. With stamps this is measured in each direction; from a server without
   them, or over the TCP fallback, on the round trip.
2. **The call's own packets.** A call sends a packet every 20 ms, so bursts are counted
   on the probes at that spacing: every other probe at the default 10 ms, every fourth at
   5 ms, every probe at 20 ms. A 100 ms outage is then the 5 packets a call loses, not 10
   probes. The shares of lost and late packets count every probe, which makes them
   steadier.
3. **The score.**
   * Mouth-to-ear delay *d*: half the median UDP round trip, plus the 80 ms a call adds
     (20 ms to fill a packet, and the 60 ms buffer).
   * Delay impairment *Id* = 0.024 *d*, plus 0.11 (*d* − 177.3) beyond 177.3 ms: Cole
     and Rosenbluth's fit to G.107's.
   * Loss impairment *Ie,eff* = 95 *Ppl* / (*Ppl* / *BurstR* + 25.1), where *Ppl* is the
     percentage of packets lost or late, and 25.1 is G.711's robustness to loss with
     concealment (ITU-T G.113 Appendix I).
   * *R* = 93.2 − *Id* − *Ie,eff*, and MOS = 1 + 0.035 *R* + 0.000007 *R* (*R* − 60)
     (100 − *R*), from 1 below *R* 0 to 4.5 above *R* 100.

4. **The rating.** The score is read to one decimal, as it is shown, and rated
   excellent from 4.3, good from 4.0, fair from 3.6, poor from 3.1, and bad below. A gap a
   caller would hear lowers the rating: the longest gap above 60 ms allows at most good,
   200 ms or more at most fair.
5. **Each direction.** With stamps, each direction is a call of its own: what you hear
   is the downstream probes that reached the server, and what the other end hears is the
   upstream. The tile shows the worse of the two, by rating and then by score.

*BurstR*, the **burst ratio**, is the average run of lost or late packets divided by the
run random loss would give, 1 / (1 − *p*) for a share *p*. It is 1 for random loss and
more for bursts. The **longest gap** is the longest run of lost or late packets, in
milliseconds of the call.

What each rating means to callers, why texas2step adjusts the E-model for gaps, the
score's limits and the sources are in [Quality and user experience](experience.md).

## Readiness

A simple interpretation layer, defined in `web/src/engine/scores.ts`. Each use case has
three tiers; the result is the highest tier whose every requirement is met. Rates and
the [call-quality rating](#call-quality) are floors, the rest are ceilings, and missing
measurements are not held against you.

| Use case | Great | Good | Fair |
|---|---|---|---|
| 4K video streaming | ↓ 25 Mbps, loss ≤ 1 %, latency ≤ 150 ms | ↓ 15, loss ≤ 2 % | ↓ 5, loss ≤ 5 % |
| Video calls | ↓ 10, ↑ 5, latency ≤ 50, jitter ≤ 10, loss ≤ 0.5 %, bloat ≤ 60, call excellent | ↓ 4, ↑ 2, ≤ 100, ≤ 30, ≤ 1.5 %, ≤ 200, call good | ↓ 1.5, ↑ 1, ≤ 200, ≤ 50, ≤ 3 %, call fair |
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

### This computer's address

Browsers hide the computer's own address from web pages. WebRTC, the only place a page
could see it, gives a random name ending in `.local` instead. So by default the NAT and
egress box shows only the addresses the server sees. Where the browser reveals it, the
box adds **This computer**, the address of the interface the UDP path leaves by, and a
**NAT** row comparing it with the address the server sees. Browser test runs save it
with their results, in History's **Local** column.

To have managed browsers reveal it to this site only:

* **Chrome and Edge**: the `WebRtcLocalIpsAllowedUrls` policy, listing the site, such as
  `https://www.example.net`.
* **Firefox**: `media.peerconnection.ice.obfuscate_host_addresses.blocklist`, a
  comma-separated list of hostnames, such as `www.example.net`. Set it in
  `about:config`, or with the `Preferences` enterprise policy.
* **Safari** has no such policy.

List only the texas2step site: each site listed can learn the internal address of every
computer that visits it. Browsers also reveal the address to a page they have given
camera or microphone access.

No browser tells a web page the computer's MAC address, and the server can't see it
past the first router. An IPv6 address formed by EUI-64 carries it, though: its last
64 bits are the MAC with `ff:fe` in the middle and one bit inverted. When this
computer's address, or the one the server sees, is such an address, the IPv6 part of the
box shows the MAC, and test runs save it. Current Windows, macOS, iOS, Android and
Linux use privacy or stable random addresses by default, so it mostly turns up for
printers, embedded devices and older systems. To find a computer from its local address
instead, look it up in the site's ARP or DHCP tables, then in the switch's MAC address
table or the wireless controller's client list.

The CLI needs none of this. `texas2step run` reports this computer's own address on its
connection to the server, as the connection's socket was bound: the address NAT turned
into the one the server sees, on whichever interface the system chose. On Linux it also
reports the MAC address of the NIC the kernel routes that connection out of
(`ip route get <server> from <address>`); traffic to the computer's own address goes
over loopback and has none. It prints both, `--save` stores them with the result, and
`--no-local` leaves them out. A server measuring for `/api/v1/measure/*` keeps its own
to itself.

## Known limitations

* The UDP test needs UDP from the client to the server. Set `rtc_port_min` and
  `rtc_port_max` to confine it to a range a firewall can open (see
  [Pinning the UDP ports](install.md#pinning-the-udp-ports)). Where UDP is blocked, the client
  falls back to TCP.
* A browser tab in the background is throttled, so keep the tab visible while testing.
* Results describe the path to *this* server. Pick a location close to the service you
  care about.
