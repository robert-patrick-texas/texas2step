// Latency distribution: one strip per condition (idle, loaded by download, loaded by
// upload) on a shared log axis, with the interquartile box and median marked.
import type { LatencyStats } from "../../types";
import { clear, h, onResize, pointerIn, s } from "../dom";
import { ms, text } from "../format";
import { hideTip, showTip } from "../tooltip";
import { hashJitter, log125 } from "./common";

export type LatencyKind = "idle" | "download" | "upload";

const ROWS: { kind: LatencyKind; label: string; color: string }[] = [
  { kind: "idle", label: "Idle", color: "var(--idle)" },
  { kind: "download", label: "During download", color: "var(--dl)" },
  { kind: "upload", label: "During upload", color: "var(--ul)" },
];

export class LatencyChart {
  readonly el: HTMLElement;
  private plot: HTMLElement;
  private samples: Record<LatencyKind, number[]> = { idle: [], download: [], upload: [] };
  private stats: Partial<Record<LatencyKind, LatencyStats | null>> = {};
  private width = 0;
  private pending = 0;

  constructor() {
    this.plot = h("div", { class: "chart-plot", role: "img", "aria-label": "Latency distribution, idle and under load" });
    this.el = h("div", { class: "chart" }, this.plot);
    onResize(this.plot, (w) => {
      this.width = w;
      this.render();
    });
  }

  reset(): void {
    this.samples = { idle: [], download: [], upload: [] };
    this.stats = {};
    this.render();
  }

  add(kind: LatencyKind, rtt: number): void {
    this.samples[kind].push(rtt);
    // Coalesce bursts of samples into one redraw per frame.
    this.pending ||= requestAnimationFrame(() => {
      this.pending = 0;
      this.render();
    });
  }

  setStats(kind: LatencyKind, stats: LatencyStats | null | undefined): void {
    this.stats[kind] = stats ?? null;
    if (stats?.samples) this.samples[kind] = [...stats.samples];
    this.render();
  }

  private render(): void {
    const W = this.width;
    if (!W) return;
    const narrow = W < 520;
    const labelW = narrow ? 0 : 128;
    const valueW = 72;
    const rowH = narrow ? 64 : 52;
    const m = { top: 8, bottom: 30, left: labelW + 8, right: valueW };
    const H = m.top + rowH * ROWS.length + m.bottom + (narrow ? 16 : 0);
    const all = ROWS.flatMap((r) => this.samples[r.kind]);
    const x = log125(all.length ? Math.min(...all) : 1, all.length ? Math.max(...all) : 100, m.left, W - m.right);

    const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, class: "chart-svg" });
    const axis = s("g", { class: "axis" });
    for (const t of x.ticks) {
      const px = x(t);
      axis.append(s("line", { x1: px, x2: px, y1: m.top, y2: H - m.bottom, class: "grid" }));
      axis.append(s("text", { x: px, y: H - m.bottom + 18, class: "tick", "text-anchor": "middle" }, t >= 1000 ? `${t / 1000}s` : String(t)));
    }
    axis.append(s("text", { x: narrow ? m.left : 0, y: H - m.bottom + (narrow ? 34 : 18), class: "tick unit", "text-anchor": "start" }, "ms, log scale"));
    svg.append(axis);

    ROWS.forEach((row, r) => {
      const top = m.top + r * rowH;
      const cy = top + (narrow ? rowH * 0.62 : rowH / 2);
      const data = this.samples[row.kind];
      const st = this.stats[row.kind];
      if (narrow) svg.append(s("text", { x: 0, y: top + 14, class: "row-label" }, row.label));
      else svg.append(s("text", { x: 0, y: cy, class: "row-label", "dominant-baseline": "middle" }, row.label));

      if (st && st.p25 !== null && st.p75 !== null && st.median !== null) {
        svg.append(s("rect", { x: x(st.p25), y: cy - 13, width: Math.max(2, x(st.p75) - x(st.p25)), height: 26, rx: 4, class: "iqr", style: `fill:${row.color}` }));
        svg.append(s("line", { x1: x(st.median), x2: x(st.median), y1: cy - 15, y2: cy + 15, class: "median", style: `stroke:${row.color}` }));
        svg.append(s("text", { x: W - m.right + 10, y: cy, class: "direct-label", "dominant-baseline": "middle" }, text(ms(st.median))));
      } else if (!data.length) {
        svg.append(s("line", { x1: m.left, x2: W - m.right, y1: cy, y2: cy, class: "placeholder" }));
      }
      const dots = s("g", {});
      data.forEach((v, i) => {
        dots.append(s("circle", { cx: x(v), cy: cy + hashJitter(i + r * 997) * 9, r: 3.5, class: "dot", style: `fill:${row.color}` }));
      });
      svg.append(dots);

      // Nearest-point hover across the whole row band.
      const hit = s("rect", { x: m.left, y: top, width: W - m.left - m.right, height: rowH, class: "hit" });
      hit.addEventListener("pointermove", (ev) => {
        if (!data.length) return;
        const px = pointerIn(ev, svg, W).x;
        let best = 0;
        data.forEach((v, i) => {
          if (Math.abs(x(v) - px) < Math.abs(x(data[best]!) - px)) best = i;
        });
        const rows = [{ value: text(ms(data[best]!)), label: `sample ${best + 1} of ${data.length}`, color: row.color, shape: "dot" as const }];
        if (st?.median != null) rows.push({ value: text(ms(st.median)), label: "median", color: row.color, shape: "dot" as const });
        if (st?.jitter != null) rows.push({ value: text(ms(st.jitter)), label: "jitter", color: row.color, shape: "dot" as const });
        showTip(ev.clientX, ev.clientY, row.label, rows);
      });
      hit.addEventListener("pointerleave", hideTip);
      svg.append(hit);
    });

    clear(this.plot);
    this.plot.append(svg);
  }
}
