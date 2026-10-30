# texas2step at a glance

**A network test you host yourself, measuring what your users experience: from their
own browser, phone or command line to your servers.** Throughput, latency, jitter, real
UDP packet loss, call quality and responsiveness, with tools for the service desk and
for engineers. Free and open source under the Apache License 2.0.

[Documentation](https://robert-patrick-texas.github.io/texas2step/) ·
[Install guide](https://robert-patrick-texas.github.io/texas2step/docs/install.html) ·
[Latest release](https://github.com/robert-patrick-texas/texas2step/releases/latest) ·
[Source on GitHub](https://github.com/robert-patrick-texas/texas2step) ·
[One page to print (PDF)](https://robert-patrick-texas.github.io/texas2step/docs/at-a-glance.pdf)

![The Test page during a download](https://robert-patrick-texas.github.io/texas2step/docs/images/hero-dark.jpg)

## Why network teams run it

* **It tests your paths.** Put servers in your data centers, branches and cloud regions.
  Results describe the path your applications take, not the way to a public speed test.
* **Nothing to install for the person testing.** A browser on a laptop or phone runs
  every test. t2s, a single file for Linux, macOS and Windows, does the same from servers
  and scripts.
* **It finds what a speed figure hides:** packet loss each way over real UDP, call quality
  for voice and video, latency under load (bufferbloat), and which protocols and address
  families get through.
* **The service desk can act on it.** A caller reads out a short Result ID, and History
  lists every saved test with the address it came from.
* **It stays inside your network.** No third-party services: no CDN, analytics,
  telemetry or cookies. It runs on air-gapped networks and updates from a local
  directory or mirror.

## What it does

| | What it shows | Web page | t2s | API |
|---|---|---|---|---|
| **Speed test** | Download and upload over parallel streams and over one; latency and jitter, idle and under load (bufferbloat, graded A+ to F); UDP packet loss each way; call quality (MOS) each way; responsiveness (RPM); readiness for video, calls, remote desktops and file transfers | Test | `t2s run` | `/api/v1/measure/full` |
| **Monitor** | A continuous UDP trace of round trip and loss over the last 1, 5 or 15 minutes, or a voice or video call's packets scored every 10 seconds; to one server, or to two side by side | Monitor | `t2s monitor` | |
| **Diag** | HTTP/1.1, HTTP/2, HTTP/3, WebSocket and UDP over IPv4 and IPv6, tried once a second; your addresses, NAT, TLS and DTLS, and an event log | Diag | `t2s diag` | `/api/v1/diag/http` |
| **IPv4 and IPv6** | Each address family tested on its own | IP switch | `-4`, `-6` | A name for each family |
| **Several servers** | A server picker; any server measures another on request | Picker | Any server's URL | `/api/v1/measure/*` |
| **Saved results** | Short Result IDs, History, a share link and a JSON download | History | `--save` | `/api/v1/results` |
| **Automation** | JSON output, copy-ready examples and an OpenAPI explorer | API tab | `--json` | `/api/docs` |

The server's package also has a command line in Python, `texas2step`, with the speed
test, on PyPI too.

## Where it helps

| Situation | Use | What you learn |
|---|---|---|
| A remote worker says the network is slow | The Test page, with the desk's access code where the server faces the Internet; the caller reads out the Result ID | Speed, loss and bufferbloat on their own connection. Testing a nearby server and a distant one tells their Wi-Fi or ISP apart from your WAN. |
| Calls break up in Teams, Zoom or Webex | Call quality on the Test page; Monitor's Voice call and Video call profiles | The score each way, and whether the loss comes in bursts, as late packets, or in one direction |
| Wi-Fi dead spots | Monitor on a phone or laptop, walking the building | Spikes and loss marks show where coverage fails; Stop holds the chart still for a screenshot |
| A new circuit, branch or cloud region | `t2s run --save` from the site; servers measuring each other | Throughput both ways, the one-stream rate and loss, kept in History as a baseline |
| A VPN, firewall or proxy change | Diag, or `t2s diag` | What gets through: HTTP/3 or UDP blocked, IPv6 broken, traffic leaving by an unexpected address |
| An IPv6 rollout | The IP switch, `t2s monitor -4` and `-6`, Diag's rows for each family | Whether IPv6 performs like IPv4, or takes a longer route |
| Two sites or providers compared | Monitor with two servers at once | Which path is worse, moment by moment, as the difference B − A |
| Scheduled checks | t2s or the REST API, from cron or your monitoring system | JSON results to trend and alert on in your own tools |

## Running it

| | |
|---|---|
| **Server** | Ubuntu 26.04 LTS with nginx: one `.deb` package. A setup tool checks the host first, then writes the install script. |
| **Network** | HTTPS on port 443, and a UDP port range for the packet-loss test, Monitor and Diag's UDP rows. |
| **Clients** | A current web browser, on a computer or a phone. t2s for Linux, macOS and Windows, on x86-64 and ARM64. |
| **Updates** | `sudo texas2step update`, by hand or on an optional timer, or offline from a directory or a local mirror. Each download is checked against its SHA-256 first. |
| **Servers open to the Internet** | Access codes from the service desk that change daily, weekly or monthly. Wrong codes lock a network out, and servers can share lockouts. Your own networks need no code. |
| **Billed traffic** | Daily and monthly caps on what the tests move, and time limits for Monitor and Diag. |
| **Who is testing** | `texas2step clients` lists the pages and command-line runs in use now. |
| **Your brand** | Your name, logo, notice, links and footer, and a choice of seven themes. Any page or header item can be hidden. |
| **Security** | A sandboxed service behind nginx, a Content-Security-Policy, and no cookies or telemetry. TLS 1.3 with post-quantum key exchange is recommended. |

## Get started

On an Ubuntu 26.04 LTS server with nginx, download `texas2step-setup` and the `.deb`
from the [latest release](https://github.com/robert-patrick-texas/texas2step/releases/latest)
into one directory, then:

```bash
sudo ./texas2step-setup --id houston --name houston.example.net --location "Houston, TX"
sudo ./texas2step-install.sh          # after reading it; it ends with the address to open
```

Open that address and press Start. From a command line:

```bash
curl -fLo t2s https://github.com/robert-patrick-texas/texas2step/releases/latest/download/t2s-linux-amd64
chmod +x t2s && ./t2s run https://houston.example.net/texas2step
```

The [install guide](https://robert-patrick-texas.github.io/texas2step/docs/install.html)
covers the firewall rule, several locations and every option.

---

**texas2step** is free and open source under the Apache License 2.0:
[documentation](https://robert-patrick-texas.github.io/texas2step/),
[releases](https://github.com/robert-patrick-texas/texas2step/releases) and
[source](https://github.com/robert-patrick-texas/texas2step) on GitHub.
