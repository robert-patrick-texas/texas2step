// Endpoint addressing, JSON helpers and Resource Timing extraction.
import type { Location, ServerInfo, WhoAmI } from "../types";

export const API = "/api/v1";

/**
 * Where this app is served: "" at the site root, or a path such as "/texas2step" behind
 * a reverse proxy. The server writes it into <base href> on every page load.
 */
export const BASE_PATH = new URL(document.baseURI).pathname.replace(/\/+$/, "");

/** The URL path for an in-app route such as "/monitor". */
export function appPath(route: string): string {
  return BASE_PATH + route;
}

/** The in-app route for a URL path; the inverse of appPath. */
export function routeOf(pathname: string): string {
  return pathname.startsWith(BASE_PATH) ? pathname.slice(BASE_PATH.length) || "/" : pathname;
}

/**
 * The access pass (docs/access.md), with every request. To this app's own server it goes
 * in an Authorization header, and a WebSocket's in its subprotocol, which keeps it out of
 * the URLs that proxies log. To another server it goes as ?pass=: a header there would
 * make the browser send a preflight before each timed ping and download, and skew the
 * timings, and a server on an earlier version wouldn't answer the subprotocol.
 */
let accessPass = "";
const WS_PROTOCOL = "texas2step";
const WS_PASS = `${WS_PROTOCOL}.pass.`;
let denied: (() => void) | null = null;
let refused: ((ep: Endpoint) => Promise<boolean>) | null = null;
/**
 * The servers that refused a request for want of a pass, by base URL, since the pass last
 * changed. A visitor inside one address family can be outside the other: the page's own
 * server then asks no code at its own address, while its IPv4-only name refuses.
 */
const refusals = new Set<string>();

export function setPass(pass: string): void {
  accessPass = pass;
  refusals.clear();
}

/** Called when this app's own server stops taking the pass (it expired). */
export function whenDenied(fn: () => void): void {
  denied = fn;
}

/**
 * Called when any other address the page tests refuses it for want of a pass (401): this
 * server's single-family names, or another server's. Resolves true once a new pass is in.
 */
export function whenRefused(fn: (ep: Endpoint) => Promise<boolean>): void {
  refused = fn;
}

/** A server refused a request for want of a pass: the access code (docs/access.md). */
export class NeedsCode extends Error {}

/** An address family the visitor can hold a server to, through its single-family hostname. */
export type Family = "ipv4" | "ipv6";

export class Endpoint {
  readonly base: string;

  /**
   * `base` is a server's base URL such as https://speed.example/texas2step, or "" for this
   * app's own server. `family` is set when `base` is that server's IPv4-only or IPv6-only
   * hostname, so the UDP probe can be held to the same family.
   */
  constructor(
    base: string,
    readonly family: Family | null = null,
  ) {
    this.base = (base || window.location.origin + BASE_PATH).replace(/\/+$/, "");
  }

  /** This app's own server, which takes the pass in a header. */
  get own(): boolean {
    return this.base === `${window.location.origin}${BASE_PATH}`.replace(/\/+$/, "");
  }

  url(path: string, params: Record<string, string | number> = {}): string {
    const url = new URL(this.base + API + path);
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
    if (accessPass && !this.own) url.searchParams.set("pass", accessPass);
    return url.href;
  }

  wsUrl(path: string): string {
    return this.url(path).replace(/^http/, "ws");
  }

  /** The subprotocols a WebSocket to this server offers: the pass, to this app's own. */
  wsProtocols(): string[] {
    return accessPass && this.own ? [WS_PROTOCOL, WS_PASS + accessPass] : [];
  }

  /** The headers that carry the pass, to this app's own server. */
  headers(): Record<string, string> {
    return accessPass && this.own ? { Authorization: `Bearer ${accessPass}` } : {};
  }

  /** `init` for a request to this server, with the pass where it goes in a header. */
  init(init: RequestInit = {}): RequestInit {
    const extra = this.headers();
    if (!Object.keys(extra).length) return init;
    const headers = new Headers(init.headers);
    for (const [k, v] of Object.entries(extra)) headers.set(k, v);
    return { ...init, headers };
  }

  /** Whether this server has refused a request for want of a pass, since the pass changed. */
  get needsCode(): boolean {
    return refusals.has(this.base);
  }

  /**
   * This server answered 401. This app's own starts over at the code screen; any other
   * address gets the code asked for over the page. True once a new pass is in, to try again.
   */
  async refused(): Promise<boolean> {
    if (this.own) {
      denied?.();
      return false;
    }
    refusals.add(this.base);
    return (await refused?.(this)) ?? false;
  }

  async json<T>(path: string, init: RequestInit = {}): Promise<T> {
    const res = await fetch(this.url(path), this.init({ cache: "no-store", ...init }));
    // Once more with the pass just bought; a second refusal comes with a pass held, which
    // isn't asked for again.
    if (res.status === 401 && (await this.refused())) return this.json<T>(path, init);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = ((await res.json()) as { detail?: string }).detail ?? detail;
      } catch {
        /* not JSON */
      }
      throw new (res.status === 401 ? NeedsCode : Error)(`${res.status} ${detail}`);
    }
    return (await res.json()) as T;
  }

  post<T>(path: string, body: unknown): Promise<T> {
    return this.json<T>(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  info(): Promise<ServerInfo> {
    return this.json<ServerInfo>("/info");
  }

  whoami(): Promise<WhoAmI> {
    return this.json<WhoAmI>("/whoami");
  }

  async locations(): Promise<Location[]> {
    return (await this.json<{ locations: Location[] }>("/locations")).locations;
  }
}

export const REFUSED = "Testing temporarily disabled.";

/** A server's traffic limit refused the test: the site's message, and its link if any. */
export class Refused extends Error {
  readonly url: string | null;

  constructor(message: string, url: string | null = null) {
    super(message);
    this.url = url;
  }
}

/** The refusal a traffic limit's 503 body describes. */
export function refusal(body: string): Refused {
  try {
    const answer = JSON.parse(body) as { message?: string; url?: string | null };
    return new Refused(answer.message ?? REFUSED, answer.url ?? null);
  } catch {
    return new Refused(REFUSED);
  }
}

export function nonce(): string {
  return Math.random().toString(36).slice(2, 10);
}

export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

performance.setResourceTimingBufferSize?.(4000);

/** Resource Timing entries land asynchronously after a fetch settles; poll briefly. */
export async function timingFor(url: string): Promise<PerformanceResourceTiming | undefined> {
  for (let i = 0; i < 6; i++) {
    const entries = performance.getEntriesByName(url, "resource");
    const entry = entries[entries.length - 1] as PerformanceResourceTiming | undefined;
    if (entry) return entry;
    await sleep(i === 0 ? 0 : 5);
  }
  return undefined;
}

/** `dur` of a Server-Timing metric, from the timing entry or a raw header. */
export function serverTiming(source: PerformanceResourceTiming | string | null, metric = "app"): number {
  if (!source) return 0;
  if (typeof source !== "string") {
    return source.serverTiming?.find((t) => t.name === metric)?.duration ?? 0;
  }
  for (const part of source.split(",")) {
    const [name, ...params] = part.split(";").map((p) => p.trim());
    if (name !== metric) continue;
    for (const p of params) if (p.startsWith("dur=")) return Number(p.slice(4)) || 0;
  }
  return 0;
}

export function clearTimings(): void {
  performance.clearResourceTimings();
}

/** Abort when either signal fires (AbortSignal.any is too new to rely on). */
export function linked(parent: AbortSignal | undefined, timeoutMs?: number): AbortController {
  const ctrl = new AbortController();
  if (parent) {
    if (parent.aborted) ctrl.abort(parent.reason);
    else parent.addEventListener("abort", () => ctrl.abort(parent.reason), { once: true });
  }
  if (timeoutMs) {
    const t = setTimeout(() => ctrl.abort(new DOMException("timeout", "TimeoutError")), timeoutMs);
    ctrl.signal.addEventListener("abort", () => clearTimeout(t), { once: true });
  }
  return ctrl;
}
