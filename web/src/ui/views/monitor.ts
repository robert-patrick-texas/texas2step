// Continuous probing: a live RTT trace with per-second loss, for watching a path over time.
import { WsProber } from "../../engine/latency";
import { RtcProbe } from "../../engine/loss";
import { jitter, percentile } from "../../engine/stats";
import type { App, View } from "../app";
import { LiveChart } from "../charts/live";
import { h, setText } from "../dom";
import { type Formatted, ms, pct } from "../format";

interface Probe {
  send(seq: number): void;
  close(): void;
}

const RATES = [5, 10, 20, 50];
const WINDOWS: [number, string][] = [
  [60, "1 min"],
  [300, "5 min"],
  [900, "15 min"],
];

export class MonitorView implements View {
  private chart = new LiveChart();
  private probe: Probe | null = null;
  private timers: number[] = [];
  private sentAt = new Map<number, number>();
  private seq = 0;
  private rate = 10;
  private startBtn!: HTMLButtonElement;
  private transport!: HTMLElement;
  private figures: Record<string, HTMLElement> = {};
  private lostCount: HTMLElement | null = null;
  private total = { sent: 0, lost: 0 };

  constructor(private readonly app: App) {}

  mount(root: HTMLElement): void {
    this.startBtn = h("button", { class: "btn primary big", type: "button" }, "Start monitoring");
    this.startBtn.addEventListener("click", () => void (this.probe ? this.stop() : this.start()));
    this.transport = h("span", { class: "chip" }, "idle");

    const seg = (label: string, values: [number, string][], current: number, set: (v: number) => void) => {
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
    };

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

    root.append(
      h(
        "section",
        { class: "page-head" },
        h("div", {}, h("h1", {}, "Monitor"), h("p", { class: "lede" }, "A steady stream of small UDP probes, echoed by the server. Watch round-trip time, jitter and loss as they happen: useful while reproducing a problem or moving around a building.")),
        h("div", { class: "head-actions" }, this.transport, this.startBtn),
      ),
      h(
        "div",
        { class: "toolbar" },
        seg("Probe rate", RATES.map((r) => [r, `${r}/s`]), this.rate, (v) => {
          this.rate = v;
          if (this.probe) this.schedule();
        }),
        seg("Window", WINDOWS, 60, (v) => this.chart.setWindow(v)),
      ),
      h("div", { class: "tiles six" }, fig("now", "Latest"), fig("median", "Median"), fig("p95", "95th percentile"), fig("jitter", "Jitter"), fig("loss", "Loss (window)"), fig("probes", "Probes")),
      h("section", { class: "card chart-card" }, h("header", { class: "card-head" }, h("h2", {}, "Round-trip time"), h("p", {}, "One dot per probe. Red ticks along the top mark probes that never came back.")), this.chart.el),
    );
    this.timers.push(window.setInterval(() => this.refresh(), 500));
  }

  unmount(): void {
    this.stop();
    for (const t of this.timers) clearInterval(t);
  }

  private async start(): Promise<void> {
    const app = this.app;
    this.startBtn.disabled = true;
    setText(this.transport, "connecting…");
    this.chart.clear();
    this.sentAt.clear();
    this.total = { sent: 0, lost: 0 };
    const record = (seq: number, rtt: number) => {
      const t = this.sentAt.get(seq);
      if (t === undefined) return;
      this.sentAt.delete(seq);
      this.chart.push({ t, rtt });
      setText(this.figures.now!, fmt(ms(rtt)));
    };
    try {
      if (!app.info?.capabilities.webrtc) throw new Error("WebRTC disabled");
      const rtc = await RtcProbe.connect(app.ep, app.info.ice_servers);
      rtc.onEcho = record;
      this.probe = { send: (seq) => rtc.send(seq, 64), close: () => rtc.close() };
      setText(this.transport, "UDP · WebRTC");
    } catch {
      const ws = new WsProber();
      if (!(await ws.open(app.ep.wsUrl("/ws")))) {
        setText(this.transport, "server unreachable");
        this.startBtn.disabled = false;
        return;
      }
      this.probe = {
        send: (seq) => void ws.ping(1000).then((rtt) => rtt !== null && record(seq, rtt)),
        close: () => ws.close(),
      };
      setText(this.transport, "TCP · WebSocket (UDP unavailable)");
    }
    this.startBtn.disabled = false;
    this.startBtn.textContent = "Stop";
    this.schedule();
  }

  private schedule(): void {
    for (const t of this.timers.splice(1)) clearInterval(t);
    this.timers.push(
      window.setInterval(() => {
        const seq = this.seq++;
        this.sentAt.set(seq, Date.now());
        this.total.sent++;
        this.probe?.send(seq);
      }, 1000 / this.rate),
      // Probes not back within a second are lost.
      window.setInterval(() => {
        const cutoff = Date.now() - 1000;
        for (const [seq, t] of this.sentAt) {
          if (t >= cutoff) continue;
          this.sentAt.delete(seq);
          this.total.lost++;
          this.chart.push({ t, rtt: null });
        }
      }, 250),
    );
  }

  private stop(): void {
    for (const t of this.timers.splice(1)) clearInterval(t);
    this.probe?.close();
    this.probe = null;
    if (this.startBtn) this.startBtn.textContent = "Start monitoring";
    if (this.transport) setText(this.transport, "idle");
  }

  private refresh(): void {
    const data = this.chart.visible();
    const got = data.filter((d) => d.rtt !== null).map((d) => d.rtt!);
    const f = this.figures;
    setText(f.median!, fmt(ms(got.length ? percentile(got, 50) : null)));
    setText(f.p95!, fmt(ms(got.length ? percentile(got, 95) : null)));
    setText(f.jitter!, fmt(ms(got.length > 1 ? jitter(got) : null)));
    const lost = data.length - got.length;
    setText(f.loss!, fmt(pct(data.length ? (lost / data.length) * 100 : null)));
    // The packets behind the percentage: red once any are lost.
    if (this.lostCount) {
      setText(this.lostCount, data.length ? `(${lost.toLocaleString()})` : "");
      this.lostCount.classList.toggle("bad", lost > 0);
      this.lostCount.title = `${lost.toLocaleString()} of ${data.length.toLocaleString()} probes lost in the window`;
    }
    setText(f.probes!, this.total.sent.toLocaleString());
  }
}

const fmt = (f: Formatted) => (f.value === "—" ? "—" : `${f.value} ${f.unit}`);
