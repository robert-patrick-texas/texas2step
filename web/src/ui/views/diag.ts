// Diagnostics: every way this browser can reach the server, tried once a second while the
// page is shown: HTTPS, WebSocket and UDP, over IPv4 and over IPv6. Each answer also
// says what the server saw: the client's address and port, the HTTP version, TLS, the
// server's own TCP round trip, and its clock.
import { eui64Mac } from "../../engine/addr";
import { WsProber } from "../../engine/latency";
import { NotOffered, RtcProbe, type UdpAddress } from "../../engine/loss";
import { Endpoint } from "../../engine/net";
import { percentile } from "../../engine/stats";
import type { DiagHttp } from "../../types";
import type { App, View } from "../app";
import { awakeSwitch } from "../awake";
import { drawTimeline, Lane, type Sample, type Tone } from "../charts/diag";
import { clear, h, onResize, setText } from "../dom";
import { ms, text } from "../format";

type Fam = "IPv4" | "IPv6";
type Kind = "https" | "ws" | "udp";

const KEEP_MS = 300_000; // five minutes of samples, for the timelines
const RECENT_MS = 60_000; // the figures beside each row
const LOG_MAX = 300;
const UDP_RETRY_MS = 10_000; // a WebRTC session takes seconds to open and holds a server port
const WS_RETRY_MS = 2_000;
const KINDS: Record<Kind, string> = { https: "HTTPS", ws: "WebSocket", udp: "UDP" };

const httpName = (p: string | null | undefined) =>
  ({ h2: "HTTP/2", "http/1.1": "HTTP/1.1", h3: "HTTP/3", "HTTP/2.0": "HTTP/2" })[p ?? ""] ?? (p || "");
// "address : port", spaced so the port can't be read as part of an IPv4 or IPv6 address.
const hostPort = (ip: string | null | undefined, port: number | null | undefined) => (!ip ? "—" : port ? `${ip} : ${port}` : ip);
const seconds = (msecs: number) => `${Math.max(1, Math.round(msecs / 1000))} s`;

/** One way of reaching the server, with what its answers have shown. */
abstract class Method {
  tone: Tone = "trying";
  reason = ""; // why it isn't offered
  detail = ""; // what the latest answer said
  port = "";
  samples: Sample[] = [];
  lostRun = 0;
  downSince = 0;
  stopped = false;
  private timer = 0;
  readonly lane = new Lane();
  readonly row: { root: HTMLElement; badge: HTMLElement; figs: HTMLElement; detail: HTMLElement; port: HTMLElement; timeline: HTMLCanvasElement; width: number };

  constructor(
    readonly fam: Fam | "Auto",
    readonly kind: Kind,
    protected readonly view: DiagView,
    protected readonly retryMs: number,
  ) {
    const timeline = h("canvas", { class: "diag-timeline", role: "img", "aria-label": `${this.label}: answers over the last five minutes` });
    const port = h("span", { class: "diag-port" });
    this.row = {
      root: h("div", { class: "diag-row" }),
      badge: h("span", { class: "diag-badge" }),
      figs: h("span", { class: "diag-figs" }),
      detail: h("div", { class: "diag-detail" }),
      port,
      timeline,
      width: 0,
    };
    const r = this.row;
    r.root.append(
      h("div", { class: "diag-what" }, h("strong", {}, this.fam === "Auto" ? "This address" : this.fam), h("span", {}, KINDS[kind]), port),
      this.lane.el,
      h("div", { class: "diag-state" }, h("div", {}, r.badge, r.figs), r.detail),
      timeline,
    );
    onResize(timeline, (w) => {
      r.width = w;
      drawTimeline(timeline, this.samples, w);
    });
  }

  get label(): string {
    return `${this.fam === "Auto" ? "This address" : this.fam} ${KINDS[this.kind]}`;
  }

  /** One attempt; made about once a second unless the method isn't offered. */
  abstract tick(): void;

  /**
   * Attempt about once a second, starting `phase` ms from now. Each wait is 850 to 1150 ms,
   * so the methods drift apart: their packets leave at different moments, on the wire as
   * on screen, instead of all at once.
   */
  start(phase: number): void {
    const step = () => {
      if (this.stopped) return;
      if (this.tone !== "off") this.tick();
      this.timer = window.setTimeout(step, 850 + Math.random() * 300);
    };
    this.timer = window.setTimeout(step, phase);
  }

  stop(): void {
    this.pause();
    this.lane.dispose();
  }

  /** No attempts and no connection held open, until resumed; the answers so far stay. */
  pause(): void {
    this.stopped = true;
    clearTimeout(this.timer);
  }

  /** Start again after a pause: it has to answer anew, so it's back to trying. */
  resume(phase: number): void {
    this.stopped = false;
    if (this.tone !== "off") {
      this.tone = "trying";
      this.lostRun = 0;
      this.lane.setTone("trying");
    }
    this.start(phase);
  }

  off(reason: string): void {
    this.tone = "off";
    this.reason = reason;
    this.lane.setTone("off");
  }

  /** An attempt's outcome. Once a method has answered, a miss counts as loss. */
  record(ok: boolean, rtt: number | null): void {
    if (this.stopped || this.tone === "off") return;
    const now = Date.now();
    const up = this.tone === "up" || this.tone === "loss";
    this.samples.push({ t: now, ok, rtt, up });
    while (this.samples.length && this.samples[0]!.t < now - KEEP_MS) this.samples.shift();
    if (ok) {
      if (this.tone === "trying") this.view.log(this, `answering${rtt !== null ? `, ${text(ms(rtt))}` : ""}`);
      if (this.tone === "loss") this.view.log(this, `answering again after ${seconds(now - this.downSince)}, ${this.lostRun} lost`);
      this.tone = "up";
      this.lostRun = 0;
    } else if (this.tone === "up") {
      this.tone = "loss";
      this.downSince = now;
      this.lostRun = 1;
      this.view.log(this, "no answer");
    } else if (this.tone === "loss") {
      this.lostRun++;
    }
    this.lane.setTone(this.tone);
  }

  render(): void {
    const r = this.row;
    r.root.dataset.tone = this.tone;
    setText(r.port, this.port);
    const recent = this.samples.filter((s) => s.t >= Date.now() - RECENT_MS);
    const got = recent.filter((s) => s.ok && s.rtt !== null).map((s) => s.rtt!);
    const lost = recent.filter((s) => !s.ok && s.up).length;
    const latest = this.samples[this.samples.length - 1];
    if (this.tone === "off") {
      setText(r.badge, "Not offered");
      setText(r.figs, "");
      setText(r.detail, this.reason);
    } else if (this.tone === "trying") {
      setText(r.badge, "No answer yet");
      setText(r.figs, this.retryMs > 1000 ? `retrying every ${seconds(this.retryMs)}` : "retrying every second");
      setText(r.detail, this.detail);
    } else {
      setText(r.badge, this.tone === "up" ? "Answering" : `No answer for ${seconds(Date.now() - this.downSince)}`);
      const now = latest?.ok && latest.rtt !== null ? text(ms(latest.rtt)) : "—";
      const median = got.length ? text(ms(percentile(got, 50))) : "—";
      setText(r.figs, [this.tone === "up" ? now : "", `median ${median}`, `${lost} of ${recent.length} lost in 1 min`].filter(Boolean).join(" · "));
      setText(r.detail, this.detail);
    }
    drawTimeline(r.timeline, this.samples, r.width);
  }
}

/** A small HTTPS request a second, on the browser's open connection to that address. */
class HttpsMethod extends Method {
  private seq = 0;

  constructor(
    fam: Fam | "Auto",
    view: DiagView,
    private readonly ep: Endpoint,
  ) {
    super(fam, "https", view, 1000);
    this.port = `TCP ${new URL(ep.base).port || (ep.base.startsWith("https") ? "443" : "80")}`;
  }

  tick(): void {
    const url = this.ep.url("/diag/http", { n: this.seq++ });
    const done = this.lane.send();
    const t0 = performance.now();
    const wall = Date.now();
    fetch(url, { cache: "no-store", signal: AbortSignal.timeout(2000) })
      .then(async (res) => {
        if (!res.ok) throw new Error(`${res.status}`);
        return (await res.json()) as DiagHttp;
      })
      .then((d) => {
        const rtt = performance.now() - t0;
        // The browser's own word on the HTTP version (the server reports nginx's too).
        const timing = performance.getEntriesByName(url).pop() as PerformanceResourceTiming | undefined;
        performance.clearResourceTimings();
        const http = httpName(timing?.nextHopProtocol) || httpName(d.http_version) || "HTTP";
        if (d.server.port) this.port = `TCP ${d.server.port}`;
        const parts = [http, d.tls ? `${d.tls.protocol} ${d.tls.cipher}` : "", d.server.ip ? `to ${d.server.ip}` : ""];
        if (d.tcp_rtt_ms !== null) parts.push(`server's TCP RTT ${text(ms(d.tcp_rtt_ms))}`);
        this.detail = parts.filter(Boolean).join(" · ");
        done(true);
        this.view.answered(d, http, rtt, wall);
        this.record(true, rtt);
      })
      .catch(() => {
        done(false);
        this.record(false, null);
      });
  }
}

/** An echo a second over a WebSocket: its own TCP connection, opened by an HTTP/1.1 upgrade. */
class WsMethod extends Method {
  private prober: WsProber | null = null;
  private next = 0;
  private opening = false;

  constructor(
    fam: Fam | "Auto",
    view: DiagView,
    private readonly ep: Endpoint,
  ) {
    super(fam, "ws", view, 1000);
    this.port = `TCP ${new URL(ep.base).port || (ep.base.startsWith("https") ? "443" : "80")}`;
    this.detail = "its own TCP connection, opened by an HTTP/1.1 upgrade";
  }

  tick(): void {
    const done = this.lane.send();
    if (this.prober?.ready) {
      void this.prober.ping(1500).then((rtt) => {
        done(rtt !== null);
        this.record(rtt !== null, rtt);
      });
      return;
    }
    // Not open: the second goes unanswered, and the socket is opened again.
    done(false);
    this.record(false, null);
    if (this.opening || Date.now() < this.next) return;
    this.opening = true;
    this.next = Date.now() + WS_RETRY_MS;
    this.prober?.close();
    const prober = new WsProber();
    this.prober = prober;
    void prober.open(this.ep.wsUrl("/ws"), 3000).then(() => {
      this.opening = false;
      if (this.stopped) prober.close();
    });
  }

  override pause(): void {
    super.pause();
    this.prober?.close();
  }

  override resume(phase: number): void {
    this.next = 0; // its socket was closed by the pause, not lost: open it again now
    super.resume(phase);
  }
}

/** A probe a second over a WebRTC data channel held to one address family. */
class UdpMethod extends Method {
  private rtc: RtcProbe | null = null;
  private next = 0;
  private connecting = false;
  private seq = 0;
  private waiting = new Map<number, (rtt: number | null) => void>();

  constructor(
    fam: Fam,
    view: DiagView,
    private readonly ep: Endpoint,
    private readonly ice: RTCIceServer[],
  ) {
    super(fam, "udp", view, UDP_RETRY_MS);
    this.port = "UDP";
  }

  tick(): void {
    const done = this.lane.send();
    const rtc = this.rtc;
    if (rtc?.open) {
      const seq = this.seq++ >>> 0;
      const finish = (rtt: number | null) => {
        clearTimeout(timer);
        this.waiting.delete(seq);
        done(rtt !== null);
        this.record(rtt !== null, rtt);
      };
      const timer = setTimeout(() => finish(null), 1500);
      this.waiting.set(seq, finish);
      rtc.send(seq, 64);
      return;
    }
    if (rtc) {
      rtc.close(); // the channel closed under us
      this.rtc = null;
    }
    done(false);
    this.record(false, null);
    if (this.connecting || Date.now() < this.next) return;
    this.connecting = true;
    this.next = Date.now() + UDP_RETRY_MS;
    RtcProbe.connect(this.ep, this.ice, 8000)
      .then(async (probe) => {
        if (this.stopped) return probe.close();
        probe.onEcho = (seq, rtt) => this.waiting.get(seq)?.(rtt);
        this.rtc = probe;
        const [path, seen] = await Promise.all([probe.path(), probe.address()]);
        if (path?.port) this.port = `UDP ${path.port}`;
        this.detail = [path ? `to ${path.address}` : "", seen?.ip ? `seen from ${hostPort(seen.ip, seen.port)}` : ""].filter(Boolean).join(" · ") || "WebRTC data channel, no retransmits";
        this.view.udpPath(this.fam as Fam, path?.address ?? null, path?.port ?? null, seen, path?.local ?? null);
      })
      .catch((err: unknown) => {
        if (err instanceof NotOffered && !this.stopped) {
          this.off(err.message);
          this.view.log(this, `not offered: ${err.message}`);
        }
      })
      .finally(() => {
        this.connecting = false;
      });
  }

  override pause(): void {
    super.pause();
    this.rtc?.close();
    this.rtc = null;
  }

  override resume(phase: number): void {
    this.next = 0; // the pause closed its session and freed the server's port
    super.resume(phase);
  }
}

interface Event {
  t: number;
  who: string;
  text: string;
}

export class DiagView implements View {
  private methods: Method[] = [];
  private timer = 0;
  private off: (() => void) | null = null;
  private https: Partial<Record<Fam, DiagHttp>> = {};
  private udp: Partial<Record<Fam, { ip: string | null; port: number | null; seen: UdpAddress | null; local: string | null }>> = {};
  private offsets: number[] = [];
  private offsetSpread: number[] = [];
  private events: Event[] = [];
  private logShown = -1;
  private away = false; // another page is shown
  private stale = false; // the server was changed while away
  private awakeOpt: ReturnType<typeof awakeSwitch> = null; // Keep screen on
  private els!: { you: HTMLElement; server: HTMLElement; clock: HTMLElement; rows: HTMLElement; egress: HTMLElement; log: HTMLElement };
  private readonly onVisibility = () => this.log(null, document.hidden ? "tab hidden: browsers slow down timers in background tabs, so gaps may follow" : "tab visible again");

  constructor(private readonly app: App) {}

  mount(root: HTMLElement): void {
    const copy = h("button", { class: "btn small", type: "button" }, "Copy log");
    copy.addEventListener("click", () => {
      const lines = [...this.events].reverse().map((e) => `${new Date(e.t).toLocaleTimeString()}  ${e.who ? `${e.who}: ` : ""}${e.text}`);
      navigator.clipboard?.writeText(lines.join("\n")).then(
        () => setText(copy, "Copied"),
        () => setText(copy, "Copy failed"),
      );
      setTimeout(() => setText(copy, "Copy log"), 2000);
    });
    this.els = {
      you: h("div", { class: "diag-you" }),
      server: h("div", { class: "diag-server" }),
      clock: h("div", { class: "diag-clock" }),
      rows: h("div", { class: "diag-rows" }),
      egress: h("div", { class: "egress" }),
      log: h("ol", { class: "diag-log" }),
    };
    const card = (title: string, note: string, ...body: (HTMLElement | null)[]) =>
      h("section", { class: "card diag-card" }, h("header", { class: "card-head head-inline" }, h("h2", {}, title), note ? h("p", { class: "wide" }, note) : null), ...body);
    this.awakeOpt = awakeSwitch(this.app);
    root.append(
      h(
        "section",
        { class: "page-head" },
        h("div", {}, h("h1", {}, "Diag"), h("p", { class: "lede wide" }, "Every way this browser can reach the server, tried once a second while this page is shown: HTTPS, WebSocket and UDP, over IPv4 and IPv6.")),
        this.awakeOpt ? h("div", { class: "head-actions" }, this.awakeOpt.el) : null,
      ),
      h("section", { class: "card diag-card diag-addresses" }, this.els.you, this.els.server, this.els.clock),
      card(
        "Connection methods",
        "Each ping leaves on the left and its pong comes back from the server on the right. Green: answering. Red: answered before, not now. Grey: no answer yet, or not offered.",
        this.els.rows,
        h(
          "p",
          { class: "diag-note" },
          "HTTP/2 carries every request to a server over one TCP connection, as streams inside it; HTTP/1.1 opens a connection for each request in flight, up to six per server. So the Test page's parallel streams are separate TCP connections only over HTTP/1.1. A WebSocket always opens its own connection. Each second's HTTPS attempt is a request on the browser's open connection, so a path that breaks shows as requests without answers.",
        ),
      ),
      h(
        "div",
        { class: "grid" },
        card(
          "NAT and egress",
          "Your address and port as the server sees them, over TCP and over UDP, and this computer's own address where the browser reveals it.",
          this.els.egress,
          h("p", { class: "diag-note" }, "Behind NAT the ports differ, as they should. Different addresses mean TCP and UDP leave by different routes or NAT pools, as with several internet links or carrier-grade NAT."),
        ),
        h("section", { class: "card" }, h("header", { class: "card-head head-inline diag-log-head" }, h("h2", {}, "Events"), copy), this.els.log),
      ),
    );
    this.off = this.app.onServer(() => {
      if (this.away) this.stale = true;
      else this.restart();
    });
    document.addEventListener("visibilitychange", this.onVisibility);
    this.restart();
    this.timer = window.setInterval(() => this.render(), 1000);
    this.app.awake.hold("diag"); // the screen stays on while Diag is shown
  }

  /** Another page is shown: the attempts pause, and what they found stays. */
  hide(): void {
    this.away = true;
    clearInterval(this.timer);
    for (const m of this.methods) m.pause();
    document.removeEventListener("visibilitychange", this.onVisibility);
    this.log(null, "paused while another page is shown");
    this.app.awake.hold("diag", false);
  }

  show(): void {
    this.away = false;
    this.app.awake.hold("diag");
    this.awakeOpt?.mark(); // it may have changed on another page
    document.addEventListener("visibilitychange", this.onVisibility);
    if (this.stale) {
      this.stale = false;
      this.restart(); // another server was chosen meanwhile
    } else {
      this.log(null, "resumed");
      for (const [i, m] of this.methods.entries()) m.resume((i * 1000) / this.methods.length);
      this.render();
    }
    this.timer = window.setInterval(() => this.render(), 1000);
  }

  unmount(): void {
    clearInterval(this.timer);
    for (const m of this.methods) m.stop();
    this.off?.();
    document.removeEventListener("visibilitychange", this.onVisibility);
    this.app.awake.hold("diag", false);
  }

  // ---- Methods -------------------------------------------------------------------

  /** Build the methods for the selected server: on first load and when it changes. */
  private restart(): void {
    for (const m of this.methods) m.stop();
    this.methods = [];
    this.https = {};
    this.udp = {};
    this.offsets = [];
    this.offsetSpread = [];
    const { info, selected: loc } = this.app;
    clear(this.els.rows);
    if (!info) {
      this.els.rows.append(h("p", { class: "status" }, "Waiting for the server to answer…"));
      this.render();
      return;
    }
    this.log(null, `checking ${loc?.name ?? info.server.name}`);
    const main = new Endpoint(loc?.url ?? "");
    const single = { IPv4: loc?.url_v4 ? new Endpoint(loc.url_v4, "ipv4") : null, IPv6: loc?.url_v6 ? new Endpoint(loc.url_v6, "ipv6") : null };
    // UDP signals over this page's own address; the probe itself is held to one family.
    const udp = (fam: Fam) => {
      const m = new UdpMethod(fam, this, new Endpoint(loc?.url ?? "", fam === "IPv4" ? "ipv4" : "ipv6"), info.ice_servers);
      if (!info.capabilities.webrtc) m.off("WebRTC is turned off on this server (webrtc_enabled)");
      return m;
    };
    const ws = (fam: Fam | "Auto", ep: Endpoint) => {
      const m = new WsMethod(fam, this, ep);
      if (!info.capabilities.websocket) m.off("WebSockets are turned off on this server");
      return m;
    };
    if (single.IPv4 || single.IPv6) {
      for (const fam of ["IPv4", "IPv6"] as const) {
        const ep = single[fam];
        const group = [new HttpsMethod(fam, this, ep ?? main), ws(fam, ep ?? main), udp(fam)];
        if (!ep) {
          const why = `this server has no ${fam}-only address (server_url_${fam === "IPv4" ? "v4" : "v6"})`;
          for (const m of group.slice(0, 2)) m.off(why);
        }
        this.methods.push(...group);
      }
    } else {
      // Without single-family names, HTTP goes wherever the browser takes this address.
      this.methods.push(new HttpsMethod("Auto", this, main), ws("Auto", main), udp("IPv4"), udp("IPv6"));
    }
    for (const [i, m] of this.methods.entries()) {
      this.els.rows.append(m.row.root);
      m.start((i * 1000) / this.methods.length);
    }
    this.render();
  }

  // ---- What the answers say ------------------------------------------------------

  answered(d: DiagHttp, _http: string, rtt: number, sentAt: number): void {
    const fam = d.client.family;
    if (fam) this.https[fam] = d;
    // The server's clock against this browser's, from the middle of the round trip.
    this.offsets.push(d.time_ms - (sentAt + rtt / 2));
    this.offsetSpread.push(rtt / 2);
    if (this.offsets.length > 30) {
      this.offsets.shift();
      this.offsetSpread.shift();
    }
  }

  udpPath(fam: Fam, ip: string | null, port: number | null, seen: UdpAddress | null, local: string | null): void {
    this.udp[fam] = { ip, port, seen, local };
  }

  log(who: Method | null, text: string): void {
    this.events.push({ t: Date.now(), who: who?.label ?? "", text });
    if (this.events.length > LOG_MAX) this.events.shift();
  }

  // ---- Rendering -----------------------------------------------------------------

  private render(): void {
    for (const m of this.methods) m.render();
    this.renderAddresses();
    this.renderEgress();
    this.renderLog();
  }

  private renderAddresses(): void {
    const { you, server, clock } = this.els;
    clear(you);
    for (const fam of ["IPv4", "IPv6"] as const) {
      const ip = this.https[fam]?.client.ip ?? this.udp[fam]?.seen?.ip ?? null;
      you.append(h("div", { class: "diag-addr" }, h("span", { class: "diag-fam" }, `Your ${fam}`), h("span", { class: `diag-ip ${fam === "IPv4" ? "v4" : "v6"}${ip ? "" : " none"}` }, ip ?? `no answer over ${fam} yet`)));
    }
    const servers = (["IPv4", "IPv6"] as const)
      .map((fam) => {
        const httpsIp = this.https[fam]?.server.ip;
        const udp = this.udp[fam];
        const ip = httpsIp ?? udp?.ip;
        if (!ip) return "";
        const via = [this.https[fam]?.server.port ? `TCP ${this.https[fam]!.server.port}` : "", udp?.port ? `UDP ${udp.port}` : ""].filter(Boolean).join(", ");
        return `${fam} ${ip}${via ? ` (${via})` : ""}`;
      })
      .filter(Boolean);
    setText(server, servers.length ? `Server: ${servers.join(" · ")}` : "Server: —");
    if (this.offsets.length) {
      const offset = percentile(this.offsets, 50);
      const spread = percentile(this.offsetSpread, 50);
      const way = offset >= 0 ? "behind" : "ahead of";
      setText(clock, `Clocks: this browser is ${text(ms(Math.abs(offset)))} ${way} the server (± ${text(ms(spread))})`);
    } else {
      setText(clock, "");
    }
  }

  private renderEgress(): void {
    clear(this.els.egress);
    for (const fam of ["IPv4", "IPv6"] as const) {
      const tcp = this.https[fam]?.client;
      const udp = this.udp[fam]?.seen;
      // This computer's own address on the UDP path: only where the browser reveals it.
      const local = this.udp[fam]?.local ?? null;
      let verdict = "—";
      if (tcp?.ip && udp?.ip) {
        if (tcp.ip !== udp.ip) verdict = "Different addresses";
        else if (tcp.port && udp.port && tcp.port !== udp.port) verdict = "Same address, other port";
        else verdict = "Same address";
      }
      const outside = udp?.ip ?? tcp?.ip;
      const mac = fam === "IPv6" ? [local, tcp?.ip, udp?.ip].map(eui64Mac).find(Boolean) : null;
      const row = (term: string, value: string | HTMLElement, mono = true) => [h("dt", {}, term), h("dd", mono ? { class: "mono" } : {}, value)];
      this.els.egress.append(
        h(
          "div",
          { class: "egress-fam" },
          h("h3", {}, fam),
          h(
            "dl",
            {},
            ...(local ? row("This computer", local) : []),
            ...row("Over HTTPS (TCP)", hostPort(tcp?.ip, tcp?.port)),
            ...row("Over UDP", hostPort(udp?.ip, udp?.port)),
            ...row("Compared", verdict, false),
            ...(local && outside ? row("NAT", local === outside ? "none: same address" : `${local} → ${outside}`) : []),
            ...(mac ? row("MAC", h("span", {}, mac, h("span", { class: "egress-note" }, "read from the IPv6 address (EUI-64)"))) : []),
          ),
        ),
      );
    }
  }

  private renderLog(): void {
    if (this.logShown === this.events.length && this.events.length < LOG_MAX) return;
    this.logShown = this.events.length;
    clear(this.els.log);
    for (const e of [...this.events].reverse().slice(0, 100)) {
      this.els.log.append(h("li", {}, h("time", {}, new Date(e.t).toLocaleTimeString()), e.who ? h("strong", {}, e.who) : null, h("span", {}, e.text)));
    }
    if (!this.events.length) this.els.log.append(h("li", { class: "none" }, "Nothing yet."));
  }
}
