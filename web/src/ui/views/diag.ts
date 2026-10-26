// Diagnostics: every way this browser can reach the server, tried once a second while the
// page is shown: HTTP/1.1, HTTP/2 and HTTP/3, WebSocket and UDP, over IPv4 and over IPv6,
// as the server offers them. Each answer also says what the server saw: the client's
// address and port, the HTTP version, TLS, the server's own TCP round trip, and its clock.
import { eui64Mac } from "../../engine/addr";
import { WsProber } from "../../engine/latency";
import { NotOffered, RtcProbe, type UdpAddress } from "../../engine/loss";
import { Endpoint } from "../../engine/net";
import { percentile } from "../../engine/stats";
import type { DiagHttp, Location } from "../../types";
import type { App, View } from "../app";
import { awakeSwitch } from "../awake";
import { drawTimeline, Lane, type Sample, type Tone } from "../charts/diag";
import { clear, h, onResize, setText } from "../dom";
import { ms, text } from "../format";

type Fam = "IPv4" | "IPv6";
type Http = "http1" | "http2" | "http3";
type Kind = Http | "ws" | "udp";

const KEEP_MS = 300_000; // five minutes of samples, for the timelines
const RECENT_MS = 60_000; // the figures beside each row
const LOG_MAX = 300;
const UDP_RETRY_MS = 10_000; // a WebRTC session takes seconds to open and holds a server port
const WS_RETRY_MS = 2_000;
const CODE_RETRY_MS = 10_000; // an address that asks for the access code, until it's in
// Answers over TCP on the HTTP/3 row's name before it says the browser stays on TCP:
// browsers ask over TCP first, and move once the server's Alt-Svc says it offers HTTP/3.
const H3_WAIT_MS = 10_000;
const KINDS: Record<Kind, string> = { http1: "HTTP/1.1", http2: "HTTP/2", http3: "HTTP/3", ws: "WebSocket", udp: "UDP" };

/** The browser's or the server's word for an HTTP version, as the rows name them. */
function httpName(p: string | null | undefined): string {
  const s = p ?? "";
  if (/^(h3|http\/3)/i.test(s)) return "HTTP/3";
  if (/^(h2|http\/2)/i.test(s)) return "HTTP/2";
  if (/^http\/1\.1$/i.test(s)) return "HTTP/1.1";
  return s;
}

/** Why a name answered over another HTTP version than its row's (HTTP/1.1 or HTTP/2). */
function mismatch(want: Http, got: string): string {
  if (got === "HTTP/3") return "this name offers HTTP/3 (Alt-Svc)";
  return want === "http2" ? "this name doesn't offer HTTP/2" : "this name offers HTTP/2 too";
}

// "address : port", spaced so the port can't be read as part of an IPv4 or IPv6 address.
const hostPort = (ip: string | null | undefined, port: number | null | undefined) => (!ip ? "—" : port ? `${ip} : ${port}` : ip);
const seconds = (msecs: number) => `${Math.max(1, Math.round(msecs / 1000))} s`; // never wrapped apart
const famName = (fam: Fam | "Auto") => (fam === "Auto" ? "This address" : fam);

/** One way of reaching the server, with what its answers have shown. */
abstract class Method {
  tone: Tone = "trying";
  reason = ""; // why it isn't offered
  detail = ""; // what the latest answer said
  warn = false; // the detail tells of a fault in how the server is set up
  waiting = ""; // the badge while trying, where "No answer yet" isn't the whole story
  fellBack = false; // the HTTP/3 row: lately answered, but over TCP
  port = "";
  server = ""; // the server's address that answered
  samples: Sample[] = [];
  lostRun = 0;
  downSince = 0;
  stopped = false;
  private timer = 0;
  private codeLogged = false; // the log says this address requires the access code
  readonly lane = new Lane();
  readonly row: {
    root: HTMLElement;
    badge: HTMLElement;
    figs: HTMLElement;
    detail: HTMLElement;
    code: HTMLButtonElement;
    port: HTMLElement;
    timeline: HTMLCanvasElement;
    width: number;
  };

  constructor(
    readonly fam: Fam | "Auto",
    readonly kind: Kind,
    protected readonly view: DiagView,
    readonly ep: Endpoint,
    protected readonly retryMs: number,
  ) {
    const timeline = h("canvas", { class: "diag-timeline", role: "img", "aria-label": `${this.label}: answers over the last five minutes` });
    const port = h("span", { class: "diag-port" });
    this.row = {
      root: h("div", { class: "diag-row" }),
      badge: h("span", { class: "diag-badge" }),
      figs: h("span", { class: "diag-figs" }),
      detail: h("span", {}),
      code: h("button", { class: "btn small diag-code", type: "button", hidden: true }, "Enter access code"),
      port,
      timeline,
      width: 0,
    };
    const r = this.row;
    r.code.addEventListener("click", () => void this.view.enterCode(this.ep));
    r.root.append(
      h("div", { class: "diag-what" }, h("strong", {}, KINDS[kind]), port),
      this.lane.el,
      h("div", { class: "diag-state" }, h("div", {}, r.badge, r.figs), h("div", { class: "diag-detail" }, r.detail, r.code)),
      timeline,
    );
    onResize(timeline, (w) => {
      r.width = w;
      drawTimeline(timeline, this.samples, w);
    });
  }

  get label(): string {
    return `${famName(this.fam)} ${KINDS[this.kind]}`;
  }

  /** Its address refused a request for want of the access code, which isn't in yet. */
  get refused(): boolean {
    return this.tone !== "off" && this.ep.needsCode;
  }

  /**
   * Its address asks for the access code: until it's in, attempts wait, and the seconds
   * don't count as loss. Once it answers, it's answering anew.
   */
  protected needsCode(): void {
    if (this.tone === "off") return;
    if (!this.codeLogged) this.view.log(this, "access code required");
    this.codeLogged = true;
    this.tone = "trying";
    this.lostRun = 0;
    this.lane.setTone("trying");
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

  /** An attempt's outcome. Once a method has answered, a miss counts as loss; `why` is
   * what the log says when answers stop. */
  record(ok: boolean, rtt: number | null, why = "no answer"): void {
    if (this.stopped || this.tone === "off") return;
    const now = Date.now();
    const up = this.tone === "up" || this.tone === "loss";
    this.samples.push({ t: now, ok, rtt, up });
    while (this.samples.length && this.samples[0]!.t < now - KEEP_MS) this.samples.shift();
    if (ok) {
      this.codeLogged = false;
      if (this.tone === "trying") this.view.log(this, `answering${rtt !== null ? `, ${text(ms(rtt))}` : ""}`);
      if (this.tone === "loss") this.view.log(this, `answering again after ${seconds(now - this.downSince)}, ${this.lostRun} lost`);
      this.tone = "up";
      this.lostRun = 0;
    } else if (this.tone === "up") {
      this.tone = "loss";
      this.downSince = now;
      this.lostRun = 1;
      this.view.log(this, why);
    } else if (this.tone === "loss") {
      this.lostRun++;
    }
    this.lane.setTone(this.tone);
  }

  /** `button`: this row offers the code's button (the first row its address refuses). */
  render(button = false): void {
    const r = this.row;
    r.root.dataset.tone = this.refused ? "code" : this.tone;
    r.code.hidden = !button;
    setText(r.port, this.port);
    const recent = this.samples.filter((s) => s.t >= Date.now() - RECENT_MS);
    const got = recent.filter((s) => s.ok && s.rtt !== null).map((s) => s.rtt!);
    const lost = recent.filter((s) => !s.ok && s.up).length;
    const latest = this.samples[this.samples.length - 1];
    r.detail.classList.toggle("diag-warn", this.warn && !this.refused && this.tone !== "off");
    if (this.refused) {
      setText(r.badge, "Restricted");
      setText(r.figs, this.kind.startsWith("http") ? `checking every ${seconds(CODE_RETRY_MS)}` : "");
      setText(r.detail, "access code required");
    } else if (this.tone === "off") {
      setText(r.badge, "Not offered");
      setText(r.figs, "");
      setText(r.detail, this.reason);
    } else if (this.tone === "trying") {
      setText(r.badge, this.waiting || "No answer yet");
      setText(r.figs, this.retryMs > 1000 ? `retrying every ${seconds(this.retryMs)}` : "retrying every second");
      setText(r.detail, this.detail);
    } else {
      setText(r.badge, this.tone === "up" ? "Answering" : `${this.fellBack ? "Over TCP" : "No answer"} for ${seconds(Date.now() - this.downSince)}`);
      const now = latest?.ok && latest.rtt !== null ? text(ms(latest.rtt)) : "—";
      const median = got.length ? text(ms(percentile(got, 50))) : "—";
      setText(r.figs, [this.tone === "up" ? now : "", `median ${median}`, `${lost} of ${recent.length} lost in 1 min`].filter(Boolean).join(" · "));
      setText(r.detail, this.detail);
    }
    drawTimeline(r.timeline, this.samples, r.width);
  }
}

/**
 * A small HTTPS request a second, on the browser's open connection to that name. The name
 * holds it to one HTTP version: its row counts answers over another version as answers
 * (HTTP/1.1, HTTP/2) and says so, except HTTP/3's, where the version is the path tested.
 */
class HttpsMethod extends Method {
  private seq = 0;
  private recheck = 0; // while its address asks for the code, the next try
  private tcpSince = 0; // the HTTP/3 row, not yet answering: its first answer over TCP
  private tcpLogged = false;

  constructor(
    fam: Fam | "Auto",
    private readonly want: Http,
    view: DiagView,
    ep: Endpoint,
  ) {
    super(fam, want, view, ep, 1000);
    const port = new URL(ep.base).port || (ep.base.startsWith("https") ? "443" : "80");
    this.port = `${want === "http3" ? "UDP" : "TCP"} ${port}`;
  }

  tick(): void {
    if (this.ep.needsCode && Date.now() < this.recheck) return;
    const url = this.ep.url("/diag/http", { n: this.seq++ });
    const done = this.lane.send();
    const t0 = performance.now();
    const wall = Date.now();
    fetch(url, this.ep.init({ cache: "no-store", signal: AbortSignal.timeout(2000) }))
      .then(async (res) => {
        if (res.status === 401) {
          // Not lost: refused for want of a pass, which the page asks for (app.ts).
          done(false);
          this.recheck = Date.now() + CODE_RETRY_MS;
          void this.ep.refused();
          this.needsCode();
          return null;
        }
        if (!res.ok) throw new Error(`${res.status}`);
        return (await res.json()) as DiagHttp;
      })
      .then((d) => {
        if (!d) return;
        const rtt = performance.now() - t0;
        // The browser's own word on the HTTP version, else the server's (nginx's).
        const timing = performance.getEntriesByName(url).pop() as PerformanceResourceTiming | undefined;
        performance.clearResourceTimings();
        const http = httpName(timing?.nextHopProtocol) || httpName(d.http_version) || "HTTP";
        const quic = http === "HTTP/3";
        this.view.answered(d, http, this.want, rtt, wall);
        if (d.server.ip) this.server = d.server.ip;
        const parts = [d.tls ? `${d.tls.protocol} ${d.tls.cipher}` : "", d.server.ip ? `to ${d.server.ip}` : ""];
        if (d.tcp_rtt_ms !== null) parts.push(`server's TCP RTT ${text(ms(d.tcp_rtt_ms))}`);
        if (this.want === "http3" && !quic) return this.overTcp(http, parts, done);
        if (d.server.port) this.port = `${quic ? "UDP" : "TCP"} ${d.server.port}`;
        const want = KINDS[this.want];
        this.warn = http !== "HTTP" && http !== want;
        this.detail = [this.warn ? `answered over ${http}, not ${want}: ${mismatch(this.want, http)}` : "", ...parts].filter(Boolean).join(" · ");
        this.waiting = "";
        this.fellBack = false;
        this.tcpSince = 0;
        this.tcpLogged = false;
        done(true);
        this.record(true, rtt);
      })
      .catch(() => {
        this.fellBack = false;
        done(false);
        this.record(false, null);
      });
  }

  /** The HTTP/3 row's name answered, but over TCP: no answer over HTTP/3. */
  private overTcp(http: string, parts: string[], done: (ok: boolean) => void): void {
    done(false);
    this.warn = false;
    if (this.tone === "trying") {
      this.tcpSince ||= Date.now();
      const staying = Date.now() - this.tcpSince >= H3_WAIT_MS;
      this.waiting = staying ? "Only over TCP" : "Not over HTTP/3 yet";
      this.detail = [
        staying
          ? `answers over ${http} only: ${this.port} may be blocked on the way, or this browser doesn't use HTTP/3`
          : `answered over ${http}: browsers move to HTTP/3 once the server says it offers it`,
        ...parts,
      ].join(" · ");
      if (staying && !this.tcpLogged) this.view.log(this, `only over ${http} so far: ${this.port} may be blocked, or this browser doesn't use HTTP/3`);
      this.tcpLogged ||= staying;
      this.record(false, null);
    } else {
      this.fellBack = true;
      this.detail = [`answered over ${http} instead: the browser fell back to TCP, and waits minutes before trying HTTP/3 again`, ...parts].join(" · ");
      this.record(false, null, `fell back to ${http} over TCP`);
    }
  }

  override resume(phase: number): void {
    this.waiting = "";
    this.fellBack = false;
    this.tcpSince = 0;
    this.tcpLogged = false;
    super.resume(phase);
  }
}

/** An echo a second over a WebSocket: its own TCP connection, opened by an HTTP/1.1 upgrade. */
class WsMethod extends Method {
  private prober: WsProber | null = null;
  private next = 0;
  private opening = false;

  constructor(fam: Fam | "Auto", view: DiagView, ep: Endpoint) {
    super(fam, "ws", view, ep, 1000);
    this.port = `TCP ${new URL(ep.base).port || (ep.base.startsWith("https") ? "443" : "80")}`;
    this.detail = "its own TCP connection, opened by an HTTP/1.1 upgrade";
  }

  tick(): void {
    // A refused WebSocket looks like any other that didn't open, so it goes by what its
    // address answers over HTTPS: no new socket until the code is in.
    if (!this.prober?.ready && this.ep.needsCode) return this.needsCode();
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
    void prober.open(this.ep.wsUrl("/ws"), 3000, this.ep.wsProtocols()).then(() => {
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
  private waitingFor = new Map<number, (rtt: number | null) => void>();

  constructor(
    fam: Fam,
    view: DiagView,
    ep: Endpoint,
    private readonly ice: RTCIceServer[],
  ) {
    super(fam, "udp", view, ep, UDP_RETRY_MS);
    this.port = "UDP";
  }

  tick(): void {
    const rtc = this.rtc;
    // Its signalling's address asks for the access code: a try every UDP_RETRY_MS, unseen.
    if (!rtc?.open && this.ep.needsCode && (this.connecting || Date.now() < this.next)) return this.needsCode();
    const done = this.lane.send();
    if (rtc?.open) {
      const seq = this.seq++ >>> 0;
      const finish = (rtt: number | null) => {
        clearTimeout(timer);
        this.waitingFor.delete(seq);
        done(rtt !== null);
        this.record(rtt !== null, rtt);
      };
      const timer = setTimeout(() => finish(null), 1500);
      this.waitingFor.set(seq, finish);
      rtc.send(seq, 64);
      return;
    }
    if (rtc) {
      rtc.close(); // the channel closed under us
      this.rtc = null;
    }
    done(false);
    if (!this.ep.needsCode) this.record(false, null);
    if (this.connecting || Date.now() < this.next) return;
    this.connecting = true;
    this.next = Date.now() + UDP_RETRY_MS;
    RtcProbe.connect(this.ep, this.ice, 8000)
      .then(async (probe) => {
        if (this.stopped) return probe.close();
        probe.onEcho = (seq, rtt) => this.waitingFor.get(seq)?.(rtt);
        this.rtc = probe;
        const [path, seen] = await Promise.all([probe.path(), probe.address()]);
        if (path?.port) this.port = `UDP ${path.port}`;
        if (path) this.server = path.address;
        const dtls = path?.dtls ? [path.dtls.version, path.dtls.cipher].filter(Boolean).join(" ") : "";
        this.detail = [dtls, path ? `to ${path.address}` : "", seen?.ip ? `seen from ${hostPort(seen.ip, seen.port)}` : ""].filter(Boolean).join(" · ") || "WebRTC data channel, no retransmits";
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

/** One address family's rows, or this address's, under a heading with the server's addresses. */
interface Group {
  fam: Fam | "Auto";
  methods: Method[];
  server: HTMLElement;
}

interface Event {
  t: number;
  who: string;
  text: string;
}

export class DiagView implements View {
  private methods: Method[] = [];
  private groups: Group[] = [];
  private families = new Set<Fam>(); // the families the rows can reach
  private timer = 0;
  private off: (() => void) | null = null;
  // Each HTTP row's latest answer, by the family the server saw it from.
  private answers: Partial<Record<Fam, Partial<Record<Http, { d: DiagHttp; http: string }>>>> = {};
  private udp: Partial<Record<Fam, { ip: string | null; port: number | null; seen: UdpAddress | null; local: string | null }>> = {};
  private offsets: number[] = [];
  private offsetSpread: number[] = [];
  private events: Event[] = [];
  private logShown = -1;
  private away = false; // another page is shown
  private stale = false; // the server was changed while away
  private awakeOpt: ReturnType<typeof awakeSwitch> = null; // Keep screen on
  private els!: { you: HTMLElement; clock: HTMLElement; rows: HTMLElement; egress: HTMLElement; log: HTMLElement };
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
        h(
          "div",
          {},
          h("h1", {}, "Diag"),
          h("p", { class: "lede wide" }, "Every way this browser can reach the server, tried once a second while this page is shown: HTTP/1.1, HTTP/2 and HTTP/3, WebSocket and UDP, over IPv4 and IPv6, as this server offers them."),
        ),
        this.awakeOpt ? h("div", { class: "head-actions" }, this.awakeOpt.el) : null,
      ),
      h("section", { class: "card diag-card diag-addresses" }, this.els.you, this.els.clock),
      card(
        "Connection methods",
        "Each ping leaves on the left and its pong comes back from the server on the right. Green: answering. Red: answered before, not now. Grey: no answer yet, or not offered. Amber: restricted, access code required.",
        this.els.rows,
        h(
          "p",
          { class: "diag-note" },
          "HTTP/2 carries every request to a server over one TCP connection, as streams inside it, and HTTP/3 does the same over one QUIC connection on UDP; HTTP/1.1 opens a connection for each request in flight, up to six per server. So the Test page's parallel streams are separate TCP connections only over HTTP/1.1. A WebSocket always opens its own TCP connection. Each second's HTTP attempt is a request on the browser's open connection, so a path that breaks shows as requests without answers.",
        ),
        h(
          "p",
          { class: "diag-note" },
          "The HTTP/3 row counts only answers over HTTP/3. A browser asks over TCP first, and moves to HTTP/3 once the server says it offers it, so the row starts grey. If it stays on TCP, UDP is blocked on the way, or the browser doesn't use HTTP/3; a browser that falls back to TCP waits minutes before trying HTTP/3 again.",
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

  /**
   * Build the methods for the selected server: on first load and when it changes. A row is
   * listed only where the server offers it: each HTTP row needs its single-family name, the
   * WebSocket row the HTTP/2 row's, and the UDP rows WebRTC. A family without names isn't
   * listed, except that a server without any lists its UDP rows for both.
   */
  private restart(): void {
    for (const m of this.methods) m.stop();
    this.methods = [];
    this.groups = [];
    this.families.clear();
    this.answers = {};
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
    const ws = (fam: Fam | "Auto", ep: Endpoint) => (info.capabilities.websocket ? [new WsMethod(fam, this, ep)] : []);
    // UDP signals over this page's own address; the probe itself is held to one family.
    const udp = (fam: Fam) => (info.capabilities.webrtc ? [new UdpMethod(fam, this, new Endpoint(loc?.url ?? "", fam === "IPv4" ? "ipv4" : "ipv6"), info.ice_servers)] : []);
    const named = (["IPv4", "IPv6"] as const).map((fam) => ({ fam, names: names(loc, fam) }));
    const listed = named.filter((n) => Object.values(n.names).some(Boolean));
    // Without a family's HTTP/2 name, HTTP goes wherever the browser takes this address.
    if (!listed.some((n) => n.names.http2)) this.groups.push(this.group("Auto", [new HttpsMethod("Auto", "http2", this, main), ...ws("Auto", main)]));
    for (const { fam, names: n } of listed.length ? listed : named) {
      const methods: Method[] = [];
      for (const http of ["http1", "http2", "http3"] as const) {
        const ep = n[http];
        if (ep) methods.push(new HttpsMethod(fam, http, this, ep));
      }
      if (n.http2) methods.push(...ws(fam, n.http2));
      methods.push(...udp(fam));
      if (methods.length) this.groups.push(this.group(fam, methods));
    }
    // Each group's rows are in the same order: HTTP/1.1, HTTP/2, HTTP/3, WebSocket, UDP.
    for (const g of this.groups) {
      for (const fam of g.fam === "Auto" ? (["IPv4", "IPv6"] as const) : [g.fam]) this.families.add(fam);
      this.methods.push(...g.methods);
    }
    if (this.groups.length) {
      this.els.rows.append(h("div", { class: "diag-cols", "aria-hidden": "true" }, h("span", {}, "Method"), h("span", {}, "Pings"), h("span", {}, "State"), h("span", {}, "Last 5 minutes")));
    }
    for (const g of this.groups) {
      this.els.rows.append(h("div", { class: "diag-group" }, h("h3", {}, famName(g.fam)), g.server), ...g.methods.map((m) => m.row.root));
    }
    for (const [i, m] of this.methods.entries()) m.start((i * 1000) / this.methods.length);
    this.render();
  }

  private group(fam: Fam | "Auto", methods: Method[]): Group {
    return { fam, methods, server: h("span", { class: "diag-group-server" }) };
  }

  // ---- What the answers say ------------------------------------------------------

  answered(d: DiagHttp, http: string, kind: Http, rtt: number, sentAt: number): void {
    const fam = d.client.family;
    if (fam) (this.answers[fam] ??= {})[kind] = { d, http };
    // The server's clock against this browser's, from the middle of the round trip.
    this.offsets.push(d.time_ms - (sentAt + rtt / 2));
    this.offsetSpread.push(rtt / 2);
    if (this.offsets.length > 30) {
      this.offsets.shift();
      this.offsetSpread.shift();
    }
  }

  /** The latest answer from that family over TCP, or over HTTP/3: the HTTP/2 row's first. */
  private seen(fam: Fam, quic: boolean): DiagHttp | undefined {
    const rows = this.answers[fam] ?? {};
    const order: Http[] = quic ? ["http3", "http2", "http1"] : ["http2", "http1", "http3"];
    return order.map((k) => rows[k]).find((a) => a && (a.http === "HTTP/3") === quic)?.d;
  }

  udpPath(fam: Fam, ip: string | null, port: number | null, seen: UdpAddress | null, local: string | null): void {
    this.udp[fam] = { ip, port, seen, local };
  }

  log(who: Method | null, text: string): void {
    this.events.push({ t: Date.now(), who: who?.label ?? "", text });
    if (this.events.length > LOG_MAX) this.events.shift();
  }

  // ---- Rendering -----------------------------------------------------------------

  /** The code asked for over the page, from a row's button. */
  enterCode(ep: Endpoint): Promise<boolean> {
    return this.app.enterCode(ep);
  }

  private render(): void {
    // One button for each address that asks for the code, on the first of its rows.
    const offered = new Set<string>();
    for (const m of this.methods) {
      const button = m.refused && this.app.takesCode && !offered.has(m.ep.base);
      if (button) offered.add(m.ep.base);
      m.render(button);
    }
    // Each group's server addresses, in its rows' order: one per name, where names differ.
    for (const g of this.groups) {
      const ips = [...new Set(g.methods.map((m) => m.server).filter(Boolean))];
      setText(g.server, ips.length ? `Server ${ips.join(", ")}` : "");
    }
    this.renderAddresses();
    this.renderEgress();
    this.renderLog();
  }

  private renderAddresses(): void {
    const { you, clock } = this.els;
    clear(you);
    for (const fam of ["IPv4", "IPv6"] as const) {
      const ip = this.seen(fam, false)?.client.ip ?? this.seen(fam, true)?.client.ip ?? this.udp[fam]?.seen?.ip ?? null;
      // Before the server answers, no rows are built: the families aren't known yet.
      const none = !this.groups.length || this.families.has(fam) ? `no answer over ${fam} yet` : "not offered by this server";
      you.append(h("div", { class: "diag-addr" }, h("span", { class: "diag-fam" }, `Your ${fam}`), h("span", { class: `diag-ip ${fam === "IPv4" ? "v4" : "v6"}${ip ? "" : " none"}` }, ip ?? none)));
    }
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
      if (!this.families.has(fam)) continue;
      const tcp = this.seen(fam, false)?.client;
      const quic = this.seen(fam, true)?.client;
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
      const mac = fam === "IPv6" ? [local, tcp?.ip, quic?.ip, udp?.ip].map(eui64Mac).find(Boolean) : null;
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
            ...(quic ? row("Over HTTP/3 (UDP)", hostPort(quic.ip, quic.port)) : []),
            ...row("Over UDP (WebRTC)", hostPort(udp?.ip, udp?.port)),
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

/** A server's single-family names in one family, by the HTTP version each serves. */
function names(loc: Location | null, fam: Fam): Record<Http, Endpoint | null> {
  const family = fam === "IPv4" ? "ipv4" : "ipv6";
  const v = fam === "IPv4" ? "v4" : "v6";
  const ep = (url: string | undefined) => (url ? new Endpoint(url, family) : null);
  return { http1: ep(loc?.[`url_${v}_http1` as const]), http2: ep(loc?.[`url_${v}` as const]), http3: ep(loc?.[`url_${v}_http3` as const]) };
}
