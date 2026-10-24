# Quality and user experience

Throughput says how much a connection carries. Whether it is good to use depends on more
than that: whether a caller is heard clearly, whether a click in a remote desktop answers
at once, whether a page opens without a pause. texas2step turns its measurements into
figures for these: a **call-quality score**, **responsiveness** and **one stream's**
rate, beside the [bufferbloat grade and readiness](methodology.md#readiness).

This page explains what each figure means to the people using the connection, where
texas2step departs from the standard models so that a figure matches what people
actually notice, and the sources behind them. How each is measured is in
[Methodology](methodology.md).

## Calls

The **Call quality** figure is a MOS (mean opinion score, 1 to 5): how a G.711 voice call
over the same path would sound, scored with the ITU-T **E-model** from the packet-loss
test's probes. A call has two directions, and either can be the bad one, so each is
scored on its own: **what you hear**, and **what the other end hears**. The tile shows
the worse. The formulas are in
[Methodology: Call quality](methodology.md#call-quality).

### Ratings

| MOS | Rating | What callers notice |
|---|---|---|
| 4.3 – 5.0 | Excellent | Clear, like a landline call. The benchmark for business voice. |
| 4.0 – 4.2 | Good | Minor imperfections that don't disrupt the call. |
| 3.6 – 3.9 | Fair | Noticeable impairments or delay; still understandable, but some users complain. |
| 3.1 – 3.5 | Poor | Choppy or clipped audio; frequent complaints, and tiring calls. |
| 1.0 – 3.0 | Bad | Dropouts, long delay or unintelligible audio; calls are largely unusable. |

The bands sit close to the ITU's guide to user satisfaction for the E-model's rating *R*
(ITU-T G.107 Annex B): *R* 90 (MOS 4.3) "very satisfied", 80 (4.0) "satisfied", 70 (3.6)
"some users dissatisfied" and 60 (3.1) "many users dissatisfied". ITU-T G.109 names the
same bands best, high, medium and low quality.
A perfect path scores 4.4, the most a G.711 call can.

**What to expect.** Calls inside an enterprise network should be **excellent**. Business
calls over the Internet should be at least **good**, 4.0.

### Adjustments for what callers hear

The E-model is a planning tool: its full title is "a computational model for use in
transmission planning", and it rates the average condition of a connection. A caller
hears moments. texas2step applies these adjustments so the rating matches what a caller
would say:

1. **A late packet is a lost packet.** A call plays its packets out of a jitter buffer,
   60 ms here. A probe that arrived more than 60 ms behind the fastest would have missed
   its turn, so it counts as lost, as it would in a call. Lateness is measured in each
   direction, as each end's buffer sees it.
2. **Bursts are counted in a call's own packets.** A call sends a packet every 20 ms. A
   100 ms outage costs it 5 packets, while the test's probes, 10 ms apart, lose 10. The
   burst pattern is taken from the probes at a call's spacing (every other probe at the
   default 10 ms), so a burst is as long as a call would find it.
3. **Rates from every probe.** The shares of lost and late packets count all the probes,
   which makes them steadier than counting a call's spacing alone.
4. **A gap a caller hears lowers the rating.** The **longest gap** is the longest run of
   lost or late packets, in milliseconds of the call:
   * longer than **60 ms**: rated at most **good**;
   * **200 ms** or more: rated at most **fair**.

   The score itself stays the E-model's, so it can be compared with other tools; only
   the rating takes the gap into account. Results keep both: `mos_rating` is the score's
   rating, `rating` the one shown.
5. **Read as shown.** A rating reads the score to one decimal, as the page shows it: 4.25
   shows as 4.3, and is excellent.
6. **Readiness follows the rating.** The [Video calls readiness](methodology.md#readiness)
   grade needs an excellent call for great, good for good and fair for fair, so a gap
   lowers it too.

### Why a gap gets its own rule

At the loss rates a working network has, the E-model barely charges for bursts. Its loss
impairment is 95 *Ppl* / (*Ppl* / *BurstR* + 25.1), for *Ppl* % of packets lost and a
burst ratio *BurstR* (1 for random loss, more for bursts). With G.711's 25.1, the burst
term can raise the impairment by at most 4 % at 1 % loss, 20 % at 5 %, and 80 % only at
20 %. The scores, for a 20 ms round trip:

| Loss | Random | Burst ratio 2 | Burst ratio 5 | Any bursts |
|---|---|---|---|---|
| 1 % | 4.27 | 4.27 | 4.27 | 4.27 |
| 2 % | 4.17 | 4.16 | 4.15 | 4.15 |
| 5 % | 3.83 | 3.77 | 3.73 | 3.69 |
| 10 % | 3.30 | 3.07 | 2.89 | 2.74 |
| 20 % | 2.52 | 1.92 | 1.44 | 1.13 |

A caller hears the difference. Packet-loss concealment covers a missing packet or two by
stretching the speech before it; G.711's standard concealment fades the sound by 20 %
every 10 ms and is silent 60 ms into a gap. A syllable lasts about 200 ms. So 1 % of
packets lost one at a time is mostly hidden, while the same 1 % lost as a single 140 ms
gap is silence in the middle of a word: "sorry, you cut out". The ITU itself recommends
using the burst ratio with caution for bursty loss until it is further validated
(ITU-T G.113).

The longer the sample, the more the score averages a gap away. In the Test page's 500
probes, a gap long enough to hear is already 1.4 % of them, and the score itself usually
drops to good. In 1,000 probes the same gap is half that share, and an 80 ms gap leaves
the score at 4.3, excellent; the rule rates it good.

### Limits

* **Older servers.** A server from before stamped echoes gives round trips only. A delay
  can't then be placed in one direction, and a probe late on the round trip may have been
  on time each way, so the late count errs toward late, and the score covers both
  directions together.
* **A snapshot.** The Test page's score covers the few seconds of its packet-loss test.
  Trouble that comes and goes, such as a microwave oven near the Wi-Fi or someone walking
  away from the access point, needs a longer look: Monitor's Voice call and Video call
  profiles score every 10 seconds for as long as they run
  ([Live monitor](methodology.md#live-monitor)).
* **Over TCP.** Where UDP is blocked, the score describes a call relayed over TCP, as
  call apps fall back to then.

### Calls over time

Monitor's call profiles send a call's packets for as long as the page runs, and score each
10 seconds as a call, so a problem that comes and goes shows in the scores of the 10
seconds it spoiled.
**Below good** is the share of the time a caller would have noticed something wrong
(below 4.0), and **Worst 10s** the moment they'd remember. A video call also counts
**frames hit**: video frames with a packet lost or late, each one a freeze or a smear on
the screen. A video call keeps working with a few frames hit; its voice, scored beside
it, is what decides whether people can talk.

## Responsiveness

**Responsiveness** is how many round trips a connection manages per minute while it is
busy, in RPM. Every click in a remote desktop, every keystroke over SSH and every request
a web page makes waits for a round trip, so this is what makes a connection feel quick
or sluggish when someone else is downloading.

| RPM | Each round trip |
|---|---|
| 6,000 | 10 ms |
| 1,000 | 60 ms |
| 300 | 200 ms |

The figure is the IETF's measure of
[responsiveness under working conditions](methodology.md#responsiveness), which macOS's
`networkQuality` also reports. As the draft does, texas2step times two kinds of round
trip while each transfer runs, since a busy connection hurts both:

* **New connections.** Every site a browser visits, and many a web app opens, starts with
  a TCP connect, a TLS handshake and a first request. Under load each waits in the same
  queues as the transfer, so a page that opens in a moment on an idle line can stall on a
  busy one. The page also shows what a new connection costs idle, for comparison.
* **Round trips on the busy connection.** A remote desktop or a video call keeps one
  connection open and sends on it while the rest of the traffic flows.

Each is the mean of the fastest 95 %, and a direction's responsiveness is the mean of the
two. The page shows the **slower direction**: a connection is as responsive as its worse
direction.

Two adjustments keep the figure honest:

1. **A browser times a new connection as one.** It can't see the TCP connect apart from
   the TLS handshake and the request, so it divides the time by the steps a connection
   takes (three with TLS, two without). The command line times each step.
2. **Over HTTP/1.1, beside the load rather than on it.** Only HTTP/2 and HTTP/3 let a
   request share a busy connection. Over HTTP/1.1 the round trips come from probes on a
   connection of their own beside the transfer, which can read somewhat better than
   round trips on the transfer's own connection.

## One stream

The download and upload use several streams at once, to find what the path can carry.
Much of what people do uses one connection at a time: copying a file, a backup, a
remote-desktop session. **One stream** downloads over a single connection for the same
time, and shows its rate as a share of the several streams' rate. It explains the common
"the speed test is fine, but my copy is slow".

One TCP connection can get much less than the path carries:

* **Distance.** A connection has at most a window of data in flight per round trip, so
  its rate is at most the window divided by the round trip. Far servers, and computers or
  middleboxes with small windows, cap a single stream.
* **Loss.** TCP slows down at each loss, so even a little loss caps a single stream's
  rate, and the more so the longer the round trip (Mathis et al.). Several streams share
  the slowdown.
* **Per-connection limits.** Some shapers and firewalls limit each connection, so one gets
  a fraction while several fill the path.

A share near 100 % means a single stream fills the path. Well below it, one of these is
likely, and a file copy will be slow even though the speed test isn't.

Two things can lower the share without the network being the cause:

* **HTTP/2 or HTTP/3.** A browser sends all its requests to one server over one
  connection, so on a server published over HTTP/2 the several streams share one
  connection too, and the Test page says so under the throughput chart. One stream then
  compares one connection with one connection: the network's limits on a single
  connection weigh on both, and what's left between them is the protocol's flow control,
  which gives each stream a window of its own, and the server's work. A server published
  over HTTP/1.1 gives each stream a connection of its own, which is what the comparison is
  for ([HTTP/2 and parallel streams](install.md#http2-and-parallel-streams)). The command
  line always uses HTTP/1.1.
* **The server, at multi-gigabit speeds.** One request is served by one of texas2step's
  worker processes, so on a short, fast path a single stream can be held back by that
  process's share of the CPU rather than by the network. On a fast LAN, a share well
  below 100 % says more about the server than about the path.

## Sources

* ITU-T [G.107](https://www.itu.int/rec/T-REC-G.107), The E-model: a computational model
  for use in transmission planning. The rating *R*, the loss impairment with the burst
  ratio, and *R* to MOS and user satisfaction (Annex B).
* ITU-T [G.109](https://www.itu.int/rec/T-REC-G.109), Definition of categories of speech
  transmission quality: the *R* bands of best, high, medium and low quality.
* ITU-T [G.113](https://www.itu.int/rec/T-REC-G.113), Transmission impairments due to
  speech processing. Appendix I gives G.711 with packet-loss concealment an *Ie* of 0 and
  a *Bpl* of 25.1, and advises caution with the burst ratio for bursty loss.
* ITU-T [G.711 Appendix I](https://www.itu.int/rec/T-REC-G.711-199909-I!AppI), A high
  quality low-complexity algorithm for packet loss concealment with G.711: the fade to
  silence over 60 ms.
* The ITU's [E-model calculator](https://www.itu.int/ITU-T/studygroups/com12/emodelv1/calculnb.php)
  and its definition of the burst ratio, the average observed burst divided by the
  average burst random loss would give.
* R. G. Cole and J. H. Rosenbluth,
  [Voice over IP performance monitoring](https://doi.org/10.1145/505666.505669), ACM
  SIGCOMM Computer Communication Review 31(2), 2001: the fit of G.107's delay impairment
  that texas2step uses.
* IETF [draft-ietf-ippm-responsiveness](https://datatracker.ietf.org/doc/draft-ietf-ippm-responsiveness/),
  Responsiveness under Working Conditions: round trips per minute, new connections and
  the loaded connection, and the trimmed mean.
* M. Mathis, J. Semke, J. Mahdavi and T. Ott,
  [The macroscopic behavior of the TCP congestion avoidance algorithm](https://doi.org/10.1145/263932.264023),
  ACM SIGCOMM Computer Communication Review 27(3), 1997: how loss and the round trip cap
  one TCP connection's rate.
