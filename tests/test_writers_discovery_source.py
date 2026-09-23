from __future__ import annotations

import json

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.errors import ServiceNowError
from intune_cmdb_sync.servicenow.client import ServiceNowClient
from intune_cmdb_sync.servicenow.writers import register_discovery_source

SNOW = "https://acme.service-now.com"


class TestRegisterDiscoverySource:
    """Every write is rejected until `cmdb_ci.discovery_source` carries the
    configured value, and on dpsnowdev nothing resembling 'Intune' exists. That
    is one row; `POST /api/now/table/sys_choice` is the same operation a
    ServiceNow admin performs through Choice Lists."""

    CHOICE = f"{SNOW}/api/now/table/sys_choice"

    def _client(self, set_env):
        set_env(SNOW_WRITE_MODE="cmdb_instance", SNOW_DISCOVERY_SOURCE="Intune")
        cfg = Config.from_env()
        client = ServiceNowClient(cfg.servicenow)
        client.auth._token = "snow-token"
        client.auth._expires_at = float("inf")
        return client, cfg

    @respx.mock
    def test_creates_the_choice_and_reads_it_back(self, set_env):
        client, cfg = self._client(set_env)
        reads = respx.get(self.CHOICE).mock(
            side_effect=[
                httpx.Response(200, json={"result": []}),            # absent before
                httpx.Response(200, json={"result": [{"value": "Intune"}]}),  # present after
            ]
        )
        create = respx.post(self.CHOICE).mock(
            return_value=httpx.Response(201, json={"result": {"sys_id": "choice-1"}})
        )

        detail = register_discovery_source(client, cfg.servicenow)

        assert "registered 'Intune'" in detail and "choice-1" in detail
        sent = json.loads(create.calls[0].request.content)
        assert sent["name"] == "cmdb_ci"
        assert sent["element"] == "discovery_source"
        assert sent["value"] == "Intune"
        assert sent["inactive"] == "false"
        # Read back rather than trusting the 201: a data policy can accept the
        # insert and store something else, and the value must match exactly.
        assert reads.call_count == 2

    @respx.mock
    def test_an_already_registered_value_writes_nothing(self, set_env):
        client, cfg = self._client(set_env)
        respx.get(self.CHOICE).mock(
            return_value=httpx.Response(200, json={"result": [{"value": "Intune"}]})
        )
        create = respx.post(self.CHOICE)

        detail = register_discovery_source(client, cfg.servicenow)

        assert "already a registered choice value" in detail
        assert create.call_count == 0

    @respx.mock
    def test_a_refusal_prints_the_record_for_an_admin(self, set_env):
        """An integration user is not normally allowed to write sys_choice, so
        this failure is an ordinary outcome -- and the useful thing to produce
        is a ticket someone can act on without a second round trip."""
        client, cfg = self._client(set_env)
        respx.get(self.CHOICE).mock(return_value=httpx.Response(200, json={"result": []}))
        respx.post(self.CHOICE).mock(
            return_value=httpx.Response(403, text="Insufficient rights on sys_choice")
        )

        with pytest.raises(ServiceNowError) as exc:
            register_discovery_source(client, cfg.servicenow)

        message = str(exc.value)
        assert "personalize_choices" in message
        assert "name='cmdb_ci'" in message
        assert "element='discovery_source'" in message
        assert "value='Intune'" in message

    @respx.mock
    def test_an_insert_that_did_not_take_is_not_reported_as_success(self, set_env):
        client, cfg = self._client(set_env)
        respx.get(self.CHOICE).mock(return_value=httpx.Response(200, json={"result": []}))
        respx.post(self.CHOICE).mock(
            return_value=httpx.Response(201, json={"result": {"sys_id": "choice-1"}})
        )

        with pytest.raises(ServiceNowError, match="still not"):
            register_discovery_source(client, cfg.servicenow)

    @respx.mock
    def test_an_inactive_existing_choice_is_not_treated_as_registered(self, set_env):
        client, cfg = self._client(set_env)
        respx.get(self.CHOICE).mock(
            side_effect=[
                httpx.Response(200, json={"result": [{"value": "Intune", "inactive": "true"}]}),
                httpx.Response(200, json={"result": [{"value": "Intune"}]}),
            ]
        )
        create = respx.post(self.CHOICE).mock(
            return_value=httpx.Response(201, json={"result": {"sys_id": "choice-2"}})
        )
        register_discovery_source(client, cfg.servicenow)
        assert create.call_count == 1
