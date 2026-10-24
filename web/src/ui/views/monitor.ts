// Continuous probing: a live RTT trace with per-second loss, for watching a path over time.
// When the site lists two or more servers, it can probe two at once, at the same moments
// from this browser, and compare the two paths on one chart. A profile picks what is sent:
// small probes to follow the path, or a voice or video call's packets, scored every 10 s.
import { WsProber } from "../../engine/latency";
import { RtcProbe } from "../../engine/loss";
import { appPath, type Endpoint, REFUSED } from "../../engine/net";
import { jitter, percentile } from "../../engine/stats";
import { CallStream, framePackets, type ProfileName, type Slice, VIDEO_BYTES, VIDEO_FPS } from "../../engine/stream";
import type { CallRating, Location, ServerInfo } from "../../types";
import type { App, View } from "../app";
import { LiveChart, type LiveSeries } from "../charts/live";
import { clear, h, save, setText, store } from "../dom";
import { type Formatted, ms, pct } from "../format";

interface Probe {
  send(seq: number, size: number): void;
  close(): void;
}

/** One server being probed: its own session, probes in flight and totals. */
interface Dest {
  ep: Endpoint;
  info: ServerInfo | null;
  probe: Probe | null;
  transport: string;
  sentAt: Map<number, number>; // the probes on the chart: path probes or voice packets
  sent: number;
  latest: number | null;
  stream: CallStream | null; // with a call profile
}

/** What each profile sends: a desk can change these, a visitor just picks the profile. */
interface Choice {
  profile: ProfileName;
  rate: number; // path probes a second
  size: number; // bytes in a path probe
  voiceSize: number; // bytes in a voice packet
  every: number; // ms between voice packets
  video: number; // the video stream's kb/s
}

const DEFAULTS: Choice = { profile: "path", rate: 10, size: 64, voiceSize: 200, every: 20, video: 1000 };

interface Figures {
  latest: number | null;
  median: number | null;
  p95: number | null;
  jitter: number | null;
  loss: number | null; // percent of the window
  lost: number;
  window: number;
  sent: number;
  // With a call profile, over the window's 10-second slices:
  mos: number | null; // the median score
  rating: CallRating | null; // the median slice's rating
  worst: number | null; // the lowest score
  worstAt: number | null;
  below: number | null; // percent of slices rated below good
  belowCount: number;
  slices: number;
  frames: number | null; // percent of video frames hit, the worse direction's
}

const RATES = [5, 10, 20, 50];
const WINDOWS: [number, string][] = [
  [60, "1 min"],
  [300, "5 min"],
  [900, "15 min"],
];
// A keeps the single monitor's green; B takes blue.
const SIDES = [
  { key: "a", short: "A", token: "--idle" },
  { key: "b", short: "B", token: "--dl" },
] as const;
const PROFILES: [ProfileName, string][] = [
  ["path", "Path"],
  ["voice", "Voice call"],
  ["video", "Video call"],
];
const RATING_WORD: Record<CallRating, string> = { excellent: "Excellent", good: "Good", fair: "Fair", poor: "Poor", bad: "Bad" };

type Key = keyof Figures | "probes";
// The tiles, and the comparison's rows, of each profile.
const SHOWN: Record<ProfileName, [Key, string][]> = {
  path: [
    ["latest", "Latest"],
    ["median", "Median"],
    ["p95", "95th percentile"],
    ["jitter", "Jitter"],
    ["loss", "Loss (window)"],
    ["sent", "Probes"],
  ],
  voice: [
    ["mos", "Call quality"],
    ["worst", "Worst 10 s"],
    ["below", "Below good"],
    ["median", "Round trip"],
    ["jitter", "Jitter"],
    ["loss", "Loss (window)"],
  ],
  video: [
    ["mos", "Call quality"],
    ["worst", "Worst 10 s"],
    ["below", "Below good"],
    ["frames", "Frames hit"],
    ["median", "Round trip"],
    ["loss", "Loss (window)"],
  ],
};

export class MonitorView implements View {
  private chart = new LiveChart();
  private dests: Dest[] = [];
  private running = false;
  private timers: number[] = [];
  private seq = 0;
  private frame = 0;
  private choice: Choice;
  private log = false; // the chart's scale
  private chosen: boolean; // a profile from the address or this browser, not the server's
  private two: boolean;
  private pick: { a: string; b: string };
  private seriesKey = "";
  private invited = false; // the Servers toggle has had its one pulse
  private off: (() => void) | null = null;
  private startBtn!: HTMLButtonElement;
  private transport!: HTMLElement;
  private lede!: HTMLElement;
  private notice!: HTMLElement;
  private destBox!: HTMLElement;
  private profileBox!: HTMLElement; // the profile switch, beside Start
  private rateBox!: HTMLElement; // the path's probe rate
  private sizes!: HTMLDetailsElement; // the chart's scale and the exact sizes and rates, folded away
  private tiles!: HTMLElement;
  private compare!: HTMLElement;
  private figures: Partial<Record<Key, HTMLElement>> = {};
  private counts: Partial<Record<Key, HTMLElement>> = {};
  private cells: Partial<Record<Key, HTMLElement>>[] = [];
  private heads: HTMLElement[] = [];
  private chartHead!: HTMLElement;

  constructor(private readonly app: App) {
    // A bookmarked address (?a=dallas&b=houston&profile=voice) wins over the last choices here.
    const saved = store("t2s.monitor", { two: false, a: "", b: "", ...DEFAULTS, chosen: false });
    const q = new URLSearchParams(window.location.search);
    this.two = q.has("a") && q.has("b") ? true : saved.two;
    this.pick = { a: q.get("a") ?? saved.a, b: q.get("b") ?? saved.b };
    const num = (name: string, fallback: number) => {
      const v = Number(q.get(name));
      return q.has(name) && Number.isFinite(v) && v > 0 ? v : fallback;
    };
    const profile = q.get("profile");
    const named = PROFILES.some(([p]) => p === profile);
    this.chosen = named || saved.chosen;
    this.choice = {
      profile: named ? (profile as ProfileName) : saved.profile,
      rate: num("rate", saved.rate),
      size: num("size", saved.size),
      voiceSize: num("voice_size", saved.voiceSize),
      every: num("every", saved.every),
      video: num("video", saved.video),
    };
  }

  mount(root: HTMLElement): void {
    this.startBtn = h("button", { class: "btn primary big", type: "button" }, "Start monitoring");
    this.startBtn.addEventListener("click", () => void (this.running ? this.stop() : this.start()));
    this.transport = h("span", { class: "chip" }, "idle");
    this.lede = h("p", { class: "lede" });
    this.notice = h("p", { class: "monitor-notice", role: "status", hidden: true });
    this.destBox = h("div", { class: "dest-box" });
    this.profileBox = h("div", { class: "dest-box" });
    this.rateBox = h("div", { class: "dest-box" });
    this.sizes = h("details", { class: "options monitor-sizes" });
    this.tiles = h("div", { class: "tiles six" });
    this.compare = h("div", { class: "table-wrap compare-wrap" });
    this.chartHead = h("p", {});

    root.append(
      h("section", { class: "page-head" }, h("div", {}, h("h1", {}, "Monitor"), this.lede, this.notice), h("div", { class: "head-actions" }, this.profileBox, this.startBtn)),
      h("div", { class: "toolbar" }, this.destBox, this.rateBox, seg("Window", WINDOWS, 60, (v) => this.chart.setWindow(v)), this.sizes),
      this.tiles,
      h("section", { class: "card chart-card" }, h("header", { class: "card-head head-inline" }, h("h2", {}, "Round-trip time"), this.chartHead, this.transport), this.compare, this.chart.el),
    );
    // The server list and the server's own choice of profile arrive after the first render.
    this.off = this.app.onServer(() => {
      if (this.running) return;
      if (!this.chosen && this.app.info?.monitor) this.choice.profile = this.app.info.monitor.profile;
      if (this.choice.profile === "video" && !this.videoOffered) this.choice.profile = "voice";
      this.layout();
    });
    this.layout();
    this.timers.push(window.setInterval(() => this.refresh(), 500));
  }

  unmount(): void {
    this.stop();
    this.off?.();
    for (const t of this.timers) clearInterval(t);
  }

  // ---- Destinations and profile --------------------------------------------------

  /** Comparing needs a second server to compare with. */
  private get comparing(): boolean {
    return this.two && this.app.locations.length >= 2;
  }

  /** The Video call profile, unless the server withholds it (monitor_video). */
  private get videoOffered(): boolean {
    return this.app.info?.monitor?.video !== false;
  }

  /** Servers A and B: the saved or bookmarked ones if still listed, else sensible picks. */
  private sides(): [Location, Location] | null {
    const locs = this.app.locations;
    if (locs.length < 2) return null;
    const find = (id: string) => locs.find((l) => l.id === id);
    const a = find(this.pick.a) ?? this.app.selected ?? locs[0]!;
    const b = [find(this.pick.b), ...locs].find((l) => l && l.id !== a.id)!;
    return [a, b];
  }

  /** Render the destination and profile choices, tiles or comparison table, and chart series. */
  private layout(): void {
    const sides = this.sides();
    if (sides) this.pick = { a: sides[0].id, b: sides[1].id };
    const comparing = this.comparing && sides !== null;
    const c = this.choice;
    setText(this.lede, lede(c, comparing));
    setText(this.chartHead, c.profile === "path" ? "One dot per probe. Red ticks along the top mark probes that never came back." : "One dot per voice packet, red ticks for lost ones; the strip scores each 10 seconds.");
    this.tiles.hidden = comparing;
    this.compare.hidden = !comparing;

    clear(this.destBox);
    if (sides) {
      // Two servers at once is what sets this monitor apart, so its switch stands out: an
      // accent ring, A's and B's colours on "Two", and one pulse when the page opens.
      const toggle = seg("Servers", [[0, "One"], [1, "Two"]], comparing ? 1 : 0, (v) => {
        this.two = v === 1;
        this.layout();
      });
      toggle.classList.add("servers-opt");
      const group = toggle.querySelector(".seg")!;
      group.classList.add("seg-feature");
      if (!comparing && !this.invited) group.classList.add("invite");
      this.invited = true;
      group.lastElementChild!.append(h("span", { class: "seg-dots", "aria-hidden": "true" }, ...SIDES.map((side) => h("span", { class: "legend-key dot", style: `--key:var(${side.token})` }))));
      this.destBox.append(toggle);
      if (comparing) {
        for (const [i, side] of SIDES.entries()) {
          const select = h("select", { class: "dest-select", "aria-label": `Server ${side.short}` });
          for (const loc of this.app.locations) {
            select.append(h("option", { value: loc.id, selected: loc.id === sides[i]!.id }, loc.location ? `${loc.name} — ${loc.location}` : loc.name));
          }
          select.addEventListener("change", () => {
            const other = i === 0 ? "b" : "a";
            // Picking the other side's server swaps the two.
            if (select.value === this.pick[other]) this.pick[other] = this.pick[side.key];
            this.pick[side.key] = select.value;
            this.layout();
          });
          const key = h("span", { class: "legend-key dot", style: `--key:var(${side.token})` });
          this.destBox.append(h("label", { class: "opt" }, h("span", { class: "opt-label" }, key, ` Server ${side.short}`), select));
        }
      }
    }
    this.renderProfile();
    this.remember(comparing);

    const series: LiveSeries[] = comparing ? sides.map((loc, i) => ({ label: `${loc.name} (${SIDES[i]!.short})`, short: SIDES[i]!.short, token: SIDES[i]!.token })) : [{ label: "", short: "", token: "--idle" }];
    const seriesKey = JSON.stringify(series);
    if (seriesKey !== this.seriesKey) {
      this.seriesKey = seriesKey;
      this.chart.setSeries(series);
      this.dests = [];
    }
    if (comparing) this.renderCompare(sides);
    else this.renderTiles();
    this.lock(false);
  }

  /** The profile switch, for anyone, and the exact sizes and rates behind it, for the desk. */
  private renderProfile(): void {
    const c = this.choice;
    clear(this.profileBox);
    const offered = PROFILES.filter(([p]) => p !== "video" || this.videoOffered);
    // The one switch everyone needs, beside Start: its name is for screen readers.
    const pick = seg("Profile", offered, c.profile, (v) => {
      c.profile = v;
      this.chosen = true;
      this.layout();
    });
    pick.querySelector(".opt-label")?.remove();
    pick.title = "Path: small probes, to follow the path. Voice call: a voice call's packets, scored as a call every 10 seconds. Video call: a video call's, with its voice.";
    this.profileBox.append(pick);
    const live = (v: number, set: () => void) => {
      set();
      this.remember(this.comparing);
      if (this.running) this.schedule();
      return v;
    };
    // The path's rate stays at hand, as it always was; the rest is folded away.
    clear(this.rateBox);
    const grid = h("div", { class: "opt-grid" }, seg("Scale", [[0, "Linear"], [1, "Log"]], this.log ? 1 : 0, (v) => this.chart.setLog((this.log = v === 1))));
    if (c.profile === "path") {
      this.rateBox.append(seg("Probe rate", RATES.map((r) => [r, `${r}/s`]), c.rate, (v) => live(v, () => (c.rate = v))));
      grid.append(seg("Probe size", [[64, "64 B"], [200, "200 B"], [VIDEO_BYTES, "1,100 B"]], c.size, (v) => live(v, () => (c.size = v))));
    } else {
      grid.append(
        seg("Voice packet", [[100, "100 B"], [200, "200 B"], [300, "300 B"]], c.voiceSize, (v) => live(v, () => (c.voiceSize = v))),
        seg("Voice every", [[10, "10 ms"], [20, "20 ms"], [30, "30 ms"]], c.every, (v) => live(v, () => (c.every = v))),
      );
      if (c.profile === "video") grid.append(seg("Video", [[500, "0.5 Mb/s"], [1000, "1 Mb/s"], [2500, "2.5 Mb/s"]], c.video, (v) => live(v, () => (c.video = v))));
    }
    const open = this.sizes.open;
    this.sizes.replaceChildren(h("summary", {}, "Options"), grid);
    this.sizes.open = open;
  }

  /** Remember the choices, and keep them in the address so a desk can send them as a link. */
  private remember(comparing: boolean): void {
    const c = this.choice;
    save("t2s.monitor", { two: this.two, ...this.pick, ...c, chosen: this.chosen });
    const q = new URLSearchParams(comparing ? this.pick : {});
    if (this.chosen && c.profile !== "path") q.set("profile", c.profile);
    else if (this.chosen && this.app.info?.monitor?.profile && this.app.info.monitor.profile !== "path") q.set("profile", "path");
    const differs = (name: string, key: keyof Choice) => c[key] !== DEFAULTS[key] && q.set(name, String(c[key]));
    if (c.profile === "path") {
      differs("rate", "rate");
      differs("size", "size");
    } else {
      differs("voice_size", "voiceSize");
      differs("every", "every");
      if (c.profile === "video") differs("video", "video");
    }
    const query = q.size ? `?${q}` : "";
    if (window.location.search !== query) history.replaceState(null, "", appPath("/monitor") + query);
  }

  private renderTiles(): void {
    clear(this.tiles);
    this.figures = {};
    this.counts = {};
    for (const [key, label] of SHOWN[this.choice.profile]) {
      const value = h("span", { class: "tile-value" }, "—");
      this.figures[key] = value;
      const figure = h("div", { class: "tile-figure" }, value);
      if (key === "loss" || key === "mos" || key === "worst" || key === "below") {
        const count = h("span", { class: "tile-count" });
        this.counts[key] = count;
        figure.append(count);
      }
      this.tiles.append(h("div", { class: "tile compact", "data-key": key }, h("div", { class: "tile-label" }, label), figure));
    }
  }

  private renderCompare(sides: Location[]): void {
    clear(this.compare);
    this.cells = [{}, {}, {}]; // A, B, and B − A
    this.heads = [];
    const head = h("tr", {}, h("th", {}));
    for (const [i, loc] of sides.entries()) {
      const sub = h("span", { class: "compare-sub" }, loc.location || "");
      this.heads.push(sub);
      head.append(
        h(
          "th",
          { class: "num" },
          h("span", { class: "compare-name" }, h("span", { class: "legend-key dot", style: `--key:var(${SIDES[i]!.token})` }), `${loc.name} (${SIDES[i]!.short})`),
          sub,
        ),
      );
    }
    head.append(h("th", { class: "num" }, "B − A"));
    const body = h("tbody", {});
    for (const [key, label] of SHOWN[this.choice.profile]) {
      const row = h("tr", {}, h("th", {}, label));
      for (const cells of this.cells) {
        const cell = h("td", { class: "num" }, "—");
        cells[key] = cell;
        row.append(cell);
      }
      body.append(row);
    }
    this.compare.append(h("table", { class: "table compare" }, h("thead", {}, head), body));
  }

  /** While probing, the destinations and the profile can't change underneath the trace;
   * the path's rate and size, and a call's sizes and rates, can. */
  private lock(on: boolean): void {
    for (const el of this.destBox.querySelectorAll<HTMLButtonElement | HTMLSelectElement>("button, select")) el.disabled = on;
    for (const el of this.profileBox.querySelectorAll<HTMLButtonElement>("[aria-label=Profile] button")) el.disabled = on;
  }

  private say(message: string | null, url: string | null = null): void {
    clear(this.notice);
    this.notice.hidden = !message;
    if (!message) return;
    this.notice.append(message);
    if (url) this.notice.append(" ", h("a", { href: url }, "Click here to continue."));
  }

  // ---- Probing -------------------------------------------------------------------

  private async start(): Promise<void> {
    const app = this.app;
    const sides = this.comparing ? this.sides() : null;
    this.startBtn.disabled = true;
    this.lock(true);
    this.say(null);
    setText(this.transport, "connecting…");
    this.chart.clear();
    this.chart.setCalls([]);
    const call = this.choice.profile !== "path";
    const dest = (ep: Endpoint, info: ServerInfo | null): Dest => ({ ep, info, probe: null, transport: "", sentAt: new Map(), sent: 0, latest: null, stream: call ? new CallStream(this.choice.every) : null });
    this.dests = sides ? sides.map((loc) => dest(app.endpointFor(loc), null)) : [dest(app.ep, app.info)];
    const refused = await Promise.all(this.dests.map((d, i) => this.connect(d, i)));
    this.startBtn.disabled = false;
    const refusal = refused.find((r) => r);
    if (refusal || !this.dests.some((d) => d.probe)) {
      for (const d of this.dests) d.probe?.close();
      this.dests = [];
      this.seriesKey = "";
      this.layout();
      setText(this.transport, refusal ? "refused" : sides ? "servers unreachable" : "server unreachable");
      if (refusal) this.say(refusal.message, refusal.url);
      return;
    }
    this.running = true;
    this.startBtn.textContent = "Stop";
    setText(this.transport, sides ? this.dests.map((d, i) => `${SIDES[i]!.short} ${d.transport.split(" ")[0]}`).join(" · ") : this.dests[0]!.transport);
    this.schedule();
  }

  /**
   * Open a UDP session to one server, or a WebSocket when UDP can't get through. With a
   * call profile, ask for the server's stamps and for the profile; why a server refused
   * it, if it did.
   */
  private async connect(d: Dest, series: number): Promise<{ message: string; url: string | null } | null> {
    const record = (seq: number, rtt: number) => {
      const t = d.sentAt.get(seq);
      if (t === undefined) return;
      d.sentAt.delete(seq);
      d.latest = rtt;
      this.chart.push({ t, rtt }, series);
    };
    const profile = this.choice.profile;
    try {
      d.info ??= await d.ep.info();
      if (!d.info.capabilities.webrtc) throw new Error("WebRTC disabled");
      const rtc = await RtcProbe.connect(d.ep, d.info.ice_servers);
      if (profile !== "path") {
        const answer = await rtc.profile(profile);
        if (answer && !answer.ok) {
          rtc.close();
          if (answer.reason === "traffic limit") return { message: answer.message ?? REFUSED, url: answer.url ?? null };
          return { message: `${d.info.server.name} doesn't offer Monitor's ${profile === "video" ? "Video call" : "Voice call"} profile.`, url: null };
        }
        if (d.info.capabilities.stamps) await rtc.stamp();
        // At a traffic limit the server holds the session to path probes: follow it.
        rtc.onNotice = (msg) => {
          if (msg.op === "limit") this.limited(typeof msg.message === "string" ? msg.message : REFUSED, typeof msg.url === "string" ? msg.url : null);
        };
      }
      rtc.onEcho = (seq, rtt, now, stamp) => {
        record(seq, rtt);
        d.stream?.echo(seq, rtt, now, stamp);
      };
      d.probe = { send: (seq, size) => rtc.send(seq, size), close: () => rtc.close() };
      const family = await rtc.family().catch(() => null);
      d.transport = ["UDP · WebRTC", family].filter(Boolean).join(" · ");
    } catch {
      const ws = new WsProber();
      if (!(await ws.open(d.ep.wsUrl("/ws")))) {
        d.transport = "unreachable";
        return null;
      }
      // Over TCP the probes are round trips of a fixed small size, and video isn't sent.
      d.probe = {
        send: (seq) =>
          void ws.ping(1000).then((rtt) => {
            if (rtt === null) return;
            record(seq, rtt);
            d.stream?.echo(seq, rtt, performance.now(), null);
          }),
        close: () => ws.close(),
      };
      d.transport = "TCP · WebSocket (UDP unavailable)";
    }
    return null;
  }

  /** A server's traffic limit is reached: carry on with path probes, and say why. */
  private limited(message: string, url: string | null): void {
    if (this.choice.profile === "path") return;
    this.choice.profile = "path";
    this.say(`${message} Monitor carries on with small probes.`, url);
    this.renderProfile();
    this.lock(true);
    if (this.running) this.schedule();
  }

  private schedule(): void {
    for (const t of this.timers.splice(1)) clearInterval(t);
    const c = this.choice;
    const call = c.profile !== "path";
    // One clock for every server, so their packets leave together.
    const send = (size: number, frame: number, charted: boolean) => {
      const seq = this.seq++;
      const wall = Date.now();
      const now = performance.now();
      for (const d of this.dests) {
        if (!d.probe) continue;
        if (frame >= 0 && d.transport.startsWith("TCP")) continue;
        if (charted) {
          d.sentAt.set(seq, wall);
          d.sent++;
        }
        d.stream?.sent(seq, frame, now, wall);
        d.probe.send(seq, size);
      }
    };
    this.timers.push(window.setInterval(() => send(call ? c.voiceSize : c.size, -1, true), call ? c.every : 1000 / c.rate));
    if (c.profile === "video") {
      // A frame at a time: its packets leave together, as a video call sends them.
      const burst = framePackets(c.video);
      this.timers.push(
        window.setInterval(() => {
          const frame = this.frame++;
          for (let i = 0; i < burst; i++) send(VIDEO_BYTES, frame, false);
        }, 1000 / VIDEO_FPS),
      );
    }
    // Probes not back within a second are lost.
    this.timers.push(
      window.setInterval(() => {
        const cutoff = Date.now() - 1000;
        for (const [i, d] of this.dests.entries()) {
          for (const [seq, t] of d.sentAt) {
            if (t >= cutoff) continue;
            d.sentAt.delete(seq);
            this.chart.push({ t, rtt: null }, i);
          }
        }
      }, 250),
    );
  }

  private stop(): void {
    for (const t of this.timers.splice(1)) clearInterval(t);
    for (const d of this.dests) {
      d.probe?.close();
      d.probe = null;
    }
    this.running = false;
    if (this.startBtn) this.startBtn.textContent = "Start monitoring";
    if (this.transport) setText(this.transport, "idle");
    if (this.destBox) this.lock(false);
  }

  // ---- Figures -------------------------------------------------------------------

  private figuresFor(i: number, slices: Slice[]): Figures {
    const data = this.chart.visible(i);
    const got = data.filter((d) => d.rtt !== null).map((d) => d.rtt!);
    const lost = data.length - got.length;
    const from = Date.now() - this.chart.windowSeconds * 1000;
    const shown = slices.filter((s) => s.end > from && (s.call || s.dead));
    const score = (s: Slice) => (s.dead ? 1 : s.call!.mos);
    const scores = shown.map(score);
    const worst = shown.reduce<Slice | null>((w, s) => (!w || score(s) < score(w) ? s : w), null);
    const below = shown.filter((s) => s.dead || !["excellent", "good"].includes(s.call!.rating)).length;
    // The median slice's rating, so the word goes with the score shown.
    const byScore = [...shown].sort((a, b) => score(a) - score(b));
    const middle = byScore[Math.floor((byScore.length - 1) / 2)];
    const hits = (side: "up" | "down" | null) => {
      const counts = shown.map((s) => (side ? s.frames?.[side] : s.frames)).filter((c) => c);
      const total = counts.reduce((n, c) => n + c!.total, 0);
      return total ? (counts.reduce((n, c) => n + c!.hit, 0) / total) * 100 : null;
    };
    const eachWay = shown.some((s) => s.frames?.up);
    const frames = eachWay ? Math.max(hits("up") ?? 0, hits("down") ?? 0) : hits(null);
    return {
      latest: this.dests[i]?.latest ?? null,
      median: got.length ? percentile(got, 50) : null,
      p95: got.length ? percentile(got, 95) : null,
      jitter: got.length > 1 ? jitter(got) : null,
      loss: data.length ? (lost / data.length) * 100 : null,
      lost,
      window: data.length,
      sent: this.dests[i]?.sent ?? 0,
      mos: scores.length ? percentile(scores, 50) : null,
      rating: middle ? (middle.dead ? "bad" : middle.call!.rating) : null,
      worst: worst ? score(worst) : null,
      worstAt: worst?.start ?? null,
      below: shown.length ? (below / shown.length) * 100 : null,
      belowCount: below,
      slices: shown.length,
      frames: shown.some((s) => s.frames) ? frames : null,
    };
  }

  private refresh(): void {
    const slices = this.dests.map((d) => d.stream?.slices() ?? null);
    this.chart.setCalls(slices);
    const figures = this.dests.length ? this.dests.map((_, i) => this.figuresFor(i, slices[i] ?? [])) : [this.figuresFor(0, [])];
    if (this.comparing && this.cells.length) this.refreshCompare(figures);
    else this.refreshTiles(figures[0]!);
  }

  private refreshTiles(v: Figures): void {
    for (const [key] of SHOWN[this.choice.profile]) {
      const el = this.figures[key];
      if (el) setText(el, value(key, v));
    }
    const count = (key: Key, text: string, title: string, bad = false) => {
      const el = this.counts[key];
      if (!el) return;
      setText(el, text);
      el.title = title;
      el.classList.toggle("bad", bad);
    };
    // The packets behind the percentage: red once any are lost.
    count("loss", v.window ? `(${v.lost.toLocaleString()})` : "", `${v.lost.toLocaleString()} of ${v.window.toLocaleString()} probes lost in the window`, v.lost > 0);
    count("mos", v.rating ? RATING_WORD[v.rating] : "", "The median 10-second score in the window, and its rating", v.rating !== null && !["excellent", "good"].includes(v.rating));
    count("worst", v.worstAt !== null ? ago(v.worstAt) : "", v.worstAt !== null ? `The worst 10 seconds began at ${new Date(v.worstAt).toLocaleTimeString()}` : "");
    count("below", v.slices ? `(${v.belowCount} of ${v.slices})` : "", "10-second slices rated below good (4.0), of those in the window", v.belowCount > 0);
  }

  private refreshCompare(sides: Figures[]): void {
    const keys = SHOWN[this.choice.profile];
    for (const [i, v] of sides.slice(0, 2).entries()) {
      const c = this.cells[i]!;
      for (const [key] of keys) {
        const cell = c[key];
        if (!cell) continue;
        let shown = value(key, v);
        if (key === "loss" && v.window) shown = `${shown} (${v.lost.toLocaleString()})`;
        if (key === "mos" && v.rating) shown = `${shown} · ${RATING_WORD[v.rating]}`;
        setText(cell, shown);
      }
      c.loss?.classList.toggle("bad", v.lost > 0);
      if (c.loss) c.loss.title = `${v.lost.toLocaleString()} of ${v.window.toLocaleString()} probes lost in the window`;
      const d = this.dests[i];
      setText(this.heads[i]!, [this.sides()?.[i]?.location, d?.transport].filter(Boolean).join(" · "));
    }
    const [a, b] = [sides[0]!, sides[1] ?? sides[0]!];
    const diff = this.cells[2]!;
    for (const [key] of keys) {
      const cell = diff[key];
      if (!cell) continue;
      if (key === "latest" || key === "median" || key === "p95" || key === "jitter") setText(cell, signed(a[key], b[key], (v) => ms(v)));
      else if (key === "loss" || key === "below" || key === "frames") setText(cell, signed(a[key], b[key], (v) => ({ value: pct(v).value, unit: "pts" })));
      else if (key === "mos" || key === "worst") setText(cell, signed(a[key], b[key], (v) => ({ value: v.toFixed(1), unit: "" })));
      else setText(cell, "");
    }
  }
}

/** What the page says each profile sends, and what that costs. */
function lede(c: Choice, two: boolean): string {
  if (c.profile === "path") {
    return two ? "UDP probes to two servers at the same moments from this browser, compared on one chart." : "A steady stream of small UDP probes, echoed by the server. Watch round-trip time, jitter and loss as they happen: useful while reproducing a problem or moving around a building.";
  }
  const kbps = (bytes: number, perSecond: number) => (bytes * 8 * perSecond) / 1000;
  const total = kbps(c.voiceSize, 1000 / c.every) + (c.profile === "video" ? kbps(VIDEO_BYTES, framePackets(c.video) * VIDEO_FPS) : 0);
  const rate = total >= 1000 ? `${(total / 1000).toFixed(1)} Mb/s` : `${Math.round(total)} kb/s`;
  const what = c.profile === "video" ? `A video call's packets, voice and ${VIDEO_FPS} frames a second of video` : "A voice call's packets";
  if (two) return `A ${c.profile} call to two servers at once: about ${rate} each way, scored every 10 s.`;
  return `${what}, about ${rate} each way, echoed by the server. Each 10 seconds is scored as the call would sound, to catch the moments it breaks up.`;
}

/** How long ago, as "40 s ago" or "3 min ago". */
function ago(t: number): string {
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  return s < 120 ? `${s} s ago` : `${Math.round(s / 60)} min ago`;
}

/** A figure as its tile or table cell shows it. */
function value(key: Key, v: Figures): string {
  switch (key) {
    case "latest":
    case "median":
    case "p95":
    case "jitter":
      return fmt(ms(v[key]));
    case "loss":
      return fmt(pct(v.loss));
    case "below":
      return fmt(pct(v.below));
    case "frames":
      return fmt(pct(v.frames));
    case "mos":
    case "worst":
      return v[key] === null ? "—" : `${v[key]!.toFixed(1)} MOS`;
    case "sent":
      return v.sent.toLocaleString();
    default:
      return "—";
  }
}

function seg<T extends string | number>(label: string, values: [T, string][], current: T, set: (v: T) => void): HTMLElement {
  const group = h("div", { class: "seg", role: "radiogroup", "aria-label": label });
  for (const [v, text] of values) {
    const b = h("button", { type: "button", role: "radio", "aria-checked": String(v === current) }, text);
    b.addEventListener("click", () => {
      for (const o of group.children) o.setAttribute("aria-checked", String(o === b));
      set(v);
    });
    group.append(b);
  }
  return h("div", { class: "opt" }, h("span", { class: "opt-label" }, label), group);
}

const fmt = (f: Formatted) => (f.value === "—" ? "—" : `${f.value} ${f.unit}`.trim());

/** B − A with its sign, e.g. "+22.4 ms". */
function signed(a: number | null, b: number | null, format: (v: number) => Formatted): string {
  if (a === null || b === null) return "—";
  const d = b - a;
  const sign = d > 0 ? "+" : d < 0 ? "−" : "";
  return sign + fmt(format(Math.abs(d)));
}
