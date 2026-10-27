# Active clients

Who is using a texas2step server right now, and who did in the last hour: each browser
page and each command-line run, what page it is on, and what it is doing.
`texas2step clients` lists them, the journal says when each starts and ends, and
internal networks can read the same list over the API.

## How pages are told apart

Each page makes a random id when it loads, such as `k7q2m9xd`, and sends it with every
request as `?session=`, with the page shown as `?page=`. The command line makes one id
for each run. So two browsers on one computer are two sessions, though they come from the
same address, and so are two tabs.

No cookie is set, and nothing is kept in the browser: the id lives only in the page's
memory, and a reload starts a new one. It identifies no one and is never reused, so it
can't follow a visitor from one visit to the next.

A client's port can't do this job. One page opens several connections: HTTP/2, a
WebSocket, the IPv4-only and IPv6-only names, HTTP/3 over UDP. Each gets a new port when
it reconnects after going idle, and behind NAT the ports change again. The id ties all
of a page's traffic together, over both address families.

Requests without an id are grouped by address and User-Agent, and listed as `(no id)`.
They come from curl, scripts, and pages and command lines of texas2step 2.4 and older.
Monitoring's `/api/v1/health` checks, which carry no id, aren't listed.

## What counts as active

A session is active while it sends. One with no request for 60 seconds has ended, and is
listed with the ended ones for an hour from its last request.

* **Monitor** and **Diag** send all the time they run. A Monitor whose server has gone
  quiet keeps asking it over HTTPS, so it stays listed.
* **Test** sends while a test runs. A Test page left idle after its test ends a minute
  later, and starts again with its next test.
* **History**, **?** and **API** count for a minute after their last request.
* **The command line** counts for as long as its run.

No heartbeat is added: a page that sends nothing isn't listed.

## Use it

```console
$ sudo texas2step clients
4 active in the last 60s: 3 pages, 1 command-line run.

  Session    Address        Program                 Page      Doing now                                      For     Moved
  k7q2m9xd   2001:db8::7    Chrome 141 on Windows   Monitor   Voice call over UDP                            12 min  210 MB
             203.0.113.7
  p3x8wq2n   203.0.113.7    Firefox 157 on Windows  Diag      WebSocket echo, HTTP checks, UDP probes        3 min   1.1 MB
  h4c9tz6e   10.20.0.15     Edge 141 on macOS       Test      Download, 4 streams, Latency, WebSocket echo   31s     1.4 GB
  d9f4m2kq   198.51.100.4   texas2step 2.5.0        CLI       Upload, 4 streams, Latency, WebSocket echo     8s      410 MB

37 ended in the last hour, the latest first:

  Session    Address        Program                 Page      Last seen    For     Moved
  w2n8kd4r   10.20.0.31     Chrome 141 on Windows   Test      2 min ago    48s     2.8 GB
  ...

17 more: --history lists them all.
```

It only reads: nothing changes, and the service keeps running.

* **Session**: the id the page or run sent, the same one the journal names.
* **Address**: each address it came from, the latest first, one per line. A page that
  tests both address families, as Diag does, shows both.
* **Program**: the browser and system its User-Agent names, or `texas2step` and the
  version for the command line.
* **Page**: Test, Monitor, Diag, History, Help (the **?** tab), API, CLI, or Server: another
  texas2step server testing toward this one for its `/api/v1/measure`.
* **Doing now**: what it has open, and what it did in the last 5 seconds:

  | Shown | What it is |
  |---|---|
  | Download, 4 streams | The download test's requests |
  | Upload, 4 streams | The upload test's |
  | Latency | `/api/v1/ping`: idle latency, and the latency under load |
  | WebSocket echo | The echo socket: latency under load, Diag's WebSocket rows, Monitor where UDP can't get through |
  | HTTP checks | Diag's HTTP rows |
  | UDP probes | The UDP probe: the Test page's packet loss, Monitor's Path profile, Diag's UDP rows |
  | Voice call over UDP, Video call over UDP | Monitor's call profiles |
  | Test toward another server | `/api/v1/measure`, run by this server on the client's behalf |

  `-` when it is between requests.
* **For**: since its first request, or since it last came back after a minute away. For
  an ended session, from its first request to its last.
* **Last seen**: an ended session's last request.
* **Moved**: the bytes of its requests and answers, WebSocket messages and UDP probes,
  both ways, without headers or packet overhead.

The latest 20 ended sessions are listed; `--history` lists every one of the last hour.
`--watch` shows the list again every 2 seconds, until Ctrl-C: each time it is read
afresh, and a session drops from the ended ones an hour after its last request. `--json`
prints the active and the ended ones for scripts, with the full User-Agent and times in
seconds since the epoch:

```json
{
  "time": 1793068862.8,
  "idle_seconds": 60.0,
  "history_seconds": 3600.0,
  "clients_max": 1000,
  "full": false,
  "clients": [
    {
      "session": "k7q2m9xd",
      "page": "monitor",
      "program": "Chrome 141 on Windows",
      "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...",
      "addresses": ["2001:db8::7", "203.0.113.7"],
      "started": 1793068142.1,
      "last_seen": 1793068862.7,
      "seconds": 720.7,
      "sent": 109051904,
      "received": 101187584,
      "doing": ["Voice call over UDP"],
      "activities": {"udp:voice": 1}
    }
  ],
  "ended": []
}
```

An ended session has the same fields, with `doing` empty and `seconds` from its first
request to its last.

It exits 0, with "No clients active in the last 60s." when there are none, or "No
clients: the service makes /run/texas2step as it starts." when the service hasn't run
since the computer started. It exits 1 when it can't read the file, saying why; run it
with `sudo`.

## How many are listed

At most `clients_max` sessions, 1,000 by default, active and ended together. Ended ones
make room for active ones, the oldest first, so a busy hour keeps fewer than an hour's.

With that many active at once, a new session is served as usual but not listed, nor
told of in the journal; once some end, it is tried again, a minute after it was left
out. The list says so ("The list is full"), and the journal gets a warning, at most once
every 10 minutes, with how many sessions were left out since the last one, and the
addresses that have the most sessions:

```text
clients: the list is full: 1,000 sessions active, clients_max = 1,000; 4,512 new ones weren't listed. The most sessions come from 203.0.113.9 (812), 198.51.100.4 (97), 10.20.0.15 (12)
```

That is what a reload a thousand times over looks like, or bots that each open the page
in a browser: every load is a new session. No one address is held to fewer, as a whole
office can share one behind NAT; the warning names the address, and the
`clients: … started` lines before it show what its sessions did.

Every 2 seconds, each of the service's workers writes the sessions that changed since
it last wrote. At 1,000 sessions that takes 20 to 30 ms if every one of them sent
something, and 5 to 7 ms if a few did; at 5,000, the most `clients_max` allows, about
six times as long. It all stays in memory. The workers take turns at the file, and the
one whose turn finds the list full leaves the new session out, so it never holds more
than `clients_max`.

## In the journal

Each session's start, each move to another page, and its end:

```text
clients: k7q2m9xd started: Monitor, Chrome 141 on Windows, from 2001:db8::7
clients: k7q2m9xd now on Diag
clients: k7q2m9xd ended: Diag, Chrome 141 on Windows, from 2001:db8::7, 203.0.113.7, after 14 min, moved 212 MB
```

`journalctl -u texas2step -g clients:` lists them. They're written at info priority,
with `debug` on or off, once each, whichever worker sees them. A session ends a minute
after its last request, so its end comes a minute after the page closed. After the
service restarts, the sessions that keep sending carry on, and the rest end a minute
after their last request. The warning that [the list is full](#how-many-are-listed) is
at warning priority, so `journalctl -u texas2step -p warning` finds it.

## Over the API

For a dashboard: `GET /api/v1/clients` answers callers in `internal_networks` with what
`--json` prints. Everyone else gets `404`, as everyone does on a server without
`internal_networks`.

```bash
curl -s https://houston.example.net/texas2step/api/v1/clients
```

## Where it's kept

| | |
|---|---|
| `/run/texas2step/clients.db` (`clients_file`) | For each active session, and each worker that served it: its id, page, User-Agent, addresses, when it started and was last seen, the bytes it moved, and what it is doing. For each ended one of the last hour, the same without the last. Nothing older, and at most `clients_max` sessions |

`/run` is a tmpfs, in memory: nothing is written to disk, and a reboot clears it.
systemd makes `/run/texas2step` for the service as it starts, belonging to the service's
user, and keeps it over a restart or an upgrade; `apt purge` removes it. `/dev/shm`, also
in memory, would serve as well, but any local user can write there and could plant a
file in its place first.
