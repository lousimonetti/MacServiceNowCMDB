from __future__ import annotations

import pytest

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.secrets import _read_ssm_parameter, resolve_secret

VAR = "SNOW_CLIENT_SECRET"


@pytest.fixture(autouse=True)
def clear_ssm_cache():
    _read_ssm_parameter.cache_clear()
    yield
    _read_ssm_parameter.cache_clear()


class TestResolutionOrder:
    def test_literal_value_wins(self, monkeypatch, tmp_path):
        path = tmp_path / "secret"
        path.write_text("from-file")
        monkeypatch.setenv(VAR, "literal")
        monkeypatch.setenv(f"{VAR}_FILE", str(path))
        assert resolve_secret(VAR) == "literal"

    def test_falls_back_to_file(self, monkeypatch, tmp_path):
        path = tmp_path / "secret"
        path.write_text("from-file")
        monkeypatch.delenv(VAR, raising=False)
        monkeypatch.setenv(f"{VAR}_FILE", str(path))
        assert resolve_secret(VAR) == "from-file"

    def test_file_trailing_newline_is_stripped(self, monkeypatch, tmp_path):
        path = tmp_path / "secret"
        path.write_text("from-file\n")
        monkeypatch.setenv(f"{VAR}_FILE", str(path))
        assert resolve_secret(VAR) == "from-file"

    def test_unreadable_file_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv(f"{VAR}_FILE", str(tmp_path / "absent"))
        assert resolve_secret(VAR) is None

    def test_empty_file_returns_none(self, monkeypatch, tmp_path):
        path = tmp_path / "secret"
        path.write_text("   \n")
        monkeypatch.setenv(f"{VAR}_FILE", str(path))
        assert resolve_secret(VAR) is None

    def test_nothing_configured_returns_none(self, monkeypatch):
        assert resolve_secret(VAR) is None


class TestSsmParameter:
    def test_reads_and_decrypts(self, monkeypatch):
        calls: list[dict] = []

        class FakeClient:
            def get_parameter(self, **kwargs):
                calls.append(kwargs)
                return {"Parameter": {"Value": "from-ssm"}}

        class FakeBoto3:
            @staticmethod
            def client(_service):
                return FakeClient()

        monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3)
        monkeypatch.setenv(f"{VAR}_PARAMETER", "/app/snow-secret")
        assert resolve_secret(VAR) == "from-ssm"
        assert calls[0]["WithDecryption"] is True
        assert calls[0]["Name"] == "/app/snow-secret"

    def test_result_is_cached_across_calls(self, monkeypatch):
        count = {"n": 0}

        class FakeClient:
            def get_parameter(self, **_kwargs):
                count["n"] += 1
                return {"Parameter": {"Value": "from-ssm"}}

        class FakeBoto3:
            @staticmethod
            def client(_service):
                return FakeClient()

        monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3)
        monkeypatch.setenv(f"{VAR}_PARAMETER", "/app/snow-secret")
        resolve_secret(VAR)
        resolve_secret(VAR)
        assert count["n"] == 1

    def test_ssm_failure_returns_none_rather_than_raising(self, monkeypatch):
        class FakeClient:
            def get_parameter(self, **_kwargs):
                raise RuntimeError("AccessDeniedException")

        class FakeBoto3:
            @staticmethod
            def client(_service):
                return FakeClient()

        monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3)
        monkeypatch.setenv(f"{VAR}_PARAMETER", "/app/snow-secret")
        assert resolve_secret(VAR) is None


class TestConfigIntegration:
    def test_config_accepts_a_file_backed_secret(self, set_env, monkeypatch, tmp_path):
        path = tmp_path / "snow-secret"
        path.write_text("file-secret\n")
        set_env(SNOW_CLIENT_SECRET=None)
        monkeypatch.setenv("SNOW_CLIENT_SECRET_FILE", str(path))
        assert Config.from_env().servicenow.client_secret == "file-secret"

    def test_missing_secret_still_fails_validation(self, set_env, monkeypatch):
        set_env(SNOW_CLIENT_SECRET=None)
        monkeypatch.delenv("SNOW_CLIENT_SECRET_FILE", raising=False)
        with pytest.raises(Exception, match="SNOW_CLIENT_SECRET is required"):
            Config.from_env()
