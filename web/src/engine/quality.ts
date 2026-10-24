// What a test's figures mean for calls and for responsiveness. Mirrors
// src/texas2step/quality.py; keep the definitions identical. docs/methodology.md explains them.
import type { CallQuality, CallRating, LossResult, Responsiveness, TestResult } from "../types";

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

/**
 * A call's quality over the path the packet-loss test measured, or null without one.
 *
 * A probe is impaired when lost, or when it came back more than the jitter buffer behind
 * the fastest. The shares of lost and late probes count every probe; the bursts count the
 * probes a call's 20 ms packets would be, such as every other one at 10 ms.
 */
export function callQuality(loss: LossResult | undefined): CallQuality | null {
  const rtts = loss?.packets ?? [];
  const times = rtts.filter((t): t is number => t !== null);
  if (!loss || !times.length || loss.error) return null;
  const fastest = Math.min(...times);
  const late = rtts.map((t) => t !== null && t - fastest > BUFFER_MS);
  const impaired = rtts.map((t, i) => t === null || late[i]!);
  const step = Math.max(1, round(CALL_MS / loss.interval_ms));
  const view = impaired.filter((_, i) => i % step === 0);
  const lateCount = late.filter(Boolean).length;
  const pct = (impaired.filter(Boolean).length / impaired.length) * 100;
  const ratio = burstRatio(view);
  const delay = loss.rtt.median! / 2 + CALL_DELAY_MS;
  const r = rFactor(delay, pct, ratio ?? 1);
  const score = mos(r);
  const gap = longestRun(view) * loss.interval_ms * step;
  return {
    basis: "round trip",
    packets: view.length,
    interval_ms: loss.interval_ms * step,
    late: lateCount,
    late_pct: (lateCount / rtts.length) * 100,
    impaired_pct: pct,
    burst_ratio: ratio === null ? null : round(ratio, 2),
    longest_gap_ms: gap,
    delay_ms: delay,
    r: round(r, 1),
    mos: round(score, 2),
    mos_rating: rating(score),
    rating: heard(rating(score), gap),
  };
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

/** Round trips per minute under load, each direction's and the lower of the two. */
export function responsiveness(r: TestResult): Responsiveness | null {
  const download = rpm(r.download?.loaded_latency?.samples);
  const upload = rpm(r.upload?.loaded_latency?.samples);
  const known = [download, upload].filter((v): v is number => v !== null);
  if (!known.length) return null;
  return { rpm: Math.min(...known), download_rpm: download, upload_rpm: upload, basis: "latency under load" };
}
