from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest

from intune_cmdb_sync.logging_setup import JsonFormatter, TextFormatter, configure_logging

SRC = Path(__file__).resolve().parents[1] / "src" / "intune_cmdb_sync"

RESERVED = set(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
) | {"message", "asctime", "taskName"}


def _extra_keys_in(path: Path) -> set[str]:
    """Collect every literal key passed as `extra={...}` in a module."""
    keys: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                continue
            for key in keyword.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: p.name)
def test_no_log_extra_key_shadows_a_logrecord_attribute(path: Path):
    """`logging` raises KeyError if `extra` reuses a LogRecord attribute name.

    That turns a diagnostic log line into a crash, so it must be caught here
    rather than in production.
    """
    clashes = _extra_keys_in(path) & RESERVED
    assert not clashes, f"{path.name} passes reserved logging keys via extra=: {sorted(clashes)}"


class TestJsonFormatter:
    def test_includes_structured_extras(self):
        record = logging.LogRecord(
            "test", logging.INFO, "f.py", 1, "hello", None, None
        )
        record.device_count = 42
        payload = json.loads(JsonFormatter().format(record))
        assert payload["msg"] == "hello"
        assert payload["level"] == "INFO"
        assert payload["device_count"] == 42

    @pytest.mark.parametrize(
        "key", ["client_secret", "password", "api_token", "Authorization", "SNOW_SECRET"]
    )
    def test_redacts_credential_shaped_keys(self, key):
        record = logging.LogRecord("test", logging.INFO, "f.py", 1, "hi", None, None)
        setattr(record, key, "super-secret-value")
        payload = json.loads(JsonFormatter().format(record))
        assert payload[key] == "***"
        assert "super-secret-value" not in json.dumps(payload)

    def test_non_serialisable_values_do_not_raise(self):
        record = logging.LogRecord("test", logging.INFO, "f.py", 1, "hi", None, None)
        record.thing = object()
        assert json.loads(JsonFormatter().format(record))["thing"].startswith("<object")


class TestTextFormatter:
    def test_appends_extras_in_brackets(self):
        record = logging.LogRecord("test", logging.INFO, "f.py", 1, "hello", None, None)
        record.count = 3
        assert "[count=3]" in TextFormatter().format(record)

    def test_redacts_secrets_too(self):
        record = logging.LogRecord("test", logging.INFO, "f.py", 1, "hello", None, None)
        record.client_secret = "leaked"
        rendered = TextFormatter().format(record)
        assert "leaked" not in rendered
        assert "client_secret=***" in rendered


def test_configure_logging_silences_chatty_dependencies():
    configure_logging("DEBUG", "json")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger().level == logging.DEBUG
    assert len(logging.getLogger().handlers) == 1
