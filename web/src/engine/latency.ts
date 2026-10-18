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
    const url = ep.url("/ping", { r: nonce() });
    const ctrl = linked(signal, 3000);
    const t0 = performance.now();
    try {
      const res = await fetch(url, { cache: "no-store", signal: ctrl.signal });
      await res.arrayBuffer();
      const t1 = performance.now();
      const entry = await timingFor(url);
      if (entry?.nextHopProtocol) protocol = entry.nextHopProtocol;
      if (entry && entry.requestStart > 0 && entry.responseStart > entry.requestStart) {
        return Math.max(0, entry.responseStart - entry.requestStart - serverTiming(entry));
      }
      return Math.max(0, t1 - t0 - serverTiming(res.headers.get("server-timing")));
    } catch (err) {
      if (signal?.aborted) throw err;
      return null;
    }
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

const PROBE_BYTES = 12; // u32 sequence + f64 send time

/** Echo probes over a dedicated WebSocket (its own TCP connection, outside the HTTP pool). */
export class WsProber {
  private ws: WebSocket | null = null;
  private seq = 0;
  private pending = new Map<number, (rtt: number | null) => void>();

  async open(url: string, timeoutMs = 4000): Promise<boolean> {
    return new Promise((resolve) => {
      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
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
      ws.onclose = () => {
        this.ws = null;
        for (const done of this.pending.values()) done(null);
        this.pending.clear();
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
