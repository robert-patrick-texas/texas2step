# Access codes

For texas2step servers open to the Internet, so employees can test from a remote office
or from home when they call the service desk about a slow connection. The desk reads them
a short code, such as `maple.river.42`, which changes every day, week or month. There are
no user names or passwords. The desk's own tools stay on your networks: saved results
(with callers' home addresses), the API explorer and the server-side measurements.

## How it fits together

* **One address to give out.** Callers open one server's page, such as
  `https://speedtest.example.net/texas2step/`. Its server picker lists your other
  Internet-facing servers (an east and a west gateway, cloud regions), each set up as in
  [Several locations](../README.md#several-locations). The browser tests the chosen server
  directly, but saves every result on the server whose page is open, so the desk finds
  all of them in that server's History.
* **One code for every server.** All of an organization's servers hold the same key. Each
  works out the same code from it, with nothing shared between them while they run.
* **A code buys a pass.** The visitor enters the code once. The server answers with a
  pass, good for 12 hours (`access_pass_hours`), which the browser then sends with every
  request, to every server in the picker. A server with the same key accepts it.
* **The desk's tools stay inside.** Visitors from addresses in `internal_networks` need no
  code, and only they get History, the API tab, saved results and the code itself.
  Everyone else gets the Test, Monitor and Diag tabs.

Put each server on its own public address, or behind a layer-4 load balancer (such as
Azure Load Balancer or AWS Network Load Balancer). A CDN or an application gateway (such
as Azure Front Door or Amazon CloudFront) ends the visitor's connection at its edge, so
tests would measure the path to the edge rather than to your network, and the UDP test
can't pass through it.

## Turn it on

**1. The key.** Each install makes its own random key, `/etc/texas2step/access.key`,
readable only by root and the service. The words and codes come from it, so no two
installs start with the same ones. Choose one server's key for the organization, and copy
it to each of the others:

```bash
ssh austin.example.net sudo texas2step access key | sudo texas2step access key --import
```

The first command prints the key on `austin`; the second, run on the server receiving it,
replaces that server's key. Keep the key as you would a password: anyone who has it can
work out every code.

**2. The settings,** the same on every server: how often the code changes, and your own
networks, as addresses or CIDR blocks. Include the corporate addresses the desk's
browsers reach the servers from.

```bash
sudo texas2step config set access_code=daily internal_networks=10.0.0.0/8,198.51.100.0/24 --write
sudo systemctl restart texas2step
```

**3. Check** that the servers agree. On each:

```bash
sudo texas2step access show
```

```text
Access codes: daily, changing at midnight, America/New_York.
Today's code: blunt.ashen.16   (until Fri 2026-10-23 00:00 EDT, in 16 h 1 min)
Yesterday's:  swore.elude.52   (still works until Fri 2026-10-23 00:00 EDT, in 16 h 1 min)
Words:        512 drawn from the built-in list by the key
Key:          /etc/texas2step/access.key
Fingerprint:  3364-dbda-c7a0   (the same on servers that give the same codes)
```

The same fingerprint on every server means the same codes. It is worked out from the
key, the words and the settings that shape a code, and reveals none of them. It reads
the key, so it needs `sudo`.

To turn codes off again: `sudo texas2step config set access_code=off --write`, then restart.

## The code

| Setting | Default | |
|---|---|---|
| `access_code` | `off` | `daily`, `weekly` (from Monday), `monthly` (from the 1st), or `fixed` |
| `access_timezone` | `America/New_York` | The time zone whose midnight starts a new code; the same on every server |
| `access_words` | `""` | Your own word list; empty uses the built-in words |
| `access_digits` | `2` | The digits that end a code: 2, 3 or 4 |
| `access_pass_hours` | `12` | How long an entered code lets a browser keep testing |
| `access_key_file` | `/etc/texas2step/access.key` | The key |
| `internal_networks` | `[]` | Your own networks: no code, and the desk's tools |

**Built-in words.** A code is two words and the digits: `maple.river.42`. The words come
from the EFF's short word list of easily spelled words (credited in the package's
copyright file), less a few that are easy to mishear over the phone. The key picks the
organization's 512 of them. `sudo texas2step access words` lists them.

**Your own words.** Set `access_words` to a text file with one entry per line: a word,
or a few words, such as `austin` or `fort bragg`. A code is then one entry and the
digits: `austin.42`, `fort.bragg.68`. Lines starting with `#` are comments. The file
needs at least 100 entries, in plain letters and digits (ASCII), each up to 40 characters
and none twice; texas2step won't start with a list that breaks these rules, and says
which line. Spaces and dashes in an entry become dots. Copy the file to every server.

A list of a few hundred names gives far fewer codes than the built-in words: 100 entries
and two digits make 10,000. Names that outsiders could guess, such as cities, shrink that
further. Wrong codes are limited (see below), and a guessed code gets nothing more than
tests until it changes, but for a short list, or a `fixed` code, consider
`access_digits = 3` or `4`.

**When it changes.** A new code starts at midnight in `access_timezone`. The previous
code keeps working for 24 hours after the change, for a call that spans midnight. A
`fixed` code changes only with a new key:

```bash
sudo texas2step access key --new
```

That makes a new key at once: every code and pass from the old one stops working. Copy
the new key to the other servers as in step 1, and restart the service on each. Without
`--yes`, it asks first.

**Typing it.** Case and separators don't matter: `Maple River 42`, `maple-river-42` and
`maple.river.42` are the same code, and `7` counts for `07`.

## For the service desk

From your networks, the History tab shows the code and a box for a result ID:

* Give the caller the server's address and the current code. They enter the code once,
  then run the test.
* The test is saved as it finishes. The caller reads out its ID, such as `k7q2-m9xd`; the
  IDs leave out letters and digits that are easy to confuse.
* Type the ID into History's **Result ID** box (case and spaces don't matter) to open
  their result: speeds, latency under load, packet loss, and the address they tested from.

The same code: `sudo texas2step access show` on any server.

From the command line, the code goes with `--code`, or in `TEXAS2STEP_CODE`:

```bash
texas2step run https://speedtest.example.net/texas2step --code maple.river.42
```

## What needs what

| | Needs |
|---|---|
| The page itself, `/api/v1/health` (for load balancers), `/api/v1/access` | Nothing |
| The tests: Test, Monitor and Diag, and saving a result | A pass, or an address in `internal_networks` |
| History, the API tab, `/r/<id>`, reading saved results, `/api/docs`, `/api/openapi.json`, `/api/v1/measure/*`, the current code | An address in `internal_networks`; others get 404 |

Loopback addresses are always inside, so commands on the server itself need no code. The
service sees each visitor's address through nginx (`trusted_proxies`). If anything else
sits in front of nginx and hides visitors' addresses, set nginx's
[real IP](https://nginx.org/en/docs/http/ngx_http_realip_module.html) from it, and never
list that device's addresses in `internal_networks`: every visitor would then count as
inside.

Without `internal_networks`, every visitor needs a code, and anyone with one can read the
saved results. `texas2step access show` warns about that.

## Wrong codes and other limits

Each address gets 10 wrong codes in 10 minutes; then the server answers `429` for a
while. Each wrong code is a warning in the journal:

```text
WARNING:  texas2step.api.access: wrong access code from 203.0.113.9
```

```bash
journalctl -u texas2step --grep 'wrong access code'
```

A tool such as fail2ban can block addresses that keep trying. The counts are kept per
worker process, in memory.

A test moves as much data as the path allows, through the same Internet link your users
share. nginx can cap the connections each visitor, and each server, may hold. In a file
such as `/etc/nginx/conf.d/texas2step-limits.conf`:

```nginx
limit_conn_zone $binary_remote_addr zone=t2s_client:10m;
limit_conn_zone $server_name zone=t2s_server:1m;
```

and in the site's `server { }` block, beside the `include` line:

```nginx
limit_conn t2s_client 16;    # a test uses up to 8 streams, plus its probes
limit_conn t2s_server 200;
```

Over HTTP/2, each request counts as a connection. Then `sudo nginx -t && sudo systemctl
reload nginx`.

Open only what the tests need: TCP 443 (and 80, to redirect), and the UDP probe's ports
([Pinning the UDP ports](install.md#pinning-the-udp-ports)).

## Privacy

A saved result holds the public address the caller tested from, often their home's,
and, where the browser or the command line can tell, their computer's own address and a
MAC address. Keep `internal_networks` set, so only the desk reads them, and keep only as
many as you need (`results_max_rows`). `sudo texas2step results clear` deletes them all
([Clear saved results](install.md#clear-saved-results)).

## On disk

| | |
|---|---|
| `/etc/texas2step/access.key` | Made when the package is installed, kept on upgrades, removed with `apt purge` |
| Your `access_words` file | Yours: kept by `apt purge` |

The pass lives in the visitor's browser (local storage) until it expires. Passes are not
written to the journal: the request log shows `pass=*` in their place.
