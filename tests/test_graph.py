from __future__ import annotations

import json
import logging
import time

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.errors import AuthError, GraphError
from intune_cmdb_sync.graph import (
    TOKEN_EXCHANGE_SCOPE,
    GraphClient,
    StaticTokenProvider,
    build_credential,
    is_corporate,
)

from .conftest import make_device

GRAPH = "https://graph.microsoft.com/v1.0"
DEVICES = f"{GRAPH}/deviceManagement/managedDevices"
# Hardware detail is batched against beta, which alone has processorArchitecture.
BETA_BATCH = "https://graph.microsoft.com/beta/$batch"


@pytest.fixture
def graph(config: Config) -> GraphClient:
    return GraphClient(config.graph, token_provider=lambda: "fake-token")


class TestOwnershipFilter:
    @pytest.mark.parametrize(
        ("owner_type", "ownership", "expected"),
        [
            ("company", "company", True),
            ("Company", "company", True),
            ("personal", "company", False),
            ("unknown", "company", False),
            (None, "company", False),
            ("personal", "any", True),
            ("personal", "personal", True),
        ],
    )
    def test_client_side_check(self, owner_type, ownership, expected):
        assert is_corporate({"managedDeviceOwnerType": owner_type}, ownership) is expected


class TestIterManagedDevices:
    @respx.mock
    def test_sends_select_top_and_filter(self, graph: GraphClient):
        route = respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device()]})
        )
        list(graph.iter_managed_devices())
        request = route.calls[0].request
        assert request.url.params["$filter"] == "managedDeviceOwnerType eq 'company'"
        assert request.url.params["$top"] == "200"
        assert "serialNumber" in request.url.params["$select"]
        assert request.headers["Authorization"] == "Bearer fake-token"

    @respx.mock
    def test_follows_next_link_without_resending_params(self, graph: GraphClient):
        page_two = f"{DEVICES}?$skiptoken=abc123"
        respx.get(DEVICES, params__contains={"$top": "200"}).mock(
            return_value=httpx.Response(
                200,
                json={"value": [make_device(id="one")], "@odata.nextLink": page_two},
            )
        )
        second = respx.get(DEVICES, params__contains={"$skiptoken": "abc123"}).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="two")]})
        )
        devices = list(graph.iter_managed_devices())
        assert [d["id"] for d in devices] == ["one", "two"]
        assert "$select" not in second.calls[0].request.url.params

    @respx.mock
    def test_falls_back_when_server_rejects_the_filter(self, graph: GraphClient):
        route = respx.get(DEVICES).mock(
            side_effect=[
                httpx.Response(400, json={"error": {"message": "Unsupported filter"}}),
                httpx.Response(200, json={"value": [make_device()]}),
            ]
        )
        devices = list(graph.iter_managed_devices())
        assert len(devices) == 1
        assert "$filter" in route.calls[0].request.url.params
        assert "$filter" not in route.calls[1].request.url.params

    @respx.mock
    def test_filter_can_be_disabled(self, set_env):
        set_env(INTUNE_SERVER_SIDE_FILTER="false")
        client = GraphClient(Config.from_env().graph, token_provider=lambda: "t")
        route = respx.get(DEVICES).mock(return_value=httpx.Response(200, json={"value": []}))
        list(client.iter_managed_devices())
        assert "$filter" not in route.calls[0].request.url.params

    @respx.mock
    def test_non_400_failure_raises(self, graph: GraphClient):
        respx.get(DEVICES).mock(return_value=httpx.Response(403, text="Forbidden"))
        with pytest.raises(GraphError, match="listing managed devices failed"):
            list(graph.iter_managed_devices())

    @respx.mock
    def test_empty_tenant_yields_nothing(self, graph: GraphClient):
        respx.get(DEVICES).mock(return_value=httpx.Response(200, json={"value": []}))
        assert list(graph.iter_managed_devices()) == []


def _batch_echo(status_for=lambda url: 200, body_for=lambda url: {}):
    """A `$batch` responder that answers every sub-request from its URL."""

    def responder(request: httpx.Request) -> httpx.Response:
        requests = json.loads(request.read())["requests"]
        assert len(requests) <= 20
        return httpx.Response(
            200,
            json={
                "responses": [
                    {"id": r["id"], "status": status_for(r["url"]), "body": body_for(r["url"])}
                    for r in requests
                ]
            },
        )

    return responder


def _device_id(url: str) -> str:
    return url.split("/managedDevices/")[1].split("?")[0]


class TestHardwareDetail:
    @respx.mock
    def test_batches_twenty_devices_per_request(self, graph: GraphClient):
        ids = [f"dev-{i}" for i in range(45)]
        route = respx.post(BETA_BATCH).mock(
            side_effect=_batch_echo(
                body_for=lambda url: {"id": _device_id(url), "physicalMemoryInBytes": 8 << 30}
            )
        )
        details = graph.get_hardware_details(ids)
        assert set(details) == set(ids)
        assert route.call_count == 3

    @respx.mock
    def test_selects_non_default_properties(self, graph: GraphClient):
        route = respx.post(BETA_BATCH).mock(
            side_effect=_batch_echo(
                body_for=lambda url: {"id": "dev-1", "physicalMemoryInBytes": 17179869184}
            )
        )
        detail = graph.get_hardware_details(["dev-1"])["dev-1"]
        assert detail["physicalMemoryInBytes"] == 17179869184
        url = json.loads(route.calls[0].request.read())["requests"][0]["url"]
        assert url.startswith("/deviceManagement/managedDevices/dev-1?$select=")
        assert "physicalMemoryInBytes" in url and "ethernetMacAddress" in url
        assert "processorArchitecture" in url

    @respx.mock
    def test_goes_to_beta_not_v1(self, graph: GraphClient):
        """processorArchitecture does not exist in v1.0."""
        v1 = respx.post(f"{GRAPH}/$batch")
        beta = respx.post(BETA_BATCH).mock(side_effect=_batch_echo())
        graph.get_hardware_details(["dev-1"])
        assert beta.call_count == 1 and v1.call_count == 0

    @respx.mock
    def test_failed_sub_request_leaves_the_device_out(self, graph: GraphClient):
        respx.post(BETA_BATCH).mock(
            side_effect=_batch_echo(
                status_for=lambda url: 404 if "dev-2" in url else 200,
                body_for=lambda url: {"id": _device_id(url)},
            )
        )
        assert set(graph.get_hardware_details(["dev-1", "dev-2"])) == {"dev-1"}

    @respx.mock
    def test_throttled_sub_requests_are_retried(self, graph: GraphClient, monkeypatch):
        monkeypatch.setattr("intune_cmdb_sync.graph.time.sleep", lambda _s: None)
        respx.post(BETA_BATCH).mock(
            side_effect=[
                httpx.Response(200, json={"responses": [{"id": "0", "status": 429, "body": {}}]}),
                httpx.Response(
                    200, json={"responses": [{"id": "0", "status": 200, "body": {"id": "dev-1"}}]}
                ),
            ]
        )
        assert set(graph.get_hardware_details(["dev-1"])) == {"dev-1"}

    @respx.mock
    def test_batch_level_failure_degrades_instead_of_raising(self, graph: GraphClient):
        """Hardware detail is optional; losing it must not fail the whole sync."""
        respx.post(BETA_BATCH).mock(return_value=httpx.Response(401, text="unauth"))
        assert graph.get_hardware_details(["dev-1"]) == {}

    @respx.mock
    def test_exhausted_retries_degrade_instead_of_raising(self, graph: GraphClient):
        """A batch throttled past the retry budget raises inside the HTTP client.
        For optional hardware detail that must cost the fields, not the run."""
        graph._http._sleep = lambda _s: None
        respx.post(BETA_BATCH).mock(return_value=httpx.Response(503, text="busy"))
        assert graph.get_hardware_details(["dev-1"]) == {}

    @respx.mock
    def test_empty_input_makes_no_request(self, graph: GraphClient):
        route = respx.post(BETA_BATCH)
        assert graph.get_hardware_details([]) == {}
        assert route.call_count == 0


SCRIPTS = "https://graph.microsoft.com/beta/deviceManagement/deviceCustomAttributeShellScripts"


class TestCustomAttributeValues:
    def _scripts(self, *scripts):
        return respx.get(SCRIPTS).mock(
            return_value=httpx.Response(200, json={"value": list(scripts)})
        )

    @respx.mock
    def test_reads_successful_outputs_by_device_across_pages(self, graph: GraphClient):
        self._scripts({"id": "s1", "customAttributeName": "RAM bytes", "displayName": "Mac RAM"})
        page2 = f"{SCRIPTS}/s1/deviceRunStates?$skiptoken=2"
        states = respx.get(f"{SCRIPTS}/s1/deviceRunStates").mock(
            side_effect=[
                httpx.Response(200, json={
                    "value": [
                        {"runState": "success", "resultMessage": "17179869184\n",
                         "managedDevice": {"id": "mac-1"}},
                        {"runState": "fail", "resultMessage": "boom",
                         "managedDevice": {"id": "mac-2"}},
                    ],
                    "@odata.nextLink": page2,
                }),
                httpx.Response(200, json={"value": [
                    {"runState": "success", "resultMessage": "34359738368",
                     "managedDevice": {"id": "mac-3"}},
                ]}),
            ]
        )
        values = graph.get_custom_attribute_values("ram BYTES")
        assert values == {"mac-1": "17179869184", "mac-3": "34359738368"}
        assert states.calls[0].request.url.params["$expand"] == "managedDevice($select=id)"

    @respx.mock
    def test_rejected_nested_select_falls_back_to_plain_expand(self, graph: GraphClient):
        self._scripts({"id": "s1", "customAttributeName": "RAM bytes"})
        states = respx.get(f"{SCRIPTS}/s1/deviceRunStates").mock(
            side_effect=[
                httpx.Response(400, json={"error": {"message": "bad $expand"}}),
                httpx.Response(200, json={"value": [
                    {"runState": "success", "resultMessage": "8589934592",
                     "managedDevice": {"id": "mac-1", "deviceName": "MDS-1"}},
                ]}),
            ]
        )
        assert graph.get_custom_attribute_values("RAM bytes") == {"mac-1": "8589934592"}
        assert states.calls[1].request.url.params["$expand"] == "managedDevice"

    @respx.mock
    def test_matches_display_name_or_id(self, graph: GraphClient):
        self._scripts({"id": "s1", "customAttributeName": "ram", "displayName": "Mac RAM"})
        respx.get(f"{SCRIPTS}/s1/deviceRunStates").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        assert graph.get_custom_attribute_values("Mac RAM") == {}
        assert graph.get_custom_attribute_values("s1") == {}

    @respx.mock
    def test_missing_attribute_is_an_error(self, graph: GraphClient):
        self._scripts({"id": "s1", "customAttributeName": "Battery", "displayName": "Battery"})
        with pytest.raises(GraphError, match="no macOS custom attribute named 'RAM bytes'"):
            graph.get_custom_attribute_values("RAM bytes")

    @respx.mock
    def test_ambiguous_name_is_an_error(self, graph: GraphClient):
        self._scripts(
            {"id": "s1", "customAttributeName": "RAM bytes", "displayName": "A"},
            {"id": "s2", "customAttributeName": "RAM bytes", "displayName": "B"},
        )
        with pytest.raises(GraphError, match="use its id"):
            graph.get_custom_attribute_values("RAM bytes")

    @respx.mock
    def test_missing_permission_is_an_error_naming_it(self, graph: GraphClient):
        """The shape Graph returned live on 2026-10-14 without the permission."""
        respx.get(SCRIPTS).mock(
            return_value=httpx.Response(403, json={"error": {
                "code": "Forbidden",
                "message": "Application must have one of the following scopes: "
                           "DeviceManagementScripts.Read.All",
            }})
        )
        with pytest.raises(GraphError, match=r"DeviceManagementScripts\.Read\.All"):
            graph.get_custom_attribute_values("RAM bytes")


class TestGetUsers:
    @respx.mock
    def test_batches_at_twenty_per_request(self, graph: GraphClient):
        ids = [f"user-{i}" for i in range(45)]

        def responder(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            import json as _json

            requests = _json.loads(body)["requests"]
            assert len(requests) <= 20
            return httpx.Response(
                200,
                json={
                    "responses": [
                        {
                            "id": r["id"],
                            "status": 200,
                            "body": {"id": r["url"].split("/users/")[1].split("?")[0]},
                        }
                        for r in requests
                    ]
                },
            )

        route = respx.post(f"{GRAPH}/$batch").mock(side_effect=responder)
        users = graph.get_users(ids)
        assert len(users) == 45
        assert route.call_count == 3

    @respx.mock
    def test_maps_fields_and_skips_missing_users(self, graph: GraphClient):
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(
                200,
                json={
                    "responses": [
                        {
                            "id": "0",
                            "status": 200,
                            "body": {
                                "id": "u1",
                                "userPrincipalName": "lou@example.com",
                                "mail": "lou.simonetti@example.com",
                                "employeeId": "E4242",
                                "department": "Platform",
                            },
                        },
                        {"id": "1", "status": 404, "body": {"error": {"code": "NotFound"}}},
                    ]
                },
            )
        )
        users = graph.get_users(["u1", "u2"])
        assert set(users) == {"u1"}
        assert users["u1"].employee_id == "E4242"
        assert users["u1"].primary_email == "lou.simonetti@example.com"

    @respx.mock
    def test_deduplicates_ids(self, graph: GraphClient):
        route = respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(
                200, json={"responses": [{"id": "0", "status": 200, "body": {"id": "u1"}}]}
            )
        )
        graph.get_users(["u1", "u1", "u1"])
        import json as _json

        assert len(_json.loads(route.calls[0].request.read())["requests"]) == 1

    @respx.mock
    def test_empty_input_makes_no_request(self, graph: GraphClient):
        route = respx.post(f"{GRAPH}/$batch")
        assert graph.get_users([]) == {}
        assert route.call_count == 0

    @respx.mock
    def test_throttled_sub_requests_are_retried(self, graph: GraphClient, monkeypatch):
        monkeypatch.setattr("intune_cmdb_sync.graph.time.sleep", lambda _s: None)
        respx.post(f"{GRAPH}/$batch").mock(
            side_effect=[
                httpx.Response(200, json={"responses": [{"id": "0", "status": 429, "body": {}}]}),
                httpx.Response(
                    200, json={"responses": [{"id": "0", "status": 200, "body": {"id": "u1"}}]}
                ),
            ]
        )
        assert set(graph.get_users(["u1"])) == {"u1"}

    @respx.mock
    def test_batch_level_failure_raises(self, graph: GraphClient):
        respx.post(f"{GRAPH}/$batch").mock(return_value=httpx.Response(401, text="unauth"))
        with pytest.raises(GraphError, match=r"user \$batch failed"):
            graph.get_users(["u1"])


class TestCredentialSelection:
    """`build_credential` maps the configured mode onto an azure-identity type.
    These assert the wiring, not azure-identity's own behaviour."""

    def test_federated_managed_identity_builds_a_client_assertion(self, set_env):
        from azure.identity import ClientAssertionCredential

        set_env(
            GRAPH_AUTH_MODE="federated_managed_identity",
            GRAPH_CLIENT_SECRET=None,
            GRAPH_ASSERTION_IDENTITY_CLIENT_ID="33333333-3333-3333-3333-333333333333",
        )
        credential = build_credential(Config.from_env().graph)
        assert isinstance(credential, ClientAssertionCredential)

    def test_the_assertion_targets_the_token_exchange_audience(self, set_env, monkeypatch):
        """The audience is fixed by Entra. Getting it wrong fails at runtime with
        an error that does not name the audience, so pin it."""
        requested: list[str] = []

        class FakeToken:
            token = "assertion-jwt"

        class FakeIdentity:
            def __init__(self, *a, **k): ...
            def get_token(self, *scopes, **kwargs):
                requested.extend(scopes)
                return FakeToken()

        monkeypatch.setattr("azure.identity.ManagedIdentityCredential", FakeIdentity)
        set_env(GRAPH_AUTH_MODE="federated_managed_identity", GRAPH_CLIENT_SECRET=None)

        credential = build_credential(Config.from_env().graph)
        # ClientAssertionCredential stores the callable; invoke it directly.
        assert credential._func() == "assertion-jwt"
        assert requested == [TOKEN_EXCHANGE_SCOPE]
        assert TOKEN_EXCHANGE_SCOPE == "api://AzureADTokenExchange/.default"

    def test_client_secret_mode_is_unchanged(self, set_env):
        from azure.identity import ClientSecretCredential

        set_env(GRAPH_AUTH_MODE="client_secret")
        assert isinstance(build_credential(Config.from_env().graph), ClientSecretCredential)


def _jwt(**claims) -> str:
    """Build an unsigned JWT. The connector never verifies signatures — Entra
    already did — so an unsigned token exercises the same code path."""
    import base64
    import json

    def seg(d):
        raw = json.dumps(d).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{seg({'alg': 'none'})}.{seg(claims)}.signature"


class TestStaticAccessToken:
    """GRAPH_AUTH_MODE=access_token: local development against a tenant where you
    cannot create an app registration. The value is short-lived and cannot be
    refreshed, so the failure modes have to be named rather than discovered."""

    def _future(self, minutes=55):
        return int(time.time()) + minutes * 60

    def test_serves_the_token_verbatim(self):
        token = _jwt(aud="https://graph.microsoft.com", exp=self._future())
        assert StaticTokenProvider(token)() == token

    def test_strips_a_pasted_bearer_prefix(self):
        """Copying from a browser's network tab brings the scheme along."""
        token = _jwt(aud="https://graph.microsoft.com", exp=self._future())
        assert StaticTokenProvider(f"Bearer {token}")() == token

    def test_rejects_a_token_for_the_wrong_audience(self):
        """The most common mistake: a token for ARM, not Graph. It looks valid
        and fails with a 401 that does not mention audience."""
        token = _jwt(aud="https://management.azure.com", exp=self._future())
        with pytest.raises(AuthError) as exc:
            StaticTokenProvider(token)
        assert "management.azure.com" in str(exc.value)
        assert "graph.microsoft.com" in str(exc.value)

    def test_rejects_an_already_expired_token(self):
        token = _jwt(aud="https://graph.microsoft.com", exp=int(time.time()) - 600)
        with pytest.raises(AuthError) as exc:
            StaticTokenProvider(token)
        assert "expired" in str(exc.value).lower()

    def test_rejects_an_empty_token(self):
        with pytest.raises(AuthError):
            StaticTokenProvider("   ")

    def test_an_opaque_token_is_a_warning_not_an_error(self, caplog):
        """We cannot read it, but that is not proof it is bad."""
        with caplog.at_level(logging.WARNING):
            assert StaticTokenProvider("not-a-jwt")() == "not-a-jwt"
        assert any("not a readable JWT" in r.getMessage() for r in caplog.records)

    def test_warns_that_this_is_development_only(self, caplog):
        token = _jwt(aud="https://graph.microsoft.com", exp=self._future(),
                     roles=["DeviceManagementManagedDevices.Read.All"])
        with caplog.at_level(logging.WARNING):
            StaticTokenProvider(token)
        assert any("LOCAL DEVELOPMENT ONLY" in r.getMessage() for r in caplog.records)

    def test_accepts_the_app_id_audience_form(self):
        """Entra issues the Graph app id as the audience for v1 tokens."""
        token = _jwt(aud="00000003-0000-0000-c000-000000000000", exp=self._future())
        assert StaticTokenProvider(token)()

    @respx.mock
    def test_client_uses_the_token_without_touching_azure_identity(self, set_env, monkeypatch):
        """The whole point: no credential is constructed, so no app registration
        and no secret are needed."""
        def explode(_cfg):
            raise AssertionError("build_credential must not be called in access_token mode")
        monkeypatch.setattr("intune_cmdb_sync.graph.build_credential", explode)

        token = _jwt(aud="https://graph.microsoft.com", exp=self._future())
        set_env(GRAPH_AUTH_MODE="access_token", GRAPH_ACCESS_TOKEN=token,
                GRAPH_CLIENT_SECRET=None)

        route = respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device()]})
        )
        client = GraphClient(Config.from_env().graph)
        assert len(list(client.iter_managed_devices())) == 1
        assert route.calls[0].request.headers["Authorization"] == f"Bearer {token}"

    def test_warns_when_the_token_lacks_intune_permission(self, caplog):
        """The az CLI's token is the common case: valid, correct audience, and
        entirely unable to read managedDevices."""
        token = _jwt(
            aud="https://graph.microsoft.com", exp=self._future(),
            scp="User.Read.All Directory.AccessAsUser.All",
        )
        with caplog.at_level(logging.WARNING):
            StaticTokenProvider(token)
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "no Intune device-read permission" in messages
        assert "az account get-access-token" in messages

    def test_no_permission_warning_when_the_app_role_is_present(self, caplog):
        token = _jwt(aud="https://graph.microsoft.com", exp=self._future(),
                     roles=["DeviceManagementManagedDevices.Read.All"])
        with caplog.at_level(logging.WARNING):
            StaticTokenProvider(token)
        assert "no Intune device-read permission" not in " ".join(
            r.getMessage() for r in caplog.records
        )

    def test_readwrite_scope_also_satisfies_the_check(self, caplog):
        token = _jwt(aud="https://graph.microsoft.com", exp=self._future(),
                     scp="DeviceManagementManagedDevices.ReadWrite.All")
        with caplog.at_level(logging.WARNING):
            StaticTokenProvider(token)
        assert "no Intune device-read permission" not in " ".join(
            r.getMessage() for r in caplog.records
        )
