# texas2step's container image: the server and nginx in one image, for Docker and Podman.
# docs/container.md says how to run it; .github/workflows/container.yml builds and publishes
# it for amd64 and arm64 with each release.
#
#   docker build -t texas2step .        # or: make image
#
# Built on Ubuntu 26.04 like the Debian package, with the same nginx, and the same Python
# and packages under /opt/texas2step: packaging/build-runtime.sh makes both. The web UI
# is prebuilt under src/texas2step/static, so Node is not needed.

ARG UBUNTU=ubuntu:26.04

# --- 1. texas2step's own Python, and the application with its locked dependencies -------
# uv, pinned by digest as the workflows pin their actions.
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv
FROM ${UBUNTU} AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /src
COPY .python-version pyproject.toml uv.lock README.md LICENSE.md ./
COPY src ./src
COPY packaging/build-runtime.sh ./packaging/
# Python only as build-runtime.sh asks for it, from .python-version.
ENV UV_PYTHON_DOWNLOADS=manual UV_LINK_MODE=copy UV_NO_CACHE=1
RUN packaging/build-runtime.sh /opt/texas2step \
    && /opt/texas2step/venv/bin/python -m compileall -q -j 0 /opt/texas2step/python/lib \
        /opt/texas2step/venv/lib

# --- 2. The image ------------------------------------------------------------------------
FROM ${UBUNTU}

# nginx: TLS, HTTP/2 and HTTP/3 in front of the server, as with the package. tini: PID 1,
# which hands signals on and reaps processes. openssl: a self-signed certificate when
# none is mounted. Python comes with texas2step, under /opt/texas2step; tzdata, netbase
# and media-types are what it reads from the system, as the package depends on them.
RUN apt-get update \
    && apt-get upgrade -y \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        nginx tini openssl ca-certificates tzdata netbase media-types \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/* \
    # The service's own account, with fixed numbers so the volumes keep their owner
    # from one image to the next.
    && groupadd --system --gid 990 texas2step \
    && useradd --system --uid 990 --gid 990 --home-dir /var/lib/texas2step --no-create-home \
        --shell /usr/sbin/nologin texas2step \
    # nginx logs to the container's output.
    && ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log

COPY --from=build /opt/texas2step /opt/texas2step
RUN ln -s /opt/texas2step/venv/bin/texas2step /usr/bin/texas2step

# nginx: the package's own snippets, unchanged, and this image's main file.
COPY deploy/nginx/texas2step-log.conf /etc/nginx/conf.d/texas2step-log.conf
COPY deploy/nginx/texas2step.conf deploy/nginx/texas2step-speedtest.conf /etc/nginx/snippets/
COPY deploy/nginx/texas2step-http.conf /usr/share/texas2step/nginx-upstream.conf
COPY deploy/container/nginx.conf /etc/nginx/nginx.conf
COPY src/texas2step/config_template.toml /usr/share/texas2step/config.toml
COPY deploy/container/entrypoint.sh /usr/lib/texas2step/entrypoint
COPY deploy/container/healthcheck.py /usr/lib/texas2step/healthcheck
# Tells the command line it runs in this image: texas2step update and the restart hints
# then say to replace or restart the container instead.
RUN echo "ghcr.io/robert-patrick-texas/texas2step" > /usr/share/texas2step/container

ENV TEXAS2STEP_CONFIG=/etc/texas2step/config.toml \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1

# Settings, the access key and TLS files; results, lockouts, traffic and CPU figures.
VOLUME ["/etc/texas2step", "/var/lib/texas2step"]
WORKDIR /var/lib/texas2step

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s CMD ["/usr/lib/texas2step/healthcheck"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/lib/texas2step/entrypoint"]

ARG VERSION=dev
LABEL org.opencontainers.image.title="texas2step" \
      org.opencontainers.image.description="Network measurement server: throughput, latency, jitter, packet loss and call quality, measured from the browser" \
      org.opencontainers.image.url="https://github.com/robert-patrick-texas/texas2step" \
      org.opencontainers.image.source="https://github.com/robert-patrick-texas/texas2step" \
      org.opencontainers.image.documentation="https://robert-patrick-texas.github.io/texas2step/docs/container.html" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}"
