"""Logging configuration."""

from __future__ import annotations

import logging
import sys

from common.requestid import get_request_id


class _RequestIdFilter(logging.Filter):
    """Attach the current request's correlation id to every log record.

    So a ``requestId`` returned in an error envelope can be grepped straight out
    of the logs. Records outside a request scope (startup, background) carry "-".
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Set ``record.request_id`` unless the caller supplied one; keep the record."""
        if not getattr(record, "request_id", None):
            record.request_id = get_request_id()
        return True


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging to stdout with a single line formatter."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")
    )
    handler.addFilter(_RequestIdFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return the module logger for ``name`` (a thin ``logging.getLogger`` wrapper)."""
    return logging.getLogger(name)
