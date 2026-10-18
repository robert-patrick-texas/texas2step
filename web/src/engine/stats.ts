// Mirrors src/texas2step/stats.py; keep the definitions identical.
import type { LatencyStats } from "../types";

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
  };
}

/** Jain's fairness index: 1 = perfectly even, 1/n = one stream took everything. */
export function fairness(values: readonly number[]): number | null {
  if (!values.length) return null;
  const sum = values.reduce((a, b) => a + b, 0);
  const squares = values.reduce((a, b) => a + b * b, 0);
  return squares ? (sum * sum) / (values.length * squares) : null;
}
