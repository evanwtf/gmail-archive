"""Logging configuration for every entry point (CLI and web)."""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def configure(level: str = "INFO") -> None:
    logging.basicConfig(level=level.upper(), format=_FORMAT)
