from __future__ import annotations

import json
import os

import httpx
import pytest
import respx

from intune_cmdb_sync.__main__ import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, main

from .conftest import make_device
from .test_sync import DEVICES, GRAPH, IRE, ire_response, mock_snow_plumbing


@pytest.fixture(autouse=True)
def no_azure_identity(monkeypatch):
    """Keep the CLI from reaching for real Entra credentials."""
    monkeypatch.setattr(
        "intune_cmdb_sync.graph.build_credential", lambda _cfg: object()
    )
    monkeypatch.setattr(
        "intune_cmdb_sync.graph._CredentialTokenProvider.__call__", lambda _self: "graph-token"
    )
    monkeypatch.setattr(
        "intune_cmdb_sync.servicenow.auth.ServiceNowAuth.token", lambda _self: "snow-token"
    )


class TestConfigErrors:
    def test_missing_config_exits_two(self, capsys):
        assert main([]) == EXIT_CONFIG

    def test_error_message_names_every_missing_variable(self, capsys):
        main([])
        output = capsys.readouterr().out
        assert "SNOW_INSTANCE" in output
        assert "GRAPH_TENANT_ID" in output


class TestRun:
    @respx.mock
    def test_successful_run_exits_zero(self, set_env):
        set_env()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=ire_response("INSERT"))
        assert main([]) == EXIT_OK

    @respx.mock
    def test_writes_a_json_report(self, set_env, tmp_path):
        set_env()
        report_path = tmp_path / "report.json"
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=ire_response("INSERT"))

        assert main(["--report", str(report_path), "--report-devices"]) == EXIT_OK
        report = json.loads(report_path.read_text())
        assert report["inserted"] == 1
        assert report["write_mode"] == "identify_reconcile"
        assert report["devices"][0]["action"] == "inserted"

    @respx.mock
    def test_dry_run_flag_overrides_env(self, set_env):
        set_env(DRY_RUN="false")
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        commit = respx.post(IRE)
        query = respx.post(f"{IRE}/query").mock(return_value=ire_response("INSERT"))

        assert main(["--dry-run"]) == EXIT_OK
        assert query.call_count == 1
        assert commit.call_count == 0

    @respx.mock
    def test_fail_on_error_exits_four(self, set_env):
        set_env()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=httpx.Response(403, text="denied"))

        assert main(["--fail-on-error"]) == EXIT_PARTIAL
        # Without the flag a partial failure is still a completed run.
        assert main([]) == EXIT_OK

    @respx.mock
    def test_graph_failure_exits_three(self, set_env):
        set_env()
        mock_snow_plumbing()
        respx.get(DEVICES).mock(return_value=httpx.Response(403, text="no permission"))
        assert main([]) == EXIT_FAILED


class TestCheck:
    @respx.mock
    def test_check_probes_both_systems_without_writing(self, set_env):
        set_env()
        mock_snow_plumbing()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        ire = respx.post(IRE)
        assert main(["--check"]) == EXIT_OK
        assert ire.call_count == 0

    @respx.mock
    def test_check_fails_when_servicenow_rejects_auth(self, set_env):
        set_env()
        respx.get("https://acme.service-now.com/api/now/table/sys_properties").mock(
            return_value=httpx.Response(401, text="unauthorized")
        )
        assert main(["--check"]) == EXIT_FAILED


class TestReportOnFailure:
    """A failed run is exactly when the per-device detail is worth keeping."""

    @respx.mock
    def test_report_is_written_even_when_the_run_fails(self, set_env, tmp_path):
        report_path = tmp_path / "run.json"
        set_env()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(403, json={"error": {"message": "Forbidden"}})
        )
        mock_snow_plumbing()

        assert main(["--report", str(report_path)]) == EXIT_FAILED

        assert report_path.is_file()
        payload = json.loads(report_path.read_text())
        assert any("run aborted" in d for d in payload["degraded"])
        assert payload["finished_at"] is not None

    @respx.mock
    def test_degraded_run_exits_partial(self, set_env, tmp_path):
        """A tripped safety guard must not look like a clean run to a scheduler."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({
            "version": 1,
            "devices": {f"old-{i}": {"sys_id": f"sys-{i}", "name": f"old-{i}",
                                     "class_name": "cmdb_ci_computer"}
                        for i in range(50)},
        }))
        set_env(
            SNOW_RETIRE_MISSING="true",
            STATE_PATH=str(state_path),
            SNOW_RETIRE_MAX_FRACTION="0.10",
        )
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=ire_response("UPDATE"))

        # No --fail-on-error: a degraded run is non-zero on its own.
        assert main([]) == EXIT_PARTIAL


class TestOverridesDoNotLeak:
    """`aws_lambda.handler` calls main() repeatedly in a warm container. A CLI
    flag that mutated os.environ permanently meant one invocation with dry_run
    left every later invocation silently in dry-run mode."""

    def test_dry_run_flag_does_not_persist_to_the_next_call(self, set_env, monkeypatch):
        set_env(DRY_RUN="false")
        seen: list[bool] = []
        monkeypatch.setattr(
            "intune_cmdb_sync.__main__._run",
            lambda _args: seen.append(os.environ.get("DRY_RUN") == "true") or EXIT_OK,
        )
        main(["--dry-run"])
        main([])
        assert seen == [True, False], "the flag leaked into the second invocation"

    def test_absent_flag_still_defers_to_the_environment(self, set_env, monkeypatch):
        """Flags are one-way: absence must not clear a value set by the env."""
        set_env(DRY_RUN="true")
        seen: list[str | None] = []
        monkeypatch.setattr(
            "intune_cmdb_sync.__main__._run",
            lambda _args: seen.append(os.environ.get("DRY_RUN")) or EXIT_OK,
        )
        main([])
        assert seen == ["true"]

    def test_environment_is_restored_exactly(self, set_env, monkeypatch):
        set_env(RUN_REPORT_PATH="/original/path.json")
        monkeypatch.setattr("intune_cmdb_sync.__main__._run", lambda _args: EXIT_OK)
        main(["--report", "/override.json", "--limit", "3", "--fail-on-error"])
        assert os.environ["RUN_REPORT_PATH"] == "/original/path.json"
        assert "INTUNE_DEVICE_LIMIT" not in os.environ
        assert "FAIL_ON_ERROR" not in os.environ


class TestReportOptionsAreEnvSettable:
    """A container deployment passes no CLI arguments, so anything only
    reachable by flag is unreachable in production."""

    @respx.mock
    def test_fail_on_error_via_environment(self, set_env):
        set_env(FAIL_ON_ERROR="true")
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=httpx.Response(403, text="denied"))
        assert main([]) == EXIT_PARTIAL

    @respx.mock
    def test_report_devices_via_environment(self, set_env, tmp_path):
        report = tmp_path / "run.json"
        set_env(RUN_REPORT_PATH=str(report), RUN_REPORT_DEVICES="true")
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=ire_response("INSERT"))

        assert main([]) == EXIT_OK
        payload = json.loads(report.read_text())
        assert payload["devices"][0]["intune_id"] == "d1"
        assert payload["run_id"]
