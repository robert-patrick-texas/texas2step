// Every UDP probe as one cell, in send order: shade = round-trip time, red = lost, with an
// arrow for the way it was lost where the server's record says: up on the way to the
// server, down on the way back.
import { cssVar, h, onResize, pointerIn } from "../dom";
import { ms, text } from "../format";
import { isDarkTheme, onThemeChange } from "../theme";
import { hideTip, showTip } from "../tooltip";
import { ramp } from "./common";

type Cell = "unsent" | "inflight" | number | "lost" | "up" | "down";
const LOST: Cell[] = ["lost", "up", "down"];

// Sequential teal ramp (latency identity), light→dark on light surfaces and the reverse
// on dark ones, so "slow" is always the more prominent end.
const RAMP_LIGHT = ["#bfeedd", "#5fcfa4", "#1baf7a", "#11805a", "#0b553c"];
const RAMP_DARK = ["#0f4a35", "#157a57", "#199e70", "#4cc799", "#9be6c6"];

export class PacketGrid {
  readonly el: HTMLElement;
  private canvas: HTMLCanvasElement;
  private cells: Cell[] = [];
  private lo = 0;
  private hi = 1;
  private width = 0;
  private pending = 0;
  private layout = { cols: 50, size: 8, gap: 2 };
  private ways = false; // the lost cells show which way they were lost
  private scale: HTMLElement;

  constructor() {
    this.canvas = h("canvas", { class: "packet-canvas", role: "img", "aria-label": "Packet-by-packet UDP results" });
    this.scale = h("div", { class: "packet-scale" });
    this.el = h("div", { class: "chart packets" }, this.canvas, this.scale);
    onResize(this.el, (w) => {
      this.width = w;
      this.draw();
    });
    onThemeChange(this.el, () => this.draw());
    this.canvas.addEventListener("pointermove", (ev) => this.hover(ev));
    this.canvas.addEventListener("pointerleave", hideTip);
  }

  reset(n: number): void {
    this.cells = new Array<Cell>(n).fill("unsent");
    this.ways = false;
    this.lo = Infinity;
    this.hi = 0;
    this.draw();
  }

  sent(seq: number): void {
    if (this.cells[seq] === "unsent") this.cells[seq] = "inflight";
    this.schedule();
  }

  echo(seq: number, rtt: number): void {
    this.cells[seq] = rtt;
    this.lo = Math.min(this.lo, rtt);
    this.hi = Math.max(this.hi, rtt);
    this.schedule();
  }

  /** The finished test; `ways`: which lost probes went which way, where known. */
  finalize(packets: (number | null)[], ways: { up: ReadonlySet<number>; down: ReadonlySet<number> } | null = null): void {
    this.cells = packets.map((p, i) => (p !== null ? p : ways?.up.has(i) ? "up" : ways?.down.has(i) ? "down" : "lost"));
    this.ways = ways !== null;
    const got = packets.filter((p): p is number => p !== null).sort((a, b) => a - b);
    this.lo = got[0] ?? 0;
    // Clip the colour scale at p98 so one outlier doesn't wash out the rest.
    this.hi = got[Math.floor(got.length * 0.98)] ?? 1;
    this.draw();
  }

  private schedule(): void {
    this.pending ||= requestAnimationFrame(() => {
      this.pending = 0;
      this.draw();
    });
  }

  private dark(): boolean {
    return isDarkTheme();
  }

  private color(cell: Cell, stops: string[]): string {
    if (cell === "unsent") return cssVar("--grid");
    if (cell === "inflight") return cssVar("--axis");
    if (typeof cell !== "number") return cssVar("--critical");
    const span = this.hi - this.lo;
    return ramp(stops, span > 0 ? (cell - this.lo) / span : 0.2);
  }

  private draw(): void {
    const W = this.width;
    const n = this.cells.length;
    if (!W) return;
    const gap = 2;
    const cols = W < 480 ? 25 : 50;
    const size = Math.max(4, Math.floor((W - gap * (cols - 1)) / cols));
    const rows = Math.max(1, Math.ceil(n / cols));
    this.layout = { cols, size, gap };
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const Hpx = rows * (size + gap) - gap;
    const Wpx = cols * (size + gap) - gap;
    this.canvas.style.width = `${Wpx}px`;
    this.canvas.style.height = `${Hpx}px`;
    this.canvas.width = Math.round(Wpx * dpr);
    this.canvas.height = Math.round(Hpx * dpr);
    const ctx = this.canvas.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const stops = this.dark() ? RAMP_DARK : RAMP_LIGHT;
    const radius = Math.min(2, size / 4);
    for (let i = 0; i < n; i++) {
      const cx = (i % cols) * (size + gap);
      const cy = Math.floor(i / cols) * (size + gap);
      const cell = this.cells[i]!;
      ctx.fillStyle = this.color(cell, stops);
      ctx.beginPath();
      ctx.roundRect(cx, cy, size, size, radius);
      ctx.fill();
      if (cell === "lost" && size >= 7) {
        // A cross keeps "lost" readable without colour.
        ctx.strokeStyle = cssVar("--surface");
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(cx + size * 0.28, cy + size * 0.28);
        ctx.lineTo(cx + size * 0.72, cy + size * 0.72);
        ctx.moveTo(cx + size * 0.72, cy + size * 0.28);
        ctx.lineTo(cx + size * 0.28, cy + size * 0.72);
        ctx.stroke();
      } else if ((cell === "up" || cell === "down") && size >= 6) {
        // Which way it was lost: a triangle pointing up (to the server) or down (back).
        const [top, bottom] = cell === "up" ? [0.24, 0.74] : [0.76, 0.26];
        ctx.fillStyle = cssVar("--surface");
        ctx.beginPath();
        ctx.moveTo(cx + size * 0.5, cy + size * top);
        ctx.lineTo(cx + size * 0.78, cy + size * bottom);
        ctx.lineTo(cx + size * 0.22, cy + size * bottom);
        ctx.closePath();
        ctx.fill();
      }
    }
    this.drawScale(stops);
  }

  private drawScale(stops: string[]): void {
    const has = this.hi >= this.lo && Number.isFinite(this.lo);
    this.scale.replaceChildren(
      h("span", { class: "scale-label" }, has ? text(ms(this.lo)) : "fast"),
      h("span", { class: "scale-bar", style: `background:linear-gradient(90deg, ${stops.join(",")})` }),
      h("span", { class: "scale-label" }, has ? `${text(ms(this.hi))}+` : "slow"),
      h("span", { class: "scale-gap" }),
      h("span", { class: "legend-key square", style: "--key:var(--critical)" }),
      h("span", { class: "scale-label" }, this.ways ? "lost: ▲ to server, ▼ from server" : "lost"),
      h("span", { class: "legend-key square", style: "--key:var(--axis)" }),
      h("span", { class: "scale-label" }, "in flight"),
    );
  }

  private hover(ev: PointerEvent): void {
    const { cols, size, gap } = this.layout;
    const at = pointerIn(ev, this.canvas, this.width);
    const col = Math.floor(at.x / (size + gap));
    const row = Math.floor(at.y / (size + gap));
    const i = row * cols + col;
    const cell = this.cells[i];
    if (cell === undefined || col >= cols) return hideTip();
    const value = typeof cell === "number" ? text(ms(cell)) : LOST.includes(cell) ? "Lost" : cell === "inflight" ? "In flight" : "Not sent";
    const label = typeof cell === "number" ? "round trip" : cell === "up" ? "on the way to the server" : cell === "down" ? "on the way back from the server" : "";
    showTip(ev.clientX, ev.clientY, `Probe #${i}`, [{ value, label }]);
  }
}
