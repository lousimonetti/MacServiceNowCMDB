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

    def test_every_probe_is_a_get_or_a_post(self, config: Config):
        # A PATCH or DELETE probe could not be made harmless.
        assert {p.method for p in build_probes(config.servicenow)} <= {"GET", "POST"}


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
        respx.post(url__startswith=f"{SNOW}/api/now/cmdb/instance").mock(
            return_value=httpx.Response(
                404, json={"error": {"message": f"No such class {PROBE_CLASS}"}}
            )
        )
        # ...whereas a 404 for the API itself is not. The probe class is in the
        # URL either way, so only the body can tell the two apart.
        respx.post(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(
                404, json={"error": {"message": "Requested URI does not represent any resource"}}
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
        respx.post(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(403, json=UNSCOPED_403)
        )
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
        respx.post(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(403, json=UNSCOPED_403)
        )
        report = probe_endpoints(snow_client, config.servicenow)
        assert any("API version" in line for line in diagnose(report))

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
        respx.post(url__startswith=f"{SNOW}/api/now").mock(
            return_value=httpx.Response(200, json={"result": {"items": []}})
        )
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
