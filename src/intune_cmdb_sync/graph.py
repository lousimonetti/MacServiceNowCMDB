"""Microsoft Graph client for Intune managed devices and Entra ID users.

Token acquisition uses `azure-identity` (the first-party Microsoft auth SDK) so
that client secrets, workload-identity federation, and managed identity all work
through the same code path with SDK-managed caching and refresh. The data calls
are plain REST against the documented Graph endpoints, which keeps the container
image small and avoids the Kiota dependency tree.

Endpoints used:
  GET  /deviceManagement/managedDevices        (list, paged via @odata.nextLink)
  GET  /deviceManagement/managedDevices/{id}   (optional non-default properties)
  POST /$batch                                 (bulk Entra user lookup)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from .config import GraphConfig
from .errors import AuthError, GraphError
from .http import RetryingClient, describe_error
from .models import EntraUser

log = logging.getLogger(__name__)

# Requested on the list call. Keeping this explicit keeps page payloads small
# and makes it obvious which Graph properties the mapping layer may rely on.
DEVICE_SELECT_FIELDS = (
    "id",
    "deviceName",
    "managedDeviceName",
    "managedDeviceOwnerType",
    "operatingSystem",
    "osVersion",
    "manufacturer",
    "model",
    "serialNumber",
    "imei",
    "meid",
    "wiFiMacAddress",
    "complianceState",
    "managementAgent",
    "managementState",
    "deviceEnrollmentType",
    "deviceRegistrationState",
    "enrolledDateTime",
    "lastSyncDateTime",
    "isEncrypted",
    "isSupervised",
    "jailBroken",
    "azureADDeviceId",
    "azureADRegistered",
    "deviceCategoryDisplayName",
    "enrollmentProfileName",
    "totalStorageSpaceInBytes",
    "freeStorageSpaceInBytes",
    "userId",
    "userPrincipalName",
    "userDisplayName",
    "emailAddress",
)

# Graph documents these as "non-default" properties: they come back null from the
# collection endpoint and require a per-device GET with $select to populate.
NON_DEFAULT_DEVICE_FIELDS = (
    "id",
    "ethernetMacAddress",
    "physicalMemoryInBytes",
    "udid",
)

# Microsoft Graph caps JSON batch requests at 20 sub-requests.
GRAPH_BATCH_LIMIT = 20

# Audience a managed identity requests when it is acting as a federated
# credential for an app registration. Fixed by Entra, not configurable.
TOKEN_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"


def build_credential(cfg: GraphConfig) -> Any:
    """Create an azure-identity credential for the configured auth mode."""
    try:
        from azure.identity import (
            ClientAssertionCredential,
            ClientSecretCredential,
            DefaultAzureCredential,
            ManagedIdentityCredential,
            WorkloadIdentityCredential,
        )
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise AuthError("azure-identity is not installed") from exc

    mode = cfg.auth_mode
    if mode == "client_secret":
        return ClientSecretCredential(
            tenant_id=cfg.tenant_id,
            client_id=cfg.client_id or "",
            client_secret=cfg.client_secret or "",
            authority=cfg.authority_host,
        )
    if mode == "managed_identity":
        # client_id targets a specific user-assigned identity; omitting it uses
        # the system-assigned identity.
        return ManagedIdentityCredential(client_id=cfg.client_id) if cfg.client_id \
            else ManagedIdentityCredential()
    if mode == "federated_managed_identity":
        # Secretless cross-tenant auth. A managed identity in *this* tenant is
        # registered as a federated credential on a multi-tenant app that has
        # been consented into the Intune tenant. The identity gets a token for
        # the token-exchange audience and presents it as a client assertion, so
        # no secret exists anywhere.
        #
        # This is the piece that a bare managed identity cannot do: an app role
        # in another directory cannot be granted to a single-tenant identity.
        assertion_identity = (
            ManagedIdentityCredential(client_id=cfg.assertion_identity_client_id)
            if cfg.assertion_identity_client_id
            else ManagedIdentityCredential()
        )

        def _assertion() -> str:
            token = assertion_identity.get_token(TOKEN_EXCHANGE_SCOPE)
            return str(token.token)

        return ClientAssertionCredential(
            tenant_id=cfg.tenant_id,
            client_id=cfg.client_id or "",
            func=_assertion,
            authority=cfg.authority_host,
        )
    if mode == "workload_identity":
        # Kubernetes/AKS and GitHub Actions OIDC: the platform projects a
        # federated token into the filesystem. Azure Container Apps does not,
        # which is why deploy/azure does not offer this mode.
        return WorkloadIdentityCredential(
            tenant_id=cfg.tenant_id,
            client_id=cfg.client_id,
        )
    return DefaultAzureCredential()


class _CredentialTokenProvider:
    """Adapts an azure-identity credential to the `() -> str` shape the HTTP layer wants."""

    def __init__(self, credential: Any, scope: str):
        self._credential = credential
        self._scope = scope

    def __call__(self) -> str:
        try:
            token = self._credential.get_token(self._scope)
        except Exception as exc:  # azure-identity raises a wide range of types
            raise AuthError(f"could not acquire a Microsoft Graph token: {exc}") from exc
        return str(token.token)


class GraphClient:
    def __init__(self, cfg: GraphConfig, *, token_provider: Any = None) -> None:
        self.cfg = cfg
        provider = token_provider or _CredentialTokenProvider(
            build_credential(cfg), cfg.scope
        )
        self._http = RetryingClient(
            base_url=cfg.base_url,
            token_provider=provider,
            timeout=cfg.request_timeout,
            max_retries=cfg.max_retries,
            default_headers={"ConsistencyLevel": "eventual"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GraphClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- managed devices -------------------------------------------------

    def iter_managed_devices(self) -> Iterator[dict[str, Any]]:
        """Yield managedDevice objects, following `@odata.nextLink` to the end.

        Applies the ownership filter server-side when possible. Graph does not
        formally document `managedDeviceOwnerType` as filterable, so a 400 from
        the filtered request falls back to an unfiltered fetch; the caller filters
        client-side either way, which makes the result correct in both cases.
        """
        params: dict[str, Any] = {
            "$select": ",".join(DEVICE_SELECT_FIELDS),
            "$top": self.cfg.page_size,
        }
        server_filter = self._ownership_filter()
        if server_filter:
            params["$filter"] = server_filter

        url: str | None = "/deviceManagement/managedDevices"
        first_request = True

        while url:
            response = self._http.request("GET", url, params=params if first_request else None)

            if (
                first_request
                and response.status_code == 400
                and server_filter
            ):
                log.warning(
                    "server-side ownership filter rejected by Graph; "
                    "falling back to an unfiltered fetch with client-side filtering",
                    extra={"filter": server_filter, "detail": describe_error(response)},
                )
                params.pop("$filter", None)
                server_filter = None
                response = self._http.request("GET", url, params=params)

            if not response.is_success:
                raise GraphError(f"listing managed devices failed: {describe_error(response)}")

            payload = response.json()
            devices = payload.get("value") or []
            yield from devices

            next_link = payload.get("@odata.nextLink")
            # nextLink is an absolute URL that already carries every query
            # parameter, so params must not be re-sent.
            url = next_link
            first_request = False
            params = {}

    def _ownership_filter(self) -> str | None:
        if not self.cfg.server_side_filter or self.cfg.ownership == "any":
            return None
        return f"managedDeviceOwnerType eq '{self.cfg.ownership}'"

    def fetch_device_hardware_detail(self, device_id: str) -> dict[str, Any]:
        """Fetch the non-default hardware properties for one device.

        Costs one Graph call per device; only worth enabling when the CMDB
        needs RAM / ethernet MAC / UDID.
        """
        response = self._http.request(
            "GET",
            f"/deviceManagement/managedDevices/{device_id}",
            params={"$select": ",".join(NON_DEFAULT_DEVICE_FIELDS)},
        )
        if not response.is_success:
            log.warning(
                "hardware detail fetch failed; continuing without it",
                extra={"device_id": device_id, "detail": describe_error(response)},
            )
            return {}
        return dict(response.json())

    # ---- Entra ID users --------------------------------------------------

    def get_users(self, object_ids: Iterable[str]) -> dict[str, EntraUser]:
        """Resolve Entra user object IDs to user records using JSON batching.

        Missing or deleted users are simply absent from the result; the caller
        treats that as "no owner" rather than an error.
        """
        unique = [oid for oid in dict.fromkeys(object_ids) if oid]
        resolved: dict[str, EntraUser] = {}
        if not unique:
            return resolved

        select = ",".join(self.cfg.user_select_fields)
        pending: list[str] = unique

        for attempt in range(1, self.cfg.max_retries + 2):
            throttled: list[str] = []

            for chunk in _chunks(pending, GRAPH_BATCH_LIMIT):
                requests = [
                    {"id": str(i), "method": "GET", "url": f"/users/{oid}?$select={select}"}
                    for i, oid in enumerate(chunk)
                ]
                response = self._http.request("POST", "/$batch", json_body={"requests": requests})
                if not response.is_success:
                    raise GraphError(f"user $batch failed: {describe_error(response)}")

                for sub in response.json().get("responses", []):
                    index = int(sub.get("id", -1))
                    if not 0 <= index < len(chunk):
                        continue
                    object_id = chunk[index]
                    status = int(sub.get("status", 0))
                    body = sub.get("body") or {}

                    if 200 <= status < 300:
                        resolved[object_id] = EntraUser.from_graph(body)
                    elif status == 404:
                        log.debug("entra user not found", extra={"object_id": object_id})
                    elif status in (429, 503, 504):
                        throttled.append(object_id)
                    else:
                        log.warning(
                            "entra user lookup failed",
                            extra={"object_id": object_id, "status": status,
                                   "error": str(body.get("error", body))[:300]},
                        )

            if not throttled:
                break

            pending = throttled
            if attempt > self.cfg.max_retries:
                log.warning(
                    "giving up on throttled Entra user lookups",
                    extra={"unresolved": len(pending)},
                )
                break

            delay = _batch_backoff(attempt)
            log.warning(
                "Entra user sub-requests throttled, retrying",
                extra={"pending": len(pending), "attempt": attempt, "delay_s": delay},
            )
            time.sleep(delay)

        return resolved


def _batch_backoff(attempt: int) -> float:
    return min(2.0 ** attempt, 60.0)


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def is_corporate(device: dict[str, Any], ownership: str) -> bool:
    """Client-side ownership check.

    This runs regardless of whether the server-side `$filter` was applied, so a
    silently-ignored filter can never leak personally-owned devices into the CMDB.
    """
    if ownership == "any":
        return True
    return str(device.get("managedDeviceOwnerType") or "").lower() == ownership


__all__ = [
    "DEVICE_SELECT_FIELDS",
    "GRAPH_BATCH_LIMIT",
    "NON_DEFAULT_DEVICE_FIELDS",
    "TOKEN_EXCHANGE_SCOPE",
    "GraphClient",
    "build_credential",
    "is_corporate",
]
