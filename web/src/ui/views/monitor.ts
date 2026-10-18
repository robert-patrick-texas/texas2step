// Continuous probing: a live RTT trace with per-second loss, for watching a path over time.
// When the site lists two or more servers, it can probe two at once, at the same moments
// from this browser, and compare the two paths on one chart.
import { WsProber } from "../../engine/latency";
import { RtcProbe } from "../../engine/loss";
import { appPath, type Endpoint } from "../../engine/net";
import { jitter, percentile } from "../../engine/stats";
import type { Location, ServerInfo } from "../../types";
import type { App, View } from "../app";
import { LiveChart, type LiveSeries } from "../charts/live";
import { clear, h, save, setText, store } from "../dom";
import { type Formatted, ms, pct } from "../format";

interface Probe {
  send(seq: number): void;
  close(): void;
}

/** One server being probed: its own session, probes in flight and totals. */
interface Dest {
  ep: Endpoint;
  info: ServerInfo | null;
  probe: Probe | null;
  transport: string;
  sentAt: Map<number, number>;
  sent: number;
  latest: number | null;
}

interface Figures {
  latest: number | null;
  median: number | null;
  p95: number | null;
  jitter: number | null;
  loss: number | null; // percent of the window
  lost: number;
  window: number;
  sent: number;
}

const RATES = [5, 10, 20, 50];
const WINDOWS: [number, string][] = [
  [60, "1 min"],
  [300, "5 min"],
  [900, "15 min"],
];
// A keeps the single monitor's green; B takes blue.
const SIDES = [
  { key: "a", short: "A", token: "--idle" },
  { key: "b", short: "B", token: "--dl" },
] as const;
const LEDE = {
  one: "A steady stream of small UDP probes, echoed by the server. Watch round-trip time, jitter and loss as they happen: useful while reproducing a problem or moving around a building.",
  two: "UDP probes to two servers at the same moments from this browser, compared on one chart.",
};
const ROWS: [keyof Figures, string][] = [
  ["latest", "Latest"],
  ["median", "Median"],
  ["p95", "95th percentile"],
  ["jitter", "Jitter"],
  ["loss", "Loss (window)"],
  ["sent", "Probes"],
];

export class MonitorView implements View {
  private chart = new LiveChart();
  private dests: Dest[] = [];
  private running = false;
  private timers: number[] = [];
  private seq = 0;
  private rate = 10;
  private two: boolean;
  private pick: { a: string; b: string };
  private seriesKey = "";
  private off: (() => void) | null = null;
  private startBtn!: HTMLButtonElement;
  private transport!: HTMLElement;
  private lede!: HTMLElement;
  private destBox!: HTMLElement;
  private tiles!: HTMLElement;
  private compare!: HTMLElement;
  private figures: Record<string, HTMLElement> = {};
  private lostCount: HTMLElement | null = null;
  private cells: Record<string, HTMLElement>[] = [];
  private heads: HTMLElement[] = [];

  constructor(private readonly app: App) {
    // A bookmarked comparison (?a=dallas&b=houston) wins over the last one used here.
    const saved = store("t2s.monitor", { two: false, a: "", b: "" });
    const q = new URLSearchParams(window.location.search);
    this.two = q.has("a") && q.has("b") ? true : saved.two;
    this.pick = { a: q.get("a") ?? saved.a, b: q.get("b") ?? saved.b };
  }

  mount(root: HTMLElement): void {
    this.startBtn = h("button", { class: "btn primary big", type: "button" }, "Start monitoring");
    this.startBtn.addEventListener("click", () => void (this.running ? this.stop() : this.start()));
    this.transport = h("span", { class: "chip" }, "idle");
    this.lede = h("p", { class: "lede" });
    this.destBox = h("div", { class: "dest-box" });

    const fig = (key: string, label: string) => {
      const value = h("span", { class: "tile-value" }, "—");
      this.figures[key] = value;
      const figure = h("div", { class: "tile-figure" }, value);
      if (key === "loss") {
        this.lostCount = h("span", { class: "tile-count" });
        figure.append(this.lostCount);
      }
      return h("div", { class: "tile compact" }, h("div", { class: "tile-label" }, label), figure);
    };
    this.tiles = h("div", { class: "tiles six" }, fig("now", "Latest"), fig("median", "Median"), fig("p95", "95th percentile"), fig("jitter", "Jitter"), fig("loss", "Loss (window)"), fig("probes", "Probes"));
    this.compare = h("div", { class: "table-wrap compare-wrap" });

    root.append(
      h("section", { class: "page-head" }, h("div", {}, h("h1", {}, "Monitor"), this.lede), h("div", { class: "head-actions" }, this.transport, this.startBtn)),
      h(
        "div",
        { class: "toolbar" },
        this.destBox,
        seg("Probe rate", RATES.map((r) => [r, `${r}/s`]), this.rate, (v) => {
          this.rate = v;
          if (this.running) this.schedule();
        }),
        seg("Window", WINDOWS, 60, (v) => this.chart.setWindow(v)),
        seg("Scale", [[0, "Linear"], [1, "Log"]], 0, (v) => this.chart.setLog(v === 1)),
      ),
      this.tiles,
      h(
        "section",
        { class: "card chart-card" },
        h("header", { class: "card-head" }, h("h2", {}, "Round-trip time"), h("p", {}, "One dot per probe. Red ticks along the top mark probes that never came back.")),
        this.compare,
        this.chart.el,
      ),
    );
    // The server list arrives after the first render; redraw the choices when it does.
    this.off = this.app.onServer(() => {
      if (!this.running) this.layout();
    });
    this.layout();
    this.timers.push(window.setInterval(() => this.refresh(), 500));
  }

  unmount(): void {
    this.stop();
    this.off?.();
    for (const t of this.timers) clearInterval(t);
  }

  // ---- Destinations --------------------------------------------------------------

  /** Comparing needs a second server to compare with. */
  private get comparing(): boolean {
    return this.two && this.app.locations.length >= 2;
  }

  /** Servers A and B: the saved or bookmarked ones if still listed, else sensible picks. */
  private sides(): [Location, Location] | null {
    const locs = this.app.locations;
    if (locs.length < 2) return null;
    const find = (id: string) => locs.find((l) => l.id === id);
    const a = find(this.pick.a) ?? this.app.selected ?? locs[0]!;
    const b = [find(this.pick.b), ...locs].find((l) => l && l.id !== a.id)!;
    return [a, b];
  }

  /** Render the destination choices, tiles or comparison table, and chart series. */
  private layout(): void {
    const sides = this.sides();
    if (sides) this.pick = { a: sides[0].id, b: sides[1].id };
    const comparing = this.comparing && sides !== null;
    setText(this.lede, comparing ? LEDE.two : LEDE.one);
    this.tiles.hidden = comparing;
    this.compare.hidden = !comparing;

    clear(this.destBox);
    if (sides) {
      this.destBox.append(
        seg("Destinations", [[0, "One"], [1, "Two"]], comparing ? 1 : 0, (v) => {
          this.two = v === 1;
          this.layout();
        }),
      );
      if (comparing) {
        for (const [i, side] of SIDES.entries()) {
          const select = h("select", { class: "dest-select", "aria-label": `Server ${side.short}` });
          for (const loc of this.app.locations) {
            select.append(h("option", { value: loc.id, selected: loc.id === sides[i]!.id }, loc.location ? `${loc.name} — ${loc.location}` : loc.name));
          }
          select.addEventListener("change", () => {
            const other = i === 0 ? "b" : "a";
            // Picking the other side's server swaps the two.
            if (select.value === this.pick[other]) this.pick[other] = this.pick[side.key];
            this.pick[side.key] = select.value;
            this.layout();
          });
          const key = h("span", { class: "legend-key dot", style: `--key:var(${side.token})` });
          this.destBox.append(h("label", { class: "opt" }, h("span", { class: "opt-label" }, key, ` Server ${side.short}`), select));
        }
      }
      // Remember the choice, and keep it in the address so it can be bookmarked.
      save("t2s.monitor", { two: this.two, ...this.pick });
      const query = comparing ? `?${new URLSearchParams(this.pick)}` : "";
      if (window.location.search !== query) history.replaceState(null, "", appPath("/monitor") + query);
    }

    const series: LiveSeries[] = comparing ? sides.map((loc, i) => ({ label: `${loc.name} (${SIDES[i]!.short})`, short: SIDES[i]!.short, token: SIDES[i]!.token })) : [{ label: "", short: "", token: "--idle" }];
    const seriesKey = JSON.stringify(series);
    if (seriesKey !== this.seriesKey) {
      this.seriesKey = seriesKey;
      this.chart.setSeries(series);
      this.dests = [];
    }
    if (comparing) this.renderCompare(sides);
    this.lock(false);
  }

  private renderCompare(sides: Location[]): void {
    clear(this.compare);
    this.cells = [{}, {}, {}]; // A, B, and B − A
    this.heads = [];
    const head = h("tr", {}, h("th", {}));
    for (const [i, loc] of sides.entries()) {
      const sub = h("span", { class: "compare-sub" }, loc.location || "");
      this.heads.push(sub);
      head.append(
        h(
          "th",
          { class: "num" },
          h("span", { class: "compare-name" }, h("span", { class: "legend-key dot", style: `--key:var(${SIDES[i]!.token})` }), `${loc.name} (${SIDES[i]!.short})`),
          sub,
        ),
      );
    }
    head.append(h("th", { class: "num" }, "B − A"));
    const body = h("tbody", {});
    for (const [key, label] of ROWS) {
      const row = h("tr", {}, h("th", {}, label));
      for (const cells of this.cells) {
        const cell = h("td", { class: "num" }, "—");
        cells[key] = cell;
        row.append(cell);
      }
      body.append(row);
    }
    this.compare.append(h("table", { class: "table compare" }, h("thead", {}, head), body));
  }

  /** While probing, the destinations can't change underneath the trace. */
  private lock(on: boolean): void {
    for (const el of this.destBox.querySelectorAll<HTMLButtonElement | HTMLSelectElement>("button, select")) el.disabled = on;
  }

  // ---- Probing -------------------------------------------------------------------

  private async start(): Promise<void> {
    const app = this.app;
    const sides = this.comparing ? this.sides() : null;
    this.startBtn.disabled = true;
    this.lock(true);
    setText(this.transport, "connecting…");
    this.chart.clear();
    const dest = (ep: Endpoint, info: ServerInfo | null): Dest => ({ ep, info, probe: null, transport: "", sentAt: new Map(), sent: 0, latest: null });
    this.dests = sides ? sides.map((loc) => dest(app.endpointFor(loc), null)) : [dest(app.ep, app.info)];
    await Promise.all(this.dests.map((d, i) => this.connect(d, i)));
    this.startBtn.disabled = false;
    if (!this.dests.some((d) => d.probe)) {
      setText(this.transport, sides ? "servers unreachable" : "server unreachable");
      this.lock(false);
      return;
    }
    this.running = true;
    this.startBtn.textContent = "Stop";
    setText(this.transport, sides ? this.dests.map((d, i) => `${SIDES[i]!.short} ${d.transport.split(" ")[0]}`).join(" · ") : this.dests[0]!.transport);
    this.schedule();
  }

  /** Open a UDP session to one server, or a WebSocket when UDP can't get through. */
  private async connect(d: Dest, series: number): Promise<void> {
    const record = (seq: number, rtt: number) => {
      const t = d.sentAt.get(seq);
      if (t === undefined) return;
      d.sentAt.delete(seq);
      d.latest = rtt;
      this.chart.push({ t, rtt }, series);
    };
    try {
      d.info ??= await d.ep.info();
      if (!d.info.capabilities.webrtc) throw new Error("WebRTC disabled");
      const rtc = await RtcProbe.connect(d.ep, d.info.ice_servers);
      rtc.onEcho = record;
      d.probe = { send: (seq) => rtc.send(seq, 64), close: () => rtc.close() };
      d.transport = "UDP · WebRTC";
    } catch {
      const ws = new WsProber();
      if (!(await ws.open(d.ep.wsUrl("/ws")))) {
        d.transport = "unreachable";
        return;
      }
      d.probe = {
        send: (seq) => void ws.ping(1000).then((rtt) => rtt !== null && record(seq, rtt)),
        close: () => ws.close(),
      };
      d.transport = "TCP · WebSocket (UDP unavailable)";
    }
  }

  private schedule(): void {
    for (const t of this.timers.splice(1)) clearInterval(t);
    this.timers.push(
      // One clock for every server, so their probes leave together.
      window.setInterval(() => {
        const seq = this.seq++;
        const now = Date.now();
        for (const d of this.dests) {
          if (!d.probe) continue;
          d.sentAt.set(seq, now);
          d.sent++;
          d.probe.send(seq);
        }
      }, 1000 / this.rate),
      // Probes not back within a second are lost.
      window.setInterval(() => {
        const cutoff = Date.now() - 1000;
        for (const [i, d] of this.dests.entries()) {
          for (const [seq, t] of d.sentAt) {
            if (t >= cutoff) continue;
            d.sentAt.delete(seq);
            this.chart.push({ t, rtt: null }, i);
          }
        }
      }, 250),
    );
  }

  private stop(): void {
    for (const t of this.timers.splice(1)) clearInterval(t);
    for (const d of this.dests) {
      d.probe?.close();
      d.probe = null;
    }
    this.running = false;
    if (this.startBtn) this.startBtn.textContent = "Start monitoring";
    if (this.transport) setText(this.transport, "idle");
    if (this.destBox) this.lock(false);
  }

  // ---- Figures -------------------------------------------------------------------

  private figuresFor(i: number): Figures {
    const data = this.chart.visible(i);
    const got = data.filter((d) => d.rtt !== null).map((d) => d.rtt!);
    const lost = data.length - got.length;
    return {
      latest: this.dests[i]?.latest ?? null,
      median: got.length ? percentile(got, 50) : null,
      p95: got.length ? percentile(got, 95) : null,
      jitter: got.length > 1 ? jitter(got) : null,
      loss: data.length ? (lost / data.length) * 100 : null,
      lost,
      window: data.length,
      sent: this.dests[i]?.sent ?? 0,
    };
  }

  private refresh(): void {
    if (this.comparing && this.cells.length) this.refreshCompare();
    else this.refreshTiles();
  }

  private refreshTiles(): void {
    const v = this.figuresFor(0);
    const f = this.figures;
    setText(f.now!, fmt(ms(v.latest)));
    setText(f.median!, fmt(ms(v.median)));
    setText(f.p95!, fmt(ms(v.p95)));
    setText(f.jitter!, fmt(ms(v.jitter)));
    setText(f.loss!, fmt(pct(v.loss)));
    // The packets behind the percentage: red once any are lost.
    if (this.lostCount) {
      setText(this.lostCount, v.window ? `(${v.lost.toLocaleString()})` : "");
      this.lostCount.classList.toggle("bad", v.lost > 0);
      this.lostCount.title = `${v.lost.toLocaleString()} of ${v.window.toLocaleString()} probes lost in the window`;
    }
    setText(f.probes!, v.sent.toLocaleString());
  }

  private refreshCompare(): void {
    const sides = [this.figuresFor(0), this.figuresFor(1)];
    for (const [i, v] of sides.entries()) {
      const c = this.cells[i]!;
      for (const key of ["latest", "median", "p95", "jitter"] as const) setText(c[key]!, fmt(ms(v[key])));
      setText(c.loss!, v.window ? `${fmt(pct(v.loss))} (${v.lost.toLocaleString()})` : "—");
      c.loss!.classList.toggle("bad", v.lost > 0);
      c.loss!.title = `${v.lost.toLocaleString()} of ${v.window.toLocaleString()} probes lost in the window`;
      setText(c.sent!, v.sent.toLocaleString());
      const d = this.dests[i];
      setText(this.heads[i]!, [this.sides()?.[i]?.location, d?.transport].filter(Boolean).join(" · "));
    }
    const [a, b] = sides as [Figures, Figures];
    const diff = this.cells[2]!;
    for (const key of ["latest", "median", "p95", "jitter"] as const) setText(diff[key]!, signed(a[key], b[key], (v) => ms(v)));
    setText(diff.loss!, signed(a.loss, b.loss, (v) => ({ value: pct(v).value, unit: "pts" })));
    setText(diff.sent!, "");
  }
}

function seg(label: string, values: [number, string][], current: number, set: (v: number) => void): HTMLElement {
  const group = h("div", { class: "seg", role: "radiogroup", "aria-label": label });
  for (const [v, text] of values) {
    const b = h("button", { type: "button", role: "radio", "aria-checked": String(v === current) }, text);
    b.addEventListener("click", () => {
      for (const o of group.children) o.setAttribute("aria-checked", String(o === b));
      set(v);
    });
    group.append(b);
  }
  return h("div", { class: "opt" }, h("span", { class: "opt-label" }, label), group);
}

const fmt = (f: Formatted) => (f.value === "—" ? "—" : `${f.value} ${f.unit}`);

/** B − A with its sign, e.g. "+22.4 ms". */
function signed(a: number | null, b: number | null, format: (v: number) => Formatted): string {
  if (a === null || b === null) return "—";
  const d = b - a;
  const sign = d > 0 ? "+" : d < 0 ? "−" : "";
  return sign + fmt(format(Math.abs(d)));
}
