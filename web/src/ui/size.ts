// Page size: a drop-down next to the theme button that scales the whole page, like
// browser zoom but for this site only, so more of each view fits without scrolling.
//
// The size is CSS zoom on <body>, via --ui-zoom. An inline script in index.html applies
// the saved size before first paint; this module takes over from there.
import { h } from "./dom";

const KEY = "t2s.size";
const EVENT = "t2s:size";
const SIZES = [0.7, 0.8, 0.9, 1, 1.1, 1.25];

/** The page's zoom factor; 1 unless the visitor picked another size. */
export function pageZoom(): number {
  return parseFloat(getComputedStyle(document.body).zoom) || 1;
}

/** Run `fn` after every size change while `el` is on the page. */
export function onSizeChange(el: Element, fn: () => void): void {
  const handler = () => (el.isConnected ? fn() : window.removeEventListener(EVENT, handler));
  window.addEventListener(EVENT, handler);
}

function saved(): number {
  try {
    const value = parseFloat(localStorage.getItem(KEY) ?? "");
    if (SIZES.includes(value)) return value;
  } catch {
    /* storage blocked */
  }
  return 1;
}

function apply(size: number): void {
  document.documentElement.style.setProperty("--ui-zoom", String(size));
  try {
    if (size === 1) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, String(size));
  } catch {
    /* the choice lasts for this page only */
  }
  window.dispatchEvent(new Event(EVENT));
}

/** The size drop-down, rendered into `root`. */
export function mountSizePicker(root: HTMLElement): void {
  const current = saved();
  const select = h("select", { class: "picker-select", "aria-label": "Page size" });
  for (const size of SIZES) {
    select.append(h("option", { value: String(size), selected: size === current }, `${Math.round(size * 100)}%`));
  }
  select.addEventListener("change", () => apply(parseFloat(select.value)));
  root.append(h("label", { class: "picker size-picker", title: "Page size" }, h("span", { class: "size-glyph", "aria-hidden": "true" }, "Aa"), select));
}
