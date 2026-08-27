from __future__ import annotations

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.errors import AuthError
from intune_cmdb_sync.servicenow.auth import ServiceNowAuth

SNOW = "https://acme.service-now.com"
TOKEN = f"{SNOW}/oauth_token.do"


class TestClientCredentials:
    @respx.mock
    def test_sends_client_credentials_grant(self, config: Config):
        route = respx.post(TOKEN).mock(
            return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 1800})
        )
        assert ServiceNowAuth(config.servicenow).token() == "abc"
        body = route.calls[0].request.read().decode()
        assert "grant_type=client_credentials" in body
        assert "client_id=snow-client" in body
        assert "username" not in body

    @respx.mock
    def test_token_is_cached_between_calls(self, config: Config):
        route = respx.post(TOKEN).mock(
            return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 1800})
        )
        auth = ServiceNowAuth(config.servicenow)
        auth.token()
        auth.token()
        assert route.call_count == 1

    @respx.mock
    def test_short_lived_token_is_refetched(self, config: Config):
        route = respx.post(TOKEN).mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "one", "expires_in": 1}),
                httpx.Response(200, json={"access_token": "two", "expires_in": 1800}),
            ]
        )
        auth = ServiceNowAuth(config.servicenow)
        assert auth.token() == "one"
        auth._expires_at = 0.0  # simulate the clock advancing past expiry
        assert auth.token() == "two"
        assert route.call_count == 2

    @respx.mock
    def test_failure_hints_at_the_enabling_property(self, config: Config):
        respx.post(TOKEN).mock(
            return_value=httpx.Response(401, json={"error": "unauthorized_client"})
        )
        with pytest.raises(AuthError, match=r"grant_type\.enabled"):
            ServiceNowAuth(config.servicenow).token()

    @respx.mock
    def test_missing_access_token_is_an_error(self, config: Config):
        respx.post(TOKEN).mock(return_value=httpx.Response(200, json={"scope": "useless"}))
        with pytest.raises(AuthError, match="no access_token"):
            ServiceNowAuth(config.servicenow).token()


class TestPasswordGrant:
    @respx.mock
    def test_sends_username_and_password(self, set_env):
        set_env(
            SNOW_AUTH_MODE="oauth_password",
            SNOW_USERNAME="svc.intune",
            SNOW_PASSWORD="pw",
        )
        cfg = Config.from_env()
        route = respx.post(TOKEN).mock(
            return_value=httpx.Response(200, json={"access_token": "abc", "expires_in": 1800})
        )
        ServiceNowAuth(cfg.servicenow).token()
        body = route.calls[0].request.read().decode()
        assert "grant_type=password" in body
        assert "username=svc.intune" in body


class TestBasic:
    def test_returns_a_credential_pair_and_no_token(self, set_env):
        set_env(SNOW_AUTH_MODE="basic", SNOW_USERNAME="svc", SNOW_PASSWORD="pw")
        auth = ServiceNowAuth(Config.from_env().servicenow)
        assert auth.uses_bearer_token is False
        assert auth.basic_auth() == ("svc", "pw")
        with pytest.raises(AuthError, match="does not use bearer tokens"):
            auth.token()

    def test_oauth_mode_has_no_basic_pair(self, config: Config):
        auth = ServiceNowAuth(config.servicenow)
        assert auth.uses_bearer_token is True
        assert auth.basic_auth() is None
