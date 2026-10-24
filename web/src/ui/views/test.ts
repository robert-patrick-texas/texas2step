// The main view: run a test (or show a saved one) with live instruments and charts.
import { appPath, Refused, REFUSED } from "../../engine/net";
import { callQuality, responsiveness } from "../../engine/quality";
import { type Phase, PHASES, type RunEvents, TestRun } from "../../engine/runner";
import type { CallQuality, CallRating, CallSide, Direction, Level, Readiness, Responsiveness, ResponsivenessSide, TestResult, ThroughputResult } from "../../types";
import type { App, Settings, View } from "../app";
import { awakeSwitch } from "../awake";
import { LatencyChart } from "../charts/latency";
import { PacketGrid } from "../charts/packets";
import { StreamsChart } from "../charts/streams";
import { ThroughputChart } from "../charts/throughput";
import { clear, h, setText } from "../dom";
import { bytes, type Formatted, ms, pct, rate, text, when } from "../format";
import { Gauge } from "../gauge";
import { LinkView } from "../link";

const PHASE_LABEL: Record<Phase, string> = {
  latency: "Latency",
  download: "Download",
  one: "One stream",
  upload: "Upload",
  loss: "Packet loss",
};

interface Tile {
  el: HTMLElement;
  value: HTMLElement;
  unit: HTMLElement;
  sub: HTMLElement;
}

function tile(key: string, label: string, color?: string): Tile {
  const value = h("span", { class: "tile-value" }, "—");
  const unit = h("span", { class: "tile-unit" }, "");
  const sub = h("div", { class: "tile-sub" }, "");
  const el = h(
    "div",
    { class: "tile", "data-key": key },
    h("div", { class: "tile-label" }, color ? h("span", { class: "legend-key dot", style: `--key:${color}` }) : null, label),
    h("div", { class: "tile-figure" }, value, unit),
    sub,
  );
  return { el, value, unit, sub };
}

function put(t: Tile, f: Formatted, sub = ""): void {
  setText(t.value, f.value);
  setText(t.unit, f.value === "—" ? "" : f.unit);
  setText(t.sub, sub);
}

function card(title: string, subtitle: string, body: HTMLElement, extra = ""): HTMLElement {
  return h(
    "section",
    { class: `card chart-card ${extra}` },
    h("header", { class: "card-head" }, h("h2", {}, title), h("p", {}, subtitle)),
    body,
  );
}

const LEVEL: Record<Level, { label: string; icon: string }> = {
  great: { label: "Great", icon: "M5 12.5l4.5 4.5L19 7.5" },
  good: { label: "Good", icon: "M5 12.5l4.5 4.5L19 7.5" },
  fair: { label: "Fair", icon: "M12 7v6m0 4v.01" },
  poor: { label: "Poor", icon: "M7 7l10 10M17 7 7 17" },
  unknown: { label: "Unknown", icon: "M12 7v.01M12 11v6" },
};

const RATING: Record<CallRating, string> = {
  excellent: "Excellent",
  good: "Good",
  fair: "Fair",
  poor: "Poor",
  bad: "Bad",
};

/** Whether the test's streams shared one connection: HTTP/2 or 3, from a browser. */
const shared = (r: TestResult) => /^h[23]$/i.test(r.client.protocol ?? "") && r.tool.startsWith("texas2step-web/");

/** Whether a gap a caller would hear rated this call below its score's own rating. */
const gapped = (c: CallSide) => c.rating !== (c.mos_rating ?? c.rating);

/** A call's rating in words, and why when a gap lowered it. */
const rated = (c: CallSide) => (gapped(c) ? `${RATING[c.rating].toLowerCase()} for its gap (the score alone: ${RATING[c.mos_rating].toLowerCase()})` : RATING[c.rating].toLowerCase());

/** Each direction's figure, what you hear first: "↓ 0 · ↑ 2". */
const ways = (call: CallQuality, f: (side: CallSide) => string) => `↓ ${f(call.down!)} · ↑ ${f(call.up!)}`;

const USE_ICON: Record<Readiness["id"], string> = {
  streaming: "M3 5h18v12H3zM8 21h8M10 8.5v5l4.5-2.5z",
  calls: "M3 7h12v10H3zM15 10.5l6-3.5v10l-6-3.5",
  interactive: "M3 5h18v14H3zM7 10l3 2.5L7 15M12.5 15H17",
  transfers: "M7 20V5M3.5 8.5 7 5l3.5 3.5M17 4v15m-3.5-3.5L17 19l3.5-3.5",
};

function glyph(d: string, cls = "glyph"): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", d);
  svg.append(p);
  return svg;
}

export class TestView implements View {
  private gauge!: Gauge;
  private link!: LinkView;
  private rail!: HTMLElement;
  private status!: HTMLElement;
  private tiles!: Record<"download" | "upload" | "latency" | "jitter" | "loss" | "bloat" | "call" | "rpm" | "one", Tile>;
  private throughput!: ThroughputChart;
  private latency!: LatencyChart;
  private packets!: PacketGrid;
  private packetStats!: HTMLElement;
  private streams!: StreamsChart;
  private throughputNote!: HTMLElement; // over HTTP/2 or 3, the streams shared one connection
  private readiness!: HTMLElement;
  private details!: HTMLElement;
  private actions!: HTMLElement;
  private banner!: HTMLElement;
  private options!: HTMLElement;
  private refreshOptions: (() => void)[] = [];
  private run: TestRun | null = null;
  private result: TestResult | null = null;
  /** Whether the page shows a saved result (/r/<id>) rather than a test from this visit. */
  private showingSaved = false;
  private trafficNoted = false; // the status line says the server's traffic limit is reached
  private away = false; // another page is shown
  private left = false; // the test running was stopped by going to another page
  private off: (() => void) | null = null;

  constructor(private readonly app: App) {}

  mount(root: HTMLElement, params: Record<string, string>): void {
    this.gauge = new Gauge(() => void this.toggle());
    this.link = new LinkView();
    this.rail = h("ol", { class: "rail", "aria-label": "Test phases" });
    for (const p of PHASES) {
      this.rail.append(
        h("li", { class: "rail-step", "data-phase": p, "data-state": "pending" }, h("span", { class: "rail-dot" }), h("span", { class: "rail-name" }, PHASE_LABEL[p]), h("span", { class: "rail-value" }, "")),
      );
    }
    this.status = h("p", { class: "status", role: "status" }, "Ready when you are.");
    this.tiles = {
      download: tile("download", "Download", "var(--dl)"),
      upload: tile("upload", "Upload", "var(--ul)"),
      latency: tile("latency", "Latency", "var(--idle)"),
      jitter: tile("jitter", "Jitter"),
      loss: tile("loss", "Packet loss"),
      bloat: tile("bloat", "Under load"),
      call: tile("call", "Call quality"),
      rpm: tile("rpm", "Responsiveness"),
      one: tile("one", "One stream", "var(--dl)"),
    };
    this.banner = h("div", { class: "banner", hidden: true });
    this.options = this.renderOptions();

    this.throughput = new ThroughputChart();
    this.latency = new LatencyChart();
    this.packets = new PacketGrid();
    this.packetStats = h("dl", { class: "stats-list" });
    this.streams = new StreamsChart();
    this.throughputNote = h("p", { class: "chart-note", hidden: true });
    this.readiness = h("ul", { class: "readiness" });
    this.details = h("div", { class: "details" });
    this.actions = h("div", { class: "actions" });

    root.append(
      this.banner,
      h(
        "section",
        { class: "hero" },
        h("div", { class: "hero-gauge" }, this.gauge.el),
        h(
          "div",
          { class: "hero-side" },
          h("div", { class: "hero-head" }, h("h1", {}, "How good is the path between you and this server?"), this.status),
          h("fieldset", { class: "hero-set" }, h("legend", {}, "Phases"), this.rail),
          h("fieldset", { class: "hero-set" }, h("legend", {}, "Results"), h("div", { class: "tiles" }, ...Object.values(this.tiles).map((t) => t.el))),
          this.options,
        ),
      ),
      this.link.el,
      h(
        "div",
        { class: "grid" },
        card("Throughput", "Aggregate rate across parallel streams, and one stream's (dashed), sampled every 100 ms. The stable figure excludes TCP ramp-up.", h("div", {}, this.throughput.el, this.throughputNote), "span-2"),
        card("Latency", "Round-trip time idle (HTTP) and under load (WebSocket probes during each transfer). Growth under load is bufferbloat.", this.latency.el),
        card("Packet loss", "Numbered UDP probes over an unreliable WebRTC data channel, echoed by the server. Each square is one packet.", h("div", { class: "packet-body" }, this.packets.el, this.packetStats)),
        card("Parallel streams", "How evenly concurrent flows shared the path.", this.streams.el),
        card("Readiness", "What this connection comfortably supports.", this.readiness),
        card("All measurements", "Every figure from this run. Export as JSON or share a link.", h("div", {}, this.details, this.actions), "span-2"),
      ),
    );

    this.off = this.app.onServer(() => {
      this.describeEndpoints();
      this.noteTraffic();
      for (const refresh of this.refreshOptions) refresh();
    });
    this.describeEndpoints();
    this.noteTraffic();
    this.renderResult(null);
    if (params.rid) void this.load(params.rid);
  }

  /** Another page is shown: a test running stops, as Stop does, and the results stay. */
  hide(): void {
    this.away = true;
    if (this.run) {
      this.left = true;
      this.run.abort();
    }
    this.app.awake.hold("test", false);
  }

  show(): void {
    this.away = false;
    for (const refresh of this.refreshOptions) refresh(); // Keep screen on is shared
  }

  unmount(): void {
    this.run?.abort();
    this.app.awake.hold("test", false);
    this.off?.();
  }

  // ---- Options -------------------------------------------------------------------

  private renderOptions(): HTMLElement {
    const s = this.app.settings;
    // `shown` is the value a choice stands at, where a setting left unset follows the server.
    const seg = <K extends keyof Settings>(key: K, label: string, values: [Settings[K], string][], hint: string, shown = () => s[key]) => {
      const group = h("div", { class: "seg", role: "radiogroup", "aria-label": label });
      const mark = () => values.forEach(([v], i) => group.children[i]?.setAttribute("aria-checked", String(shown() === v)));
      for (const [v, text] of values) {
        const b = h("button", { type: "button", role: "radio" }, text);
        b.addEventListener("click", () => {
          s[key] = v;
          this.app.saveSettings();
          mark();
        });
        group.append(b);
      }
      mark();
      this.refreshOptions.push(mark);
      return h("div", { class: "opt" }, h("span", { class: "opt-label", title: hint }, label), group);
    };
    return h(
      "details",
      { class: "options" },
      h("summary", {}, "Test options"),
      h(
        "div",
        { class: "opt-grid" },
        seg("streams", "Parallel streams", [[1, "1"], [2, "2"], [4, "4"], [6, "6"], [8, "8"]], "Concurrent transfers. Browsers allow six HTTP/1.1 connections per server; HTTP/2 multiplexes on one."),
        seg("seconds", "Duration", [[5, "5 s"], [10, "10 s"], [15, "15 s"], [20, "20 s"]], "Length of each of the download and upload phases."),
        seg("packets", "UDP probes", [[0, "Skip"], [200, "200"], [500, "500"], [1000, "1000"]], "Packets sent in the loss test."),
        seg("intervalMs", "Probe spacing", [[5, "5 ms"], [10, "10 ms"], [20, "20 ms"]], "Time between UDP probes."),
        seg("oneStream", "One stream", [[true, "Download"], [false, "Skip"]], "After the download, one stream for the same time: what a single file copy gets. Skipped when the test uses one stream. Until you choose, as the server says.", () => this.oneStream()),
        this.awakeOption(),
      ),
    );
  }

  /** The screen is kept on while a test runs: one setting with Monitor's and Diag's. */
  private awakeOption(): HTMLElement | null {
    const sw = awakeSwitch(this.app);
    if (sw) this.refreshOptions.push(sw.mark);
    return sw?.el ?? null;
  }

  /** Whether a test downloads over one stream too: the visitor's choice, or the server's. */
  private oneStream(): boolean {
    return this.app.settings.oneStream ?? this.app.info?.defaults.one_stream ?? true;
  }

  /** A server whose traffic limit is reached says so before a test is tried. */
  private noteTraffic(): void {
    if (this.run || this.showingSaved) return;
    const traffic = this.app.info?.traffic;
    if (traffic?.open === false) this.refused(traffic.message, traffic.url);
    else if (this.trafficNoted) {
      setText(this.status, "Ready when you are.");
      this.trafficNoted = false;
    }
  }

  /** The site's message for a reached traffic limit, and its link to continue elsewhere. */
  private refused(message: string | null, url: string | null): void {
    clear(this.status);
    this.status.append(message ?? REFUSED);
    if (url) this.status.append(" ", h("a", { href: url }, "Click here to continue."));
    this.trafficNoted = true;
  }

  private describeEndpoints(): void {
    const { info, whoami } = this.app;
    // A saved result's own two ends; otherwise the server the next test goes to, which
    // may not be the last test's after a switch in the picker.
    const saved = this.showingSaved ? this.result : null;
    const client = saved?.client;
    // The address gets a line of its own, so a full IPv6 address fits.
    const line = (el: HTMLElement, text: string) => {
      setText(el, text);
      el.title = text;
    };
    setText(this.link.client.title, "You");
    line(this.link.client.detail, client?.ip ?? whoami?.ip ?? "—");
    line(this.link.client.sub, [client?.family ?? whoami?.family, client?.protocol ?? whoami?.http_version].filter(Boolean).join(" · "));
    const server = saved?.server ?? info?.server;
    const version = saved?.server.version ?? info?.version;
    setText(this.link.server.title, server?.name ?? "Server");
    line(this.link.server.detail, server?.location || (info || saved ? "" : "unreachable"));
    line(this.link.server.sub, version ? `v${version}` : "");
  }

  // ---- Running -------------------------------------------------------------------

  private async toggle(): Promise<void> {
    if (this.run) {
      this.run.abort();
      return;
    }
    const app = this.app;
    if (!app.info) await app.refreshFamily();
    if (!app.info) {
      setText(this.status, "That server isn't answering. Check the address or pick another server.");
      return;
    }
    // Its traffic limit may have been reached since the page loaded.
    const traffic = (await app.ep.info().catch(() => null))?.traffic;
    if (traffic?.open === false) {
      this.refused(traffic.message, traffic.url);
      return;
    }
    if (this.away) return; // another page was chosen meanwhile
    this.trafficNoted = false;
    this.left = false;
    // Leave a saved-result URL without re-routing (that would build this page anew).
    app.setAddress("/");
    this.banner.hidden = true;
    this.result = null;
    this.showingSaved = false;
    this.describeEndpoints();
    this.renderResult(null);
    const { streams, seconds, packets, intervalMs } = app.settings;
    const oneStream = this.oneStream();
    this.packets.reset(packets);
    this.options.removeAttribute("open");

    const events: RunEvents = {
      phase: (p) => this.onPhase(p),
      latency: (kind, rtt) => {
        this.latency.add(kind, rtt);
        if (kind === "idle") {
          const f = ms(rtt);
          this.gauge.readoutText("Latency", f.value, f.unit);
          this.link.pulse();
        }
      },
      throughput: (dir, live) => {
        this.gauge.set(live.mbps, dir === "one" ? "download" : dir);
        this.link.setRate(live.mbps);
        this.link.setLabel(`${dir === "upload" ? "↑" : "↓"} ${text(rate(live.mbps))}`);
        put(this.tiles[dir], rate(live.mbps), `${live.elapsed.toFixed(1)} s of ${seconds} s`);
        this.throughput.set(dir, { timeline: live.timeline, binMs: 100, warmupS: 0, durationS: seconds, stable: null });
      },
      packetSent: (seq) => {
        this.packets.sent(seq);
        this.link.packetOut();
        this.gauge.readoutText("Packets", String(seq + 1), `of ${packets}`);
      },
      packetEcho: (seq, rtt) => {
        this.packets.echo(seq, rtt);
        this.link.packetBack();
      },
      partial: (r) => this.renderResult(r, true),
    };

    this.run = new TestRun(app.ep, app.info, app.whoami, { streams, seconds, packets, intervalMs, oneStream }, events);
    app.awake.hold("test");
    this.gauge.setRunning(true);
    this.gauge.button.setAttribute("aria-label", "Stop test");
    let result: TestResult | null = null;
    try {
      result = await this.run.run();
    } catch (err) {
      if (err instanceof Refused) {
        this.refused(err.message, err.url);
      } else if (!this.run.aborted) {
        console.error(err);
        setText(this.status, `The test stopped unexpectedly: ${err instanceof Error ? err.message : String(err)}`);
      }
    }
    const aborted = this.run.aborted;
    this.run = null;
    app.awake.hold("test", false);
    this.gauge.setRunning(false, true);
    this.gauge.button.removeAttribute("aria-label");
    this.link.setMode("idle");
    if (aborted) setText(this.status, this.left ? "Stopped when you went to another page. Partial results are shown below." : "Stopped. Partial results are shown below.");
    if (aborted || !result) {
      for (const li of this.rail.querySelectorAll<HTMLElement>("[data-state=active]")) li.dataset.state = "pending";
      return;
    }
    this.result = result;
    this.describeEndpoints();
    this.renderResult(result);
    setText(this.status, `Done in ${result.duration_s?.toFixed(0)} s.`);
    await this.save(result);
  }

  private onPhase(p: Phase): void {
    const order = PHASES.indexOf(p);
    for (const li of this.rail.querySelectorAll<HTMLElement>(".rail-step")) {
      const i = PHASES.indexOf(li.dataset.phase as Phase);
      li.dataset.state = i < order ? "done" : i === order ? "active" : li.dataset.state === "skipped" ? "skipped" : "pending";
    }
    const say: Record<Phase, string> = {
      latency: "Measuring idle round-trip time…",
      download: "Downloading in parallel streams; probing latency under load…",
      one: "Downloading over one stream, as a single file copy does…",
      upload: "Uploading; probing latency under load…",
      loss: "Sending numbered UDP probes…",
    };
    setText(this.status, say[p]);
    this.gauge.setCaption(PHASE_LABEL[p]);
    if (p === "download" || p === "upload" || p === "one") this.gauge.set(0, p === "one" ? "download" : p);
    else this.gauge.set(0, p === "latency" ? "latency" : "loss");
    this.link.setMode(p === "one" ? "download" : p, p === "latency" ? "round trips" : p === "loss" ? "UDP probes" : p === "one" ? "one stream" : "");
  }

  private async save(result: TestResult): Promise<void> {
    if (!this.app.info?.capabilities.results) return this.renderActions();
    try {
      const saved = await this.app.origin.post<{ id: string }>("/results", result);
      result.id = saved.id;
      setText(this.status, `${this.status.textContent ?? ""} Saved as ${saved.id}.`.trim());
      // A visitor without the desk's functions can't open /r/ links; the desk can.
      if (this.app.desk) this.app.setAddress(`/r/${saved.id}`);
    } catch (err) {
      console.warn("could not save result", err);
    }
    this.renderActions();
  }

  private async load(id: string): Promise<void> {
    try {
      const result = await this.app.origin.json<TestResult>(`/results/${encodeURIComponent(id)}`);
      this.result = result;
      this.showingSaved = true;
      this.packets.reset(result.loss?.packets.length ?? 0);
      this.renderResult(result);
      this.describeEndpoints();
      clear(this.banner);
      this.banner.append(
        h("span", {}, `Saved result · ${when(result.saved_at ?? result.started_at)} · ${result.server.name ?? result.server.url}${result.client.ip ? ` · from ${result.client.ip}` : ""}`),
        h("a", { href: "/", "data-link": true, class: "btn small" }, "Run your own test"),
      );
      this.banner.hidden = false;
      this.gauge.setRunning(false, true);
      setText(this.status, "Showing a saved result.");
    } catch {
      setText(this.status, "That saved result could not be found.");
    }
  }

  // ---- Rendering -----------------------------------------------------------------

  private renderResult(r: TestResult | null, partial = false): void {
    if (!r) {
      for (const t of Object.values(this.tiles)) put(t, { value: "—", unit: "" });
      for (const li of this.rail.querySelectorAll<HTMLElement>(".rail-step")) {
        li.dataset.state = "pending";
        setText(li.querySelector(".rail-value"), "");
      }
      this.throughput.reset();
      this.throughputNote.hidden = true;
      this.latency.reset();
      this.streams.reset();
      this.packets.reset(this.app.settings.packets);
      clear(this.packetStats);
      clear(this.readiness);
      this.readiness.append(h("li", { class: "empty-note" }, "Grades appear when the test completes."));
      clear(this.details);
      clear(this.actions);
      return;
    }
    // A phase shows its figure while the test runs; once it's done, the results carry it.
    const railValue = (p: Phase, v: string) => {
      const li = this.rail.querySelector<HTMLElement>(`[data-phase=${p}]`)!;
      setText(li.querySelector(".rail-value"), partial ? v : v ? "done" : "skipped");
      if (!partial) li.dataset.state = v ? "done" : "skipped";
    };

    if (r.latency) {
      const l = r.latency;
      put(this.tiles.latency, ms(l.median), `min ${text(ms(l.min))} · p90 ${text(ms(l.p90))}`);
      put(this.tiles.jitter, ms(l.jitter), `σ ${text(ms(l.stdev))} over ${l.count} probes`);
      this.latency.setStats("idle", l);
      railValue("latency", text(ms(l.median)));
    }
    for (const dir of ["download", "upload"] as const) {
      const t = r[dir];
      if (!t) continue;
      put(this.tiles[dir], rate(t.mbps), `peak ${text(rate(t.peak_mbps))} · ${t.streams} stream${t.streams > 1 ? "s" : ""}`);
      this.throughput.set(dir, { timeline: t.timeline, binMs: t.bin_ms, warmupS: t.warmup_s, durationS: t.duration_s, stable: t.mbps });
      this.latency.setStats(dir, t.loaded_latency);
      this.streams.set(dir, t.per_stream_mbps);
      railValue(dir, text(rate(t.mbps)));
    }
    const one = r.one_stream;
    if (one) {
      const multi = r.download?.mbps;
      const share = multi ? `${Math.round((one.mbps / multi) * 100)} % of ${r.download!.streams} streams · ` : "";
      put(this.tiles.one, rate(one.mbps), `${share}peak ${text(rate(one.peak_mbps))}`);
      this.tiles.one.el.title = `One stream's download, after the download over several: what a single file copy, backup or remote-desktop session gets.${shared(r) ? " Over HTTP/2 or 3 the several streams shared one connection too." : ""}`;
      this.throughput.set("one", { timeline: one.timeline, binMs: one.bin_ms, warmupS: one.warmup_s, durationS: one.duration_s, stable: one.mbps });
      railValue("one", text(rate(one.mbps)));
    } else if (!partial) {
      railValue("one", "");
      const why = r.config.streams === 1 ? "the test used one stream" : r.config.one_stream === false ? "skipped" : "not measured";
      put(this.tiles.one, { value: "—", unit: "" }, why);
    }
    // Over HTTP/2 or 3 a browser's streams share one TCP connection: say so, as it changes
    // what "parallel streams", and one stream beside them, mean.
    const n = r.download?.streams ?? r.upload?.streams ?? 0;
    this.throughputNote.hidden = !(shared(r) && n > 1);
    setText(
      this.throughputNote,
      [
        `Over ${r.client.protocol === "h3" ? "HTTP/3" : "HTTP/2"}, the ${n} streams shared one connection, as a browser's requests to one server do.`,
        r.one_stream ? "One stream then compares one connection with one connection: it shows the protocol's flow control and the server's work, not the network's limits on a single connection." : "",
        "A server published over HTTP/1.1 gives each stream a connection of its own.",
      ]
        .filter(Boolean)
        .join(" "),
    );
    const bloat = r.bufferbloat;
    if (bloat?.grade) {
      const worst = Math.max(bloat.added_download_ms ?? -Infinity, bloat.added_upload_ms ?? -Infinity);
      const f = ms(Math.abs(worst));
      put(this.tiles.bloat, { value: `${worst >= 0 ? "+" : "−"}${f.value}`, unit: f.unit }, "");
      const chip = h("span", { class: "grade", "data-grade": bloat.grade[0]! }, `Grade ${bloat.grade}`);
      this.tiles.bloat.sub.replaceChildren(chip, " bufferbloat");
    }
    // Results saved before call quality and responsiveness get them from their own figures.
    const call = r.call ?? callQuality(r.loss);
    const rpm = r.responsiveness ?? responsiveness(r);
    if (call) this.renderCall(call);
    if (rpm) this.renderRpm(rpm);
    if (r.loss) this.renderLoss(r.loss, railValue, call);
    else if (!partial && r.config.packets === 0) {
      railValue("loss", "");
      put(this.tiles.loss, { value: "—", unit: "" }, "skipped");
      put(this.tiles.call, { value: "—", unit: "" }, "needs the UDP test");
      this.packetStats.replaceChildren(h("p", { class: "note" }, "The UDP test was skipped. Enable it under Test options → UDP probes."));
    }

    if (!partial) {
      this.renderReadiness(r.readiness ?? []);
      this.renderDetails(r);
      this.renderActions();
    }
  }

  private renderCall(call: CallQuality): void {
    put(this.tiles.call, { value: call.mos.toFixed(1), unit: "MOS" });
    const chip = h("span", { class: "grade", "data-rating": call.rating }, RATING[call.rating]);
    const seconds = (call.packets * call.interval_ms) / 1000;
    const each = call.basis === "each way" && call.up && call.down;
    const where = each ? (call.worse === "up" ? " ↑" : " ↓") : "";
    // A gap a caller would hear lowers the rating below the score's own; say which gap.
    let sub = ` ${seconds.toLocaleString()} s snapshot`;
    if (gapped(call)) sub = ` ${call.longest_gap_ms.toLocaleString()} ms gap${where}`;
    else if (each) sub = ` ${ways(call, (c) => c.mos.toFixed(1))}`;
    this.tiles.call.sub.replaceChildren(chip, sub);
    const about = each ? `You hear ${call.down!.mos.toFixed(1)} (${rated(call.down!)}); the other end hears ${call.up!.mos.toFixed(1)} (${rated(call.up!)}).` : gapped(call) ? `The E-model scores this ${RATING[call.mos_rating].toLowerCase()}, but its longest gap, ${call.longest_gap_ms} ms, is long enough for a caller to hear, so it is rated ${RATING[call.rating].toLowerCase()}.` : "";
    this.tiles.call.el.title = `A G.711 voice call scored with the ITU-T E-model, over the packet-loss test's ${seconds.toLocaleString()} seconds. ${about}`.trim();
  }

  private renderRpm(rpm: Responsiveness): void {
    const n = (v: number | null) => (v === null ? "—" : v.toLocaleString());
    put(this.tiles.rpm, { value: n(rpm.rpm), unit: "RPM" }, `↓ ${n(rpm.download_rpm)} · ↑ ${n(rpm.upload_rpm)}`);
    const setups = [rpm.download, rpm.upload].filter((s): s is ResponsivenessSide => !!s).map((s) => s.setup_ms);
    const opening = setups.length ? ` A new connection took ${rpm.idle_setup_ms !== undefined ? `${text(ms(rpm.idle_setup_ms))} idle and ` : ""}${text(ms(Math.max(...setups)))} under load.` : "";
    this.tiles.rpm.el.title = `Round trips per minute under load, as the IETF's responsiveness measure counts them: higher is better. The slower direction's.${opening}`;
  }

  private renderLoss(loss: NonNullable<TestResult["loss"]>, railValue: (p: Phase, v: string) => void, call: CallQuality | null): void {
    const lossPct = pct(loss.loss_pct);
    put(this.tiles.loss, lossPct, `${loss.lost} of ${loss.sent} lost · ${loss.transport === "webrtc" ? "UDP" : "TCP fallback"}`);
    railValue("loss", text(lossPct));
    this.packets.finalize(loss.packets);
    clear(this.packetStats);
    const row = (k: string, v: string) => this.packetStats.append(h("dt", {}, k), h("dd", {}, v));
    row("Transport", loss.transport === "webrtc" ? "UDP (WebRTC, no retransmit)" : "TCP (WebSocket fallback)");
    row("Sent / received", `${loss.sent} / ${loss.received}`);
    row("Loss", text(lossPct));
    if (loss.up_loss_pct !== null) row("Upstream loss", text(pct(loss.up_loss_pct)));
    if (loss.down_loss_pct !== null) row("Downstream loss", text(pct(loss.down_loss_pct)));
    row("Out of order", String(loss.out_of_order));
    row("Round trip", `${text(ms(loss.rtt.median))} median`);
    row("Jitter", text(ms(loss.rtt.jitter)));
    if (call?.basis === "each way" && call.up && call.down) {
      row("Late for a call", ways(call, (c) => String(c.late)));
      row("Longest gap", ways(call, (c) => `${c.longest_gap_ms.toLocaleString()} ms`));
      row("Burst ratio", ways(call, (c) => c.burst_ratio?.toFixed(2) ?? "—"));
      row("You hear", `${call.down.mos.toFixed(1)} MOS · ${rated(call.down)}`);
      row("They hear", `${call.up.mos.toFixed(1)} MOS · ${rated(call.up)}`);
    } else if (call) {
      row("Late for a call", `${call.late} (${text(pct(call.late_pct))})`);
      row("Longest gap", `${call.longest_gap_ms.toLocaleString()} ms`);
      row("Burst ratio", call.burst_ratio === null ? "no loss" : call.burst_ratio.toFixed(2));
      row("Call quality", `${call.mos.toFixed(1)} MOS · ${rated(call)}`);
    }
    if (loss.note) this.packetStats.append(h("p", { class: "note" }, loss.note));
  }

  private renderReadiness(items: Readiness[]): void {
    clear(this.readiness);
    for (const item of items) {
      const level = LEVEL[item.level];
      this.readiness.append(
        h(
          "li",
          { class: "ready-row" },
          // Results saved before 0.1.0 used "gaming" for the interactive case.
          glyph(USE_ICON[item.id] ?? USE_ICON.interactive, "glyph use"),
          h("div", { class: "ready-text" }, h("strong", {}, item.label), h("span", {}, item.detail)),
          h("span", { class: "level", "data-level": item.level }, glyph(level.icon, "glyph lvl"), level.label),
        ),
      );
    }
  }

  private renderDetails(r: TestResult): void {
    const rows: [string, string][] = [];
    const add = (k: string, v: string | undefined | null) => v && rows.push([k, v]);
    add("Server", [r.server.name, r.server.location, r.server.url].filter(Boolean).join(" · "));
    add("Client", [r.client.ip, r.client.family, r.client.protocol].filter(Boolean).join(" · "));
    add("This computer", r.client.local_ip);
    // Browser results from before mac_source read theirs from EUI-64 too.
    add("MAC", r.client.mac && `${r.client.mac}, ${r.client.mac_source === "nic" ? "of the NIC the connection left by" : "read from the IPv6 address (EUI-64)"}`);
    add("Started", when(r.started_at));
    add("Configuration", `${r.config.streams} streams · ${r.config.seconds} s per direction${r.config.one_stream ? " · one stream's download" : ""} · ${r.config.packets} UDP probes every ${r.config.interval_ms} ms`);
    const lat = r.latency;
    if (lat) add("Idle latency", `median ${text(ms(lat.median))} · min ${text(ms(lat.min))} · p90 ${text(ms(lat.p90))} · max ${text(ms(lat.max))} · jitter ${text(ms(lat.jitter))} · ${lat.count} samples${lat.lost ? `, ${lat.lost} lost` : ""}`);
    const tp = (t: ThroughputResult | undefined, d: Direction) => {
      if (!t) return;
      const loaded = t.loaded_latency;
      add(d === "download" ? "Download" : "Upload", `${text(rate(t.mbps))} stable · peak ${text(rate(t.peak_mbps))} · ${bytes(t.bytes)} in ${t.duration_s} s · ${t.streams} streams`);
      if (loaded) add(`Latency during ${d}`, `median ${text(ms(loaded.median))} · p90 ${text(ms(loaded.p90))} · jitter ${text(ms(loaded.jitter))} · ${loaded.count} probes`);
    };
    tp(r.download, "download");
    const one = r.one_stream;
    if (one) add("One stream", `${text(rate(one.mbps))} stable · peak ${text(rate(one.peak_mbps))} · ${bytes(one.bytes)} in ${one.duration_s} s${r.download?.mbps ? ` · ${Math.round((one.mbps / r.download.mbps) * 100)} % of the ${r.download.streams}-stream rate` : ""}`);
    tp(r.upload, "upload");
    if (r.bufferbloat?.grade) add("Bufferbloat", `grade ${r.bufferbloat.grade}`);
    const l = r.loss;
    if (l) add("Packet loss", `${text(pct(l.loss_pct))} (${l.lost}/${l.sent}) over ${l.transport === "webrtc" ? "UDP" : "TCP"}${l.up_loss_pct !== null ? ` · up ${text(pct(l.up_loss_pct))} · down ${text(pct(l.down_loss_pct))}` : ""} · ${l.out_of_order} out of order · RTT ${text(ms(l.rtt.median))} · jitter ${text(ms(l.rtt.jitter))}`);
    const call = r.call ?? callQuality(l);
    const side = (c: CallSide) => `${c.mos.toFixed(2)} MOS, ${rated(c)} · R ${c.r.toFixed(1)} · ${text(pct(c.impaired_pct))} lost or late (${c.late} late) · longest gap ${c.longest_gap_ms} ms · burst ratio ${c.burst_ratio?.toFixed(2) ?? "—"}`;
    if (call?.basis === "each way" && call.up && call.down) {
      add("Call, you hear", side(call.down));
      add("Call, they hear", side(call.up));
      add("Call", `${text(ms(call.delay_ms))} mouth to ear · ${call.packets} packets every ${call.interval_ms} ms, each direction timed with the server's stamps`);
    } else if (call) add("Call quality", `${side(call)} · ${text(ms(call.delay_ms))} mouth to ear · ${call.packets} packets every ${call.interval_ms} ms, from round trips`);
    const rpm = r.responsiveness ?? responsiveness(r);
    const how = (s: ResponsivenessSide | undefined, v: number | null) =>
      s ? `${s.rpm.toLocaleString()} (new connections ${text(ms(s.setup_ms))}, ${s.loaded_ms === null ? "no loaded round trips" : `round trips ${text(ms(s.loaded_ms))} ${s.loaded_on === "loaded connection" ? "on the loaded connection" : "beside the load"}`})` : (v?.toLocaleString() ?? "—");
    if (rpm?.basis === "ippm") add("Responsiveness", `${rpm.rpm.toLocaleString()} RPM by the IETF draft's method · download ${how(rpm.download, rpm.download_rpm)} · upload ${how(rpm.upload, rpm.upload_rpm)}${rpm.idle_setup_ms !== undefined ? ` · a new connection idle: ${text(ms(rpm.idle_setup_ms))}` : ""}`);
    else if (rpm) add("Responsiveness", `${rpm.rpm.toLocaleString()} RPM · download ${rpm.download_rpm?.toLocaleString() ?? "—"} · upload ${rpm.upload_rpm?.toLocaleString() ?? "—"} · from the latency-under-load probes`);
    clear(this.details);
    const table = h("table", { class: "table" }, h("tbody", {}, ...rows.map(([k, v]) => h("tr", {}, h("th", { scope: "row" }, k), h("td", {}, v)))));
    this.details.append(table);
  }

  private renderActions(): void {
    clear(this.actions);
    const r = this.result;
    if (!r) return;
    const json = () => JSON.stringify(r, null, 2);
    const flash = (b: HTMLButtonElement, msg: string) => {
      const old = b.textContent;
      b.textContent = msg;
      setTimeout(() => (b.textContent = old), 1400);
    };
    const download = h("button", { class: "btn", type: "button" }, "Download JSON");
    download.addEventListener("click", () => {
      const a = h("a", { href: URL.createObjectURL(new Blob([json()], { type: "application/json" })), download: `texas2step-${r.started_at.replace(/[:.]/g, "-")}.json` });
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });
    const copy = h("button", { class: "btn", type: "button" }, "Copy JSON");
    copy.addEventListener("click", () => void navigator.clipboard.writeText(json()).then(() => flash(copy, "Copied")));
    this.actions.append(download, copy);
    if (r.id && this.app.desk) {
      const share = h("button", { class: "btn primary", type: "button" }, "Copy share link");
      share.addEventListener("click", () => void navigator.clipboard.writeText(`${window.location.origin}${appPath(`/r/${r.id}`)}`).then(() => flash(share, "Link copied")));
      this.actions.append(share);
    } else if (r.id) {
      // Read out over the phone: the desk opens it from History.
      this.actions.append(h("p", { class: "result-id" }, "Result ID ", h("strong", {}, r.id), " · read it to the service desk"));
    }
  }
}
