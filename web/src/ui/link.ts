// The path between the two endpoints, drawn as a two-lane link: the upper lane carries
// traffic server → you, the lower lane you → server. Particles follow what the test is
// doing right now, so the page shows *where* the bytes are going, not only how many.
import { cssVar, h, reducedMotion, setText } from "./dom";
import { isDarkTheme, onThemeChange } from "./theme";

export type LinkMode = "idle" | "latency" | "download" | "upload" | "loss";

interface Particle {
  lane: 0 | 1; // 0 = downstream (right → left), 1 = upstream (left → right)
  t: number; // 0..1 along the lane
  speed: number; // lane lengths per second
  size: number;
  color: string;
  alpha: number;
  echo?: boolean; // latency pulse: turns around at the far end
}

const MAX_PARTICLES = 420;

export class LinkView {
  readonly el: HTMLElement;
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private particles: Particle[] = [];
  private mode: LinkMode = "idle";
  private mbps = 0;
  private spawnDebt = 0;
  private last = 0;
  private raf = 0;
  private visible = true;
  private colors = { dl: "", ul: "", idle: "", muted: "", lane: "" };
  readonly client: { title: HTMLElement; detail: HTMLElement };
  readonly server: { title: HTMLElement; detail: HTMLElement };
  private center: HTMLElement;

  constructor() {
    this.canvas = h("canvas", { class: "link-canvas", "aria-hidden": "true" });
    this.ctx = this.canvas.getContext("2d")!;
    const node = (side: "client" | "server", glyph: SVGElement | HTMLElement) => {
      const title = h("div", { class: "node-title" }, "—");
      const detail = h("div", { class: "node-detail" }, "");
      const el = h("div", { class: `node node-${side}` }, h("div", { class: "node-icon" }, glyph), title, detail);
      return { el, title, detail };
    };
    const c = node("client", icon("client"));
    const sv = node("server", icon("server"));
    this.client = c;
    this.server = sv;
    this.center = h("div", { class: "link-center" }, "");
    this.el = h(
      "section",
      { class: "link card", "aria-label": "Connection between you and the server" },
      c.el,
      h("div", { class: "link-wire" }, this.canvas, h("span", { class: "lane-label lane-down" }, "server → you"), h("span", { class: "lane-label lane-up" }, "you → server"), this.center),
      sv.el,
    );
    new ResizeObserver(() => this.resize()).observe(this.canvas);
    new IntersectionObserver(([e]) => {
      this.visible = !!e?.isIntersecting;
      if (this.visible) this.loop();
    }).observe(this.el);
    onThemeChange(this.el, () => this.readColors());
    this.readColors();
    this.loop();
  }

  setMode(mode: LinkMode, label = ""): void {
    this.mode = mode;
    this.el.dataset.mode = mode;
    setText(this.center, label);
    if (mode !== "download" && mode !== "upload") this.mbps = 0;
  }

  setLabel(label: string): void {
    setText(this.center, label);
  }

  setRate(mbps: number): void {
    this.mbps = mbps;
  }

  /** One latency probe: out on the upper lane, back on the lower. */
  pulse(): void {
    this.add({ lane: 1, t: 0, speed: 1.6, size: 3.2, color: this.colors.idle, alpha: 1, echo: true });
  }

  packetOut(): void {
    this.add({ lane: 1, t: 0, speed: 0.9, size: 2, color: this.colors.idle, alpha: 0.9 });
  }

  packetBack(): void {
    this.add({ lane: 0, t: 0, speed: 0.9, size: 2, color: this.colors.idle, alpha: 0.9 });
  }

  private add(p: Particle): void {
    if (this.particles.length < MAX_PARTICLES) this.particles.push(p);
  }

  private readColors(): void {
    this.colors = {
      dl: cssVar("--dl"),
      ul: cssVar("--ul"),
      idle: cssVar("--idle"),
      muted: cssVar("--muted"),
      lane: cssVar("--grid"),
    };
  }

  private resize(): void {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const { width, height } = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.round(width * dpr);
    this.canvas.height = Math.round(height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  private lanes(): { w: number; y: [number, number] } {
    const { width, height } = this.canvas.getBoundingClientRect();
    return { w: width, y: [height * 0.36, height * 0.64] };
  }

  private spawn(dt: number): void {
    const busy = this.mode === "download" || this.mode === "upload";
    if (busy) {
      // Density and speed grow with log(rate) so 10 Mbps and 10 Gbps both read.
      const level = Math.min(1, Math.log10(1 + this.mbps) / 4);
      this.spawnDebt += dt * (8 + level * 160);
      const lane: 0 | 1 = this.mode === "download" ? 0 : 1;
      const color = this.mode === "download" ? this.colors.dl : this.colors.ul;
      while (this.spawnDebt >= 1) {
        this.spawnDebt -= 1;
        this.add({
          lane,
          t: Math.random() * 0.04,
          speed: 0.35 + level * 0.9 + Math.random() * 0.25,
          size: 1.2 + Math.random() * 1.6,
          color,
          alpha: 0.55 + Math.random() * 0.45,
        });
      }
    } else if (this.mode === "idle") {
      this.spawnDebt += dt * 1.2;
      while (this.spawnDebt >= 1) {
        this.spawnDebt -= 1;
        this.add({ lane: Math.random() < 0.5 ? 0 : 1, t: 0, speed: 0.12 + Math.random() * 0.08, size: 1.6, color: this.colors.muted, alpha: 0.5 });
      }
    }
  }

  private loop(): void {
    if (this.raf || !this.visible) return;
    const frame = (now: number) => {
      this.raf = 0;
      const dt = this.last ? Math.min(0.05, (now - this.last) / 1000) : 0.016;
      this.last = now;
      if (!reducedMotion()) {
        this.spawn(dt);
        this.step(dt);
      }
      this.draw();
      if (this.visible) this.raf = requestAnimationFrame(frame);
      else this.last = 0;
    };
    this.raf = requestAnimationFrame(frame);
  }

  private step(dt: number): void {
    const next: Particle[] = [];
    for (const p of this.particles) {
      p.t += p.speed * dt;
      if (p.t >= 1) {
        if (p.echo) {
          next.push({ ...p, lane: 0, t: 0, echo: false });
        }
        continue;
      }
      next.push(p);
    }
    this.particles = next;
  }

  private draw(): void {
    const ctx = this.ctx;
    const { w, y } = this.lanes();
    const { height } = this.canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, w, height);

    // Lanes: hairlines with soft end caps.
    ctx.lineWidth = 1;
    ctx.strokeStyle = this.colors.lane;
    for (const ly of y) {
      ctx.beginPath();
      ctx.moveTo(4, ly);
      ctx.lineTo(w - 4, ly);
      ctx.stroke();
    }

    ctx.globalCompositeOperation = isDarkTheme() ? "lighter" : "source-over";
    for (const p of this.particles) {
      const x = p.lane === 1 ? 4 + p.t * (w - 8) : w - 4 - p.t * (w - 8);
      const fade = Math.min(1, p.t * 8, (1 - p.t) * 8);
      ctx.globalAlpha = p.alpha * fade;
      ctx.fillStyle = p.color;
      ctx.beginPath();
      // A short comet tail sells direction at a glance.
      const dir = p.lane === 1 ? -1 : 1;
      const tail = 4 + p.speed * 10;
      ctx.ellipse(x + (dir * tail) / 2, y[p.lane], tail / 2 + p.size, p.size, 0, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
  }
}

function icon(kind: "client" | "server"): SVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute(
    "d",
    kind === "client"
      ? "M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5V15H4zM2 17h20v1a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z"
      : "M5 3h14a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zm0 11h14a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2zm2-7.5a1 1 0 1 0 0 2 1 1 0 0 0 0-2zm0 11a1 1 0 1 0 0 2 1 1 0 0 0 0-2z",
  );
  path.setAttribute("fill", "currentColor");
  path.setAttribute("fill-rule", "evenodd");
  svg.append(path);
  return svg;
}
