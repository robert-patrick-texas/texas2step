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
| `/etc/nginx/snippets/texas2step.conf` | The `/texas2step/` location for your site |
| `/etc/nginx/snippets/texas2step-speedtest.conf` | Optional `/speedtest` redirect |
| `/var/lib/texas2step/` | Saved results (SQLite) |

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

If your site already uses `/speedtest`, leave out the second include. To give
texas2step its own hostname instead, such as `speed.example.net`, include
`snippets/texas2step.conf` in that site's server block and add
`location = / { return 302 /texas2step/; }`.

### HTTP/2 and parallel streams

Over HTTP/2, every parallel stream of a throughput test shares **one** TCP connection.
That is realistic for modern web traffic, but it doesn't show how several TCP flows
behave. To make "4 streams" mean four TCP connections, publish texas2step in a server
block without `http2 on` (a separate hostname or port).

## 3. Configure

Edit `/etc/texas2step/config.toml`, then run `sudo systemctl restart texas2step`. The
file lists every setting with its default. The ones most sites set:

```toml
server_id = "dal1"              # short id stored with results
server_name = "Dallas DC"       # shown in the header and in results
server_location = "Dallas, TX"
```

All settings are described in [Configuration](configuration.md).

**Branding and themes.** You can rename the site, show your logo and organization, add
links to your service desk or support group, put a notice at the top of every page, and
choose the default theme (Light, Dark, Federal, Texas, America, Energy or Military). See
[Branding and themes](configuration.md#branding-and-themes).

**Several locations.** Install texas2step at each site, give each a distinct
`server_id`, `server_name` and `server_location`, and list the others under
`[[locations]]`. Users then pick a site in the header, and the browser tests that site
directly. Sites can also measure each other with `POST /api/v1/measure/full`, but only
toward listed locations.

**Listen address and workers.** These are set in `/etc/default/texas2step`. If you move
the service off `127.0.0.1:8080`, change the `upstream` in
`/etc/nginx/conf.d/texas2step.conf` to match. More workers help on multi-gigabit links,
where Python's per-connection CPU cost is the limit. Each worker keeps its own UDP
sessions, which is fine, because a session lives entirely in the worker that answered
it.

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
* Logs: `journalctl -u texas2step -f`.

## Upgrade

Run the commands from [step 1](#1-install-the-package) again: they fetch the newest
release. The service restarts with your settings, and saved results are kept.

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

## Remove

```bash
sudo apt remove texas2step   # keeps /etc/texas2step and saved results
sudo apt purge texas2step    # also removes them, and the nginx snippets
```

Before purging, take the `include snippets/texas2step*.conf;` lines out of your site,
otherwise `nginx -t` fails once the snippets are gone.

## Security notes

* The download, upload and echo endpoints exist to make the server move a lot of
  bytes. Expose texas2step to networks whose users you would let run a speed test.
  Rate-limit in nginx if needed.
* `/api/v1/results` lists saved results, including client IP addresses, to anyone who
  can reach the server. Set `results_enabled = false` if that's unwanted.
* Keep `allow_any_target = false` unless the server is on an isolated management
  network. Otherwise anyone can use the server to make requests to other hosts.
* `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Prefix` are believed only
  from `trusted_proxies` (by default, nginx on the same host).
* The service runs as the unprivileged `texas2step` user, under systemd sandboxing
  (`ProtectSystem=strict`, `NoNewPrivileges` and more). It can write only to
  `/var/lib/texas2step`.
