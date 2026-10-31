// The hero instrument: a 270° log-scale speed dial with a start button at its heart.
import { h, reducedMotion, s, setText } from "./dom";
import { rate } from "./format";

const START = 135; // degrees, clockwise from +x
const SWEEP = 270;
const R = 150;
const C = 200;
const MAX_MBPS = 10_000;
// The packet-loss test's ring: inside the speed arc, around the start button, on the same
// 270° sweep, filling as probes go out, so half the probes reach 12 o'clock.
const RP = 128;

export type GaugeTone = "idle" | "download" | "upload" | "latency" | "loss";

const pos = (mbps: number) => Math.min(1, Math.log10(1 + Math.max(0, mbps)) / Math.log10(1 + MAX_MBPS));

function polar(deg: number, r = R): [number, number] {
  const rad = (deg * Math.PI) / 180;
  return [C + r * Math.cos(rad), C + r * Math.sin(rad)];
}

function arc(from: number, to: number, r = R): string {
  const [x0, y0] = polar(from, r);
  const [x1, y1] = polar(to, r);
  return `M ${x0} ${y0} A ${r} ${r} 0 ${to - from > 180 ? 1 : 0} 1 ${x1} ${y1}`;
}

export class Gauge {
  readonly el: HTMLElement;
  readonly button: HTMLButtonElement;
  private value: SVGPathElement;
  private glow: SVGPathElement;
  private tip: SVGCircleElement;
  private readout: HTMLElement;
  private number: HTMLElement;
  private unit: HTMLElement;
  private caption: HTMLElement;
  private shown = 0;
  private target = 0;
  private raf = 0;
  private tone: GaugeTone = "idle";
  private packetBand: SVGPathElement;
  private packetEdge: SVGPathElement;
  private packetShine: SVGPathElement;
  private packetLost: SVGGElement;
  private packetsShown = 0; // the share of the ring drawn
  private packetsTarget = 0;
  private packetRaf = 0;

  constructor(onStart: () => void) {
    const ticks = s("g", { class: "gauge-ticks" });
    for (let decade = 0; decade <= 4; decade++) {
      for (let m = 1; m < 10; m++) {
        const v = m * 10 ** decade;
        if (v > MAX_MBPS) break;
        const deg = START + SWEEP * pos(v);
        const major = m === 1;
        const [x0, y0] = polar(deg, R + 16);
        const [x1, y1] = polar(deg, R + (major ? 30 : 22));
        ticks.append(s("line", { x1: x0, y1: y0, x2: x1, y2: y1, class: major ? "major" : "minor" }));
        if (major) {
          const [lx, ly] = polar(deg, R + 46);
          const label = v >= 1000 ? `${v / 1000}G` : String(v);
          ticks.append(s("text", { x: lx, y: ly, class: "gauge-label" }, label));
        }
      }
    }
    const [zx, zy] = polar(START, R + 46);
    ticks.append(s("text", { x: zx, y: zy, class: "gauge-label" }, "0"));

    this.glow = s("path", { d: arc(START, START + 0.01), class: "gauge-glow" });
    this.value = s("path", { d: arc(START, START + 0.01), class: "gauge-value" });
    this.tip = s("circle", { r: 9, cx: polar(START)[0], cy: polar(START)[1], class: "gauge-tip" });
    this.packetEdge = s("path", { d: "", class: "packet-edge" });
    this.packetBand = s("path", { d: "", class: "packet-band" });
    this.packetShine = s("path", { d: "", class: "packet-shine" });
    this.packetLost = s("g", { class: "packet-lost" });
    const packets = s(
      "g",
      { class: "packet-ring" },
      s("path", { d: arc(START, START + SWEEP, RP), class: "packet-track" }),
      this.packetEdge,
      this.packetBand,
      this.packetShine,
      this.packetLost,
    );

    const svg = s(
      "svg",
      { viewBox: "0 0 400 400", class: "gauge-svg", "aria-hidden": "true" },
      s(
        "defs",
        {},
        gradient("g-download", "--dl-from", "--dl"),
        gradient("g-upload", "--ul-from", "--ul"),
        gradient("g-latency", "--idle-from", "--idle"),
        metal(),
        s("filter", { id: "g-blur", x: "-50%", y: "-50%", width: "200%", height: "200%" }, s("feGaussianBlur", { stdDeviation: 10 })),
      ),
      s("circle", { cx: C, cy: C, r: R + 74, class: "gauge-ring ring-3" }),
      s("circle", { cx: C, cy: C, r: R + 62, class: "gauge-ring ring-2" }),
      s("circle", { cx: C, cy: C, r: R - 34, class: "gauge-ring ring-1" }),
      s("path", { d: arc(START, START + SWEEP), class: "gauge-track" }),
      packets,
      ticks,
      this.glow,
      this.value,
      this.tip,
    );

    this.number = h("span", { class: "gauge-number" }, "0");
    this.unit = h("span", { class: "gauge-unit" }, "Mbps");
    this.caption = h("span", { class: "gauge-caption" }, "");
    this.readout = h("div", { class: "gauge-readout", "aria-live": "polite" }, this.caption, this.number, this.unit);
    this.button = h(
      "button",
      { class: "start-btn", type: "button", onclick: () => onStart() },
      h("span", { class: "start-label" }, "Start"),
      h("span", { class: "start-sub" }, "Run the full test"),
    );
    this.el = h("div", { class: "gauge", "data-tone": "idle" }, svg, this.readout, this.button);
  }

  /** idle: start button visible; running: live readout. */
  setRunning(running: boolean, again = false): void {
    this.el.classList.toggle("running", running);
    setText(this.button.querySelector(".start-label"), again ? "Again" : "Start");
    setText(this.button.querySelector(".start-sub"), again ? "Run another test" : "Run the full test");
    if (!running) this.set(0, "idle");
  }

  set(mbps: number, tone: GaugeTone = this.tone): void {
    if (tone !== this.tone) {
      this.tone = tone;
      this.el.dataset.tone = tone; // CSS picks the arc gradient
    }
    this.target = pos(mbps);
    if (tone === "download" || tone === "upload") {
      const f = rate(mbps);
      setText(this.number, f.value === "—" ? "0" : f.value);
      setText(this.unit, f.unit);
    }
    this.animate();
  }

  /** The packet-loss test's ring: `sent` of `total` probes, filling clockwise from the
   * speed arc's start. 0 clears it, as a new test begins. */
  setPackets(sent: number, total: number): void {
    const share = total > 0 ? Math.min(1, Math.max(0, sent / total)) : 0;
    if (share === 0) {
      this.packetsShown = this.packetsTarget = 0;
      this.drawPackets();
      this.packetLost.replaceChildren();
      return;
    }
    this.packetsTarget = share;
    if (!this.packetRaf) {
      this.packetRaf = requestAnimationFrame(() => {
        this.packetRaf = 0;
        this.packetsShown = this.packetsTarget;
        this.drawPackets();
      });
    }
  }

  /** The finished test: the ring as far as its probes went, with a notch at each lost one
   * (null in `packets`) out of `total`. */
  setPacketResult(packets: (number | null)[], total: number): void {
    if (!total) return this.setPackets(0, 0);
    this.packetsShown = this.packetsTarget = Math.min(1, packets.length / total);
    this.drawPackets();
    const marks = packets.flatMap((p, seq) => {
      if (p !== null) return [];
      const deg = START + (SWEEP * (seq + 0.5)) / total;
      const [x0, y0] = polar(deg, RP - 7);
      const [x1, y1] = polar(deg, RP + 7);
      return [s("line", { x1: x0, y1: y0, x2: x1, y2: y1 })];
    });
    this.packetLost.replaceChildren(...marks);
  }

  private drawPackets(): void {
    const on = this.packetsShown > 0;
    this.el.classList.toggle("has-packets", on);
    const d = on ? arc(START, START + Math.max(0.5, SWEEP * this.packetsShown), RP) : "";
    this.packetEdge.setAttribute("d", d);
    this.packetBand.setAttribute("d", d);
    this.packetShine.setAttribute("d", on ? arc(START, START + Math.max(0.5, SWEEP * this.packetsShown), RP - 2) : "");
  }

  /** Free-form readout for phases that aren't a rate (latency, packets). */
  readoutText(caption: string, value: string, unit: string): void {
    setText(this.caption, caption);
    setText(this.number, value);
    setText(this.unit, unit);
  }

  setCaption(text: string): void {
    setText(this.caption, text);
  }

  private animate(): void {
    if (this.raf) return;
    const step = () => {
      const diff = this.target - this.shown;
      this.shown = reducedMotion() || Math.abs(diff) < 0.001 ? this.target : this.shown + diff * 0.14;
      const deg = START + Math.max(0.01, SWEEP * this.shown);
      const d = arc(START, deg);
      this.value.setAttribute("d", d);
      this.glow.setAttribute("d", d);
      const [x, y] = polar(deg);
      this.tip.setAttribute("cx", String(x));
      this.tip.setAttribute("cy", String(y));
      this.el.style.setProperty("--level", this.shown.toFixed(3));
      this.raf = this.shown === this.target ? 0 : requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  }
}

/** Silver and gold, in bands across the packet ring that stay put as it fills. */
function metal(): SVGLinearGradientElement {
  const stops: [number, string][] = [
    [0, "--metal-gold-deep"],
    [0.2, "--metal-gold"],
    [0.36, "--metal-shine"],
    [0.5, "--metal-silver"],
    [0.64, "--metal-shine"],
    [0.8, "--metal-gold"],
    [1, "--metal-gold-deep"],
  ];
  return s(
    "linearGradient",
    { id: "g-metal", gradientUnits: "userSpaceOnUse", x1: C - RP, y1: C - RP, x2: C + RP, y2: C + RP },
    ...stops.map(([offset, color]) => s("stop", { offset, style: `stop-color: var(${color})` })),
  );
}

function gradient(id: string, from: string, to: string): SVGLinearGradientElement {
  return s(
    "linearGradient",
    { id, x1: 0, y1: 1, x2: 1, y2: 0 },
    s("stop", { offset: 0, style: `stop-color: var(${from})` }),
    s("stop", { offset: 1, style: `stop-color: var(${to})` }),
  );
}
