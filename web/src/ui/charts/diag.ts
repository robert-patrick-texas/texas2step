// The Diag page's pictures: a lane for each connection method, where every attempt runs
// as a ping from this browser (left) to the server (right) and a pong back; and a
// five-minute timeline of answers with the round trip drawn over it.
import { cssVar, h, onResize, reducedMotion } from "../dom";
import { percentile } from "../../engine/stats";

/** A method's state: answering, answered before but not now, never answered yet, not offered. */
export type Tone = "up" | "loss" | "trying" | "off";

export interface Sample {
  t: number; // ms since the epoch
  ok: boolean;
  rtt: number | null;
  up: boolean; // the method had answered before this sample
}

const TRAVEL = 900; // ms each way on screen; real round trips are far too quick to see
const FADE = 1200; // ms a lost ping's mark stays
const H = 24;

const lanes = new Set<Lane>();
let raf = 0;

/** One animation clock for every lane; it sleeps while nothing moves. */
function wake(): void {
  raf ||= requestAnimationFrame(function frame(now) {
    raf = 0;
    let busy = false;
    for (const lane of lanes) busy = lane.draw(now) || busy;
    if (busy) raf = requestAnimationFrame(frame);
  });
}

interface Shot {
  t0: number;
  result: "ok" | "lost" | null;
  settled: number;
}

export class Lane {
  readonly el: HTMLCanvasElement;
  private shots: Shot[] = [];
  private tone: Tone = "trying";
  private width = 0;

  constructor() {
    this.el = h("canvas", { class: "lane", "aria-hidden": "true" });
    onResize(this.el, (w) => {
      this.width = w;
      this.draw(performance.now());
    });
    lanes.add(this);
  }

  dispose(): void {
    lanes.delete(this);
  }

  setTone(tone: Tone): void {
    this.tone = tone;
    wake();
  }

  /** A ping leaves now; call the result with whether it came back. */
  send(): (ok: boolean) => void {
    const shot: Shot = { t0: performance.now(), result: null, settled: 0 };
    this.shots.push(shot);
    if (this.shots.length > 6) this.shots.shift();
    wake();
    return (ok) => {
      shot.result = ok ? "ok" : "lost";
      shot.settled = performance.now();
      wake();
    };
  }

  /** Draw at `now`; true while something is still moving. */
  draw(now: number): boolean {
    const W = this.width;
    if (!W || !this.el.isConnected) return false;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    this.el.style.height = `${H}px`;
    this.el.width = Math.round(W * dpr);
    this.el.height = Math.round(H * dpr);
    const ctx = this.el.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const x0 = 7;
    const x1 = W - 7;
    const y = H / 2;
    const good = cssVar("--good");
    const bad = cssVar("--critical");
    const grey = cssVar("--axis");
    const ink = cssVar("--ink-2");

    // The path itself, in the method's colour: dashed until it has answered.
    ctx.lineWidth = 2;
    ctx.strokeStyle = this.tone === "up" ? good : this.tone === "loss" ? bad : grey;
    ctx.globalAlpha = this.tone === "off" ? 0.45 : this.tone === "up" ? 0.55 : 0.8;
    ctx.setLineDash(this.tone === "up" || this.tone === "loss" ? [] : [3, 4]);
    ctx.beginPath();
    ctx.moveTo(x0, y);
    ctx.lineTo(x1, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    for (const x of [x0, x1]) {
      ctx.fillStyle = this.tone === "off" ? grey : ink;
      ctx.beginPath();
      ctx.arc(x, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
    }
    if (this.tone === "off") return false;

    const dot = (x: number, color: string, alpha = 1) => {
      ctx.globalAlpha = alpha;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, y, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
    };
    const lostColor = this.tone === "loss" ? bad : grey;
    let busy = false;
    for (const shot of this.shots) {
      const out = now - shot.t0;
      if (reducedMotion()) {
        // No motion: a mark at the far end for the latest answer only.
        if (shot === this.shots[this.shots.length - 1] && shot.result) dot(x1 - 10, shot.result === "ok" ? good : lostColor);
        continue;
      }
      if (out < TRAVEL) {
        dot(x0 + (x1 - x0) * (out / TRAVEL), ink);
        busy = true;
      } else if (shot.result === null) {
        dot(x1 - 6, ink, 0.4 + 0.4 * Math.abs(Math.sin(out / 160))); // waiting at the server
        busy = true;
      } else {
        const since = now - Math.max(shot.t0 + TRAVEL, shot.settled);
        if (shot.result === "ok" && since < TRAVEL) {
          dot(x1 - (x1 - x0) * (since / TRAVEL), good);
          busy = true;
        } else if (shot.result === "lost" && since < FADE) {
          const alpha = 1 - since / FADE;
          ctx.globalAlpha = alpha;
          ctx.strokeStyle = lostColor;
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.moveTo(x1 - 14, y - 4);
          ctx.lineTo(x1 - 6, y + 4);
          ctx.moveTo(x1 - 6, y - 4);
          ctx.lineTo(x1 - 14, y + 4);
          ctx.stroke();
          ctx.globalAlpha = 1;
          busy = true;
        }
      }
    }
    this.shots = this.shots.filter((s) => s.result === null || now - Math.max(s.t0 + TRAVEL, s.settled) < Math.max(TRAVEL, FADE));
    return busy;
  }
}

/** Five minutes at one sample a second: answers along the bottom, round trips above. */
export function drawTimeline(canvas: HTMLCanvasElement, samples: readonly Sample[], width: number, windowS = 300): void {
  if (!width) return;
  const TH = 30;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  canvas.style.height = `${TH}px`;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(TH * dpr);
  const ctx = canvas.getContext("2d")!;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const now = Date.now();
  const x = (t: number) => width - ((now - t) / (windowS * 1000)) * width;
  const bw = Math.max(1, width / windowS - 0.5);
  const good = cssVar("--good");
  const bad = cssVar("--critical");
  const grey = cssVar("--axis");

  ctx.fillStyle = cssVar("--grid");
  ctx.fillRect(0, TH - 6, width, 6);
  for (const s of samples) {
    ctx.fillStyle = s.ok ? good : s.up ? bad : grey;
    ctx.fillRect(x(s.t) - bw, TH - 6, bw, 6);
  }

  const rtts = samples.filter((s) => s.ok && s.rtt !== null).map((s) => s.rtt!);
  if (!rtts.length) return;
  const top = Math.max(1, percentile(rtts, 95) * 1.5);
  const y = (v: number) => TH - 9 - (Math.min(v, top) / top) * (TH - 12);
  ctx.strokeStyle = cssVar("--ink-2");
  ctx.globalAlpha = 0.75;
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  let pen = false;
  let last = 0;
  for (const s of samples) {
    if (!s.ok || s.rtt === null || s.t - last > 1600) pen = false;
    if (s.ok && s.rtt !== null) {
      if (pen) ctx.lineTo(x(s.t), y(s.rtt));
      else ctx.moveTo(x(s.t), y(s.rtt));
      pen = true;
    }
    last = s.t;
  }
  ctx.stroke();
  ctx.globalAlpha = 1;
}
