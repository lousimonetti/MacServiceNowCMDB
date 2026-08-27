"""Structured logging.

JSON by default so Azure Log Analytics / CloudWatch Insights can query fields
directly; `LOG_FORMAT=text` gives readable output when running locally.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from typing import Any

# Attributes present on every LogRecord; anything else was attached by the
# caller via `extra=` and belongs in the structured payload.
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
) | {"asctime", "message", "taskName"}

_REDACT_KEYS = ("secret", "password", "token", "authorization", "client_secret", "apikey")

# One id per process, stamped on every log record and carried in the run report.
# Without it there is no way to group a run's lines in a log store that holds
# weeks of them, or to tie a report back to the run that produced it.
_run_id: str | None = None


def current_run_id() -> str:
    """The id for this run, generated on first use."""
    global _run_id
    if _run_id is None:
        _run_id = uuid.uuid4().hex[:12]
    return _run_id


def reset_run_id(value: str | None = None) -> str:
    """Start a new run id. Called at startup, and by tests wanting determinism."""
    global _run_id
    _run_id = value or uuid.uuid4().hex[:12]
    return _run_id


class _RunIdFilter(logging.Filter):
    """Stamps run_id onto every record, including ones from libraries."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = current_run_id()
        return True


def _redact(key: str, value: Any) -> Any:
    if any(marker in key.lower() for marker in _REDACT_KEYS):
        return "***"
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = _redact(key, value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s  %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: _redact(k, v) for k, v in record.__dict__.items() if k not in _RESERVED
        }
        if extras:
            rendered = " ".join(f"{k}={v}" for k, v in extras.items())
            return f"{base}  [{rendered}]"
        return base


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(_RunIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # These libraries log a line per request at INFO, which drowns the run log.
    for noisy in ("httpx", "httpcore", "azure.identity", "azure.core.pipeline"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
