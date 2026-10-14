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

import base64
import binascii
import json
import logging
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from .config import GraphConfig
from .errors import AuthError, GraphError, RetryableHTTPError
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
# processorArchitecture exists only on the beta endpoint, which is why the
# hardware-detail lookup goes to beta (see GraphClient.get_hardware_details).
NON_DEFAULT_DEVICE_FIELDS = (
    "id",
    "ethernetMacAddress",
    "physicalMemoryInBytes",
    "processorArchitecture",
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


def inspect_access_token(token: str) -> dict[str, Any]:
    """Read the claims out of a Graph bearer token without verifying it.

    We are not authenticating anything here — Entra already did. The point is to
    turn the two mistakes that actually happen with a hand-pasted token into an
    error that names the problem, instead of a 401 an hour later that does not.

    Returns {} for anything that is not a decodable JWT, which is not fatal:
    the token may still be perfectly good.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


class StaticTokenProvider:
    """Serves a pre-obtained Graph token. Local development only.

    There is no refresh: when the token expires, the run fails. That is the
    accepted trade for being able to test against a tenant where you cannot
    create an app registration.
    """

    #: Either of these permits reading managedDevices. Held as app roles in an
    #: application token (`roles`) or as scopes in a delegated one (`scp`).
    DEVICE_READ_PERMISSIONS = (
        "DeviceManagementManagedDevices.Read.All",
        "DeviceManagementManagedDevices.ReadWrite.All",
    )

    #: Audiences Entra issues for Microsoft Graph.
    GRAPH_AUDIENCES = frozenset({
        "https://graph.microsoft.com",
        "https://graph.microsoft.com/",
        "00000003-0000-0000-c000-000000000000",
    })

    def __init__(self, token: str) -> None:
        self._token = token.strip()
        if not self._token:
            raise AuthError("GRAPH_ACCESS_TOKEN is empty")
        # Some tools copy the header prefix along with the value.
        if self._token.lower().startswith("bearer "):
            self._token = self._token[7:].strip()
        self._validate()

    def _validate(self) -> None:
        claims = inspect_access_token(self._token)
        if not claims:
            log.warning(
                "GRAPH_ACCESS_TOKEN is not a readable JWT; using it as-is. "
                "If Graph rejects it, check that it is a Microsoft Graph token."
            )
            return

        audience = str(claims.get("aud") or "")
        if audience and audience not in self.GRAPH_AUDIENCES:
            raise AuthError(
                f"GRAPH_ACCESS_TOKEN is for audience {audience!r}, not Microsoft Graph. "
                "A token for one resource is never valid for another. Get a Graph token "
                "with: az account get-access-token --resource https://graph.microsoft.com"
            )

        expires_at = claims.get("exp")
        if isinstance(expires_at, int | float):
            remaining = expires_at - time.time()
            if remaining <= 0:
                raise AuthError(
                    "GRAPH_ACCESS_TOKEN expired "
                    f"{int(-remaining / 60)} minute(s) ago. Tokens are short-lived; "
                    "fetch a fresh one."
                )
            log.warning(
                "using a static Graph access token: LOCAL DEVELOPMENT ONLY. "
                "It cannot be refreshed, so a run longer than its remaining life "
                "will fail partway through.",
                extra={
                    "expires_in_minutes": int(remaining / 60),
                    # Application permissions land in `roles`; a delegated token
                    # carries `scp` instead and is bounded by the signed-in user.
                    "app_roles": claims.get("roles"),
                    "delegated_scopes": claims.get("scp"),
                },
            )

        self._warn_if_missing_device_permission(claims)

    def _warn_if_missing_device_permission(self, claims: dict[str, Any]) -> None:
        """Say why the run is about to 403, before it does.

        Not fatal: the claim set is not always the whole story, and refusing a
        token that would have worked is worse than a warning that is occasionally
        redundant. But the common case -- a token from `az account
        get-access-token`, which carries no Intune permission at all -- otherwise
        surfaces as a 403 whose text does not mention where the token came from.
        """
        granted = set(claims.get("roles") or [])
        granted.update(str(claims.get("scp") or "").split())
        if not granted or granted.intersection(self.DEVICE_READ_PERMISSIONS):
            return
        log.warning(
            "this token carries no Intune device-read permission, so listing "
            "managed devices will fail with 403. It needs one of %s. A token from "
            "'az account get-access-token' will not have it: that is the Azure CLI's "
            "own app registration, which has no Intune permissions. Use Graph "
            "Explorer, or an app registration that has been consented for it.",
            " or ".join(self.DEVICE_READ_PERMISSIONS),
            extra={"granted": sorted(granted)},
        )

    def __call__(self) -> str:
        return self._token


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
        if token_provider is None:
            if cfg.auth_mode == "access_token":
                token_provider = StaticTokenProvider(cfg.access_token or "")
            else:
                token_provider = _CredentialTokenProvider(build_credential(cfg), cfg.scope)
        provider = token_provider
        # Same host and version layout as base_url, so a national cloud keeps
        # working: https://graph.microsoft.us/v1.0 -> https://graph.microsoft.us/beta.
        self._beta_base = cfg.base_url.rsplit("/", 1)[0] + "/beta"
        self._beta_batch_url = f"{self._beta_base}/$batch"
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

    def get_hardware_details(self, device_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Fetch the non-default hardware properties, 20 devices per `$batch`.

        Graph returns these only from a per-device GET, so batching is the only
        way to cut the round trips: N devices cost N/20 requests instead of N.
        Each sub-request still counts against Intune's throttling limits.

        Goes to the beta endpoint: `processorArchitecture` does not exist in
        v1.0. Beta returns the same RAM / ethernet MAC values (checked against a
        39-device tenant on 2026-10-14). Macs report RAM as 0 on both.

        Best-effort: a device whose lookup fails is absent from the result and
        syncs without these fields, rather than failing the run.
        """
        select = ",".join(NON_DEFAULT_DEVICE_FIELDS)
        return self._batch_get(
            {
                did: f"/deviceManagement/managedDevices/{did}?$select={select}"
                for did in device_ids
                if did
            },
            what="hardware detail",
            required=False,
            batch_url=self._beta_batch_url,
        )

    def get_custom_attribute_values(self, attribute: str) -> dict[str, str]:
        """Latest successful output of a macOS custom attribute, by device id.

        `attribute` matches the custom attribute's name, its profile's display
        name, or its id. Intune runs the script on each Mac every 8 hours and
        keeps the output as `resultMessage`. Beta only; needs
        DeviceManagementScripts.Read.All. Raises GraphError when the attribute
        cannot be found or read, and the caller decides what that costs.
        """
        scripts_url = f"{self._beta_base}/deviceManagement/deviceCustomAttributeShellScripts"
        wanted = attribute.strip().lower()
        matches = [
            script
            for script in self._iter_pages(
                scripts_url,
                {"$select": "id,customAttributeName,displayName"},
                what="listing macOS custom attributes",
            )
            if wanted in {
                str(script.get(key) or "").strip().lower()
                for key in ("id", "customAttributeName", "displayName")
            }
        ]
        if not matches:
            raise GraphError(f"no macOS custom attribute named {attribute!r}")
        if len(matches) > 1:
            raise GraphError(
                f"{len(matches)} macOS custom attributes match {attribute!r}; use its id"
            )

        states_url = f"{scripts_url}/{matches[0]['id']}/deviceRunStates"
        what = f"reading custom attribute {attribute!r}"
        try:
            # The nested $select keeps each state small. It is standard OData
            # but has not been seen against this endpoint, so a 400 falls back
            # to the documented plain expand rather than losing Mac RAM.
            states = list(
                self._iter_pages(states_url, {"$expand": "managedDevice($select=id)"}, what=what)
            )
        except _GraphBadRequest:
            states = list(self._iter_pages(states_url, {"$expand": "managedDevice"}, what=what))

        values: dict[str, str] = {}
        not_successful = 0
        for state in states:
            if str(state.get("runState") or "").lower() != "success":
                # A failed run can still carry output (an Integer attribute whose
                # value overflowed, for one), but Intune did not accept it.
                not_successful += 1
                continue
            device_id = (state.get("managedDevice") or {}).get("id")
            output = str(state.get("resultMessage") or "").strip()
            if device_id and output:
                values[str(device_id)] = output
        log.info(
            "read custom attribute run states",
            extra={"attribute": attribute, "states": len(states),
                   "not_successful": not_successful},
        )
        return values

    def _iter_pages(
        self, url: str, params: dict[str, Any], *, what: str
    ) -> Iterator[dict[str, Any]]:
        next_url: str | None = url
        next_params: dict[str, Any] | None = params
        while next_url:
            response = self._http.request("GET", next_url, params=next_params)
            if response.status_code == 400:
                raise _GraphBadRequest(f"{what} failed: {describe_error(response)}")
            if not response.is_success:
                raise GraphError(f"{what} failed: {describe_error(response)}")
            payload = response.json()
            yield from payload.get("value") or []
            # nextLink already carries every query parameter.
            next_url, next_params = payload.get("@odata.nextLink"), None

    # ---- Entra ID users --------------------------------------------------

    def get_users(self, object_ids: Iterable[str]) -> dict[str, EntraUser]:
        """Resolve Entra user object IDs to user records using JSON batching.

        Missing or deleted users are simply absent from the result; the caller
        treats that as "no owner" rather than an error.
        """
        select = ",".join(self.cfg.user_select_fields)
        bodies = self._batch_get(
            {oid: f"/users/{oid}?$select={select}" for oid in object_ids if oid},
            what="user",
            required=True,
        )
        return {oid: EntraUser.from_graph(body) for oid, body in bodies.items()}

    # ---- JSON batching ---------------------------------------------------

    def _batch_get(
        self, urls: dict[str, str], *, what: str, required: bool, batch_url: str = "/$batch"
    ) -> dict[str, dict[str, Any]]:
        """GET every URL through `/$batch`, at most 20 per request.

        Returns the 2xx bodies keyed like `urls`. A 404 is simply absent;
        throttled sub-requests (429/503/504) are retried with backoff, since the
        batch itself succeeds while its parts are refused. When a whole batch
        request fails, `required` decides between raising and skipping it.
        """
        resolved: dict[str, dict[str, Any]] = {}
        pending = list(urls)
        if not pending:
            return resolved

        for attempt in range(1, self.cfg.max_retries + 2):
            throttled: list[str] = []

            for chunk in _chunks(pending, GRAPH_BATCH_LIMIT):
                requests = [
                    {"id": str(i), "method": "GET", "url": urls[key]}
                    for i, key in enumerate(chunk)
                ]
                try:
                    response = self._http.request(
                        "POST", batch_url, json_body={"requests": requests}
                    )
                except RetryableHTTPError as exc:
                    # Retries exhausted on the batch itself (throttling, 5xx,
                    # network). Optional lookups must not take the run down.
                    if required:
                        raise
                    log.warning(
                        "graph $batch failed; continuing without it",
                        extra={"what": what, "size": len(chunk), "detail": str(exc)},
                    )
                    continue
                if not response.is_success:
                    if required:
                        raise GraphError(f"{what} $batch failed: {describe_error(response)}")
                    log.warning(
                        "graph $batch failed; continuing without it",
                        extra={"what": what, "size": len(chunk),
                               "detail": describe_error(response)},
                    )
                    continue

                for sub in response.json().get("responses", []):
                    index = int(sub.get("id", -1))
                    if not 0 <= index < len(chunk):
                        continue
                    graph_id = chunk[index]
                    status = int(sub.get("status", 0))
                    body = sub.get("body") or {}

                    if 200 <= status < 300:
                        resolved[graph_id] = dict(body)
                    elif status == 404:
                        log.debug(
                            "graph object not found", extra={"what": what, "graph_id": graph_id}
                        )
                    elif status in (429, 503, 504):
                        throttled.append(graph_id)
                    else:
                        log.warning(
                            "graph lookup failed",
                            extra={"what": what, "graph_id": graph_id, "status": status,
                                   "error": str(body.get("error", body))[:300]},
                        )

            if not throttled:
                break

            pending = throttled
            if attempt > self.cfg.max_retries:
                log.warning(
                    "giving up on throttled graph lookups",
                    extra={"what": what, "unresolved": len(pending)},
                )
                break

            delay = _batch_backoff(attempt)
            log.warning(
                "graph sub-requests throttled, retrying",
                extra={"what": what, "pending": len(pending), "attempt": attempt,
                       "delay_s": delay},
            )
            time.sleep(delay)

        return resolved


class _GraphBadRequest(GraphError):
    """A 400 from a paged read, which a caller may answer with a simpler query."""


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
    "StaticTokenProvider",
    "build_credential",
    "inspect_access_token",
    "is_corporate",
]
