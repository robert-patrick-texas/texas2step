// Colour themes: the picker in the top bar, the visitor's saved choice, and a change
// notification for charts that paint in theme colours.
//
// A theme is a data-theme attribute on <html>, matched by token blocks in styles.css.
// "system" leaves the attribute off, so the page follows the OS light/dark setting. An
// inline script in index.html applies the saved theme (or the site's default) before
// first paint; this module takes over from there.
import { h, s } from "./dom";

export const THEMES = [
  { id: "system", label: "Match system" },
  { id: "light", label: "Light" },
  { id: "dark", label: "Dark" },
  { id: "america", label: "America" },
  { id: "energy", label: "Energy" },
  { id: "federal", label: "Federal" },
  { id: "military", label: "Military" },
  { id: "texas", label: "Texas" },
] as const;

export type ThemeId = (typeof THEMES)[number]["id"];

const KEY = "t2s.theme";
const EVENT = "t2s:theme";
const isTheme = (value: unknown): value is ThemeId => THEMES.some((t) => t.id === value);

/** The site's default theme, which the server writes onto <html>. */
function siteDefault(): ThemeId {
  const value = document.documentElement.dataset.defaultTheme;
  return isTheme(value) ? value : "system";
}

function current(): ThemeId {
  try {
    const saved = localStorage.getItem(KEY);
    if (isTheme(saved)) return saved;
  } catch {
    /* storage blocked: use the default */
  }
  return siteDefault();
}

function choose(theme: ThemeId): void {
  const root = document.documentElement;
  if (theme === "system") delete root.dataset.theme;
  else root.dataset.theme = theme;
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* the choice lasts for this page only */
  }
}

/** True when the active theme draws on dark surfaces. */
export function isDarkTheme(): boolean {
  return getComputedStyle(document.documentElement).colorScheme === "dark";
}

/** Run `fn` after every theme change, from the picker or the OS, while `el` is on the page. */
export function onThemeChange(el: Element, fn: () => void): void {
  const handler = () => (el.isConnected ? fn() : window.removeEventListener(EVENT, handler));
  window.addEventListener(EVENT, handler);
}

function announceChanges(): void {
  const announce = () => window.dispatchEvent(new Event(EVENT));
  new MutationObserver(announce).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", announce);
}

function swatch(theme: ThemeId): HTMLElement {
  if (theme === "system") {
    return h(
      "span",
      { class: "theme-swatch split", "aria-hidden": "true" },
      h("span", { "data-theme": "light" }),
      h("span", { "data-theme": "dark" }),
    );
  }
  return h("span", { class: "theme-swatch", "data-theme": theme, "aria-hidden": "true" });
}

const PALETTE_ICON =
  "M12 3a9 9 0 1 0 0 18c1.2 0 1.8-.9 1.8-1.8 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-1 .8-1.8 1.8-1.8H17a4 4 0 0 0 4-4C21 6.6 17 3 12 3z";

/** The theme button and its menu, rendered into `root`. */
export function mountThemeMenu(root: HTMLElement): void {
  announceChanges();

  const check = () =>
    s("svg", { class: "theme-check", viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d: "M5 12.5l4.5 4.5L19 7.5" }));
  const items = THEMES.map((t) =>
    h("button", { class: "theme-item", type: "button", role: "menuitemradio", "data-value": t.id }, swatch(t.id), h("span", {}, t.label), check()),
  );
  const pop = h("div", { class: "theme-pop", role: "menu", id: "theme-pop", "aria-label": "Theme", hidden: true }, h("div", { class: "theme-pop-title", "aria-hidden": "true" }, "Theme"), ...items);
  const button = h(
    "button",
    { class: "icon-btn", type: "button", "aria-haspopup": "menu", "aria-expanded": "false", "aria-controls": "theme-pop" },
    s(
      "svg",
      { viewBox: "0 0 24 24", "aria-hidden": "true" },
      s("path", { d: PALETTE_ICON }),
      s("circle", { cx: "7.5", cy: "11", r: "1.2" }),
      s("circle", { cx: "10.5", cy: "7", r: "1.2" }),
      s("circle", { cx: "15", cy: "7.5", r: "1.2" }),
    ),
  );
  root.append(button, pop);

  const show = () => {
    const theme = current();
    const label = THEMES.find((t) => t.id === theme)!.label;
    button.setAttribute("aria-label", `Theme: ${label}`);
    button.title = `Theme: ${label}`;
    for (const item of items) item.setAttribute("aria-checked", String(item.dataset.value === theme));
  };

  const open = (focus: boolean) => {
    pop.hidden = false;
    button.setAttribute("aria-expanded", "true");
    if (focus) (items.find((i) => i.getAttribute("aria-checked") === "true") ?? items[0])!.focus();
  };
  const close = (refocus: boolean) => {
    if (pop.hidden) return;
    pop.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (refocus) button.focus();
  };

  button.addEventListener("click", () => (pop.hidden ? open(false) : close(false)));
  button.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      open(true);
    }
  });
  for (const item of items) {
    item.addEventListener("click", () => {
      const theme = item.dataset.value;
      if (isTheme(theme)) choose(theme);
      show();
      close(true);
    });
  }
  pop.addEventListener("keydown", (ev) => {
    if (ev.key === "Tab") return close(false); // and let focus move on
    const at = items.indexOf(document.activeElement as HTMLButtonElement);
    const move = (to: number) => items[(to + items.length) % items.length]!.focus();
    if (ev.key === "ArrowDown") move(at + 1);
    else if (ev.key === "ArrowUp") move(at - 1);
    else if (ev.key === "Home") move(0);
    else if (ev.key === "End") move(items.length - 1);
    else if (ev.key === "Escape") close(true);
    else return;
    ev.preventDefault();
  });
  document.addEventListener("click", (ev) => {
    if (!root.contains(ev.target as Node)) close(false);
  });

  show();
}
