"""The App Service WebJob entry point, and the telemetry it depends on.

The OpenTelemetry handler on the root logger is the only route a log record has
to Application Insights, and both alert rules parse the JSON `run complete` line
from there. A connector that replaced that handler, as configure_logging does
everywhere else, would run with no telemetry and both alerts blind.
"""

from __future__ import annotations

import json
import logging
import warnings
from collections.abc import Iterator

import pytest

from intune_cmdb_sync import appservice_job, logging_setup
from intune_cmdb_sync.__main__ import EXIT_OK, EXIT_PARTIAL
from intune_cmdb_sync.logging_setup import configure_logging, current_run_id


class _CollectingHandler(logging.Handler):
    """A host handler that keeps what it was asked to emit, formatted."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


@pytest.fixture
def host_handler(monkeypatch: pytest.MonkeyPatch) -> Iterator[_CollectingHandler]:
    root = logging.getLogger()
    saved = list(root.handlers)
    handler = _CollectingHandler()
    root.handlers[:] = [handler]
    monkeypatch.setattr(logging_setup, "_host_handlers", ())
    yield handler
    root.handlers[:] = saved


class TestHostHandlers:
    def test_configure_logging_keeps_an_adopted_handler(self, host_handler):
        logging_setup.adopt_host_handlers()
        configure_logging("INFO", "json")
        handlers = logging.getLogger().handlers
        # pytest attaches its own capture handlers too; every one is kept.
        assert host_handler in handlers
        assert handlers == list(logging_setup._host_handlers)

    def test_adopted_handler_emits_the_json_line_the_alerts_parse(self, host_handler):
        logging_setup.adopt_host_handlers()
        configure_logging("INFO", "json")
        logging.getLogger("x").info("run complete", extra={"errors": 0, "degraded": []})
        line = json.loads(host_handler.messages[-1])
        assert line["msg"] == "run complete"
        assert line["errors"] == 0 and line["degraded"] == []
        assert line["run_id"] == current_run_id()

    def test_repeated_configuration_does_not_stack_filters(self, host_handler):
        logging_setup.adopt_host_handlers()
        for _ in range(3):
            configure_logging("INFO", "json")
        assert len(host_handler.filters) == 2  # run id + redaction, once each

    def test_adoption_is_captured_once(self, host_handler):
        """A second capture after a stdout configure would adopt the connector's
        own handler and lose the host's."""
        logging_setup.adopt_host_handlers()
        configure_logging("INFO", "json")
        adopted = logging_setup._host_handlers
        stray = logging.StreamHandler()
        logging.getLogger().handlers[:] = [stray]
        logging_setup.adopt_host_handlers()
        configure_logging("INFO", "json")
        assert logging_setup._host_handlers is adopted
        assert host_handler in logging.getLogger().handlers
        assert stray not in logging.getLogger().handlers

    def test_without_adoption_the_cli_still_replaces_handlers(self, host_handler):
        configure_logging("INFO", "json")
        handlers = logging.getLogger().handlers
        assert host_handler not in handlers and len(handlers) == 1


class TestTelemetryShape:
    """Pins what actually reaches Application Insights, using the same handler
    class configure_azure_monitor installs. The alert queries depend on it."""

    @pytest.fixture
    def exported(self, monkeypatch: pytest.MonkeyPatch):
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import (
            InMemoryLogRecordExporter,
            SimpleLogRecordProcessor,
        )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from opentelemetry.instrumentation.logging.handler import LoggingHandler

        exporter = InMemoryLogRecordExporter()
        provider = LoggerProvider()
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers[:] = [LoggingHandler(logger_provider=provider)]
        monkeypatch.setattr(logging_setup, "_host_handlers", ())
        logging_setup.adopt_host_handlers()
        configure_logging("INFO", "json")
        yield exporter
        root.handlers[:] = saved

    def test_trace_message_is_the_json_line(self, exported):
        """AppTraces.Message = this body. The alerts run parse_json(Message)."""
        logging.getLogger("x").info("run complete", extra={"errors": 2, "degraded": ["x"]})
        body = exported.get_finished_logs()[-1].log_record.body
        line = json.loads(body)
        assert line["msg"] == "run complete"
        assert line["errors"] == 2 and line["degraded"] == ["x"]

    def test_extra_fields_are_redacted_in_attributes_too(self, exported):
        """The handler copies extras into attributes (customDimensions) without
        the formatter. The redaction filter must have masked them first."""
        logging.getLogger("x").info("auth", extra={"client_secret": "hunter2", "count": 3})
        record = exported.get_finished_logs()[-1].log_record
        assert record.attributes["client_secret"] == "***"
        assert record.attributes["count"] == 3
        assert "hunter2" not in record.body


class TestRun:
    @pytest.fixture(autouse=True)
    def _isolated_root(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        root = logging.getLogger()
        saved = list(root.handlers)
        monkeypatch.setattr(logging_setup, "_host_handlers", ())
        yield
        root.handlers[:] = saved

    def test_returns_mains_exit_code(self, monkeypatch):
        """run.py passes it to sys.exit, so a failed sync is a failed WebJob run."""
        monkeypatch.setattr(appservice_job, "main", lambda argv: EXIT_PARTIAL)
        assert appservice_job.run() == EXIT_PARTIAL

    def test_passes_no_arguments(self, monkeypatch):
        """Everything comes from app settings."""
        seen: list[list[str]] = []
        monkeypatch.setattr(appservice_job, "main", lambda argv: seen.append(argv) or EXIT_OK)
        appservice_job.run()
        assert seen == [[]]

    def test_no_telemetry_without_a_connection_string(self, monkeypatch):
        monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
        flushed: list[bool] = []
        monkeypatch.setattr(appservice_job, "_flush_telemetry", lambda: flushed.append(True))
        monkeypatch.setattr(appservice_job, "main", lambda argv: EXIT_OK)
        assert appservice_job.run() == EXIT_OK
        assert flushed == []

    def test_flushes_telemetry_even_when_main_raises(self, monkeypatch):
        """An unflushed `run complete` line fires the absence alert."""
        monkeypatch.setattr(appservice_job, "_configure_telemetry", lambda: True)
        flushed: list[bool] = []
        monkeypatch.setattr(appservice_job, "_flush_telemetry", lambda: flushed.append(True))

        def boom(argv: list[str]) -> int:
            raise RuntimeError("crash")

        monkeypatch.setattr(appservice_job, "main", boom)
        with pytest.raises(RuntimeError):
            appservice_job.run()
        assert flushed == [True]

    def test_adopts_handlers_before_main_runs_and_keeps_stdout(self, monkeypatch):
        """Adoption must precede main(), and stdout stays so the WebJob's own
        run history shows output."""
        seen: list[tuple[logging.Handler, ...]] = []
        monkeypatch.setattr(
            appservice_job, "main", lambda argv: seen.append(logging_setup._host_handlers) or 0
        )
        appservice_job.run()
        assert len(seen) == 1
        assert any(type(h) is logging.StreamHandler for h in seen[0])

    def test_each_run_gets_a_fresh_run_id(self, monkeypatch):
        ids: list[str] = []
        monkeypatch.setattr(appservice_job, "main", lambda argv: ids.append(current_run_id()) or 0)
        appservice_job.run()
        appservice_job.run()
        assert len(set(ids)) == 2

    def test_telemetry_skips_per_request_instrumentation(self):
        """The connector's HTTP client is httpx; a dependency row per Graph and
        ServiceNow request would be noise nobody queries."""
        assert "httpx" in appservice_job._DISABLED_INSTRUMENTATIONS
        assert "requests" in appservice_job._DISABLED_INSTRUMENTATIONS


def test_lambda_gives_each_invocation_its_own_run_id(monkeypatch):
    """A warm Lambda container runs several invocations in one process; each is
    its own run and must not share a run_id."""
    from intune_cmdb_sync import aws_lambda

    ids: list[str] = []
    monkeypatch.setattr(aws_lambda, "main", lambda argv: ids.append(current_run_id()) or 0)
    aws_lambda.handler({})
    aws_lambda.handler({})
    assert len(set(ids)) == 2
