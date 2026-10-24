// Scales and axis furniture shared by the SVG charts.
import { s } from "../dom";
import { compact, niceTicks } from "../format";

export interface Scale {
  (v: number): number;
  ticks: number[];
  domain: [number, number];
}

export function linear(d0: number, d1: number, r0: number, r1: number, ticks?: number[]): Scale {
  const span = d1 - d0 || 1;
  const f = ((v: number) => r0 + ((v - d0) / span) * (r1 - r0)) as Scale;
  f.ticks = ticks ?? niceTicks(d1);
  f.domain = [d0, d1];
  return f;
}

/** Log scale snapped to 1-2-5 decades, for latency spanning idle (ms) and loaded (100s ms). */
export function log125(min: number, max: number, r0: number, r1: number): Scale {
  const steps: number[] = [];
  for (let e = -2; e <= 5; e++) for (const m of [1, 2, 5]) steps.push(m * 10 ** e);
  const lo = [...steps].reverse().find((v) => v <= Math.max(min, 0.01)) ?? 0.01;
  const hi = steps.find((v) => v >= max * 1.05) ?? 1e5;
  const l0 = Math.log10(lo);
  const l1 = Math.log10(hi);
  const f = ((v: number) => r0 + ((Math.log10(Math.max(v, lo)) - l0) / (l1 - l0)) * (r1 - r0)) as Scale;
  f.ticks = steps.filter((v) => v >= lo && v <= hi);
  if (f.ticks.length > 9) f.ticks = f.ticks.filter((v) => /^[15]/.test(String(v)) || v === lo || v === hi);
  f.domain = [lo, hi];
  return f;
}

export function gridY(y: Scale, x0: number, x1: number, format = compact): SVGGElement {
  const g = s("g", { class: "axis axis-y" });
  for (const t of y.ticks) {
    const py = y(t);
    g.append(s("line", { x1: x0, x2: x1, y1: py, y2: py, class: t === 0 ? "baseline" : "grid" }));
    g.append(s("text", { x: x0 - 8, y: py, class: "tick", "text-anchor": "end", "dominant-baseline": "middle" }, format(t)));
  }
  return g;
}

export function axisX(x: Scale, y0: number, format: (v: number) => string, gridTop?: number): SVGGElement {
  const g = s("g", { class: "axis axis-x" });
  for (const t of x.ticks) {
    const px = x(t);
    if (gridTop !== undefined) g.append(s("line", { x1: px, x2: px, y1: gridTop, y2: y0, class: "grid" }));
    g.append(s("text", { x: px, y: y0 + 18, class: "tick", "text-anchor": "middle" }, format(t)));
  }
  return g;
}

export function areaPath(points: [number, number][], baseline: number): string {
  if (!points.length) return "";
  const line = points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
  return `${line}L${points[points.length - 1]![0].toFixed(1)},${baseline}L${points[0]![0].toFixed(1)},${baseline}Z`;
}

export function linePath(points: [number, number][]): string {
  return points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
}

/** Deterministic pseudo-random in [-1, 1] for strip-plot jitter (stable across redraws). */
export function hashJitter(i: number): number {
  const x = Math.sin(i * 12.9898 + 78.233) * 43758.5453;
  return (x - Math.floor(x)) * 2 - 1;
}

export function legend(items: { label: string; color: string; shape: "line" | "dash" | "square" }[]): HTMLElement {
  const el = document.createElement("div");
  el.className = "legend";
  for (const item of items) {
    const key = document.createElement("span");
    key.className = `legend-key ${item.shape}`;
    key.style.setProperty("--key", item.color);
    const label = document.createElement("span");
    label.textContent = item.label;
    const row = document.createElement("span");
    row.className = "legend-item";
    row.append(key, label);
    el.append(row);
  }
  return el;
}

/** Linear interpolation across colour stops (hex), t in [0, 1]. */
export function ramp(stops: string[], t: number): string {
  const c = stops.map((hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)));
  const x = Math.min(1, Math.max(0, t)) * (c.length - 1);
  const i = Math.min(c.length - 2, Math.floor(x));
  const f = x - i;
  const [a, b] = [c[i]!, c[i + 1]!];
  return `rgb(${a.map((v, k) => Math.round(v + (b[k]! - v) * f)).join(",")})`;
}
