// Mirrors src/texas2step/stats.py; keep the definitions identical.
import type { LatencyStats, RateLimit } from "../types";

export function percentile(values: readonly number[], q: number): number {
  if (!values.length) return NaN;
  const sorted = [...values].sort((a, b) => a - b);
  const pos = ((sorted.length - 1) * q) / 100;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  const a = sorted[lo]!;
  return lo === hi ? a : a + (sorted[hi]! - a) * (pos - lo);
}

/** Mean absolute difference between consecutive samples (RFC 3550 style, unsmoothed). */
export function jitter(values: readonly number[]): number {
  if (values.length < 2) return 0;
  let sum = 0;
  for (let i = 1; i < values.length; i++) sum += Math.abs(values[i]! - values[i - 1]!);
  return sum / (values.length - 1);
}

export function summarize(samples: readonly number[], lost = 0, keepSamples = true): LatencyStats {
  const n = samples.length;
  const out: LatencyStats = {
    count: n,
    lost,
    min: null,
    max: null,
    mean: null,
    median: null,
    p25: null,
    p75: null,
    p90: null,
    stdev: null,
    jitter: null,
  };
  if (n) {
    const mean = samples.reduce((a, b) => a + b, 0) / n;
    const variance = samples.reduce((a, b) => a + (b - mean) ** 2, 0) / n;
    Object.assign(out, {
      min: Math.min(...samples),
      max: Math.max(...samples),
      mean,
      median: percentile(samples, 50),
      p25: percentile(samples, 25),
      p75: percentile(samples, 75),
      p90: percentile(samples, 90),
      stdev: Math.sqrt(variance),
      jitter: jitter(samples),
    });
  }
  if (keepSamples) out.samples = samples.map((s) => Math.round(s * 1000) / 1000);
  return out;
}

/** Ramp-up excluded from the stable throughput figure (TCP slow start, window growth). */
export function warmupSeconds(duration: number): number {
  return Math.min(Math.max(0.2 * duration, 1), 0.4 * duration);
}

export function summarizeThroughput(binBytes: readonly number[], binMs: number, duration: number) {
  const scale = 8 / (binMs * 1000);
  const timeline = binBytes.map((b) => b * scale);
  const warm = warmupSeconds(duration);
  const first = Math.min(timeline.length, Math.ceil((warm * 1000) / binMs));
  const steady = timeline.slice(first).length ? timeline.slice(first) : timeline;
  return {
    mbps: steady.length ? steady.reduce((a, b) => a + b, 0) / steady.length : 0,
    peak_mbps: timeline.length ? percentile(timeline, 95) : 0,
    bytes: binBytes.reduce((a, b) => a + b, 0),
    duration_s: duration,
    warmup_s: warm,
    bin_ms: binMs,
    timeline: timeline.map((v) => Math.round(v * 1000) / 1000),
    rate_limit: rateLimit(timeline, binBytes, binMs, first),
  };
}

// A rate limit: a rate that holds, then steps down and stays down to the end, as a shaper,
// a policer or the end of an ISP's speed boost makes it.
const LIMIT_HIGH_S = 1; // the higher rate lasts at least this long after ramp-up
const LIMIT_LOW_S = 2; // and the lower rate at least this long, to the end
const LIMIT_SHARE = 0.75; // the lower rate is at most this share of the higher

/** Where a transfer's rate stepped down and stayed down; null when it didn't. The rules are
 * stats.py's rate_limit. */
export function rateLimit(timeline: readonly number[], binBytes: readonly number[], binMs: number, first: number): RateLimit | null {
  const xs = timeline.slice(first);
  const n = xs.length;
  const high = Math.ceil((LIMIT_HIGH_S * 1000) / binMs);
  const low = Math.ceil((LIMIT_LOW_S * 1000) / binMs);
  if (n < high + low) return null;
  // Running sums, added left to right, so this splits where stats.py does.
  const heads: number[] = [];
  let sum = 0;
  for (const v of xs) heads.push((sum += v));
  const total = heads[n - 1]!;
  // The best split leaves the two parts' means furthest apart for their sizes. It is sought
  // everywhere, then held to the two lengths.
  let best = 0;
  let split = 0;
  for (let k = 1; k < n; k++) {
    const head = heads[k - 1]!;
    const rest = total - head;
    if (head / k <= rest / (n - k)) continue; // not a step down
    const fit = (head * head) / k + (rest * rest) / (n - k);
    if (fit > best) {
      best = fit;
      split = k;
    }
  }
  if (split < high || n - split < low) return null;
  const fromMbps = heads[split - 1]! / split;
  const toMbps = (total - heads[split - 1]!) / (n - split);
  const lastMbps = (total - heads[n - high - 1]!) / high;
  const holds = 0 < toMbps && toMbps <= LIMIT_SHARE * fromMbps && percentile(xs.slice(split), 90) < percentile(xs.slice(0, split), 50) && lastMbps <= LIMIT_SHARE * fromMbps;
  if (!holds) return null;
  const at = first + split;
  return { from_mbps: fromMbps, to_mbps: toMbps, at_s: (at * binMs) / 1000, before_bytes: binBytes.slice(0, at).reduce((a, b) => a + b, 0) };
}

/** Jain's fairness index: 1 = perfectly even, 1/n = one stream took everything. */
export function fairness(values: readonly number[]): number | null {
  if (!values.length) return null;
  const sum = values.reduce((a, b) => a + b, 0);
  const squares = values.reduce((a, b) => a + b * b, 0);
  return squares ? (sum * sum) / (values.length * squares) : null;
}
