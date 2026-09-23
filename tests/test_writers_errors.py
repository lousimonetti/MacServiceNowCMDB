from __future__ import annotations

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.errors import ServiceNowError
from intune_cmdb_sync.servicenow.client import ServiceNowClient
from intune_cmdb_sync.servicenow.writers import (
    CiPayload,
    CmdbInstanceWriter,
    IdentifyReconcileWriter,
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
        gated is per-instance -- see TestVerifyCmdbInstanceAccess in
        test_writers_access.py for the case where identifyreconcile is refused
        and this endpoint is not."""
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
