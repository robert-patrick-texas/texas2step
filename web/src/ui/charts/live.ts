// Scrolling RTT chart for the monitor: one dot per probe, lost probes as ticks on top,
// and a per-second loss strip underneath.
import { cssVar, h, onResize } from "../dom";
import { compact, ms, niceTicks, pct, text } from "../format";
import { hideTip, showTip } from "../tooltip";

export interface LiveSample {
  t: number; // ms since epoch
  rtt: number | null; // null = lost
}

export class LiveChart {
  readonly el: HTMLElement;
  private canvas: HTMLCanvasElement;
  private strip: HTMLCanvasElement;
  private samples: LiveSample[] = [];
  private windowS = 60;
  private width = 0;
  private raf = 0;
  private hoverX: number | null = null;
  private m = { top: 14, right: 12, bottom: 24, left: 46 };

  constructor() {
    this.canvas = h("canvas", { class: "live-canvas", role: "img", "aria-label": "Round-trip time over the monitoring window" });
    this.strip = h("canvas", { class: "loss-strip", role: "img", "aria-label": "Packet loss per second" });
    this.el = h("div", { class: "chart live" }, this.canvas, h("div", { class: "strip-label" }, "Loss per second"), this.strip);
    onResize(this.el, (w) => {
      this.width = w;
      this.draw();
    });
    this.canvas.addEventListener("pointermove", (ev) => {
      this.hoverX = ev.clientX - this.canvas.getBoundingClientRect().left;
      this.draw();
      this.tip(ev);
    });
    this.canvas.addEventListener("pointerleave", () => {
      this.hoverX = null;
      hideTip();
      this.draw();
    });
  }

  setWindow(seconds: number): void {
    this.windowS = seconds;
    this.draw();
  }

  clear(): void {
    this.samples = [];
    this.draw();
  }

  push(sample: LiveSample): void {
    this.samples.push(sample);
    const cutoff = Date.now() - 15 * 60 * 1000;
    while (this.samples.length && this.samples[0]!.t < cutoff) this.samples.shift();
    this.raf ||= requestAnimationFrame(() => {
      this.raf = 0;
      this.draw();
    });
  }

  visible(): LiveSample[] {
    const from = Date.now() - this.windowS * 1000;
    return this.samples.filter((s) => s.t >= from);
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

  private draw(): void {
    const W = this.width;
    if (!W) return;
    const H = 260;
    const { top, right, bottom, left } = this.m;
    const ctx = this.setup(this.canvas, H);
    const now = Date.now();
    const from = now - this.windowS * 1000;
    const data = this.visible();
    const got = data.filter((d) => d.rtt !== null).map((d) => d.rtt!) as number[];
    const sorted = [...got].sort((a, b) => a - b);
    const p99 = sorted[Math.floor(sorted.length * 0.99)] ?? 10;
    const ticks = niceTicks(Math.max(1, p99 * 1.25), 4);
    const ymax = ticks[ticks.length - 1]!;
    const x = (t: number) => left + ((t - from) / (this.windowS * 1000)) * (W - left - right);
    const y = (v: number) => H - bottom - (Math.min(v, ymax) / ymax) * (H - top - bottom);
    const ink = { grid: cssVar("--grid"), axis: cssVar("--axis"), muted: cssVar("--muted"), idle: cssVar("--idle"), crit: cssVar("--critical"), surface: cssVar("--surface") };

    ctx.font = "11px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    for (const t of ticks) {
      ctx.strokeStyle = t === 0 ? ink.axis : ink.grid;
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

    // RTT trace: thin line, then dots when sparse enough to read individually.
    ctx.strokeStyle = ink.idle;
    ctx.globalAlpha = 0.45;
    ctx.lineWidth = 1.25;
    ctx.beginPath();
    let pen = false;
    for (const d of data) {
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
    const dense = data.length > (W - left - right) / 3;
    ctx.fillStyle = ink.idle;
    for (const d of data) {
      if (d.rtt === null) continue;
      ctx.beginPath();
      ctx.arc(x(d.t), y(d.rtt), dense ? 1.5 : 3, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.fillStyle = ink.crit;
    for (const d of data) {
      if (d.rtt !== null) continue;
      ctx.fillRect(x(d.t) - 1, top - 10, 2, 8);
    }

    if (this.hoverX !== null && this.hoverX >= left) {
      ctx.strokeStyle = ink.axis;
      ctx.beginPath();
      ctx.moveTo(this.hoverX + 0.5, top);
      ctx.lineTo(this.hoverX + 0.5, H - bottom);
      ctx.stroke();
    }

    this.drawStrip(from, ink);
  }

  private drawStrip(from: number, ink: Record<string, string>): void {
    const W = this.width;
    const H = 18;
    const ctx = this.setup(this.strip, H);
    const { left, right } = this.m;
    const buckets = new Map<number, { sent: number; lost: number }>();
    for (const d of this.visible()) {
      const k = Math.floor((d.t - from) / 1000);
      const b = buckets.get(k) ?? { sent: 0, lost: 0 };
      b.sent++;
      if (d.rtt === null) b.lost++;
      buckets.set(k, b);
    }
    const bw = (W - left - right) / this.windowS;
    const warn = cssVar("--warning");
    const good = cssVar("--good");
    for (const [k, b] of buckets) {
      const loss = b.lost / b.sent;
      ctx.fillStyle = loss === 0 ? good : loss < 0.02 ? warn : ink.crit!;
      ctx.globalAlpha = loss === 0 ? 0.35 : 1;
      ctx.beginPath();
      ctx.roundRect(left + k * bw + 1, 2, Math.max(1, bw - 2), H - 4, 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  private tip(ev: PointerEvent): void {
    const data = this.visible();
    const { left, right } = this.m;
    const W = this.width;
    const from = Date.now() - this.windowS * 1000;
    const t = from + ((ev.clientX - this.canvas.getBoundingClientRect().left - left) / (W - left - right)) * this.windowS * 1000;
    let best: LiveSample | undefined;
    for (const d of data) if (!best || Math.abs(d.t - t) < Math.abs(best.t - t)) best = d;
    if (!best) return hideTip();
    const second = data.filter((d) => Math.abs(d.t - best.t) <= 500);
    const lost = second.filter((d) => d.rtt === null).length;
    showTip(ev.clientX, ev.clientY, new Date(best.t).toLocaleTimeString(), [
      { value: best.rtt === null ? "Lost" : text(ms(best.rtt)), label: "round trip", color: "var(--idle)", shape: "dot" },
      { value: text(pct(second.length ? (lost / second.length) * 100 : 0)), label: "loss in this second" },
    ]);
  }
}
