"""Azure App Service WebJob entry point.

Deliberately thin, like `aws_lambda.handler`: the scheduled WebJob's `run.py`
calls `run()`, which calls the same `main()` the CLI uses, so there is exactly
one code path to reason about.

Why App Service and not Azure Functions: every function app needs a storage
account, and the landing zone denies public access to storage, which would
force VNet integration and private endpoints. An App Service app needs no
storage account of ours; files live on its built-in persistent /home.

Telemetry is the one thing this module adds. A WebJob's stdout reaches only the
WebJob's own log, which no alert can query. So when
APPLICATIONINSIGHTS_CONNECTION_STRING is set, log records also go to
Application Insights through OpenTelemetry, as the identity named by
AZURE_CLIENT_ID (the Application Insights resource has local auth disabled).
"""

from __future__ import annotations

import logging
import os
import sys

from .__main__ import main
from .logging_setup import adopt_host_handlers, reset_run_id

log = logging.getLogger(__name__)

# Logs only. The distro would otherwise also emit a dependency row for every
# Graph and ServiceNow request, which nothing queries.
_DISABLED_INSTRUMENTATIONS = (
    "azure_sdk",
    "django",
    "fastapi",
    "flask",
    "httpx",
    "httpx2",
    "psycopg2",
    "requests",
    "urllib",
    "urllib3",
)


def _configure_telemetry() -> bool:
    """Send log records to Application Insights. Returns False when not configured."""
    if not os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return False

    from azure.identity import ManagedIdentityCredential
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        credential=ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID")),
        instrumentation_options={name: {"enabled": False} for name in _DISABLED_INSTRUMENTATIONS},
        enable_live_metrics=False,
    )
    return True


def _flush_telemetry() -> None:
    """Export buffered telemetry before the process exits.

    Records are exported in batches on a background thread. A WebJob process
    exits as soon as run() returns, and anything still buffered is lost. That
    includes the `run complete` line, whose absence fires the no-successful-run
    alert.
    """
    from opentelemetry._logs import get_logger_provider

    provider = get_logger_provider()
    for action in ("force_flush", "shutdown"):
        method = getattr(provider, action, None)
        if callable(method):
            method()


def run() -> int:
    """Run one sync as a WebJob and return its exit code."""
    telemetry = _configure_telemetry()
    # Also keep stdout, so the WebJob's own run history shows the output.
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    # Must precede main(): main() configures logging, and without this it would
    # replace the OpenTelemetry handler with a stdout one.
    adopt_host_handlers()
    # One process per run today, but a fresh id costs nothing and keeps run()
    # correct if it is ever called twice in a process.
    reset_run_id()
    try:
        return main([])
    finally:
        if telemetry:
            _flush_telemetry()
