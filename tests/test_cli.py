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


IRE_QUERY = f"{IRE}/query"


class TestCheck:
    """`--check` must prove the write path too. Read access is the easy half;
    the failures that bite on a new instance -- a missing `itil` role, an
    unregistered discovery source -- are all on the write side."""

    def _graph_ok(self):
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )

    @respx.mock
    def test_probes_both_systems_without_committing_anything(self, set_env):
        set_env()
        mock_snow_plumbing()
        self._graph_ok()
        probe = respx.post(IRE_QUERY).mock(
            return_value=httpx.Response(200, json={"result": {"items": [{"operation": "INSERT"}]}})
        )
        committing = respx.post(IRE)

        assert main(["--check"]) == EXIT_OK
        assert probe.call_count == 1
        # The simulation endpoint was used; the one that writes was not.
        assert committing.call_count == 0

    @respx.mock
    def test_missing_itil_role_fails_the_check(self, set_env):
        set_env()
        mock_snow_plumbing()
        self._graph_ok()
        respx.post(IRE_QUERY).mock(return_value=httpx.Response(403, text="insufficient rights"))
        assert main(["--check"]) == EXIT_FAILED

    @respx.mock
    def test_unregistered_discovery_source_fails_with_a_pointed_message(self, set_env, capsys):
        """`Invalid data source` does not say where the value has to be
        registered, which is the entire difficulty of that error."""
        set_env()
        mock_snow_plumbing()
        self._graph_ok()
        respx.post(IRE_QUERY).mock(
            return_value=httpx.Response(400, json={"error": {"message": "Invalid data source"}})
        )
        assert main(["--check"]) == EXIT_FAILED
        # configure_logging replaces the root handlers, so caplog sees nothing;
        # the JSON goes to stdout.
        output = capsys.readouterr().out
        assert "cmdb_ci.discovery_source" in output
        assert "SNOW_DISCOVERY_SOURCE" in output

    @respx.mock
    def test_absent_query_endpoint_is_unverified_not_passed(self, set_env):
        """An older release without the API is not a failure, but reporting it
        as a pass would defeat the point of the check."""
        set_env()
        mock_snow_plumbing()
        self._graph_ok()
        respx.post(IRE_QUERY).mock(return_value=httpx.Response(404, text="not found"))
        assert main(["--check"]) == EXIT_PARTIAL

    @respx.mock
    def test_cmdb_instance_mode_is_verified_and_its_caveats_are_printed(self, set_env, capsys):
        """This mode is the write path on an instance where the OAuth client is
        refused identifyreconcile but not the CMDB Instance API, so `--check`
        has to check it -- and has to be plain about what it could not prove."""
        set_env(SNOW_WRITE_MODE="cmdb_instance")
        mock_snow_plumbing()
        self._graph_ok()
        respx.get("https://acme.service-now.com/api/now/table/sys_choice").mock(
            return_value=httpx.Response(200, json={"result": [{"value": "Intune"}]})
        )
        probe = respx.post(
            url__startswith="https://acme.service-now.com/api/now/cmdb/instance/"
        ).mock(return_value=httpx.Response(400, json={"error": {"message": "Invalid class"}}))

        assert main(["--check"]) == EXIT_OK
        assert probe.call_count == 1
        output = capsys.readouterr().out
        assert "identification rules" in output
        assert "--limit 1" in output

    @respx.mock
    def test_check_fails_when_servicenow_rejects_auth(self, set_env):
        set_env()
        respx.get("https://acme.service-now.com/api/now/table/sys_properties").mock(
            return_value=httpx.Response(401, text="unauthorized")
        )
        assert main(["--check"]) == EXIT_FAILED


class TestCheckApi:
    """`--check-api` exists because `--check` stops at the first refusal and so
    cannot say whether the *next* endpoint would have behaved the same way. The
    fix for the live blocker is bound per API and per HTTP method, so the shape
    of the whole matrix is the actionable output."""

    @respx.mock
    def test_reports_the_matrix_and_fails_when_the_write_path_is_refused(
        self, set_env, capsys
    ):
        set_env()
        respx.get(url__startswith="https://acme.service-now.com/api/now").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        gated = httpx.Response(
            403,
            json={
                "error": {
                    "message": "User Not Authorized",
                    "detail": "Access to unscoped api is not allowed",
                }
            },
            headers={"X-Is-Logged-In": "true"},
        )
        respx.post(url__startswith="https://acme.service-now.com/api/now").mock(
            return_value=gated
        )
        respx.patch(url__startswith="https://acme.service-now.com/api/now").mock(
            return_value=gated
        )
        graph = respx.get(DEVICES)

        assert main(["--check-api"]) == EXIT_FAILED
        output = capsys.readouterr().out
        assert "POST /api/now/identifyreconcile" in output
        assert "REFUSED AT OAUTH GATE" in output
        assert "GET /api/now/table/sys_properties" in output
        # Graph is the half that already works; a ServiceNow diagnostic must not
        # need it, or an unrelated Graph outage hides the answer.
        assert graph.call_count == 0

    @respx.mock
    def test_passes_when_the_configured_write_endpoint_is_allowed(self, set_env):
        set_env()
        respx.get(url__startswith="https://acme.service-now.com/api/now").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        respx.post(url__startswith="https://acme.service-now.com/api/now").mock(
            return_value=httpx.Response(200, json={"result": {"items": []}})
        )
        respx.patch(url__startswith="https://acme.service-now.com/api/now").mock(
            return_value=httpx.Response(200, json={"result": {}})
        )
        assert main(["--check-api"]) == EXIT_OK

    @respx.mock
    def test_writes_nothing_even_when_every_endpoint_is_open(self, set_env):
        """The probes must be incapable of creating a CI: no item to identify,
        and a class that does not exist."""
        set_env()
        respx.get(url__startswith="https://acme.service-now.com/api/now").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        posts = respx.post(url__startswith="https://acme.service-now.com/api/now").mock(
            return_value=httpx.Response(200, json={"result": {"items": []}})
        )
        patches = respx.patch(url__startswith="https://acme.service-now.com/api/now").mock(
            return_value=httpx.Response(200, json={"result": {}})
        )
        main(["--check-api"])
        # Retirement PATCHes the Table API, so that method is probed too -- at a
        # sys_id no record can have, with a body that would change nothing.
        for call in patches.calls:
            assert call.request.url.path.endswith("0" * 32)
            assert json.loads(call.request.content or b"{}") == {}
        for call in posts.calls:
            body = json.loads(call.request.content or b"{}")
            assert body.get("items", []) == []
            if "/cmdb/instance/" in call.request.url.path:
                assert "no_such_class" in call.request.url.path


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
