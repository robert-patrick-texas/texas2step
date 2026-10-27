# Install

This guide is for operators. It covers installing texas2step from the Debian package on
an **Ubuntu 26.04 LTS** server that already serves a website with **nginx**, and
publishing it at a path on that site:

* `https://www.example.net/texas2step/`
* optionally `https://www.example.net/speedtest`, which redirects to the address above.

Replace `www.example.net` with your site throughout. To build the package yourself or
work on the code, see [Development](development.md).

## What you need

* Ubuntu 26.04 LTS on amd64, with `sudo`.
* nginx serving your site over HTTPS. The package doesn't touch your certificates or
  your site's existing configuration.
* Inbound UDP to the server for the packet-loss test. See
  [Firewall and UDP](#4-firewall-and-udp).

## Install or upgrade with the setup tool

Each release comes with `texas2step-setup` next to the `.deb`. It checks the server,
tells you what it will change and what you must fix first, and writes an install script
for that server. It does a fresh install, a reinstall or an upgrade, whichever applies.
To do the same by hand, follow steps 1 to 5 below instead.

1. Copy both files to the server, into the same directory:

   ```bash
   scp texas2step-setup texas2step-*-amd64.deb admin@houston.example.net:
   ```

2. Run the check. It changes nothing on the system; it needs `sudo` only to read nginx's
   configuration, the firewall and the package database.

   ```bash
   sudo ./texas2step-setup --id houston --name "houston.example.net" --location "Houston, TX" \
       --peer https://www.example.net/texas2step
   ```

   It prints a report and saves it as `texas2step-preflight.txt`, in four sections:
   **OK**, **Will change**, **You must fix** and **Note**. If anything is under
   **You must fix**, fix it and run the check again. Until then it writes no install
   script.

3. Read `texas2step-install.sh`, then run it:

   ```bash
   sudo ./texas2step-install.sh
   ```

   It logs to `texas2step-install.log` beside itself. When it finishes, the service
   answers both on `127.0.0.1:8080` (or the `--port` you gave) and through nginx, and it
   prints the address to open.
   You can run it again safely: it skips each step that is already done.

What the check looks at, and what the script then does:

| Area | Check | Install script |
|---|---|---|
| System | Ubuntu 26.04 LTS, the package's architecture, systemd, disk space | |
| Package | The `.deb` is the one this tool was built with (SHA-256), and its dependencies are installed or available from apt | Verifies the checksum again, then installs with apt, keeping your settings file |
| Version | Installed version: none, the same, older or newer | Fresh install, reinstall or upgrade. It refuses to downgrade unless you pass `--allow-downgrade` to the check. |
| Settings | On an upgrade, whether your edited settings need the new template. If they do, the report shows the merge's changes. | Runs `texas2step config merge --write`, keeping a dated backup |
| Identity | `--id`, `--name`, `--location` against the current settings | `texas2step config set ... --write` |
| Peers | Each `--peer URL` is asked who it is (`/api/v1/info`). A peer that can't be reached is left out, with the command to add it later. | `texas2step config add-location ... --write` |
| nginx | nginx is installed and running, its configuration passes `nginx -t`, and it loads `conf.d/*.conf`. It finds your HTTPS site, and checks that nothing else on that site uses `/texas2step/`. If texas2step is already published, nginx is left alone. | Adds `include snippets/texas2step.conf;` after the site's `server_name` line, runs `nginx -t` and reloads. The file is backed up to `/var/backups/texas2step/` first, and put back if the test fails. |
| UDP | The probe's port range: the one you set, or 61000–61999. It checks this range against the firewall and the kernel's ephemeral ports. | Pins the range in the settings. Opens it in ufw or firewalld if one of them is active. |
| Port | Nothing else listens on texas2step's TCP port: 8080, `LISTEN_PORT` in `/etc/default/texas2step`, or `--port`. nginx's `upstream` in `/etc/nginx/conf.d/texas2step.conf` points at that port. | With `--port`, sets `LISTEN_PORT` and the `upstream` before the package is installed, so the service never starts on 8080. An `upstream` that points elsewhere is pointed at the port. |

Options:

| Option | Meaning |
|---|---|
| `--id`, `--name`, `--location` | This server's identity: `server_id`, `server_name`, `server_location`. Give every server its own `--id`. |
| `--peer URL` | Another texas2step server to offer in the location picker and in two-destination monitoring. Repeat it for each server. |
| `--site FILE` | The nginx site to publish on, when there are several HTTPS sites. Use `FILE:LINE` when the file holds several `server` blocks. Naming a site publishes texas2step there even if another site already does. |
| `--speedtest` | Also add the `/speedtest` redirect |
| `--path PATH` | Publish texas2step at `PATH` instead of `/texas2step/`, e.g. `/speedtest`. The check moves the package's snippet to that path and shows it in the install script, which writes it as `snippets/texas2step-at-speedtest.conf` and includes that. Each later run writes it again from the new package. Use the same path in `--url-v4` and `--url-v6`. |
| `--no-nginx` | Install the service only, and publish it yourself |
| `--udp-ports MIN-MAX` | The probe's UDP range, e.g. `61000-61999` |
| `--port PORT` | The TCP port texas2step listens on, on `127.0.0.1` behind nginx, when another program needs 8080. Choose one from 1024 to 32767. Later runs and `texas2step update` keep it. See [Another port](#another-port). |
| `--url-v4 URL`, `--url-v6 URL` | This server's IPv4-only and IPv6-only base URLs, for the header's IP drop-down; see [IPv4 and IPv6](configuration.md#ipv4-and-ipv6). The check looks up each name: it should have only an A record (or only AAAA), pointing at this server. After the install, the script checks that nginx answers for each name. |
| `--no-firewall` | Leave the firewall alone; the report lists the rule to add |
| `--allow-downgrade` | Allow installing an older version than the one installed |
| `--deb FILE` | A package other than the one next to the tool. Its checksum must still match. |
| `--out DIR` | Where to write the report and the script (default: the current directory) |

**Firewalls.** The script adds the UDP rule only to ufw and firewalld, which keep their
rules across reboots on their own. For iptables or a hand-written nftables ruleset, the
report gives the exact rule and where to save it, and you add it yourself. The script
never runs `netfilter-persistent save`: on a host that also runs Docker, that would
save Docker's own rules as well. Nothing on the server can see a cloud security group
or a network firewall in front of it, so open the range there as well.

### Two servers

To offer two servers to each other, for example for two-destination monitoring, install
the second server with `--peer` pointing at the first:

```bash
sudo ./texas2step-setup --id houston --name houston.example.net --location "Houston, TX" \
    --url-v4 https://v4.houston.example.net/texas2step \
    --url-v6 https://v6.houston.example.net/texas2step \
    --peer https://dallas.example.net/texas2step

# or published at /speedtest/ instead:
sudo ./texas2step-setup --path /speedtest --id houston --name houston.example.net \
    --location "Houston, TX" --url-v4 https://v4.houston.example.net/speedtest \
    --url-v6 https://v6.houston.example.net/speedtest \
    --peer https://dallas.example.net/texas2step
```

Then add the second server to the first, on the first server:

```bash
sudo texas2step config add-location https://houston.example.net/texas2step          # preview
sudo texas2step config add-location https://houston.example.net/texas2step --write
sudo systemctl restart texas2step
```

`--peer` and `add-location` ask the other server for its id, name and location, and
for its IPv4-only and IPv6-only URLs if it has them, so the IP drop-down works for it
too. They use the system's trusted certificates, so a certificate from your own CA
works once the CA is installed with `update-ca-certificates`. If the server can't be
reached yet, give `--id`, `--name` and `--location` (and `--url-v4`, `--url-v6`)
yourself.

To make the second server by cloning the first's virtual machine instead, see
[Clone a server](#clone-a-server).

### Another port

texas2step listens on `127.0.0.1:8080`, and nginx reaches it there through the
`upstream` in `/etc/nginx/conf.d/texas2step.conf`. When another program needs 8080,
give the setup tool another port, the same way for a new install or an installed server:

```bash
sudo ./texas2step-setup --port 8081
sudo ./texas2step-install.sh
```

The install script sets `LISTEN_PORT=8081` in `/etc/default/texas2step` and points the
`upstream` at `127.0.0.1:8081`, keeping a dated copy of each file it changes in
`/var/backups/texas2step/`. It does this before installing the package, so the service
never starts on 8080, even on a new install. Then it restarts texas2step, reloads nginx
and checks both.

Choose a port from 1024 to 32767. texas2step runs as its own user, which can't listen
below 1024, and Linux takes the ports of outgoing connections from 32768 up. If the port
is in use, the check says by what. Both files are kept on upgrades, and the setup tool
and `texas2step update` read the port from `LISTEN_PORT`. When a new version changes the
`upstream` file, the update installs the new one with your port in it.

If you changed `LISTEN_PORT` by hand and not the `upstream`, the next run of the setup
tool points the `upstream` at it.

## 1. Install the package

Download the newest `.deb` from the
[releases page](https://github.com/robert-patrick-texas/texas2step/releases) and install
it with apt, which also pulls in its few dependencies. From the command line:

```bash
cd "$(mktemp -d)"
curl -sL https://api.github.com/repos/robert-patrick-texas/texas2step/releases/latest \
    | grep -o 'https://[^"]*-amd64\.deb' | xargs curl -sLO
sudo apt install ./texas2step-*-amd64.deb
```

The service starts straight away, listening on `127.0.0.1:8080`, so it's reachable only
through nginx. Check it:

```bash
systemctl status texas2step
curl -s http://127.0.0.1:8080/api/v1/health      # {"status":"ok","version":"…"}
```

What the package installs:

| Path | Purpose |
|---|---|
| `/opt/texas2step/venv` | The application and every Python library it needs, self-contained. It runs on the system's Python 3.14. |
| `/usr/bin/texas2step` | The command-line tool (`texas2step --help`) |
| `texas2step.service` | systemd service, enabled at boot, running as the `texas2step` system user |
| `/etc/texas2step/config.toml` | Settings; see [Configuration](configuration.md) |
| `/etc/default/texas2step` | Listen address and number of worker processes |
| `/etc/nginx/conf.d/texas2step.conf` | nginx upstream definition, which does nothing until a site includes the snippet |
| `/etc/nginx/conf.d/texas2step-log.conf` | The snippets' log format: nginx's usual one, without query strings, which can hold an access pass |
| `/etc/nginx/snippets/texas2step.conf` | The `/texas2step/` location for your site |
| `/etc/nginx/snippets/texas2step-speedtest.conf` | Optional `/speedtest` redirect |
| `/var/lib/texas2step/` | Saved results (SQLite); [`texas2step results clear`](#clear-saved-results) deletes them. With access codes on, also the [lockouts](access.md#lockouts-and-blocks) (`access.db`); with a traffic limit, the [traffic figures](traffic.md) (`traffic.db`) |
| `/run/texas2step/` | The [active clients](clients.md) (`clients.db`), in memory: kept over a restart, gone at a reboot |

Files under `/etc` are conffiles: upgrades keep your edits.

## 2. Publish it at /texas2step/

Open your site's configuration, for example
`/etc/nginx/sites-available/www.example.net`. Add the include lines inside the
`server { }` block that serves HTTPS:

```nginx
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name www.example.net;
    # ... your certificates, root, other locations ...

    include snippets/texas2step.conf;             # https://www.example.net/texas2step/
    include snippets/texas2step-speedtest.conf;   # optional: /speedtest redirects there
}
```

Test the configuration, reload nginx, and open `https://www.example.net/texas2step/`:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

The snippet does the following:

* Proxies `/texas2step/` to the service.
  * It turns buffering and compression off, so measurement traffic streams straight
    through.
  * It passes WebSocket upgrades through.
  * It tells texas2step its public path (`X-Forwarded-Prefix`), so pages, links, shared
    results and the API explorer all stay under `/texas2step/`.
* Uses `location ^~`, so regex rules elsewhere in your site, such as static-file
  caching for `\.(js|css)$`, can't capture texas2step's URLs.
* Sends `X-Forwarded-For $remote_addr`, so a client can't spoof the address shown in
  its results.
* Tells texas2step about each connection for the Diag tab: the client's port, the
  server address and port it reached, TLS version and cipher, and the kernel's TCP
  round-trip time (`/api/v1/diag/http` in [API](api.md#discovery)). The setup tool
  writes a snippet it generated again from the new package. A copy you made by hand
  needs these lines added; without them, Diag shows those fields as unknown.
* Leaves that request out of nginx's access log: an open Diag tab sends it once a
  second. Every other request is logged as usual.

If your site already uses `/speedtest`, leave out the second include. To give
texas2step its own hostname instead, such as `speed.example.net`, include
`snippets/texas2step.conf` in that site's server block and add
`location = / { return 302 /texas2step/; }`.

### Another path, such as /speedtest/

To serve texas2step at `https://www.example.net/speedtest/` itself rather than
redirecting there, give the setup tool `--path /speedtest`. By hand, make a copy of the
snippet with the path changed and include that instead of `snippets/texas2step.conf`:

```bash
sed 's# /texas2step# /speedtest#g' /etc/nginx/snippets/texas2step.conf \
    | sudo tee /etc/nginx/snippets/texas2step-at-speedtest.conf >/dev/null
```

Matching on the space changes the paths (the `location` lines, the redirect and
`X-Forwarded-Prefix`), but not `proxy_pass http://texas2step/`, where `texas2step` is
the upstream's name. A copy made by hand doesn't follow the package, so after an upgrade
compare it with the package's snippet. The setup tool leaves a copy it didn't write
alone.

### HTTP/2 and parallel streams

Over HTTP/2, every parallel stream of a throughput test shares **one** TCP connection.
That is realistic for modern web traffic, but it doesn't show how several TCP flows
behave. To make "4 streams" mean four TCP connections, publish texas2step in a server
block without `http2 on` (a separate hostname or port).

### Names for each HTTP version

Diag tests HTTP/1.1, HTTP/2 and HTTP/3 each on a name of its own
([HTTP versions](configuration.md#http-versions)). The IPv4-only and IPv6-only names
are its HTTP/2 rows. For HTTP/1.1 and HTTP/3, add two more names in each family you
want tested, each with one A record or one AAAA record. Over HTTP/2 and HTTP/3,
browsers share a connection between names that resolve to the same address when the
certificate covers both, so keep the names apart in one of two ways:

* **An address for each name.** Simplest where the server has addresses to spare, as
  with IPv6. The site's certificate can cover every name.
* **A certificate for each version.** On one address, give each version's names a
  certificate that covers only them, and keep the names outside the site's wildcard:
  `h3-6.houston.example.net` isn't covered by `*.example.net`.

With an address for each, here `2001:db8::2` for HTTP/1.1 and `2001:db8::3` for
HTTP/3, beside the site's `server` block:

```nginx
server {                                     # HTTP/1.1 only
    listen [2001:db8::2]:443 ssl;
    http2 off;
    server_name h1-6.houston.example.net;
    # the site's ssl_certificate and ssl_certificate_key lines
    include snippets/texas2step.conf;
}

server {                                     # HTTP/3, found over TCP first
    listen [2001:db8::3]:443 ssl;
    listen [2001:db8::3]:443 quic;
    http2 on;
    server_name h3-6.houston.example.net;
    # the site's ssl_certificate and ssl_certificate_key lines
    add_header Alt-Svc 'h3=":443"; ma=86400' always;
    include snippets/texas2step.conf;
}
```

* A `listen` on an address takes every connection to that address, so each block
  includes texas2step itself. Use the include line from the site's block, such as
  `snippets/texas2step-at-speedtest.conf` for `/speedtest/`. Copy any `add_header`
  lines you want from the site's block too, such as HSTS: they don't carry over to
  another `server` block.
* `always` puts `Alt-Svc` on every answer, including a 401 to a visitor who needs the
  access code. Send it only from the HTTP/3 name: from the site's own block, browsers
  would move all of texas2step there to HTTP/3, and Diag's HTTP/2 rows with it.
* Allow TCP 443 to both addresses in the firewall, and UDP 443 to the HTTP/3 address.
* Linux may start sending the server's own outgoing traffic from a new address, such as
  updates and the access peers' messages. To keep them on the main address, mark the
  new ones not to be chosen as a source: `lifetime: 0` on each in netplan, or
  `preferred_lft 0` with `ip address`. They still take connections as before.

With a certificate for each version, the same blocks listen on the site's address
(`listen 443 ssl;` and `listen [::]:443 ssl;`, plus `quic` for HTTP/3), each with its own
certificate lines.

Check each name, then set the URLs and restart:

```bash
curl -s -D - -o /dev/null https://h1-6.houston.example.net/texas2step/api/v1/health   # HTTP/1.1 200
curl -s -D - -o /dev/null https://h3-6.houston.example.net/texas2step/api/v1/health   # alt-svc: h3=":443"
sudo texas2step config set \
    server_url_v6_http1=https://h1-6.houston.example.net/texas2step \
    server_url_v6_http3=https://h3-6.houston.example.net/texas2step --write
sudo systemctl restart texas2step
```

Diag then shows each row's version, and says so when a name answers over another one.
The setup tool and `texas2step update` still report and check the site's own name: of
several blocks that include texas2step, they pass over those named in a `server_url_`
setting, then take the one that listens on every address, then nginx's `default_server`.

## 3. Configure

Edit `/etc/texas2step/config.toml`, then run `sudo systemctl restart texas2step`. The
file lists every setting with its default. The ones most sites set:

```toml
server_id = "dallas"              # short id stored with results
server_name = "dallas.example.net"       # shown in the header and in results
server_location = "Dallas, TX"
```

All settings are described in [Configuration](configuration.md).

**Branding and themes.** You can rename the site, show your logo and organization, add
links to your service desk or support group, put a notice at the top of every page, and
choose the default theme (Light, Dark, America, Energy, Federal, Military or Texas). See
[Branding and themes](configuration.md#branding-and-themes).

**Several locations.** Install texas2step at each site, give each a distinct
`server_id`, `server_name` and `server_location`, and list the others under
`[[locations]]`. `sudo texas2step config add-location <url> --write` adds one for you (see
[Two servers](#two-servers)). Users then pick a site in the header, and the browser tests
that site directly. Sites can also measure each other with `POST /api/v1/measure/full`,
but only toward listed locations.

**Listen address and workers.** These are set in `/etc/default/texas2step`. To move the
service to another port, see [Another port](#another-port). More workers help on
multi-gigabit links, where Python's per-connection CPU cost is the limit. Each worker
keeps its own UDP sessions, which is fine, because a session lives entirely in the
worker that answered it.

## 4. Firewall and UDP

* TCP 443 for the page, the API and the WebSocket. nginx already has this.
* **UDP from clients to the server** for the packet-loss test and the live monitor.
  WebRTC traffic doesn't pass through nginx: the browser sends UDP straight to the
  server's address. If UDP is blocked, the page falls back to TCP and says so.
* Behind NAT (cloud instances), list a STUN server in `ice_servers` so the server can
  advertise its public address.

### Pinning the UDP ports

By default the OS picks a random high port for each probe session. To allow just a
fixed range through the firewall, set both ends of the range in
`/etc/texas2step/config.toml` and restart the service:

```toml
rtc_port_min = 61000
rtc_port_max = 61999
```

Then allow that range inbound:

```bash
# ufw (Ubuntu's default front end)
sudo ufw allow 61000:61999/udp
# iptables / ip6tables
sudo iptables  -A INPUT -p udp --dport 61000:61999 -j ACCEPT
sudo ip6tables -A INPUT -p udp --dport 61000:61999 -j ACCEPT
# nftables
sudo nft add rule inet filter input udp dport 61000-61999 accept
# firewalld
sudo firewall-cmd --permanent --add-port=61000-61999/udp && sudo firewall-cmd --reload
```

To accept probes only from your own networks, add `from <network>` (ufw),
`-s <network>` (iptables) or `ip saddr` (nftables).

* **Sizing.** Each session takes one port on each of the server's addresses, and each
  address has the whole range to itself. So the range needs one port per concurrent
  session: at least `WORKERS × max_rtc_sessions`, which is 2 × 32 = 64 by default. When
  the range is full, `/rtc/offer` returns 503, and the browser falls back to TCP and
  gives the reason.
* **Pick a range outside the kernel's ephemeral range**
  (`/proc/sys/net/ipv4/ip_local_port_range`, 32768–60999 by default). A range inside
  it still works, but ordinary outbound connections can take ports from it.
* **Addresses.** The server offers every address on the host except loopback and IPv6
  link-local, including container bridges. Clients use whichever one they can reach, so
  the rule only has to cover the addresses your clients actually use.
* **STUN/TURN.** Server-reflexive (STUN) candidates use the same pinned sockets. A TURN
  relay socket only connects outbound to the TURN server.
* **The measuring side needs no inbound rule.** Browsers, the CLI, and servers running
  `/api/v1/measure/loss` use OS-chosen ports. They need only outbound UDP and its
  replies, which any stateful ruleset already allows.
* **UDP 5353** may appear in `ss` output. It is the mDNS resolver for browsers'
  `.local` addresses, used only on the same LAN, and needs no firewall rule.

To see the ports in use during a test, run `sudo ss -uanp | grep texas2step`.

## 5. Check it works

* Open `https://www.example.net/texas2step/` and press **Start**. The packet-loss
  tile should end in **UDP**. If it says **TCP**, UDP isn't getting through; see
  [Firewall and UDP](#4-firewall-and-udp).
* From another machine that has texas2step installed:
  `texas2step run https://www.example.net/texas2step`.
* Logs: `journalctl -u texas2step -f`. They stay quiet unless something fails; see
  [Logging](configuration.md#logging) for the `debug` setting.

## Upgrade

### With texas2step update

If `texas2step --help` lists the `update` command, it upgrades the server in two steps:
check whether a newer release is out, then install it.

```bash
texas2step update --check   # only looks: no sudo, no downloads, no changes
sudo texas2step update      # downloads, shows the plan, asks, installs
```

**Check.** `--check` asks GitHub for the newest release and compares it with the
installed version. It downloads nothing and changes nothing, so any user can run it:

```text
$ texas2step update --check
texas2step <installed> is installed; <newest> is available (<date>).
  Release notes: https://github.com/robert-patrick-texas/texas2step/releases/tag/v<newest>
To install it: sudo texas2step update
```

When there is nothing newer, it says `texas2step <installed> is installed: up to date.`
Its exit status tells scripts, cron jobs and monitoring checks which it was:

| Exit status | Meaning |
|---|---|
| 0 | Up to date, or the installed version is newer than the newest release |
| 100 | A newer release is out |
| 1 | It couldn't tell, for example GitHub couldn't be reached; the message says why |

**Install.** `sudo texas2step update` takes the steps of an upgrade for you:

1. It downloads the newest release's `.deb` and `texas2step-setup` to
   `/var/cache/texas2step/update/<version>/`. Each must match the SHA-256 that GitHub
   publishes with the release, or it isn't used.
2. It runs that release's setup tool in update mode, which plans only the update of what
   is installed: the package, the [settings merge](#with-the-setup-tool-or-by-hand) and
   texas2step's own nginx files. It publishes nothing anew, adds no firewall rule and
   changes no setting. If the check finds something you must fix first, it stops there.
3. It shows that plan and asks before it runs the install script. The script reloads
   nginx when the package changes an nginx file that nginx loads, and checks that the new
   version answers, directly and through nginx.

```text
$ sudo texas2step update
texas2step <installed> is installed; <newest> is available (<date>).
  Release notes: https://github.com/robert-patrick-texas/texas2step/releases/tag/v<newest>
Downloading texas2step-<newest>-1-amd64.deb (39 MB) and texas2step-setup to /var/cache/texas2step/update/<newest>-1/
Both match the SHA-256 published with the release.

texas2step setup for www.example.net, <date and time>
...
Will change
  - upgrade texas2step <installed>-1 -> <newest>-1; settings are kept
...
Wrote texas2step-install.sh for texas2step update to run.

Install texas2step <newest> now? [y/N] y
texas2step install, <date and time>

== Checking the package
...
== Installing texas2step <newest>-1 (was <installed>-1)
...
== Restarting texas2step
== Checking
texas2step <newest> answers on http://127.0.0.1:8080/api/v1/health
and through nginx at https://www.example.net/texas2step/

Done. Open https://www.example.net/texas2step/
```

Answer **n** (or just Enter) and nothing is installed. The downloads, the setup tool's
report (`texas2step-preflight.txt`), its plan and the install script stay in
`/var/cache/texas2step/update/<version>/` to read. The next run uses those downloads
again rather than fetching them anew. Once you answer **y**, the install script writes
its log, `texas2step-install.log`, there too. A run for another release removes the
folders of earlier ones, and purging the package removes them all.

The options:

* `--yes` installs without asking, for scripts and configuration management.
* `--version <version>` installs that release instead of the newest. Add
  `--allow-downgrade` for an older one, to go back to it.
* `--from <directory>` installs a release whose two files are already on the server,
  without downloading anything.
* `--proxy <url>` downloads through a proxy:
  `sudo texas2step update --proxy=http://proxy.example.net:3128`. A user name and
  password can go in the URL (`http://user:password@proxy.example.net:3128`); they are
  never printed. To use the proxy every time, and for the update timer too, set
  `update_proxy` in the settings. Without either, `https_proxy` from the environment
  is used, if set.
* `-k` or `--insecure` checks no certificate, as `curl -k` does: see
  [A firewall that inspects TLS](#a-firewall-that-inspects-tls).

#### A firewall that inspects TLS

A firewall that inspects outbound HTTPS (break-and-inspect) ends each connection itself
and presents a certificate of its own, signed by the organization's CA. A server that
doesn't trust that CA refuses it, and `texas2step update` says so:

```text
texas2step update: can't reach https://api.github.com/...: ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] ...
```

The better fix is to trust the CA on the server, once. Downloads then stay checked, and
the update timer works too:

```bash
sudo cp organization-ca.crt /usr/local/share/ca-certificates/   # PEM, ending in .crt
sudo update-ca-certificates
```

Where that can't be done, `sudo texas2step update --insecure` downloads without checking
any certificate, and says so each time. Use it knowingly: the SHA-256 digests the
downloads are checked against come over the same unchecked connection, so anything on
the path could hand the server another release with matching digests, and it would run
as root. The setting has no such option, so the update timer never installs this way.

Where the server can't reach GitHub, put a release's files on it, or point it at a
mirror: see [Without access to GitHub](#without-access-to-github). On a server whose texas2step
doesn't have the command yet, upgrade once [with the setup tool](#with-the-setup-tool-or-by-hand).

### Unattended updates

The package brings a timer for them, off until you turn it on:

```bash
sudo systemctl enable --now texas2step-update.timer
```

Every day at a random time between 04:00 and 05:00, the timer runs
`texas2step update --auto`. When a newer release is out, it downloads and checks it as
above. Then:

* If the plan changes nothing but the package and the settings, it installs it. That
  restarts texas2step, which ends any test running at that moment.
* If the plan would also change nginx (a release with a new nginx snippet) or the
  firewall, it installs nothing and logs a warning. Install that release yourself with
  `sudo texas2step update`.

It only ever moves to the newest release: it never reinstalls or goes back to an older
one. Check on it:

```bash
systemctl list-timers texas2step-update.timer   # the next and the last run
journalctl -u texas2step-update                 # what each run did
journalctl -u texas2step-update -p warning      # only the runs that left a release to you
systemctl --failed                              # a failed last run shows here
```

A run fails, and `journalctl -u texas2step-update` says why, in two kinds of case:

* Before installing: it can't reach GitHub, a download doesn't match its SHA-256, or
  the setup tool finds something you must fix first. Nothing has changed, and the next
  day's run tries again.
* After installing: the new version doesn't answer, directly or through nginx. It is
  installed, so the next run finds nothing newer; see `journalctl -u texas2step`, or go
  back with `sudo texas2step update --version <previous> --allow-downgrade`.

A run that leaves a release to you doesn't count as failed: look for its warning. Only
one update runs at a time, so a `sudo texas2step update` started while the timer's run
is busy stops with `another texas2step update is running`.

To run it at another time, `sudo systemctl edit texas2step-update.timer` and add:

```ini
[Timer]
OnCalendar=
OnCalendar=Sun 03:00
```

Behind a proxy, set it in the settings, and restart nothing: the timer reads them on each
run.

```bash
sudo texas2step config set 'update_proxy="http://proxy.example.net:3128"' --write
```

Behind a firewall that inspects TLS, install its CA as in
[A firewall that inspects TLS](#a-firewall-that-inspects-tls); the timer never skips the
certificate check.

To turn unattended updates off again: `sudo systemctl disable --now texas2step-update.timer`.

### Without access to GitHub

On a closed network, put each release on the server by hand, or keep a mirror that
`texas2step update` and the update timer can reach: on a web server, or in a directory
on the server itself.

**From files on the server.** On a machine that can reach GitHub, download the
release's two files, `texas2step-<version>-1-amd64.deb` and `texas2step-setup`, and
compare their SHA-256 with those in its release notes:

```bash
sha256sum texas2step-<version>-1-amd64.deb texas2step-setup
```

Copy both into one directory on the server, such as `/root/texas2step-<version>/`, then:

```bash
texas2step update --check --from /root/texas2step-<version>   # is it newer? exit 100 if so
sudo texas2step update --from /root/texas2step-<version>
```

`--from` takes the place of the download. The two files must be a pair: the setup tool
holds the file name and SHA-256 of the package it was built with, and any other package
stops the update with nothing installed. Both are copied to
`/var/cache/texas2step/update/<version>/` and checked again there, so what runs as root
is what was checked. The update then goes on as from GitHub: the setup tool's plan, the
question (or `--yes`) and the install script. The same version is reinstalled; an older
one needs `--allow-downgrade`. The update timer doesn't use `--from`; for unattended
updates on a closed network, keep a mirror, which can be a directory on the server.

**From GitHub Enterprise or a mirror.** The `update_url` setting tells
`texas2step update` where to look for releases. For GitHub Enterprise, use its API
address for your copy of the project, such as
`https://github.example.net/api/v3/repos/example/texas2step`.

Any web server can hold a mirror instead. It must answer
`<update_url>/releases/latest`, and `<update_url>/releases/tags/v<version>` for
`--version`, with the release as GitHub's API describes it. For each of the two files,
that gives its `name`, `size`, `digest` (`"sha256:<hex>"`) and `browser_download_url`,
the address to download it from. On a machine that can reach GitHub, this copies a
release into `mirror/`, for a web server that serves that directory at
`https://mirror.example.net/texas2step`:

```bash
python3 - <version> https://mirror.example.net/texas2step <<'PY'
import json, os, sys, urllib.request
version, base = sys.argv[1], sys.argv[2]
api = "https://api.github.com/repos/robert-patrick-texas/texas2step/releases/tags/v"
release = json.load(urllib.request.urlopen(api + version))
os.makedirs(f"mirror/download/v{version}", exist_ok=True)
os.makedirs("mirror/releases/tags", exist_ok=True)
for asset in release["assets"]:
    path = f"download/v{version}/{asset['name']}"
    urllib.request.urlretrieve(asset["browser_download_url"], f"mirror/{path}")
    asset["browser_download_url"] = f"{base}/{path}"
for path in (f"releases/tags/v{version}", "releases/latest"):
    with open(f"mirror/{path}", "w") as f:
        json.dump(release, f)
PY
```

Then, on each server:

```bash
sudo texas2step config set update_url=https://mirror.example.net/texas2step --write
texas2step update --check
```

The mirror's description carries the SHA-256 values with it, so compare them once with
those in the release notes on GitHub.

**From a mirror in a directory.** Without a web server, each server can hold the mirror
itself, in a directory such as `/opt/texas2step/mirror`. Run the script above with
`file:///opt/texas2step/mirror` in place of `https://mirror.example.net/texas2step`, so
that the description names each file where it will be on the server. Copy the `mirror/`
it made to the server, then:

```bash
sudo mkdir -p /opt/texas2step/mirror
sudo cp -r mirror/. /opt/texas2step/mirror/
sudo texas2step config set update_url=file:///opt/texas2step/mirror --write
texas2step update --check
```

```text
texas2step <installed> is installed; <newest> is available (<date>).
  Release notes: https://github.com/robert-patrick-texas/texas2step/releases/tag/v<newest>
To install it: sudo texas2step update
```

`texas2step update` and the update timer then work as with a mirror on a web server,
except that they copy the two files rather than download them: each is checked against
the SHA-256 in the description as it is written to
`/var/cache/texas2step/update/<version>/`, so what runs as root is what was checked. For
each new release, run the script again in the same place and copy `mirror/` over once
more: the release is added, `releases/latest` now describes it, and the timer installs it
on its next run. Earlier releases stay there for `--version`. A mirror made for a web
server doesn't work from a directory, nor the other way round, since each names its files
at its own address.

Whoever can write to the mirror decides what `texas2step update` installs as root,
including the timer's runs with nobody there. Keep it writable only by root, or by an
account you trust as much, such as one that copies new releases in.

### With the setup tool, or by hand

The [setup tool](#install-or-upgrade-with-the-setup-tool) does what `texas2step update`
does, step by step. Copy the new `texas2step-setup` and `.deb` to the server, then run
the check and the install script. The check shows how your settings file will be merged
with the new template, and the script does the merge for you. The script also reloads
nginx when the package changes one of its nginx files that nginx loads, and rewrites and
reloads a snippet the setup tool made for another path, such as
`texas2step-at-speedtest.conf`.

By hand: run the commands from [step 1](#1-install-the-package) again. They fetch the
newest release. The service restarts with your settings, and saved results are kept.
nginx reads a new version of the package's snippets in `/etc/nginx/snippets/` when it
next reloads: `sudo nginx -t && sudo systemctl reload nginx`.

When a new version's settings template differs from your edited
`/etc/texas2step/config.toml`, apt asks what to do with it. Keep yours (**N**, the
default). Then bring it up to date with the new template:

```bash
sudo texas2step config merge           # shows the changes; writes nothing
sudo texas2step config merge --write   # applies them, keeping a dated backup
sudo systemctl restart texas2step
```

The merge rebuilds the file from the new version's template and carries over every
value you set:

* Each setting stays in its place in the new layout.
* `[[locations]]` and `[[brand_links]]` move to the end of the file.
* A setting the new version no longer uses is kept as a comment.

Before writing, it checks that the result gives back every one of your values and that
the server accepts it. Comments you added yourself aren't carried over; the backup keeps
them. The package reminds you to run it when an upgrade needs it.

If you answered **Y** instead, apt saved your old file as
`/etc/texas2step/config.toml.dpkg-old`. Add `--from /etc/texas2step/config.toml.dpkg-old`
to both merge commands to put your values back. To skip the question on future
upgrades, add `-o Dpkg::Options::=--force-confold` to the `apt install` command; it
always keeps your file.

## Clear saved results

`texas2step results clear` deletes every saved result: History starts over, and links to
the old results stop working. Use it for a fresh start, or on a server
[cloned from another](#clone-a-server). The service keeps running, so there is nothing to
restart. It shows what it would delete, then asks:

```text
$ sudo texas2step results clear
/var/lib/texas2step/results.db holds 27 saved results, from 2026-10-18 to 2026-10-21.
By server id: dallas 15, local 8, houston 4
Delete them all? Links to them stop working. [y/N] y
Deleted 27 saved results. History starts over with the next test saved.
```

The server id is the server each test ran against; the dates are in the server's time
zone.

* **Without a terminal to ask on**, such as a script or `ssh` with a command, it shows
  the same summary and stops without deleting anything. Add `--yes` (`-y`) to delete
  without asking.
* **It needs `sudo`**: the file belongs to the service's `texas2step` user, and stays
  theirs.
* **What is left on disk**: `/var/lib/texas2step/results.db`, empty and a few kilobytes
  in size, and beside it, while the service runs, its `-wal` and `-shm` files. The
  deleted results are overwritten, not only marked as free space, so nothing of them
  stays in the file. Copies made earlier, such as backups or snapshots of the server,
  keep what they hold, and so does the journal, which names visitors' addresses on
  failed requests, and on every request with `debug` on
  ([Logging](configuration.md#logging)).
* `--config FILE` clears the `results_path` that settings file names. It works whether
  `results_enabled` is on or off.

| Exit status | Meaning |
|---|---|
| 0 | It deleted the results, or there were none |
| 1 | It deleted nothing: you answered no, there was no terminal to ask on, or it couldn't change the file; the message says which |

To keep fewer results from now on, lower `results_max_rows`; to keep none, set
`results_enabled = false` (see [Configuration](configuration.md)).

## Clone a server

A virtual machine cloned from a texas2step server runs texas2step straight away, as a
copy of the first server: the same id, names and links, the same picker, and the first
server's History. These steps make it a server of its own. They call the first server
`dallas` and the clone `houston`.

On the clone:

1. **Give it its own identity.** Every server needs its own `server_id`: the picker
   leaves out the entry with the server's own id, so two servers sharing one would hide
   each other, and History tells servers apart by it. Change `server_url_v4` and
   `server_url_v6` too, or empty them (`server_url_v4=`) if the clone has no
   single-family names: until then, the clone's IPv4/IPv6 switch sends tests to the
   first server.

   ```bash
   sudo texas2step config set server_id=houston server_name=houston.example.net \
       "server_location=Houston, TX" \
       server_url_v4=https://v4.houston.example.net/texas2step \
       server_url_v6=https://v6.houston.example.net/texas2step    # then again with --write
   ```

2. **List the first server in its picker.** The clone has the first server's
   `[[locations]]`, which never lists the first server itself. Then restart, which
   applies both steps:

   ```bash
   sudo texas2step config add-location https://dallas.example.net/texas2step --write
   sudo systemctl restart texas2step
   ```

3. **Start History over**: the clone holds the first server's saved results, with its
   visitors' addresses. `sudo texas2step results clear`, as
   [above](#clear-saved-results). With a [traffic limit](traffic.md), it also holds the
   first server's traffic figures, which would count against its own day and month.
   Delete them with the service stopped:

   ```bash
   sudo systemctl stop texas2step
   sudo rm -f /var/lib/texas2step/traffic.db*
   sudo systemctl start texas2step
   ```

4. **Give it its own names and certificate**, outside texas2step: DNS records for its
   names, including the IPv4-only and IPv6-only ones; `server_name` in its nginx site;
   and a TLS certificate for them. The first server's certificate came with the clone.
   Once nginx uses the clone's own, remove the first server's from the clone so only the
   first server renews it, with certbot `sudo certbot delete --cert-name dallas.example.net`.

5. **Check the firewall in front of it.** The server's own rules came with the clone,
   UDP range included. Open that range for the clone's addresses in any cloud security
   group or network firewall in front of it too. If the clone is on another network,
   check `internal_networks` and `trusted_proxies`. With
   [servers that defend together](access.md#servers-that-defend-together), add the
   clone's addresses to the others' `access_peer_networks`, and check with
   `sudo texas2step access peers` on each.

Then, on the first server and every other server whose picker should offer the clone:

```bash
sudo texas2step config add-location https://houston.example.net/texas2step --write
sudo systemctl restart texas2step
```

`add-location` asks the clone for its id, name and location, so run it once the clone
answers at its new address with its new identity.

What stays as it is:

* **The access key**, `/etc/texas2step/access.key`. An organization's servers share one,
  so the clone gives the same [access codes](access.md) as the first server and accepts
  its passes; `sudo texas2step access show` prints the same fingerprint on both. Only a
  clone for another organization needs a key of its own:
  `sudo texas2step access key --new`, then a restart.
* **The standing phrases**, `/etc/texas2step/access-phrases.toml`, if you made any: the
  clone takes the same [phrases](access.md#standing-phrases) as the first server.
* The first server's [lockouts](access.md#lockouts-and-blocks), which end by themselves;
  `sudo texas2step access lockouts --clear all` ends them now.
* `/etc/default/texas2step`, and the [update timer](#unattended-updates), on or off as on
  the first server. Downloads in `/var/cache/texas2step/update` do no harm; the next
  update removes them.
* The first server's dated settings backups, `/etc/texas2step/config.toml.*.bak`. Delete
  them if you like.

The usual steps for any cloned machine apply as well, though texas2step relies on none
of them: a new `/etc/machine-id` and SSH host keys, and its own hostname and addresses
(`virt-sysprep` and cloud-init can do these). The journal came with the clone too, and
names the first server's visitors as described above;
`sudo journalctl --rotate && sudo journalctl --vacuum-time=1s` empties the whole journal.

## Remove

```bash
sudo apt remove texas2step   # keeps /etc/texas2step and saved results
sudo apt purge texas2step    # also removes them, and the nginx snippets
```

Before purging, take the `include snippets/texas2step*.conf;` lines out of your site,
otherwise `nginx -t` fails once the snippets are gone.

Purging removes the dated backups of your settings, the [access key](access.md) and the
[standing phrases](access.md#standing-phrases) too, but keeps what is yours: a file you
added to `/etc/texas2step`, such as a logo or a word list, a
[mirror in a directory](#without-access-to-github) under `/opt/texas2step`, and the
copies of your nginx site that the setup tool made in `/var/backups/texas2step/`. dpkg
warns that a directory holding one of them isn't empty. Remove them yourself once you no
longer need them, for example `sudo rm -r /opt/texas2step`.

## Security notes

* The download, upload and echo endpoints exist to make the server move a lot of
  bytes. Expose texas2step to networks whose users you would let run a speed test.
  Rate-limit in nginx if needed.
* `/api/v1/results` lists saved results, including client IP addresses, to anyone who
  can reach the server. Set `results_enabled = false` if that's unwanted, and
  [`sudo texas2step results clear`](#clear-saved-results) deletes those already saved.
* Keep `allow_any_target = false` unless the server is on an isolated management
  network. Otherwise anyone can use the server to make requests to other hosts.
* `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Prefix` are believed only
  from `trusted_proxies` (by default, nginx on the same host). A request from a trusted
  proxy that names no visitor address counts as outside, not as the proxy's loopback.
* For a server open to the Internet, turn on [access codes](access.md) and set
  `internal_networks`. Then a visitor from outside gets only the page that asks for the
  code until they enter it, with no version number, and [three wrong codes in a
  row](access.md#lockouts-and-blocks) lock their /24 or /48 out. Use `access_digits = 4`
  there.
* Request bodies are capped by endpoint (a code 1 KB, a saved result 2 MB, others
  64 KB) before they are read; only the upload test takes any size, and with access
  codes on it needs a pass.
* nginx logs texas2step's requests to `/var/log/nginx/access.log` without their query
  strings, so the log never holds an access pass.
* Post-quantum key exchange: nginx on Ubuntu 26.04 (OpenSSL 3.5) can agree each
  connection's keys with ML-KEM-768 and X25519 together (`X25519MLKEM768`), so traffic
  recorded today can't be decrypted later by a quantum computer. Current Chrome, Edge and
  Firefox use it, and so do texas2step's own requests. A site that sets
  `ssl_ecdh_curve` leaves it out unless it is listed; put it first, keeping the curves
  you had:

  ```nginx
  ssl_ecdh_curve X25519MLKEM768:X25519:secp384r1;
  ```

  Then `sudo nginx -t && sudo systemctl reload nginx`, and check:
  `echo | openssl s_client -connect www.example.net:443 2>/dev/null | grep group` should
  say `Negotiated TLS1.3 group: X25519MLKEM768`.
* The service runs as the unprivileged `texas2step` user, under systemd sandboxing
  (`ProtectSystem=strict`, `NoNewPrivileges` and more). It can write only to
  `/var/lib/texas2step`.
