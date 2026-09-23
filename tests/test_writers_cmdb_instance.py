from __future__ import annotations

import json

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.servicenow.client import ServiceNowClient
from intune_cmdb_sync.servicenow.writers import (
    CiPayload,
    CmdbInstanceWriter,
    IdentifyReconcileWriter,
)

SNOW = "https://acme.service-now.com"

# Verbatim body of the 403 ServiceNow returns when an OAuth client is not
# authorised for a global-scope API, observed 2026-08-28.
UNSCOPED_403 = {
    "error": {
        "message": "User Not Authorized",
        "detail": "Access to unscoped api is not allowed",
    },
    "status": "failure",
}

# The verbatim body this endpoint returns when the discovery source is not a
# registered choice value, captured live 2026-09-04. The message sits behind
# enough identification bookkeeping that a 400-character snippet of the raw
# body cuts off mid-sentence.
INVALID_DATA_SOURCE = {
    "result": {
        "items": [
            {
                "identifierEntrySysId": "Unknown",
                "identificationAttempts": [],
                "info": [],
                "errorCount": 1,
                "markers": [],
                "warningCount": 0,
                "inputIndices": [0],
                "mergedPayloadIds": [],
                "className": "cmdb_ci_computer",
                "sysId": "Unknown",
                "errors": [
                    {
                        "error": "INVALID_INPUT_DATA",
                        "message": (
                            "In payload invalid data source [Intune] exist. You need to "
                            "provide a valid choice value from field [discovery_source] "
                            "in table [cmdb_ci]"
                        ),
                    }
                ],
            }
        ]
    }
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


class TestCmdbInstanceErrorReporting:
    """This endpoint runs through IRE, so a rejected write comes back as an IRE
    result envelope rather than a plain error. Reporting the raw body truncates
    away the only sentence worth reading."""

    def _error(self, snow_client, config: Config, response: httpx.Response) -> str:
        respx.post(f"{SNOW}/api/now/cmdb/instance/cmdb_ci_computer").mock(
            return_value=response
        )
        result = CmdbInstanceWriter(snow_client, config.servicenow).write([payload()])[0]
        assert result.action == "error"
        return result.message

    @respx.mock
    def test_the_structured_error_is_reported_not_the_raw_body(
        self, snow_client, config: Config
    ):
        message = self._error(
            snow_client, config, httpx.Response(400, json=INVALID_DATA_SOURCE)
        )
        assert "INVALID_INPUT_DATA" in message
        # The whole sentence survives, including the table the field lives on --
        # which is past the truncation point of the raw body.
        assert "in table [cmdb_ci]" in message
        assert "identificationAttempts" not in message

    @respx.mock
    def test_it_says_where_the_discovery_source_has_to_be_registered(
        self, snow_client, config: Config
    ):
        """The API names the field but not that the value is a choice list entry
        somebody has to add."""
        message = self._error(
            snow_client, config, httpx.Response(400, json=INVALID_DATA_SOURCE)
        )
        assert "cmdb_ci.discovery_source" in message
        assert "SNOW_DISCOVERY_SOURCE" in message

    @respx.mock
    def test_an_unstructured_error_still_falls_back_to_the_body(
        self, snow_client, config: Config
    ):
        """Not every failure carries an IRE envelope; those must not be lost."""
        message = self._error(
            snow_client, config, httpx.Response(400, text="Bad Request, no envelope")
        )
        assert "Bad Request, no envelope" in message
        assert "POST /api/now/cmdb/instance/cmdb_ci_computer" in message

    @respx.mock
    def test_the_oauth_gate_explanation_survives(self, snow_client, config: Config):
        message = self._error(snow_client, config, httpx.Response(403, json=UNSCOPED_403))
        assert "REST API Auth Scope" in message


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
