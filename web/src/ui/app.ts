// Application shell: routing, theme, server selection and shared settings.
import { appPath, Endpoint, type Family, routeOf, whenDenied } from "../engine/net";
import type { Location, ServerInfo, WhoAmI } from "../types";
import { $, clear, h, save, setText, store } from "./dom";
import { admit, forgetPass } from "./gate";
import { mountSizePicker } from "./size";
import { mountThemeMenu } from "./theme";

export interface Settings {
  streams: number;
  seconds: number;
  packets: number;
  intervalMs: number;
  oneStream: boolean;
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
  /** Whether this visitor gets the service desk's functions (saved results, share links). */
  desk = true;
  settings: Settings;
  /** The pages this site shows: all of them unless the server lists fewer. */
  readonly pages = new Set((document.documentElement.dataset.pages ?? "test monitor diag history api").split(" "));
  /** Where an address with no page of its own lands: the first page shown. */
  readonly home = this.pages.has("test") ? "/" : `/${[...this.pages][0]}`;
  private listeners = new Set<ServerListener>();
  private current: View | null = null;
  private routes: [RegExp, () => View][] = [];

  constructor() {
    this.settings = store<Settings>("t2s.settings", { streams: 4, seconds: 10, packets: 500, intervalMs: 10, oneStream: true, family: "auto" });
  }

  route(pattern: RegExp, view: () => View): this {
    this.routes.push([pattern, view]);
    return this;
  }

  async start(): Promise<void> {
    mountSizePicker($("#size-picker"));
    mountThemeMenu($("#theme-menu"));
    for (const event of ["resize", "t2s:size"]) window.addEventListener(event, () => this.fitHeader());
    void document.fonts?.ready.then(() => this.fitHeader());
    this.fitHeader();
    // A server that asks for the desk's code gets it before anything else; when the pass
    // runs out, the page starts over at the code screen.
    whenDenied(() => {
      forgetPass();
      window.location.reload();
    });
    this.desk = (await admit(this.origin))?.desk ?? true;
    // The page leaves out its version until the code is in (it would help an attacker).
    const version = document.querySelector<HTMLMetaElement>('meta[name="texas2step-version"]');
    if (version && !version.content) {
      try {
        version.content = (await this.origin.json<ServerInfo>("/info")).version;
      } catch {
        /* the results then say "dev" */
      }
    }
    this.fitHeader(); // the tabs are back
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
    this.navigate(this.home, true);
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
    // Chosen before the picker is drawn, so the header shows the remembered server.
    this.selected = this.locations.find((l) => l.id === saved) ?? this.locations[0] ?? null;
    this.renderPicker();
    await this.select(this.selected);
  }

  /** The address family the chosen setting holds `loc` to: only where it has that hostname. */
  familyFor(loc: Location | null): Family | null {
    const f = this.settings.family;
    if (f === "ipv4" && loc?.url_v4) return "ipv4";
    if (f === "ipv6" && loc?.url_v6) return "ipv6";
    return null;
  }

  /** An endpoint for any listed server, in the chosen address family. */
  endpointFor(loc: Location): Endpoint {
    const family = this.familyFor(loc);
    if (family === "ipv4") return new Endpoint(loc.url_v4, family);
    if (family === "ipv6") return new Endpoint(loc.url_v6, family);
    return new Endpoint(loc.url);
  }

  async select(loc: Location | null): Promise<void> {
    this.selected = loc;
    if (loc) save("t2s.server", { id: loc.id });
    this.ep = loc ? this.endpointFor(loc) : this.origin;
    this.renderFamily();
    this.info = null;
    this.whoami = null;
    try {
      [this.info, this.whoami] = await Promise.all([this.ep.info(), this.ep.whoami()]);
    } catch (err) {
      console.warn("server unreachable", err);
    }
    // A site with brand_credit = "none" has no credit in its footer.
    setText(document.querySelector("#footer-version"), this.info ? `texas2step ${this.info.version}` : "texas2step");
    const picker = document.querySelector("#server-picker .picker-status");
    picker?.classList.toggle("down", !this.info);
    for (const fn of this.listeners) fn();
  }

  async refreshFamily(): Promise<void> {
    await this.select(this.selected);
  }

  /**
   * The address-family drop-down, for a server with an IPv4-only or IPv6-only hostname.
   * A browser picks the family itself for a name with both, so only those hostnames can
   * hold a test to one; elsewhere the drop-down is hidden.
   */
  private renderFamily(): void {
    const root = $("#family-picker");
    clear(root);
    const loc = this.selected;
    if (!loc?.url_v4 && !loc?.url_v6) return;
    const select = h("select", { class: "picker-select", "aria-label": "Address family" });
    const current = this.familyFor(loc) ?? "auto";
    const choices: [Settings["family"], string, string][] = [
      ["auto", "Auto", ""],
      ["ipv4", "IPv4 only", loc.url_v4],
      ["ipv6", "IPv6 only", loc.url_v6],
    ];
    for (const [value, label, url] of choices) {
      if (value === "auto" || url) select.append(h("option", { value, selected: value === current }, label));
    }
    select.addEventListener("change", () => {
      this.settings.family = select.value as Settings["family"];
      this.saveSettings();
      void this.refreshFamily();
    });
    root.append(h("label", { class: "picker family-picker", title: "Address family" }, h("span", { class: "picker-glyph", "aria-hidden": "true" }, "IP"), select));
    this.fitHeader();
  }

  /**
   * One row for the header when everything fits once shrunk, else a second row for the
   * controls. How wide they are depends on the server names, so it's measured, not guessed.
   */
  private fitHeader(): void {
    const bar = document.querySelector<HTMLElement>(".topbar");
    if (!bar) return;
    bar.classList.remove("wrapped");
    bar.classList.toggle("wrapped", bar.scrollWidth > bar.clientWidth + 1);
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
      select.append(h("option", { value: loc.id, selected: loc.id === this.selected?.id }, label));
    }
    select.addEventListener("change", () => void this.select(this.locations.find((l) => l.id === select.value) ?? null));
    root.append(h("label", { class: "picker" }, h("span", { class: "picker-status" }), select));
    this.fitHeader();
  }
}
