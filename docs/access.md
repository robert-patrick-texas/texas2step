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
sudo texas2step config set access_code=daily internal_networks=10.0.0.0/8,2001:db8::/32,198.51.100.0/24 --write
sudo systemctl restart texas2step
```

On the command line, separate the networks with commas. In `config.toml` they are a list
of quoted strings: `internal_networks = ["10.0.0.0/8", "2001:db8::/32", "198.51.100.0/24"]`.
One string of networks separated by spaces is refused.

**3. Check** that the servers agree. On each:

```bash
sudo texas2step access show
```

```text
Access codes: daily, changing at midnight, America/New_York.
Today's code: blunt.ashen.16   (until Fri 2026-10-23 00:00 EDT, in 16 h 1 min)
Yesterday's:  swore.elude.52   (still works until Fri 2026-10-23 00:00 EDT, in 16 h 1 min)
Words:        512 drawn from the built-in list by the key
Phrases:      none
Key:          /etc/texas2step/access.key
Fingerprint:  3364-dbda-c7a0   (the same on servers that give the same codes)
```

The same fingerprint on every server means the same codes. It is worked out from the
key, the words, the settings that shape a code and any
[standing phrases](#standing-phrases), and reveals none of them. It reads
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
| `access_phrases_file` | `/etc/texas2step/access-phrases.toml` | [Standing phrases](#standing-phrases), if the file exists |
| `internal_networks` | `[]` | Your own networks: no code, and the desk's tools. A list of IPv4 and IPv6 addresses and networks in CIDR form, mixed freely: `["192.168.0.0/16", "2001:db8::/32", "203.0.113.7"]`. An address alone is a single host. Bits past the prefix are ignored, so `10.2.1.30/24` means all of `10.2.1.0/24`. Give the addresses as the server sees them: staff who come in through your firewall's NAT arrive from its public address. |

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
further. Wrong codes lock a network out ([below](#lockouts-and-blocks)), and a guessed code gets nothing more than
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

## Standing phrases

The day's code suits most calls. A standing phrase is for the others: a VIP who calls
while the desk is away from a PC, or a team that needs access for a while. Each phrase
has a name, and it works as an access code beside the day's code, until you remove it or
until the end of its `until` day. Case and separators don't matter here either.

```bash
sudo texas2step access phrase add vip                            # asks for the phrase
sudo texas2step access phrase add field-team --until 2026-12-31  # works through that day
sudo texas2step access phrase list
sudo systemctl restart texas2step
```

Typing the phrase when asked keeps it out of the shell's history; you can also give it
after the name, in quotes. `list` shows each one:

```text
/etc/texas2step/access-phrases.toml:
  vip          copper lantern harbor violet
  field-team   maple orbit canyon             until 2026-12-31
```

**The file.** Phrases live in `/etc/texas2step/access-phrases.toml` (`access_phrases_file`),
readable only by root and the service, like the key, and kept out of `config.toml`,
which every user on the server can read. The package makes it when it is installed or
upgraded, if there is none, holding no phrases: only two examples, commented out.

```toml
# [[phrase]]
# name = "future"
# phrase = "2026 the future is bright"
# until = 2026-12-31                    # works through December 31, 2026
#
# [[phrase]]
# name = "vip"
# phrase = "vip ticket to ride 1776"    # no until: works until it is removed
```

You can edit the file by hand, taking the examples as a pattern. The example phrases
themselves never work: they are published here, so anyone could enter them, and
texas2step refuses them, in any case or spacing, as it would any other broken entry.

A name is up to 40 letters, digits, `-`, `_` or `.`, and a phrase is plain letters and
digits (ASCII) with any spaces or punctuation; no two may share a name or a phrase.
While access codes are on, texas2step won't start with a file that breaks these rules,
and says which phrase. `add` and `remove` write the file again, keeping the phrases and
the examples but not comments of your own. Without the file there are no phrases;
`sudo texas2step access phrase init` makes it again.

**Every server.** The phrase is checked by the server whose page the visitor opens, so
copy the file to each server, as you did the key. On each of the others:

```bash
ssh austin.example.net sudo cat /etc/texas2step/access-phrases.toml | sudo texas2step access phrase import
sudo systemctl restart texas2step
```

`import` checks the file before writing it. `sudo texas2step access show` lists the
phrases by name, and its fingerprint covers them, so servers with the same fingerprint
take the same codes and phrases.

**Every use is logged** by the phrase's name, never the phrase itself, and so is an
expired phrase, which counts as a wrong code:

```text
WARNING:  texas2step.api.access: access phrase vip used from 203.0.113.9
WARNING:  texas2step.api.access: expired access phrase field-team from 198.51.100.4
```

```bash
journalctl -u texas2step --grep 'access phrase'
```

**Choose phrases outsiders can't guess.** A phrase doesn't change, so a guessable one
stays guessable: several words that don't belong together, such as
`copper lantern harbor violet`, rather than `vip access`. texas2step takes any phrase
you choose. To withdraw one, `sudo texas2step access phrase remove vip` on every server,
and restart; passes it already bought last until they expire (`access_pass_hours`).
A new key doesn't change the phrases.

A phrase buys the same pass as the day's code, so the visitor gets what any visitor
does, and never sees the day's code: only `internal_networks` get that, at the top of
History. History shows the day's code, never the phrases.

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
| The page at `/`, which asks for the code; its scripts, styles and logo; `/api/v1/health` (for load balancers, without the version); `/api/v1/access` | Nothing |
| The Monitor and Diag pages, the tests, and saving a result | A pass, or an address in `internal_networks`. Without a pass, `/monitor` and `/diag` send the visitor to `/`. |
| History, the API tab, `/r/<id>`, reading saved results, `/api/docs`, `/api/openapi.json`, `/api/v1/measure/*`, the current code, [lockouts and blocks](#lockouts-and-blocks) | An address in `internal_networks`; others get 404 |

The pass goes with every request the page makes, as `?pass=`. A page load can't carry
that, so a right code also sets the pass as a cookie for the server's own pages:
`HttpOnly`, `Secure` over HTTPS, `SameSite=Strict`, gone when the pass expires.

Until a visitor enters a code, nothing tells them the version: the page leaves it out and
`/api/v1/health` answers only `{"status":"ok"}`. Inside `internal_networks`, and once in,
it is there as before. This slows an attacker down rather than stopping one: the page's
scripts are those of a published release.

Loopback addresses are always inside, so commands on the server itself need no code. The
service sees each visitor's address through nginx (`trusted_proxies`), which the packaged
snippets send as `X-Forwarded-For`, replacing any the visitor sent. A request that comes
through a proxy (it has `X-Forwarded-Proto`, `X-Forwarded-Prefix`, `X-Forwarded-Host` or
`Forwarded`) without the visitor's address counts as outside, not as the proxy's own
loopback address, so a hand-written nginx location that forgets
`proxy_set_header X-Forwarded-For $remote_addr;` fails closed. If anything else sits in
front of nginx and hides visitors' addresses, set nginx's
[real IP](https://nginx.org/en/docs/http/ngx_http_realip_module.html) from it, and never
list that device's addresses in `internal_networks`: every visitor would then count as
inside.

Without `internal_networks`, every visitor needs a code, and anyone with one can read the
saved results. `texas2step access show` warns about that.

## Lockouts and blocks

Wrong codes count against the visitor's network, its IPv4 /24 or IPv6 /48, not its
address alone: one host can take a new IPv6 address for every guess. After three wrong
codes in a row from a network, it is locked out:

* Every code from it is refused unread, **the right one too**, with the same answer as a
  wrong code. Nothing tells the visitor they are locked out.
* Each code sent while locked out moves the end of the lockout to 30 minutes after it. A
  network that keeps trying, even once every 10 minutes, stays locked out; one that stops
  for 30 minutes is let go.
* A network quiet for 30 minutes starts counting again from nothing, and so does one that
  enters a right code.
* Visitors from `internal_networks` and loopback need no code, so they are never counted
  or locked out. Passes already given keep working.

| Setting | Default | |
|---|---|---|
| `access_lockout_after` | `3` | Wrong codes in a row that lock a network out (1 to 100) |
| `access_lockout_minutes` | `30` | How long a lockout lasts after the last code from it (1 to 10080) |
| `access_lockouts_file` | `/var/lib/texas2step/access.db` | Where the counts and lockouts are kept |

The counts live in a small SQLite file that the service's workers and the command line
share, so each worker sees the same counts and a lockout outlasts a restart. Checking a
code and counting it happen together, so however many guesses a network sends at once,
only three are read. Each step is a warning in the journal:

```text
WARNING:  texas2step.api.access: wrong access code from 203.0.113.9
WARNING:  texas2step.api.access: 203.0.113.0/24 locked out after 3 wrong access codes in a row, until 2026-10-23 14:04 EDT
WARNING:  texas2step.api.access: access code from 203.0.113.12 ignored: 203.0.113.0/24 is locked out until 2026-10-23 14:31 EDT
```

```bash
journalctl -u texas2step --grep 'access code'
```

**From the command line.** `sudo texas2step access lockouts` lists the networks locked out
now, when each began and when it ends:

```text
Locked out (the right code is refused too, until the end, which each try moves):
  Network           Since                  Until                  Why
  203.0.113.0/24    2026-10-23 13:37 EDT   2026-10-23 14:07 EDT   3 wrong codes in a row
  198.51.100.0/24   2026-10-23 13:37 EDT   2026-10-23 15:37 EDT   blocked from the command line
1 more network with wrong codes in a row.
```

```bash
sudo texas2step access lockouts --clear 203.0.113.77   # ends the lockout that holds it
sudo texas2step access lockouts --clear all
sudo texas2step access block 198.51.100.0/24 --minutes 120
```

`--clear` lets in at once a caller whose office mistyped the code, or whose network shares
a /24 with someone who did. `block` locks out an address or network by hand, as if it had
entered too many wrong codes, for `--minutes` (`access_lockout_minutes` if left out), and
longer if it keeps trying; blocking it again keeps it going. A block can't overlap
`internal_networks` or loopback, or be wider than a /16 of IPv4 or a /32 of IPv6. Both
take effect at once, with no restart, and with
[servers that defend together](#servers-that-defend-together), on every server.

**From automation.** The same, as an API that answers only `internal_networks` and the
server itself, so a script can carry a block from the server that saw an attack to all
the others, and renew it there:

```bash
curl -s https://dallas.example.net/texas2step/api/v1/access/blocks                # list
curl -s -X POST https://houston.example.net/texas2step/api/v1/access/blocks \
     -H 'Content-Type: application/json' -d '{"network": "198.51.100.0/24", "minutes": 120}'
curl -s -X DELETE 'https://houston.example.net/texas2step/api/v1/access/blocks?network=198.51.100.0/24'
```

A list gives each lockout's `network`, `source`, `wrong_codes`, `since` and `until`, and
`counting`, the networks with wrong codes in a row. A block that overlaps
`internal_networks`, or is wider than a /16 or a /32, is refused with `400`. These calls
change only the server they are sent to; for one that reaches every server by itself, see
[Servers that defend together](#servers-that-defend-together).

**How strong is a code, then?** A daily code always has two codes that work (yesterday's
is still good), so an attacker's chance in a day is about twice their guesses over the
codes there are. With lockouts, each network an attacker controls gets three guesses
every 30 minutes. One with 10,000 networks gets about 1.4 million a day:

| Code | Codes there are | Chance in a day, 10,000 networks |
|---|---|---|
| Built-in words, `access_digits = 2` | 26 million | about 11% |
| Built-in words, `access_digits = 3` | 262 million | about 1% |
| Built-in words, `access_digits = 4` | 2.6 billion | about 0.1% |
| A site list of 100 entries, 2 digits | 10,000 | certain |

For a server open to the Internet, use `access_digits = 4`, and standing phrases of
several unrelated words.

## Servers that defend together

When a lockout begins on one server, the others can block that network too, so an
attacker who trips the limit on one can't carry on at the next. On each server:

```toml
access_share_lockouts = true
# The addresses the other servers' requests come from.
access_peer_networks = ["198.51.100.20", "2001:db8:20::20", "203.0.113.30"]
```

* **What goes to the others.** A lockout here goes, as a block, to every server in
  `[[locations]]`, at once. While the attacker keeps trying here, it goes again every
  third of `access_lockout_minutes`, so the others' blocks don't run out first.
  `sudo texas2step access block` and `access lockouts --clear` go to every server too:
  `--clear` on any one of them lets a caller back in everywhere. The
  [API's blocks](#lockouts-and-blocks) stay on the server they are sent to.
* **What a server takes.** Only requests from an address in its `access_peer_networks`,
  signed with a key the servers work out from the access key they share. Use the
  addresses the other servers' requests leave from, which behind NAT may not be the
  addresses of their names. A request more than 5 minutes off the server's clock, sent
  before, or meant for another server, is refused, so keep the servers' clocks right
  (Ubuntu's `systemd-timesyncd` does). Unlike `internal_networks`, `access_peer_networks`
  grants nothing else: no tests without a code, no History.
* **What one server can make another do,** in case one is ever misconfigured or
  compromised: a block from another server covers at most a /24 or a /48, never
  `internal_networks` or loopback, lasts at most a week, and isn't passed on. A server
  holds at most 1,000 of them. A wider block by hand stays on the server where it was set.
* **Counts stay on each server.** Wrong codes count where they are entered, so with three
  servers an attacker gets up to about seven guesses, two at each and one more, before
  the whole cluster locks them out.

Check that each server takes this one's lockouts, with a signed request that changes
nothing:

```console
$ sudo texas2step access peers
Sharing lockouts: on.
Takes them from: 198.51.100.20, 2001:db8:20::20, 203.0.113.30.
A signed check, which changes nothing, to each server in [[locations]]:
  dallas    takes them from this server
  houston   404: sharing is off there, or this server's address isn't in its access_peer_networks
```

A server whose access key differs answers that the signature doesn't match. The journal
of each side says what it sent and what it took:

```text
WARNING:  texas2step.cluster: block of 203.0.113.0/24 sent to dallas, houston
WARNING:  texas2step.api.access: 203.0.113.0/24 blocked until 2026-10-23 19:18 EDT, shared by austin from 198.51.100.10
```

The signature is HMAC-SHA-256 with a 256-bit key, over the time, a one-time value and the
request. Quantum computers break public-key cryptography, not keyed hashes like this, so
it holds against them too. For the connection itself, see
[post-quantum key exchange](install.md#security-notes) in nginx.

### A hook for anything else

`access_lockout_hook` names a program that runs on each lockout, block and clear on this
server, blocks from the others included, such as to send an alert or call a firewall's or
a SIEM's API. It gets three arguments: the event (`lockout`, `block` or `clear`), the
network, and when it ends (ISO 8601; empty for a clear). The environment has the same in
`TEXAS2STEP_EVENT`, `TEXAS2STEP_NETWORK` and `TEXAS2STEP_UNTIL`, with `TEXAS2STEP_WHY` and
`TEXAS2STEP_SERVER`. For example, `/usr/local/bin/texas2step-hook`:

```sh
#!/bin/sh
curl -s -m 10 -X POST https://siem.example.net/api/events \
     -H 'Content-Type: application/json' \
     -d "{\"source\": \"texas2step $TEXAS2STEP_SERVER\", \"event\": \"$1\", \"network\": \"$2\"}"
```

```toml
access_lockout_hook = "/usr/local/bin/texas2step-hook"
```

It runs as the service's user, even when `sudo texas2step access block` sets it off,
inside the service's sandbox: it can reach the network and write only under
`/var/lib/texas2step`, and it can't use `sudo`. It has 30 seconds; a failure is a warning
in the journal. Keep it owned by root and writable by nobody else.

## Other limits

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
([Pinning the UDP ports](install.md#pinning-the-udp-ports)). Nothing listens on those UDP
ports between tests: a test's port opens when its pass-holding browser asks for one,
takes only packets carrying that test's own credentials, and closes when the test ends.

Requests that need no code are kept small: a code (`/api/v1/access`) takes up to 1 KB,
a saved result 2 MB, and the other requests 64 KB, refused with `413` before they are
read. Only the upload test takes any size, and it needs a pass.

Where the server's traffic is billed, a [traffic limit](traffic.md) caps what the tests
move in a day or a month.

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
| `/etc/texas2step/access-phrases.toml` | Made when the package is installed or upgraded, if missing, with two examples commented out; kept on upgrades; removed with `apt purge` |
| Your `access_words` file | Yours: kept by `apt purge` |
| `/var/lib/texas2step/access.db` | Wrong codes, lockouts and blocks, and with servers that defend together, the one-time values of their requests; made by the service when access codes are on; removed with `apt purge` |

The pass lives in the visitor's browser (local storage, and the cookie for this server's
pages) until it expires. Passes are not written to the journal: the request log shows
`pass=*` in their place. nginx's access log doesn't have them either: the packaged
snippets log texas2step's requests to `/var/log/nginx/access.log` without their query
strings (the `texas2step` log format, in `/etc/nginx/conf.d/texas2step-log.conf`).
