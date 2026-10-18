// Application shell: routing, theme, server selection and shared settings.
import { appPath, Endpoint, routeOf } from "../engine/net";
import type { Location, ServerInfo, WhoAmI } from "../types";
import { $, clear, h, save, setText, store } from "./dom";

export interface Settings {
  streams: number;
  seconds: number;
  packets: number;
  intervalMs: number;
  family: "auto" | "ipv4" | "ipv6";
}

export interface View {
  mount(root: HTMLElement, params: Record<string, string>): void;
  unmount?(): void;
}

type ServerListener = () => void;

export class App {
  readonly origin = new Endpoint("");
  ep: Endpoint = this.origin;
  info: ServerInfo | null = null;
  whoami: WhoAmI | null = null;
  locations: Location[] = [];
  selected: Location | null = null;
  settings: Settings;
  private listeners = new Set<ServerListener>();
  private current: View | null = null;
  private routes: [RegExp, () => View][] = [];

  constructor() {
    this.settings = store<Settings>("t2s.settings", { streams: 4, seconds: 10, packets: 500, intervalMs: 10, family: "auto" });
  }

  route(pattern: RegExp, view: () => View): this {
    this.routes.push([pattern, view]);
    return this;
  }

  async start(): Promise<void> {
    this.initTheme();
    document.addEventListener("click", (ev) => {
      const a = (ev.target as Element).closest<HTMLAnchorElement>("a[data-link]");
      if (!a || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
      ev.preventDefault();
      this.navigate(routeOf(new URL(a.href).pathname));
    });
    window.addEventListener("popstate", () => this.render());
    this.render();
    await this.loadLocations();
  }

  /** Go to an in-app route such as "/monitor". */
  navigate(route: string, replace = false): void {
    if (route === routeOf(window.location.pathname)) return;
    if (replace) history.replaceState(null, "", appPath(route));
    else history.pushState(null, "", appPath(route));
    this.render();
  }

  private render(): void {
    const path = routeOf(window.location.pathname);
    for (const a of document.querySelectorAll<HTMLAnchorElement>(".tabs a")) {
      const href = routeOf(new URL(a.href).pathname);
      const active = href === "/" ? path === "/" || path.startsWith("/r/") : path.startsWith(href);
      a.toggleAttribute("aria-current", active);
      if (active) a.setAttribute("aria-current", "page");
    }
    for (const [pattern, make] of this.routes) {
      const match = pattern.exec(path);
      if (!match) continue;
      this.current?.unmount?.();
      const root = $("#view");
      clear(root);
      this.current = make();
      this.current.mount(root, match.groups ?? {});
      return;
    }
    this.navigate("/", true);
  }

  saveSettings(): void {
    save("t2s.settings", this.settings);
  }

  onServer(fn: ServerListener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  // ---- Servers -------------------------------------------------------------------

  private async loadLocations(): Promise<void> {
    try {
      this.locations = await this.origin.locations();
    } catch {
      this.locations = [];
    }
    const saved = store<{ id: string }>("t2s.server", { id: "" }).id;
    const pick = this.locations.find((l) => l.id === saved) ?? this.locations[0] ?? null;
    this.renderPicker();
    await this.select(pick);
  }

  /** The base URL for the chosen server and address family. */
  private baseFor(loc: Location | null): string {
    if (!loc) return "";
    if (this.settings.family === "ipv4" && loc.url_v4) return loc.url_v4;
    if (this.settings.family === "ipv6" && loc.url_v6) return loc.url_v6;
    return loc.url;
  }

  async select(loc: Location | null): Promise<void> {
    this.selected = loc;
    if (loc) save("t2s.server", { id: loc.id });
    this.ep = new Endpoint(this.baseFor(loc));
    this.info = null;
    this.whoami = null;
    try {
      [this.info, this.whoami] = await Promise.all([this.ep.info(), this.ep.whoami()]);
    } catch (err) {
      console.warn("server unreachable", err);
    }
    setText($("#footer-version"), this.info ? `texas2step ${this.info.version}` : "texas2step");
    const picker = document.querySelector("#server-picker .picker-status");
    picker?.classList.toggle("down", !this.info);
    for (const fn of this.listeners) fn();
  }

  async refreshFamily(): Promise<void> {
    await this.select(this.selected);
  }

  private renderPicker(): void {
    const root = $("#server-picker");
    clear(root);
    if (this.locations.length <= 1) {
      const loc = this.locations[0];
      root.append(
        h(
          "div",
          { class: "picker single", title: "Test server" },
          h("span", { class: "picker-status" }),
          h("span", { class: "picker-name" }, loc?.name ?? "This server"),
          loc?.location ? h("span", { class: "picker-loc" }, loc.location) : null,
        ),
      );
      return;
    }
    const select = h("select", { class: "picker-select", "aria-label": "Test server" });
    for (const loc of this.locations) {
      const label = loc.location ? `${loc.name} — ${loc.location}` : loc.name;
      const opt = h("option", { value: loc.id }, label);
      if (loc.id === this.selected?.id || (!this.selected && loc === this.locations[0])) opt.selected = true;
      select.append(opt);
    }
    select.addEventListener("change", () => void this.select(this.locations.find((l) => l.id === select.value) ?? null));
    root.append(h("label", { class: "picker" }, h("span", { class: "picker-status" }), select));
  }

  // ---- Theme ---------------------------------------------------------------------

  private initTheme(): void {
    const btn = $("#theme-toggle");
    const modes = ["system", "light", "dark"] as const;
    const icons: Record<(typeof modes)[number], string> = {
      system: "M12 3a9 9 0 1 0 0 18zm0 1.5a7.5 7.5 0 0 1 0 15z",
      light: "M12 7a5 5 0 1 1 0 10 5 5 0 0 1 0-10zm0-5v3m0 14v3M2 12h3m14 0h3M4.9 4.9l2.1 2.1m10 10 2.1 2.1m0-14.2-2.1 2.1m-10 10-2.1 2.1",
      dark: "M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z",
    };
    let mode: (typeof modes)[number] = (() => {
      try {
        const t = localStorage.getItem("t2s.theme");
        return t === "light" || t === "dark" ? t : "system";
      } catch {
        return "system";
      }
    })();
    const paint = () => {
      if (mode === "system") delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = mode;
      btn.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icons[mode]}"/></svg>`;
      btn.setAttribute("aria-label", `Colour theme: ${mode}. Click to change.`);
      btn.title = `Theme: ${mode}`;
      try {
        if (mode === "system") localStorage.removeItem("t2s.theme");
        else localStorage.setItem("t2s.theme", mode);
      } catch {
        /* ignore */
      }
    };
    btn.addEventListener("click", () => {
      mode = modes[(modes.indexOf(mode) + 1) % modes.length]!;
      paint();
    });
    paint();
  }
}
