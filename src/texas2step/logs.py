"""What the server logs: quiet by default, verbose with ``debug = true``.

uvicorn applies :func:`config` in every worker. Lines go to stderr, which systemd sends
to the journal. One line per request comes from texas2step (``app.RequestLog``), with the
client's real address behind nginx: successes at INFO and failures at WARNING, so the
``texas2step`` logger's level decides which are written.
"""

from __future__ import annotations

import logging
from typing import Any

# uvicorn's own WebSocket lines, by their format strings. They show the proxy's address,
# and RequestLog writes the same events with the client's.
UVICORN_WEBSOCKET = ('"WebSocket %s"', "connection open", "connection closed")


class NoUvicornWebSockets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not any(text in str(record.msg) for text in UVICORN_WEBSOCKET)


def config(debug: bool) -> dict[str, Any]:
    """A logging.config.dictConfig for uvicorn.run."""
    level = "DEBUG" if debug else "WARNING"

    def logger(handler: str, level: str | None = None) -> dict[str, Any]:
        return {"handlers": [handler], "propagate": False} | ({"level": level} if level else {})

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"no_websockets": {"()": NoUvicornWebSockets}},
        "formatters": {
            "plain": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
                "use_colors": None,
            },
            "named": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(name)s: %(message)s",
                "use_colors": None,
            },
        },
        "handlers": {
            "plain": {
                "class": "logging.StreamHandler",
                "formatter": "plain",
                "stream": "ext://sys.stderr",
            },
            "named": {
                "class": "logging.StreamHandler",
                "formatter": "named",
                "stream": "ext://sys.stderr",
            },
        },
        "loggers": {
            # Start, stop and worker events stay; uvicorn's request lines are turned off
            # (access_log=False), and its WebSocket lines dropped for RequestLog's.
            "uvicorn": logger("plain", "INFO"),
            "uvicorn.error": {"level": "INFO", "filters": ["no_websockets"]},
            "texas2step": logger("named", level),
            "texas2step.access": logger("plain"),  # its level is texas2step's
            # The UDP probe's libraries. Their DEBUG lines for STUN and SCTP come once per
            # packet, too many even here: debug gets ICE's checks and the DTLS, SCTP and
            # data channel state changes.
            "aioice": logger("named", "INFO" if debug else "WARNING"),
            "aiortc": logger("named", level),
            "aiortc.rtcsctptransport": {"level": "INFO" if debug else "WARNING"},
        },
    }
