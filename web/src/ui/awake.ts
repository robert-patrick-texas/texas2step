// Keeping the screen on while a page measures (the Screen Wake Lock API). A phone whose
// screen goes off suspends the page, and a locked computer slows its timers, so either
// would put gaps in the figures. Browsers let go of the lock whenever the page is out of
// view, so it is asked for again when the page is back.
import type { App } from "./app";
import { h } from "./dom";

/** Whether this browser can keep the screen on: only over HTTPS (or from localhost). */
export const canKeepAwake = "wakeLock" in navigator;

const HINT = "Keeps the screen from dimming or turning off while Monitor or Diag is open and while a test runs, so the browser keeps measuring. Switching to another app or tab lets it sleep as usual.";

export class Awake {
  private holders = new Set<string>();
  private lock: WakeLockSentinel | null = null;
  private asking = false;

  constructor(private readonly enabled: () => boolean) {
    document.addEventListener("visibilitychange", () => this.apply());
  }

  /** `who` (a page) wants the screen kept on, or no longer does. */
  hold(who: string, on = true): void {
    if (on) this.holders.add(who);
    else this.holders.delete(who);
    this.apply();
  }

  /** Ask for the lock, or let it go, as the pages, the setting and the page's visibility say. */
  apply(): void {
    const want = canKeepAwake && this.enabled() && this.holders.size > 0 && document.visibilityState === "visible";
    if (!want) {
      void this.lock?.release();
      this.lock = null;
      return;
    }
    if (this.lock || this.asking) return;
    this.asking = true;
    navigator.wakeLock.request("screen").then(
      (lock) => {
        this.asking = false;
        this.lock = lock;
        lock.addEventListener("release", () => {
          if (this.lock === lock) this.lock = null;
        });
        this.apply(); // let go again if it's no longer wanted
      },
      (err: unknown) => {
        // Refused, e.g. in a phone's battery saver: the screen sleeps as it would anyway.
        this.asking = false;
        console.info("the screen can't be kept on:", err);
      },
    );
  }
}

/**
 * The "Keep screen on" switch. It's one setting for every page; a page marks its switch
 * again when it's shown, as it may have changed on another. None where it can't be done.
 */
export function awakeSwitch(app: App): { el: HTMLElement; mark: () => void } | null {
  if (!canKeepAwake) return null;
  const values: [boolean, string][] = [
    [true, "On"],
    [false, "Off"],
  ];
  const group = h("div", { class: "seg", role: "radiogroup", "aria-label": "Keep screen on" });
  const mark = () => values.forEach(([v], i) => group.children[i]?.setAttribute("aria-checked", String(app.settings.keepAwake === v)));
  for (const [v, text] of values) {
    const b = h("button", { type: "button", role: "radio" }, text);
    b.addEventListener("click", () => {
      app.settings.keepAwake = v;
      app.saveSettings();
      app.awake.apply();
      mark();
    });
    group.append(b);
  }
  mark();
  return { el: h("div", { class: "opt" }, h("span", { class: "opt-label", title: HINT }, "Keep screen on"), group), mark };
}
