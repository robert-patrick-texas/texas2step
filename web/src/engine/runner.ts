// Orchestrates a full test: idle latency → download → one stream → upload → packet loss.
import type { Direction, LatencyStats, NewConnections, ServerInfo, TestResult, ThroughputResult, WhoAmI } from "../types";
import { eui64Mac } from "./addr";
import { measureHttpLatency, probeWhile, responsivenessWhile, WsProber } from "./latency";
import { measureLossRtc, measureLossWs, RtcProbe } from "./loss";
import { clearTimings, type Endpoint, nonce } from "./net";
import { callQuality, responsiveness } from "./quality";
import { bufferbloat, readiness } from "./scores";
import { summarize, summarizeThroughput } from "./stats";
import { BIN_MS, type LiveThroughput, measureDownload, measureUpload } from "./throughput";

export type Phase = "latency" | "download" | "one" | "upload" | "loss";
export const PHASES: Phase[] = ["latency", "download", "one", "upload", "loss"];

export interface RunConfig {
  streams: number;
  seconds: number;
  packets: number;
  intervalMs: number;
  oneStream: boolean; // after the download, one stream's: skipped when the test uses one
}

export interface RunEvents {
  phase(phase: Phase): void;
  latency(kind: "idle" | Direction, rtt: number): void;
  throughput(direction: Direction | "one", live: LiveThroughput): void;
  packetSent(seq: number): void;
  packetEcho(seq: number, rtt: number): void;
  partial(result: TestResult): void;
}

/** A result's MAC fields, where `ip` is an EUI-64 IPv6 address. */
function macFrom(ip: string | undefined): Pick<TestResult["client"], "mac" | "mac_source"> {
  const mac = eui64Mac(ip);
  return mac ? { mac, mac_source: "eui64" } : {};
}

/** This UI's version, written into the page by the server that served it. A visitor who
 * has to enter the access code gets the page without it, and the app fills it in after. */
export function uiVersion(): string {
  return document.querySelector<HTMLMetaElement>('meta[name="texas2step-version"]')?.content || "dev";
}

export class TestRun {
  private ctrl = new AbortController();
  private readonly test = nonce(); // sent with each transfer, so a traffic limit lets it finish
  private multiplexed = false; // HTTP/2 or 3: a request shares the transfer's connection
  readonly result: TestResult;

  constructor(
    private readonly ep: Endpoint,
    private readonly info: ServerInfo,
    whoami: WhoAmI | null,
    private readonly config: RunConfig,
    private readonly on: RunEvents,
  ) {
    this.result = {
      version: 1,
      tool: `texas2step-web/${uiVersion()}`,
      started_at: new Date().toISOString(),
      server: { ...info.server, url: ep.base, version: info.version },
      client: {
        ip: whoami?.ip,
        family: whoami?.family ?? null,
        protocol: whoami?.http_version,
        user_agent: navigator.userAgent,
        ...macFrom(whoami?.ip),
      },
      config: {
        streams: config.streams,
        seconds: config.seconds,
        packets: config.packets,
        interval_ms: config.intervalMs,
        one_stream: config.oneStream && config.streams > 1,
      },
    };
  }

  abort(): void {
    this.ctrl.abort(new DOMException("stopped", "AbortError"));
  }

  get aborted(): boolean {
    return this.ctrl.signal.aborted;
  }

  async run(): Promise<TestResult> {
    const t0 = performance.now();
    const signal = this.ctrl.signal;
    const r = this.result;
    const prober = new WsProber();

    try {
      this.on.phase("latency");
      const idle = await measureHttpLatency(this.ep, 20, (rtt) => this.on.latency("idle", rtt), signal);
      r.latency = summarize(idle.samples, idle.lost);
      if (idle.protocol) r.client.protocol = idle.protocol;
      this.multiplexed = /^h[23]$/i.test(idle.protocol ?? "");
      this.on.partial(r);
      clearTimings();

      // Opening the probe's own connection, idle, is what each new connection costs.
      const opening = performance.now();
      if (await prober.open(this.ep.wsUrl("/ws"))) r.latency.setup_ms = performance.now() - opening;
      for (const direction of ["download", "upload"] as const) {
        if (signal.aborted) break;
        this.on.phase(direction);
        r[direction] = await this.throughput(direction, prober, signal);
        r.bufferbloat = bufferbloat(r);
        this.judge();
        this.on.partial(r);
        clearTimings();
        if (direction === "download" && r.config.one_stream && !signal.aborted) {
          this.on.phase("one");
          r.one_stream = await this.oneStream(signal);
          this.on.partial(r);
          clearTimings();
        }
      }

      if (this.config.packets > 0 && !signal.aborted) {
        this.on.phase("loss");
        r.loss = await this.loss(prober, signal);
        this.judge();
        this.on.partial(r);
      }
    } finally {
      prober.close();
    }

    r.bufferbloat = bufferbloat(r);
    this.judge();
    r.readiness = readiness(r);
    r.duration_s = (performance.now() - t0) / 1000;
    return r;
  }

  /** What the figures so far mean for calls and responsiveness. */
  private judge(): void {
    const r = this.result;
    const call = callQuality(r.loss);
    const rpm = responsiveness(r);
    if (call) r.call = call;
    if (rpm) r.responsiveness = rpm;
  }

  private async throughput(direction: Direction, prober: WsProber, signal: AbortSignal): Promise<ThroughputResult> {
    const { seconds, streams } = this.config;
    const limits = this.info.limits;
    const stopProbe = prober.ready ? probeWhile(prober, 250, (rtt) => this.on.latency(direction, rtt)) : null;
    const stopResponsiveness = responsivenessWhile(this.ep, this.multiplexed);
    const measure = direction === "download" ? measureDownload : measureUpload;
    const raw = await measure(this.ep, {
      seconds,
      streams,
      maxBytes: direction === "download" ? limits.max_download_bytes : limits.max_upload_bytes,
      test: this.test,
      signal,
      onTick: (live) => this.on.throughput(direction, live),
    });
    const loaded = stopProbe?.();
    const probes = stopResponsiveness();
    const loadedStats: LatencyStats | null = loaded?.samples.length ? summarize(loaded.samples, loaded.lost) : null;
    const rounded = (values: number[]) => values.map((v) => Math.round(v * 1000) / 1000);
    const connections: NewConnections = { setup_ms: rounded(probes.setup), lost: probes.lost, tls: this.ep.base.startsWith("https:") };
    return {
      direction,
      ...summarizeThroughput(raw.bins, BIN_MS, seconds),
      streams,
      per_stream_mbps: raw.perStream.map((b) => (b * 8) / (raw.elapsed * 1e6)),
      loaded_latency: loadedStats,
      connections,
      ...(probes.loaded.length ? { loaded_probes: rounded(probes.loaded) } : {}),
    };
  }

  /** One stream's download, for the same time: what a single file copy gets. */
  private async oneStream(signal: AbortSignal): Promise<ThroughputResult> {
    const { seconds } = this.config;
    const raw = await measureDownload(this.ep, {
      seconds,
      streams: 1,
      maxBytes: this.info.limits.max_download_bytes,
      test: this.test,
      signal,
      onTick: (live) => this.on.throughput("one", live),
    });
    return {
      direction: "download",
      ...summarizeThroughput(raw.bins, BIN_MS, seconds),
      streams: 1,
      per_stream_mbps: raw.perStream.map((b) => (b * 8) / (raw.elapsed * 1e6)),
      loaded_latency: null,
    };
  }

  private async loss(prober: WsProber, signal: AbortSignal) {
    const opts = {
      packets: this.config.packets,
      intervalMs: this.config.intervalMs,
      size: 200, // at least a voice packet: a path that can't carry these can't carry calls
      waitMs: 2000,
      stamps: this.info.capabilities.stamps === true,
      signal,
      onSent: (seq: number) => this.on.packetSent(seq),
      onEcho: (seq: number, rtt: number) => this.on.packetEcho(seq, rtt),
    };
    let reason = "WebRTC disabled on server";
    if (this.info.capabilities.webrtc && "RTCPeerConnection" in window) {
      try {
        const probe = await RtcProbe.connect(this.ep, this.info.ice_servers);
        try {
          const local = (await probe.path())?.local;
          if (local) {
            this.result.client.local_ip = local;
            if (!this.result.client.mac) Object.assign(this.result.client, macFrom(local));
          }
          return await measureLossRtc(probe, opts);
        } finally {
          probe.close();
        }
      } catch (err) {
        reason = err instanceof Error ? err.message : String(err);
      }
    }
    if (!prober.ready) await prober.open(this.ep.wsUrl("/ws"));
    const result = await measureLossWs(prober, opts);
    result.note = `${result.note} (${reason})`;
    return result;
  }
}
