from __future__ import annotations

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.models import EntraUser
from intune_cmdb_sync.servicenow.client import ServiceNowClient
from intune_cmdb_sync.user_resolver import UserResolver

SNOW = "https://acme.service-now.com"
SYS_USER = f"{SNOW}/api/now/table/sys_user"


@pytest.fixture
def snow_client(config: Config) -> ServiceNowClient:
    client = ServiceNowClient(config.servicenow)
    client.auth._token = "snow-token"
    client.auth._expires_at = float("inf")
    return client


def entra(**overrides) -> EntraUser:
    base = {
        "object_id": "99999999-9999-9999-9999-999999999999",
        "user_principal_name": "lou@example.com",
        "mail": "lou@example.com",
        "employee_id": "E4242",
        "display_name": "Lou Simonetti",
    }
    base.update(overrides)
    return EntraUser(**base)


def row(**overrides) -> dict:
    base = {
        "sys_id": "snow-user-1",
        "user_name": "lou",
        "email": "lou@example.com",
        "employee_number": "E4242",
        "active": "true",
    }
    base.update(overrides)
    return base


class TestMatchOrder:
    @respx.mock
    def test_employee_number_is_tried_first_and_wins(self, snow_client, config: Config):
        route = respx.get(SYS_USER).mock(
            return_value=httpx.Response(200, json={"result": [row()]})
        )
        resolved = UserResolver(snow_client, config.servicenow).resolve_many([entra()])
        ref = resolved["99999999-9999-9999-9999-999999999999"]
        assert ref is not None
        assert ref.matched_on == "employee_number"
        assert ref.sys_id == "snow-user-1"
        # One query only: the first key matched, so email/user_name never ran.
        assert route.call_count == 1
        assert "employee_number=E4242" in route.calls[0].request.url.params["sysparm_query"]

    @respx.mock
    def test_falls_through_to_email_when_employee_number_misses(self, snow_client, config: Config):
        def responder(request: httpx.Request) -> httpx.Response:
            query = request.url.params["sysparm_query"]
            if "email=" in query:
                return httpx.Response(200, json={"result": [row()]})
            return httpx.Response(200, json={"result": []})

        route = respx.get(SYS_USER).mock(side_effect=responder)
        user = entra()  # has an employeeId, but no sys_user carries it
        ref = UserResolver(snow_client, config.servicenow).resolve_many([user])[user.object_id]
        assert ref is not None and ref.matched_on == "email"
        queries = [c.request.url.params["sysparm_query"] for c in route.calls]
        assert any("employee_number=" in q for q in queries)

    @respx.mock
    def test_key_with_no_candidate_values_issues_no_query(self, snow_client, config: Config):
        route = respx.get(SYS_USER).mock(
            return_value=httpx.Response(200, json={"result": [row()]})
        )
        # No employeeId at all, so the employee_number key is skipped entirely
        # rather than querying with an empty IN list (which would match nothing
        # in the best case and everything in the worst).
        user = entra(employee_id=None)
        UserResolver(snow_client, config.servicenow).resolve_many([user])
        queries = [c.request.url.params["sysparm_query"] for c in route.calls]
        assert not any("employee_number=" in q for q in queries)

    @respx.mock
    def test_user_name_tries_upn_then_local_part(self, snow_client, config: Config):
        captured: list[str] = []

        def responder(request: httpx.Request) -> httpx.Response:
            query = request.url.params["sysparm_query"]
            captured.append(query)
            if "user_name=" in query:
                return httpx.Response(200, json={"result": [row()]})
            return httpx.Response(200, json={"result": []})

        respx.get(SYS_USER).mock(side_effect=responder)
        user = entra(employee_id=None, mail=None)
        ref = UserResolver(snow_client, config.servicenow).resolve_many([user])[user.object_id]
        assert ref is not None and ref.matched_on == "user_name"
        user_name_query = next(q for q in captured if "user_name=" in q)
        assert "user_name=lou@example.com" in user_name_query
        assert "user_name=lou^" in user_name_query or user_name_query.endswith("user_name=lou")

    @respx.mock
    def test_custom_order_is_respected(self, set_env, snow_client):
        set_env(SNOW_USER_MATCH_ORDER="email")
        cfg = Config.from_env()
        route = respx.get(SYS_USER).mock(
            return_value=httpx.Response(200, json={"result": [row()]})
        )
        ref = UserResolver(snow_client, cfg.servicenow).resolve_many([entra()])[entra().object_id]
        assert ref is not None and ref.matched_on == "email"
        assert "email=lou@example.com" in route.calls[0].request.url.params["sysparm_query"]

    @respx.mock
    def test_entra_object_id_field(self, set_env, snow_client):
        set_env(
            SNOW_USER_MATCH_ORDER="entra_id",
            SNOW_USER_ENTRA_ID_FIELD="u_entra_object_id",
        )
        cfg = Config.from_env()
        route = respx.get(SYS_USER).mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": [
                        row(u_entra_object_id="99999999-9999-9999-9999-999999999999")
                    ]
                },
            )
        )
        ref = UserResolver(snow_client, cfg.servicenow).resolve_many([entra()])[entra().object_id]
        assert ref is not None and ref.matched_on == "entra_id"
        params = route.calls[0].request.url.params
        assert "u_entra_object_id=99999999-9999-9999-9999-999999999999" in params["sysparm_query"]
        assert "u_entra_object_id" in params["sysparm_fields"]


class TestSafety:
    @respx.mock
    def test_ambiguous_match_assigns_nobody(self, snow_client, config: Config):
        respx.get(SYS_USER).mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": [
                        row(sys_id="a"),
                        row(sys_id="b"),  # two users share the employee number
                    ]
                },
            )
        )
        user = entra(mail=None, user_principal_name=None)
        assert UserResolver(snow_client, config.servicenow).resolve_many([user])[
            user.object_id
        ] is None

    @respx.mock
    def test_no_match_returns_none(self, snow_client, config: Config):
        respx.get(SYS_USER).mock(return_value=httpx.Response(200, json={"result": []}))
        user = entra()
        assert UserResolver(snow_client, config.servicenow).resolve_many([user])[
            user.object_id
        ] is None

    @respx.mock
    def test_values_containing_query_operators_are_skipped(self, snow_client, config: Config):
        route = respx.get(SYS_USER).mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        # A comma or caret would terminate the encoded query and silently widen it.
        user = entra(employee_id="E1,E2", mail="a^b@example.com", user_principal_name=None)
        UserResolver(snow_client, config.servicenow).resolve_many([user])
        for call in route.calls:
            query = call.request.url.params["sysparm_query"]
            # A caret would inject an extra condition and widen the match.
            assert "a^b@example.com" not in query
        # A comma is harmless with OR-chained equality, so it is still looked up.
        assert any(
            "employee_number=E1,E2" in c.request.url.params["sysparm_query"] for c in route.calls
        )

    @respx.mock
    def test_active_only_is_applied_by_default(self, snow_client, config: Config):
        route = respx.get(SYS_USER).mock(
            return_value=httpx.Response(200, json={"result": [row()]})
        )
        UserResolver(snow_client, config.servicenow).resolve_many([entra()])
        # The AND clause trails the OR group so it scopes the whole group.
        assert route.calls[0].request.url.params["sysparm_query"].endswith("^active=true")

    @respx.mock
    def test_query_preserves_the_casing_entra_reported(self, snow_client, config: Config):
        route = respx.get(SYS_USER).mock(
            return_value=httpx.Response(200, json={"result": [row(employee_number="e4242")]})
        )
        user = entra(employee_id="E4242")
        ref = UserResolver(snow_client, config.servicenow).resolve_many([user])[user.object_id]
        # Sent as-is (Oracle-backed instances collate case-sensitively) but still
        # matched against a differently-cased sys_user row.
        assert "employee_number=E4242" in route.calls[0].request.url.params["sysparm_query"]
        assert ref is not None and ref.matched_on == "employee_number"

    @respx.mock
    def test_active_filter_can_be_disabled(self, set_env, snow_client):
        set_env(SNOW_USER_ACTIVE_ONLY="false")
        cfg = Config.from_env()
        route = respx.get(SYS_USER).mock(
            return_value=httpx.Response(200, json={"result": [row()]})
        )
        UserResolver(snow_client, cfg.servicenow).resolve_many([entra()])
        assert "active=true" not in route.calls[0].request.url.params["sysparm_query"]


class TestBatching:
    @respx.mock
    def test_chunks_large_lookups(self, snow_client, config: Config):
        users = [
            entra(object_id=f"oid-{i}", employee_id=f"E{i}", mail=None, user_principal_name=None)
            for i in range(120)
        ]
        route = respx.get(SYS_USER).mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        UserResolver(snow_client, config.servicenow).resolve_many(users)
        # 120 values / 50 per chunk = 3 requests for the employee_number key.
        assert route.call_count == 3

    @respx.mock
    def test_repeat_resolution_is_cached(self, snow_client, config: Config):
        route = respx.get(SYS_USER).mock(
            return_value=httpx.Response(200, json={"result": [row()]})
        )
        resolver = UserResolver(snow_client, config.servicenow)
        resolver.resolve_many([entra()])
        resolver.resolve_many([entra()])
        assert route.call_count == 1

    @respx.mock
    def test_shared_owner_resolves_once_for_many_devices(self, snow_client, config: Config):
        route = respx.get(SYS_USER).mock(
            return_value=httpx.Response(200, json={"result": [row()]})
        )
        resolver = UserResolver(snow_client, config.servicenow)
        resolved = resolver.resolve_many([entra(), entra(), entra()])
        assert route.call_count == 1
        assert len(resolved) == 1
