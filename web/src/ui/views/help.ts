// The ? tab: what this tool does, for anyone. A summary up top for a reader with a minute,
// then more on each page shown. It never names the tool, as a site may be renamed.
import type { App, View } from "../app";
import { h, reducedMotion } from "../dom";
import { maxRuntime, span } from "../runtime";

type Part = string | HTMLElement;

const b = (text: string) => h("strong", {}, text);
const p = (...parts: Part[]) => h("p", {}, ...parts);
const list = (tag: "ul" | "ol", items: Part[][]) => h(tag, {}, ...items.map((parts) => h("li", {}, ...parts)));
/** Where the site stops a page after a while, a line that says so. */
const limit = (page: "monitor" | "diag", text: (span: string) => string) => (maxRuntime(page) ? [p(text(span(maxRuntime(page) / 1000)))] : []);

/** One of the pages described: its summary up top, and its section below. */
interface Topic {
  page: string;
  href: string;
  name: string;
  question: string;
  summary: string;
  details: () => HTMLElement[];
}

const TOPICS: Topic[] = [
  {
    page: "test",
    href: "./",
    name: "Test",
    question: "How good is my connection right now?",
    summary: "A check of under a minute: speed both ways, delay, and lost packets, graded for video calls, streaming, remote desktops and file transfers.",
    details: () => [
      p("Press ", b("Start"), ". The test runs five steps in turn, shown under Phases:"),
      list("ol", [
        [b("Latency"), ": 20 small requests, each timed to the server and back. The figure is the middle one, in milliseconds (ms); lower is better. ", b("Jitter"), " is how much one differs from the next."],
        [b("Download"), ": pulling data from the server to your device, several transfers at once, 4 by default, for 10s. The figure, in megabits a second (Mbps), leaves out the first seconds, while the connection speeds up."],
        [b("One stream"), ": one transfer on its own for the same time, as a single file copy or remote desktop gets."],
        [b("Upload"), ": pushing data from your device to the server, with the same transfers and timing as Download."],
        [b("Packet loss"), ": 500 small numbered UDP packets, 10 ms apart, each echoed by the server. Any that don't come back were lost. Calls and video travel this way."],
      ]),
      p("While the transfers run, the page also measures:"),
      list("ul", [
        [b("Under load"), ": how much the delay grows while the connection is busy, graded A+ to F. A big rise is why calls break up while someone else on the network downloads."],
        [b("Responsiveness"), ": round trips per minute while busy (RPM); higher is better."],
        [b("Call quality"), ": how a voice call would sound, from the packet-loss step, scored 1 to 5 and rated Excellent, Good, Fair, Poor or Bad. Each direction is scored, and the worse is shown."],
        [b("Rate limit"), ": a speed that holds, then drops partway and stays down, as when a provider's speed boost ends or a firewall limits a transfer. The tile shows the lower speed, when it came, and the speed before. A longer test, under Test options, catches a boost a short one ends inside."],
        [b("Server CPU-bound"), ": the server ran at a full core while it served the transfer, so the server, not the network, set its speed. Fast links can outrun a server; the path may carry more."],
      ]),
      p(
        b("Readiness"),
        " sums it up: Great, Good, Fair or Poor for 4K video streaming, video calls, reaction time (remote desktops, VDI and SSH) and large file transfers. ",
        b("All measurements"),
        " lists every figure, to download or copy. Where the site keeps results, a finished test is saved with the address the server saw it from, and the page shows its ID: read it to the service desk, who can open the same result.",
      ),
      p(b("Test options"), " change the number of transfers at once, how long each runs, the number of UDP packets and their spacing, and whether One stream runs. Where the site has set it up, they also choose the HTTP version: HTTP/1.1, where each transfer has a connection of its own, or HTTP/3, over UDP."),
      h("h3", {}, "For good results"),
      list("ul", [
        ["Keep the page in front while it runs: browsers slow pages in the background."],
        ["Pause other downloads, streaming and backups on the same connection, unless they're what you want to see."],
        ["To find where a problem lies, compare: Wi-Fi and a cable, near the access point and far from it, at home and at the office."],
        ["The results describe the path to this server. Where the site has several, the picker in the header chooses one, and ", b("IP"), ", where shown, holds the test to IPv4 or IPv6."],
      ]),
    ],
  },
  {
    page: "monitor",
    href: "monitor",
    name: "Monitor",
    question: "Does it stay good?",
    summary: "Runs until you stop it, and shows each moment the connection slows or drops. Use it while a problem is happening, or while walking around a building.",
    details: () => [
      p(
        "Monitor sends packets to the server without a break until you press ",
        b("Stop"),
        ", and charts each one's round trip, with loss per second beneath. The figures cover the last 1, 5 or 15 minutes (",
        b("Window"),
        "). Leave it running while you reproduce a problem, or carry a laptop or phone around a building to find where the Wi-Fi fails: a spike or a red mark shows the moment.",
      ),
      p("A profile sets what it sends:"),
      list("ul", [
        [b("Path"), ": small probes, 10 a second by default, for round trip, jitter and loss with very little traffic."],
        [b("Voice call"), ": a voice call's packets, about 160 kb/s each way."],
        [b("Video call"), ": a video call's voice and video, about 1.5 Mb/s each way. An hour uses about 1.3 GB in all, which counts on a metered mobile plan."],
      ]),
      p(
        "With a call profile, each 10s is scored as the call would sound, 1 to 5 as on Test. ",
        b("Call quality"),
        " is the typical score, ",
        b("Worst 10s"),
        " the lowest and when, ",
        b("Below good"),
        " how often it fell below good, and for video, ",
        b("Frames hit"),
        " the share of frames that would freeze or smear.",
      ),
      p(
        "Where the site has more than one server, ",
        b("Servers: Two"),
        " sends the same packets to two of them at the same moments and compares them side by side, with the difference B − A. When one path is worse, the trouble lies on that path, not in this device.",
      ),
      p("A server that stops answering for 3s is reported over the chart; Monitor keeps trying, and says how long it was gone once it answers. Stop holds the chart still for a screenshot. Going to another page stops Monitor too."),
      ...limit("monitor", (t) => `This site stops Monitor ${t} after each Start; reload the page to run it again.`),
    ],
  },
  {
    page: "diag",
    href: "diag",
    name: "Diag",
    question: "Can I reach the server, and how?",
    summary: "Tries every way a browser can connect, once a second, and shows which work: what a firewall or VPN lets through, and what it blocks.",
    details: () => [
      p(
        "Diag tries each way this browser can reach the server, once a second, for as long as the page is open: HTTP/1.1, HTTP/2 and HTTP/3 (the web's own protocols), WebSocket (a lasting two-way connection) and UDP (what calls and video use), over IPv4 and IPv6 where the server offers them. Each row shows its packets going out and coming back, its round trip and its losses.",
      ),
      list("ul", [
        [b("Green"), ": answering."],
        [b("Red"), ": answered before, not now."],
        [b("Grey"), ": no answer yet, or not offered."],
        [b("Amber"), ": restricted; it needs the access code."],
      ]),
      p("It shows what a speed test can't: that web pages load but calls fail because UDP is blocked, that IPv6 fails where IPv4 works, or that a VPN or firewall turns HTTP/3 away."),
      p(
        "The box at the top gives this device's address as the server sees it, and how far its clock is from the server's. ",
        b("NAT and egress"),
        " compares the addresses TCP and UDP traffic leave from. ",
        b("Events"),
        " records each change with its time, and ",
        b("Copy log"),
        " copies them, to send to the service desk.",
      ),
      ...limit("diag", (t) => `This site stops Diag after ${t} on screen; reload the page to run it again.`),
    ],
  },
];

const WORDS: [string, string][] = [
  ["Latency", "the time a packet takes to reach the server and come back, in milliseconds (ms). Lower is better."],
  ["Jitter", "how much latency changes from one packet to the next. Calls and video need it low."],
  ["Packet loss", "the share of packets that never arrive. Calls and video suffer first."],
  ["Mbps, Mb/s, kb/s", "megabits or kilobits a second: how much data a connection carries."],
  ["Bufferbloat", "delay that builds up while a connection is busy, as data waits its turn in a queue."],
  ["Call quality (MOS)", "a mean opinion score from 1 to 5: how a call would sound. 4.0 and above is good."],
  ["TCP and UDP", "the two ways data travels. TCP resends what is lost, so loss shows as delay; UDP doesn't, and calls and video use it for speed."],
  ["IPv4 and IPv6", "the two kinds of internet address. A network can work over one and fail over the other."],
];

export class HelpView implements View {
  constructor(private readonly app: App) {}

  mount(root: HTMLElement): void {
    const topics = TOPICS.filter((t) => this.app.pages.has(t.page));
    const sections = new Map(topics.map((t) => [t.page, h("section", { class: "card help-card" }, h("h2", {}, t.name), ...t.details())]));
    const desk = [this.app.pages.has("history") && [b("History"), " lists the tests saved here, newest first, and opens any by its Result ID."], this.app.pages.has("api") && [b("API"), " shows how scripts and monitoring systems run the same measurements."]].filter((x): x is Part[] => !!x);

    root.append(
      h("section", { class: "page-head" }, h("div", {}, h("h1", {}, "What this tool does"), h("p", { class: "lede" }, "A one-minute summary, then more on each page for those who want it."))),
      h(
        "section",
        { class: "card help-summary" },
        h("h2", {}, "In short"),
        p(
          "This tool checks the network connection between your device and a server your organization runs. It works in the web browser, with nothing to install. It sends test traffic to the server and back and times it, so it sees what your applications see: how fast the connection is, how quickly it answers, and whether anything is lost on the way.",
        ),
        h(
          "div",
          { class: "help-topics" },
          ...topics.map((t) =>
            h(
              "div",
              { class: "help-topic" },
              h("h3", {}, h("a", { href: t.href, "data-link": true }, t.name)),
              h("p", { class: "help-question" }, t.question),
              h("p", {}, t.summary),
              h("button", { class: "btn small", type: "button", onclick: () => this.reveal(sections.get(t.page)!) }, "More below"),
            ),
          ),
        ),
        p("The service desk uses the results to tell whether a problem lies in your device, your Wi-Fi or local network, or further along the way. The tool sees only its own test traffic, not your files, browsing or other applications."),
      ),
      h("h2", { class: "help-more" }, "Additional information"),
      ...sections.values(),
      h("section", { class: "card help-card" }, h("h2", {}, "Words used on these pages"), h("dl", { class: "help-words" }, ...WORDS.flatMap(([word, meaning]) => [h("dt", {}, word), h("dd", {}, meaning)]))),
      ...(desk.length ? [h("section", { class: "card help-card" }, h("h2", {}, "For the service desk"), ...desk.map((parts) => p(...parts)))] : []),
    );
  }

  /** Scroll a section to just under the header, gently unless motion is turned down. */
  private reveal(section: HTMLElement): void {
    const bar = document.querySelector<HTMLElement>(".topbar")?.offsetHeight ?? 0;
    window.scrollTo({ top: section.getBoundingClientRect().top + window.scrollY - bar - 12, behavior: reducedMotion() ? "auto" : "smooth" });
  }
}
