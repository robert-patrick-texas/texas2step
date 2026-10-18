// The main view: run a test (or show a saved one) with live instruments and charts.
import { appPath, routeOf } from "../../engine/net";
import { type Phase, PHASES, type RunEvents, TestRun } from "../../engine/runner";
import type { Direction, Level, Readiness, TestResult, ThroughputResult } from "../../types";
import type { App, Settings, View } from "../app";
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

const USE_ICON: Record<Readiness["id"], string> = {
  streaming: "M3 5h18v12H3zM8 21h8M10 8.5v5l4.5-2.5z",
  calls: "M3 7h12v10H3zM15 10.5l6-3.5v10l-6-3.5",
  gaming: "M6 9h12a3 3 0 0 1 3 3v3a2.5 2.5 0 0 1-4.5 1.5L15 15H9l-1.5 1.5A2.5 2.5 0 0 1 3 15v-3a3 3 0 0 1 3-3zM8 11v3M6.5 12.5h3M16 12h.01M18 13.5h.01",
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
  private tiles!: Record<"download" | "upload" | "latency" | "jitter" | "loss" | "bloat", Tile>;
  private throughput!: ThroughputChart;
  private latency!: LatencyChart;
  private packets!: PacketGrid;
  private packetStats!: HTMLElement;
  private streams!: StreamsChart;
  private readiness!: HTMLElement;
  private details!: HTMLElement;
  private actions!: HTMLElement;
  private banner!: HTMLElement;
  private options!: HTMLElement;
  private run: TestRun | null = null;
  private result: TestResult | null = null;
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
    };
    this.banner = h("div", { class: "banner", hidden: true });
    this.options = this.renderOptions();

    this.throughput = new ThroughputChart();
    this.latency = new LatencyChart();
    this.packets = new PacketGrid();
    this.packetStats = h("dl", { class: "stats-list" });
    this.streams = new StreamsChart();
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
          this.rail,
          h("div", { class: "tiles" }, ...Object.values(this.tiles).map((t) => t.el)),
          this.options,
        ),
      ),
      this.link.el,
      h(
        "div",
        { class: "grid" },
        card("Throughput", "Aggregate rate across parallel streams, sampled every 100 ms. The stable figure excludes TCP ramp-up.", this.throughput.el, "span-2"),
        card("Latency", "Round-trip time idle (HTTP) and under load (WebSocket probes during each transfer). Growth under load is bufferbloat.", this.latency.el),
        card("Packet loss", "Numbered UDP probes over an unreliable WebRTC data channel, echoed by the server. Each square is one packet.", h("div", { class: "packet-body" }, this.packets.el, this.packetStats)),
        card("Parallel streams", "How evenly concurrent flows shared the path.", this.streams.el),
        card("Readiness", "What this connection comfortably supports.", this.readiness),
        card("All measurements", "Every figure from this run. Export as JSON or share a link.", h("div", {}, this.details, this.actions), "span-2"),
      ),
    );

    this.off = this.app.onServer(() => this.describeEndpoints());
    this.describeEndpoints();
    this.renderResult(null);
    if (params.rid) void this.load(params.rid);
  }

  unmount(): void {
    this.run?.abort();
    this.off?.();
  }

  // ---- Options -------------------------------------------------------------------

  private renderOptions(): HTMLElement {
    const s = this.app.settings;
    const seg = <K extends keyof Settings>(key: K, label: string, values: [Settings[K], string][], hint: string) => {
      const group = h("div", { class: "seg", role: "radiogroup", "aria-label": label });
      for (const [v, text] of values) {
        const b = h("button", { type: "button", role: "radio", "aria-checked": String(s[key] === v) }, text);
        b.addEventListener("click", () => {
          s[key] = v;
          this.app.saveSettings();
          for (const other of group.children) other.setAttribute("aria-checked", String(other === b));
          if (key === "family") void this.app.refreshFamily();
        });
        group.append(b);
      }
      return h("div", { class: "opt" }, h("span", { class: "opt-label", title: hint }, label), group);
    };
    const loc = this.app.selected;
    const family = loc && (loc.url_v4 || loc.url_v6);
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
        family ? seg("family", "Address family", [["auto", "Auto"], ["ipv4", "IPv4"], ["ipv6", "IPv6"]], "Uses this server's IPv4-only or IPv6-only hostname.") : null,
      ),
    );
  }

  private describeEndpoints(): void {
    const { info, whoami } = this.app;
    const client = this.result?.client;
    setText(this.link.client.title, "You");
    setText(this.link.client.detail, [client?.ip ?? whoami?.ip, client?.family ?? whoami?.family, client?.protocol ?? whoami?.http_version].filter(Boolean).join(" · ") || "—");
    const server = this.result?.server ?? info?.server;
    setText(this.link.server.title, server?.name ?? "Server");
    setText(this.link.server.detail, [server?.location, info ? `v${info.version}` : ""].filter(Boolean).join(" · ") || (info ? "" : "unreachable"));
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
    // Leave a saved-result URL without re-routing (that would unmount this view).
    if (routeOf(window.location.pathname) !== "/") history.replaceState(null, "", appPath("/"));
    this.banner.hidden = true;
    this.result = null;
    this.renderResult(null);
    const { streams, seconds, packets, intervalMs } = app.settings;
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
        this.gauge.set(live.mbps, dir);
        this.link.setRate(live.mbps);
        this.link.setLabel(`${dir === "download" ? "↓" : "↑"} ${text(rate(live.mbps))}`);
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

    this.run = new TestRun(app.ep, app.info, app.whoami, { streams, seconds, packets, intervalMs }, events);
    this.gauge.setRunning(true);
    this.gauge.button.setAttribute("aria-label", "Stop test");
    let result: TestResult | null = null;
    try {
      result = await this.run.run();
    } catch (err) {
      if (!this.run.aborted) {
        console.error(err);
        setText(this.status, `The test stopped unexpectedly: ${err instanceof Error ? err.message : String(err)}`);
      }
    }
    const aborted = this.run.aborted;
    this.run = null;
    this.gauge.setRunning(false, true);
    this.gauge.button.removeAttribute("aria-label");
    this.link.setMode("idle");
    if (aborted) {
      setText(this.status, "Stopped. Partial results are shown below.");
      for (const li of this.rail.querySelectorAll<HTMLElement>("[data-state=active]")) li.dataset.state = "pending";
      return;
    }
    if (!result) return;
    this.result = result;
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
      upload: "Uploading; probing latency under load…",
      loss: "Sending numbered UDP probes…",
    };
    setText(this.status, say[p]);
    this.gauge.setCaption(PHASE_LABEL[p]);
    if (p === "download" || p === "upload") this.gauge.set(0, p);
    else this.gauge.set(0, p === "latency" ? "latency" : "loss");
    this.link.setMode(p, p === "latency" ? "round trips" : p === "loss" ? "UDP probes" : "");
  }

  private async save(result: TestResult): Promise<void> {
    if (!this.app.info?.capabilities.results) return this.renderActions();
    try {
      const saved = await this.app.origin.post<{ id: string }>("/results", result);
      result.id = saved.id;
      history.replaceState(null, "", appPath(`/r/${saved.id}`));
    } catch (err) {
      console.warn("could not save result", err);
    }
    this.renderActions();
  }

  private async load(id: string): Promise<void> {
    try {
      const result = await this.app.origin.json<TestResult>(`/results/${encodeURIComponent(id)}`);
      this.result = result;
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
    const railValue = (p: Phase, v: string) => {
      const li = this.rail.querySelector<HTMLElement>(`[data-phase=${p}]`)!;
      setText(li.querySelector(".rail-value"), v);
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
    const bloat = r.bufferbloat;
    if (bloat?.grade) {
      const worst = Math.max(bloat.added_download_ms ?? -Infinity, bloat.added_upload_ms ?? -Infinity);
      const f = ms(Math.abs(worst));
      put(this.tiles.bloat, { value: `${worst >= 0 ? "+" : "−"}${f.value}`, unit: f.unit }, "");
      const chip = h("span", { class: "grade", "data-grade": bloat.grade[0]! }, `Grade ${bloat.grade}`);
      this.tiles.bloat.sub.replaceChildren(chip, " bufferbloat");
    }
    if (r.loss) this.renderLoss(r.loss, railValue);
    else if (!partial && r.config.packets === 0) {
      railValue("loss", "");
      put(this.tiles.loss, { value: "—", unit: "" }, "skipped");
      this.packetStats.replaceChildren(h("p", { class: "note" }, "The UDP test was skipped. Enable it under Test options → UDP probes."));
    }

    if (!partial) {
      this.renderReadiness(r.readiness ?? []);
      this.renderDetails(r);
      this.renderActions();
    }
  }

  private renderLoss(loss: NonNullable<TestResult["loss"]>, railValue: (p: Phase, v: string) => void): void {
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
          glyph(USE_ICON[item.id], "glyph use"),
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
    add("Started", when(r.started_at));
    add("Configuration", `${r.config.streams} streams · ${r.config.seconds} s per direction · ${r.config.packets} UDP probes every ${r.config.interval_ms} ms`);
    const lat = r.latency;
    if (lat) add("Idle latency", `median ${text(ms(lat.median))} · min ${text(ms(lat.min))} · p90 ${text(ms(lat.p90))} · max ${text(ms(lat.max))} · jitter ${text(ms(lat.jitter))} · ${lat.count} samples${lat.lost ? `, ${lat.lost} lost` : ""}`);
    const tp = (t: ThroughputResult | undefined, d: Direction) => {
      if (!t) return;
      const loaded = t.loaded_latency;
      add(d === "download" ? "Download" : "Upload", `${text(rate(t.mbps))} stable · peak ${text(rate(t.peak_mbps))} · ${bytes(t.bytes)} in ${t.duration_s} s · ${t.streams} streams`);
      if (loaded) add(`Latency during ${d}`, `median ${text(ms(loaded.median))} · p90 ${text(ms(loaded.p90))} · jitter ${text(ms(loaded.jitter))} · ${loaded.count} probes`);
    };
    tp(r.download, "download");
    tp(r.upload, "upload");
    if (r.bufferbloat?.grade) add("Bufferbloat", `grade ${r.bufferbloat.grade}`);
    const l = r.loss;
    if (l) add("Packet loss", `${text(pct(l.loss_pct))} (${l.lost}/${l.sent}) over ${l.transport === "webrtc" ? "UDP" : "TCP"}${l.up_loss_pct !== null ? ` · up ${text(pct(l.up_loss_pct))} · down ${text(pct(l.down_loss_pct))}` : ""} · ${l.out_of_order} out of order · RTT ${text(ms(l.rtt.median))} · jitter ${text(ms(l.rtt.jitter))}`);
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
    if (r.id) {
      const share = h("button", { class: "btn primary", type: "button" }, "Copy share link");
      share.addEventListener("click", () => void navigator.clipboard.writeText(`${window.location.origin}${appPath(`/r/${r.id}`)}`).then(() => flash(share, "Link copied")));
      this.actions.append(share);
    }
  }
}
