#!/opt/texas2step/venv/bin/python
"""The container's health check (/usr/lib/texas2step/healthcheck in the image).

Healthy when the texas2step server answers on LISTEN_PORT and, unless HTTPS_PORT is 0,
nginx answers for it on HTTPS_PORT. Docker runs it through the image's HEALTHCHECK;
Podman, which reads no HEALTHCHECK from an OCI image, through HealthCmd in
deploy/container/texas2step.container.
"""

import os
import ssl
import sys
import urllib.request


def answers(url: str, context: ssl.SSLContext | None = None) -> None:
    try:
        with urllib.request.urlopen(url, timeout=4, context=context) as reply:
            reply.read()
    except OSError as exc:
        sys.exit(f"texas2step: no answer from {url}: {exc}")


answers(f"http://127.0.0.1:{os.environ.get('LISTEN_PORT') or 8080}/api/v1/health")
https = os.environ.get("HTTPS_PORT") or "443"
if int(https) != 0:
    # Its own certificate, whatever it is: this checks that nginx serves the site.
    unchecked = ssl.create_default_context()
    unchecked.check_hostname = False
    unchecked.verify_mode = ssl.CERT_NONE
    answers(f"https://127.0.0.1:{int(https)}/texas2step/api/v1/health", unchecked)
