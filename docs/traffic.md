# Traffic limits

For texas2step servers whose traffic is billed, in a cloud region or at a hosting
provider. texas2step can cap the traffic its tests move in a day, in a month, or both.
Once a limit is reached, new tests are refused until the next day or month begins; tests
already running finish.

## What a test moves

A test moves as much as the path carries, for 10 seconds each way by default, and 10
seconds more for [one stream's download](methodology.md#one-stream), which moves at
most as much as the download:

| Speed of the path | Download | One stream, at most | Upload | One test, at most |
|---|---|---|---|---|
| 100 Mbps | 125 MB | 125 MB | 125 MB | 375 MB |
| 1 Gbps | 1.25 GB | 1.25 GB | 1.25 GB | 3.75 GB |
| 10 Gbps | 12.5 GB | 12.5 GB | 12.5 GB | 37.5 GB |

With `default_one_stream = false` a test leaves one stream out unless the visitor turns it
on in Test options, or the command line asks with `--one-stream`.

History's Traffic column shows what each saved test moved.

Clouds such as AWS, Azure and Google Cloud generally bill the traffic a server sends,
which is the visitors' downloads, and not what it receives. Some hosting providers bill
both directions. Check your provider's terms.

## Turn it on

In `/etc/texas2step/config.toml`:

```toml
traffic_day_gb = 50
traffic_month_gb = 1000
traffic_counts = "sent"
traffic_url = "https://houston.example.net/texas2step/"
```

Then `sudo systemctl restart texas2step`.

* **`traffic_day_gb`, `traffic_month_gb`**: gigabytes of 10^9 bytes, the unit providers
  bill in; against one that bills per 2^30 bytes, the limit errs low. 0, the default,
  sets no limit. Days and months begin at midnight in `access_timezone`.
* **`traffic_counts`**: what counts. `sent` is what the server sends: the visitors'
  downloads, and its own uploads to other servers. `received` is the reverse. `both`, the
  default, adds them up.
* **`traffic_count_internal`**: `true`, the default, counts tests from `internal_networks`
  like any other. Set it to `false` when they reach the server by a path that costs little
  or nothing, such as a VDI farm in the same cloud region: their tests then neither count
  nor are refused.
* **`traffic_message`**: what a refused visitor reads; by default "Testing temporarily
  disabled."
* **`traffic_url`**: if set, the message is followed by "Click here to continue.", a link
  to this address: another texas2step server, another provider's test, or the service
  desk's page. It starts with `https://`, `http://`, `mailto:` or `tel:`.

Without a limit, nothing is counted and no file is kept.

## At the limit

* **New tests are refused.** Their download and upload requests get `503`, and the Test
  page shows `traffic_message` and, if `traffic_url` is set, "Click here to continue." as
  a link to it. The command line, which can't show a link, spells the address out:
  "Testing temporarily disabled. Click here to continue:
  https://houston.example.net/texas2step/". Visitors aren't told when tests start again,
  and no countdown is shown; the desk sees it with `texas2step traffic`.
* **Running tests finish.** The browser and the command line mark each test's requests
  with an id (`?test=`). Once a server lets a test in, it lets the rest of that test in
  too, for up to 10 minutes. So the tests running when a limit is reached carry the total
  past it, by up to one test each.
* **Older clients are cut short.** A client without the id, such as `curl` or texas2step
  1.8 and older, has each request checked alone: the first one after the limit is
  refused.
* **Everything else keeps working**: the pages, History, Monitor and Diag.

The journal tells of each limit twice, once at 80% and once when it is used up:

```text
traffic: 80% of today's allowance is used: 41.5 GB of 50 GB
traffic: today's allowance of 50 GB is used up (50.2 GB); new tests are refused until midnight (America/New_York)
```

`journalctl -u texas2step -g traffic:` lists them, and a monitoring system can watch for
them.

## For the service desk

```console
$ sudo texas2step traffic
Traffic limit: 50 GB a day, 1,000 GB a month.
It counts what the server sends; internal_networks included.
Days and months begin at midnight in America/New_York.

               Counted    Allowed    Left
  Today        33.8 GB    50 GB      16.2 GB
  This month   539.1 GB   1,000 GB   460.9 GB

Tests are open.

  Day          Sent      Received   Internal sent   Internal received
  2026-10-23   31.6 GB   7.9 GB     2.2 GB          733.3 MB
  2026-10-22   51 GB     12.4 GB    800 MB          266.7 MB
  2026-10-21   38.2 GB   9.1 GB     3.3 GB          1.1 GB
```

It lists 30 days; `--days` changes that. Sent and received there are the traffic with
visitors outside `internal_networks`; internal is theirs. Once a limit is reached, "Tests
are open." gives way to why, when tests start again, and what visitors read:

```text
New tests are refused: Today's allowance is used up; tests start again at midnight (America/New_York).
Visitors read: Testing temporarily disabled. Click here to continue. (links to https://houston.example.net/texas2step/)
```

When a test must run after the day's limit is reached, let more through:

```bash
sudo texas2step traffic allow 5      # 5 GB more today, and so this month
sudo texas2step traffic allow -5     # take it back
```

The service sees it at once. An allowance added by hand lasts for that day; it raises the
month's by as much.

For scripts, `/api/v1/info` gives `traffic`: whether the caller may start a test, and to
callers in `internal_networks`, the day's and month's figures and when tests start again.
See [the API](api.md#discovery).

## Monitor's call profiles

Monitor's [Voice call and Video call profiles](methodology.md#live-monitor) send a call's
packets for as long as the page runs, so they count toward the limit too. They are
counted as the network carries them: each packet with its headers, and the
acknowledgements the server sends back, as measured over IPv6. A tab left running:

| Profile | Server sends | Server receives | Both, a day |
|---|---|---|---|
| Voice call | 171 kb/s, 1.85 GB a day | 133 kb/s, 1.43 GB a day | 3.3 GB |
| Video call, 1 Mb/s of video | 1.46 Mb/s, 15.7 GB a day | 1.37 Mb/s, 14.7 GB a day | 30.5 GB |

When the limit is reached, the server holds those sessions to the small probes of the
Path profile, which aren't counted, and Monitor says so with the site's message and link.
Until the limit opens again, a call profile is refused at its start.
`monitor_video = false` in the settings withholds the Video call profile altogether, and
`monitor_voice = false` the Voice call one: the page doesn't offer them, and the server
refuses them. With both, Monitor runs only the uncounted Path profile.

## What isn't counted

The limit counts the bytes of the download and upload tests, and of Monitor's call
profiles. The bill also has:

* **Packet headers**: TCP, IP and TLS add 3% to 5% to what the tests move.
* **Page loads**: about 150 KB a visit, less once the browser has cached the page.
* **Monitor's Path profile, Diag, the UDP probe and the latency checks**: a few hundred
  bytes a second.
  A page left open adds up, though. Over 24 hours, measured at the IP layer over IPv6
  (IPv4's headers are smaller, so about 10% less):

  | Page left open | Server sends | Server receives | Both |
  |---|---|---|---|
  | Monitor, 10 probes a second (its default) | 257 MB | 174 MB | 431 MB |
  | Monitor, 50 probes a second | 1.26 GB | 839 MB | 2.1 GB |
  | Diag, six rows (HTTP/2, WebSocket and UDP over both families) | 219 MB | 128 MB | 347 MB |

  So a month of one Monitor tab at its default rate costs the server about 7.7 GB sent,
  and a Diag tab 6.6 GB. A Monitor comparing two servers costs each the same. Diag's
  HTTP/1.1 and HTTP/3 rows add a request a second each, as its HTTP/2 rows make.
* **Anything else on the server**: operating system updates, other sites behind the same
  nginx.

Set the limits below what you can afford, and keep your provider's budget alert on as a
backstop.

## Tests between servers

`/api/v1/measure/download`, `/upload` and `/full` count this server's side of the test:
what it downloads from the other server is traffic it receives. A limit refuses them as
it does a visitor's test. They count as `internal_networks` traffic when every address of
the other server's name is in `internal_networks`. The other server counts its own side.

## On disk

| | |
|---|---|
| `/var/lib/texas2step/traffic.db` (`traffic_file`) | Bytes per day for 400 days, with internal and other traffic apart, and what was allowed by hand. No addresses. Made by the service while a limit is set, shared by its workers and `texas2step traffic`; removed with `apt purge` |

A server cloned from another starts with the other's figures. Delete the file on the clone,
with its service stopped: see [Clone a server](install.md#clone-a-server).
