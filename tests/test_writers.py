from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.errors import ServiceNowError
from intune_cmdb_sync.servicenow.client import ServiceNowClient
from intune_cmdb_sync.servicenow.writers import (
    PROBE_SOURCE_KEY,
    CiPayload,
    CmdbInstanceWriter,
    IdentifyReconcileWriter,
    build_writer,
    verify_write_access,
)

SNOW = "https://acme.service-now.com"
IRE = f"{SNOW}/api/now/identifyreconcile"

# Verbatim body of the 403 ServiceNow returns when an OAuth client is not
# authorised for a global-scope API, observed 2026-08-28.
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
    # Bypass the token endpoint; auth itself is covered in test_auth.py.
    client.auth._token = "snow-token"
    client.auth._expires_at = float("inf")
    return client


def payload(**overrides) -> CiPayload:
    base = {
        "intune_id": "intune-1",
        "class_name": "cmdb_ci_computer",
        "values": {"name": "LOU-MBP16", "serial_number": "C02XY1Z2ABCD"},
        "device_name": "LOU-MBP16",
        "serial_number": "C02XY1Z2ABCD",
        "source_recency": "2026-08-25 06:11:02",
    }
    base.update(overrides)
    return CiPayload(**base)


class TestIrePayloadShape:
    def test_carries_source_native_key_and_feed(self, snow_client, config: Config):
        writer = IdentifyReconcileWriter(snow_client, config.servicenow)
        body = writer.build_payload([payload()])
        item = body["items"][0]
        assert item["className"] == "cmdb_ci_computer"
        assert item["internal_id"] == "intune-1"
        assert item["values"]["serial_number"] == "C02XY1Z2ABCD"
        source = item["sys_object_source_info"]
        assert source["source_native_key"] == "intune-1"
        assert source["source_name"] == "Intune"
        assert source["source_feed"] == "Intune Managed Devices"
        assert source["source_recency_timestamp"] == "2026-08-25 06:11:02"

    def test_recency_is_omitted_when_unknown(self, snow_client, config: Config):
        writer = IdentifyReconcileWriter(snow_client, config.servicenow)
        item = writer.build_payload([payload(source_recency=None)])["items"][0]
        assert "source_recency_timestamp" not in item["sys_object_source_info"]

    def test_batches_all_items_into_one_request(self, snow_client, config: Config):
        writer = IdentifyReconcileWriter(snow_client, config.servicenow)
        body = writer.build_payload([payload(intune_id=f"i{n}") for n in range(50)])
        assert len(body["items"]) == 50


class TestIreWrite:
    @respx.mock
    def test_maps_operations_to_actions(self, snow_client, config: Config):
        route = respx.post(IRE).mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": {
                        "items": [
                            {"className": "cmdb_ci_computer", "operation": "INSERT",
                             "sysId": "sys-1"},
                            {"className": "cmdb_ci_computer", "operation": "UPDATE",
                             "sysId": "sys-2"},
                            {"className": "cmdb_ci_computer", "operation": "NO_CHANGE",
                             "sysId": "sys-3"},
                        ]
                    }
                },
            )
        )
        writer = IdentifyReconcileWriter(snow_client, config.servicenow)
        results = writer.write([payload(intune_id=f"i{n}") for n in range(3)])
        assert [r.action for r in results] == ["inserted", "updated", "unchanged"]
        assert [r.sys_id for r in results] == ["sys-1", "sys-2", "sys-3"]
        assert route.calls[0].request.url.params["sysparm_data_source"] == "Intune"

    @respx.mock
    def test_per_item_errors_become_error_outcomes(self, snow_client, config: Config):
        respx.post(IRE).mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": {
                        "items": [
                            {"operation": "INSERT", "sysId": "sys-1"},
                            {
                                "errors": [
                                    {
                                        "error": "Required_Attribute_Empty",
                                        "message": "serial_number is required",
                                    }
                                ]
                            },
                        ]
                    }
                },
            )
        )
        writer = IdentifyReconcileWriter(snow_client, config.servicenow)
        results = writer.write([payload(intune_id="a"), payload(intune_id="b")])
        assert results[0].action == "inserted"
        assert results[1].action == "error"
        assert "Required_Attribute_Empty" in results[1].message

    @respx.mock
    def test_short_result_list_marks_the_remainder_as_errors(self, snow_client, config: Config):
        respx.post(IRE).mock(
            return_value=httpx.Response(
                200, json={"result": {"items": [{"operation": "INSERT", "sysId": "sys-1"}]}}
            )
        )
        writer = IdentifyReconcileWriter(snow_client, config.servicenow)
        results = writer.write([payload(intune_id="a"), payload(intune_id="b")])
        assert results[0].action == "inserted"
        assert results[1].action == "error"

    @respx.mock
    def test_http_failure_raises(self, snow_client, config: Config):
        respx.post(IRE).mock(return_value=httpx.Response(403, text="itil role required"))
        writer = IdentifyReconcileWriter(snow_client, config.servicenow)
        with pytest.raises(ServiceNowError, match="identifyreconcile"):
            writer.write([payload()])

    def test_empty_batch_makes_no_call(self, snow_client, config: Config):
        assert IdentifyReconcileWriter(snow_client, config.servicenow).write([]) == []

    @respx.mock
    def test_dry_run_uses_the_query_endpoint(self, snow_client, config: Config):
        route = respx.post(f"{IRE}/query").mock(
            return_value=httpx.Response(
                200, json={"result": {"items": [{"operation": "INSERT", "sysId": "would-be"}]}}
            )
        )
        writer = IdentifyReconcileWriter(snow_client, config.servicenow, dry_run=True)
        results = writer.write([payload()])
        assert route.call_count == 1
        assert results[0].action == "dry_run:inserted"

    @respx.mock
    def test_enhanced_endpoint_and_options(self, set_env, snow_client):
        set_env(SNOW_USE_ENHANCED_IRE="true")
        cfg = Config.from_env()
        route = respx.post(f"{IRE}/enhanced").mock(
            return_value=httpx.Response(
                200, json={"result": {"items": [{"operation": "INSERT", "sysId": "s"}]}}
            )
        )
        IdentifyReconcileWriter(snow_client, cfg.servicenow).write([payload()])
        assert "partial_payloads:true" in route.calls[0].request.url.params["options"]


class TestCmdbInstanceWriter:
    @respx.mock
    def test_posts_per_ci_with_attributes_and_source(self, snow_client, config: Config):
        route = respx.post(f"{SNOW}/api/now/cmdb/instance/cmdb_ci_computer").mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": {
                        "attributes": {
                            "sys_id": "sys-9",
                            "sys_created_on": "2026-08-25 06:00:00",
                            "sys_updated_on": "2026-08-25 06:00:00",
                        }
                    }
                },
            )
        )
        writer = CmdbInstanceWriter(snow_client, config.servicenow)
        results = writer.write([payload()])
        assert results[0].action == "inserted"
        assert results[0].sys_id == "sys-9"
        body = json.loads(route.calls[0].request.read())
        assert body["source"] == "Intune"
        assert body["attributes"]["serial_number"] == "C02XY1Z2ABCD"

    @respx.mock
    def test_existing_record_reports_updated(self, snow_client, config: Config):
        respx.post(f"{SNOW}/api/now/cmdb/instance/cmdb_ci_computer").mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": {
                        "attributes": {
                            "sys_id": "sys-9",
                            "sys_created_on": "2025-01-01 00:00:00",
                            "sys_updated_on": "2026-08-25 06:00:00",
                        }
                    }
                },
            )
        )
        writer = CmdbInstanceWriter(snow_client, config.servicenow)
        assert writer.write([payload()])[0].action == "updated"

    @respx.mock
    def test_one_failure_does_not_sink_the_batch(self, snow_client, config: Config):
        respx.post(f"{SNOW}/api/now/cmdb/instance/cmdb_ci_computer").mock(
            side_effect=[
                httpx.Response(400, text="bad payload"),
                httpx.Response(200, json={"result": {"attributes": {"sys_id": "sys-2"}}}),
            ]
        )
        writer = CmdbInstanceWriter(snow_client, config.servicenow)
        results = writer.write([payload(intune_id="a"), payload(intune_id="b")])
        actions = {r.intune_id: r.action for r in results}
        assert actions == {"a": "error", "b": "updated"}

    @respx.mock
    def test_embedded_error_object_is_surfaced(self, snow_client, config: Config):
        respx.post(f"{SNOW}/api/now/cmdb/instance/cmdb_ci_computer").mock(
            return_value=httpx.Response(
                200,
                json={"result": {"attributes": {}, "error": {"message": "no identifier matched"}}},
            )
        )
        writer = CmdbInstanceWriter(snow_client, config.servicenow)
        result = writer.write([payload()])[0]
        assert result.action == "error"
        assert "no identifier matched" in result.message

    def test_dry_run_writes_nothing(self, snow_client, config: Config):
        writer = CmdbInstanceWriter(snow_client, config.servicenow, dry_run=True)
        assert writer.write([payload()])[0].action == "dry_run:pending"


class TestBuildWriter:
    def test_defaults_to_identify_reconcile(self, snow_client, config: Config):
        assert build_writer(snow_client, config.servicenow).mode == "identify_reconcile"

    def test_selects_cmdb_instance_when_configured(self, set_env, snow_client):
        set_env(SNOW_WRITE_MODE="cmdb_instance")
        cfg = Config.from_env()
        assert build_writer(snow_client, cfg.servicenow).mode == "cmdb_instance"


class TestUnrecognisedOperation:
    """An IRE `operation` the connector cannot interpret must never be silently
    absorbed — least of all during a dry run, which is where an unexpected
    response vocabulary is supposed to become visible."""

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_unknown_operation_is_an_error_in_both_modes(self, config, dry_run):
        writer = IdentifyReconcileWriter(
            ServiceNowClient(config.servicenow), config.servicenow, dry_run=dry_run
        )
        results = writer._parse_results(
            [payload()],
            {"items": [{"operation": "SOMETHING_NEW", "sysId": "abc"}]},
        )
        assert results[0].action == "error"
        assert "SOMETHING_NEW" in results[0].message

    def test_known_operation_still_reports_normally_in_dry_run(self, config):
        writer = IdentifyReconcileWriter(
            ServiceNowClient(config.servicenow), config.servicenow, dry_run=True
        )
        results = writer._parse_results(
            [payload()], {"items": [{"operation": "NO_CHANGE", "sysId": "abc"}]}
        )
        assert results[0].action == "dry_run:unchanged"
        assert results[0].errors == []


class TestLogContextId:
    """IRE's logContextId is the only handle tying a request to what ServiceNow
    recorded on its own side. Without it a support case starts from a timestamp."""

    def test_carried_on_a_failed_batch(self, config, snow_client):
        # 400, not 500: retryable statuses are retried by the HTTP layer and
        # never reach the writer's error path.
        with respx.mock:
            respx.post(IRE).mock(return_value=httpx.Response(
                400, json={"result": {"logContextId": "ctx-abc123"}}
            ))
            writer = IdentifyReconcileWriter(snow_client, config.servicenow)
            with pytest.raises(ServiceNowError) as exc:
                writer.write([payload()])
        assert "ctx-abc123" in str(exc.value)

    def test_carried_on_a_per_device_error(self, config, snow_client):
        writer = IdentifyReconcileWriter(snow_client, config.servicenow)
        results = writer._parse_results([payload()], {
            "logContextId": "ctx-def456",
            "items": [{"errors": [{"error": "Required_Attribute_Empty",
                                   "message": "serial_number"}]}],
        })
        assert results[0].action == "error"
        assert "ctx-def456" in results[0].message

    def test_carried_when_an_item_is_missing_entirely(self, config, snow_client):
        writer = IdentifyReconcileWriter(snow_client, config.servicenow)
        results = writer._parse_results(
            [payload(), payload(intune_id="intune-2")],
            {"logContextId": "ctx-ghi789", "items": [{"operation": "INSERT", "sysId": "s1"}]},
        )
        assert "ctx-ghi789" in results[1].message

    def test_absent_context_id_adds_no_noise(self, config, snow_client):
        writer = IdentifyReconcileWriter(snow_client, config.servicenow)
        results = writer._parse_results([payload()], {
            "items": [{"errors": [{"error": "Boom", "message": "bang"}]}],
        })
        assert "logContextId" not in (results[0].message or "")

    def test_non_json_failure_body_does_not_crash(self, config, snow_client):
        """A 400 from a proxy or WAF is HTML, not an IRE response."""
        with respx.mock:
            respx.post(IRE).mock(return_value=httpx.Response(400, text="<html>blocked</html>"))
            writer = IdentifyReconcileWriter(snow_client, config.servicenow)
            with pytest.raises(ServiceNowError) as exc:
                writer.write([payload()])
        assert "400" in str(exc.value)


class TestVerifyWriteAccess:
    """Proving the write path works without writing. The probe goes to the
    query endpoint, which runs identification and commits nothing, so this is
    safe to point at production."""

    def _probe(self, config, snow_client, response):
        with respx.mock:
            respx.post(f"{IRE}/query").mock(return_value=response)
            return verify_write_access(snow_client, config.servicenow)

    def test_success_is_verified(self, config, snow_client):
        check = self._probe(config, snow_client, httpx.Response(
            200, json={"result": {"items": [{"operation": "INSERT"}]}}))
        assert check.verified
        assert "nothing was committed" in check.detail

    def test_it_never_touches_the_committing_endpoint(self, config, snow_client):
        with respx.mock:
            committing = respx.post(IRE)
            respx.post(f"{IRE}/query").mock(return_value=httpx.Response(
                200, json={"result": {"items": [{"operation": "INSERT"}]}}))
            verify_write_access(snow_client, config.servicenow)
        assert committing.call_count == 0

    def test_the_probe_cannot_collide_with_a_real_ci(self, config, snow_client):
        """Even if this were ever sent to a committing endpoint, its source key
        is nothing like an Intune device GUID."""
        with respx.mock:
            route = respx.post(f"{IRE}/query").mock(return_value=httpx.Response(
                200, json={"result": {"items": [{"operation": "INSERT"}]}}))
            verify_write_access(snow_client, config.servicenow)
        sent = json.loads(route.calls[0].request.read())
        assert sent["items"][0]["sys_object_source_info"]["source_native_key"] == PROBE_SOURCE_KEY
        # Intune device ids are GUIDs; this deliberately is not one, so it can
        # never be mistaken for -- or collide with -- a real device's key.
        guid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
        assert not guid.match(PROBE_SOURCE_KEY)

    @pytest.mark.parametrize("status", [401, 403])
    def test_missing_role_raises(self, config, snow_client, status):
        with pytest.raises(ServiceNowError) as exc:
            self._probe(config, snow_client, httpx.Response(status, text="insufficient rights"))
        assert "itil" in str(exc.value)

    def test_invalid_data_source_explains_where_to_register_it(self, config, snow_client):
        with pytest.raises(ServiceNowError) as exc:
            self._probe(config, snow_client, httpx.Response(
                400, json={"error": {"message": "Invalid data source"}}))
        assert "cmdb_ci.discovery_source" in str(exc.value)

    def test_missing_endpoint_is_unverified_not_a_failure(self, config, snow_client):
        check = self._probe(config, snow_client, httpx.Response(404, text="not found"))
        assert not check.verified
        assert "cmdb_instance" in check.detail

    def test_item_level_rejection_raises(self, config, snow_client):
        with pytest.raises(ServiceNowError) as exc:
            self._probe(config, snow_client, httpx.Response(200, json={"result": {
                "logContextId": "ctx-1",
                "items": [{"errors": [{"error": "Required_Attribute_Empty",
                                       "message": "asset_tag"}]}],
            }}))
        assert "asset_tag" in str(exc.value)
        assert "ctx-1" in str(exc.value)

    def test_cmdb_instance_mode_is_honestly_unverified(self, set_env):
        set_env(SNOW_WRITE_MODE="cmdb_instance")
        cfg = Config.from_env()
        check = verify_write_access(ServiceNowClient(cfg.servicenow), cfg.servicenow)
        assert not check.verified
        assert "cannot be checked" in check.detail


class TestUnscopedApiRefusal:
    """ServiceNow refuses an OAuth client that is not authorised for a
    global-scope API before it consults roles or ACLs, and says only "User Not
    Authorized". Observed live on 2026-08-28 with a credential whose Table API
    reads were succeeding in the same run, so the default advice -- grant
    `itil` -- is the one thing that cannot fix it."""

    @respx.mock
    def test_identifyreconcile_write_explains_the_oauth_gate(self, snow_client, config: Config):
        respx.post(IRE).mock(return_value=httpx.Response(403, json=UNSCOPED_403))
        with pytest.raises(ServiceNowError) as exc:
            IdentifyReconcileWriter(snow_client, config.servicenow).write([payload()])
        assert "REST API Auth Scope" in str(exc.value)

    @respx.mock
    def test_cmdb_instance_write_explains_it_too(self, snow_client, config: Config):
        """The fallback write mode is behind the same gate, so its per-device
        error must not read as a reason to switch to it."""
        respx.post(f"{SNOW}/api/now/cmdb/instance/cmdb_ci_computer").mock(
            return_value=httpx.Response(403, json=UNSCOPED_403)
        )
        results = CmdbInstanceWriter(snow_client, config.servicenow).write([payload()])
        assert results[0].action == "error"
        assert "REST API Auth Scope" in results[0].message

    @respx.mock
    def test_check_does_not_blame_the_itil_role(self, snow_client, config: Config):
        respx.post(f"{IRE}/query").mock(return_value=httpx.Response(403, json=UNSCOPED_403))
        with pytest.raises(ServiceNowError) as exc:
            verify_write_access(snow_client, config.servicenow)
        message = str(exc.value)
        assert "REST API Auth Scope" in message
        assert "It needs the 'itil' or 'asset' role" not in message

    @respx.mock
    def test_an_ordinary_403_still_names_the_role(self, snow_client, config: Config):
        respx.post(f"{IRE}/query").mock(
            return_value=httpx.Response(403, text="insufficient rights")
        )
        with pytest.raises(ServiceNowError) as exc:
            verify_write_access(snow_client, config.servicenow)
        assert "itil" in str(exc.value)
        assert "REST API Auth Scope" not in str(exc.value)
