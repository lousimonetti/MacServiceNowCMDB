"""The two CMDB write paths.

`identify_reconcile` (default, recommended)
    `POST /api/now/identifyreconcile` — the base-platform Identification and
    Reconciliation API. It accepts a bulk `items` array in a single request and
    carries `sys_object_source_info`, which lets IRE key each CI on the Intune
    `managedDevice.id` (`source_native_key`). That is what makes the sync stable
    across motherboard swaps, serial-number corrections, and device renames.
    Requires the `itil` or `asset` role. No plugin purchase, no Service Graph
    Connector subscription.

`cmdb_instance`
    `POST /api/now/cmdb/instance/{className}` — one HTTP call per CI. Still runs
    through IRE, but the documented request body has no slot for
    `sys_object_source_info`, so identification falls back to the class's
    identifier rules (serial number, then name). Use it only where the
    identifyreconcile endpoint is blocked.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import ServiceNowConfig
from ..errors import ServiceNowError
from ..http import describe_error
from .client import CMDB_INSTANCE_API, ServiceNowClient

log = logging.getLogger(__name__)

IDENTIFY_RECONCILE_API = "/api/now/identifyreconcile"
IDENTIFY_RECONCILE_ENHANCED_API = "/api/now/identifyreconcile/enhanced"
IDENTIFY_RECONCILE_QUERY_API = "/api/now/identifyreconcile/query"

# Source key for the write-access probe. Deliberately unlike any Intune device
# GUID, so that even if a future change sent it to a committing endpoint it
# could not collide with a real CI.
PROBE_SOURCE_KEY = "intune-cmdb-sync:write-access-probe"

# ServiceNow refuses an OAuth client that is not authorised for a global-scope
# API *before* it looks at roles or ACLs, and the response says only "User Not
# Authorized". Left unexplained that reads as a missing `itil` role, which is
# the one thing that cannot fix it.
_UNSCOPED_API_MARKER = "unscoped api"
_UNSCOPED_API_HINT = (
    " This is the OAuth client being refused the API at the gate, not a missing role: "
    "the Application Registry entry is Securely Scoped and has no REST API Auth Scope "
    "linked for POST on this API. Adding 'itil' will not change it, and "
    "SNOW_WRITE_MODE=cmdb_instance is behind the same gate. "
    "See docs/servicenow-setup.md."
)

# IRE `operation` values mapped onto the connector's outcome vocabulary.
_OPERATION_TO_ACTION = {
    "INSERT": "inserted",
    "UPDATE": "updated",
    "UPDATE_WITH_UPGRADE": "updated",
    "UPDATE_WITH_DOWNGRADE": "updated",
    "UPDATE_WITH_SWITCH": "updated",
    "NO_CHANGE": "unchanged",
    "DELETE": "updated",
}


@dataclass
class CiPayload:
    """One device, already mapped to CMDB shape and ready to write."""

    intune_id: str
    class_name: str
    values: dict[str, Any]
    device_name: str
    serial_number: str | None
    source_recency: str | None = None


@dataclass
class WriteResult:
    intune_id: str
    action: str
    sys_id: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def message(self) -> str | None:
        return "; ".join(self.errors) if self.errors else None


class Writer(Protocol):
    mode: str

    def write(self, batch: list[CiPayload]) -> list[WriteResult]: ...


class IdentifyReconcileWriter:
    """Bulk writer built on `POST /api/now/identifyreconcile`."""

    mode = "identify_reconcile"

    def __init__(self, client: ServiceNowClient, cfg: ServiceNowConfig, *, dry_run: bool = False):
        self._client = client
        self._cfg = cfg
        self._dry_run = dry_run

    def _endpoint(self) -> str:
        if self._dry_run:
            # The query endpoint runs identification and reports what *would*
            # happen without committing anything to the database.
            return IDENTIFY_RECONCILE_QUERY_API
        if self._cfg.use_enhanced_ire:
            return IDENTIFY_RECONCILE_ENHANCED_API
        return IDENTIFY_RECONCILE_API

    def build_payload(self, batch: list[CiPayload]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for item in batch:
            source_info: dict[str, Any] = {
                "source_native_key": item.intune_id,
                "source_name": self._cfg.discovery_source,
            }
            if self._cfg.source_feed:
                source_info["source_feed"] = self._cfg.source_feed
            if item.source_recency:
                source_info["source_recency_timestamp"] = item.source_recency

            items.append(
                {
                    "className": item.class_name,
                    "internal_id": item.intune_id,
                    "values": item.values,
                    "sys_object_source_info": source_info,
                }
            )
        return {"items": items, "relations": []}

    def write(self, batch: list[CiPayload]) -> list[WriteResult]:
        if not batch:
            return []

        params: dict[str, Any] = {"sysparm_data_source": self._cfg.discovery_source}
        if self._cfg.use_enhanced_ire and not self._dry_run and self._cfg.enhanced_ire_options:
            params["options"] = self._cfg.enhanced_ire_options

        response = self._client.request(
            "POST", self._endpoint(), params=params, json_body=self.build_payload(batch)
        )
        if not response.is_success:
            # IRE returns its log context id even on failure, and it is the only
            # handle that ties this request to what ServiceNow recorded on its
            # own side. Without it, a support case starts from a timestamp.
            detail = describe_error(response)
            raise ServiceNowError(
                f"identifyreconcile ({len(batch)} items) failed: {detail}"
                f"{_unscoped_api_suffix(detail)}"
                f"{_log_context_suffix(_log_context_id(response))}"
            )

        result = (response.json() or {}).get("result") or {}
        log_context_id = result.get("logContextId")
        log.info(
            "identifyreconcile accepted",
            extra={"items": len(batch), "log_context_id": log_context_id},
        )
        return self._parse_results(batch, result)

    def _parse_results(
        self, batch: list[CiPayload], result: dict[str, Any]
    ) -> list[WriteResult]:
        items = result.get("items") or []
        log_context_id = result.get("logContextId")
        if len(items) != len(batch):
            log.warning(
                "identifyreconcile returned a different item count than submitted; "
                "correlating by position for the overlap only",
                extra={"submitted": len(batch), "returned": len(items),
                       "log_context_id": log_context_id},
            )

        results: list[WriteResult] = []
        for index, payload in enumerate(batch):
            if index >= len(items):
                results.append(
                    WriteResult(
                        intune_id=payload.intune_id,
                        action="error",
                        errors=[
                            "no IRE result returned for this item"
                            + _log_context_suffix(log_context_id)
                        ],
                    )
                )
                continue

            item = items[index] or {}
            errors = _collect_item_errors(item)
            if errors:
                # Carry the trace id on the per-device message: this is what an
                # operator pastes into a ServiceNow case for a single bad CI.
                errors[-1] += _log_context_suffix(log_context_id)
                results.append(
                    WriteResult(intune_id=payload.intune_id, action="error", errors=errors)
                )
                continue

            operation = str(item.get("operation") or "").upper()
            action = _OPERATION_TO_ACTION.get(operation)
            if action is None:
                # An operation we cannot interpret is an error in every mode. A
                # dry run is precisely where an unexpected vocabulary has to be
                # visible: reporting it as "unchanged" would let a run that
                # understood none of the response look completely clean.
                results.append(
                    WriteResult(
                        intune_id=payload.intune_id,
                        action="error",
                        errors=[
                            f"unrecognised IRE operation {operation!r}"
                            + _log_context_suffix(log_context_id)
                        ],
                    )
                )
                continue

            if self._dry_run:
                action = f"dry_run:{action}"

            results.append(
                WriteResult(
                    intune_id=payload.intune_id,
                    action=action,
                    sys_id=item.get("sysId") or None,
                )
            )
        return results


@dataclass
class WriteAccessCheck:
    """Outcome of proving the integration user can write, without writing."""

    verified: bool
    detail: str


def verify_write_access(client: ServiceNowClient, cfg: ServiceNowConfig) -> WriteAccessCheck:
    """Prove the IRE write path works, without creating anything.

    `--check` previously proved only that ServiceNow was reachable and readable,
    which leaves the two most common misconfigurations undetected until the
    first real run: an integration user without `itil`/`asset`, and a discovery
    source that was never registered as a choice value.

    This posts a synthetic item to `/api/now/identifyreconcile/query`, which runs
    identification and reports what *would* happen without committing anything.
    It is safe against production: the endpoint has no write path.

    Raises `ServiceNowError` when the write path is definitively broken. Returns
    an unverified result when the answer is genuinely unknown, which is not the
    same thing and must not be reported as success.
    """
    if cfg.write_mode != "identify_reconcile":
        return WriteAccessCheck(
            verified=False,
            detail=(
                f"SNOW_WRITE_MODE={cfg.write_mode} has no simulation endpoint, so write "
                "access cannot be checked without creating a CI. Verify it manually "
                "before the first run."
            ),
        )

    payload = {
        "items": [
            {
                "className": cfg.default_class or "cmdb_ci_computer",
                "values": {
                    "name": "intune-cmdb-sync-write-access-probe",
                    "serial_number": "INTUNE-CMDB-SYNC-PROBE",
                },
                "sys_object_source_info": {
                    "source_native_key": PROBE_SOURCE_KEY,
                    "source_name": cfg.discovery_source,
                },
            }
        ],
        "relations": [],
    }

    response = client.request(
        "POST",
        IDENTIFY_RECONCILE_QUERY_API,
        params={"sysparm_data_source": cfg.discovery_source},
        json_body=payload,
    )

    if response.status_code == 404:
        # Older releases predate the API. That is a real constraint, not a
        # permissions problem, and the fallback write mode still works.
        return WriteAccessCheck(
            verified=False,
            detail=(
                "this instance has no /api/now/identifyreconcile/query endpoint, so write "
                "access could not be simulated. If the release predates the IRE API, use "
                "SNOW_WRITE_MODE=cmdb_instance."
            ),
        )

    if response.status_code in (401, 403):
        detail = describe_error(response)
        if _unscoped_api_suffix(detail):
            raise ServiceNowError(
                f"the Identification and Reconciliation API is not available to these "
                f"credentials: {detail}.{_unscoped_api_suffix(detail)}"
            )
        raise ServiceNowError(
            f"the integration user cannot use the Identification and Reconciliation API: "
            f"{detail}. It needs the 'itil' or 'asset' role."
        )

    if not response.is_success:
        detail = describe_error(response)
        hint = ""
        if "data source" in detail.lower():
            # By far the most common first-run failure, and the message alone
            # does not say where the value has to be registered.
            hint = (
                f" SNOW_DISCOVERY_SOURCE={cfg.discovery_source!r} must exist as a choice "
                "value on cmdb_ci.discovery_source, matching exactly including case "
                "(docs/servicenow-setup.md section 5)."
            )
        raise ServiceNowError(
            f"IRE rejected a write-access probe: {detail}"
            f"{_log_context_suffix(_log_context_id(response))}{hint}"
        )

    result = (response.json() or {}).get("result") or {}
    items = result.get("items") or []
    errors = _collect_item_errors(items[0]) if items else []
    if errors:
        raise ServiceNowError(
            "IRE accepted the request but rejected the probe item: "
            + "; ".join(errors)
            + _log_context_suffix(result.get("logContextId"))
        )

    return WriteAccessCheck(
        verified=True,
        detail=(
            f"IRE simulated a write as {cfg.discovery_source!r} into "
            f"{cfg.default_class or 'cmdb_ci_computer'}; nothing was committed"
        ),
    )


class CmdbInstanceWriter:
    """Per-CI writer built on `POST /api/now/cmdb/instance/{className}`."""

    mode = "cmdb_instance"

    def __init__(self, client: ServiceNowClient, cfg: ServiceNowConfig, *, dry_run: bool = False):
        self._client = client
        self._cfg = cfg
        self._dry_run = dry_run

    def write(self, batch: list[CiPayload]) -> list[WriteResult]:
        if not batch:
            return []
        if self._dry_run:
            return [
                WriteResult(intune_id=item.intune_id, action="dry_run:pending") for item in batch
            ]

        workers = min(self._cfg.concurrency, len(batch))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self._write_one, batch))

    def _write_one(self, item: CiPayload) -> WriteResult:
        body = {"attributes": item.values, "source": self._cfg.discovery_source}
        try:
            response = self._client.request(
                "POST", f"{CMDB_INSTANCE_API}/{item.class_name}", json_body=body
            )
        except Exception as exc:  # one failing device must not sink the whole run
            return WriteResult(intune_id=item.intune_id, action="error", errors=[str(exc)])

        if not response.is_success:
            detail = describe_error(response)
            return WriteResult(
                intune_id=item.intune_id,
                action="error",
                errors=[f"{detail}{_unscoped_api_suffix(detail)}"],
            )

        result = (response.json() or {}).get("result") or {}
        attributes = result.get("attributes") or {}
        error = result.get("error")
        if error:
            detail = error.get("message") or error.get("detail") or str(error)
            return WriteResult(intune_id=item.intune_id, action="error", errors=[str(detail)])

        sys_id = attributes.get("sys_id")
        if not sys_id:
            return WriteResult(
                intune_id=item.intune_id,
                action="error",
                errors=["CMDB Instance API response contained no sys_id"],
            )

        # This endpoint reports no INSERT/UPDATE distinction, so treat every
        # success as an upsert. sys_created_on == sys_updated_on is a reliable
        # enough signal for a fresh record when both are present.
        created = attributes.get("sys_created_on")
        updated = attributes.get("sys_updated_on")
        action = "inserted" if created and created == updated else "updated"
        return WriteResult(intune_id=item.intune_id, action=action, sys_id=str(sys_id))


def _unscoped_api_suffix(detail: str) -> str:
    """Explain a "User Not Authorized / Access to unscoped api" refusal.

    Confirmed live on 2026-08-28 against both `/api/now/identifyreconcile` and
    `/api/now/cmdb/instance/{class}`, on a credential whose Table API reads were
    working in the same run.
    """
    return _UNSCOPED_API_HINT if _UNSCOPED_API_MARKER in detail.lower() else ""


def _log_context_id(response: Any) -> str | None:
    """Pull IRE's logContextId out of a response body that may not be JSON."""
    try:
        payload = response.json() or {}
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if isinstance(result, dict) and result.get("logContextId"):
        return str(result["logContextId"])
    return str(payload["logContextId"]) if payload.get("logContextId") else None


def _log_context_suffix(log_context_id: str | None) -> str:
    return f" [IRE logContextId={log_context_id}]" if log_context_id else ""


def _collect_item_errors(item: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    for err in item.get("errors") or []:
        if isinstance(err, dict):
            label = err.get("error") or ""
            detail = err.get("message") or ""
            messages.append(f"{label}: {detail}".strip(": ").strip())
        else:
            messages.append(str(err))
    return [m for m in messages if m]


def build_writer(
    client: ServiceNowClient, cfg: ServiceNowConfig, *, dry_run: bool = False
) -> Writer:
    if cfg.write_mode == "cmdb_instance":
        return CmdbInstanceWriter(client, cfg, dry_run=dry_run)
    return IdentifyReconcileWriter(client, cfg, dry_run=dry_run)
