// How long Monitor and Diag may run: each page's limit (page_monitor_max_runtime,
// page_diag_max_runtime), set by the site that served the page, on <html data-monitor-max>,
// and by each server the page tests, in its /api/v1/info, the strictest of them; and a
// server's on any one session (session_max_runtime). Either way the page stops as it does
// for another page, keeps what it shows, and starts again only once it's reloaded. Where
// a page is limited, a small Runtime box on it counts the time against the limit.
import type { ServerInfo } from "../types";
import { h, setText } from "./dom";

type Page = "monitor" | "diag";

/** The site's limit on how long `page` runs at a time, in milliseconds; 0 for none. */
export function maxRuntime(page: Page): number {
  const seconds = Number(document.documentElement.dataset[`${page}Max`]);
  return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : 0;
}

/** A limit on how long a page runs, in milliseconds (0 for none), and whose: this site's
 * (`who` null), or a tested server's, by its name. */
export interface Limit {
  ms: number;
  who: string | null;
}

/**
 * The strictest limit on `page`: the site's own, and each server's it tests, as its
 * /api/v1/info says (servers from before 2.5 say none). So a page from a site without
 * limits, testing a server that has them, keeps that server's, as its own pages would.
 */
export function strictest(page: Page, servers: (ServerInfo | null | undefined)[]): Limit {
  let limit: Limit = { ms: maxRuntime(page), who: null };
  for (const info of servers) {
    const seconds = info?.limits[page === "monitor" ? "max_monitor_seconds" : "max_diag_seconds"];
    const ms = typeof seconds === "number" && seconds > 0 ? seconds * 1000 : 0;
    if (info && ms && (!limit.ms || ms < limit.ms)) limit = { ms, who: info.server.name };
  }
  return limit;
}

/** Whose limit, for a sentence: "this site's", "Dallas's". */
export function whose(limit: Limit): string {
  return limit.who ? `${limit.who}'s` : "this site's";
}

/** A small box with the time counted against the page's limit: "312s of 900s". Hidden
 * while there is none; the limit may change as the servers tested do. */
export class RuntimeCounter {
  readonly el: HTMLElement;
  private readonly ran = h("span", { class: "runtime-ran" }, "0s");
  private readonly of = h("span", {});
  private readonly label = h("span", { class: "opt-label" }, "Runtime");
  private readonly box = h("span", { class: "runtime", role: "timer" }, this.ran, this.of);
  private limit: Limit = { ms: 0, who: null };

  constructor(private readonly hint: (limit: string, whose: string) => string) {
    this.el = h("div", { class: "opt", hidden: true }, this.label, this.box);
  }

  /** The limit to count against; none hides the box. */
  setLimit(limit: Limit): void {
    if (limit.ms === this.limit.ms && limit.who === this.limit.who) return;
    this.limit = limit;
    this.el.hidden = !limit.ms;
    setText(this.of, `of ${limit.ms / 1000}s`);
    this.label.title = this.box.title = this.hint(span(limit.ms / 1000), whose(limit));
  }

  /** Show `ms` of running, up to the limit. */
  set(ms: number): void {
    setText(this.ran, `${Math.floor(Math.min(Math.max(ms, 0), this.limit.ms) / 1000)}s`);
  }
}

/** A limit as the page says it: "90s", "15 min", "1 h", "1 h 30 min". Units never wrap. */
export function span(seconds: number): string {
  if (seconds < 120) return `${Math.round(seconds)}s`;
  const min = Math.round(seconds / 60);
  if (min < 60) return `${min}\u00a0min`;
  const hours = `${Math.floor(min / 60)}\u00a0h`;
  return min % 60 ? `${hours} ${min % 60}\u00a0min` : hours;
}

/** Why a server ended a session, from its session_max_runtime where it gave it. */
export function sessionEnded(name: string, seconds: unknown): string {
  return typeof seconds === "number" && seconds > 0 ? `${name} ends each session after ${span(seconds)}.` : `${name} ended the session at its time limit.`;
}

/** The way on, once a limit is reached. */
export function reloadButton(): HTMLButtonElement {
  return h("button", { class: "btn small urgent", type: "button", onclick: () => window.location.reload() }, "Reload page");
}
