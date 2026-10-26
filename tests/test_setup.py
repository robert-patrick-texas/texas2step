"""packaging/texas2step-setup.py: parsing the host's state, and the plan and script it writes."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).parents[1] / "packaging" / "texas2step-setup.py"
spec = importlib.util.spec_from_file_location("texas2step_setup", PATH)
setup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = setup  # dataclasses look their module up
spec.loader.exec_module(setup)

NGINX_CONF = """\
# configuration file /etc/nginx/nginx.conf:
user www-data;
worker_processes auto;
include /etc/nginx/modules-enabled/*.conf;

events {
	worker_connections 768;
}

http {
	sendfile on;
	include /etc/nginx/mime.types;
	ssl_protocols TLSv1.2 TLSv1.3;
	include /etc/nginx/conf.d/*.conf;
	include /etc/nginx/sites-enabled/*;
}

# configuration file /etc/nginx/sites-enabled/plain:
server {
	listen 80 default_server;
	listen [::]:80 default_server;
	root /var/www/html;
	server_name _;
	location / {
		try_files $uri $uri/ =404;
	}
}
"""

CERTBOT_SITE = """\
# configuration file /etc/nginx/sites-enabled/www.example.net:
server {
    server_name www.example.net example.net;
    root /var/www/html;
    location / {
        try_files $uri $uri/ =404;
    }
    location ~ "\\.(js|css)$" { expires 7d; }

    listen [::]:443 ssl ipv6only=on; # managed by Certbot
    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/www.example.net/fullchain.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
}
server {
    if ($host = www.example.net) {
        return 301 https://$host$request_uri;
    }
    listen 80;
    server_name www.example.net;
    return 404;
}

# configuration file /etc/letsencrypt/options-ssl-nginx.conf:
ssl_session_cache shared:le_nginx_SSL:10m;
"""

SECOND_SITE = """\
# configuration file /etc/nginx/sites-enabled/intranet:
server {
    listen 10.1.2.3:8443 ssl;
    server_name intranet.example.net;
}
"""

# As on a host that already publishes texas2step at /speedtest/ through its own snippet.
PUBLISHED = """\
# configuration file /etc/nginx/sites-enabled/dev.example.net:
server {
    listen 443 ssl;
    server_name dev.example.net;
    include snippets/speedtest.conf;
}

# configuration file /etc/nginx/snippets/speedtest.conf:
location = /speedtest {
    return 301 /speedtest/;
}
location ^~ /speedtest/ {
    proxy_pass http://texas2step/;
}
"""

UFW_ACTIVE = """\
Status: active

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW       Anywhere
443                        ALLOW       Anywhere
22/tcp (v6)                ALLOW       Anywhere (v6)
"""

TEMPLATE = (Path(__file__).parents[1] / "src/texas2step/config_template.toml").read_text()
SNIPPET = (Path(__file__).parents[1] / "deploy/nginx/texas2step.conf").read_text()
UPSTREAM = (Path(__file__).parents[1] / "deploy/nginx/texas2step-http.conf").read_text()
DEFAULTS = (Path(__file__).parents[1] / "deploy/texas2step.default").read_text()


@pytest.fixture(autouse=True)
def no_real_paths(monkeypatch):
    # Fixture paths must not resolve through this machine's own /etc/nginx.
    monkeypatch.setattr(setup.os.path, "realpath", lambda p, **_: p)


def host(*parts: str, **kw) -> "setup.Host":
    dump = "".join(parts) if parts else NGINX_CONF + CERTBOT_SITE
    h = setup.Host(
        hostname="houston.example.net",
        os_release={"ID": "ubuntu", "VERSION_ID": "26.04", "PRETTY_NAME": "Ubuntu 26.04 LTS"},
        deb_path="/root/texas2step-0.2.0-1-amd64.deb",
        deb_sha256="ab" * 32,
        deb={"Version": "0.2.0-1", "Architecture": "amd64", "Installed-Size": "150000"},
        deps=[setup.Dep("python3", ">= 3.14, << 3.15", "3.14.2-1", True, True)],
        nginx_dump=dump,
        site_texts=setup.split_dump(dump),
        ufw=UFW_ACTIVE,
        packaged_snippet=SNIPPET,
        packaged_upstream=UPSTREAM,
        packaged_defaults=DEFAULTS,
    )
    for key, value in kw.items():
        setattr(h, key, value)
    return h


def levels(p, level):
    return [f.text for f in p.findings if f.level == level]


# ---- Parsing --------------------------------------------------------------------------


def test_servers_found_with_their_https_listen_and_insert_point():
    found = setup.servers(setup.split_dump(NGINX_CONF + CERTBOT_SITE + SECOND_SITE))
    https = [s for s in found if s.https]
    assert [(s.names, s.port, s.addr) for s in https] == [
        (["www.example.net", "example.net"], 443, "127.0.0.1"),
        (["intranet.example.net"], 8443, "10.1.2.3"),
    ]
    site = https[0]
    assert (site.line, site.insert_after, site.indent) == (1, 2, "    ")
    assert site.locations == ["/", "\\.(js|css)$"]
    assert [s.names for s in found if not s.https] == [["_"], ["www.example.net"]]


def test_published_through_an_included_snippet():
    (server,) = setup.servers(setup.split_dump(PUBLISHED))
    assert server.published == ["/speedtest/"]
    assert server.locations == ["/speedtest", "/speedtest/"]


def test_upstream_conf_must_be_loaded():
    assert setup.loads_upstream(setup.split_dump(NGINX_CONF))
    assert not setup.loads_upstream(setup.split_dump(PUBLISHED))


@pytest.mark.parametrize(
    ("args", "port", "addr"),
    [
        (["443", "ssl"], 443, "127.0.0.1"),
        (["[::]:443", "ssl"], 443, "127.0.0.1"),
        (["10.0.0.5:8443", "ssl"], 8443, "10.0.0.5"),
        (["127.0.0.1"], 80, "127.0.0.1"),
        (["*:80"], 80, "127.0.0.1"),
    ],
)
def test_listen_directives(args, port, addr):
    assert (setup._listen_port(args), setup._listen_addr(args)) == (port, addr)


def test_port_ranges():
    assert setup.covers("61000:61999", 61000, 61999)
    assert setup.covers("22,60000-62000", 61000, 61999)
    assert not setup.covers("61000:61500", 61000, 61999)
    assert setup.ufw_allows(UFW_ACTIVE + "61000:61999/udp  ALLOW  Anywhere\n", 61000, 61999)
    assert not setup.ufw_allows(UFW_ACTIVE + "61000:61999/tcp  ALLOW  Anywhere\n", 61000, 61999)
    assert not setup.ufw_allows(UFW_ACTIVE, 61000, 61999)
    assert setup.firewalld_allows("22/tcp 61000-61999/udp", 61000, 61999)
    assert not setup.firewalld_allows("61000-61999/tcp", 61000, 61999)


def test_iptables_and_nftables():
    rules = """-P INPUT ACCEPT
-A INPUT -i lo -j ACCEPT
-A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT
-A INPUT -p tcp -m tcp --dport 443 -j ACCEPT
-A INPUT -j DROP
"""
    assert setup.iptables_state(rules, 61000, 61999) == (True, False)
    opened = rules.replace(
        "-A INPUT -j DROP", "-A INPUT -p udp -m udp --dport 61000:61999 -j ACCEPT\n-A INPUT -j DROP"
    )
    assert setup.iptables_state(opened, 61000, 61999) == (True, True)
    assert setup.iptables_state("-P INPUT ACCEPT\n", 61000, 61999) == (False, False)
    nft = """table inet filter {
	chain input {
		type filter hook input priority filter; policy drop;
		tcp dport 443 accept
	}
}"""
    assert setup.nft_state(nft, 61000, 61999) == (True, False)
    assert setup.nft_state(nft.replace("tcp dport 443", "udp dport 61000-61999"), 61000, 61999) == (
        True,
        True,
    )


def test_package_metadata():
    assert setup.parse_depends(
        "python3 (>= 3.14), python3 (<< 3.15), libc6 (>= 2.39), adduser"
    ) == [
        ("python3", ">= 3.14"),
        ("python3", "<< 3.15"),
        ("libc6", ">= 2.39"),
        ("adduser", ""),
    ]
    assert setup.parse_os_release('ID=ubuntu\nVERSION_ID="26.04"\n')["VERSION_ID"] == "26.04"
    assert setup.parse_defaults("#WORKERS=2\nWORKERS=4\n") == {"WORKERS": "4"}
    err = (
        '2026/10/18 23:29:19 [emerg] 979#979: host not found in upstream "texas2step" in '
        "/etc/nginx/snippets/texas2step.conf:19\n"
        "nginx: configuration file /etc/nginx/nginx.conf test failed\n"
    )
    assert setup.nginx_error(err).startswith('host not found in upstream "texas2step"')


# ---- Planning -------------------------------------------------------------------------


def test_fresh_install_on_a_certbot_site_with_ufw():
    opts = setup.Options(id="houston", name="houston.example.net", location="Houston, TX")
    p = setup.plan(host(), opts)
    assert not p.blocked, levels(p, setup.FIX)
    assert p.mode == "fresh"
    assert p.settings == [
        'server_id="houston"',
        'server_name="houston.example.net"',
        'server_location="Houston, TX"',
        "rtc_port_min=61000",
        "rtc_port_max=61999",
    ]
    assert p.nginx.file == "/etc/nginx/sites-enabled/www.example.net"
    assert (p.nginx.after, p.nginx.lines) == (2, ["    include snippets/texas2step.conf;"])
    assert p.public == "https://www.example.net/texas2step/"
    assert p.resolve == "www.example.net:443:127.0.0.1"
    assert p.firewall == [["ufw", "allow", "61000:61999/udp", "comment", "texas2step probes"]]
    assert any("security groups" in t for t in levels(p, setup.NOTE))


def test_install_script_is_valid_bash_and_does_the_plan():
    opts = setup.Options(id="houston", name="houston.example.net", speedtest=True)
    h = host(
        peers=[
            setup.Peer(
                "https://dallas.example.net/texas2step/",
                {
                    "server": {
                        "id": "dallas",
                        "name": "dallas.example.net",
                        "location": "Dallas, TX",
                    }
                },
            )
        ]
    )
    p = setup.plan(h, opts)
    script = setup.install_script(h, p, opts)
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert "sha256sum --check --strict" in script
    assert "apt-get install -y -o Dpkg::Options::=--force-confold" in script
    assert (
        "texas2step config set 'server_id=\"houston\"' 'server_name=\"houston.example.net\"'"
        in script
    )
    assert (
        "texas2step config add-location https://dallas.example.net/texas2step --id dallas "
        "--name dallas.example.net --location 'Dallas, TX' --write"
    ) in script
    assert "awk -v n=2 -v add='    include snippets/texas2step.conf;\\n    include " in script
    assert "ufw allow 61000:61999/udp comment 'texas2step probes'" in script
    assert "http://127.0.0.1:8080/api/v1/health" in script
    assert "--resolve www.example.net:443:127.0.0.1" in script


def test_upgrade_previews_the_merge_and_keeps_pinned_ports():
    config = TEMPLATE.replace("# rtc_port_min = 0", "rtc_port_min = 62000").replace(
        "# rtc_port_max = 0", "rtc_port_max = 62999"
    )
    h = host(
        installed="0.1.0-1",
        version_cmp="lt",
        config=config,
        service_active=True,
        config_md5="edited",
        shipped_md5="old",
        template_md5="new",
        merge_preview="--- a\n+++ b\n-old\n+new\nKept 2 setting(s): ...",
    )
    p = setup.plan(h, setup.Options())
    assert p.mode == "upgrade" and not p.blocked and p.merge
    assert any("config merge" in t for t in levels(p, setup.CHANGE))
    assert p.udp == (62000, 62999) and not any("rtc_port" in s for s in p.settings)
    assert any('server_id stays "local"' in t for t in levels(p, setup.NOTE))
    text = setup.report(h, p)
    assert "Plan: upgrade to texas2step 0.2.0-1" in text and "    +new" in text
    script = setup.install_script(h, p, setup.Options())
    assert "if [ -e /etc/texas2step/config.toml.dpkg-dist ]; then" in script

    # dpkg only leaves the new template beside an edited file when the template changed.
    h.template_md5 = "old"
    p = setup.plan(h, setup.Options())
    assert not p.merge and any("template is the same" in t for t in levels(p, setup.OK))
    h.template_md5, h.config_md5 = "new", "old"
    p = setup.plan(h, setup.Options())
    assert not p.merge and any("never edited" in t for t in levels(p, setup.OK))

    # A reinstall of a rebuilt package of the same version can bring a new template too.
    h.installed, h.version_cmp, h.config_md5 = "0.2.0-1", "eq", "edited"
    p = setup.plan(h, setup.Options())
    assert p.mode == "reinstall" and p.merge
    assert any("config merge" in t for t in levels(p, setup.CHANGE))


def test_an_already_published_host_is_left_alone():
    p = setup.plan(host(NGINX_CONF, PUBLISHED, ufw=None), setup.Options())
    assert p.nginx is None and not p.blocked
    assert p.public == "https://dev.example.net/speedtest/"
    assert any("already published at /speedtest/" in t for t in levels(p, setup.OK))
    # Naming another site publishes it there as well.
    both = host(NGINX_CONF, PUBLISHED, CERTBOT_SITE, ufw=None)
    p = setup.plan(both, setup.Options(site="/etc/nginx/sites-enabled/www.example.net"))
    assert p.nginx.file == "/etc/nginx/sites-enabled/www.example.net"
    p = setup.plan(both, setup.Options(site="/etc/nginx/sites-enabled/dev.example.net"))
    assert p.nginx is None and p.public == "https://dev.example.net/speedtest/"


@pytest.mark.parametrize(
    ("change", "fix"),
    [
        ({"nginx_installed": False}, "nginx isn't installed"),
        ({"nginx_active": False}, "not running"),
        ({"nginx_error": "unknown directive"}, "nginx -T fails"),
        ({"os_release": {"ID": "debian", "VERSION_ID": "13"}}, "packaged for Ubuntu 26.04"),
        ({"installed": "0.3.0-1", "version_cmp": "gt"}, "newer than this package"),
        ({"port_users": ["apache2 (pid 42)"]}, "port 8080 is in use by apache2"),
        ({"deb_exists": False}, "package not found"),
        ({"free_kib": 1000}, "not enough disk space"),
        ({"deps": [setup.Dep("python3", ">= 3.14", "3.12.3-1", False, False)]}, "apt can't find"),
    ],
)
def test_blockers(change, fix):
    p = setup.plan(host(**change), setup.Options())
    assert p.blocked and any(fix in t for t in levels(p, setup.FIX)), levels(p, setup.FIX)


def test_several_https_sites_need_a_choice():
    h = host(NGINX_CONF, CERTBOT_SITE, SECOND_SITE)
    p = setup.plan(h, setup.Options())
    assert p.blocked and "several HTTPS sites" in levels(p, setup.FIX)[0]
    p = setup.plan(h, setup.Options(site="/etc/nginx/sites-enabled/intranet"))
    assert not p.blocked and p.public == "https://intranet.example.net:8443/texas2step/"
    assert p.resolve == "intranet.example.net:8443:10.1.2.3"


def test_a_site_already_using_the_path_is_a_blocker():
    taken = CERTBOT_SITE.replace("location / {", "location /texas2step/ {")
    p = setup.plan(host(NGINX_CONF, taken), setup.Options())
    assert any("already has a location /texas2step/" in t for t in levels(p, setup.FIX))


def test_firewalls_that_are_only_reported():
    rules = "-P INPUT DROP\n-A INPUT -p tcp -m tcp --dport 443 -j ACCEPT\n"
    p = setup.plan(host(ufw=None, iptables=rules, docker=True), setup.Options())
    assert not p.firewall
    (note,) = [t for t in levels(p, setup.NOTE) if "iptables drops" in t]
    assert "sudo iptables  -I INPUT -p udp --dport 61000:61999 -j ACCEPT" in note
    assert "netfilter-persistent save" in note
    p = setup.plan(host(ufw="Status: inactive\n"), setup.Options())
    assert "no inbound firewall filtering" in " ".join(levels(p, setup.OK))
    p = setup.plan(host(), setup.Options(no_firewall=True))
    assert not p.firewall and any("sudo ufw allow" in t for t in levels(p, setup.NOTE))


def test_peers():
    reachable = setup.Peer(
        "https://dallas.example.net/texas2step", {"server": {"id": "dallas", "name": "Dallas"}}
    )
    me = setup.Peer(
        "https://houston.example.net/texas2step", {"server": {"id": "houston", "name": "Houston"}}
    )
    down = setup.Peer("https://down.example.net/texas2step", error="timed out")
    listed = setup.Peer(
        "https://austin.example.net/texas2step", {"server": {"id": "austin", "name": "Austin"}}
    )
    config = (
        TEMPLATE
        + '\n[[locations]]\nid = "austin"\nname = "Austin"\nurl = "https://austin.example.net/texas2step"\n'
    )
    h = host(peers=[reachable, me, down, listed], config=config)
    p = setup.plan(h, setup.Options(id="houston"))
    assert [x["id"] for x in p.peers] == ["dallas"]
    notes = " ".join(levels(p, setup.NOTE))
    assert "same as this server" in notes and "can't reach it (timed out)" in notes
    assert "config add-location https://down.example.net/texas2step --write" in notes
    assert any("Austin" in t and "already listed" in t for t in levels(p, setup.OK))

    plain = setup.Peer(
        "http://dallas.example.net/texas2step", {"server": {"id": "dallas", "name": "D"}}
    )
    p = setup.plan(host(peers=[plain]), setup.Options(id="houston"))
    assert any("browsers on the HTTPS page will block it" in t for t in levels(p, setup.NOTE))


def test_peers_bring_their_single_family_urls():
    v4 = "https://v4.dallas.example.net/texas2step"
    v6 = "https://v6.dallas.example.net/texas2step"
    h3 = "https://h3-6.dallas.example.net/texas2step"
    peer = setup.Peer(
        "https://dallas.example.net/texas2step",
        {"server": {"id": "dallas", "name": "dallas.example.net", "location": "Dallas, TX"}},
        own={"id": "dallas", "url": "", "url_v4": v4, "url_v6": v6, "url_v6_http3": h3},
    )
    h = host(peers=[peer])
    p = setup.plan(h, setup.Options(id="houston"))
    assert (p.peers[0]["url_v4"], p.peers[0]["url_v6"]) == (v4, v6)
    assert p.peers[0]["url_v6_http3"] == h3
    assert any("with its IPv4-only and IPv6-only URLs" in t for t in levels(p, setup.CHANGE))
    script = setup.install_script(h, p, setup.Options(id="houston"))
    assert f"--url-v4 {v4} --url-v6 {v6} --url-v6-http3 {h3} --write" in script


def test_single_family_urls_are_set_and_their_names_checked():
    v4 = "https://v4.houston.example.net/texas2step"
    v6 = "https://v6.houston.example.net/texas2step"
    names = {
        "v4.houston.example.net": (["192.0.2.10"], []),
        "v6.houston.example.net": (["192.0.2.10"], ["2001:db8::10"]),  # a stray A record
    }
    h = host(names=names, local_addrs={"192.0.2.10", "2001:db8::10"})
    opts = setup.Options(id="houston", url_v4=v4 + "/", url_v6=v6)
    p = setup.plan(h, opts)
    assert f'server_url_v4="{v4}"' in p.settings and f'server_url_v6="{v6}"' in p.settings
    assert any("v4.houston.example.net has only A records" in t for t in levels(p, setup.OK))
    assert any("v6.houston.example.net also has A records" in t for t in levels(p, setup.NOTE))
    # nginx is asked for each name after the install, as a warning only.
    script = setup.install_script(h, p, opts)
    assert "--resolve v4.houston.example.net:443:127.0.0.1" in script
    assert f"and as {v4}" in script and "warning: nginx does not answer for" in script

    # A name that points elsewhere (a CDN proxy?), one with no records yet, and not a URL.
    elsewhere = host(names={"v4.houston.example.net": (["203.0.113.5"], [])})
    elsewhere.local_addrs = {"192.0.2.10"}
    p = setup.plan(elsewhere, setup.Options(url_v4=v4))
    assert any("isn't one of this host's addresses" in t for t in levels(p, setup.NOTE))
    p = setup.plan(host(), setup.Options(url_v4=v4))
    assert any("has no A record yet" in t for t in levels(p, setup.NOTE))
    assert setup.plan(host(), setup.Options(url_v4="v4.houston.example.net")).blocked

    # The URL's path must match the published one; an unset URL has none to check.
    p = setup.plan(host(), setup.Options(url_v4="https://v4.houston.example.net/speedtest"))
    assert any(
        "v4.houston.example.net/speedtest ends in /speedtest, but texas2step is published at "
        "/texas2step/" in t
        for t in levels(p, setup.NOTE)
    )
    unset = levels(setup.plan(host(), setup.Options()), setup.NOTE)
    assert not [t for t in unset if "ends in" in t]


# ---- Another path, such as /speedtest ----------------------------------------------------


def test_the_packaged_snippet_moved_to_another_path():
    text = setup.snippet_for(SNIPPET, "/speedtest")
    assert text.startswith(setup.MARK)
    for line in (
        "location = /speedtest {",
        "return 301 /speedtest/;",
        "location ^~ /speedtest/ {",
        "X-Forwarded-Prefix /speedtest;",
        "proxy_pass         http://texas2step/;",  # the upstream's name, not a path
        "location = /speedtest/api/v1/diag/http {",  # left out of nginx's access log
        "proxy_pass http://texas2step/api/v1/diag/http;",
    ):
        assert line in text
    assert "location ^~ /texas2step/" not in text
    assert setup.snippet_for("location /x { }", "/speedtest") is None
    assert setup.snippet_name("/net/speed") == "snippets/texas2step-at-net-speed.conf"


def test_publishing_at_another_path():
    h = host(ufw=None)
    opts = setup.Options(id="houston", path="/speedtest/")
    p = setup.plan(h, opts)
    assert not p.blocked
    assert p.snippet[0] == "/etc/nginx/snippets/texas2step-at-speedtest.conf"
    assert p.nginx.include == "snippets/texas2step-at-speedtest.conf"
    assert p.nginx.lines == ["    include snippets/texas2step-at-speedtest.conf;"]
    assert p.public == "https://www.example.net/speedtest/"
    script = setup.install_script(h, p, opts)
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert "<<'TEXAS2STEP_SNIPPET'" in script and "X-Forwarded-Prefix /speedtest;" in script
    assert "https://www.example.net/speedtest/api/v1/health" in script

    # Blockers: --speedtest as well, a path that isn't one, a hand-written file in the way.
    assert setup.plan(host(ufw=None), setup.Options(path="/speedtest", speedtest=True)).blocked
    assert setup.plan(host(ufw=None), setup.Options(path="speed test")).blocked
    theirs = host(ufw=None, snippet_on_disk="location /speedtest/ { }\n")
    assert setup.plan(theirs, setup.Options(path="/speedtest")).blocked
    # A single-family URL on the other path is pointed out.
    v4 = "https://v4.houston.example.net/texas2step"
    p = setup.plan(host(ufw=None), setup.Options(path="/speedtest", url_v4=v4))
    assert any("should end in the same path" in t for t in levels(p, setup.NOTE))


def test_a_generated_snippet_is_refreshed_and_a_hand_made_one_left_alone():
    site = """\
# configuration file /etc/nginx/sites-enabled/dev.example.net:
server {
    listen 443 ssl;
    server_name dev.example.net;
    include snippets/texas2step-at-speedtest.conf;
}

# configuration file /etc/nginx/snippets/texas2step-at-speedtest.conf:
"""
    current = setup.snippet_for(SNIPPET, "/speedtest")
    older = current.replace("proxy_connect_timeout 10s;", "proxy_connect_timeout 5s;")
    h = host(site + older, ufw=None)
    p = setup.plan(h, setup.Options())
    assert p.nginx is None and p.snippet[0].endswith("texas2step-at-speedtest.conf")
    assert any("rewrite" in t for t in levels(p, setup.CHANGE))
    script = setup.install_script(h, p, setup.Options())
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert "proxy_connect_timeout 10s;" in script and "nginx -t" in script

    p = setup.plan(host(site + current, ufw=None), setup.Options())
    assert p.snippet is None
    assert any("matches the package's snippet" in t for t in levels(p, setup.OK))
    # A hand-made /speedtest snippet, as on PUBLISHED, isn't touched.
    p = setup.plan(host(NGINX_CONF, PUBLISHED, ufw=None), setup.Options())
    assert p.snippet is None and p.nginx is None


def test_the_checksum_stamp(monkeypatch):
    p = setup.plan(host(), setup.Options())
    assert any("carries no checksum" in t for t in levels(p, setup.NOTE))
    monkeypatch.setattr(setup, "DEB_SHA256", "cd" * 32)
    assert setup.plan(host(), setup.Options()).blocked
    monkeypatch.setattr(setup, "DEB_SHA256", "ab" * 32)
    assert not setup.plan(host(), setup.Options()).blocked


def test_missing_dependencies_apt_can_install():
    deps = [setup.Dep("python3", ">= 3.14", None, False, True)]
    p = setup.plan(host(deps=deps), setup.Options())
    assert "apt installs python3 >= 3.14" in levels(p, setup.CHANGE)


# ---- --update, as texas2step update runs it ------------------------------------------

UPGRADE = {"installed": "0.1.0-1", "version_cmp": "lt", "config": TEMPLATE, "service_active": True}
PINNED = TEMPLATE.replace("# rtc_port_min = 0", "rtc_port_min = 61000").replace(
    "# rtc_port_max = 0", "rtc_port_max = 61999"
)


def changes(p):
    return json.loads(setup.plan_json(p))["changes"]


def test_an_update_changes_only_what_is_installed():
    # Not published, no ufw rule, OS-chosen ports: a full run would change all three.
    full = setup.plan(host(**UPGRADE), setup.Options())
    assert full.nginx and full.firewall and any("rtc_port_min" in s for s in full.settings)
    assert changes(full) == {"package": True, "settings": True, "nginx": True, "firewall": True}

    p = setup.plan(host(**UPGRADE), setup.Options(update=True))
    assert not p.blocked and p.nginx is None and not p.firewall and not p.settings
    assert levels(p, setup.CHANGE) == ["upgrade texas2step 0.1.0-1 -> 0.2.0-1; settings are kept"]
    assert any("an update leaves that to you" in t for t in levels(p, setup.NOTE))
    assert not any('server_id stays "local"' in t for t in levels(p, setup.NOTE))
    assert changes(p) == {"package": True, "settings": False, "nginx": False, "firewall": False}
    assert "for texas2step update to run" in setup.report(host(**UPGRADE), p, update=True)

    # With pinned ports the firewall is reported, never changed.
    p = setup.plan(host(**UPGRADE | {"config": PINNED}), setup.Options(update=True))
    assert not p.firewall
    assert (
        "ufw doesn't allow UDP 61000-61999 (an update leaves the firewall alone): "
        "sudo ufw allow 61000:61999/udp"
    ) in levels(p, setup.NOTE)
    # nginx that isn't there, or isn't running, is no blocker for an update.
    for broken in ({"nginx_installed": False}, {"nginx_active": False}, {"nginx_error": "x"}):
        assert not setup.plan(host(**UPGRADE | broken), setup.Options(update=True)).blocked
    # It only updates an install.
    p = setup.plan(host(), setup.Options(update=True))
    assert any("--update only updates an install" in t for t in levels(p, setup.FIX))


def test_an_update_refreshes_a_generated_snippet():
    site = """\
# configuration file /etc/nginx/sites-enabled/dev.example.net:
server {
    listen 443 ssl;
    server_name dev.example.net;
    include snippets/texas2step-at-speedtest.conf;
}

# configuration file /etc/nginx/snippets/texas2step-at-speedtest.conf:
"""
    older = setup.snippet_for(SNIPPET, "/speedtest").replace("10s;", "5s;")
    p = setup.plan(host(site + older, **UPGRADE), setup.Options(update=True))
    assert p.snippet and p.public == "https://dev.example.net/speedtest/"
    assert changes(p)["nginx"]


def test_nginx_reloads_when_the_upgrade_changes_a_file_it_loads():
    snippet, upstream = setup.PACKAGED_SNIPPET, setup.UPSTREAM_CONF
    files = {snippet: ("old", "old", "new"), upstream: ("same", "same", "same")}
    h = host(**UPGRADE, nginx_files=files)
    p = setup.plan(h, setup.Options(update=True))
    assert p.nginx_watch == [snippet, upstream] and p.nginx_reload == [snippet]
    assert f"reload nginx after the upgrade, which changes {snippet}" in levels(p, setup.CHANGE)
    assert changes(p)["nginx"]
    script = setup.install_script(h, p, setup.Options(update=True))
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert f"nginx_files=({snippet} {upstream})" in script
    assert "-o DPkg::Lock::Timeout=600" in script  # waits for unattended-upgrades

    # dpkg keeps a file the operator edited: nothing to reload, but worth a note.
    h.nginx_files = {snippet: ("mine", "old", "new")}
    p = setup.plan(h, setup.Options(update=True))
    assert not p.nginx_reload and not changes(p)["nginx"]
    assert any(f"{snippet}.dpkg-dist" in t for t in levels(p, setup.NOTE))


@pytest.mark.parametrize(
    ("changed", "nginx_t", "status", "calls", "content"),
    [
        (False, 0, 0, [], "old"),
        (True, 0, 0, ["nginx -t", "systemctl reload nginx"], "new"),
        (True, 1, 1, ["nginx -t"], "old"),  # the previous file is back
    ],
)
def test_the_reload_lines_run(tmp_path, changed, nginx_t, status, calls, content):
    conf = tmp_path / "etc/nginx/snippets/texas2step.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("old")
    conf.chmod(0o640)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    for name, code in (("nginx", nginx_t), ("systemctl", 0)):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\nexit {code}\n')
        stub.chmod(0o755)
    before, after = setup.nginx_watch([str(conf)])
    apt = [f"echo new > {conf}"] if changed else []  # dpkg puts the new file in place
    script = "\n".join(["set -euo pipefail", "step() { :; }", *before, *apt, *after])
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert r.returncode == status, r.stderr
    assert (log.read_text().splitlines() if log.exists() else []) == calls
    assert conf.read_text().strip() == content and conf.stat().st_mode & 0o777 == 0o640


# ---- The port texas2step listens on ----------------------------------------------------


def md5(text):
    return setup.hashlib.md5(text.encode()).hexdigest()


def test_the_upstream_and_listen_port_are_read_and_set():
    assert setup.upstream_server(UPSTREAM) == "127.0.0.1:8080"
    moved = setup.with_upstream(UPSTREAM, "127.0.0.1:8181")
    assert setup.upstream_server(moved) == "127.0.0.1:8181"
    assert moved == UPSTREAM.replace("server 127.0.0.1:8080;", "server 127.0.0.1:8181;")
    two = UPSTREAM.replace("keepalive", "server 127.0.0.1:8082;\n    keepalive")
    assert setup.upstream_server(two) is None and setup.upstream_server("") is None

    port = setup.with_listen_port(DEFAULTS, 8181)
    assert "\nLISTEN_PORT=8181\n" in port and "#LISTEN_PORT" not in port
    assert setup.parse_defaults(port)["LISTEN_PORT"] == "8181"
    assert setup.with_listen_port(port, 8282).count("LISTEN_PORT=8282") == 1
    assert setup.with_listen_port("WORKERS=4", 8181) == "WORKERS=4\nLISTEN_PORT=8181\n"

    assert setup.upstream_addr("127.0.0.1", "8181") == "127.0.0.1:8181"
    assert setup.upstream_addr("0.0.0.0", "8181") == "127.0.0.1:8181"
    assert setup.upstream_addr("::", "8181") == "[::1]:8181"
    assert setup.upstream_addr("[::1]", "8181") == "[::1]:8181"


def test_a_fresh_install_on_another_port():
    h = host(port_users=[])  # ss looked at 8181, which is free
    p = setup.plan(h, setup.Options(port=8181))
    assert not p.blocked and p.listen == ("127.0.0.1", "8181")
    assert f"listen on port 8181 instead of 8080 (LISTEN_PORT in {setup.DEFAULTS})" in levels(
        p, setup.CHANGE
    )
    assert any("upstream" in t and "at 127.0.0.1:8181" in t for t in levels(p, setup.CHANGE))
    assert setup.parse_defaults(p.defaults_text)["LISTEN_PORT"] == "8181"
    assert setup.upstream_server(p.upstream_text) == "127.0.0.1:8181"
    assert p.drop_dist == [setup.DEFAULTS, setup.UPSTREAM_CONF]
    script = setup.install_script(h, p, setup.Options(port=8181))
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    # Written before the package, so the service never starts on 8080.
    assert script.index("TEXAS2STEP_DEFAULTS") < script.index("apt-get install")
    assert script.index("TEXAS2STEP_UPSTREAM") < script.index("apt-get install")
    assert f"rm -f {setup.DEFAULTS}.dpkg-dist" in script
    assert "http://127.0.0.1:8181/api/v1/health" in script
    assert "http://127.0.0.1:8080" not in script
    # Below the ephemeral ports there's no note; inside them there is.
    assert not any("outgoing" in t for t in levels(p, setup.NOTE))
    p = setup.plan(h, setup.Options(port=40000))
    assert any("outgoing connections' ports" in t for t in levels(p, setup.NOTE))


def test_the_port_in_use_is_the_one_asked_for():
    p = setup.plan(host(port_users=["python3 (pid 330)"]), setup.Options(port=8181))
    assert levels(p, setup.FIX) == [
        "port 8181 is in use by python3 (pid 330); texas2step listens there "
        f"(LISTEN_PORT in {setup.DEFAULTS}); choose another with --port"
    ]


def test_an_upstream_that_points_elsewhere_is_pointed_at_the_service():
    # LISTEN_PORT set by hand, the package's upstream still at 8080: nginx would send
    # visitors to whatever else listens there.
    h = host(defaults_text="LISTEN_PORT=8181\n", defaults={"LISTEN_PORT": "8181"})
    p = setup.plan(h, setup.Options())
    assert p.defaults_text is None and setup.upstream_server(p.upstream_text) == "127.0.0.1:8181"
    assert any("(was 127.0.0.1:8080)" in t for t in levels(p, setup.CHANGE))
    # Installed, the upstream never edited: it is edited now, and dpkg keeps that.
    shipped = md5(UPSTREAM)
    h = host(
        **UPGRADE | {"installed": "0.2.0-1", "version_cmp": "eq"},
        defaults_text="LISTEN_PORT=8181\n",
        defaults={"LISTEN_PORT": "8181"},
        upstream_on_disk=UPSTREAM,
        nginx_files={setup.UPSTREAM_CONF: (shipped, shipped, shipped)},
    )
    p = setup.plan(h, setup.Options())
    assert setup.upstream_server(p.upstream_text) == "127.0.0.1:8181" and not p.drop_dist
    assert changes(p)["nginx"]
    # In step already: nothing to do.
    h.upstream_on_disk = setup.with_upstream(UPSTREAM, "127.0.0.1:8181")
    p = setup.plan(h, setup.Options())
    assert p.upstream_text is None and not any("upstream" in t for t in levels(p, setup.CHANGE))


def test_an_upgrade_keeps_the_port_and_takes_the_new_upstream_file():
    newer = UPSTREAM.replace("keepalive 32;", "keepalive 64;")
    mine = setup.with_upstream(UPSTREAM, "127.0.0.1:8181")
    old = md5(UPSTREAM)
    h = host(
        **UPGRADE,
        defaults_text="LISTEN_PORT=8181\n",
        defaults={"LISTEN_PORT": "8181"},
        upstream_on_disk=mine,
        packaged_upstream=newer,
        nginx_files={setup.UPSTREAM_CONF: (md5(mine), old, md5(newer))},
    )
    p = setup.plan(h, setup.Options(update=True))
    assert p.upstream_text == setup.with_upstream(newer, "127.0.0.1:8181")
    assert p.drop_dist == [setup.UPSTREAM_CONF]
    assert (
        f"bring {setup.UPSTREAM_CONF} up to date from the package, keeping 127.0.0.1:8181"
        in levels(p, setup.CHANGE)
    )
    assert not any(".dpkg-dist" in t for t in levels(p, setup.NOTE))
    # Edited in other ways too: dpkg keeps it, and the report says so.
    tuned = mine.replace("keepalive 32;", "keepalive 16;")
    h.upstream_on_disk = tuned
    h.nginx_files = {setup.UPSTREAM_CONF: (md5(tuned), old, md5(newer))}
    p = setup.plan(h, setup.Options(update=True))
    assert p.upstream_text is None
    assert any(f"{setup.UPSTREAM_CONF}.dpkg-dist" in t for t in levels(p, setup.NOTE))


@pytest.mark.parametrize(
    ("disk", "now", "status", "says", "content"),
    [
        (None, None, 0, "wrote", "new\n"),
        (None, "new\n", 0, "is up to date", "new\n"),
        ("old\n", "old\n", 0, "the previous one is in", "new\n"),
        ("old\n", "changed\n", 1, "changed since texas2step-setup checked it", "changed\n"),
        (None, "appeared\n", 1, "changed since texas2step-setup checked it", "appeared\n"),
    ],
)
def test_the_write_lines_run(tmp_path, disk, now, status, says, content):
    target = tmp_path / "etc/default/texas2step"
    if now is not None:
        target.parent.mkdir(parents=True)
        target.write_text(now)
    backups = tmp_path / "backups"
    lines = setup.write_file(str(target), "new\n", disk, "EOF_TEST", str(backups))
    script = "\n".join(["set -euo pipefail", *lines])
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == status and says in r.stdout + r.stderr, r.stdout + r.stderr
    assert target.read_text() == content
    assert len(list(backups.glob("texas2step.*"))) == (says == "the previous one is in")


def test_the_port_option():
    for bad in ("80", "70000", "http"):
        with pytest.raises(SystemExit):
            setup.main(["--port", bad])
    with pytest.raises(SystemExit):
        setup.main(["--update", "--port", "8181"])
