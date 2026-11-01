// Which way Monitor's lost probes went: each one the server never got was lost on the way
// there, and each it got on the way back. Monitor learns it from the server's record of
// the probes that arrived ({"op": "seen"}), asked about now and then while any wait.
import type { SeenAnswer } from "./quality";

/** Lost on the way to the server, on the way back, held back by the server at a traffic
 * limit (lost neither way), or not known: sent over an earlier session, over TCP, to a
 * server that keeps no record, or not sent at all, as to a server gone quiet. */
export type Way = "up" | "down" | "held" | "unknown";

/** Whether `seq` is in one of `runs`, [first, last] in order. */
export function inAnyRun(runs: readonly [number, number][] | undefined, seq: number): boolean {
  if (!runs) return false;
  let lo = 0;
  let hi = runs.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const [first, last] = runs[mid]!;
    if (seq < first) hi = mid - 1;
    else if (seq > last) lo = mid + 1;
    else return true;
  }
  return false;
}

interface Waiting {
  seq: number;
  session: number;
  at: number; // Date.now() when it was found lost
  done: (way: Way) => void;
}

/** One server's lost probes, waiting for the server's record to place them. */
export class LostWays {
  /** The UDP session probes go out on now: a new one starts over. */
  session = 0;
  private waiting: Waiting[] = [];
  private asking: Promise<void> | null = null;

  get pending(): number {
    return this.waiting.length;
  }

  /** A probe found lost, sent over `session` (null: not sent); `can`: the session's server
   * keeps a record. `done` hears which way it went, once known. */
  lost(seq: number, session: number | null, can: boolean, done: (way: Way) => void): void {
    if (session === null || session !== this.session || !can) return done("unknown");
    this.waiting.push({ seq, session, at: Date.now(), done });
  }

  /** A new session: the probes of the last can't be placed any more. */
  newSession(): void {
    this.session++;
    for (const w of this.waiting.splice(0)) w.done("unknown");
  }

  /** Asks the server about those waiting, from the oldest; while it's being asked already,
   * that question's answer. */
  ask(seen: (from: number) => Promise<SeenAnswer | null>): Promise<void> {
    if (this.asking) return this.asking;
    if (!this.waiting.length) return Promise.resolve();
    const session = this.session;
    const at = Date.now();
    this.asking = seen(this.waiting.reduce((low, w) => Math.min(low, w.seq), Infinity))
      .then((answer) => {
        if (answer && session === this.session) this.take(answer, at);
      })
      .catch(() => {})
      .finally(() => {
        this.asking = null;
      });
    return this.asking;
  }

  /** The server's answer to a question asked at `at`. A probe after the last that arrived
   * never did, if it was already lost when asked: it went on the way there. */
  take(a: SeenAnswer, at: number): void {
    this.waiting = this.waiting.filter((w) => {
      let way: Way | null = null;
      if (w.session !== this.session || w.seq < a.from) way = "unknown";
      else if (w.seq <= a.to) way = inAnyRun(a.held, w.seq) ? "held" : inAnyRun(a.missing, w.seq) ? "up" : "down";
      else if (!a.more && w.at <= at) way = "up";
      if (way) w.done(way);
      return way === null;
    });
  }
}
