// Throughput over time: download and upload on one Mbps axis, ramp-up shaded.
import type { Direction } from "../../types";
import { clear, h, onResize, pointerIn, s } from "../dom";
import { mbpsTick, rate, text } from "../format";
import { hideTip, showTip } from "../tooltip";
import { areaPath, axisX, gridY, legend, linear, linePath } from "./common";

export interface ThroughputSeries {
  timeline: number[]; // Mbps per bin
  binMs: number;
  warmupS: number;
  durationS: number;
  stable: number | null; // null while still running
}

const LABEL: Record<Direction, string> = { download: "Download", upload: "Upload" };
const COLOR: Record<Direction, string> = { download: "var(--dl)", upload: "var(--ul)" };

export class ThroughputChart {
  readonly el: HTMLElement;
  private plot: HTMLElement;
  private data: Partial<Record<Direction, ThroughputSeries>> = {};
  private width = 0;

  constructor() {
    this.plot = h("div", { class: "chart-plot", role: "img", "aria-label": "Throughput over time" });
    this.el = h(
      "div",
      { class: "chart" },
      legend([
        { label: "Download", color: COLOR.download, shape: "line" },
        { label: "Upload", color: COLOR.upload, shape: "line" },
      ]),
      this.plot,
    );
    onResize(this.plot, (w) => {
      this.width = w;
      this.render();
    });
  }

  set(direction: Direction, series: ThroughputSeries | undefined): void {
    if (series) this.data[direction] = series;
    else delete this.data[direction];
    this.render();
  }

  reset(): void {
    this.data = {};
    this.render();
  }

  private render(): void {
    const W = this.width;
    if (!W) return;
    const H = 210;
    const m = { top: 16, right: 16, bottom: 30, left: 52 };
    const series = (Object.entries(this.data) as [Direction, ThroughputSeries][]).filter(([, d]) => d.timeline.length || d.stable !== null);
    const duration = Math.max(1, ...series.map(([, d]) => d.durationS));
    const peak = Math.max(1, ...series.flatMap(([, d]) => d.timeline));
    const x = linear(0, duration, m.left, W - m.right);
    x.ticks = Array.from({ length: Math.floor(duration / (duration > 12 ? 5 : 2)) + 1 }, (_, i) => i * (duration > 12 ? 5 : 2));
    const y = linear(0, peak * 1.12, H - m.bottom, m.top);
    y.ticks = y.ticks.filter((t) => t <= peak * 1.12);

    const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, class: "chart-svg" });
    const warm = Math.max(0, ...series.map(([, d]) => d.warmupS));
    if (warm > 0) {
      svg.append(s("rect", { x: x(0), y: m.top, width: x(warm) - x(0), height: H - m.bottom - m.top, class: "warmup" }));
      svg.append(s("text", { x: x(0) + 8, y: m.top + 14, class: "annot" }, "ramp-up, excluded"));
    }
    if (series.length) {
      svg.append(gridY(y, m.left, W - m.right, mbpsTick));
      svg.append(axisX(x, H - m.bottom, (v) => `${v}s`));
    } else {
      svg.append(s("line", { x1: m.left, x2: W - m.right, y1: y(0), y2: y(0), class: "baseline" }));
    }

    for (const [dir, d] of series) {
      const pts = d.timeline.map((v, i): [number, number] => [x(((i + 0.5) * d.binMs) / 1000), y(v)]);
      if (pts.length) {
        svg.append(s("path", { d: areaPath(pts, y(0)), class: "area", style: `fill:${COLOR[dir]}` }));
        svg.append(s("path", { d: linePath(pts), class: "line", style: `stroke:${COLOR[dir]}` }));
      }
      if (d.stable !== null && d.stable > 0) {
        const sy = y(d.stable);
        svg.append(s("line", { x1: x(d.warmupS), x2: W - m.right, y1: sy, y2: sy, class: "ref", style: `stroke:${COLOR[dir]}` }));
        const label = `${LABEL[dir]} ${text(rate(d.stable))}`;
        svg.append(s("text", { x: W - m.right - 4, y: sy - 7, class: "direct-label", "text-anchor": "end" }, label));
      }
    }

    // Crosshair layer.
    const cross = s("line", { y1: m.top, y2: H - m.bottom, class: "crosshair", visibility: "hidden" });
    const dots = s("g", {});
    const hit = s("rect", { x: m.left, y: m.top, width: W - m.left - m.right, height: H - m.top - m.bottom, class: "hit" });
    hit.addEventListener("pointermove", (ev) => {
      const t = Math.max(0, ((pointerIn(ev, svg, W).x - m.left) / (W - m.left - m.right)) * duration);
      const rows = [];
      clear(dots);
      let px = 0;
      for (const [dir, d] of series) {
        const i = Math.min(d.timeline.length - 1, Math.floor((t * 1000) / d.binMs));
        const v = d.timeline[i];
        if (v === undefined) continue;
        px = x(((i + 0.5) * d.binMs) / 1000);
        dots.append(s("circle", { cx: px, cy: y(v), r: 4, class: "dot", style: `fill:${COLOR[dir]}` }));
        rows.push({ value: text(rate(v)), label: LABEL[dir], color: COLOR[dir] });
      }
      if (!rows.length) return;
      cross.setAttribute("x1", String(px));
      cross.setAttribute("x2", String(px));
      cross.setAttribute("visibility", "visible");
      showTip(ev.clientX, ev.clientY, `${t.toFixed(1)} s`, rows);
    });
    hit.addEventListener("pointerleave", () => {
      cross.setAttribute("visibility", "hidden");
      clear(dots);
      hideTip();
    });
    svg.append(cross, dots, hit);

    if (!series.length) {
      svg.append(s("text", { x: W / 2, y: H / 2, class: "empty", "text-anchor": "middle" }, "Run a test to see throughput over time"));
    }
    clear(this.plot);
    this.plot.append(svg);
  }
}
