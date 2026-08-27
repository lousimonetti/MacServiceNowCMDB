"""ServiceNow authentication.

Three modes, in descending order of preference:

`oauth_client_credentials`
    Washington DC and later. A pure machine-to-machine grant: the OAuth
    application registry entry carries an "OAuth Application User", so no human
    account's password is stored anywhere. Requires the system property
    `glide.oauth.inbound.client.credential.grant_type.enabled = true`.

`oauth_password`
    The classic resource-owner-password grant, for instances older than
    Washington DC or where the client-credentials property cannot be enabled.

`basic`
    HTTP Basic. Simplest to stand up, and the least desirable: the integration
    account's password travels on every request.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from ..config import ServiceNowConfig
from ..errors import AuthError

log = logging.getLogger(__name__)

TOKEN_PATH = "/oauth_token.do"

# Refresh this many seconds before the token actually expires, so a long-running
# batch never hands a just-expired token to ServiceNow.
EXPIRY_SKEW_SECONDS = 60.0


class ServiceNowAuth:
    """Acquires and caches an access token, or supplies Basic credentials."""

    def __init__(self, cfg: ServiceNowConfig, *, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._transport = transport

    @property
    def uses_bearer_token(self) -> bool:
        return self.cfg.auth_mode in {"oauth_client_credentials", "oauth_password"}

    def basic_auth(self) -> tuple[str, str] | None:
        if self.cfg.auth_mode != "basic":
            return None
        return (self.cfg.username or "", self.cfg.password or "")

    def token(self) -> str:
        """Return a valid bearer token, fetching or refreshing as needed."""
        if not self.uses_bearer_token:
            raise AuthError(f"auth mode {self.cfg.auth_mode!r} does not use bearer tokens")

        with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            self._token, self._expires_at = self._fetch_token()
            return self._token

    def _fetch_token(self) -> tuple[str, float]:
        data: dict[str, str] = {
            "client_id": self.cfg.client_id or "",
            "client_secret": self.cfg.client_secret or "",
        }
        if self.cfg.auth_mode == "oauth_client_credentials":
            data["grant_type"] = "client_credentials"
        else:
            data["grant_type"] = "password"
            data["username"] = self.cfg.username or ""
            data["password"] = self.cfg.password or ""

        with httpx.Client(
            base_url=self.cfg.base_url,
            timeout=self.cfg.request_timeout,
            transport=self._transport,
        ) as client:
            try:
                response = client.post(
                    TOKEN_PATH,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                raise AuthError(f"ServiceNow token request failed: {exc}") from exc

        if not response.is_success:
            raise AuthError(_describe_token_failure(response, self.cfg.auth_mode))

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise AuthError("ServiceNow token endpoint returned a non-JSON body") from exc

        token = payload.get("access_token")
        if not token:
            raise AuthError(f"ServiceNow token response contained no access_token: {payload}")

        expires_in = float(payload.get("expires_in") or 1800)
        expires_at = time.monotonic() + max(expires_in - EXPIRY_SKEW_SECONDS, 30.0)
        log.info(
            "acquired ServiceNow access token",
            extra={"grant_type": data["grant_type"], "expires_in": expires_in},
        )
        return str(token), expires_at


def _describe_token_failure(response: httpx.Response, auth_mode: str) -> str:
    body = " ".join(response.text.split())[:300]
    hint = ""
    if auth_mode == "oauth_client_credentials" and response.status_code in (400, 401):
        hint = (
            " Check that the system property "
            "'glide.oauth.inbound.client.credential.grant_type.enabled' is true and that the "
            "Application Registry entry has an OAuth Application User set."
        )
    return f"ServiceNow token request returned HTTP {response.status_code}: {body}.{hint}"
