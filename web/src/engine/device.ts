// When the device is the limit (docs/methodology.md#when-the-device-is-the-limit): how late
// the page's own work ran during each phase of a test. Everything a browser test does runs
// on the page's main thread, so a busy device reads slower and adds its lateness to every
// latency it times. A 50 ms timer measures that lateness in every browser; Chromium adds
// its long tasks and, on the desktop, the computer's CPU pressure.

const PERIOD = 50; // ms between the timer's runs
const GRACE = 2; // ms late that any timer may be, idle or not

export type Pressure = "nominal" | "fair" | "serious" | "critical";
const PRESSURES: Pressure[] = ["nominal", "fair", "serious", "critical"];

/** A phase's figures: the share of its time the page ran late (`busy`), its lateness at
 * the 95th percentile and the median, and (Chromium) the time its long tasks took. */
export interface PhaseLag {
  busy: number;
  lag_p95_ms: number;
  lag_median_ms: number;
  long_ms?: number;
}

interface PressureRecordLike {
  state: Pressure;
}
interface PressureObserverLike {
  observe(source: "cpu", options?: { sampleInterval?: number }): Promise<void>;
  disconnect(): void;
}
type PressureObserverCtor = new (callback: (records: PressureRecordLike[]) => void) => PressureObserverLike;

const percentile = (sorted: number[], p: number): number => {
  if (!sorted.length) return 0;
  const i = Math.min(sorted.length - 1, Math.max(0, Math.ceil(p * sorted.length) - 1));
  return sorted[i]!;
};

export class DeviceWatch {
  private ticks: [number, number][] = []; // when each timer ran, and how late (ms)
  private longs: [number, number][] = []; // Chromium's long tasks: start and duration
  private worst = -1; // the highest CPU pressure seen, as an index of PRESSURES
  private timer: ReturnType<typeof setTimeout> | null = null;
  private observers: { disconnect(): void }[] = [];

  /** `details`: Chromium's long tasks and CPU pressure too (device_details). */
  constructor(private readonly details: boolean) {}

  start(): void {
    if (this.timer !== null) return;
    let due = performance.now() + PERIOD;
    const tick = () => {
      const now = performance.now();
      this.ticks.push([now, Math.max(0, now - due)]);
      due = now + PERIOD;
      this.timer = setTimeout(tick, PERIOD);
    };
    this.timer = setTimeout(tick, PERIOD);
    if (!this.details) return;
    try {
      if (PerformanceObserver.supportedEntryTypes?.includes("longtask")) {
        const o = new PerformanceObserver((list) => {
          for (const e of list.getEntries()) this.longs.push([e.startTime, e.duration]);
        });
        o.observe({ type: "longtask" });
        this.observers.push(o);
      }
    } catch {
      /* not this browser */
    }
    const Ctor = (window as unknown as { PressureObserver?: PressureObserverCtor }).PressureObserver;
    if (Ctor) {
      try {
        const o = new Ctor((records) => {
          for (const r of records) this.worst = Math.max(this.worst, PRESSURES.indexOf(r.state));
        });
        o.observe("cpu", { sampleInterval: 1000 }).catch(() => undefined);
        this.observers.push(o);
      } catch {
        /* a phone, or a page without permission */
      }
    }
  }

  stop(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    for (const o of this.observers) o.disconnect();
    this.observers = [];
  }

  /** The figures between two performance.now() times; null for a phase under a second.
   * Each run of the timer takes its period plus its lateness, so the time the runs
   * didn't account for is the time the page was late, even when a busy page starved the
   * timer of all but a few runs. */
  phase(t0: number, t1: number): PhaseLag | null {
    const span = t1 - t0;
    if (span < 1000 || this.ticks.length === 0) return null;
    const late = this.ticks.filter(([t]) => t >= t0 && t < t1).map(([, l]) => l);
    const sorted = [...late].sort((a, b) => a - b);
    // Too few runs to rank: each stood for an equal share of the phase.
    const gap = span / (late.length + 1);
    const out: PhaseLag = {
      busy: Math.min(1, Math.max(0, span - late.length * (PERIOD + GRACE)) / span),
      lag_p95_ms: late.length >= 10 ? percentile(sorted, 0.95) : gap,
      lag_median_ms: late.length >= 10 ? percentile(sorted, 0.5) : gap,
    };
    if (this.details && PerformanceObserver.supportedEntryTypes?.includes("longtask")) {
      out.long_ms = this.longs.reduce((sum, [s, d]) => sum + Math.max(0, Math.min(t1, s + d) - Math.max(t0, s)), 0);
    }
    return out;
  }

  /** The highest CPU pressure Chromium reported while it watched; null where it can't. */
  get pressure(): Pressure | null {
    return this.worst >= 0 ? PRESSURES[this.worst]! : null;
  }
}

/** A download's share of bytes read in full-size reads that held this much or more:
 * Chromium hands a fetch's body over in up to 2 MiB at a time, and reads that come full
 * mean the bytes waited for the page, not the other way round. */
export const FULL_READ = 1 << 20;

/** Over this share of a download in full reads, the page itself fell behind (calibrated:
 * 98-100 % in every device-bound download, at most 77 % in any other). Chromium only. */
export const FULL_READS_BOUND = 0.93;
