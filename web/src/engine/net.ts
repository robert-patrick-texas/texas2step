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

  url(path: string, params: Record<string, string | number> = {}): string {
    const url = new URL(this.base + API + path);
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
    return url.href;
  }

  wsUrl(path: string): string {
    return this.url(path).replace(/^http/, "ws");
  }

  async json<T>(path: string, init: RequestInit = {}): Promise<T> {
    const res = await fetch(this.url(path), { cache: "no-store", ...init });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = ((await res.json()) as { detail?: string }).detail ?? detail;
      } catch {
        /* not JSON */
      }
      throw new Error(`${res.status} ${detail}`);
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
