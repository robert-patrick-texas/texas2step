// API reference with copy-ready examples against this server.
import { appPath } from "../../engine/net";
import type { App, View } from "../app";
import { h } from "../dom";

interface Endpoint {
  method: "GET" | "POST" | "WS";
  path: string;
  title: string;
  about: string;
  example: (origin: string) => string;
}

const GROUPS: { title: string; about: string; items: Endpoint[] }[] = [
  {
    title: "Measurement primitives",
    about: "The wire endpoints every test is built from. Stable, tiny and safe to script against.",
    items: [
      { method: "GET", path: "/api/v1/ping", title: "Round-trip probe", about: "Minimal response with Server-Timing. Reuse the connection and time request → first byte.", example: (o) => `curl -s -w '%{time_starttransfer}\\n' -o /dev/null ${o}/api/v1/ping` },
      { method: "GET", path: "/api/v1/down?bytes=N", title: "Download", about: "Streams N incompressible bytes from memory (no disk). Use ?seconds=S to stream for a fixed time instead.", example: (o) => `curl -o /dev/null -w '%{speed_download} B/s\\n' '${o}/api/v1/down?bytes=1000000000'` },
      { method: "POST", path: "/api/v1/up", title: "Upload", about: "Reads and discards the body as it arrives; returns bytes received and the server-observed rate.", example: (o) => `head -c 500000000 /dev/urandom | curl -s -X POST --data-binary @- ${o}/api/v1/up` },
      { method: "WS", path: "/api/v1/ws", title: "Echo socket", about: "Echoes every message unchanged. Used for latency under load and live monitoring.", example: (o) => `websocat ${o.replace(/^http/, "ws")}/api/v1/ws` },
      { method: "POST", path: "/api/v1/rtc/offer", title: "UDP probe signalling", about: "Answers a WebRTC offer with a 'probe' channel (unordered, no retransmit) echoing packets, and a 'control' channel reporting server-side counters ({\"op\":\"stats\"}).", example: (o) => `texas2step loss ${o}` },
    ],
  },
  {
    title: "Server-side measurements",
    about: "This server measures toward another one like it and returns the result. Targets must be configured locations.",
    items: [
      { method: "POST", path: "/api/v1/measure/latency", title: "Latency", about: "Idle HTTP round-trip time and jitter.", example: (o) => `curl -s -X POST ${o}/api/v1/measure/latency -H 'content-type: application/json' -d '{"target":"https://peer.example","count":20}'` },
      { method: "POST", path: "/api/v1/measure/download", title: "Download", about: "Multi-stream download from the target, with latency under load.", example: (o) => `curl -s -X POST ${o}/api/v1/measure/download -H 'content-type: application/json' -d '{"target":"https://peer.example","seconds":10,"streams":4}'` },
      { method: "POST", path: "/api/v1/measure/upload", title: "Upload", about: "Multi-stream upload to the target.", example: (o) => `curl -s -X POST ${o}/api/v1/measure/upload -H 'content-type: application/json' -d '{"target":"https://peer.example"}'` },
      { method: "POST", path: "/api/v1/measure/loss", title: "Packet loss", about: "UDP loss, RTT and jitter over WebRTC.", example: (o) => `curl -s -X POST ${o}/api/v1/measure/loss -H 'content-type: application/json' -d '{"target":"https://peer.example","packets":500}'` },
      { method: "POST", path: "/api/v1/measure/full", title: "Everything", about: "Latency, download, upload and loss in one call.", example: (o) => `curl -s -X POST ${o}/api/v1/measure/full -H 'content-type: application/json' -d '{"target":"https://peer.example"}'` },
    ],
  },
  {
    title: "Discovery and results",
    about: "Who the server is, who you are, and saved test history.",
    items: [
      { method: "GET", path: "/api/v1/info", title: "Server info", about: "Identity, version, capabilities and limits.", example: (o) => `curl -s ${o}/api/v1/info` },
      { method: "GET", path: "/api/v1/whoami", title: "Client view", about: "Your address and family as the server sees them.", example: (o) => `curl -s ${o}/api/v1/whoami` },
      { method: "GET", path: "/api/v1/locations", title: "Locations", about: "Servers offered in the picker.", example: (o) => `curl -s ${o}/api/v1/locations` },
      { method: "GET", path: "/api/v1/results", title: "Saved results", about: "Summaries, newest first. GET /api/v1/results/{id} returns one in full; POST stores one.", example: (o) => `curl -s '${o}/api/v1/results?limit=10'` },
    ],
  },
];

export class ApiView implements View {
  constructor(private readonly app: App) {}

  mount(root: HTMLElement): void {
    const origin = this.app.origin.base;
    root.append(
      h(
        "section",
        { class: "page-head" },
        h(
          "div",
          {},
          h("h1", {}, "API"),
          h("p", { class: "lede" }, "Every measurement the page makes is available to scripts, monitoring systems and other servers. All endpoints send CORS and Timing-Allow-Origin headers."),
        ),
        h("div", { class: "head-actions" }, h("a", { class: "btn", href: appPath("/api/docs") }, "OpenAPI explorer"), h("a", { class: "btn", href: appPath("/api/openapi.json") }, "openapi.json")),
      ),
      h(
        "section",
        { class: "card cli-card" },
        h("h2", {}, "Command line"),
        h("p", {}, "The Python package ships the same engine as a CLI. Results are comparable with browser runs."),
        code(`uv tool install texas2step          # or: pipx install texas2step\ntexas2step run ${origin}            # full test\ntexas2step latency ${origin} --count 50\ntexas2step download ${origin} --streams 8 --seconds 15\ntexas2step loss ${origin} --packets 1000 --json`),
      ),
      ...GROUPS.map((g) =>
        h(
          "section",
          { class: "api-group" },
          h("h2", {}, g.title),
          h("p", { class: "lede" }, g.about),
          h(
            "div",
            { class: "api-list" },
            ...g.items.map((e) =>
              h(
                "article",
                { class: "card api-item" },
                h("div", { class: "api-sig" }, h("span", { class: `method m-${e.method.toLowerCase()}` }, e.method), h("code", {}, e.path)),
                h("h3", {}, e.title),
                h("p", {}, e.about),
                code(e.example(origin)),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

function code(src: string): HTMLElement {
  const btn = h("button", { class: "copy", type: "button", "aria-label": "Copy to clipboard" }, "Copy");
  btn.addEventListener("click", () =>
    void navigator.clipboard.writeText(src).then(() => {
      btn.textContent = "Copied";
      setTimeout(() => (btn.textContent = "Copy"), 1200);
    }),
  );
  return h("div", { class: "code" }, h("pre", {}, h("code", {}, src)), btn);
}
