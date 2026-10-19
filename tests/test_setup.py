"""packaging/texas2step-setup.py: parsing the host's state, and the plan and script it writes."""

import importlib.util
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


@pytest.fixture(autouse=True)
def no_real_paths(monkeypatch):
    # Fixture paths must not resolve through this machine's own /etc/nginx.
    monkeypatch.setattr(setup.os.path, "realpath", lambda p: p)


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
        "https://aus.example.net/texas2step", {"server": {"id": "aus1", "name": "Austin"}}
    )
    config = (
        TEMPLATE
        + '\n[[locations]]\nid = "aus1"\nname = "Austin"\nurl = "https://aus.example.net/texas2step"\n'
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
