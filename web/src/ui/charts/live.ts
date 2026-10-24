// Scrolling RTT chart for the monitor: one dot per probe, lost probes as ticks on top,
// and a per-second loss strip underneath, where a call's score marks each 10 seconds. It
// draws one series, or two (one per server) on shared axes, so two paths can be compared
// directly. Stopped, it holds still where it stopped.
import type { Slice } from "../../engine/stream";
import type { CallRating } from "../../types";
import { clear, cssVar, h, onResize, pointerIn } from "../dom";
import { compact, ms, niceTicks, pct, text } from "../format";
import { onSizeChange } from "../size";
import { onThemeChange } from "../theme";
import { hideTip, showTip, type TipRow } from "../tooltip";
import { log125 } from "./common";

export interface LiveSample {
  t: number; // ms since epoch
  rtt: number | null; // null = lost
}

export interface LiveSeries {
  label: string; // "" for a lone series
  short: string; // marks its row of lost-probe ticks, e.g. "A"
  token: string; // colour, as a CSS custom property name
}

const LONE: LiveSeries = { label: "", short: "", token: "--idle" };

// A call's rating as the colour of its mark on the loss strip: the grades' colours.
const RATING_INK: Record<CallRating, string> = {
  excellent: "--good",
  good: "--good",
  fair: "--warning",
  poor: "--serious",
  bad: "--critical",
};
const RATING_WORD: Record<CallRating, string> = { excellent: "Excellent", good: "Good", fair: "Fair", poor: "Poor", bad: "Bad" };

export class LiveChart {
  readonly el: HTMLElement;
  private canvas: HTMLCanvasElement;
  private strips: HTMLCanvasElement[] = [];
  private stripBox: HTMLElement;
  private stripLabel: HTMLElement;
  private calls: (Slice[] | null)[] = []; // per series, with a call profile
  private frozen: number | null = null; // when monitoring stopped: the chart holds still there
  private series: (LiveSeries & { samples: LiveSample[] })[] = [];
  private windowS = 60;
  private log = false;
  private width = 0;
  private raf = 0;
  private hoverX: number | null = null;
  private m = { top: 14, right: 12, bottom: 24, left: 46 };

  constructor() {
    this.canvas = h("canvas", { class: "live-canvas", role: "img", "aria-label": "Round-trip time over the monitoring window" });
    this.stripBox = h("div", {});
    this.stripLabel = h("div", { class: "strip-label" }, "Loss per second");
    this.el = h("div", { class: "chart live" }, this.canvas, this.stripBox);
    this.setSeries([LONE]);
    onResize(this.el, (w) => {
      this.width = w;
      this.draw();
    });
    onThemeChange(this.el, () => this.draw());
    onSizeChange(this.el, () => this.draw());
    const refit = () => (this.el.isConnected ? this.draw() : window.removeEventListener("resize", refit));
    window.addEventListener("resize", refit);
    this.canvas.addEventListener("pointermove", (ev) => {
      this.hoverX = pointerIn(ev, this.canvas, this.width).x;
      this.draw();
      this.tip(ev);
    });
    this.canvas.addEventListener("pointerleave", () => {
      this.hoverX = null;
      hideTip();
      this.draw();
    });
  }

  /** Start over with these series (one per server), each with its own loss strip. */
  setSeries(list: LiveSeries[]): void {
    this.series = list.map((s) => ({ ...s, samples: [] }));
    this.m.top = 4 + 10 * list.length;
    this.m.right = list.length > 1 ? 24 : 12; // room to name each row of lost-probe ticks
    clear(this.stripBox);
    // One label for every strip; with two servers, each strip names its own at its left edge.
    this.stripBox.append(this.stripLabel);
    this.strips = this.series.map((s, i) => {
      const strip = h("canvas", { class: "loss-strip", role: "img", "aria-label": s.label ? `Packet loss per second, ${s.label}` : "Packet loss per second" });
      strip.addEventListener("pointermove", (ev) => this.stripTip(ev, strip, i));
      strip.addEventListener("pointerleave", () => hideTip());
      this.stripBox.append(strip);
      return strip;
    });
    this.calls = this.series.map(() => null);
    this.draw();
  }

  /** A call's 10-second slices, per series, marked on the loss strips; null for the path
   * profile. */
  setCalls(calls: (Slice[] | null)[]): void {
    this.calls = calls;
    const label = calls.some((c) => c) ? "Loss per second · call quality per 10\u00a0s" : "Loss per second";
    if (this.stripLabel.textContent !== label) this.stripLabel.textContent = label;
    this.draw();
  }

  /** Hold the chart still at `at` (Date.now() when monitoring stopped), or let it run on. */
  freeze(at: number | null): void {
    this.frozen = at;
    this.draw();
  }

  /** The chart's present: now, or when monitoring stopped. */
  get now(): number {
    return this.frozen ?? Date.now();
  }

  get windowSeconds(): number {
    return this.windowS;
  }

  setWindow(seconds: number): void {
    this.windowS = seconds;
    this.draw();
  }

  setLog(log: boolean): void {
    this.log = log;
    this.draw();
  }

  clear(): void {
    for (const s of this.series) s.samples = [];
    this.draw();
  }

  push(sample: LiveSample, series = 0): void {
    const s = this.series[series];
    if (!s) return;
    s.samples.push(sample);
    const cutoff = Date.now() - 15 * 60 * 1000;
    while (s.samples.length && s.samples[0]!.t < cutoff) s.samples.shift();
    this.raf ||= requestAnimationFrame(() => {
      this.raf = 0;
      this.draw();
    });
  }

  visible(series = 0): LiveSample[] {
    const from = this.now - this.windowS * 1000;
    return (this.series[series]?.samples ?? []).filter((s) => s.t >= from);
  }

  private setup(canvas: HTMLCanvasElement, H: number): CanvasRenderingContext2D {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.style.height = `${H}px`;
    canvas.width = Math.round(this.width * dpr);
    canvas.height = Math.round(H * dpr);
    const ctx = canvas.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }

  /** Tall enough to fill the window below the chart, so the whole card fits on screen. */
  private plotHeight(): number {
    const box = this.canvas.getBoundingClientRect();
    const k = box.width ? this.width / box.width : 1; // layout pixels per screen pixel
    // The loss strips (or a call's), their label and the card's padding.
    const below = 74 + 22 * this.series.length;
    const room = (window.innerHeight - box.top - window.scrollY) * k - below;
    return Math.round(Math.min(340, Math.max(this.series.length > 1 ? 140 : 170, room)));
  }

  private draw(): void {
    const W = this.width;
    if (!W) return;
    const H = this.plotHeight();
    const { top, right, bottom, left } = this.m;
    const ctx = this.setup(this.canvas, H);
    const now = this.now;
    const from = now - this.windowS * 1000;
    const data = this.series.map((_, i) => this.visible(i));
    const got = data.flat().filter((d) => d.rtt !== null).map((d) => d.rtt!);
    const sorted = [...got].sort((a, b) => a - b);
    const p99 = sorted[Math.floor(sorted.length * 0.99)] ?? 10;
    let ticks: number[];
    let y: (v: number) => number;
    if (this.log) {
      const scale = log125(sorted[0] ?? 1, sorted[sorted.length - 1] ?? 10, H - bottom, top);
      ticks = scale.ticks;
      y = (v) => Math.max(top, scale(v));
    } else {
      ticks = niceTicks(Math.max(1, p99 * 1.25), 4);
      const ymax = ticks[ticks.length - 1]!;
      y = (v) => H - bottom - (Math.min(v, ymax) / ymax) * (H - top - bottom);
    }
    const x = (t: number) => left + ((t - from) / (this.windowS * 1000)) * (W - left - right);
    const ink = { grid: cssVar("--grid"), axis: cssVar("--axis"), muted: cssVar("--muted"), crit: cssVar("--critical"), surface: cssVar("--surface") };

    ctx.font = "11px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    for (const [i, t] of ticks.entries()) {
      ctx.strokeStyle = i === 0 ? ink.axis : ink.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, Math.round(y(t)) + 0.5);
      ctx.lineTo(W - right, Math.round(y(t)) + 0.5);
      ctx.stroke();
      ctx.fillStyle = ink.muted;
      ctx.fillText(compact(t), left - 8, y(t));
    }
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    const step = this.windowS <= 60 ? 10 : this.windowS <= 300 ? 60 : 180;
    for (let s = 0; s <= this.windowS; s += step) {
      ctx.fillText(s === 0 ? "now" : `−${s >= 60 ? `${s / 60}m` : `${s}s`}`, x(now - s * 1000), H - 6);
    }
    ctx.textAlign = "left";
    ctx.fillText("ms", 4, top);

    const plotted = data.reduce((n, d) => n + d.length, 0);
    const dense = plotted > (W - left - right) / 3;
    for (const [i, s] of this.series.entries()) {
      const color = cssVar(s.token);
      // RTT trace: thin line, then dots when sparse enough to read individually.
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.45;
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      let pen = false;
      for (const d of data[i]!) {
        if (d.rtt === null) {
          pen = false;
          continue;
        }
        if (pen) ctx.lineTo(x(d.t), y(d.rtt));
        else ctx.moveTo(x(d.t), y(d.rtt));
        pen = true;
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = color;
      for (const d of data[i]!) {
        if (d.rtt === null) continue;
        ctx.beginPath();
        ctx.arc(x(d.t), y(d.rtt), dense ? 1.5 : 3, 0, Math.PI * 2);
        ctx.fill();
      }
      // Lost probes: a row of red ticks above the plot, one row per series.
      const row = top - 10 * (this.series.length - i);
      if (s.short) {
        ctx.fillStyle = ink.muted;
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.font = "600 9px system-ui, sans-serif";
        ctx.fillText(s.short, W - right + 6, row + 4);
      }
      ctx.fillStyle = ink.crit;
      for (const d of data[i]!) {
        if (d.rtt !== null) continue;
        ctx.fillRect(x(d.t) - 1, row, 2, 8);
      }
    }

    if (this.hoverX !== null && this.hoverX >= left) {
      ctx.strokeStyle = ink.axis;
      ctx.beginPath();
      ctx.moveTo(this.hoverX + 0.5, top);
      ctx.lineTo(this.hoverX + 0.5, H - bottom);
      ctx.stroke();
    }

    for (const [i, strip] of this.strips.entries()) this.drawStrip(strip, data[i] ?? [], from, ink, this.series[i]!, this.calls[i] ?? []);
  }

  private drawStrip(strip: HTMLCanvasElement, data: LiveSample[], from: number, ink: Record<string, string>, series: LiveSeries, slices: Slice[]): void {
    const W = this.width;
    const H = 18;
    const ctx = this.setup(strip, H);
    const { left, right } = this.m;
    const buckets = new Map<number, { sent: number; lost: number }>();
    for (const d of data) {
      const k = Math.floor((d.t - from) / 1000);
      const b = buckets.get(k) ?? { sent: 0, lost: 0 };
      b.sent++;
      if (d.rtt === null) b.lost++;
      buckets.set(k, b);
    }
    const bw = (W - left - right) / this.windowS;
    const warn = cssVar("--warning");
    // A second without loss: green, or with two servers, that server's own colour.
    const many = this.series.length > 1;
    const clean = many ? cssVar(series.token) : cssVar("--good");
    if (many) {
      ctx.fillStyle = clean;
      ctx.font = "600 10px system-ui, sans-serif";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(series.short, left - 8, H / 2);
    }
    for (const [k, b] of buckets) {
      const loss = b.lost / b.sent;
      ctx.fillStyle = loss === 0 ? clean : loss < 0.02 ? warn : ink.crit!;
      ctx.globalAlpha = loss === 0 ? 0.35 : 1;
      ctx.beginPath();
      ctx.roundRect(left + k * bw + 1, 2, Math.max(1, bw - 2), H - 4, 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    this.markCalls(ctx, slices, from, H);
  }

  /**
   * A call's 10 seconds on the loss strip, once whole: a line beneath them in its rating's
   * colour, and its score on a label over their middle where there's room. The seconds'
   * own loss stays visible around and under them.
   */
  private markCalls(ctx: CanvasRenderingContext2D, slices: Slice[], from: number, H: number): void {
    const W = this.width;
    const { left, right } = this.m;
    const x = (t: number) => left + ((t - from) / (this.windowS * 1000)) * (W - left - right);
    const surface = cssVar("--surface");
    const ink = cssVar("--ink");
    ctx.font = "650 10px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (const s of slices) {
      const x0 = Math.max(left, x(s.start));
      const x1 = Math.min(W - right, x(s.end));
      if (x1 - x0 < 2) continue;
      const rating: CallRating = s.dead ? "bad" : (s.call?.rating ?? "bad");
      const color = cssVar(RATING_INK[rating]);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.roundRect(x0 + 1.5, H - 2, x1 - x0 - 3, 2, 1);
      ctx.fill();
      const label = s.dead ? "—" : s.call!.mos.toFixed(1);
      const w = ctx.measureText(label).width + 10;
      if (x1 - x0 < w + 6) continue;
      const cx = (x0 + x1) / 2;
      ctx.fillStyle = surface;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      ctx.roundRect(cx - w / 2, 2.5, w, H - 6, (H - 6) / 2);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = ink;
      ctx.fillText(label, cx, 2.5 + (H - 6) / 2 + 0.5);
    }
  }

  /** A second of a loss strip: its loss, and with a call, the score of its 10 seconds. */
  private stripTip(ev: PointerEvent, strip: HTMLCanvasElement, i: number): void {
    const { left, right } = this.m;
    const from = this.now - this.windowS * 1000;
    const t = from + ((pointerIn(ev, strip, this.width).x - left) / (this.width - left - right)) * this.windowS * 1000;
    const second = Math.floor((t - from) / 1000);
    const inSecond = this.visible(i).filter((d) => Math.floor((d.t - from) / 1000) === second);
    const who = this.series[i]!.label ? `${this.series[i]!.label}: ` : "";
    const rows: TipRow[] = [];
    if (inSecond.length) {
      const lost = inSecond.filter((d) => d.rtt === null).length;
      rows.push({ value: text(pct((lost / inSecond.length) * 100)), label: `${who}lost in this second (${lost} of ${inSecond.length})` });
    }
    const s = (this.calls[i] ?? []).find((c) => t >= c.start && t < c.end);
    if (s && (s.dead || !s.call)) rows.push({ value: "No packets came back", label: `in these 10 s, ${s.sent} sent` });
    else if (s?.call) {
      const c = s.call;
      const rated = (r: CallRating, mos: number) => `${mos.toFixed(1)} · ${RATING_WORD[r]}`;
      rows.push({ value: rated(c.rating, c.mos), label: "call quality, these 10 s", color: `var(${RATING_INK[c.rating]})`, shape: "dot" });
      if (c.down && c.up) {
        rows.push({ value: rated(c.down.rating, c.down.mos), label: "you hear" });
        rows.push({ value: rated(c.up.rating, c.up.mos), label: "the other end hears" });
      }
      rows.push({ value: `${c.longest_gap_ms.toLocaleString()} ms`, label: "longest gap" });
      rows.push({ value: text(pct((s.lost / Math.max(1, s.sent)) * 100)), label: `lost, ${c.late} late` });
      if (s.frames) rows.push({ value: text(pct((s.frames.hit / Math.max(1, s.frames.total)) * 100)), label: `of ${s.frames.total} video frames hit` });
    }
    if (!rows.length) return hideTip();
    const title = s ? `${new Date(s.start).toLocaleTimeString()} – ${new Date(s.end).toLocaleTimeString()}` : new Date(from + second * 1000).toLocaleTimeString();
    showTip(ev.clientX, ev.clientY, title, rows);
  }

  private tip(ev: PointerEvent): void {
    const { left, right } = this.m;
    const W = this.width;
    const from = this.now - this.windowS * 1000;
    const t = from + ((pointerIn(ev, this.canvas, W).x - left) / (W - left - right)) * this.windowS * 1000;
    const nearest = (data: LiveSample[]) => {
      let best: LiveSample | undefined;
      for (const d of data) if (!best || Math.abs(d.t - t) < Math.abs(best.t - t)) best = d;
      return best && Math.abs(best.t - t) <= 1000 ? best : undefined;
    };
    const rows = this.series.map((s, i) => {
      const data = this.visible(i);
      const best = nearest(data);
      const second = best ? data.filter((d) => Math.abs(d.t - best.t) <= 500) : [];
      const lost = second.filter((d) => d.rtt === null).length;
      return { s, best, loss: text(pct(second.length ? (lost / second.length) * 100 : 0)) };
    });
    const at = rows.map((r) => r.best).find((b) => b);
    if (!at) return hideTip();
    const rtt = (b: LiveSample | undefined) => (!b ? "—" : b.rtt === null ? "Lost" : text(ms(b.rtt)));
    if (rows.length === 1) {
      const [r] = rows as [(typeof rows)[number]];
      return showTip(ev.clientX, ev.clientY, new Date(at.t).toLocaleTimeString(), [
        { value: rtt(r.best), label: "round trip", color: `var(${r.s.token})`, shape: "dot" },
        { value: r.loss, label: "loss in this second" },
      ]);
    }
    showTip(
      ev.clientX,
      ev.clientY,
      new Date(at.t).toLocaleTimeString(),
      rows.map((r) => ({ value: rtt(r.best), label: `${r.s.label} · ${r.loss} loss this second`, color: `var(${r.s.token})`, shape: "dot" as const })),
    );
  }
}
