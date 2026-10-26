// Round-trip measurements: HTTP (idle latency) and WebSocket (latency under load).
import { type Endpoint, linked, nonce, serverTiming, sleep, timingFor } from "./net";

export interface HttpLatencyResult {
  samples: number[];
  lost: number;
  protocol: string | null;
}

/**
 * Sequential GETs of /ping on a warm keep-alive connection. RTT is taken from Resource
 * Timing (requestStart → responseStart) minus the server's own processing time, which
 * removes browser scheduling noise; falls back to wall-clock when timing is unavailable.
 */
export async function measureHttpLatency(
  ep: Endpoint,
  count: number,
  onSample: (rtt: number, index: number) => void,
  signal?: AbortSignal,
): Promise<HttpLatencyResult> {
  const samples: number[] = [];
  let lost = 0;
  let protocol: string | null = null;

  const once = async (): Promise<number | null> => {
    const ping = await pingOnce(ep, signal);
    if (ping?.protocol) protocol = ping.protocol;
    return ping?.rtt ?? null;
  };

  await once(); // connection setup and TLS are not latency
  for (let i = 0; i < count; i++) {
    const rtt = await once();
    if (rtt === null) lost++;
    else {
      samples.push(rtt);
      onSample(rtt, i);
    }
    await sleep(25);
  }
  return { samples, lost, protocol };
}

/**
 * One GET of /ping: its round trip, the server's own time taken out, and the HTTP version
 * it went over; null if it failed. Throws only when `signal` aborts it.
 */
export async function pingOnce(ep: Endpoint, signal?: AbortSignal, timeoutMs = 3000): Promise<{ rtt: number; protocol: string | null } | null> {
  const url = ep.url("/ping", { r: nonce() });
  const ctrl = linked(signal, timeoutMs);
  const t0 = performance.now();
  try {
    const res = await fetch(url, ep.init({ cache: "no-store", signal: ctrl.signal }));
    await res.arrayBuffer();
    const t1 = performance.now();
    const entry = await timingFor(url);
    const protocol = entry?.nextHopProtocol || null;
    if (entry && entry.requestStart > 0 && entry.responseStart > entry.requestStart) {
      return { rtt: Math.max(0, entry.responseStart - entry.requestStart - serverTiming(entry)), protocol };
    }
    return { rtt: Math.max(0, t1 - t0 - serverTiming(res.headers.get("server-timing"))), protocol };
  } catch (err) {
    if (signal?.aborted) throw err;
    return null;
  }
}

/**
 * How long a new connection takes to open: a WebSocket's TCP connect, TLS handshake and
 * HTTP upgrade, which browsers always make on a connection of its own. Null if it didn't
 * open within `timeoutMs`.
 */
export function openTime(url: string, timeoutMs = 5000, protocols: string[] = []): Promise<number | null> {
  return new Promise((resolve) => {
    const t0 = performance.now();
    let ws: WebSocket;
    try {
      ws = new WebSocket(url, protocols);
    } catch {
      resolve(null);
      return;
    }
    const timer = setTimeout(() => {
      ws.close();
      resolve(null);
    }, timeoutMs);
    ws.onopen = () => {
      clearTimeout(timer);
      resolve(performance.now() - t0);
      ws.close();
    };
    ws.onerror = () => {
      clearTimeout(timer);
      resolve(null);
    };
  });
}

export interface ResponsivenessProbes {
  setup: number[]; // new connections: open times
  lost: number; // new connections that didn't open
  loaded: number[]; // round trips on the loaded connection
}

/**
 * The responsiveness test's probes while a transfer runs (draft-ietf-ippm-responsiveness):
 * a new connection opened every `everyMs`, and with `loaded` (HTTP/2 or 3, where requests
 * share the transfer's connection) a GET of /ping on it every `pingMs`.
 */
export function responsivenessWhile(ep: Endpoint, loaded: boolean, everyMs = 1000, pingMs = 250): () => ResponsivenessProbes {
  const probes: ResponsivenessProbes = { setup: [], lost: 0, loaded: [] };
  let running = true;
  const every = async (delay: number, period: number, once: () => Promise<void>) => {
    await sleep(delay);
    while (running) {
      const started = performance.now();
      await once();
      await sleep(Math.max(0, period - (performance.now() - started)));
    }
  };
  void every(500, everyMs, async () => {
    const t = await openTime(ep.wsUrl("/ws"), undefined, ep.wsProtocols());
    if (!running) return;
    if (t === null) probes.lost++;
    else probes.setup.push(t);
  });
  if (loaded) {
    void every(125, pingMs, async () => {
      const ping = await pingOnce(ep);
      if (running && ping) probes.loaded.push(ping.rtt);
    });
  }
  return () => {
    running = false;
    return probes;
  };
}

const PROBE_BYTES = 12; // u32 sequence + f64 send time
/** The echo socket's close code when the server's session_max_runtime is up (docs/api.md). */
export const RUNTIME_CLOSE = 4001;

/** Echo probes over a dedicated WebSocket (its own TCP connection, outside the HTTP pool). */
export class WsProber {
  private ws: WebSocket | null = null;
  private seq = 0;
  private pending = new Map<number, (rtt: number | null) => void>();
  /** The socket closed, by either end: its close code, such as RUNTIME_CLOSE. */
  onClose: ((code: number) => void) | null = null;

  /** `protocols`: the subprotocols that carry the pass to the page's own server (net.ts). */
  async open(url: string, timeoutMs = 4000, protocols: string[] = []): Promise<boolean> {
    return new Promise((resolve) => {
      let ws: WebSocket;
      try {
        ws = new WebSocket(url, protocols);
      } catch {
        resolve(false);
        return;
      }
      ws.binaryType = "arraybuffer";
      const timer = setTimeout(() => {
        ws.close();
        resolve(false);
      }, timeoutMs);
      ws.onopen = () => {
        clearTimeout(timer);
        this.ws = ws;
        resolve(true);
      };
      ws.onerror = () => {
        clearTimeout(timer);
        resolve(false);
      };
      ws.onclose = (ev) => {
        this.ws = null;
        for (const done of this.pending.values()) done(null);
        this.pending.clear();
        this.onClose?.(ev.code);
      };
      ws.onmessage = (ev) => {
        const now = performance.now();
        if (!(ev.data instanceof ArrayBuffer) || ev.data.byteLength < PROBE_BYTES) return;
        const view = new DataView(ev.data);
        const done = this.pending.get(view.getUint32(0, true));
        if (done) done(now - view.getFloat64(4, true));
      };
    });
  }

  get ready(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  ping(timeoutMs = 2000): Promise<number | null> {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return Promise.resolve(null);
    const seq = this.seq++ >>> 0;
    const buf = new ArrayBuffer(PROBE_BYTES);
    const view = new DataView(buf);
    view.setUint32(0, seq, true);
    return new Promise((resolve) => {
      const timer = setTimeout(() => finish(null), timeoutMs);
      const finish = (rtt: number | null) => {
        clearTimeout(timer);
        this.pending.delete(seq);
        resolve(rtt);
      };
      this.pending.set(seq, finish);
      view.setFloat64(4, performance.now(), true);
      ws.send(buf);
    });
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }
}

/** Periodic probing while something else (a throughput test) loads the path. */
export function probeWhile(
  prober: WsProber,
  intervalMs: number,
  onSample: (rtt: number) => void,
): () => { samples: number[]; lost: number } {
  const samples: number[] = [];
  let lost = 0;
  let running = true;
  void (async () => {
    while (running && prober.ready) {
      const started = performance.now();
      const rtt = await prober.ping(3000);
      if (!running) break;
      if (rtt === null) lost++;
      else {
        samples.push(rtt);
        onSample(rtt);
      }
      await sleep(Math.max(0, intervalMs - (performance.now() - started)));
    }
  })();
  return () => {
    running = false;
    return { samples, lost };
  };
}
