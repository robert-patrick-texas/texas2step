"""What the server logs: quiet by default, verbose with ``debug = true``.

uvicorn applies :func:`config` in every worker. Lines go to stderr, which systemd sends
to the journal, each with its level as a syslog priority in front for journald to file
it by. One line per request comes from texas2step (``app.RequestLog``), with the
client's real address behind nginx: successes at INFO and failures at WARNING, so the
``texas2step`` logger's level decides which are written. Each page's or command-line run's
start, change of page and end (clients.py) is written at INFO either way.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from uvicorn.logging import DefaultFormatter

# uvicorn's own WebSocket lines, by their format strings. They show the proxy's address,
# and RequestLog writes the same events with the client's.
UVICORN_WEBSOCKET = ('"WebSocket %s"', "connection open", "connection closed")


class NoUvicornWebSockets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not any(text in str(record.msg) for text in UVICORN_WEBSOCKET)


def to_journal() -> bool:
    """Whether stderr is systemd's stream to the journal, which it names in JOURNAL_STREAM
    as device:inode. A program the service starts inherits the variable, not always the
    stream."""
    try:
        device, inode = (int(n) for n in os.environ["JOURNAL_STREAM"].split(":"))
        stat = os.fstat(sys.stderr.fileno())
    except (KeyError, ValueError, OSError, AttributeError):
        return False
    return (stat.st_dev, stat.st_ino) == (device, inode)


def priority(level: int) -> int:
    """The syslog priority of a logging level: 2 critical, 3 error, 4 warning, 6 info, 7 debug."""
    for floor, value in ((logging.CRITICAL, 2), (logging.ERROR, 3), (logging.WARNING, 4)):
        if level >= floor:
            return value
    return 6 if level >= logging.INFO else 7


class Formatter(DefaultFormatter):
    """uvicorn's, with ``journal``: each line starts with its priority, as ``<4>``, which
    journald files it by and drops (sd-daemon(3)). Without it, journald files every line
    as info, and ``journalctl -p warning`` finds nothing. A traceback goes as many lines,
    so each gets one."""

    def __init__(self, *args: Any, journal: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.journal = journal

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.journal:
            return text
        prefix = f"<{priority(record.levelno)}>"
        return "\n".join(prefix + line for line in text.split("\n"))


def config(debug: bool) -> dict[str, Any]:
    """A logging.config.dictConfig for uvicorn.run."""
    level = "DEBUG" if debug else "WARNING"
    journal = to_journal()

    def logger(handler: str, level: str | None = None) -> dict[str, Any]:
        return {"handlers": [handler], "propagate": False} | ({"level": level} if level else {})

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"no_websockets": {"()": NoUvicornWebSockets}},
        "formatters": {
            "plain": {
                "()": Formatter,
                "fmt": "%(levelprefix)s %(message)s",
                "use_colors": None,
                "journal": journal,
            },
            "named": {
                "()": Formatter,
                "fmt": "%(levelprefix)s %(name)s: %(message)s",
                "use_colors": None,
                "journal": journal,
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
            # Each session's start, change of page and end (clients.py), with or without debug.
            "texas2step.clients": logger("plain", "DEBUG" if debug else "INFO"),
            # The UDP probe's libraries. Their DEBUG lines for STUN and SCTP come once per
            # packet, too many even here: debug gets ICE's checks and the DTLS, SCTP and
            # data channel state changes.
            "aioice": logger("named", "INFO" if debug else "WARNING"),
            "aiortc": logger("named", level),
            "aiortc.rtcsctptransport": {"level": "INFO" if debug else "WARNING"},
        },
        # Any other library's warnings and errors, such as asyncio's "Task exception was
        # never retrieved", which logging's last resort used to write without a level.
        "root": {"handlers": ["named"], "level": "WARNING"},
    }
