"""`identify_reconcile` (default, recommended)

`POST /api/now/identifyreconcile` — the base-platform Identification and
Reconciliation API. It accepts a bulk `items` array in a single request and
carries `sys_object_source_info`, which lets IRE key each CI on the Intune
`managedDevice.id` (`source_native_key`). That is what makes the sync stable
across motherboard swaps, serial-number corrections, and device renames.
Requires the `itil` or `asset` role. No plugin purchase, no Service Graph
Connector subscription.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from ...config import ServiceNowConfig
from ...errors import ServiceNowError
from ...http import describe_error
from ..client import ServiceNowClient
from ._constants import (
    IDENTIFY_RECONCILE_API,
    IDENTIFY_RECONCILE_ENHANCED_API,
    IDENTIFY_RECONCILE_QUERY_API,
)
from .errors import (
    _collect_item_errors,
    _ire_item_error,
    _log_context_id,
    _log_context_suffix,
    _unscoped_api_suffix,
)

log = logging.getLogger(__name__)

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
    # Set when the writer stopped early because writing was failing
    # systematically. A run that stopped early is not a complete run, so the
    # report has to be able to see it.
    aborted: str | None

    def write(self, batch: list[CiPayload]) -> list[WriteResult]: ...


class IdentifyReconcileWriter:
    """Bulk writer built on `POST /api/now/identifyreconcile`."""

    mode = "identify_reconcile"

    def __init__(self, client: ServiceNowClient, cfg: ServiceNowConfig, *, dry_run: bool = False):
        self._client = client
        self._cfg = cfg
        self._dry_run = dry_run
        # This writer submits a whole batch per request, so a systematic
        # failure already surfaces as one error rather than hundreds. Declared
        # to satisfy the Writer protocol; never set.
        self.aborted: str | None = None

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
            #
            # Per-item errors normally arrive inside a 200, which `_parse_results`
            # handles. A 4xx can still carry the same envelope, and there the
            # message sits behind enough identification bookkeeping that the
            # snippet in `describe_error` cuts off before reaching it -- observed
            # on the CMDB Instance endpoint, which runs through the same engine.
            detail = _ire_item_error(response) or describe_error(response)
            raise ServiceNowError(
                f"identifyreconcile ({len(batch)} items) failed: {detail}"
                f"{_unscoped_api_suffix(detail, path=self._endpoint())}"
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
