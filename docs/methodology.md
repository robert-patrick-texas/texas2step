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

Each page keeps what it shows while another page is open, but only the page on screen
measures, so one page's traffic never skews another's figures. Going to another page
during a test stops it as Stop does, and its partial results stay.

While a test runs, and while Monitor or Diag is open, the page asks the browser to keep
the screen on (the Screen Wake Lock API). A phone whose screen goes off suspends the
page, and a locked computer slows its timers, so either would put gaps in the figures.
The **Keep screen on** switch, in Test options, Monitor's Options and on Diag, turns
this off for that browser. Browsers offer it only over HTTPS. They let the screen sleep
as usual when you switch to another app or tab, and some refuse it in battery saver.

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
number of probes that timed out (3 s). A probe the server answers with an error, such as
401 for want of an access code, isn't lost: it ends the test, which says so. The page
then asks for the code, and `/api/v1/measure/latency` answers `502` with the target's
refusal, as a refused download does.

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
* Payloads are served from memory, so the server never touches disk: each download
  starts with five founding documents as text, about 770 KB, then random bytes made once
  when the server starts; uploads are random throughout. See
  [What the tests send](#what-the-tests-send).

From the 100 ms timeline:

* **Stable rate** (the headline) is the mean rate after a **ramp-up window** of
  `clamp(20 % of T, 1 s, 40 % of T)`, which excludes TCP slow start and window growth.
  With the default 10 s test, the first 2 s are excluded.
* **Peak** is the 95th percentile of the 100 ms bins.
* **Per-stream rate** is each stream's bytes over the test, and **fairness** is
  Jain's index over streams: (Σx)² / (n·Σx²). 100 % means every flow got an equal share.

### Rate limit

A rate that holds, then steps down and stays down to the end of the transfer, is marked as
a **rate limit**. It is what a shaper or a policer does once a flow has used its
allowance, and what an ISP's speed boost does when it ends: the first seconds, or the first
few hundred megabytes, go faster than the line's lasting rate. The test reports the rate
before, when it fell, how much the transfer had moved by then, and the rate after; the
stable rate stays the mean after ramp-up, so it then blends the two.

The 100 ms bins after the ramp-up window are split in two, a higher level then a lower
one, where two levels fit them best (the split that leaves the two parts' means furthest
apart for their sizes). It is a rate limit when all of these hold:

* the higher part lasts at least 1 s and the lower part at least 2 s, to the end;
* the lower level is above zero and at most 75 % of the higher;
* the lower part's 90th percentile is below the higher part's median, so a rate that
  wobbles between two levels, as over Wi-Fi, isn't one;
* the last second is at most 75 % of the higher level too, so a dip that recovered isn't
  one.

It is found in each transfer: the download, one stream and the upload. A test that ends
before the allowance runs out sees only the faster rate and finds nothing, so a longer
test, 30 s from the Test page or `--seconds 30` from the command line, catches boosts a
10 s test ends inside. A transfer that starts with the allowance already spent, as one
stream often does right after the download, runs at the lower rate throughout and isn't
marked: compare it with the download's rate limit. What causes it, a shaper's queue or a
policer's dropped packets, isn't named: latency under load rises behind a shaper and not
behind a policer, but a shaper with modern queue management keeps it low as well.

### One stream

After the download, the test downloads over **one stream** for the same time, measured in
the same way: what a single file copy, backup or remote-desktop session gets, as each of
those uses one connection. It is shown beside the multi-stream rate, as a share of it.
It is skipped when the test itself uses one stream. Whether it runs otherwise is the
server's `default_one_stream`, unless the visitor chooses in Test options or the command
line with `--one-stream` or `--no-one-stream`. Why one stream can get less than several is in
[Quality and user experience](experience.md#one-stream).

The browser's own window can set one stream's rate. Over HTTP/2, Safari on an iPhone
(iOS 18) let one stream have only about 2 to 2.5 MB in flight, so at a 43 ms round trip
its one stream stopped near 330 Mbps, 61 % of the download, at rest and in Low Power
Mode alike, where Chrome on an Android phone and Edge on a desktop reached the full rate
on the same path. The server always had data in flight, so this isn't
[Device-bound](#when-the-device-is-the-limit): it is the browser's flow control, which a
faster device doesn't change.

### Streams and HTTP versions

Browsers open at most **six HTTP/1.1 connections** per server, so more than six streams
queue. Over **HTTP/2** all streams share **one TCP connection** as multiplexed streams;
"parallel streams" then measures one TCP flow. The negotiated protocol is shown in the
connection strip. To measure several TCP flows through a reverse proxy, serve
texas2step over HTTP/1.1 (see [HTTP/2 and parallel streams](install.md#http2-and-parallel-streams)),
or choose HTTP/1.1 for the test, below.

A page can't choose the HTTP version itself, but where the server has a name for each
version ([HTTP versions](configuration.md#http-versions)), Test options offer **HTTP:
Auto, HTTP/1.1 or HTTP/3**, in the address family the test goes over. The whole test
then runs on that name, which has an address of its own, so the browser opens a
connection of its own to it rather than reusing the page's:

* **HTTP/1.1:** each stream is a TCP connection of its own, as with many applications
  and some other speed tests, each with its own congestion window.
* **HTTP/3:** the streams share one QUIC connection over UDP, which firewalls and QoS
  often treat apart from TCP. A browser moves to HTTP/3 only after the server's
  `Alt-Svc` has offered it, so before the test the page pings the name until the browser
  answers over HTTP/3. If it hasn't within 10 s, the test doesn't start, and the page
  says that UDP may be blocked on the way or the browser doesn't use HTTP/3.

The result keeps the version chosen (`config.http`) and the one the browser used
(`client.protocol`); when they differ, as when an HTTP/1.1 name offers HTTP/2 too, the
page says so.

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
reduces this. A transfer the server's CPU held back says so: "Server CPU-bound" on the
Test page and a line under it from the command line, when the busiest worker serving it
ran at 90 % of a core or more ([Server CPU](cpu.md#server-cpu-bound-results)).

A busy device makes latency under load read high too: when the page itself runs late,
every probe's answer waits for it. The grade is then withheld, "No grade: device busy"
([When the device is the limit](#when-the-device-is-the-limit)).

## When the device is the limit

Everything a browser test does runs on the page's main thread: reading the downloads,
timing the probes, pacing the UDP test and drawing the charts. A busy or slow device
reads more slowly than the network delivers, and adds its own lateness to every
latency it times, so a good path can look slow. Each test says when that happened:
"Device-bound" on the transfer's tile, in the style of "Server CPU-bound", with the
figures under All measurements. The server's CPU comes first: when it set the rate, the
device didn't.

**The server's figure** (`device_window`, on by default). While it sends a download, the
server asks the kernel for the connection's TCP figures every quarter second, as `ss -ti`
does. Behind nginx the connection is nginx's, which the server finds by the addresses
and ports the package's snippet passes on (`X-Client-Port`, `X-Server-Addr`,
`X-Server-Port`, believed only from `trusted_proxies`); serving on its own, it asks for its
own. Any user may read these figures, and a lookup takes a few tens of microseconds. The
**share of the download the client held the server back** is the time the client's TCP
receive window limited what could be sent and, over HTTP/2, the time with nothing in
flight. A browser that can't keep up stops reading: over HTTP/1.1 its TCP receive window
fills, and over HTTP/2 it stops granting the server room on the stream, so the connection
goes idle. Over HTTP/1.1 an idle connection means the server's side had nothing to send,
so it doesn't count. When that share reaches `device_bound_percent` (10 % by default), the
download is Device-bound, unless the server's CPU set the rate, or the server had the
next bytes ready less than half the time, which would mean the server itself couldn't
keep up. The server can't say this of HTTP/3, which isn't TCP, or of uploads, where it is
the receiver. The same lookups give **the kernel's round-trip time** on the connection
while the download ran, and the lowest it saw, saved as `server_rtt_ms` and
`server_min_rtt_ms`.

**The page's figures** (`device_check`, on by default). A 50 ms timer runs through the
test, and each phase keeps how late it ran: the share of the phase it was late
(`device_busy`) and its 95th percentile (`device_lag_ms`). Chromium hands a download's body
to the page up to 2 MiB at a time, and reads that come full mean the bytes were waiting
for the page: `device_full_reads` is the share of the download read that way. Where the
server has no figure for a download, over HTTP/3 or from an older server, 93 % or more in
full reads makes it Device-bound. Other browsers' reads never come full, so they get no
verdict there. With `device_details` (on by default), Chromium's long tasks
(`device_long_ms`), the highest CPU pressure it reported (`client.pressure`, desktop
Chromium) and the browser's logical cores (`client.cores`) are saved too. Anyone with a
Result ID can open a saved result, so turn `device_details` off if those shouldn't be in it.

**The command line.** `texas2step run` and t2s time their own CPU for each transfer
(`device_cpu`, in cores), and say "client CPU-bound" when it reached 90 % of the cores the
transfer's streams could use: one for `texas2step run`, which runs on one core; for t2s,
the streams or the computer's cores, whichever is fewer. They also take the server's
figure, and say "device-bound" by it.

`device_bound` in a result says what showed it: `"window"` (the server), `"page"` (full
reads) or `"cpu"` (the command line).

**Uploads** are flagged far less often. The browser sends from its own network process, so
a slow page barely slows an upload, and the server, as the receiver, has no figure for
it. In calibration, headless Chromium's upload stopped near 200 Mbps, where Firefox
reached 283 to 473 Mbps and the command line 560, with nothing in the page to show it.
The page's lateness during the upload is kept under All measurements.

**Latency under load.** The page's lateness adds to each probe it times. When the median
lateness under load, beyond what it was at idle, explains at least half the rise in a
direction, and taking it away would change the grade, a grade of B or worse is withheld:
the tile shows the rise with "No grade: device busy". An A or A+ stands, as the
lateness could only have made it look worse. The server's round-trip time under load is
kept beside it, as a figure the page's lateness can't touch.

**Calls** keep their score. When the page ran late a quarter of the call test or more,
the call tile says so: a busy device can make the call read worse than the path is.

### How the thresholds were set

Two rounds of calibration, in October 2026, against two servers on HTTP/2.

Round 1 slowed a browser on purpose: headless Chromium and Firefox on a 4-core server,
testing a second server about 550 Mbps away and itself over loopback, with Chromium's CPU
throttling at 4×, 6× and 20×, programs keeping the computer's cores busy, and the page
itself kept 30, 60 and 90 % busy.

| Run | Download / one stream, Mbps | Held back by the client | Page's full reads |
|---|---|---|---|
| At rest | 559 / 559 | 0-2 % | 0-1 % |
| 4× throttling | 554 / 526 | 3-5 % | 57-77 % |
| 6× throttling | 548 / **327** | 8 / **32 %** | 67 / **98.5 %** |
| 20× throttling | **115 / 57** | **77 / 88 %** | **98.5 / 100 %** |
| Loopback, 4× | **1,040 / 603** | **47 / 73 %** | **99 / 93 %** |
| Cores busy (2, 4, 8 programs) | 545-552 | 0-3 % | 1-62 % |
| Page 90 % busy | 551 / **331** | 1 / **32 %** | 18 / **99.5 %** |
| Firefox, page 60 % busy | 554 / **447** | 2 / **18 %** | 0-1 % |
| Firefox, cores busy | 584 / **465** | 3 / **14 %** | 9-15 % |

Whenever the network set the rate, the client held the server back 0 to 5 % of the time;
whenever the device held the rate down, 14 to 97 %, roughly the share of the rate lost.
The page's timer, on the other hand, ran late under any load, even in runs whose rate
didn't move (at 4×, half the time), so it informs latency and calls, not the rate.

Round 2 took real devices over a home connection of about 570 Mbps down, Wi-Fi for the
phones: a Windows desktop in Edge, at rest and with every core kept busy; an iPhone in
Safari, at rest and in Low Power Mode; a Samsung phone in Chrome, at rest and in Power
saving. None was Device-bound, slowed or not: the client held the server back 0 to 1.1 %
of the time in all 24 downloads, the rates held, and the page's latency under load
matched the kernel's round-trip time within about 3 ms. Today's phones and computers keep
up with a link this fast; a faster link or an older device is where Device-bound shows.

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
3. The client sends numbered probes (`u32 sequence | f64 send time | random padding`) on
   a fixed schedule. They are 200 bytes, at least the size of a voice call's packets, so a path
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

The monitor sends packets continuously over the UDP channel (or the WebSocket fallback)
and draws each one's round trip. A packet that has not returned within one second is
counted as lost. Statistics cover the visible window (1, 5 or 15 minutes).

A **profile** sets what is sent. Anyone can switch it beside the Start button. Options
holds the exact sizes and rates, and the page's address carries them, so the service
desk can send a caller a link such as `/monitor?profile=voice`. The site picks the
profile a visitor starts with (`monitor_profile`), and can withhold the call profiles
(`monitor_voice`, `monitor_video`): the switch then shows only the ones offered, and none
at all when only Path is. A link or a saved choice for a withheld profile starts with
the nearest one offered.

| Profile | Sends | Each way, as the network carries it |
|---|---|---|
| **Path** | Small probes, 64 bytes, 5 to 50 a second (10 by default): a running record of the path's round trip, jitter and loss | about 25 kb/s at 10 a second |
| **Voice call** | A voice call's packets: 200 bytes every 20 ms | about 160 kb/s |
| **Video call** | The voice packets, and 30 video frames a second, each a burst of 1,100-byte packets (4 for 1 Mb/s of video) | about 1.5 Mb/s |

The call figures, as the page states them, count each packet's headers, encryption and a
VPN's wrapping. Options change them: 0.8 Mb/s for 0.5 Mb/s of video, 3.0 Mb/s for 2.5 Mb/s.
Comparing two servers sends the same to each, so twice as much in all.

Video packets are 1,100 bytes so that each travels as a single UDP packet: a browser
splits a data-channel message much larger than that, and one lost piece would lose the
whole. A frame goes out as a burst because a video call sends it that way, and bursts are
what shallow buffers and policers drop.

With a call profile, the server [stamps](#packet-loss-udp) each echo, and every
10 seconds of the call, counted from the start, is scored as the Test page scores its
packet-loss test, each direction on its own ([Call quality](#call-quality)):

* **On the loss-per-second strip**, each 10 seconds, once whole, gets a line beneath it
  in its rating's colour and its score on a label over its middle, where there's room.
  The seconds' own loss stays in view around and under them. Hovering a second gives its
  loss and the score of its 10 seconds, each way.
* **Call quality**, the median 10-second score in the window; **Worst 10s**, and how
  long ago; **Below good**, the share of the 10-second scores below good (4.0).
* **Frames hit** (Video call): video frames with a packet lost or more than 60 ms late,
  in the worse direction. Each is a freeze or a smear on the screen.

Over hours the two computers' clocks drift apart, by tens of milliseconds an hour, so each
10 seconds measures delays against its own fastest packet, where the drift is a fraction
of a millisecond. A server without stamps scores each 10 seconds on round trips, and over
the TCP fallback only the voice packets are sent.

**Stop** holds the chart and its figures where they stopped, to show or to take a
screenshot of. The window can still be changed, to look back over the last 15 minutes.
The 10 seconds cut short by Stop aren't scored. Going to another page stops the monitor
the same way, and the page says so when you come back to it.

**Time limits.** A site can stop Monitor a set time after each Start
(`page_monitor_max_runtime`), for pages left running unattended, and a server can end any
session after a set time (`session_max_runtime`). Either way Monitor stops as Stop does,
holds its chart and figures, says why, and starts again only once the page is reloaded.
With a site limit, a small **Runtime** box in the toolbar counts the seconds since Start
against it ("312s of 900s"); it holds at Stop, and the next Start counts from zero. A page
monitoring another server, picked at the top of the page or compared, keeps the
strictest of its own site's limit and each server's, and says whose stopped it
("Stopped after 15 min, Houston's limit.").

**Outages.** A server that hasn't answered for 3 seconds has gone quiet: when Wi-Fi or
mobile data drops while you move around, say, or when the server restarts. The line
beside the chart's title says so. Its probes keep their places and count as lost, so
the 10 seconds of a call through an outage score as a dropped call would. Monitor then
asks the server over HTTPS, with a small request, every 2 seconds, or every 10 once a
minute has passed. It also keeps one probe a second going on the old session, which
picks up again by itself after a short blip. Once the server answers over HTTPS but not
to its probes, as after a restart or a change of network, Monitor opens a new session.
It opens one at a time, and waits 5, then 10, then 30 seconds between tries that fail,
since each holds a port on the server for up to 30 seconds. Where only TCP gets through,
it carries on over the WebSocket and tries UDP again every 30 seconds. When the server
answers again, the line says how long it was gone and how many outages there have been
since Start.

A server closes the session of a browser that has gone away after about 30 seconds of
unanswered consent checks (RFC 7675), or after `rtc_session_timeout` (120 seconds)
without a probe.

Voice and Video calls count toward a [traffic limit](traffic.md#monitors-call-profiles).
When one is reached, the server holds the session to path probes and says so, and the
monitor carries on with the Path profile and shows the site's message.

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

The Diag page tries each way of reaching the server once a second while the page is
shown: HTTP/1.1, HTTP/2 and HTTP/3, WebSocket and UDP, over IPv4 and IPv6, as the server
offers them ([HTTP versions](configuration.md#http-versions) says which rows are
listed). The rows are grouped by family, each group headed by the server addresses its
rows reached. Going to another page pauses it and closes its connections. Its rows and
events stay, and the timelines show the gap.

* **HTTP/1.1, HTTP/2 and HTTP/3** are each a small request to `/api/v1/diag/http` on a
  name of the server's that serves that version in one family. The answer gives the
  client's address and port as the server saw them, the server's own address and port,
  the TLS version and cipher, the server kernel's TCP round trip for that connection
  (nginx's `$tcpinfo_rtt`, none over HTTP/3), and the server's clock. The browser
  reports the HTTP version it used (Resource Timing's `nextHopProtocol`), else the
  server does. Browsers keep connections open, so each attempt is a request on an
  existing connection, not a new one. An HTTP/1.1 or HTTP/2 row whose answer came over
  another version still counts it, and its detail says what's wrong with the name.
* **HTTP/3** counts only answers that came over HTTP/3, on UDP. A browser's first
  requests to the name go over TCP, and it moves to HTTP/3 once the `Alt-Svc` header in
  their answers says the server offers it: in Chrome and Firefox, within the first few
  seconds. Until then the row is grey, **Not over HTTP/3 yet**. Still on TCP after 10 s,
  it says **Only over TCP**: UDP is blocked on the way, or the browser doesn't use
  HTTP/3, as when an organization turns it off. Once answering, answers that come over
  TCP again count as lost (**Over TCP**, red): the browser fell back, and browsers wait
  minutes before trying HTTP/3 again after a failure, so recovery shows late.
* **WebSocket** opens its own TCP connection by an HTTP/1.1 upgrade and echoes a probe
  a second. A closed socket is opened again every 2 s.
* **UDP** is a WebRTC data channel held to one family (the server's candidates of the
  other family are dropped) that echoes a probe a second. A session that won't open is
  tried again every 10 s, since opening one takes seconds and uses one of the server's
  ports. The server reports the client's address and port on the chosen path (`addr` on
  the control channel), which is how the NAT and egress table compares UDP with TCP.
  The row shows the DTLS version and cipher where the browser reports them (its
  `transport` statistics).

An attempt not answered within 2 s (1.5 s for WebSocket and UDP) counts as missed. A
method that has answered before shows a miss as loss (red). One that hasn't answered
yet stays grey and keeps trying. One the server turns out not to offer, such as UDP in
a family it has no address in, is grey and isn't tried again.
A site can stop Diag a set time after it's shown (`page_diag_max_runtime`), and a server
can end its WebSocket or UDP session after a set time (`session_max_runtime`). Either way
Diag stops as it does for another page, keeps its rows and events, says why, and starts
again only once the page is reloaded. With a site limit, a small **Runtime** box beside
Keep screen on counts the seconds since Diag was last shown against it; coming back to
Diag, or picking another server, counts from zero. With another server picked, Diag keeps
the stricter of its own site's limit and that server's, and says whose stopped it.
One whose address asks for the access code, as a server's IPv4-only name can for a
visitor inside on IPv6 only, says **Restricted** (amber) and doesn't count as loss:
its HTTP rows are tried every 10 s until the code is in, and no WebSocket is opened there
meanwhile ([Inside on one address family](access.md#inside-on-one-address-family)).
The clock offset is the server's time minus the browser's at the middle of the round
trip, ± half the round trip.

### This computer's address

Browsers hide the computer's own address from web pages. WebRTC, the only place a page
could see it, gives a random name ending in `.local` instead. So by default the NAT and
egress box shows only the addresses the server sees. Where the browser reveals it, the
box adds **This computer**, the address of the interface the UDP path leaves by, and a
**NAT** row comparing it with the address the server sees. Browser test runs save it
with their results (`client.local_ip`).

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

## What the tests send

What the tests put in their packets. On the wire none of it shows: TLS encrypts every
HTTPS payload, and DTLS every UDP one, so a capture sees ciphertext either way.

**Downloads** (`GET /api/v1/down`) start with five founding documents, as UTF-8 Markdown
text, about 770 KB together, in the order they were written:

1. The Declaration of Independence, 1776 (the National Archives' transcription)
2. The Constitution of the United States, 1787, with its 27 amendments (the National
   Archives' transcriptions)
3. The Texas Declaration of Independence, March 2, 1836
4. The Constitution of the Republic of Texas, March 17, 1836
5. The Texas Constitution of 1876, as amended (the Texas Legislative Council's text)

They are public domain, and ship with the package in `src/texas2step/documents/`, each
naming its source at its top. Random bytes follow to the end: 8 MiB from the operating
system's random source, made when each worker starts and sent in turn, round and round.
Every response starts with the documents: each stream of a test, and each request a
stream makes. A download shorter than them is the start of their text. With
`download_documents = false`, downloads are random from the first byte.

Text compresses and random bytes don't, but nothing between the server and the client
compresses a test: nginx's gzip is off for texas2step, and TLS and QUIC don't compress.
Only a device that decrypts and compresses traffic, such as a WAN optimizer doing TLS
interception, would carry the text faster. Even then, a test's 4 streams carry 3.1 MB of
text in their first moments, inside the ramp-up window the stable rate leaves out on any
path faster than about 12 Mb/s. A site behind such a device can turn the documents off.

**Uploads** are random. The browser fills 1 MiB from `crypto.getRandomValues` once and
sends it over and over, each request cut to its size; the command line sends the 8 MiB
pool in turn. The server reads and discards them.

**UDP probes** have a 12-byte header, little-endian: the probe's sequence number (`u32`)
and the sender's clock in milliseconds (`f64`). Random bytes fill the rest, new for each
probe. The server echoes each one as it came, except that with stamps on, bytes 12 to 23
of the echo become its own clock when the probe arrived (`f64`) and how many probes it
had received (`u32`).

| Probes | Bytes each |
|---|---|
| Monitor's Path profile; Diag's UDP rows | 64; Path also offers 200 and 1,100 |
| Packet loss on the Test page; the command line's `loss` | 200 |
| Monitor's voice packets | 100, 200 or 300 |
| Monitor's video packets, in a burst for each frame | 1,100 |

A DTLS record and an SCTP chunk carry each probe, so its UDP datagram is 65 bytes longer
with the cipher browsers and the server agree on (DTLS 1.2, AES-128-GCM): 129 bytes for
a 64-byte probe, 1,165 for a video packet, under what a browser sends in one packet.

**WebSocket probes** (latency under load, Diag's WebSocket rows, and Monitor where UDP
can't get through) are the 12-byte header alone, echoed as they came.

**Everything else is small**: `/ping` answers `{"pong":true}`, and the page's other
requests and answers are JSON. Each request carries its page's session and name as
`?session=` and `?page=` (see [Active clients](clients.md)), and each of a test's
downloads and uploads the test's `?test=`.

## Known limitations

* The UDP test needs UDP from the client to the server. Set `rtc_port_min` and
  `rtc_port_max` to confine it to a range a firewall can open (see
  [Pinning the UDP ports](install.md#pinning-the-udp-ports)). Where UDP is blocked, the client
  falls back to TCP.
* A browser tab in the background is throttled, so keep the tab visible while testing.
* Results describe the path to *this* server. Pick a location close to the service you
  care about.
