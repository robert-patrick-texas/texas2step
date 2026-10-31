// Orchestrates a full test: idle latency → download → one stream → upload → packet loss.
import type { Direction, LatencyStats, NewConnections, ServerInfo, TestResult, ThroughputResult, WhoAmI } from "../types";
import { eui64Mac } from "./addr";
import { DeviceWatch, FULL_READS_BOUND, type PhaseLag } from "./device";
import { measureHttpLatency, probeWhile, responsivenessWhile, WsProber } from "./latency";
import { measureLossRtc, measureLossWs, RtcProbe } from "./loss";
import { clearTimings, type Endpoint, nonce } from "./net";
import { callQuality, responsiveness } from "./quality";
import { bufferbloat, readiness, withheld } from "./scores";
import { summarize, summarizeThroughput } from "./stats";
import { BIN_MS, type LiveThroughput, measureDownload, measureUpload, type ThroughputRaw } from "./throughput";

export type Phase = "latency" | "download" | "one" | "upload" | "loss";
export const PHASES: Phase[] = ["latency", "download", "one", "upload", "loss"];

export interface RunConfig {
  streams: number;
  seconds: number;
  packets: number;
  intervalMs: number;
  oneStream: boolean; // after the download, one stream's: skipped when the test uses one
  http?: "http1" | "http3"; // held to this HTTP version, by the endpoint's name for it
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

const round3 = (v: number) => Math.round(v * 1000) / 1000;

/** A download's share of bytes in full-size reads (device.ts), where the page counted. */
function fullReads(raw: ThroughputRaw): { device_full_reads?: number } {
  return raw.readBytes ? { device_full_reads: round3((raw.fullBytes ?? 0) / raw.readBytes) } : {};
}

export class TestRun {
  private ctrl = new AbortController();
  private readonly test = nonce(); // sent with each transfer, so a traffic limit lets it finish
  private multiplexed = false; // HTTP/2 or 3: a request shares the transfer's connection
  // How late the page ran in each phase (device.ts): the device's share of what was timed.
  private readonly watch: DeviceWatch | null;
  private readonly lags: Partial<Record<Phase, PhaseLag>> = {};
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
        ...(config.http ? { http: config.http } : {}),
      },
    };
    const device = info.device ?? { check: true, details: true, bound_percent: 10 };
    this.watch = device.check ? new DeviceWatch(device.details) : null;
  }

  private get details(): boolean {
    return this.info.device?.details ?? true;
  }

  /** Runs one phase of the test, and keeps how late the page ran during it. */
  private async timed<T>(phase: Phase, work: () => Promise<T>): Promise<T> {
    const t0 = performance.now();
    try {
      return await work();
    } finally {
      const lag = this.watch?.phase(t0, performance.now());
      if (lag) this.lags[phase] = lag;
    }
  }

  /** The page's figures for a phase, onto its part of the result. */
  private pageFigures(phase: Phase, into: { device_busy?: number; device_lag_ms?: number; device_long_ms?: number } | undefined): void {
    const lag = this.lags[phase];
    if (!lag || !into) return;
    into.device_busy = round3(lag.busy);
    into.device_lag_ms = round3(lag.lag_p95_ms);
    if (lag.long_ms !== undefined) into.device_long_ms = Math.round(lag.long_ms);
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
    this.watch?.start();

    try {
      this.on.phase("latency");
      const idle = await this.timed("latency", () => measureHttpLatency(this.ep, 20, (rtt) => this.on.latency("idle", rtt), signal));
      r.latency = summarize(idle.samples, idle.lost);
      if (idle.protocol) r.client.protocol = idle.protocol;
      this.multiplexed = /^h[23]$/i.test(idle.protocol ?? "");
      this.on.partial(r);
      clearTimings();

      // Opening the probe's own connection, idle, is what each new connection costs.
      const opening = performance.now();
      if (await prober.open(this.ep.wsUrl("/ws"), undefined, this.ep.wsProtocols())) r.latency.setup_ms = performance.now() - opening;
      for (const direction of ["download", "upload"] as const) {
        if (signal.aborted) break;
        this.on.phase(direction);
        r[direction] = await this.timed(direction, () => this.throughput(direction, prober, signal));
        this.pageFigures(direction, r[direction]);
        r.bufferbloat = bufferbloat(r);
        this.judge();
        this.on.partial(r);
        clearTimings();
        if (direction === "download" && r.config.one_stream && !signal.aborted) {
          this.on.phase("one");
          r.one_stream = await this.timed("one", () => this.oneStream(signal));
          this.pageFigures("one", r.one_stream);
          this.on.partial(r);
          clearTimings();
        }
      }

      if (this.config.packets > 0 && !signal.aborted) {
        this.on.phase("loss");
        r.loss = await this.timed("loss", () => this.loss(prober, signal));
        this.pageFigures("loss", r.loss);
        this.judge();
        this.on.partial(r);
      }
    } finally {
      prober.close();
      this.watch?.stop();
    }
    await this.serverFigures();
    this.deviceVerdict();
    if (this.details && this.watch) {
      if (navigator.hardwareConcurrency) r.client.cores = navigator.hardwareConcurrency;
      const pressure = this.watch.pressure;
      if (pressure) r.client.pressure = pressure;
    }

    r.bufferbloat = withheld(bufferbloat(r), this.inflation());
    this.judge();
    r.readiness = readiness(r);
    r.duration_s = (performance.now() - t0) / 1000;
    return r;
  }

  /** The server's CPU while it served each transfer, where it can say: a server at a full
   * core set the rate, not the network. For a download, how long this device held the
   * server back, and the kernel's round-trip time while it ran. */
  private async serverFigures(): Promise<void> {
    if (!this.info.capabilities.test_cpu) return;
    const r = this.result;
    type Window = { held: number; rtt_ms: number | null; min_rtt_ms: number | null; bound: boolean };
    try {
      const parts = await this.ep.json<Partial<Record<"download" | "one_stream" | "upload", { cpu?: number; window?: Window }>>>(`/tests/${this.test}`);
      for (const name of ["download", "one_stream", "upload"] as const) {
        const t = r[name];
        const part = parts[name];
        if (!t || !part) continue;
        if (typeof part.cpu === "number") t.server_cpu = part.cpu;
        const w = part.window;
        if (w && typeof w.held === "number") {
          t.device_window = w.held;
          if (typeof w.rtt_ms === "number") t.server_rtt_ms = w.rtt_ms;
          if (typeof w.min_rtt_ms === "number") t.server_min_rtt_ms = w.min_rtt_ms;
          if (w.bound) t.device_bound = "window";
        }
      }
    } catch {
      /* the result goes on without it */
    }
  }

  /** A download the server had no figure for (HTTP/3, or an older server) is Device-bound
   * by the page's own reads: Chromium's came full, so the bytes waited for the page. */
  private deviceVerdict(): void {
    const percent = this.info.device?.bound_percent ?? 10;
    for (const t of [this.result.download, this.result.one_stream]) {
      if (!t || t.device_window !== undefined || !percent || (t.server_cpu ?? 0) >= 0.9) continue;
      if ((t.device_full_reads ?? 0) >= FULL_READS_BOUND) t.device_bound = "page";
    }
  }

  /** How much the page's own lateness added to each direction's latency under load,
   * beyond what it added at idle: the medians' difference. */
  private inflation(): { download: number; upload: number } {
    const idle = this.lags.latency?.lag_median_ms ?? 0;
    const added = (phase: Phase) => Math.max(0, (this.lags[phase]?.lag_median_ms ?? idle) - idle);
    return { download: added("download"), upload: added("upload") };
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
    const raw: ThroughputRaw = await measure(this.ep, {
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
      ...fullReads(raw),
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
      transfer: "one_stream",
      signal,
      onTick: (live) => this.on.throughput("one", live),
    });
    return {
      direction: "download",
      ...summarizeThroughput(raw.bins, BIN_MS, seconds),
      streams: 1,
      per_stream_mbps: raw.perStream.map((b) => (b * 8) / (raw.elapsed * 1e6)),
      loaded_latency: null,
      ...fullReads(raw),
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
    if (!prober.ready) await prober.open(this.ep.wsUrl("/ws"), undefined, this.ep.wsProtocols());
    const result = await measureLossWs(prober, opts);
    result.note = `${result.note} (${reason})`;
    return result;
  }
}
