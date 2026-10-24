// Saved results on this server: a trend chart and a table that links to each run.
import { appPath } from "../../engine/net";
import type { AccessCode, ResultSummary } from "../../types";
import type { App, View } from "../app";
import { axisX, gridY, legend, linear, linePath } from "../charts/common";
import { clear, h, onResize, pointerIn, s } from "../dom";
import { bytes, mbpsTick, ms, pct, rate, text, when } from "../format";
import { hideTip, showTip } from "../tooltip";

const TRAFFIC_NOTE = "The bytes the test downloaded and uploaded, as the client counted them. The network carried a few percent more in packet headers.";

export class HistoryView implements View {
  private body!: HTMLElement;
  private desk!: HTMLElement;

  constructor(private readonly app: App) {}

  mount(root: HTMLElement): void {
    this.body = h("div", {}, h("p", { class: "status" }, "Loading…"));
    this.desk = h("div", { class: "desk-tools" });
    root.append(
      h("section", { class: "page-head" }, h("div", {}, h("h1", {}, "History"), h("p", { class: "lede" }, "Tests saved on this server, newest first. Browser runs save automatically; use texas2step run --save from the CLI.")), this.desk),
      this.body,
    );
    if (this.app.pages.has("test")) this.desk.append(this.opener());
    this.show();
  }

  /** Back from another page: a test run there is on the list now, and the code may have changed. */
  show(): void {
    void this.code(this.desk);
    void this.load(this.body);
  }

  /** Opens a result by the ID a caller reads out; case and spaces don't matter. */
  private opener(): HTMLFormElement {
    const input = h("input", { class: "code-input small", type: "text", placeholder: "Result ID", "aria-label": "Result ID", autocomplete: "off", autocapitalize: "none", spellcheck: "false", required: true });
    const form = h("form", { class: "desk-open" }, input, h("button", { class: "btn small", type: "submit" }, "Open"));
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const id = input.value.trim().replace(/\s+/g, "");
      if (id) this.app.navigate(`/r/${encodeURIComponent(id)}`);
    });
    return form;
  }

  /** The visitors' access code, where this server asks for one (docs/access.md). */
  private async code(desk: HTMLElement): Promise<void> {
    let code: AccessCode;
    try {
      code = await this.app.origin.json<AccessCode>("/access/code");
    } catch {
      return; // no access codes here, or not for this client
    }
    const at = (iso: string) => new Date(iso).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit", timeZone: code.timezone, timeZoneName: "short" });
    const changes = code.changes_at ? `changes ${at(code.changes_at)}` : "fixed";
    const until = code.previous_until === code.changes_at ? "then" : at(code.previous_until ?? "");
    const before = code.previous && code.previous_until ? ` · ${code.previous} still works until ${until}` : "";
    desk.querySelector(".desk-code")?.remove();
    desk.prepend(h("p", { class: "desk-code", title: `The code visitors enter (${code.interval}, ${code.timezone})` }, "Access code ", h("strong", {}, code.code), h("span", { class: "cell-sub" }, ` ${changes}${before}`)));
  }

  private async load(body: HTMLElement): Promise<void> {
    let results: ResultSummary[];
    try {
      results = (await this.app.origin.json<{ results: ResultSummary[] }>("/results?limit=200")).results;
    } catch (err) {
      body.replaceChildren(h("p", { class: "status" }, `History is unavailable: ${err instanceof Error ? err.message : String(err)}`));
      return;
    }
    clear(body);
    if (!results.length) {
      const run = this.app.pages.has("test") ? h("a", { href: "/", "data-link": true, class: "btn primary" }, "Run the first one") : null;
      body.append(h("div", { class: "card empty-card" }, h("p", {}, "No saved tests yet."), run));
      return;
    }
    if (results.length > 1) body.append(this.trend([...results].reverse()));
    const rows = results.map((r) =>
      h(
        "tr",
        {},
        // A saved result opens on the Test page, when this site shows it.
        h("td", { class: "when" }, this.app.pages.has("test") ? h("a", { href: appPath(`/r/${r.id}`), "data-link": true }, when(r.created_at)) : when(r.created_at)),
        h("td", {}, r.server_id ?? "—"),
        h("td", { class: "mono" }, h("span", { class: "addr" }, r.client_ip ?? "—")),
        h("td", { class: "num" }, text(rate(r.download_mbps))),
        h("td", { class: "num" }, text(rate(r.upload_mbps))),
        h("td", { class: "num" }, text(ms(r.latency_ms))),
        h("td", { class: "num" }, text(ms(r.jitter_ms))),
        h("td", { class: "num" }, text(pct(r.loss_pct))),
        h("td", { class: "num" }, r.traffic_bytes === null || r.traffic_bytes === undefined ? "—" : bytes(r.traffic_bytes)),
        h("td", {}, (r.source ?? "").replace(/\/.*/, "").replace("texas2step-", "")),
      ),
    );
    body.append(
      h(
        "section",
        { class: "card" },
        h(
          "div",
          { class: "table-wrap" },
          h(
            "table",
            { class: "table data" },
            h("thead", {}, h("tr", {}, ...["When", "Server", "Client", "Download", "Upload", "Latency", "Jitter", "Loss", "Traffic", "Via"].map((c, i) => h("th", { scope: "col", class: i >= 3 && i <= 8 ? "num" : "", title: c === "Traffic" ? TRAFFIC_NOTE : null }, c)))),
            h("tbody", {}, ...rows),
          ),
        ),
      ),
    );
  }

  private trend(results: ResultSummary[]): HTMLElement {
    const plot = h("div", { class: "chart-plot", role: "img", "aria-label": "Download and upload across saved tests" });
    const series = [
      { key: "download_mbps" as const, label: "Download", color: "var(--dl)" },
      { key: "upload_mbps" as const, label: "Upload", color: "var(--ul)" },
    ];
    onResize(plot, (W) => {
      const H = 200;
      const m = { top: 12, right: 16, bottom: 30, left: 52 };
      const max = Math.max(1, ...results.flatMap((r) => [r.download_mbps ?? 0, r.upload_mbps ?? 0]));
      const x = linear(0, Math.max(1, results.length - 1), m.left, W - m.right);
      const step = Math.max(1, Math.ceil(results.length / Math.max(2, Math.floor(W / 140))));
      x.ticks = results.map((_, i) => i).filter((i) => i % step === 0);
      const y = linear(0, max * 1.1, H - m.bottom, m.top);
      const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, class: "chart-svg" });
      svg.append(gridY(y, m.left, W - m.right, mbpsTick));
      const span = results[results.length - 1]!.created_at - results[0]!.created_at;
      const label = (i: number) => {
        const d = new Date(results[i]!.created_at * 1000);
        return span < 86400 ? d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
      };
      svg.append(axisX(x, H - m.bottom, label));
      for (const sr of series) {
        const pts = results.flatMap((r, i): [number, number][] => (r[sr.key] === null ? [] : [[x(i), y(r[sr.key]!)]]));
        svg.append(s("path", { d: linePath(pts), class: "line", style: `stroke:${sr.color}` }));
        if (results.length <= 40) for (const [px, py] of pts) svg.append(s("circle", { cx: px, cy: py, r: 4, class: "dot", style: `fill:${sr.color}` }));
      }
      const cross = s("line", { y1: m.top, y2: H - m.bottom, class: "crosshair", visibility: "hidden" });
      const hit = s("rect", { x: m.left, y: m.top, width: W - m.left - m.right, height: H - m.top - m.bottom, class: "hit" });
      hit.addEventListener("pointermove", (ev) => {
        const px = pointerIn(ev, svg, W).x;
        const i = Math.round(((px - m.left) / (W - m.left - m.right)) * (results.length - 1));
        const r = results[Math.max(0, Math.min(results.length - 1, i))]!;
        cross.setAttribute("x1", String(x(results.indexOf(r))));
        cross.setAttribute("x2", String(x(results.indexOf(r))));
        cross.setAttribute("visibility", "visible");
        showTip(ev.clientX, ev.clientY, when(r.created_at), series.map((sr) => ({ value: text(rate(r[sr.key])), label: sr.label, color: sr.color })));
      });
      hit.addEventListener("pointerleave", () => {
        cross.setAttribute("visibility", "hidden");
        hideTip();
      });
      svg.append(cross, hit);
      plot.replaceChildren(svg);
    });
    return h(
      "section",
      { class: "card chart-card" },
      h("header", { class: "card-head" }, h("h2", {}, "Throughput trend"), h("p", {}, "Stable download and upload rate of each saved test.")),
      h("div", { class: "chart" }, legend(series.map((sr) => ({ label: sr.label, color: sr.color, shape: "line" as const }))), plot),
    );
  }
}
