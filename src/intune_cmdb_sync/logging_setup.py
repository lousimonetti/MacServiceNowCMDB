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
    """Start a new run id. Called per invocation by hosts that keep a process warm
    between runs (Lambda, Functions), and by tests wanting determinism."""
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


# Handlers a host process installed before the connector ran, kept rather than
# replaced. See adopt_host_handlers.
_host_handlers: tuple[logging.Handler, ...] = ()
_run_id_filter = _RunIdFilter()


class _RedactFilter(logging.Filter):
    """Masks secret-looking `extra=` fields on the record itself.

    JsonFormatter and TextFormatter redact when they render, but an OpenTelemetry
    handler also copies every extra field into the telemetry's attributes
    without going through the formatter. Masking the record covers both.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED:
                masked = _redact(key, value)
                if masked is not value:
                    record.__dict__[key] = masked
        return True


_redact_filter = _RedactFilter()


def adopt_host_handlers() -> None:
    """Keep the root handlers already installed, instead of replacing them.

    On Azure App Service the WebJob entry point (appservice_job.py) installs an
    OpenTelemetry handler on the root logger, and it is the only path a log
    record has to Application Insights. Replacing it, as configure_logging does
    everywhere else, would leave the job running with no telemetry and both
    alerts blind. That handler sends `handler.format(record)` as the trace
    message when a formatter is set, so giving it the JSON formatter keeps the
    one-line summary the alerts parse.

    Idempotent, and a no-op once captured: later calls would otherwise capture
    the stdout handler a previous configure_logging installed.
    """
    global _host_handlers
    if not _host_handlers:
        _host_handlers = tuple(logging.getLogger().handlers)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    formatter = JsonFormatter() if fmt == "json" else TextFormatter()
    root = logging.getLogger()

    if _host_handlers:
        root.handlers[:] = list(_host_handlers)
    else:
        root.handlers.clear()
        root.addHandler(logging.StreamHandler(sys.stdout))
    for handler in root.handlers:
        handler.setFormatter(formatter)
        # One shared instance, so a warm host calling this every run does not
        # stack a filter per invocation.
        handler.addFilter(_run_id_filter)
        handler.addFilter(_redact_filter)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # These libraries log a line per request at INFO, which drowns the run log.
    # The Azure Monitor exporter logs "Transmission succeeded" per batch, and
    # with its own handler on the root logger each line is exported, which logs
    # again: an endless loop that kept the WebJob from ever reaching the sync.
    for noisy in (
        "httpx",
        "httpcore",
        "azure.identity",
        "azure.core.pipeline",
        "azure.monitor.opentelemetry.exporter",
        "opentelemetry",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
