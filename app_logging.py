"""Structured logging helpers.

Provides:
  * configure_logging()   - one-time root logger setup honouring LOG_LEVEL +
                            LOG_JSON env vars (JSON formatter for production,
                            human-friendly otherwise).
  * REQUEST_ID            - contextvar populated by the request_id middleware so
                            log records emitted during a request automatically
                            carry the right correlation id.
  * RequestIdFilter       - injects request_id into every LogRecord.

Designed so existing code can simply replace ``print(...)`` with ``log.info(...)``
without learning a new API. Falls back to noisy-but-safe defaults if env vars
are missing.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from typing import Optional


REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.request_id = REQUEST_ID.get()
        except LookupError:
            record.request_id = "-"
        return True


class JsonFormatter(logging.Formatter):
    """One-line JSON per log record. Cheap to grep, cheap to ship to anything."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Surface any extras the caller passed via logger.<level>(msg, extra={...}).
        for key, value in record.__dict__.items():
            if key in payload or key.startswith("_"):
                continue
            if key in (
                "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
                "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
                "created", "msecs", "relativeCreated", "thread", "threadName",
                "processName", "process",
            ):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        return json.dumps(payload, separators=(",", ":"), default=str)


_CONFIGURED = False


def configure_logging(level: Optional[str] = None, json_mode: Optional[bool] = None) -> None:
    """Idempotent root-logger setup. Safe to call from every entry point."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    lvl_name = (level or os.getenv("LOG_LEVEL", "INFO")).strip().upper()
    lvl = getattr(logging, lvl_name, logging.INFO)
    use_json = json_mode if json_mode is not None else os.getenv("LOG_JSON", "0").strip() == "1"

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    if use_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))

    root = logging.getLogger()
    # Replace existing handlers so we don't double-log when uvicorn also configures.
    root.handlers[:] = [handler]
    root.setLevel(lvl)

    # Tame uvicorn's default access log unless explicitly asked for DEBUG.
    if lvl > logging.DEBUG:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def new_request_id() -> str:
    """Return a short request id suitable for response headers + log correlation."""
    return uuid.uuid4().hex[:12]


def get_logger(name: str) -> logging.Logger:
    """Convenience accessor; configures on first use so callers don't have to."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)


__all__ = (
    "REQUEST_ID",
    "RequestIdFilter",
    "JsonFormatter",
    "configure_logging",
    "new_request_id",
    "get_logger",
)
