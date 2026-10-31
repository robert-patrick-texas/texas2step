// Time-boxed, multi-stream throughput. Bytes are credited to 100 ms bins as they are
// observed (download: as the body reader yields; upload: as XHR progress reports), so
// the timeline shows ramp-up and the stable figure can exclude it.
import { type Endpoint, linked, nonce, Refused, refusal } from "./net";

export const BIN_MS = 100;

export interface ThroughputOptions {
  seconds: number;
  streams: number;
  maxBytes: number;
  test: string; // the test's id: a server's traffic limit lets a test it let in finish
  transfer?: "one_stream"; // the one stream's download, which the server times apart
  signal: AbortSignal;
  onTick?: (live: LiveThroughput) => void;
}

export interface LiveThroughput {
  elapsed: number; // seconds
  mbps: number; // rate over the most recent ~500 ms
  timeline: number[]; // Mbps per completed bin
  perStream: number[]; // bytes per stream so far
}

export interface ThroughputRaw {
  bins: number[];
  perStream: number[];
  elapsed: number;
  errors: number;
}

class Meter {
  readonly start = performance.now();
  readonly bins: number[];
  readonly perStream: number[];

  constructor(
    readonly seconds: number,
    streams: number,
  ) {
    this.bins = new Array<number>(Math.max(1, Math.round((seconds * 1000) / BIN_MS))).fill(0);
    this.perStream = new Array<number>(streams).fill(0);
  }

  get deadline(): number {
    return this.start + this.seconds * 1000;
  }

  add(stream: number, n: number, now = performance.now()): void {
    const i = Math.floor((now - this.start) / BIN_MS);
    if (i >= 0 && i < this.bins.length) {
      this.bins[i]! += n;
      this.perStream[stream]! += n;
    }
  }

  live(): LiveThroughput {
    const elapsed = (performance.now() - this.start) / 1000;
    const done = Math.min(this.bins.length, Math.floor((elapsed * 1000) / BIN_MS));
    const recent = this.bins.slice(Math.max(0, done - 5), done);
    const scale = 8 / (BIN_MS * 1000);
    return {
      elapsed,
      mbps: recent.length ? (recent.reduce((a, b) => a + b, 0) * scale) / recent.length : 0,
      timeline: this.bins.slice(0, done).map((b) => b * scale),
      perStream: [...this.perStream],
    };
  }

  raw(errors: number): ThroughputRaw {
    const elapsed = Math.min((performance.now() - this.start) / 1000, this.seconds);
    const complete = Math.max(1, Math.floor((elapsed * 1000) / BIN_MS));
    return { bins: this.bins.slice(0, complete), perStream: this.perStream, elapsed, errors };
  }
}

function ticker(meter: Meter, opts: ThroughputOptions): () => void {
  const id = setInterval(() => opts.onTick?.(meter.live()), 100);
  return () => clearInterval(id);
}

export async function measureDownload(ep: Endpoint, opts: ThroughputOptions): Promise<ThroughputRaw> {
  const meter = new Meter(opts.seconds, opts.streams);
  const stop = ticker(meter, opts);
  let errors = 0;

  const stream = async (i: number) => {
    while (performance.now() < meter.deadline && !opts.signal.aborted) {
      const ctrl = linked(opts.signal);
      try {
        const res = await fetch(ep.url("/down", { bytes: opts.maxBytes, test: opts.test, ...(opts.transfer ? { transfer: opts.transfer } : {}), r: nonce() }), ep.init({
          cache: "no-store",
          signal: ctrl.signal,
        }));
        if (res.status === 503) throw refusal(await res.text());
        if (!res.ok || !res.body) throw new Error(`download failed: ${res.status}`);
        const reader = res.body.getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          const now = performance.now();
          if (now >= meter.deadline) {
            ctrl.abort();
            return;
          }
          meter.add(i, value.byteLength, now);
        }
      } catch (err) {
        if (err instanceof Refused) throw err;
        if (opts.signal.aborted || performance.now() >= meter.deadline) return;
        errors++;
        await new Promise((r) => setTimeout(r, 100));
      }
    }
  };

  try {
    await Promise.all(Array.from({ length: opts.streams }, (_, i) => stream(i)));
  } finally {
    stop();
  }
  return meter.raw(errors);
}

// ---- Upload ------------------------------------------------------------------------

const MIB = 1 << 20;
let randomBlock: Uint8Array<ArrayBuffer> | null = null;
const blobs = new Map<number, Blob>();

function payload(size: number): Blob {
  let blob = blobs.get(size);
  if (!blob) {
    if (!randomBlock) {
      randomBlock = new Uint8Array(MIB);
      for (let off = 0; off < MIB; off += 65536) crypto.getRandomValues(randomBlock.subarray(off, off + 65536));
    }
    const parts = new Array<Uint8Array<ArrayBuffer>>(Math.ceil(size / MIB)).fill(randomBlock);
    blob = new Blob(parts).slice(0, size);
    blobs.set(size, blob);
  }
  return blob;
}

/** Whether the upload went through; a traffic limit's refusal rejects with Refused. */
function xhrUpload(
  url: string,
  headers: Record<string, string>,
  body: Blob,
  onBytes: (n: number) => void,
  signal: AbortSignal,
): Promise<boolean> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let reported = 0;
    const abort = () => xhr.abort();
    signal.addEventListener("abort", abort, { once: true });
    const settle = (ok: boolean) => {
      signal.removeEventListener("abort", abort);
      resolve(ok);
    };
    xhr.open("POST", url);
    for (const [k, v] of Object.entries(headers)) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => {
      if (e.loaded > reported) {
        onBytes(e.loaded - reported);
        reported = e.loaded;
      }
    };
    xhr.onload = () => {
      if (xhr.status === 503) {
        signal.removeEventListener("abort", abort);
        reject(refusal(xhr.responseText));
        return;
      }
      // Small bodies may complete without any progress event.
      if (xhr.status < 300 && body.size > reported) onBytes(body.size - reported);
      settle(xhr.status < 300);
    };
    xhr.onerror = () => settle(false);
    xhr.onabort = () => settle(false);
    xhr.send(body);
  });
}

export async function measureUpload(ep: Endpoint, opts: ThroughputOptions): Promise<ThroughputRaw> {
  const meter = new Meter(opts.seconds, opts.streams);
  const stop = ticker(meter, opts);
  const cap = Math.min(opts.maxBytes, 64 * MIB);
  const ctrl = linked(opts.signal);
  let errors = 0;

  // Close in-flight requests exactly at the deadline.
  const deadlineTimer = setTimeout(() => ctrl.abort(), opts.seconds * 1000);

  const stream = async (i: number) => {
    let size = 256 * 1024;
    while (performance.now() < meter.deadline && !ctrl.signal.aborted) {
      const t0 = performance.now();
      const ok = await xhrUpload(
        ep.url("/up", { test: opts.test, r: nonce() }),
        ep.headers(),
        payload(size),
        (n) => meter.add(i, n),
        ctrl.signal,
      );
      if (!ok) {
        if (ctrl.signal.aborted) return;
        errors++;
        await new Promise((r) => setTimeout(r, 100));
        continue;
      }
      // Grow requests until each takes about a second: big enough for TCP to reach
      // steady state, small enough to keep many samples per test.
      const took = performance.now() - t0;
      if (took < 250) size = Math.min(cap, size * 4);
      else if (took < 700) size = Math.min(cap, size * 2);
      else if (took > 2500) size = Math.max(64 * 1024, size / 2);
    }
  };

  try {
    await Promise.all(Array.from({ length: opts.streams }, (_, i) => stream(i)));
  } finally {
    clearTimeout(deadlineTimer);
    stop();
  }
  return meter.raw(errors);
}
