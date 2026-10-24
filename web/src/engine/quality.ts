// What a test's figures mean for calls and for responsiveness. Mirrors
// src/texas2step/quality.py; keep the definitions identical. docs/methodology.md explains them.
import type { CallQuality, CallRating, CallSide, LossResult, NewConnections, Responsiveness, ResponsivenessSide, TestResult, ThroughputResult } from "../types";

const CALL_MS = 20; // a call's packet interval
const BUFFER_MS = 60; // a call's jitter buffer: a packet further behind the fastest is too late
// What a call adds to the network's one-way delay: filling a packet, then the buffer.
const CALL_DELAY_MS = CALL_MS + BUFFER_MS;
// The E-model (ITU-T G.107) for G.711 with packet-loss concealment: Ie 0 and Bpl 25.1
// (ITU-T G.113 Appendix I), and R 93.2 with every other factor at its default.
const BPL = 25.1;
const R_DEFAULT = 93.2;
// Lowest MOS of each rating, read to one decimal as it is shown.
const RATINGS: [number, CallRating][] = [
  [4.3, "excellent"],
  [4.0, "good"],
  [3.6, "fair"],
  [3.1, "poor"],
];
const ORDER: CallRating[] = ["bad", "poor", "fair", "good", "excellent"];
// The E-model barely charges a gap at low loss, yet a caller hears one. G.711's concealment
// fades to silence 60 ms into a gap, and a syllable lasts about 200 ms.
const CONCEALED_MS = 60; // a longer gap is heard: at best "good"
const SYLLABLE_MS = 200; // a gap this long takes a syllable: at best "fair"
const TRIM = 0.05; // responsiveness leaves out the slowest 5 % of round trips

function round(value: number, digits = 0): number {
  const scale = 10 ** digits;
  return Math.floor(value * scale + 0.5) / scale;
}

/** Mean opinion score, 1 to 4.5, from the E-model's rating R (ITU-T G.107 Annex B). */
export function mos(r: number): number {
  if (r <= 0) return 1;
  if (r >= 100) return 4.5;
  return 1 + 0.035 * r + 7e-6 * r * (r - 60) * (100 - r);
}

export function rating(score: number): CallRating {
  const shown = round(score, 1);
  return RATINGS.find(([floor]) => shown >= floor)?.[1] ?? "bad";
}

/** The rating as a caller would hear it: the E-model's, at most what the longest gap allows. */
export function heard(scoreRating: CallRating, longestGapMs: number): CallRating {
  let best: CallRating;
  if (longestGapMs >= SYLLABLE_MS) best = "fair";
  else if (longestGapMs > CONCEALED_MS) best = "good";
  else return scoreRating;
  return ORDER.indexOf(scoreRating) < ORDER.indexOf(best) ? scoreRating : best;
}

/** The E-model's R for a call with this mouth-to-ear delay, with this share of its packets
 * lost or too late, in bursts this much longer than random loss would give. */
export function rFactor(delayMs: number, impairedPct: number, burstRatio: number): number {
  // The delay impairment, as Cole and Rosenbluth fitted it to G.107's.
  const delay = 0.024 * delayMs + 0.11 * Math.max(0, delayMs - 177.3);
  const loss = (95 * impairedPct) / (impairedPct / burstRatio + BPL);
  return R_DEFAULT - delay - loss;
}

/** How much longer the runs of impaired packets are than random loss would make them (the
 * ITU's BurstR): 1 for random loss, more for bursts. Null without any. */
export function burstRatio(impaired: readonly boolean[]): number | null {
  const count = impaired.filter(Boolean).length;
  if (!count) return null;
  const runs = impaired.filter((bad, i) => bad && (i === 0 || !impaired[i - 1])).length;
  // Random loss with probability p gives runs 1 / (1 - p) long on average.
  return (count / runs) * (1 - count / impaired.length);
}

export function longestRun(impaired: readonly boolean[]): number {
  let longest = 0;
  let run = 0;
  for (const bad of impaired) {
    run = bad ? run + 1 : 0;
    longest = Math.max(longest, run);
  }
  return longest;
}

export interface OneWay {
  up_ms: (number | null)[];
  down_ms: (number | null)[];
  lost_upstream: number[];
}

/**
 * Each direction's delays and losses, from the server's stamps.
 *
 * For each probe: when this client sent it and, for those echoed, when the server got it
 * (by its own clock), how many it had got by then, and when the echo came back. `total` is
 * how many it got in all, null if unknown. A delay is given above the direction's fastest,
 * which takes out the difference between the two clocks.
 *
 * Between two echoes, the server's counts tell how many of the probes in between reached
 * it: the rest were lost on the way there (`lost_upstream`), and the others on the way
 * back. Which of them went which way can't be told; those lost upstream are taken to be
 * the first.
 */
export function oneWay(
  sent: readonly number[],
  server: readonly (number | null)[],
  back: readonly (number | null)[],
  received: readonly (number | null)[],
  total: number | null,
): OneWay | null {
  const n = sent.length;
  const echoed: number[] = [];
  for (let i = 0; i < n; i++) if (server[i] !== null && back[i] !== null) echoed.push(i);
  if (!echoed.length) return null;
  const up = new Map(echoed.map((i) => [i, server[i]! - sent[i]!]));
  const down = new Map(echoed.map((i) => [i, back[i]! - server[i]!]));
  const upMin = Math.min(...up.values());
  const downMin = Math.min(...down.values());
  const lostUpstream: number[] = [];
  let prev = -1;
  let had = 0;
  for (const i of [...echoed, n]) {
    // n: past the last probe, where the server's total stands
    const count = i < n ? (received[i] ?? 0) : (total ?? had) + 1;
    const missing = i - prev - 1;
    const reached = Math.min(Math.max(count - had - 1, 0), missing);
    for (let j = prev + 1; j < prev + 1 + missing - reached; j++) lostUpstream.push(j);
    prev = i;
    had = Math.max(had, count);
  }
  return {
    up_ms: Array.from({ length: n }, (_, i) => (up.has(i) ? round(up.get(i)! - upMin, 3) : null)),
    down_ms: Array.from({ length: n }, (_, i) => (down.has(i) ? round(down.get(i)! - downMin, 3) : null)),
    lost_upstream: lostUpstream,
  };
}

/** A call over these probes, each lost or late or neither. Its bursts count the probes whose
 * sequence numbers are a call's packets: every `step`-th. */
function judge(seqs: readonly number[], lost: readonly boolean[], late: readonly boolean[], step: number, intervalMs: number, delayMs: number): CallSide {
  const impaired = lost.map((bad, i) => bad || late[i]!);
  const view = impaired.filter((_, i) => seqs[i]! % step === 0);
  const lateCount = late.filter(Boolean).length;
  const pct = (impaired.filter(Boolean).length / impaired.length) * 100;
  const ratio = burstRatio(view);
  const r = rFactor(delayMs, pct, ratio ?? 1);
  const score = mos(r);
  const gap = longestRun(view) * intervalMs * step;
  return {
    packets: view.length,
    late: lateCount,
    late_pct: (lateCount / late.length) * 100,
    impaired_pct: pct,
    burst_ratio: ratio === null ? null : round(ratio, 2),
    longest_gap_ms: gap,
    r: round(r, 1),
    mos: round(score, 2),
    mos_rating: rating(score),
    rating: heard(rating(score), gap),
  };
}

/**
 * A call's quality over the path the packet-loss test measured, or null without one.
 *
 * A probe is impaired when lost, or when it arrived more than the jitter buffer behind the
 * fastest. The shares of lost and late probes count every probe; the bursts count the
 * probes a call's 20 ms packets would be, such as every other one at 10 ms.
 *
 * With the server's stamps (`up_ms`, `down_ms` and `lost_upstream`, from oneWay), each
 * direction is a call of its own: `down` what you hear, `up` what the other end hears,
 * and the figures at the top are the worse one's. Without them, a probe is late when its
 * round trip was more than the buffer behind the fastest.
 */
export function callQuality(loss: LossResult | undefined): CallQuality | null {
  if (!loss || loss.error) return null;
  const rtts = loss.packets ?? [];
  if (!rtts.some((t) => t !== null)) return null;
  const n = rtts.length;
  const all = Array.from({ length: n }, (_, i) => i);
  const step = Math.max(1, round(CALL_MS / loss.interval_ms));
  const delay = loss.rtt.median! / 2 + CALL_DELAY_MS;
  const common = { interval_ms: loss.interval_ms * step, delay_ms: delay };
  const judged = (seqs: number[], lost: boolean[], late: boolean[]) => judge(seqs, lost, late, step, loss.interval_ms, delay);
  if (loss.up_ms && loss.down_ms) {
    const upstream = new Set(loss.lost_upstream ?? []);
    const { up_ms: upMs, down_ms: downMs } = loss;
    const reached = all.filter((i) => !upstream.has(i));
    const up = judged(
      all,
      all.map((i) => upstream.has(i)),
      upMs.map((t) => (t ?? 0) > BUFFER_MS),
    );
    const down = judged(
      reached,
      reached.map((i) => rtts[i] === null),
      reached.map((i) => (downMs[i] ?? 0) > BUFFER_MS),
    );
    // The worse direction: the lower rating, then the lower score; what you hear first.
    const rank = (c: CallSide) => [ORDER.indexOf(c.rating), c.mos] as const;
    const [ru, rd] = [rank(up), rank(down)];
    const worse = ru[0] < rd[0] || (ru[0] === rd[0] && ru[1] < rd[1]) ? "up" : "down";
    return { basis: "each way", ...common, ...(worse === "up" ? up : down), worse, down, up };
  }
  const fastest = Math.min(...rtts.filter((t): t is number => t !== null));
  const late = rtts.map((t) => t !== null && t - fastest > BUFFER_MS);
  return { basis: "round trip", ...common, ...judged(all, rtts.map((t) => t === null), late) };
}

/** The mean of the fastest 95 %: the slowest 5 % of round trips are left out. */
export function trimmedMean(values: readonly number[]): number {
  const ordered = [...values].sort((a, b) => a - b);
  const kept = ordered.slice(0, ordered.length - Math.floor(ordered.length * TRIM));
  return kept.reduce((a, b) => a + b, 0) / kept.length;
}

/** Round trips per minute from round-trip times in milliseconds. */
export function rpm(samples: readonly number[] | undefined): number | null {
  if (!samples?.length) return null;
  const mean = trimmedMean(samples);
  return mean > 0 ? round(60000 / mean) : null;
}

/** A new connection's round trip: the mean of its steps' (TCP connect, TLS handshake and
 * first request), each the mean of its fastest 95 %. Timed as one, its open time is split
 * evenly among the steps it took. */
function foreignMs(c: NewConnections): number {
  if (c.tcp_ms?.length && c.http_ms?.length) {
    const steps = [c.tcp_ms, ...(c.tls_ms?.length ? [c.tls_ms] : []), c.http_ms].map(trimmedMean);
    return steps.reduce((a, b) => a + b, 0) / steps.length;
  }
  return trimmedMean(c.setup_ms) / (c.tls ? 3 : 2);
}

/** One direction by the draft's method: the mean of the new connections' RPM and the
 * loaded connection's. Null without new connections. */
function side(t: ThroughputResult | undefined): ResponsivenessSide | null {
  const c = t?.connections;
  if (!t || !c?.setup_ms.length || foreignMs(c) <= 0) return null;
  const foreign = round(60000 / foreignMs(c));
  // On HTTP/2 or 3, requests share the transfer's connection; otherwise the probes beside it.
  const onLoad = !!t.loaded_probes?.length;
  const samples = onLoad ? t.loaded_probes : t.loaded_latency?.samples;
  const loadedMs = samples?.length ? trimmedMean(samples) : null;
  const loaded = loadedMs ? round(60000 / loadedMs) : null;
  return {
    rpm: loaded === null ? foreign : round((foreign + loaded) / 2),
    foreign_rpm: foreign,
    loaded_rpm: loaded,
    setup_ms: round(trimmedMean(c.setup_ms), 3),
    loaded_ms: loadedMs === null ? null : round(loadedMs, 3),
    loaded_on: onLoad ? "loaded connection" : "own connection",
  };
}

/**
 * Round trips per minute under load, each direction's and the lower of the two. With new
 * connections timed during the transfers, by the IETF draft's method; otherwise from the
 * latency-under-load probes alone.
 */
export function responsiveness(r: TestResult): Responsiveness | null {
  const sides = { download: side(r.download), upload: side(r.upload) };
  if (sides.download || sides.upload) {
    const known = [sides.download?.rpm, sides.upload?.rpm].filter((v): v is number => v !== undefined);
    const idle = r.latency?.setup_ms;
    return {
      rpm: Math.min(...known),
      download_rpm: sides.download?.rpm ?? null,
      upload_rpm: sides.upload?.rpm ?? null,
      basis: "ippm",
      ...(sides.download ? { download: sides.download } : {}),
      ...(sides.upload ? { upload: sides.upload } : {}),
      ...(idle !== undefined ? { idle_setup_ms: round(idle, 3) } : {}),
    };
  }
  const download = rpm(r.download?.loaded_latency?.samples);
  const upload = rpm(r.upload?.loaded_latency?.samples);
  const known = [download, upload].filter((v): v is number => v !== null);
  if (!known.length) return null;
  return { rpm: Math.min(...known), download_rpm: download, upload_rpm: upload, basis: "latency under load" };
}
