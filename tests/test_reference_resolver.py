from __future__ import annotations

import json

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.reference_resolver import ReferenceResolver
from intune_cmdb_sync.servicenow.client import ServiceNowClient

SNOW = "https://acme.service-now.com"
COMPANY = f"{SNOW}/api/now/table/core_company"
MODEL = f"{SNOW}/api/now/table/cmdb_model"


@pytest.fixture
def snow_client(config: Config) -> ServiceNowClient:
    client = ServiceNowClient(config.servicenow)
    client.auth._token = "snow-token"
    client.auth._expires_at = float("inf")
    return client


class TestPrimeAndResolve:
    @respx.mock
    def test_resolves_known_manufacturer_and_model(self, snow_client):
        respx.get(COMPANY).mock(
            return_value=httpx.Response(
                200, json={"result": [{"sys_id": "mfr-1", "name": "Apple"}]}
            )
        )
        respx.get(MODEL).mock(
            return_value=httpx.Response(
                200,
                json={"result": [{"sys_id": "model-1", "name": "MacBook Pro (16-inch, 2023)"}]},
            )
        )
        resolver = ReferenceResolver(snow_client)
        resolver.prime(["Apple"], ["MacBook Pro (16-inch, 2023)"])
        refs = resolver.references_for("Apple", "MacBook Pro (16-inch, 2023)")
        assert refs == {"manufacturer": "mfr-1", "model_id": "model-1"}

    @respx.mock
    def test_lookup_is_case_insensitive(self, snow_client):
        respx.get(COMPANY).mock(
            return_value=httpx.Response(
                200, json={"result": [{"sys_id": "mfr-1", "name": "Apple"}]}
            )
        )
        respx.get(MODEL).mock(return_value=httpx.Response(200, json={"result": []}))
        resolver = ReferenceResolver(snow_client)
        resolver.prime(["Apple"], [])
        assert resolver.references_for("APPLE", None)["manufacturer"] == "mfr-1"

    @respx.mock
    def test_unknown_names_are_reported_not_guessed(self, snow_client):
        respx.get(COMPANY).mock(return_value=httpx.Response(200, json={"result": []}))
        respx.get(MODEL).mock(return_value=httpx.Response(200, json={"result": []}))
        resolver = ReferenceResolver(snow_client)
        resolver.prime(["Framework"], ["Laptop 13"])
        assert resolver.references_for("Framework", "Laptop 13") == {}
        assert resolver.unresolved == {
            "manufacturers": ["Framework"],
            "models": ["Laptop 13"],
        }

    @respx.mock
    def test_blank_names_are_ignored(self, snow_client):
        company = respx.get(COMPANY).mock(return_value=httpx.Response(200, json={"result": []}))
        respx.get(MODEL).mock(return_value=httpx.Response(200, json={"result": []}))
        resolver = ReferenceResolver(snow_client)
        resolver.prime(["", "   "], [])
        assert resolver.references_for(None, "") == {}
        assert company.call_count == 0

    @respx.mock
    def test_names_containing_commas_are_still_looked_up(self, snow_client):
        # Apple model identifiers ("Mac16,1") and company names ("Acme, Inc.")
        # routinely contain commas. An IN-list would silently match nothing.
        company = respx.get(COMPANY).mock(
            return_value=httpx.Response(
                200, json={"result": [{"sys_id": "mfr-1", "name": "Acme, Inc."}]}
            )
        )
        model = respx.get(MODEL).mock(
            return_value=httpx.Response(
                200, json={"result": [{"sys_id": "model-1", "name": "Mac16,1"}]}
            )
        )
        resolver = ReferenceResolver(snow_client)
        resolver.prime(["Acme, Inc."], ["Mac16,1"])
        assert resolver.references_for("Acme, Inc.", "Mac16,1") == {
            "manufacturer": "mfr-1",
            "model_id": "model-1",
        }
        assert "name=Acme, Inc." in company.calls[0].request.url.params["sysparm_query"]
        assert "name=Mac16,1" in model.calls[0].request.url.params["sysparm_query"]

    @respx.mock
    def test_names_containing_a_caret_are_skipped(self, snow_client):
        company = respx.get(COMPANY).mock(return_value=httpx.Response(200, json={"result": []}))
        respx.get(MODEL).mock(return_value=httpx.Response(200, json={"result": []}))
        ReferenceResolver(snow_client).prime(["Acme^Corp"], [])
        assert company.call_count == 0

    @respx.mock
    def test_large_name_sets_are_chunked(self, snow_client):
        company = respx.get(COMPANY).mock(return_value=httpx.Response(200, json={"result": []}))
        respx.get(MODEL).mock(return_value=httpx.Response(200, json={"result": []}))
        ReferenceResolver(snow_client).prime([f"Vendor{i}" for i in range(85)], [])
        assert company.call_count == 3  # 40 + 40 + 5


class TestCreation:
    @respx.mock
    def test_creates_missing_model_when_enabled(self, snow_client):
        respx.get(COMPANY).mock(
            return_value=httpx.Response(
                200, json={"result": [{"sys_id": "mfr-1", "name": "Apple"}]}
            )
        )
        respx.get(MODEL).mock(return_value=httpx.Response(200, json={"result": []}))
        create = respx.post(MODEL).mock(
            return_value=httpx.Response(201, json={"result": {"sys_id": "model-new"}})
        )
        resolver = ReferenceResolver(snow_client, create_missing_models=True)
        resolver.prime(["Apple"], ["Mac16,1"])
        refs = resolver.references_for("Apple", "Mac16,1")
        assert refs["model_id"] == "model-new"
        body = json.loads(create.calls[0].request.read())
        assert body["name"] == "Mac16,1"
        # A new model is linked to its manufacturer so the catalogue stays usable.
        assert body["manufacturer"] == "mfr-1"

    @respx.mock
    def test_creation_is_off_by_default(self, snow_client):
        respx.get(COMPANY).mock(return_value=httpx.Response(200, json={"result": []}))
        respx.get(MODEL).mock(return_value=httpx.Response(200, json={"result": []}))
        create = respx.post(MODEL)
        resolver = ReferenceResolver(snow_client)
        resolver.prime([], [])
        resolver.references_for("Apple", "Mac16,1")
        assert create.call_count == 0

    @respx.mock
    def test_dry_run_never_creates(self, snow_client):
        respx.get(COMPANY).mock(return_value=httpx.Response(200, json={"result": []}))
        respx.get(MODEL).mock(return_value=httpx.Response(200, json={"result": []}))
        create = respx.post(MODEL)
        resolver = ReferenceResolver(snow_client, create_missing_models=True, dry_run=True)
        resolver.prime([], [])
        assert resolver.references_for("Apple", "Mac16,1") == {}
        assert create.call_count == 0

    @respx.mock
    def test_failed_creation_is_not_retried_per_device(self, snow_client):
        respx.get(COMPANY).mock(return_value=httpx.Response(200, json={"result": []}))
        respx.get(MODEL).mock(return_value=httpx.Response(200, json={"result": []}))
        create = respx.post(MODEL).mock(return_value=httpx.Response(403, text="no ACL"))
        resolver = ReferenceResolver(snow_client, create_missing_models=True)
        resolver.prime([], [])
        for _ in range(5):
            resolver.references_for(None, "Mac16,1")
        assert create.call_count == 1
