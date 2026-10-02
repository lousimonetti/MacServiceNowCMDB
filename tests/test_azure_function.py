"""The Azure Functions entry point, and the logging it depends on.

The worker's root handler is the only route a log record has to Application
Insights, and both alert rules parse the JSON `run complete` line from there. A
connector that replaced that handler, as configure_logging does everywhere else,
would run with no telemetry and both alerts blind.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from intune_cmdb_sync import azure_function, logging_setup
from intune_cmdb_sync.__main__ import EXIT_OK, EXIT_PARTIAL
from intune_cmdb_sync.azure_function import SyncRunFailed, run
from intune_cmdb_sync.logging_setup import configure_logging, current_run_id


class _WorkerHandler(logging.Handler):
    """Stands in for the Functions worker's AsyncLoggingHandler, which sends
    `self.format(record)` as the trace message."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


@pytest.fixture
def worker_handler(monkeypatch: pytest.MonkeyPatch) -> Iterator[_WorkerHandler]:
    root = logging.getLogger()
    saved = list(root.handlers)
    handler = _WorkerHandler()
    root.handlers[:] = [handler]
    monkeypatch.setattr(logging_setup, "_host_handlers", ())
    yield handler
    root.handlers[:] = saved


class TestHostHandlers:
    def test_configure_logging_keeps_an_adopted_handler(self, worker_handler):
        logging_setup.adopt_host_handlers()
        configure_logging("INFO", "json")
        handlers = logging.getLogger().handlers
        # pytest attaches its own capture handlers too; every one is kept.
        assert worker_handler in handlers
        assert handlers == list(logging_setup._host_handlers)
        assert not any(type(h) is logging.StreamHandler for h in handlers)

    def test_adopted_handler_emits_the_json_line_the_alerts_parse(self, worker_handler):
        logging_setup.adopt_host_handlers()
        configure_logging("INFO", "json")
        logging.getLogger("x").info("run complete", extra={"errors": 0, "degraded": []})
        line = json.loads(worker_handler.messages[-1])
        assert line["msg"] == "run complete"
        assert line["errors"] == 0 and line["degraded"] == []
        assert line["run_id"] == current_run_id()

    def test_repeated_runs_do_not_stack_filters(self, worker_handler):
        """A warm instance calls configure_logging once per invocation."""
        logging_setup.adopt_host_handlers()
        for _ in range(3):
            configure_logging("INFO", "json")
        assert len(worker_handler.filters) == 1

    def test_adoption_is_captured_once(self, worker_handler):
        """A second capture after a stdout configure would adopt the connector's
        own handler and lose the worker's."""
        logging_setup.adopt_host_handlers()
        configure_logging("INFO", "json")
        adopted = logging_setup._host_handlers
        stray = logging.StreamHandler()
        logging.getLogger().handlers[:] = [stray]
        logging_setup.adopt_host_handlers()
        configure_logging("INFO", "json")
        assert logging_setup._host_handlers is adopted
        assert worker_handler in logging.getLogger().handlers
        assert stray not in logging.getLogger().handlers

    def test_without_adoption_the_cli_still_replaces_handlers(self, worker_handler):
        configure_logging("INFO", "json")
        handlers = logging.getLogger().handlers
        assert worker_handler not in handlers and len(handlers) == 1


class TestRun:
    def test_success_returns_ok(self, worker_handler, monkeypatch):
        monkeypatch.setattr(azure_function, "main", lambda argv: EXIT_OK)
        assert run() == EXIT_OK

    def test_failure_raises_with_the_exit_code(self, worker_handler, monkeypatch):
        """Raising marks the invocation Failed. A timer trigger has no retry
        policy here, so it cannot rerun the sync."""
        monkeypatch.setattr(azure_function, "main", lambda argv: EXIT_PARTIAL)
        with pytest.raises(SyncRunFailed) as caught:
            run()
        assert caught.value.exit_code == EXIT_PARTIAL

    def test_adopts_the_worker_handler_before_main_runs(self, worker_handler, monkeypatch):
        seen: list[tuple[logging.Handler, ...]] = []
        monkeypatch.setattr(
            azure_function, "main", lambda argv: seen.append(logging_setup._host_handlers) or 0
        )
        run()
        assert len(seen) == 1 and worker_handler in seen[0]

    def test_each_invocation_gets_its_own_run_id(self, worker_handler, monkeypatch):
        ids: list[str] = []
        monkeypatch.setattr(azure_function, "main", lambda argv: ids.append(current_run_id()) or 0)
        run()
        run()
        assert len(set(ids)) == 2

    def test_passes_no_arguments_by_default(self, worker_handler, monkeypatch):
        """Everything comes from app settings, like the container did."""
        seen: list[list[str]] = []
        monkeypatch.setattr(azure_function, "main", lambda argv: seen.append(argv) or 0)
        run()
        assert seen == [[]]


def test_lambda_gives_each_invocation_its_own_run_id(monkeypatch):
    """Same warm-process problem as Functions: without a reset, every run a
    container serves shares one run_id and their logs cannot be told apart."""
    from intune_cmdb_sync import aws_lambda

    ids: list[str] = []
    monkeypatch.setattr(aws_lambda, "main", lambda argv: ids.append(current_run_id()) or 0)
    aws_lambda.handler({})
    aws_lambda.handler({})
    assert len(set(ids)) == 2
