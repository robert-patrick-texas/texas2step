// How long Monitor and Diag may run: the site's limit on each page (page_monitor_max_runtime,
// page_diag_max_runtime, written on <html data-monitor-max>), and a server's on any one
// session (session_max_runtime). Either way the page stops as it does for another page,
// keeps what it shows, and starts again only once it's reloaded.
import { h } from "./dom";

/** The site's limit on how long `page` runs at a time, in milliseconds; 0 for none. */
export function maxRuntime(page: "monitor" | "diag"): number {
  const seconds = Number(document.documentElement.dataset[`${page}Max`]);
  return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : 0;
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
  return h("button", { class: "btn small", type: "button", onclick: () => window.location.reload() }, "Reload page");
}
