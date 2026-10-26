// How long Monitor and Diag may run: the site's limit on each page (page_monitor_max_runtime,
// page_diag_max_runtime, written on <html data-monitor-max>), and a server's on any one
// session (session_max_runtime). Either way the page stops as it does for another page,
// keeps what it shows, and starts again only once it's reloaded. Where the site limits a
// page, a small Runtime box on it counts the time against that limit.
import { h, setText } from "./dom";

/** The site's limit on how long `page` runs at a time, in milliseconds; 0 for none. */
export function maxRuntime(page: "monitor" | "diag"): number {
  const seconds = Number(document.documentElement.dataset[`${page}Max`]);
  return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : 0;
}

/** A small box with the time counted against the page's limit: "312s of 900s". */
export class RuntimeCounter {
  readonly el: HTMLElement;
  private readonly ran = h("span", { class: "runtime-ran" }, "0s");

  constructor(
    private readonly limit: number,
    hint: string,
  ) {
    const box = h("span", { class: "runtime", role: "timer" }, this.ran, ` of ${limit / 1000}s`);
    this.el = h("div", { class: "opt" }, h("span", { class: "opt-label", title: hint }, "Runtime"), box);
    box.title = hint;
  }

  /** Show `ms` of running, up to the limit. */
  set(ms: number): void {
    setText(this.ran, `${Math.floor(Math.min(Math.max(ms, 0), this.limit) / 1000)}s`);
  }
}

/** The page's runtime counter, where the site limits it. */
export function runtimeCounter(page: "monitor" | "diag", hint: (limit: string) => string): RuntimeCounter | null {
  const limit = maxRuntime(page);
  return limit ? new RuntimeCounter(limit, hint(span(limit / 1000))) : null;
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
