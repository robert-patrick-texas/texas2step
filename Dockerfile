# texas2step's container image: the server and nginx in one image, for Docker and Podman.
# docs/container.md says how to run it; .github/workflows/container.yml builds and publishes
# it for amd64 and arm64 with each release.
#
#   docker build -t texas2step .        # or: make image
#
# Built on Ubuntu 26.04 like the Debian package, so the image runs the same Python and
# nginx. The web UI is prebuilt under src/texas2step/static, so Node is not needed.

ARG UBUNTU=ubuntu:26.04

# --- 1. The application and its locked dependencies in a virtualenv ----------------------
# uv, pinned by digest as the workflows pin their actions.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM ${UBUNTU} AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE.md ./
COPY src ./src
# The same steps as packaging/build-deb.sh: exactly the versions in uv.lock, then the
# wheel built from this source.
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_NO_CACHE=1
RUN uv build --quiet --wheel --out-dir /tmp/wheel \
    && uv export --quiet --frozen --no-default-groups --no-emit-project \
        --format requirements-txt --output-file /tmp/requirements.txt \
    && uv venv --quiet --relocatable --python /usr/bin/python3 /opt/texas2step/venv \
    && uv pip install --quiet --python /opt/texas2step/venv/bin/python -r /tmp/requirements.txt \
    && uv pip install --quiet --python /opt/texas2step/venv/bin/python --no-deps --no-index \
        --find-links /tmp/wheel texas2step \
    && rm -f /opt/texas2step/venv/.lock /opt/texas2step/venv/.gitignore \
    && /opt/texas2step/venv/bin/python -m compileall -q -j 0 /opt/texas2step/venv/lib

# --- 2. The image ------------------------------------------------------------------------
FROM ${UBUNTU}

# python3: the virtualenv runs on it. nginx: TLS, HTTP/2 and HTTP/3 in front of the
# server, as with the package. tini: PID 1, which hands signals on and reaps processes.
# openssl: a self-signed certificate when none is mounted.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends python3 nginx tini openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/* \
    # The service's own account, with fixed numbers so the volumes keep their owner
    # from one image to the next.
    && groupadd --system --gid 990 texas2step \
    && useradd --system --uid 990 --gid 990 --home-dir /var/lib/texas2step --no-create-home \
        --shell /usr/sbin/nologin texas2step \
    # nginx logs to the container's output.
    && ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log

COPY --from=build /opt/texas2step/venv /opt/texas2step/venv
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
