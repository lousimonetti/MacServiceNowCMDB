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
    PROBE_CLASS,
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

class TestCmdbInstanceDryRun:
    """Without a simulation endpoint this mode used to report every device as
    "pending", which is no preview at all -- and on an instance where it is the
    only allowed write path, that puts nothing between configuring the
    connector and letting it write to production. The prediction reproduces the
    class's identifier rules with reads."""

    def _writer(self, snow_client, config: Config) -> CmdbInstanceWriter:
        return CmdbInstanceWriter(snow_client, config.servicenow, dry_run=True)

    @respx.mock
    def test_writes_nothing(self, snow_client, config: Config):
        write = respx.post(url__startswith=f"{SNOW}/api/now/cmdb/instance")
        respx.get(f"{SNOW}/api/now/table/cmdb_ci_computer").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        self._writer(snow_client, config).write([payload()])
        assert write.call_count == 0

    @respx.mock
    def test_a_matching_serial_predicts_an_update(self, snow_client, config: Config):
        route = respx.get(f"{SNOW}/api/now/table/cmdb_ci_computer").mock(
            return_value=httpx.Response(
                200,
                json={"result": [{"sys_id": "ci-1", "serial_number": "C02XY1Z2ABCD",
                                  "name": "OTHER-NAME"}]},
            )
        )
        result = self._writer(snow_client, config).write([payload()])[0]
        assert result.action == "dry_run:updated"
        assert result.sys_id == "ci-1"
        # One read for the whole batch, not one per device.
        assert route.call_count == 1

    @respx.mock
    def test_no_match_predicts_an_insert(self, snow_client, config: Config):
        respx.get(f"{SNOW}/api/now/table/cmdb_ci_computer").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        result = self._writer(snow_client, config).write([payload()])[0]
        assert result.action == "dry_run:inserted"
        assert result.sys_id is None

    @respx.mock
    def test_falls_back_to_name_when_the_serial_is_absent(self, snow_client, config: Config):
        """Serial first, then name: the order the identifier rules use."""
        respx.get(f"{SNOW}/api/now/table/cmdb_ci_computer").mock(
            return_value=httpx.Response(
                200, json={"result": [{"sys_id": "ci-2", "serial_number": "", "name": "LOU-MBP16"}]}
            )
        )
        result = self._writer(snow_client, config).write([payload(serial_number=None)])[0]
        assert result.action == "dry_run:updated"
        assert result.sys_id == "ci-2"

    @respx.mock
    def test_a_failed_lookup_is_reported_not_guessed(self, snow_client, config: Config):
        """Predicting "insert" from a read that failed would be a fabrication,
        and the first real run would then quietly update instead."""
        respx.get(f"{SNOW}/api/now/table/cmdb_ci_computer").mock(
            return_value=httpx.Response(403, text="denied")
        )
        result = self._writer(snow_client, config).write([payload()])[0]
        assert result.action == "dry_run:pending"
        assert "could not look up" in result.message

    @respx.mock
    def test_a_value_that_would_break_the_encoded_query_is_left_out(
        self, snow_client, config: Config
    ):
        """`,` separates IN values and `^` separates clauses. Escaping is not
        worth the risk of a malformed query matching the wrong CIs."""
        route = respx.get(f"{SNOW}/api/now/table/cmdb_ci_computer").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        self._writer(snow_client, config).write(
            [payload(serial_number="BAD,SERIAL", device_name="LOU-MBP16")]
        )
        query = route.calls[0].request.url.params["sysparm_query"]
        assert "BAD,SERIAL" not in query
        assert "nameINLOU-MBP16" in query


class TestCmdbInstanceAttributeTypes:
    """`POST /api/now/cmdb/instance/{class}` deserialises `attributes` as
    String->String and throws HTTP 500 on anything else, before any validation
    the connector could learn from. Observed live 2026-09-04:

        class java.lang.Double cannot be cast to class java.lang.String

    from `disk_space`, which is a rounded float. It failed all 17 devices."""

    def _sent(self, snow_client, config: Config, values: dict) -> dict:
        route = respx.post(f"{SNOW}/api/now/cmdb/instance/cmdb_ci_computer").mock(
            return_value=httpx.Response(
                201,
                json={"result": {"attributes": {
                    "sys_id": "ci-1", "sys_created_on": "x", "sys_updated_on": "x"}}},
            )
        )
        CmdbInstanceWriter(snow_client, config.servicenow).write([payload(values=values)])
        return json.loads(route.calls[0].request.content)["attributes"]

    @respx.mock
    def test_a_float_is_sent_as_a_string(self, snow_client, config: Config):
        # bytes_to_gb(256060514304) -> 238.47
        assert self._sent(snow_client, config, {"disk_space": 238.47})["disk_space"] == "238.47"

    @respx.mock
    def test_a_whole_float_drops_its_decimal(self, snow_client, config: Config):
        """"128" rather than "128.0": the same number as any other source would
        write, and a value that differs between runs makes every device an
        update."""
        assert self._sent(snow_client, config, {"disk_space": 128.0})["disk_space"] == "128"

    @respx.mock
    def test_an_int_is_sent_as_a_string(self, snow_client, config: Config):
        """`ram` would have thrown the same way, naming Integer instead."""
        assert self._sent(snow_client, config, {"ram": 16384})["ram"] == "16384"

    @respx.mock
    def test_a_bool_uses_the_json_spelling(self, snow_client, config: Config):
        """`virtual` is a bool; Python's str() would send "False"."""
        assert self._sent(snow_client, config, {"virtual": False})["virtual"] == "false"

    @respx.mock
    def test_every_value_sent_is_a_string(self, snow_client, config: Config):
        """The whole payload is coerced, not the fields known to have broken:
        an override mapping can put any JSON type on the payload."""
        sent = self._sent(
            snow_client,
            config,
            {"name": "HOST-1", "ram": 16384, "disk_space": 238.47, "virtual": False,
             "u_score": 0.5, "install_status": 1},
        )
        assert all(isinstance(v, str) for v in sent.values())

    @respx.mock
    def test_a_none_is_dropped_rather_than_stringified(self, snow_client, config: Config):
        """"None" as a field value would overwrite a real CMDB value with the
        word None."""
        sent = self._sent(snow_client, config, {"name": "HOST-1", "asset_tag": None})
        assert "asset_tag" not in sent

    def test_ire_payloads_keep_their_types(self, snow_client, config: Config):
        """The fix belongs to this one endpoint. IRE accepts typed values, and
        narrowing them there would change a working payload for another API's
        benefit."""
        writer = IdentifyReconcileWriter(snow_client, config.servicenow)
        body = writer.build_payload([payload(values={"disk_space": 238.47, "virtual": False})])
        assert body["items"][0]["values"] == {"disk_space": 238.47, "virtual": False}


class TestCmdbInstanceAbortGuard:
    """A per-CI writer turns one systematic problem into one failed POST per
    device. A 200-device run against a production instance would issue 200
    identical failures before anyone saw the first."""

    def _writer(self, snow_client, cfg) -> CmdbInstanceWriter:
        return CmdbInstanceWriter(snow_client, cfg)

    def _batch(self, count: int) -> list[CiPayload]:
        return [
            payload(
                intune_id=f"d{i}",
                serial_number=f"SN{i}",
                device_name=f"HOST-{i}",
                values={"name": f"HOST-{i}", "serial_number": f"SN{i}"},
            )
            for i in range(count)
        ]

    @respx.mock
    def test_stops_once_every_write_has_failed(self, snow_client, config: Config):
        route = respx.post(url__startswith=f"{SNOW}/api/now/cmdb/instance").mock(
            return_value=httpx.Response(400, json={"error": {"message": "Invalid data source"}})
        )
        writer = self._writer(snow_client, config.servicenow)

        results = writer.write(self._batch(200))

        # Default SNOW_ABORT_AFTER_ERRORS=10. Concurrency means a few extra
        # requests can be in flight when the guard trips, so assert the order
        # of magnitude rather than an exact count.
        assert route.call_count < 30
        assert writer.aborted is not None
        assert "Invalid data source" in writer.aborted
        assert len(results) == 200
        assert all(r.action == "error" for r in results)
        assert any("not attempted" in (r.message or "") for r in results)

    @respx.mock
    def test_a_few_bad_devices_do_not_stop_a_working_run(self, snow_client, config: Config):
        """The guard trips only while *nothing* has succeeded, so a fleet with
        genuinely bad records still writes the good ones."""
        def respond(request):
            body = json.loads(request.content)
            # Every 20th device, offset so successes land first: the guard must
            # see a success before the failure count reaches the threshold.
            if int(body["attributes"]["serial_number"].removeprefix("SN")) % 20 == 7:
                return httpx.Response(400, json={"error": {"message": "bad record"}})
            return httpx.Response(
                201,
                json={"result": {"attributes": {
                    "sys_id": "ci-x", "sys_created_on": "a", "sys_updated_on": "b"}}},
            )

        route = respx.post(url__startswith=f"{SNOW}/api/now/cmdb/instance").mock(
            side_effect=respond
        )
        writer = self._writer(snow_client, config.servicenow)

        results = writer.write(self._batch(200))

        assert writer.aborted is None
        assert route.call_count == 200
        assert sum(1 for r in results if r.action == "error") == 10
        assert sum(1 for r in results if r.action == "updated") == 190

    @respx.mock
    def test_the_guard_can_be_disabled(self, set_env, snow_client):
        set_env(SNOW_WRITE_MODE="cmdb_instance", SNOW_ABORT_AFTER_ERRORS="0")
        cfg = Config.from_env()
        route = respx.post(url__startswith=f"{SNOW}/api/now/cmdb/instance").mock(
            return_value=httpx.Response(400, json={"error": {"message": "no"}})
        )
        writer = self._writer(snow_client, cfg.servicenow)
        writer.write(self._batch(50))
        assert writer.aborted is None
        assert route.call_count == 50

    @respx.mock
    def test_the_guard_persists_across_batches(self, snow_client, config: Config):
        """SNOW_BATCH_SIZE chunks the run, and the writer outlives a chunk."""
        route = respx.post(url__startswith=f"{SNOW}/api/now/cmdb/instance").mock(
            return_value=httpx.Response(400, json={"error": {"message": "no"}})
        )
        writer = self._writer(snow_client, config.servicenow)
        writer.write(self._batch(100))
        calls_after_first = route.call_count
        writer.write(self._batch(100))
        assert route.call_count == calls_after_first


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

class TestVerifyCmdbInstanceAccess:
    """On an instance where the OAuth client is refused identifyreconcile but
    not the CMDB Instance API -- observed live 2026-09-04 -- this mode is the
    write path, so `--check` has to actually check it. It still cannot create a
    CI to find out: it posts to a class that cannot exist."""

    def _client(self, set_env) -> tuple[ServiceNowClient, Config]:
        set_env(SNOW_WRITE_MODE="cmdb_instance")
        cfg = Config.from_env()
        client = ServiceNowClient(cfg.servicenow)
        client.auth._token = "snow-token"
        client.auth._expires_at = float("inf")
        return client, cfg

    def _probe_route(self, status: int, **kwargs):
        return respx.post(
            url__startswith=f"{SNOW}/api/now/cmdb/instance/"
        ).mock(return_value=httpx.Response(status, **kwargs))

    def _source_registered(self, rows=({"value": "Intune"},)):
        return respx.get(f"{SNOW}/api/now/table/sys_choice").mock(
            return_value=httpx.Response(200, json={"result": list(rows)})
        )

    @respx.mock
    def test_a_rejected_probe_class_proves_the_endpoint_is_callable(self, set_env):
        client, cfg = self._client(set_env)
        route = self._probe_route(400, json={"error": {"message": "Invalid class"}})
        self._source_registered()

        check = verify_write_access(client, cfg.servicenow)
        assert check.verified
        # The probe reached the API with nowhere to write.
        assert PROBE_CLASS in str(route.calls[0].request.url)
        assert json.loads(route.calls[0].request.content) == {}

    @respx.mock
    def test_the_oauth_gate_is_still_reported_as_the_gate(self, set_env):
        client, cfg = self._client(set_env)
        self._probe_route(403, json=UNSCOPED_403)
        with pytest.raises(ServiceNowError) as exc:
            verify_write_access(client, cfg.servicenow)
        assert "REST API Auth Scope" in str(exc.value)
        assert "It needs the 'itil'" not in str(exc.value)

    @respx.mock
    def test_an_ordinary_403_still_names_the_role(self, set_env):
        client, cfg = self._client(set_env)
        self._probe_route(403, text="insufficient rights")
        with pytest.raises(ServiceNowError) as exc:
            verify_write_access(client, cfg.servicenow)
        assert "itil" in str(exc.value)

    @respx.mock
    def test_an_absent_api_is_unverified_not_a_failure(self, set_env):
        client, cfg = self._client(set_env)
        self._probe_route(
            404,
            json={"error": {"message": "Requested URI does not represent any resource"}},
        )
        check = verify_write_access(client, cfg.servicenow)
        assert not check.verified

    @respx.mock
    def test_an_unregistered_discovery_source_is_a_caveat(self, set_env):
        """It does not make the endpoint uncallable, but it does make the first
        write fail, and the error alone never says where to register it."""
        client, cfg = self._client(set_env)
        self._probe_route(400, json={"error": {"message": "Invalid class"}})
        self._source_registered(rows=())

        check = verify_write_access(client, cfg.servicenow)
        assert check.verified
        assert any("cmdb_ci.discovery_source" in c for c in check.caveats)

    @respx.mock
    def test_identification_rules_are_always_flagged_as_unproven(self, set_env):
        """`verified` here means callable, not "the first run will succeed".
        Collapsing that distinction is what made this check worth having."""
        client, cfg = self._client(set_env)
        self._probe_route(400, json={"error": {"message": "Invalid class"}})
        self._source_registered()
        check = verify_write_access(client, cfg.servicenow)
        assert any("identification rules" in c for c in check.caveats)
        assert any("--limit 1" in c for c in check.caveats)

    @respx.mock
    def test_retirement_is_flagged_as_a_separately_scoped_api(self, set_env):
        """A run can write every CI and then fail only at retirement, because
        that is a Table API PATCH behind its own auth scope."""
        set_env(
            SNOW_WRITE_MODE="cmdb_instance",
            SNOW_RETIRE_MISSING="true",
            STATE_PATH="/tmp/intune-cmdb-sync-test-state.json",
        )
        cfg = Config.from_env()
        client = ServiceNowClient(cfg.servicenow)
        client.auth._token = "snow-token"
        client.auth._expires_at = float("inf")
        self._probe_route(400, json={"error": {"message": "Invalid class"}})
        self._source_registered()
        check = verify_write_access(client, cfg.servicenow)
        assert any("--check-api" in c for c in check.caveats)


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
        """When this API is gated too, its per-device error has to explain the
        gate rather than read as a plain permissions failure. Whether it *is*
        gated is per-instance -- see TestVerifyCmdbInstanceAccess for the case
        where identifyreconcile is refused and this endpoint is not."""
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
