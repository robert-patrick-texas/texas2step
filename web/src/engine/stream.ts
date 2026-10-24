// Monitor's call profiles: a call's packets sent for as long as Monitor runs, and every
// 10 seconds scored as the Test page scores its packet-loss test (quality.ts).
import type { CallQuality } from "../types";
import type { Stamp } from "./loss";
import { callQuality } from "./quality";
import { percentile } from "./stats";

export type ProfileName = "path" | "voice" | "video";

export interface Profile {
  name: ProfileName;
  size: number; // bytes in each path probe, or each voice packet
  intervalMs: number; // between path probes, or between voice packets
  videoKbps: number; // the video stream's rate; the video profile only
}

export const VIDEO_BYTES = 1100; // a video packet: under what a browser sends as one UDP packet
export const VIDEO_FPS = 30;
export const SLICE_MS = 10_000;
const BUFFER_MS = 60; // a call's jitter buffer, as quality.ts has it
const SETTLE_MS = 1500; // a slice is scored once its last packet has had time to come back
const KEEP_MS = 15 * 60_000; // Monitor's longest window

/** Video packets per frame for a rate: a frame is sent as a burst of them. */
export function framePackets(kbps: number): number {
  return Math.max(1, Math.round((kbps * 1000) / (8 * VIDEO_BYTES * VIDEO_FPS)));
}

interface Packet {
  seq: number;
  frame: number; // the video frame it belongs to; -1 for voice
  wall: number; // Date.now() when sent
  sent: number; // performance.now() when sent
  rtt: number | null;
  server: number | null; // the server's stamp: its clock on arrival
  back: number | null; // performance.now() when the echo came back
  lostUp: boolean; // lost on the way to the server, by the server's counts
}

/** Video frames hit by a lost or late packet, of those sent. */
export interface FrameCount {
  total: number;
  hit: number;
}

export interface Frames extends FrameCount {
  up?: FrameCount; // what the other end sees
  down?: FrameCount; // what you see
}

/** One 10-second slice of a call: its quality, or `dead` when no packet came back. */
export interface Slice {
  start: number; // Date.now() at its start, or at the first packet's in a first slice
  end: number;
  call: CallQuality | null;
  dead: boolean;
  lost: number;
  sent: number;
  frames: Frames | null;
}

export class CallStream {
  private packets = new Map<number, Packet>(); // by sequence number
  private bySlice = new Map<number, Packet[]>(); // the slices not yet scored
  private last: { seq: number; count: number } | null = null; // the latest stamped echo
  private scored = new Map<number, Slice>();

  constructor(readonly voiceIntervalMs: number) {}

  /** A packet sent: voice (`frame` -1) or one of a video frame's. */
  sent(seq: number, frame: number, sent: number, wall: number): void {
    // The server counts from its session's first probe, which is this stream's first.
    this.last ??= { seq: seq - 1, count: 0 };
    const p: Packet = { seq, frame, wall, sent, rtt: null, server: null, back: null, lostUp: false };
    this.packets.set(seq, p);
    const k = Math.floor(wall / SLICE_MS);
    const list = this.bySlice.get(k);
    if (list) list.push(p);
    else this.bySlice.set(k, [p]);
  }

  /**
   * An echo. With the server's stamps, the gap since the last stamped echo shows how many
   * of the packets in between reached the server: the rest were lost on the way there, and
   * taken to be the first, as quality.ts's oneWay takes them.
   */
  echo(seq: number, rtt: number, now: number, stamp: Stamp | null): void {
    const p = this.packets.get(seq);
    if (!p || p.rtt !== null) return;
    p.rtt = rtt;
    p.back = now;
    if (!stamp) return;
    p.server = stamp.at;
    const last = this.last!;
    if (seq <= last.seq) return; // came back out of order: the gap was placed already
    const missing = seq - last.seq - 1;
    const reached = Math.min(Math.max(stamp.count - last.count - 1, 0), missing);
    for (let s = last.seq + 1; s < last.seq + 1 + missing - reached; s++) {
      const gone = this.packets.get(s);
      if (gone) gone.lostUp = true;
    }
    this.last = { seq, count: Math.max(last.count, stamp.count) };
  }

  /** Every slice scored in Monitor's longest window, oldest first, scoring those whose
   * packets have settled; their packets are then let go. */
  slices(wall = Date.now()): Slice[] {
    for (const [k, list] of this.bySlice) {
      if ((k + 1) * SLICE_MS + SETTLE_MS > wall) continue;
      this.scored.set(k, this.score(k, list));
      this.bySlice.delete(k);
      // Kept until here, for echoes and the server's counts placing losses in them. A packet
      // still missing with no stamped echo after it, as in an outage, counts as lost coming back.
      for (const p of list) this.packets.delete(p.seq);
    }
    for (const k of this.scored.keys()) if ((k + 1) * SLICE_MS < wall - KEEP_MS) this.scored.delete(k);
    return [...this.scored.values()].sort((a, b) => a.start - b.start);
  }

  private score(k: number, packets: Packet[]): Slice {
    const voice = packets.filter((p) => p.frame < 0);
    const video = packets.filter((p) => p.frame >= 0);
    const got = voice.filter((p) => p.rtt !== null);
    const lost = voice.length - got.length;
    const first = Math.min(...packets.map((p) => p.wall));
    const slice = { start: Math.max(k * SLICE_MS, Math.floor(first)), end: (k + 1) * SLICE_MS, lost, sent: voice.length, frames: frames(video) };
    if (!got.length) return { ...slice, call: null, dead: voice.length > 0 };
    const stamped = got.some((p) => p.server !== null);
    const ways = stamped ? delays(voice) : null;
    const call = callQuality({
      packets: voice.map((p) => p.rtt),
      interval_ms: this.voiceIntervalMs,
      rtt: { median: percentile(got.map((p) => p.rtt!), 50) },
      ...(ways ?? {}),
    });
    return { ...slice, call, dead: false };
  }
}

/** Each packet's delay above the fastest, each way, from the server's stamps (null when
 * it didn't come back), and which were lost on the way there, by position. */
function delays(packets: Packet[]): { up_ms: (number | null)[]; down_ms: (number | null)[]; lost_upstream: number[] } {
  const back = packets.filter((p) => p.server !== null && p.rtt !== null);
  const upMin = Math.min(...back.map((p) => p.server! - p.sent));
  const downMin = Math.min(...back.map((p) => p.back! - p.server!));
  return {
    up_ms: packets.map((p) => (p.server !== null && p.rtt !== null ? p.server - p.sent - upMin : null)),
    down_ms: packets.map((p) => (p.server !== null && p.rtt !== null ? p.back! - p.server - downMin : null)),
    lost_upstream: packets.flatMap((p, i) => (p.rtt === null && p.lostUp ? [i] : [])),
  };
}

/** Video frames hit by a lost or late packet: each way with the server's stamps, the worse
 * way's at the top; on the round trip without them. Null without video. */
function frames(video: Packet[]): Frames | null {
  if (!video.length) return null;
  const byFrame = new Map<number, Packet[]>();
  for (const p of video) {
    const list = byFrame.get(p.frame);
    if (list) list.push(p);
    else byFrame.set(p.frame, [p]);
  }
  const all = [...byFrame.values()];
  const stamped = video.some((p) => p.server !== null);
  if (!stamped) {
    const rtts = video.filter((p) => p.rtt !== null).map((p) => p.rtt!);
    const fastest = rtts.length ? Math.min(...rtts) : 0;
    const hit = all.filter((f) => f.some((p) => p.rtt === null || p.rtt - fastest > BUFFER_MS)).length;
    return { total: all.length, hit };
  }
  const ways = delays(video);
  const at = new Map(video.map((p, i) => [p, i]));
  const late = (list: (number | null)[], p: Packet) => (list[at.get(p)!] ?? 0) > BUFFER_MS;
  // Upstream, every frame; downstream, the frames with a packet that reached the server.
  const up = { total: all.length, hit: all.filter((f) => f.some((p) => (p.rtt === null && p.lostUp) || late(ways.up_ms, p))).length };
  const reached = all.map((f) => f.filter((p) => !(p.rtt === null && p.lostUp))).filter((f) => f.length);
  const down = { total: reached.length, hit: reached.filter((f) => f.some((p) => p.rtt === null || late(ways.down_ms, p))).length };
  const share = (c: FrameCount) => (c.total ? c.hit / c.total : 0);
  const worse = share(up) > share(down) ? up : down;
  return { ...worse, up, down };
}
