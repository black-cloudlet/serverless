"""Logging configuration."""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging to stdout with a single line formatter.

    Args:
        level: The root log level (e.g. "INFO", "DEBUG").
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return the module logger for ``name`` (a thin ``logging.getLogger`` wrapper).

    Args:
        name: The logger name, typically ``__name__``.

    Returns:
        The named logger.
    """
    return logging.getLogger(name)
