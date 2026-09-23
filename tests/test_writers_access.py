from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.errors import ServiceNowError
from intune_cmdb_sync.servicenow.client import ServiceNowClient
from intune_cmdb_sync.servicenow.writers import PROBE_CLASS, verify_write_access

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


class TestDiscoverySourceCaveat:
    """A stock instance carries 200+ discovery sources. Listing the first
    twenty alphabetically answers nothing -- the live 2026-09-04 check returned
    'ACC-Visibility' nine times over and never mentioned anything like Intune --
    and a bounded read cannot prove absence either. So this asks for values
    resembling the configured one instead."""

    def _caveats(self, set_env, rows: list[dict], *, source: str = "Intune") -> list[str]:
        """Returns caveats; raises ServiceNowError when the value is absent."""
        set_env(SNOW_WRITE_MODE="cmdb_instance", SNOW_DISCOVERY_SOURCE=source)
        cfg = Config.from_env()
        client = ServiceNowClient(cfg.servicenow)
        client.auth._token = "snow-token"
        client.auth._expires_at = float("inf")
        respx.post(url__startswith=f"{SNOW}/api/now/cmdb/instance/").mock(
            return_value=httpx.Response(400, json={"error": {"message": "Invalid class"}})
        )
        self.route = respx.get(f"{SNOW}/api/now/table/sys_choice").mock(
            return_value=httpx.Response(200, json={"result": rows})
        )
        return verify_write_access(client, cfg.servicenow).caveats

    @respx.mock
    def test_asks_only_for_values_resembling_the_configured_one(self, set_env):
        self._caveats(set_env, [{"value": "Intune"}])
        query = self.route.calls[0].request.url.params["sysparm_query"]
        assert "valueLIKEIntune" in query

    @respx.mock
    def test_a_registered_value_produces_no_caveat(self, set_env):
        caveats = self._caveats(set_env, [{"value": "Intune"}, {"value": "Intune Connector"}])
        assert not any("discovery_source" in c for c in caveats)

    @respx.mock
    def test_duplicate_language_rows_are_collapsed(self, set_env):
        """sys_choice holds one row per language, so the live check printed
        'ACC-Visibility' seven times and 'Altiris' nine."""
        with pytest.raises(ServiceNowError) as exc:
            self._caveats(set_env, [{"value": "Microsoft Intune"}] * 9)
        assert str(exc.value).count("Microsoft Intune") == 1

    @respx.mock
    def test_an_inactive_choice_is_not_offered(self, set_env):
        """Present but unusable is not the same as registered."""
        with pytest.raises(ServiceNowError, match="not a choice value"):
            self._caveats(set_env, [{"value": "Intune", "inactive": "true"}])

    @respx.mock
    def test_a_case_only_difference_is_called_out_on_its_own(self, set_env):
        """The field matches exactly, and this is the one case where the fix is
        a config edit rather than a ServiceNow admin ticket."""
        with pytest.raises(ServiceNowError) as exc:
            self._caveats(set_env, [{"value": "intune"}])
        assert "differs only by case" in str(exc.value)
        assert "'intune' IS registered" in str(exc.value)

    @respx.mock
    def test_a_different_spelling_is_suggested(self, set_env):
        with pytest.raises(ServiceNowError) as exc:
            self._caveats(set_env, [{"value": "Microsoft Intune"}])
        assert "'Microsoft Intune'" in str(exc.value)
        assert "resemble it" in str(exc.value)

    @respx.mock
    def test_nothing_resembling_it_says_so_and_names_the_action(self, set_env):
        with pytest.raises(ServiceNowError) as exc:
            self._caveats(set_env, [])
        assert "No registered value contains" in str(exc.value)
        assert "ServiceNow admin action" in str(exc.value)

    @respx.mock
    def test_an_unreadable_choice_list_stays_a_caveat(self, set_env):
        """Not knowing is different from knowing it is wrong, and only the
        second should fail the check."""
        set_env(SNOW_WRITE_MODE="cmdb_instance")
        cfg = Config.from_env()
        client = ServiceNowClient(cfg.servicenow)
        client.auth._token = "snow-token"
        client.auth._expires_at = float("inf")
        respx.post(url__startswith=f"{SNOW}/api/now/cmdb/instance/").mock(
            return_value=httpx.Response(400, json={"error": {"message": "Invalid class"}})
        )
        respx.get(f"{SNOW}/api/now/table/sys_choice").mock(
            return_value=httpx.Response(403, text="no read on sys_choice")
        )
        check = verify_write_access(client, cfg.servicenow)
        assert check.verified
        assert any("could not read sys_choice" in c for c in check.caveats)


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
    def test_an_unregistered_discovery_source_fails_the_check(self, set_env):
        """The endpoint is still callable, but every write will be rejected.
        Reporting that as a pass with a note attached -- which is what the live
        2026-09-04 check did -- defeats the point of checking."""
        client, cfg = self._client(set_env)
        self._probe_route(400, json={"error": {"message": "Invalid class"}})
        self._source_registered(rows=())

        with pytest.raises(ServiceNowError, match=re.escape("cmdb_ci.discovery_source")):
            verify_write_access(client, cfg.servicenow)

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
