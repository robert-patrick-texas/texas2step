// Application shell: routing, theme, server selection and shared settings.
import { appPath, Endpoint, type Family, routeOf, setPage, whenDenied, whenRefused } from "../engine/net";
import type { AccessStatus, Location, ServerInfo, WhoAmI } from "../types";
import { Awake } from "./awake";
import { $, clear, h, save, setText, store } from "./dom";
import { admit, askOver, forgetPass, passHeld, sentence } from "./gate";
import { mountSizePicker } from "./size";
import { mountThemeMenu } from "./theme";

export interface Settings {
  streams: number;
  seconds: number;
  packets: number;
  intervalMs: number;
  oneStream: boolean | null; // null: as the server tested says (defaults.one_stream)
  family: "auto" | "ipv4" | "ipv6";
  keepAwake: boolean; // keep the screen on while a page measures (awake.ts)
}

export interface View {
  mount(root: HTMLElement, params: Record<string, string>): void;
  /** Another page is shown: stop measuring, and keep what's on screen to come back to. */
  hide?(): void;
  /** Back from another page, at the address it was left at. */
  show?(): void;
  /** Built anew: let go of everything. */
  unmount?(): void;
}

/** A page built once and kept, hidden while another is shown. */
interface Page {
  name: string;
  view: View;
  el: HTMLElement;
  /** Where it was: its address (a saved result's, Monitor's choices) and how far down. */
  address: string;
  scroll: number;
}

interface Route {
  page: string;
  pattern: RegExp;
  make: () => View;
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
  /** What this server said about the access code; null where it has none (or didn't say). */
  access: AccessStatus | null = null;
  settings: Settings;
  readonly awake = new Awake(() => this.settings.keepAwake);
  /** The pages this site shows: all of them unless the server lists fewer. */
  readonly pages = new Set((document.documentElement.dataset.pages ?? "test monitor diag history help api").split(" "));
  /** Where an address with no page of its own lands: the first page shown, other than help. */
  readonly home = this.pages.has("test") ? "/" : `/${[...this.pages].find((p) => p !== "help") ?? "help"}`;
  private listeners = new Set<ServerListener>();
  private current: Page | null = null;
  private kept = new Map<string, Page>();
  private routes: Route[] = [];

  constructor() {
    this.settings = store<Settings>("t2s.settings", { streams: 4, seconds: 10, packets: 500, intervalMs: 10, oneStream: null, family: "auto", keepAwake: true });
  }

  /** A route to `page`; a page with several (Test's / and /r/<id>) is one page, kept once. */
  route(page: string, pattern: RegExp, make: () => View): this {
    this.routes.push({ page, pattern, make });
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
    whenRefused((ep) => this.refusedBy(ep));
    this.access = await admit(this.origin);
    this.desk = this.access?.desk ?? true;
    // The page leaves out its version until the code is in (it would help an attacker).
    const version = document.querySelector<HTMLMetaElement>('meta[name="texas2step-version"]');
    if (version && !version.content) {
      try {
        version.content = (await this.origin.json<ServerInfo>("/info")).version ?? "";
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
    clear($("#view")); // the access-code screen, where there was one
    this.render();
    await this.loadLocations();
  }

  /** Go to an in-app route such as "/monitor". */
  navigate(route: string, replace = false): void {
    // Another page's plain address (its tab) brings it back as it was left; a saved
    // result's address opens that result.
    const to = this.match(route);
    const kept = to && to.page !== this.current?.name && !Object.keys(to.params).length ? this.kept.get(to.page) : undefined;
    if (kept) route = kept.address;
    if (route === this.address()) return;
    if (replace) history.replaceState(null, "", appPath(route));
    else history.pushState(null, "", appPath(route));
    this.render();
  }

  /**
   * A page's own address, as what it shows changes: in the address bar while it's shown,
   * and kept for its tab while it's hidden.
   */
  setAddress(route: string): void {
    const page = this.kept.get(this.match(route)?.page ?? "");
    if (!page) return;
    page.address = route;
    if (page === this.current && route !== this.address()) history.replaceState(null, "", appPath(route));
  }

  /** The route and query shown in the address bar. */
  private address(): string {
    return routeOf(window.location.pathname) + window.location.search;
  }

  private match(route: string): (Route & { params: Record<string, string> }) | null {
    const path = route.split("?")[0]!;
    for (const r of this.routes) {
      const match = r.pattern.exec(path);
      if (match) return { ...r, params: match.groups ?? {} };
    }
    return null;
  }

  /**
   * Show the page for the address. Each page is built once and kept: going to another
   * hides it, and coming back to where it was left shows it again as it was. Any other
   * address of a page (another saved result, or a new test after one) builds it anew.
   */
  private render(): void {
    const path = routeOf(window.location.pathname);
    for (const a of document.querySelectorAll<HTMLAnchorElement>(".tabs a")) {
      const href = routeOf(new URL(a.href).pathname);
      const active = href === "/" ? path === "/" || path.startsWith("/r/") : path.startsWith(href);
      a.toggleAttribute("aria-current", active);
      if (active) a.setAttribute("aria-current", "page");
    }
    const to = this.match(path);
    if (!to) return this.navigate(this.home, true);
    setPage(to.page); // a hidden page stops sending, so what's sent is the shown page's
    const address = this.address();
    const from = this.current;
    let page = this.kept.get(to.page);
    if (page === from && page?.address === address) return;
    if (from && from !== page) {
      from.scroll = window.scrollY;
      from.view.hide?.();
      from.el.hidden = true;
    }
    if (page && page !== from && page.address === address) {
      this.current = page;
      page.el.hidden = false;
      page.view.show?.();
      window.scrollTo(0, page.scroll);
      return;
    }
    if (page) {
      page.view.unmount?.();
      page.el.remove();
    }
    page = { name: to.page, view: to.make(), el: h("div", { class: "page" }), address, scroll: 0 };
    this.kept.set(to.page, page);
    this.current = page;
    $("#view").append(page.el);
    page.view.mount(page.el, to.params);
    if (from && from !== page) window.scrollTo(0, 0); // not where the page left behind was
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

  // ---- The access code, asked by another address -----------------------------------

  /**
   * Another address the page tests refused it for want of a pass: this server's IPv4-only
   * or IPv6-only name, to a visitor inside on the other family only, or another server.
   * A code entered here buys a pass that every server with the same key takes. True once
   * it's in, to try again.
   */
  private async refusedBy(ep: Endpoint): Promise<boolean> {
    // No codes here, so no pass to buy here; or a pass that holds was refused, as from a
    // network blocked by hand, which another code wouldn't change. The pages say why.
    if (!this.takesCode || passHeld()) return false;
    // Outside here too: the pass ran out, so the page starts over at the code screen.
    if (this.access?.required) {
      forgetPass();
      window.location.reload();
      return false;
    }
    return askOver(this.origin, this.over(ep));
  }

  /** Whether the code can be entered here, for an address that asks for it. */
  get takesCode(): boolean {
    return this.access?.codes === true;
  }

  /** The code asked for over the page, when the visitor chooses: true once it's in. */
  async enterCode(ep: Endpoint): Promise<boolean> {
    const got = await askOver(this.origin, this.over(ep), true);
    if (got && !this.info) await this.refreshFamily(); // the header's server, refused until now
    return got;
  }

  /**
   * How an address that asks for the code is reached, to begin a sentence: "Over IPv4, "
   * for an IPv4-only name, of this server or another; nothing for one over both families.
   */
  private over(ep: Endpoint): string {
    return ep.family ? `Over ${ep.family === "ipv4" ? "IPv4" : "IPv6"}, ` : "";
  }

  /** What an address that refused the page says, for a page's status line. */
  needsCodeText(ep: Endpoint): string {
    return sentence(this.over(ep), "this function is restricted.");
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
    setText(document.querySelector("#footer-version"), this.info?.version ? `texas2step ${this.info.version}` : "texas2step");
    const picker = document.querySelector("#server-picker .picker-status");
    const code = !this.info && this.ep.needsCode; // up, but asks for the access code
    picker?.classList.toggle("down", !this.info && !code);
    picker?.classList.toggle("code", code);
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
