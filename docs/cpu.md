# Server CPU

What a texas2step server's CPU is doing: the CPU its worker processes use, nginx's, and
the rest of the host's, beside the tests and UDP sessions the workers serve. It shows
whether the workers' CPU grows over the days, whether it follows the traffic, and when
it runs high with nothing to serve. `texas2step cpu` shows the figures. The journal warns
of what stands out. Internal networks can read them over the API, from Prometheus and on
Diag. Two more things use the same figures:

* **A busy host.** As a setting, the server can refuse new high-volume tests while
  other work on the host is busy.
* **CPU-bound results.** Each test's result says when the server's CPU, not the network,
  set a transfer's rate, as it can on links of 100 Gb/s and faster.

## CPU without much traffic

A UDP probe costs the server about the same CPU whatever its size. Each one goes through
WebRTC's SCTP and DTLS, written in Python: the worker decrypts it, reads it, sends it back
encrypted, and acknowledges it. So Monitor's sessions cost CPU by the packet, not by the
byte. One worker on a 4-core virtual machine measured:

| Open on one worker | Its CPU | On the wire, each way |
|---|---|---|
| Nothing | 0.1 to 0.2 % of a core | none |
| 30 Path sessions, 10 probes a second each | 21 % | under 1 Mb/s |
| 10 Voice calls, 50 packets a second each | 25 % | about 1.6 Mb/s |
| 5 Video calls, about 164 packets a second each | 34 % | about 7.5 Mb/s |

That is about half a millisecond of a core for each probe. So 40 % of a core on each of
two workers, with the rest of the host idle and the network quiet, is about a dozen
Monitor Video calls, or some thirty Voice calls. New sessions spread over both workers,
so both show it. Next to a 100 Gb/s link their traffic looks like none.

`texas2step cpu` puts the sessions and probes beside the CPU, and
`sudo texas2step clients` names each session ("Video call over UDP", "UDP probes"). These
were also checked, and cost nothing once over:

* **A client that disappears without closing its session.** The worker's CPU drops to
  idle at once. The session keeps its place until `rtc_session_timeout` (120 s), so
  for that long it counts toward `max_rtc_sessions`.
* **A download whose client leaves partway.** Nothing goes on after the client has gone.
* **An offer whose UDP never connects.** About 6 ms of CPU each.

If the workers use CPU with nothing to serve, the journal says so (see below). Then
`py-spy top --pid <worker>`, run as root, shows which Python function is using it.
py-spy is a separate tool: `pip install py-spy`.

## Use it

```console
$ sudo texas2step cpu
texas2step on houston, 16 cores. Now, over the last 30s:

  Workers     81% of a core: 2310 41%, 2311 40%
  nginx       0.4% of a core
  Other work  3.1% of the host's CPU   (cpu_busy_percent = 80: tests let in)
  Host        8.2% of the host's CPU
  Serving     12 UDP sessions, 1,968 probes/s; tests 0.0 Mb/s
  Memory      251.2 MB: 2310 126.0 MB, 2311 125.2 MB

The last hour, by ten minutes:

  From    Workers   Busiest   Other   Probes/s   Sessions   Tests
  13:10   80%       42%       3.0%    1,964      12         0.4 Mb/s
  13:20   81%       42%       3.2%    1,968      12         0.0 Mb/s
  13:30   81%       41%       2.9%    1,968      12         0.0 Mb/s
  13:40   95%       57%       3.4%    1,970      12         2,840.3 Mb/s
  13:50   81%       42%       3.1%    1,968      12         0.0 Mb/s
  14:00   81%       41%       3.1%    1,968      12         0.0 Mb/s

Each day, the latest first:

  Day          Idle   Average   Peak   Other   Probes        Tests     Memory
  2026-11-03   -      80%       97%    3.3%    84,602,112    38.2 GB   251.2 MB
  2026-11-02   0.3%   14%       88%    3.9%    12,402,118    41.0 GB   250.7 MB
  2026-11-01   0.3%   3.1%      71%    4.2%    1,120,440     12.6 GB   249.9 MB

Workers, Busiest, Idle, Average and Peak are shares of one core, as top shows each
process; Other work and Host, of all the host's cores. Idle: with no tests, probes or
UDP sessions to serve (docs/cpu.md).
```

It only reads: nothing changes, and the service keeps running.

* **Workers**: the CPU texas2step's worker processes used together over the last 30
  seconds, and each one's, by process id. 100 % is one core, as `top` shows each
  process. One worker can use one core at most.
* **nginx**: nginx's processes. They carry the tests' HTTPS, so on a fast link they work
  as hard as the workers. nginx's whole CPU counts, including any other sites it serves.
* **Other work**: what the host's other programs use, as a share of all its cores (see
  [What counts as other work](#what-counts-as-other-work)). With `cpu_busy_percent`,
  whether new tests are let in.
* **Host**: everything, as a share of all its cores.
* **Serving**: the UDP sessions open (Monitor, Diag's UDP rows, the Test page's
  packet-loss step), the probes a second they take in, and the tests' TCP traffic: the
  downloads, uploads and WebSocket echoes, both ways together.
* **Memory**: the workers' resident memory.

The last hour comes in steps of ten minutes. **Workers** is their average, and
**Busiest** is the highest any single worker reached in one of those minutes.
**Sessions** is the most that were open at once.

Each day, in `access_timezone`:

* **Idle**: the workers' CPU with nothing to serve, the median of the day's quiet
  minutes. A quiet minute is one with no tests, no probes and no UDP sessions open. A
  day needs 60 of them, or it shows `-`, as on a day a Monitor ran throughout. This is
  the figure that should hold steady from one week to the next. If it climbs, something
  in the workers uses more the longer they run.
* **Average**, **Peak**: the workers' CPU over the day, and in its busiest minute.
* **Other**: other work's average.
* **Probes**, **Tests**: the probes taken in and the tests' TCP bytes. Read them beside
  Average to see whether the CPU follows the traffic.
* **Memory**: the workers' memory at the day's highest.

`--days N` shows N days, 0 to 30. `--watch` shows the figures again every 5 seconds,
until Ctrl-C. `--json` prints them for scripts, as `GET /api/v1/cpu` does: `texas2step`
and `nginx` in cores (1.0 is one core), and `host` and `other` as shares of all the
host's cores (1.0 is all of them):

```json
{
  "now": {
    "seconds": 30.0,
    "texas2step": 0.8102,
    "nginx": 0.0041,
    "host": 0.0819,
    "other": 0.0310,
    "cores": 16,
    "sessions": 12,
    "probes_per_s": 1968.4,
    "sent_mbps": 0.0,
    "received_mbps": 0.0,
    "workers": [
      {"pid": 2310, "cpu": 0.4121, "memory": 126017536, "sessions": 6,
       "probes_per_s": 984.1, "sent_mbps": 0.0, "received_mbps": 0.0}
    ]
  },
  "busy": {"percent": 80.0, "seconds": 30.0, "refusing": false, "since": null},
  "hour": [
    {"minute": "2026-11-03T13:10:00-05:00", "texas2step": 0.8031, "busiest": 0.4188,
     "nginx": 0.0039, "host": 0.0801, "other": 0.0298, "sent_mbps": 0.2,
     "received_mbps": 0.2, "probes_per_s": 1964.0, "sessions": 12, "workers": 2}
  ],
  "days": [
    {"day": "2026-11-03", "minutes": 840, "quiet_minutes": 0, "idle": null,
     "average": 0.8007, "peak": 0.9688, "other": 0.0331, "bytes": 38204118016,
     "probes": 84602112, "memory": 251232256}
  ]
}
```

`busy` is null without `cpu_busy_percent`. `now` is null when no worker has written in
the last 15 seconds. The command then prints "No worker has written in the last 15s: is
the service running?" and exits 0. It exits 1 when the service has never run ("no
figures yet: the service writes /var/lib/texas2step/cpu.db as it runs"), or when it can't
read the file. Then it says why; run it with `sudo`.

## What counts as other work

The host's busy time, from `/proc/stat`, less texas2step's workers and nginx. Busy time
is user, nice, system and steal time. Idle time and time waiting for the disk aren't
busy. The kernel's interrupt time, mostly its network processing, counts for neither
side. At 100 Gb/s the tests' own packets would otherwise count as other work, and could
keep out the next test.

On a virtual machine, steal time is the host under it giving the core to another
machine, so it counts as other work. A host whose cloud neighbours are busy is busy.

## Keep tests off a busy host

On a lab host that does other work, the tests can wait until it has room. In
`/etc/texas2step/config.toml`:

```toml
cpu_busy_percent = 80
cpu_busy_seconds = 30
```

Then `sudo systemctl restart texas2step`. `sudo texas2step config set cpu_busy_percent=80
--write` does the same edit.

* **`cpu_busy_percent`**: the share of all the host's cores other work may use before
  new tests are refused. It is averaged over `cpu_busy_seconds`. 0, the default, never
  refuses.
* **`cpu_busy_seconds`**: how long it is averaged over, 10 to 3600 seconds; 30 by default.

Each worker looks every 5 seconds. Past the setting:

* **New download and upload tests are refused**, with `503` and "This server is busy.
  Try again in a few minutes." The page, `texas2step run` and t2s show the message, as
  they do for a [traffic limit](traffic.md). A request without a test id, such as
  curl's, is refused too.
* **Tests toward other servers** that move traffic (`/api/v1/measure/download`,
  `/upload` and `/full`) are refused.
* **Monitor's Video call is refused**, and Video calls already open go on as their Voice
  call. The page says "This server is busy. Try again in a few minutes. Monitor carries
  on with the Voice call." A page from before 2.12.0 goes on with Path probes instead.
* **Tests already running finish.** The browser and the command line give each test an
  id. A test let in before the host got busy goes on, for up to 10 minutes.
* **Everything else goes on**: latency, the packet-loss test, Monitor's Path and Voice,
  and Diag.

Once refusing, the server lets tests in again when other work falls under nine tenths of
the setting, 72 % for 80. A load that hovers at the setting would otherwise refuse and
let in by turns. The journal hears of both:

```text
cpu: other work is using 85% of the host's CPU (cpu_busy_percent = 80): new download and upload tests and Video calls are refused
cpu: other work is down to 61% of the host's CPU: tests are let in again, after 4 min
```

`texas2step cpu` says whether tests are refused, and since when; Diag, whether.

## Server CPU-bound results

One worker process serves each request, on one core at most. A worker serves all of a
test's streams that reach it, so on a fast, short path a worker can be the limit. Over
loopback on a 4-core test host, t2s's download over 4 streams reached 35 Gb/s from two
workers, the busier at a full core.

For each transfer of a test (the download, one stream's download, the upload) the server
keeps the busiest worker's share of a core while it served it. The client asks for it
when the test ends:

* **The Test page** puts "Server CPU-bound" and the share in the tile's small print, at
  90 % of a core or more. All measurements lists the share for each transfer.
* **`texas2step run` and t2s** print "server CPU-bound: a worker ran at 99% of a core, so
  the server, not the network, set this rate" under the transfer.
* **The result** carries it as `server_cpu` in each transfer, saved with the rest.

A CPU-bound rate says what the server can serve, not what the path carries. More workers
(`WORKERS` in `/etc/default/texas2step`, up to one per core) spread a test's streams over
more cores. Servers from before 2.12.0 don't say, and their results have no
`server_cpu`.

## In the journal

```text
cpu: the workers used 40% of a core for the last 10 min with no tests, probes or UDP sessions to serve (now by worker: 2310 21%, 2311 19%): not their traffic. py-spy top --pid <worker> shows where it goes (docs/cpu.md)
cpu: with nothing to serve, the workers use 4.1% of a core today, against 1.2% on the 7 days before: something in them uses more as they run. texas2step cpu has the figures (docs/cpu.md)
```

* **CPU with nothing to serve**: 10 % of a core or more for 10 minutes, with no tests,
  probes or UDP sessions. At most once an hour.
* **Idle CPU climbing**: a day's Idle at least twice the median of the 7 days before,
  and at least 2 % of a core higher. It needs 3 earlier days with an Idle figure. At most
  once a day.
* **A busy host**: see [Keep tests off a busy host](#keep-tests-off-a-busy-host).

`journalctl -u texas2step -g cpu:` lists them. They are warnings, once each, whichever
worker sees them first.

## Over the API

For callers in `internal_networks`. Everyone else gets `404`, as everyone does on a
server without `internal_networks`.

* **`GET /api/v1/cpu`** answers with what `--json` prints; `?days=0` leaves out the days.
* **`GET /api/v1/metrics`** answers in Prometheus's text format:

  | Metric | Type | |
  |---|---|---|
  | `texas2step_worker_cpu_seconds_total{worker}` | counter | CPU time each worker has used |
  | `texas2step_worker_memory_bytes{worker}` | gauge | Each worker's resident memory |
  | `texas2step_test_bytes_total{worker,direction}` | counter | The tests' TCP bytes, `sent` and `received` |
  | `texas2step_udp_probes_total{worker}` | counter | UDP probes taken in |
  | `texas2step_udp_sessions{worker}` | gauge | UDP sessions open |
  | `texas2step_cpu_cores` | gauge | The host's cores |
  | `texas2step_nginx_cpu_cores` | gauge | Cores nginx used, over the last 30 s |
  | `texas2step_host_cpu_ratio` | gauge | The host's busy share of all its cores, over the last 30 s |
  | `texas2step_other_cpu_ratio` | gauge | Other work's share, over the last 30 s |
  | `texas2step_busy` | gauge | 1 while tests are refused; only with `cpu_busy_percent` |

  A worker's counters start again when it restarts, with its new process id.
  `rate(texas2step_worker_cpu_seconds_total[5m])` beside
  `rate(texas2step_udp_probes_total[5m])` shows the CPU following the probes. A scrape
  from Prometheus:

  ```yaml
  scrape_configs:
    - job_name: texas2step
      scheme: https
      metrics_path: /texas2step/api/v1/metrics
      static_configs:
        - targets: ["houston.example.net", "dallas.example.net"]
  ```

* **Diag** shows a **Server CPU** card under NAT and egress to visitors from
  `internal_networks`, asked every 5 seconds: the figures for now, and whether tests are
  refused. Its Events list says when the server starts and stops refusing.

`GET /api/v1/tests/{id}` is open to anyone who may test. A test's client uses it to ask
about its own transfers; see the [API](api.md).

## Where it's kept

| | |
|---|---|
| `/var/lib/texas2step/cpu.db` (`cpu_file`) | Each worker's row every 5 seconds, for 2 hours; each minute's figures, for 30 days; each test's CPU figures, for an hour; the tests let in while the host was busy, for 10 minutes; and which warnings the journal was given |

30 days of minutes come to a few megabytes. The file belongs to the service's user, and
`apt purge` removes it with the rest of `/var/lib/texas2step`.
