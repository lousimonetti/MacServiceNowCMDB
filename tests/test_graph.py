from __future__ import annotations

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.errors import GraphError
from intune_cmdb_sync.graph import GraphClient, is_corporate

from .conftest import make_device

GRAPH = "https://graph.microsoft.com/v1.0"
DEVICES = f"{GRAPH}/deviceManagement/managedDevices"


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


class TestHardwareDetail:
    @respx.mock
    def test_selects_non_default_properties(self, graph: GraphClient):
        route = respx.get(f"{DEVICES}/dev-1").mock(
            return_value=httpx.Response(
                200, json={"id": "dev-1", "physicalMemoryInBytes": 17179869184}
            )
        )
        detail = graph.fetch_device_hardware_detail("dev-1")
        assert detail["physicalMemoryInBytes"] == 17179869184
        assert "physicalMemoryInBytes" in route.calls[0].request.url.params["$select"]

    @respx.mock
    def test_failure_degrades_to_empty(self, graph: GraphClient):
        respx.get(f"{DEVICES}/dev-1").mock(return_value=httpx.Response(404, text="gone"))
        assert graph.fetch_device_hardware_detail("dev-1") == {}


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
