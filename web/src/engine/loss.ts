// UDP probing over a WebRTC data channel (unordered, no retransmissions), with a
// WebSocket fallback when UDP is blocked.
import type { LossResult } from "../types";
import type { WsProber } from "./latency";
import { type Endpoint, type Family, sleep } from "./net";
import { summarize } from "./stats";

const HEADER = 12; // u32 sequence + f64 send time

function waitFor(target: EventTarget, event: string, timeoutMs: number, ok: () => boolean): Promise<boolean> {
  if (ok()) return Promise.resolve(true);
  return new Promise((resolve) => {
    const timer = setTimeout(() => done(false), timeoutMs);
    const check = () => ok() && done(true);
    const done = (v: boolean) => {
      clearTimeout(timer);
      target.removeEventListener(event, check);
      resolve(v);
    };
    target.addEventListener(event, check);
  });
}

/** The server offers no address in the family the endpoint asks for, so UDP can't be tried. */
export class NotOffered extends Error {}

/** How the server sees the client on the UDP path, and its own end of it. */
export interface UdpAddress {
  ip: string | null;
  port: number | null;
  server_ip: string | null;
  server_port: number | null;
}

type ControlReply = Record<string, unknown>;

export class RtcProbe {
  onEcho: ((seq: number, rtt: number) => void) | null = null;
  private controlWaiters: { op: string; done: (msg: ControlReply) => void }[] = [];

  private constructor(
    private readonly pc: RTCPeerConnection,
    private readonly probe: RTCDataChannel,
    private readonly control: RTCDataChannel,
  ) {
    probe.binaryType = "arraybuffer";
    probe.onmessage = (ev) => {
      const now = performance.now();
      if (!(ev.data instanceof ArrayBuffer) || ev.data.byteLength < HEADER) return;
      const view = new DataView(ev.data);
      this.onEcho?.(view.getUint32(0, true), now - view.getFloat64(4, true));
    };
    control.onmessage = (ev) => {
      try {
        const msg = JSON.parse(String(ev.data)) as ControlReply;
        // Each reply names its request ("op"), so a late one can't answer another request.
        const at = this.controlWaiters.findIndex((w) => w.op === msg.op);
        if (at >= 0) this.controlWaiters.splice(at, 1)[0]!.done(msg);
      } catch {
        /* ignore */
      }
    };
  }

  static async connect(ep: Endpoint, iceServers: RTCIceServer[], timeoutMs = 8000): Promise<RtcProbe> {
    const pc = new RTCPeerConnection({ iceServers });
    try {
      const probe = pc.createDataChannel("probe", { ordered: false, maxRetransmits: 0 });
      const control = pc.createDataChannel("control");
      await pc.setLocalDescription(await pc.createOffer());
      // The server answers without trickle ICE, so send a complete candidate list.
      await waitFor(pc, "icegatheringstatechange", 2500, () => pc.iceGatheringState === "complete");
      const answer = await ep.post<RTCSessionDescriptionInit>("/rtc/offer", {
        sdp: pc.localDescription!.sdp,
        type: "offer",
      });
      // A single-family hostname covers HTTP; ICE would still pick either family for UDP.
      const sdp = ep.family ? onlyFamily(answer.sdp!, ep.family) : answer.sdp;
      if (ep.family && !/^a=candidate:/m.test(sdp ?? "")) {
        throw new NotOffered(`the server offers no ${ep.family === "ipv6" ? "IPv6" : "IPv4"} address for UDP`);
      }
      await pc.setRemoteDescription({ sdp, type: "answer" });
      const open = await waitFor(probe, "open", timeoutMs, () => probe.readyState === "open");
      if (!open) throw new Error("UDP path did not open (ICE failed or UDP blocked)");
      await waitFor(control, "open", 2000, () => control.readyState === "open");
      return new RtcProbe(pc, probe, control);
    } catch (err) {
      pc.close();
      throw err;
    }
  }

  get open(): boolean {
    return this.probe.readyState === "open";
  }

  /** The address family of the UDP path ICE chose, once the channel is open. */
  async family(): Promise<"IPv4" | "IPv6" | null> {
    return (await this.path())?.family ?? null;
  }

  /** The server's end of the UDP path ICE chose: its address and port. */
  async path(): Promise<{ address: string; port: number | null; family: "IPv4" | "IPv6" } | null> {
    const stats = await this.pc.getStats();
    let pair: { remoteCandidateId?: string } | undefined;
    stats.forEach((s) => {
      if (s.type === "transport" && s.selectedCandidatePairId) pair = stats.get(s.selectedCandidatePairId);
    });
    stats.forEach((s) => {
      if (!pair && s.type === "candidate-pair" && s.nominated && s.state === "succeeded") pair = s;
    });
    const remote = pair?.remoteCandidateId ? stats.get(pair.remoteCandidateId) : undefined;
    const address: string | undefined = remote?.address ?? remote?.ip;
    if (!address) return null;
    return { address, port: typeof remote?.port === "number" ? remote.port : null, family: address.includes(":") ? "IPv6" : "IPv4" };
  }

  send(seq: number, size: number): void {
    const buf = new ArrayBuffer(Math.max(size, HEADER));
    const view = new DataView(buf);
    view.setUint32(0, seq >>> 0, true);
    view.setFloat64(4, performance.now(), true);
    this.probe.send(buf);
  }

  async counters(timeoutMs = 2000): Promise<{ rx: number; tx: number } | null> {
    const msg = await this.ask("stats", timeoutMs);
    return msg ? { rx: Number(msg.rx ?? 0), tx: Number(msg.tx ?? 0) } : null;
  }

  /** The client's address and port as the server sees them on this UDP path (after NAT). */
  async address(timeoutMs = 2000): Promise<UdpAddress | null> {
    const msg = await this.ask("addr", timeoutMs);
    if (!msg) return null;
    const str = (v: unknown) => (typeof v === "string" && v ? v : null);
    const num = (v: unknown) => (typeof v === "number" ? v : null);
    return { ip: str(msg.ip), port: num(msg.port), server_ip: str(msg.server_ip), server_port: num(msg.server_port) };
  }

  /** One request on the control channel; replies come back in order. */
  private ask(op: string, timeoutMs: number): Promise<ControlReply | null> {
    if (this.control.readyState !== "open") return Promise.resolve(null);
    return new Promise((resolve) => {
      const waiter = {
        op,
        done: (msg: ControlReply) => {
          clearTimeout(timer);
          resolve(msg);
        },
      };
      // An older server doesn't know every op and never replies: give up, and forget it.
      const timer = setTimeout(() => {
        this.controlWaiters = this.controlWaiters.filter((w) => w !== waiter);
        resolve(null);
      }, timeoutMs);
      this.controlWaiters.push(waiter);
      this.control.send(JSON.stringify({ op }));
    });
  }

  close(): void {
    this.probe.close();
    this.control.close();
    this.pc.close();
  }
}

/** The SDP with only the server's candidates of one address family. */
function onlyFamily(sdp: string, family: Family): string {
  return sdp
    .split("\r\n")
    .filter((line) => !line.startsWith("a=candidate:") || (line.split(" ")[4] ?? "").includes(":") === (family === "ipv6"))
    .join("\r\n");
}

export interface LossOptions {
  packets: number;
  intervalMs: number;
  size: number;
  waitMs: number;
  signal: AbortSignal;
  onSent?: (seq: number) => void;
  onEcho?: (seq: number, rtt: number) => void;
}

function finish(
  rtts: (number | null)[],
  order: number[],
  duplicates: number,
  counters: { rx: number; tx: number } | null,
  transport: LossResult["transport"],
  opts: LossOptions,
): LossResult {
  const sent = rtts.length;
  const received = rtts.filter((r) => r !== null).length;
  let outOfOrder = 0;
  for (let i = 1; i < order.length; i++) if (order[i]! < order[i - 1]!) outOfOrder++;
  let up: number | null = null;
  let down: number | null = null;
  if (counters && sent) {
    up = Math.max(0, ((sent - counters.rx) / sent) * 100);
    if (counters.tx) down = Math.max(0, ((counters.tx - received) / counters.tx) * 100);
  }
  return {
    transport,
    sent,
    received,
    lost: sent - received,
    loss_pct: sent ? ((sent - received) / sent) * 100 : 0,
    up_loss_pct: up,
    down_loss_pct: down,
    out_of_order: outOfOrder,
    duplicates,
    interval_ms: opts.intervalMs,
    size: opts.size,
    rtt: summarize(
      rtts.filter((r): r is number => r !== null),
      0,
      false,
    ),
    packets: rtts.map((r) => (r === null ? null : Math.round(r * 1000) / 1000)),
  };
}

/** Paced against a schedule (not setInterval) so timer clamping cannot drop probes. */
async function paced(opts: LossOptions, send: (seq: number) => void): Promise<void> {
  const start = performance.now();
  let seq = 0;
  while (seq < opts.packets && !opts.signal.aborted) {
    const due = Math.floor((performance.now() - start) / opts.intervalMs) + 1;
    while (seq < Math.min(due, opts.packets)) {
      send(seq);
      opts.onSent?.(seq);
      seq++;
    }
    await sleep(Math.max(0, start + seq * opts.intervalMs - performance.now()));
  }
}

export async function measureLossRtc(probe: RtcProbe, opts: LossOptions): Promise<LossResult> {
  const rtts: (number | null)[] = new Array<number | null>(opts.packets).fill(null);
  const order: number[] = [];
  let duplicates = 0;
  let pending = opts.packets;
  probe.onEcho = (seq, rtt) => {
    if (seq >= opts.packets) return;
    if (rtts[seq] !== null) {
      duplicates++;
      return;
    }
    rtts[seq] = rtt;
    order.push(seq);
    pending--;
    opts.onEcho?.(seq, rtt);
  };
  await paced(opts, (seq) => probe.send(seq, opts.size));
  const waitUntil = performance.now() + opts.waitMs;
  while (pending > 0 && performance.now() < waitUntil && !opts.signal.aborted) await sleep(50);
  probe.onEcho = null;
  const counters = await probe.counters();
  return finish(rtts, order, duplicates, counters, "webrtc", opts);
}

/** TCP fallback: delivery is reliable, so only probes that time out count as lost. */
export async function measureLossWs(prober: WsProber, opts: LossOptions): Promise<LossResult> {
  const rtts: (number | null)[] = new Array<number | null>(opts.packets).fill(null);
  const order: number[] = [];
  const inflight: Promise<void>[] = [];
  await paced(opts, (seq) => {
    inflight.push(
      prober.ping(opts.waitMs).then((rtt) => {
        if (rtt === null) return;
        rtts[seq] = rtt;
        order.push(seq);
        opts.onEcho?.(seq, rtt);
      }),
    );
  });
  await Promise.all(inflight);
  const result = finish(rtts, order, 0, null, "websocket", opts);
  result.note = "UDP unavailable; measured over TCP, where loss shows up as delay, not drops.";
  return result;
}
