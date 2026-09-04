"""The per-endpoint authorization probe.

The point of these tests is the *classification*: the connector's live blocker
is a 403 whose body says only "User Not Authorized", and three different people
fix it depending on which layer produced it. Getting that wrong sends an
operator to the wrong team for a week.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.servicenow.client import ServiceNowClient
from intune_cmdb_sync.servicenow.probe import (
    AUTHORIZED,
    BLOCKED_AT_GATE,
    DENIED_BY_ROLE,
    NOT_FOUND,
    PROBE_CLASS,
    PROBE_SYS_ID,
    UNAUTHENTICATED,
    build_probes,
    diagnose,
    format_report,
    probe_endpoints,
)

SNOW = "https://acme.service-now.com"

UNSCOPED_403 = {
    "error": {
        "message": "User Not Authorized",
        "detail": "Access to unscoped api is not allowed",
    },
    "status": "failure",
}


@pytest.fixture
def snow_client(config: Config) -> ServiceNowClient:
    client = ServiceNowClient(config.servicenow)
    client.auth._token = "snow-token"
    client.auth._expires_at = float("inf")
    return client


def mock_all(*, reads: httpx.Response, writes: httpx.Response) -> None:
    respx.get(url__startswith=f"{SNOW}/api/now").mock(return_value=reads)
    respx.post(url__startswith=f"{SNOW}/api/now").mock(return_value=writes)
    respx.patch(url__startswith=f"{SNOW}/api/now").mock(return_value=writes)


def mock_reads_ok() -> None:
    respx.get(url__startswith=f"{SNOW}/api/now").mock(
        return_value=httpx.Response(200, json={"result": []})
    )


def mock_writes(response: httpx.Response) -> None:
    respx.post(url__startswith=f"{SNOW}/api/now").mock(return_value=response)
    respx.patch(url__startswith=f"{SNOW}/api/now").mock(return_value=response)


class TestProbeSafety:
    """Nothing in the matrix may be capable of creating a CI."""

    def test_identifyreconcile_probes_submit_no_items(self, config: Config):
        for probe in build_probes(config.servicenow):
            if "identifyreconcile" in probe.path:
                assert probe.json_body == {"items": [], "relations": []}

    def test_cmdb_instance_writes_target_a_class_that_cannot_exist(self, config: Config):
        posts = [
            p
            for p in build_probes(config.servicenow)
            if p.method == "POST" and "/cmdb/instance/" in p.path
        ]
        assert posts
        for probe in posts:
            assert probe.path.endswith(PROBE_CLASS)
            assert probe.json_body == {}

    def test_the_only_patch_probe_cannot_reach_a_record(self, config: Config):
        """Retirement PATCHes the Table API, so that method has to be probed.
        It is made harmless twice over: a sys_id no record can have, and an
        empty body that would change nothing even if one did."""
        patches = [p for p in build_probes(config.servicenow) if p.method == "PATCH"]
        assert len(patches) == 1
        assert patches[0].path.endswith(PROBE_SYS_ID)
        assert patches[0].json_body == {}

    def test_no_probe_can_delete(self, config: Config):
        assert {p.method for p in build_probes(config.servicenow)} <= {"GET", "POST", "PATCH"}


class TestClassification:
    @respx.mock
    def test_gate_refusal_is_distinguished_from_a_role_denial(self, snow_client, config: Config):
        respx.get(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        respx.post(f"{SNOW}/api/now/identifyreconcile").mock(
            return_value=httpx.Response(403, json=UNSCOPED_403, headers={"X-Is-Logged-In": "true"})
        )
        respx.post(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(403, text="insufficient rights: itil")
        )
        respx.patch(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(403, text="insufficient rights: itil")
        )

        report = probe_endpoints(snow_client, config.servicenow)
        assert report.by_name("table_read").verdict == AUTHORIZED
        assert report.by_name("ire_write").verdict == BLOCKED_AT_GATE
        assert report.by_name("ire_query").verdict == DENIED_BY_ROLE
        assert report.write_path_ok is False

    @respx.mock
    def test_a_401_is_reported_as_authentication_not_authorization(
        self, snow_client, config: Config
    ):
        mock_all(
            reads=httpx.Response(401, text="User Not Authenticated"),
            writes=httpx.Response(401, text="User Not Authenticated"),
        )
        report = probe_endpoints(snow_client, config.servicenow)
        assert report.by_name("table_read").verdict == UNAUTHENTICATED
        assert "did not authenticate" in " ".join(diagnose(report))

    @respx.mock
    def test_a_payload_rejection_counts_as_authorized(self, snow_client, config: Config):
        """The probes are built to be rejected on their content. Reaching that
        rejection is the proof that the request got through the gate."""
        mock_all(
            reads=httpx.Response(200, json={"result": []}),
            writes=httpx.Response(400, json={"error": {"message": "Invalid payload"}}),
        )
        report = probe_endpoints(snow_client, config.servicenow)
        assert report.by_name("ire_write").verdict == AUTHORIZED
        assert report.write_path_ok is True

    @respx.mock
    def test_a_404_naming_the_probe_class_is_the_api_answering(self, snow_client, config: Config):
        respx.get(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        respx.patch(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(404, json={"error": {"message": "No Record found"}})
        )
        respx.post(url__startswith=f"{SNOW}/api/now/cmdb/instance").mock(
            return_value=httpx.Response(
                404, json={"error": {"message": f"No such class {PROBE_CLASS}"}}
            )
        )
        # ...whereas a 404 for the API itself is not. The probe class is in the
        # URL either way, so only the body can tell the two apart.
        respx.post(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(
                404,
                json={
                    "error": {
                        "message": "Requested URI does not represent any resource",
                        "detail": None,
                    },
                    "status": "failure",
                },
            )
        )
        report = probe_endpoints(snow_client, config.servicenow)
        assert report.by_name("cmdb_instance_write").verdict == AUTHORIZED
        assert report.by_name("ire_write").verdict == NOT_FOUND


class TestDiagnosis:
    @respx.mock
    def test_names_the_refused_methods_and_paths(self, snow_client, config: Config):
        mock_all(
            reads=httpx.Response(200, json={"result": []}),
            writes=httpx.Response(403, json=UNSCOPED_403, headers={"X-Is-Logged-In": "true"}),
        )
        report = probe_endpoints(snow_client, config.servicenow)
        text = format_report(report)
        assert "POST /api/now/identifyreconcile" in text
        assert "REST API Auth Scope" in text
        assert "per API *and per HTTP method*" in text
        # The working read is the evidence that roles are not the problem.
        assert "GET /api/now/table/sys_properties" in text
        assert "X-Is-Logged-In" in text

    @respx.mock
    def test_calls_out_a_per_method_restriction(self, snow_client, config: Config):
        """GET allowed and POST refused on the same API is the single most
        useful thing to hand a ServiceNow admin: it names what to change."""
        respx.get(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        mock_writes(httpx.Response(403, json=UNSCOPED_403))
        report = probe_endpoints(snow_client, config.servicenow)
        assert any("per-method" in line for line in diagnose(report))

    @respx.mock
    def test_calls_out_a_version_asymmetry(self, snow_client, config: Config):
        respx.get(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        respx.post(f"{SNOW}/api/now/v1/identifyreconcile").mock(
            return_value=httpx.Response(200, json={"result": {"items": []}})
        )
        mock_writes(httpx.Response(403, json=UNSCOPED_403))
        report = probe_endpoints(snow_client, config.servicenow)
        assert any("API version" in line for line in diagnose(report))

    @respx.mock
    def test_names_the_open_write_path_when_the_configured_one_is_shut(
        self, snow_client, config: Config
    ):
        """The live case on 2026-09-04: every identifyreconcile variant refused
        at the gate, the CMDB Instance API not. Leaving the operator to spot
        that in a wall of 403s is the difference between waiting on the
        ServiceNow team and running the same day."""
        mock_reads_ok()
        respx.patch(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(404, json={"error": {"message": "No Record found"}})
        )
        respx.post(url__startswith=f"{SNOW}/api/now/cmdb/instance").mock(
            return_value=httpx.Response(400, json={"error": {"message": "Invalid class"}})
        )
        respx.post(url__startswith=f"{SNOW}/api/now/v1/cmdb/instance").mock(
            return_value=httpx.Response(400, json={"error": {"message": "Invalid class"}})
        )
        respx.post(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(403, json=UNSCOPED_403)
        )
        report = probe_endpoints(snow_client, config.servicenow)
        text = " ".join(diagnose(report))
        assert "SNOW_WRITE_MODE=cmdb_instance works on this instance" in text
        # ...and never as a free lunch.
        assert "serial correction duplicates a CI" in text

    @respx.mock
    def test_warns_when_retirement_is_shut_in_every_mode(self, snow_client, config: Config):
        mock_reads_ok()
        respx.patch(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(403, json=UNSCOPED_403)
        )
        respx.post(url__startswith=f"{SNOW}/api/now/cmdb/instance").mock(
            return_value=httpx.Response(400, json={"error": {"message": "Invalid class"}})
        )
        respx.post(url__startswith=f"{SNOW}/api/now/v1/cmdb/instance").mock(
            return_value=httpx.Response(400, json={"error": {"message": "Invalid class"}})
        )
        respx.post(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(403, json=UNSCOPED_403)
        )
        report = probe_endpoints(snow_client, config.servicenow)
        assert any("SNOW_RETIRE_MISSING=false" in line for line in diagnose(report))

    @respx.mock
    def test_a_clean_pass_points_at_the_next_step(self, snow_client, config: Config):
        mock_all(
            reads=httpx.Response(200, json={"result": []}),
            writes=httpx.Response(200, json={"result": {"items": []}}),
        )
        report = probe_endpoints(snow_client, config.servicenow)
        assert report.write_path_ok is True
        assert any("--check" in line for line in diagnose(report))

    @respx.mock
    def test_write_path_ok_follows_the_configured_write_mode(self, set_env, snow_client):
        """cmdb_instance mode must not be called healthy because IRE is open."""
        set_env(SNOW_WRITE_MODE="cmdb_instance")
        cfg = Config.from_env().servicenow
        respx.get(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        respx.post(url__startswith=f"{SNOW}/api/now/cmdb/instance").mock(
            return_value=httpx.Response(403, json=UNSCOPED_403)
        )
        mock_writes(httpx.Response(200, json={"result": {"items": []}}))
        report = probe_endpoints(snow_client, cfg)
        assert report.by_name("ire_write").verdict == AUTHORIZED
        assert report.write_path_ok is False


@respx.mock
def test_report_serialises_for_the_json_log(snow_client, config: Config):
    mock_all(
        reads=httpx.Response(200, json={"result": []}),
        writes=httpx.Response(403, json=UNSCOPED_403),
    )
    data = probe_endpoints(snow_client, config.servicenow).as_dict()
    assert data["write_path_ok"] is False
    assert {"name", "method", "path", "verdict", "status"} <= set(data["endpoints"][0])
