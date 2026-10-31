# Container

texas2step also comes as a container image for Docker and Podman, for amd64 and arm64:

```text
ghcr.io/robert-patrick-texas/texas2step
```

It is the same server as the [Debian package](install.md), with nginx in front of it for
TLS, HTTP/2 and, if you turn it on, HTTP/3. The image's site serves texas2step at
`/texas2step/`, as the package does, and its root (`/`) leads there. Use it on any
Linux host that runs Docker Engine or Podman, whatever its distribution.

## What you need

* A Linux host with Docker Engine or Podman, on amd64 or arm64.
* **Host networking**, or a [macvlan](#macvlan-an-address-of-its-own) network that gives
  the container an address of its own. The packet-loss test, Monitor and Diag's UDP rows
  send UDP straight to the server's own address, which must be one the browser can reach.
  Docker's default bridge network hides it behind NAT, so `-p` port publishing doesn't
  work: see [Why not -p](#why-not--p). For the same reason, the UDP test can't work on
  Docker Desktop for macOS and Windows.
* TCP 443 and 80 free on the host, or [other ports](#the-containers-settings), and
  [inbound UDP](#firewall-and-udp) for the loss test.

## Run it

```bash
docker run -d --name texas2step --network host --restart unless-stopped \
    -v texas2step-etc:/etc/texas2step -v texas2step-data:/var/lib/texas2step \
    -e TEXAS2STEP_SERVER_ID=houston -e TEXAS2STEP_SERVER_NAME=houston.example.net \
    -e TEXAS2STEP_SERVER_LOCATION="Houston, TX" \
    ghcr.io/robert-patrick-texas/texas2step:2
```

Then open `https://houston.example.net/texas2step/` and press **Start**. The packet-loss
tile should end in **UDP**. The first start makes the settings file, the access key and
a certificate, and the log says what it did (`docker logs texas2step`):

```text
texas2step: wrote the settings file /etc/texas2step/config.toml
texas2step: made a self-signed certificate for houston, good for 825 days; browsers warn about it. To use your own, put fullchain.pem and privkey.pem in /etc/texas2step/tls/ and restart
texas2step: https on port 443 (HTTP3=off), HTTP_PORT=80 redirects; server on 127.0.0.1:8080, 2 workers
```

The two volumes keep everything that must outlive the container:

| Volume | Holds |
| --- | --- |
| `/etc/texas2step` | `config.toml`, the access key and phrases, `tls/` and your nginx files in `nginx/` |
| `/var/lib/texas2step` | saved results, lockouts, traffic counts and CPU figures |

Named volumes, as above, are simplest. A directory of the host works too
(`-v /srv/texas2step/etc:/etc/texas2step`); the image keeps `/var/lib/texas2step` owned by
its service account, user and group 990.

`:2` follows the newest 2.x release. To choose when to move, name a release instead,
as `:<version>`. See [Update](#update).

### Docker Compose

[`deploy/container/compose.yaml`](../deploy/container/compose.yaml) holds the same
container. Copy it, set your server's id, name and location in it, then:

```bash
docker compose up -d
```

### Podman and systemd (Quadlet)

[`deploy/container/texas2step.container`](../deploy/container/texas2step.container) runs
it as a systemd service under Podman 4.4 or later. Set your server's details in it, then:

```bash
sudo cp texas2step.container /etc/containers/systemd/
sudo systemctl daemon-reload
sudo systemctl start texas2step
```

It is labelled for `podman auto-update`: with `sudo systemctl enable --now
podman-auto-update.timer`, Podman pulls a newer image for its tag each day and restarts
the service with it.

**Rootless Podman** can use host networking too, but an unprivileged user can't open
ports below 1024: set `HTTPS_PORT=8443` and `HTTP_PORT=0`, or lower the limit for the
host with `sudo sysctl net.ipv4.ip_unprivileged_port_start=80`.

## Settings

### texas2step's settings

Every setting in [Configuration](configuration.md) can be given two ways:

* **Environment variables**, `TEXAS2STEP_` and the setting's name in capitals, as in the
  example above: `-e TEXAS2STEP_ACCESS_CODE=daily`. Lists are comma separated, and
  booleans take `true` or `false`. They win over the file.
* **`/etc/texas2step/config.toml`** in the settings volume, made from the package's
  template at the first start. `[[locations]]`, `[[brand_links]]` and
  `[[brand_footer_links]]` can only be set there. Edit it with the command line in the
  container, then restart it:

  ```bash
  docker exec texas2step texas2step config set access_code=daily --write
  docker restart texas2step
  ```

### The container's settings

These are read by the image's entrypoint, not by texas2step:

| Variable | Default | What it does |
| --- | --- | --- |
| `HTTPS_PORT` | `443` | nginx's HTTPS port. `0`: no site of the image's own; only your files in `/etc/texas2step/nginx/` |
| `HTTP_PORT` | `80` | A redirect to HTTPS. `0`: none |
| `HTTP3` | `off` | `on`: [HTTP/3](#http3) on `HTTPS_PORT`'s UDP port too |
| `TLS_CERT` | `/etc/texas2step/tls/fullchain.pem` | The certificate and its chain |
| `TLS_KEY` | `/etc/texas2step/tls/privkey.pem` | Its private key |
| `LISTEN_PORT` | `8080` | The texas2step server, on 127.0.0.1 behind nginx |
| `WORKERS` | `2` | The server's worker processes: more spread a test over more cores ([Server CPU](cpu.md#server-cpu-bound-results)) |

With host networking these are the host's own ports, so pick ones nothing else on the
host uses. The entrypoint checks them and stops with a reason if one is wrong; nginx
says so in the log if a port is taken.

## Certificate

Without one of your own, the first start makes a self-signed certificate for the host's
name, kept in the settings volume. Browsers warn about it until you replace it.

To use your own, put the chain and key in the settings volume as
`tls/fullchain.pem` and `tls/privkey.pem`, or mount them and say where:

```bash
docker run ... -v /etc/letsencrypt:/etc/letsencrypt:ro \
    -e TLS_CERT=/etc/letsencrypt/live/houston.example.net/fullchain.pem \
    -e TLS_KEY=/etc/letsencrypt/live/houston.example.net/privkey.pem ...
```

Mount all of `/etc/letsencrypt`, not only `live/`: the files there are links into
`archive/`. When the files change, such as at a renewal, nginx loads them within the
hour, with no restart. With certbot on the same host, its standalone mode needs port 80:
set `HTTP_PORT=0`, or use certbot's DNS challenge.

## Networking

### Host networking

`--network host` (Compose: `network_mode: host`) puts the container on the host's own
addresses, as the package is. The server sees each visitor's real address, in both
IPv4 and IPv6, which access codes, `internal_networks`, traffic limits and History
depend on, and it offers the browser the host's addresses for UDP.

### macvlan: an address of its own

On a host whose ports 443 and 80 are taken, or to give the server its own address on a
VLAN, attach it to a macvlan network instead. Here the host's `eth0` is on
`192.0.2.0/24` and `2001:db8:10::/64`:

```bash
docker network create -d macvlan -o parent=eth0 \
    --subnet 192.0.2.0/24 --gateway 192.0.2.1 \
    --ipv6 --subnet 2001:db8:10::/64 --gateway 2001:db8:10::1 lan
docker run -d --name texas2step --network lan --ip 192.0.2.50 --ip6 2001:db8:10::50 \
    --restart unless-stopped \
    -v texas2step-etc:/etc/texas2step -v texas2step-data:/var/lib/texas2step \
    ghcr.io/robert-patrick-texas/texas2step:2
```

* Docker assigns the addresses itself, not your DHCP server, so give it ones outside the
  range that server hands out.
* The host itself can't reach a macvlan address. Test from another machine.
* The switch port must accept more than one MAC address. On a VLAN, use the VLAN's
  interface as the parent, such as `-o parent=eth0.20`.

### Why not -p

With Docker's default bridge network and `-p 443:443`, the page loads, but:

* The UDP test offers the browser the container's bridge address, which it can't reach.
  The page falls back to TCP and says so.
* On a bridge network without IPv6, Docker's port proxy takes IPv6 connections and passes
  them on over IPv4, so the server sees the proxy's address instead of each visitor's.
  Access codes, `internal_networks` and History then see one visitor.

### Firewall and UDP

As with the package: TCP for the page, and inbound UDP from clients for the loss test.
Pin the UDP ports with `rtc_port_min` and `rtc_port_max` and allow that range on the host,
as [Firewall and UDP](install.md#4-firewall-and-udp) describes:

```bash
docker run ... -e TEXAS2STEP_RTC_PORT_MIN=61000 -e TEXAS2STEP_RTC_PORT_MAX=61999 ...
sudo ufw allow 61000:61999/udp
```

## HTTP/3

`HTTP3=on` adds HTTP/3 on `HTTPS_PORT`'s UDP port and advertises it with an `Alt-Svc`
header. Allow inbound UDP to that port. Browsers then move to HTTP/3 after their first
visit, and the Test page's **Auto** choice with them.

To keep the site on HTTP/2 and offer HTTP/1.1 and HTTP/3 the way Diag and the Test page
compare them, on names of their own, leave `HTTP3` off and add their `server` blocks as
files in `/etc/texas2step/nginx/`, as in
[Names for each HTTP version](install.md#names-for-each-http-version). Use the image's
certificate lines:

```nginx
server {                                     # HTTP/3, found over TCP first
    listen [2001:db8::3]:443 ssl;
    listen [2001:db8::3]:443 quic;
    http2 on;
    server_name h3-6.houston.example.net;
    ssl_certificate     /etc/texas2step/tls/fullchain.pem;
    ssl_certificate_key /etc/texas2step/tls/privkey.pem;
    add_header Alt-Svc 'h3=":443"; ma=86400' always;
    include snippets/texas2step.conf;
}
```

Then set `server_url_v6_http3` and restart the container. nginx reads these files after
the image's own site, at every start.

## Command line

The `texas2step` command is in the container. Run it with `docker exec`, as root, with no
`sudo`:

```bash
docker exec texas2step texas2step access show
docker exec texas2step texas2step cpu
docker exec texas2step texas2step clients
docker exec -it texas2step texas2step results clear
```

Where the other guides say `sudo texas2step …`, run `docker exec texas2step texas2step …`,
and for `sudo systemctl restart texas2step`, `docker restart texas2step`.

## Logs

`docker logs -f texas2step` shows the entrypoint, the server and nginx together. The
server stays quiet unless something fails ([Logging](configuration.md#logging)). nginx
logs each texas2step request without its query string, which can hold an access pass.

If the server or nginx stops, the entrypoint stops the other one and the container
ends, so `--restart unless-stopped` (or Quadlet's `Restart=always`) starts both again.
Every 30 seconds the image checks that both answer, and `docker ps` shows the result as
`healthy` or `unhealthy`. Podman reads no such check from the image, so the Quadlet file
names it with `HealthCmd`.

## Update

A newer image updates the server. `texas2step update` is for the package and refuses to
run in the container. Pull the image and replace the container. The volumes keep the
settings, the key and the results:

```bash
docker pull ghcr.io/robert-patrick-texas/texas2step:2
docker rm -f texas2step
docker run -d --name texas2step ...   # as before
```

With Compose, `docker compose pull && docker compose up -d`. Under Quadlet,
`podman auto-update`, or `sudo systemctl restart texas2step` after a `podman pull`.

Each release is tagged by its version (`2.13.0`, say), its minor version (`2.13`), its
major version (`2`) and `latest`. Each release's notes say what changes. Settings added
by a release take their defaults; to see them in your file too, as the package's upgrade
does:

```bash
docker exec texas2step texas2step config merge            # shows the changes
docker exec texas2step texas2step config merge --write
```

Each image carries a signed attestation of the GitHub Actions run that built it from
this repository. To check one:

```bash
gh attestation verify oci://ghcr.io/robert-patrick-texas/texas2step:<version> \
    --owner robert-patrick-texas
```

## Several servers

Servers that share access codes need the same access key
([Access codes](access.md)). From the first to each other server:

```bash
docker exec texas2step texas2step access key > access.key                         # on the first
docker exec -i texas2step texas2step access key --import < access.key && docker restart texas2step
```

Delete the copy afterwards. Each server's `[[locations]]` go in its `config.toml`.

## Build it yourself

From a clone of the repository:

```bash
docker build -t texas2step .          # or: make image
```

The [Dockerfile](../Dockerfile) builds the server's virtual environment from `uv.lock`
on Ubuntu 26.04, then copies it into an image with the distribution's nginx.

## Security notes

* nginx's master process runs as root inside the container, so it can open ports 443
  and 80, and its workers as `www-data`. The texas2step server runs as its own account,
  without the means to gain privileges.
* With host networking the container shares the host's network stack, as the package
  does. It can open any port on the host and lists the host's interfaces.
* The server's CPU figures ([Server CPU](cpu.md)) read the host's whole CPU time, so
  "other work" includes other containers on the host.
* [Security](security.md) covers the rest, as for the package.
