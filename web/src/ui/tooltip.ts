// One shared tooltip. Values lead, labels follow; series are keyed with a short line.
import { $, clear, h } from "./dom";

export interface TipRow {
  value: string;
  label: string;
  color?: string; // CSS colour for the key
  shape?: "line" | "dot" | "square";
}

let el: HTMLElement | null = null;

export function showTip(clientX: number, clientY: number, title: string, rows: TipRow[]): void {
  el ??= $("#tooltip");
  clear(el);
  if (title) el.append(h("div", { class: "tip-title" }, title));
  for (const row of rows) {
    const key = row.color
      ? h("span", { class: `tip-key ${row.shape ?? "line"}`, style: `--key:${row.color}` })
      : h("span", { class: "tip-key none" });
    el.append(h("div", { class: "tip-row" }, key, h("strong", {}, row.value), h("span", {}, row.label)));
  }
  el.hidden = false;
  const pad = 14;
  const { width, height } = el.getBoundingClientRect();
  let x = clientX + pad;
  let y = clientY + pad;
  if (x + width > window.innerWidth - 8) x = clientX - width - pad;
  if (y + height > window.innerHeight - 8) y = clientY - height - pad;
  el.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
}

export function hideTip(): void {
  if (el) el.hidden = true;
}
