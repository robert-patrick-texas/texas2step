#!/bin/bash
# texas2step's container entrypoint (/usr/lib/texas2step/entrypoint in the image).
#
# With no command, it gets the volumes ready, writes nginx's site, then runs the texas2step
# server and nginx. When either stops, it stops the other and the container ends, so the
# restart policy starts both again. With a command, it runs only that:
#
#     docker run --rm ghcr.io/robert-patrick-texas/texas2step texas2step --version
#
# The container's own settings (docs/container.md):
#
#   HTTPS_PORT=443    nginx's HTTPS port. 0: no site of the image's own, only the files
#                     in /etc/texas2step/nginx/
#   HTTP_PORT=80      redirects to HTTPS. 0: none
#   HTTP3=off         on: HTTP/3 on HTTPS_PORT's UDP port too, advertised with Alt-Svc
#   LISTEN_PORT=8080  the texas2step server, on 127.0.0.1 behind nginx
#   WORKERS=2         its worker processes
#   TLS_CERT, TLS_KEY the certificate chain and its key; by default fullchain.pem and
#                     privkey.pem in /etc/texas2step/tls/, self-signed at the first start
#                     if neither is there
#
# texas2step's own settings come from TEXAS2STEP_* variables or /etc/texas2step/config.toml.
set -euo pipefail

if [ $# -gt 0 ]; then
    exec "$@"
fi

say() { echo "texas2step: $*" >&2; }
fail() {
    say "$*"
    exit 1
}

# A port number from 1 to 65535, or 0 where "none" is allowed; printed without leading zeros.
port() {
    local name=$1 value=$2 none=${3:-}
    case $value in '' | *[!0-9]* | ???????*) fail "$name=$value: give a port number" ;; esac
    value=$((10#$value))
    if [ "$value" -gt 65535 ] || { [ "$value" -eq 0 ] && [ -z "$none" ]; }; then
        fail "$name=$value: give a port from 1 to 65535${none:+, or 0 for $none}"
    fi
    echo "$value"
}

HTTPS_PORT=$(port HTTPS_PORT "${HTTPS_PORT:-443}" "no site of the image's own")
HTTP_PORT=$(port HTTP_PORT "${HTTP_PORT:-80}" "no redirect")
LISTEN_PORT=$(port LISTEN_PORT "${LISTEN_PORT:-8080}")
case ${WORKERS:-2} in '' | *[!0-9]* | 0 | ????*) fail "WORKERS=$WORKERS: give a number from 1 to 999" ;; esac
WORKERS=$((10#${WORKERS:-2}))
case ${HTTP3:-off} in
    on | true | yes | 1) HTTP3=on ;;
    off | false | no | 0) HTTP3=off ;;
    *) fail "HTTP3=$HTTP3: give on or off" ;;
esac
if [ "$HTTPS_PORT" -ne 0 ] && [ "$HTTPS_PORT" -eq "$HTTP_PORT" ]; then
    fail "HTTPS_PORT and HTTP_PORT are both $HTTPS_PORT: give them different ports"
fi
if [ "$LISTEN_PORT" -eq "$HTTPS_PORT" ] || [ "$LISTEN_PORT" -eq "$HTTP_PORT" ]; then
    fail "LISTEN_PORT=$LISTEN_PORT is nginx's port too: give it another"
fi
tls=/etc/texas2step/tls
TLS_CERT=${TLS_CERT:-$tls/fullchain.pem}
TLS_KEY=${TLS_KEY:-$tls/privkey.pem}

# 1. The settings volume: the settings file, the access key and the standing phrases, as
#    the package makes them. Each stays as it is once there.
install -d -m 755 /etc/texas2step /etc/texas2step/nginx
if [ ! -e /etc/texas2step/config.toml ]; then
    install -m 644 /usr/share/texas2step/config.toml /etc/texas2step/config.toml
    say "wrote the settings file /etc/texas2step/config.toml"
fi
texas2step access key --init || say "no access key was made; see docs/access.md"
texas2step access phrase init >/dev/null || say "no access phrases file was made; see docs/access.md"

# 2. The data volume belongs to the service, including files a command run as root made.
find /var/lib/texas2step \! -user texas2step -exec chown texas2step:texas2step {} +
# The active clients (texas2step clients): in memory, gone when the container is.
install -d -o texas2step -g texas2step -m 755 /run/texas2step

# 3. The certificate: the one given, or a self-signed one made once.
if [ "$HTTPS_PORT" -ne 0 ]; then
    if [ ! -e "$TLS_CERT" ] && [ ! -e "$TLS_KEY" ] \
        && [ "$TLS_CERT" = "$tls/fullchain.pem" ] && [ "$TLS_KEY" = "$tls/privkey.pem" ]; then
        name=$(hostname)
        install -d -m 755 "$tls"
        (umask 077 && openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -sha256 \
            -days 825 -nodes -subj "/CN=$name" \
            -addext "subjectAltName=DNS:$name,DNS:localhost,IP:127.0.0.1,IP:::1" \
            -keyout "$TLS_KEY" -out "$TLS_CERT" 2>/dev/null)
        chmod 644 "$TLS_CERT"
        say "made a self-signed certificate for $name, good for 825 days; browsers warn about it." \
            "To use your own, put fullchain.pem and privkey.pem in $tls/ and restart"
    fi
    [ -r "$TLS_CERT" ] || fail "TLS_CERT=$TLS_CERT: no certificate there"
    [ -r "$TLS_KEY" ] || fail "TLS_KEY=$TLS_KEY: no key there"
fi

# 4. nginx's site, written again at each start from the settings above.
site=/run/texas2step-nginx
rm -rf "$site"
install -d -m 755 "$site"
sed "s/server 127\.0\.0\.1:8080;/server 127.0.0.1:$LISTEN_PORT;/" \
    /usr/share/texas2step/nginx-upstream.conf >"$site/upstream.conf"
# Without IPv6 in the kernel, nginx can't open [::] and would not start.
v6=
[ -e /proc/net/if_inet6 ] && v6=1
listen() { # port [parameters]
    echo "    listen $1${2:+ $2};"
    [ -z "$v6" ] || echo "    listen [::]:$1${2:+ $2};"
}
if [ "$HTTPS_PORT" -ne 0 ]; then
    {
        echo "# Written by the entrypoint at each start, from HTTPS_PORT, HTTP_PORT, HTTP3 and TLS_*."
        echo "server {"
        listen "$HTTPS_PORT" "ssl default_server"
        if [ "$HTTP3" = on ]; then
            listen "$HTTPS_PORT" "quic reuseport"
            echo "    add_header Alt-Svc 'h3=\":$HTTPS_PORT\"; ma=86400' always;"
        fi
        echo "    http2 on;"
        echo "    server_name _;"
        echo "    ssl_certificate \"$TLS_CERT\";"
        echo "    ssl_certificate_key \"$TLS_KEY\";"
        echo
        echo "    # texas2step at /texas2step/, as with the package; the site's root leads there."
        echo "    location = / {"
        echo "        absolute_redirect off;"
        echo "        return 302 /texas2step/;"
        echo "    }"
        echo "    include snippets/texas2step.conf;"
        echo "}"
        if [ "$HTTP_PORT" -ne 0 ]; then
            at=
            [ "$HTTPS_PORT" -eq 443 ] || at=":$HTTPS_PORT"
            echo
            echo "server {"
            listen "$HTTP_PORT" default_server
            echo "    server_name _;"
            echo "    return 301 https://\$host$at\$request_uri;"
            echo "}"
        fi
    } >"$site/site.conf"
fi
nginx -t -q || fail "nginx's configuration has an error (above)"

# 5. Run both. texas2step runs as its own account; nginx's workers as www-data.
if [ "$HTTPS_PORT" -ne 0 ]; then
    redirect="HTTP_PORT=$HTTP_PORT redirects"
    [ "$HTTP_PORT" -ne 0 ] || redirect="no HTTP redirect"
    say "https on port $HTTPS_PORT (HTTP3=$HTTP3), $redirect; server on 127.0.0.1:$LISTEN_PORT, $WORKERS workers"
else
    say "HTTPS_PORT=0: only the sites in /etc/texas2step/nginx/; server on 127.0.0.1:$LISTEN_PORT"
fi
cd /var/lib/texas2step
HOME=/var/lib/texas2step setpriv --reuid=texas2step --regid=texas2step --init-groups --no-new-privs \
    texas2step serve --host 127.0.0.1 --port "$LISTEN_PORT" --workers "$WORKERS" &
server=$!
nginx -g "daemon off;" &
proxy=$!

# A renewed certificate (certbot on the host, say) is picked up within the hour.
watcher=
if [ "$HTTPS_PORT" -ne 0 ]; then
    (
        stamp() { stat -L -c %Y.%s "$TLS_CERT" "$TLS_KEY" 2>/dev/null || true; }
        last=$(stamp)
        while sleep 3600; do
            now=$(stamp)
            if [ "$now" != "$last" ]; then
                say "the certificate changed; reloading nginx"
                nginx -s reload || true
                last=$now
            fi
        done
    ) &
    watcher=$!
fi

stopping=
stop() {
    stopping=1
    kill -TERM "$server" 2>/dev/null || true
    kill -QUIT "$proxy" 2>/dev/null || true # nginx's graceful shutdown
    [ -z "$watcher" ] || kill "$watcher" 2>/dev/null || true
}
trap stop TERM INT

set +e
wait -n "$server" "$proxy"
status=$?
if [ -z "$stopping" ]; then
    if kill -0 "$server" 2>/dev/null; then
        say "nginx stopped (exit status $status); stopping the server too"
    else
        say "the server stopped (exit status $status); stopping nginx too"
    fi
    stop
    wait "$server" "$proxy"
    exit 1
fi
wait "$server" "$proxy"
exit 0
