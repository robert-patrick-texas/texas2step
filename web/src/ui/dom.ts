// Tiny DOM helpers. All text goes through textContent; nothing builds HTML from data.

type Attrs = Record<string, string | number | boolean | null | undefined | EventListener>;
type Child = Node | string | number | null | undefined | false;

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  apply(el, attrs);
  append(el, children);
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";

export function s<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: Child[]
): SVGElementTagNameMap[K] {
  const el = document.createElementNS(SVG_NS, tag);
  apply(el, attrs);
  append(el, children);
  return el;
}

function apply(el: Element, attrs: Attrs): void {
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (typeof value === "function") el.addEventListener(key.replace(/^on/, ""), value);
    else if (key === "class") el.setAttribute("class", String(value));
    else el.setAttribute(key, value === true ? "" : String(value));
  }
}

function append(el: Element, children: Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
}

export function clear(el: Element): void {
  while (el.firstChild) el.firstChild.remove();
}

export function setText(el: Element | null, text: string): void {
  if (el && el.textContent !== text) el.textContent = text;
}

export function $(selector: string, root: ParentNode = document): HTMLElement {
  const el = root.querySelector<HTMLElement>(selector);
  if (!el) throw new Error(`missing ${selector}`);
  return el;
}

export const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Re-run `draw` whenever the element's width changes (charts redraw to fit). */
export function onResize(el: Element, draw: (width: number) => void): () => void {
  let last = -1;
  const ro = new ResizeObserver(([entry]) => {
    const w = Math.round(entry!.contentRect.width);
    if (w !== last && w > 0) {
      last = w;
      draw(w);
    }
  });
  ro.observe(el);
  return () => ro.disconnect();
}

/**
 * The pointer's position over `el` in the element's own pixels (`width` of them across),
 * whatever the page size: on-screen and layout pixels differ once the page is zoomed.
 */
export function pointerIn(ev: MouseEvent, el: Element, width: number): { x: number; y: number } {
  const box = el.getBoundingClientRect();
  const k = box.width ? width / box.width : 1;
  return { x: (ev.clientX - box.left) * k, y: (ev.clientY - box.top) * k };
}

export function cssVar(name: string, el: Element = document.documentElement): string {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

export function store<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? { ...fallback, ...(JSON.parse(raw) as T) } : fallback;
  } catch {
    return fallback;
  }
}

export function save(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private mode */
  }
}
