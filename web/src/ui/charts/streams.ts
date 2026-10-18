// Per-stream throughput: how evenly the parallel flows shared the path.
import { fairness } from "../../engine/stats";
import type { Direction } from "../../types";
import { clear, h, onResize, s } from "../dom";
import { rate, text } from "../format";
import { hideTip, showTip } from "../tooltip";
import { legend, linear } from "./common";

const COLOR: Record<Direction, string> = { download: "var(--dl)", upload: "var(--ul)" };

export class StreamsChart {
  readonly el: HTMLElement;
  private plot: HTMLElement;
  private summary: HTMLElement;
  private data: Partial<Record<Direction, number[]>> = {};
  private width = 0;

  constructor() {
    this.plot = h("div", { class: "chart-plot", role: "img", "aria-label": "Throughput per parallel stream" });
    this.summary = h("div", { class: "fairness" });
    this.el = h(
      "div",
      { class: "chart" },
      legend([
        { label: "Download", color: COLOR.download, shape: "square" },
        { label: "Upload", color: COLOR.upload, shape: "square" },
      ]),
      this.plot,
      this.summary,
    );
    onResize(this.plot, (w) => {
      this.width = w;
      this.render();
    });
  }

  set(direction: Direction, perStream: number[] | undefined): void {
    if (perStream) this.data[direction] = perStream;
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
    const dirs = (["download", "upload"] as const).filter((d) => this.data[d]?.length);
    const n = Math.max(0, ...dirs.map((d) => this.data[d]!.length));
    const bar = Math.min(12, n > 8 ? 8 : 12);
    const group = dirs.length * bar + (dirs.length - 1) * 2 + 12;
    const m = { top: 4, right: 84, bottom: 4, left: 34 };
    const H = Math.max(60, m.top + m.bottom + n * group);
    const max = Math.max(1, ...dirs.flatMap((d) => this.data[d]!));
    const x = linear(0, max, m.left, W - m.right);
    const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, class: "chart-svg" });

    svg.append(s("line", { x1: m.left, x2: m.left, y1: m.top, y2: H - m.bottom, class: "baseline" }));
    for (let i = 0; i < n; i++) {
      const top = m.top + i * group + 6;
      svg.append(s("text", { x: m.left - 8, y: top + (group - 12) / 2, class: "tick", "text-anchor": "end", "dominant-baseline": "middle" }, `#${i + 1}`));
      dirs.forEach((d, k) => {
        const v = this.data[d]![i] ?? 0;
        const y = top + k * (bar + 2);
        const w = Math.max(2, x(v) - m.left);
        // Square at the baseline, 4px rounded data-end.
        const r = Math.min(4, w / 2, bar / 2);
        const path = `M${m.left},${y}H${m.left + w - r}Q${m.left + w},${y} ${m.left + w},${y + r}V${y + bar - r}Q${m.left + w},${y + bar} ${m.left + w - r},${y + bar}H${m.left}Z`;
        const el = s("path", { d: path, class: "bar", style: `fill:${COLOR[d]}` });
        el.addEventListener("pointermove", (ev) =>
          showTip(ev.clientX, ev.clientY, `Stream ${i + 1}`, [{ value: text(rate(v)), label: d, color: COLOR[d], shape: "square" }]),
        );
        el.addEventListener("pointerleave", hideTip);
        svg.append(el);
        if (n <= 8 || k === 0) {
          svg.append(s("text", { x: m.left + w + 6, y: y + bar / 2, class: "bar-label", "dominant-baseline": "middle" }, text(rate(v))));
        }
      });
    }
    if (!n) svg.append(s("text", { x: W / 2, y: H / 2, class: "empty", "text-anchor": "middle" }, "Per-stream rates appear after each phase"));
    clear(this.plot);
    this.plot.append(svg);

    clear(this.summary);
    for (const d of dirs) {
      const f = fairness(this.data[d]!);
      if (f === null || this.data[d]!.length < 2) continue;
      this.summary.append(
        h(
          "span",
          { class: "fair-item", title: "Jain's fairness index: 100% means every stream got an equal share" },
          h("span", { class: "legend-key square", style: `--key:${COLOR[d]}` }),
          `${d === "download" ? "Download" : "Upload"} fairness `,
          h("strong", {}, `${(f * 100).toFixed(1)}%`),
        ),
      );
    }
  }
}
