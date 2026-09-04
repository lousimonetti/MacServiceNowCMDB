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
import threading
from collections.abc import Mapping
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

# A class name that cannot exist. POSTing to it exercises the CMDB Instance API
# with nowhere to write, which is how that endpoint's availability gets proven
# without creating a CI. Shared with probe.py.
PROBE_CLASS = "cmdb_ci_intune_cmdb_sync_probe_no_such_class"

# ServiceNow's body for a URI that routes to no REST API at all. It is the only
# thing separating "this API does not exist" from "this API answered and the
# class you named does not exist" -- both come back 404.
NO_SUCH_API_MARKER = "does not represent any resource"

# ServiceNow refuses an OAuth client that is not authorised for a global-scope
# API *before* it looks at roles or ACLs, and the response says only "User Not
# Authorized". Left unexplained that reads as a missing `itil` role, which is
# the one thing that cannot fix it.
_UNSCOPED_API_MARKER = "unscoped api"

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
            detail = describe_error(response)
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


@dataclass
class WriteAccessCheck:
    """Outcome of proving the integration user can write, without writing."""

    verified: bool
    detail: str
    # Things this check could not establish. `verified` means "the write path
    # is callable and its prerequisites exist", which is not the same as "the
    # first run will succeed"; anything in that gap belongs here rather than
    # being folded into a pass or a fail.
    caveats: list[str] = field(default_factory=list)


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
    if cfg.write_mode == "cmdb_instance":
        return _verify_cmdb_instance_access(client, cfg)
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
                f"credentials: {detail}."
                f"{_unscoped_api_suffix(detail, path=IDENTIFY_RECONCILE_QUERY_API)}"
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


def _verify_cmdb_instance_access(
    client: ServiceNowClient, cfg: ServiceNowConfig
) -> WriteAccessCheck:
    """Verify the CMDB Instance write path without creating a CI.

    This mode has no simulation endpoint, so it used to be reported as
    uncheckable. That was the right answer while it was a fallback nobody used.
    It is the wrong answer on an instance where the identifyreconcile API is
    refused at the OAuth gate and this one is not, because then it is the write
    path -- and "uncheckable" leaves the operator with a first run as their
    first test.

    Two things can be established without writing, and they are the two that
    fail first:

    * whether `POST /api/now/cmdb/instance/{class}` is callable at all, proven
      by posting to a class that cannot exist -- the request reaches the API,
      which rejects it on the class name, having already cleared the gate;
    * whether `SNOW_DISCOVERY_SOURCE` is a registered choice value, read from
      `sys_choice`.

    What cannot be established is whether the class's identification rules will
    accept the connector's attributes. That is a caveat, not a pass.
    """
    response = client.request("POST", f"{CMDB_INSTANCE_API}/{PROBE_CLASS}", json_body={})

    if response.status_code in (401, 403):
        detail = describe_error(response)
        if unscoped_api_refusal(detail):
            raise ServiceNowError(
                f"the CMDB Instance API is not available to these credentials: {detail}."
                f"{_unscoped_api_suffix(detail, path=f'{CMDB_INSTANCE_API}/{{className}}')}"
            )
        raise ServiceNowError(
            f"the integration user cannot write through the CMDB Instance API: {detail}. "
            "It needs the 'itil' or 'asset' role."
        )

    if response.status_code == 404 and NO_SUCH_API_MARKER in (response.text or "").lower():
        return WriteAccessCheck(
            verified=False,
            detail=(
                f"this instance has no {CMDB_INSTANCE_API} endpoint, so "
                "SNOW_WRITE_MODE=cmdb_instance cannot run here."
            ),
        )

    caveats = _discovery_source_caveats(client, cfg)
    caveats.append(
        "the CMDB Instance API has no simulation endpoint, so whether the identification "
        f"rules for {cfg.default_class or 'cmdb_ci_computer'} accept these attributes is "
        "only knowable from a real write. Run with --limit 1 first."
    )
    if cfg.retire_missing:
        # Retirement is a Table API PATCH, a different API behind the same
        # gate. A run can therefore write CIs happily and then fail only at
        # retirement, which is the worst place to discover it.
        caveats.append(
            "SNOW_RETIRE_MISSING is on: retirement PATCHes the Table API, which is a "
            "separate API from this one and separately scoped. Confirm it with "
            "`intune-cmdb-sync --check-api` (the table_update row)."
        )
    if not (cfg.set_correlation and cfg.correlation_field):
        # Without sys_object_source_info there is nothing on the CI tying it to
        # the Intune device, so the correlation field is the only such link.
        caveats.append(
            "SNOW_SET_CORRELATION is off. This write mode cannot send "
            "sys_object_source_info, so with no correlation field there is nothing on "
            "the CI recording which Intune device it came from."
        )

    return WriteAccessCheck(
        verified=True,
        detail=(
            f"POST {CMDB_INSTANCE_API}/{{className}} is callable by these credentials "
            f"(probed with a class that does not exist: HTTP {response.status_code}); "
            "nothing was created"
        ),
        caveats=caveats,
    )


def _discovery_source_caveats(client: ServiceNowClient, cfg: ServiceNowConfig) -> list[str]:
    """Check `SNOW_DISCOVERY_SOURCE` against the registered choice values.

    Reads the whole choice list rather than querying for the configured value,
    so that a miss can name the alternatives. "Intune is not registered" sends
    someone to a ServiceNow admin; "Intune is not registered, these eleven are"
    is often solvable without leaving the terminal -- and it catches the
    case-and-spacing near-misses this field is prone to, since the value must
    match exactly.
    """
    try:
        rows = client.query_table(
            "sys_choice",
            query="name=cmdb_ci^element=discovery_source",
            fields=("value", "label"),
            limit=200,
        )
    except ServiceNowError as exc:
        return [
            f"could not read sys_choice to confirm SNOW_DISCOVERY_SOURCE="
            f"{cfg.discovery_source!r} is registered ({exc}); an unregistered value is "
            "rejected on every write"
        ]

    registered = [str(row.get("value") or "") for row in rows]
    if cfg.discovery_source in registered:
        return []

    caveat = (
        f"SNOW_DISCOVERY_SOURCE={cfg.discovery_source!r} is not a choice value on "
        "cmdb_ci.discovery_source, so every write will be rejected with "
        "INVALID_INPUT_DATA. Register it (docs/servicenow-setup.md section 5) or use one "
        "of the registered values"
    )
    near = [v for v in registered if v.strip().lower() == cfg.discovery_source.strip().lower()]
    if near:
        # The field matches exactly, so a case difference is a real failure and
        # an easy one to stare past.
        caveat += (
            f". Note {near[0]!r} is registered and differs from the configured value only "
            "by case or spacing; this field matches exactly"
        )
    elif registered:
        caveat += ": " + ", ".join(repr(v) for v in sorted(registered)[:20])
        if len(registered) > 20:
            caveat += f", and {len(registered) - 20} more"
    else:
        caveat += ", but cmdb_ci.discovery_source has no choice values at all"
    return [caveat]


class CmdbInstanceWriter:
    """Per-CI writer built on `POST /api/now/cmdb/instance/{className}`."""

    mode = "cmdb_instance"

    def __init__(self, client: ServiceNowClient, cfg: ServiceNowConfig, *, dry_run: bool = False):
        self._client = client
        self._cfg = cfg
        self._dry_run = dry_run
        self.aborted: str | None = None
        # A per-CI writer turns one systematic problem -- an unregistered
        # discovery source, a mandatory attribute the payload omits, an ACL on
        # one field -- into one failed POST per device. IRE fails such a run
        # once; this one would fail it 200 times against a production instance.
        # The guard only trips while *nothing* has succeeded, so a fleet with a
        # handful of genuinely bad devices still runs to completion.
        self._lock = threading.Lock()
        self._failures = 0
        self._successes = 0

    def write(self, batch: list[CiPayload]) -> list[WriteResult]:
        if not batch:
            return []
        if self._dry_run:
            return self._preview(batch)

        workers = min(self._cfg.concurrency, len(batch))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self._write_one, batch))

    def _record(self, result: WriteResult) -> WriteResult:
        """Track outcomes and trip the guard once failure looks systematic."""
        threshold = self._cfg.abort_after_errors
        with self._lock:
            if result.action == "error":
                self._failures += 1
            else:
                self._successes += 1
            if (
                threshold
                and self.aborted is None
                and self._successes == 0
                and self._failures >= threshold
            ):
                self.aborted = (
                    f"stopped after {self._failures} consecutive write failures with no "
                    f"successes; the first {self._failures} devices all failed the same "
                    f"way, so this is a configuration problem rather than bad data. Last "
                    f"error: {result.message}. Raise SNOW_ABORT_AFTER_ERRORS or set it to "
                    "0 to write the whole batch anyway."
                )
                log.error(
                    "aborting per-CI writes: every write is failing",
                    extra={"failures": self._failures, "threshold": threshold},
                )
        return result

    def _preview(self, batch: list[CiPayload]) -> list[WriteResult]:
        """Predict insert vs update using reads only.

        The CMDB Instance API has no simulation endpoint, so this mode used to
        report every device as "pending" -- a dry run that could not say
        anything at all. That is tolerable for a fallback and not tolerable for
        the only write path an instance allows, because it removes the one step
        between configuring the connector and letting it write to production.

        The prediction reproduces how the API identifies a CI: the class's
        identifier rules, which for the computer classes are serial number
        first, then name. It is a prediction, not a simulation -- a rule the
        admin has customised, or an IRE reclassification, can still make the
        real write do something else. `_lookup` failing is not fatal: an
        unpredictable device is reported as such rather than guessed at.
        """
        try:
            by_serial, by_name = self._lookup(batch)
        except ServiceNowError as exc:
            log.warning("dry run could not read existing CIs", extra={"error": str(exc)})
            return [
                WriteResult(
                    intune_id=item.intune_id,
                    action="dry_run:pending",
                    errors=[f"could not look up the existing CI: {exc}"],
                )
                for item in batch
            ]

        results: list[WriteResult] = []
        for item in batch:
            serial = (item.serial_number or "").strip().lower()
            match = by_serial.get(serial) if serial else None
            if match is None:
                match = by_name.get((item.device_name or "").strip().lower())
            results.append(
                WriteResult(
                    intune_id=item.intune_id,
                    action="dry_run:updated" if match else "dry_run:inserted",
                    sys_id=match.get("sys_id") if match else None,
                )
            )
        return results

    def _lookup(
        self, batch: list[CiPayload]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """One Table API read per class, indexed by serial number and by name."""
        by_serial: dict[str, dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}

        classes: dict[str, list[CiPayload]] = {}
        for item in batch:
            classes.setdefault(item.class_name, []).append(item)

        for class_name, items in classes.items():
            serials = {_query_safe(i.serial_number) for i in items}
            names = {_query_safe(i.device_name) for i in items}
            serials.discard(None)
            names.discard(None)
            clauses = []
            if serials:
                clauses.append("serial_numberIN" + ",".join(sorted(serials)))  # type: ignore[arg-type]
            if names:
                clauses.append("nameIN" + ",".join(sorted(names)))  # type: ignore[arg-type]
            if not clauses:
                continue

            rows = self._client.query_table(
                class_name,
                query="^OR".join(clauses),
                fields=("sys_id", "name", "serial_number"),
                limit=max(len(items) * 2, 10),
            )
            for row in rows:
                serial = str(row.get("serial_number") or "").strip().lower()
                name = str(row.get("name") or "").strip().lower()
                # First match wins: a duplicate serial in the CMDB is a data
                # problem there, and picking arbitrarily would make the dry run
                # unstable between runs for no benefit.
                if serial:
                    by_serial.setdefault(serial, row)
                if name:
                    by_name.setdefault(name, row)
        return by_serial, by_name

    def _write_one(self, item: CiPayload) -> WriteResult:
        if self.aborted is not None:
            # Not "skipped": these devices were meant to be written and were
            # not, and a run report that called that a skip would look clean.
            return WriteResult(
                intune_id=item.intune_id,
                action="error",
                errors=[f"not attempted: {self.aborted}"],
            )

        body = {
            "attributes": stringify_attributes(item.values),
            "source": self._cfg.discovery_source,
        }
        try:
            response = self._client.request(
                "POST", f"{CMDB_INSTANCE_API}/{item.class_name}", json_body=body
            )
        except Exception as exc:  # one failing device must not sink the whole run
            return self._record(
                WriteResult(intune_id=item.intune_id, action="error", errors=[str(exc)])
            )

        if not response.is_success:
            api_path = f"{CMDB_INSTANCE_API}/{item.class_name}"
            # A 4xx from this endpoint usually carries a structured IRE result,
            # not a plain error: the useful part is result.items[].errors[]. The
            # raw body is ~800 characters of identification bookkeeping with the
            # message near the end, so reporting it verbatim truncates away the
            # only sentence worth reading.
            detail = _ire_item_error(response) or describe_error(response)
            return self._record(
                WriteResult(
                    intune_id=item.intune_id,
                    action="error",
                    errors=[
                        f"{detail}"
                        f"{_unscoped_api_suffix(detail, path=api_path)}"
                        f"{_data_source_hint(detail, self._cfg)}"
                    ],
                )
            )

        result = (response.json() or {}).get("result") or {}
        attributes = result.get("attributes") or {}
        error = result.get("error")
        if error:
            detail = error.get("message") or error.get("detail") or str(error)
            return self._record(
                WriteResult(intune_id=item.intune_id, action="error", errors=[str(detail)])
            )

        sys_id = attributes.get("sys_id")
        if not sys_id:
            return self._record(
                WriteResult(
                    intune_id=item.intune_id,
                    action="error",
                    errors=["CMDB Instance API response contained no sys_id"],
                )
            )

        # This endpoint reports no INSERT/UPDATE distinction, so treat every
        # success as an upsert. sys_created_on == sys_updated_on is a reliable
        # enough signal for a fresh record when both are present.
        created = attributes.get("sys_created_on")
        updated = attributes.get("sys_updated_on")
        action = "inserted" if created and created == updated else "updated"
        return self._record(
            WriteResult(intune_id=item.intune_id, action=action, sys_id=str(sys_id))
        )


def _ire_item_error(response: Any) -> str | None:
    """Pull the per-item IRE errors out of a failed CMDB Instance response.

    That endpoint runs through IRE, so a rejected write comes back as an IRE
    result envelope rather than a simple `{"error": ...}`. Observed live
    2026-09-04:

        {"result":{"items":[{"identifierEntrySysId":"Unknown",
         "identificationAttempts":[],...,"errors":[{"error":"INVALID_INPUT_DATA",
         "message":"In payload invalid data source [Intune] exist..."}]}]}}

    The message sits behind enough bookkeeping that the body's first 400
    characters -- what `describe_error` shows -- cut off mid-sentence.
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if not isinstance(first, dict):
        return None
    errors = _collect_item_errors(first)
    if not errors:
        return None
    return f"HTTP {response.status_code}: " + "; ".join(errors)


def _data_source_hint(detail: str, cfg: ServiceNowConfig) -> str:
    """Say where `SNOW_DISCOVERY_SOURCE` has to be registered.

    `INVALID_INPUT_DATA - In payload invalid data source [X] exist` names the
    field but not the table, and never says that the value is a choice list
    entry someone has to add. It failed every device of the 2026-09-04 run.
    """
    lowered = detail.lower()
    if "data source" not in lowered and "discovery_source" not in lowered:
        return ""
    return (
        f" SNOW_DISCOVERY_SOURCE={cfg.discovery_source!r} must exist as a choice value on "
        "cmdb_ci.discovery_source before any write is accepted, matching exactly including "
        "case. Add it under System Definition > Choice Lists (table cmdb_ci, element "
        "discovery_source), or set SNOW_DISCOVERY_SOURCE to a value already registered — "
        "`intune-cmdb-sync --check` lists the registered ones. See "
        "docs/servicenow-setup.md section 5."
    )


def stringify_attributes(values: Mapping[str, Any]) -> dict[str, str]:
    """Render every attribute as a string, as this API requires.

    `POST /api/now/cmdb/instance/{class}` deserialises `attributes` as a
    map of String to String, and a JSON number or boolean makes it throw before
    it reaches any validation the connector could learn from:

        HTTP 500 - class java.lang.Double cannot be cast to class java.lang.String

    Observed live 2026-09-04, where it failed every device in the run. The
    culprit was `disk_space` (`bytes_to_gb` returns a rounded float), but `ram`
    (int) and `virtual` (bool) would have thrown the same way with a different
    class name, so this coerces the whole payload rather than that one field.

    IRE is deliberately left alone: `/api/now/identifyreconcile` accepts typed
    values, and narrowing them to strings there would be a change to a working
    payload made for another endpoint's benefit.
    """
    rendered: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            # Python's str() gives "True"/"False"; ServiceNow's boolean fields
            # want the JSON spelling.
            rendered[key] = "true" if value else "false"
        elif isinstance(value, float):
            # 128.0 as "128" rather than "128.0": the field is a decimal, but a
            # value that moves between runs makes every device an update, and
            # "128" is what the same number looks like from any other source.
            rendered[key] = str(int(value)) if value.is_integer() else repr(value)
        else:
            rendered[key] = str(value)
    return rendered


def _query_safe(value: str | None) -> str | None:
    """Drop values that would break an encoded query rather than escaping them.

    `,` separates the values of an `IN` clause and `^` separates clauses, so a
    serial or device name containing either cannot go in one. Losing the
    lookup costs a dry-run prediction; a malformed query would silently match
    the wrong records, which is worse.
    """
    cleaned = (value or "").strip()
    if not cleaned or "," in cleaned or "^" in cleaned:
        return None
    return cleaned


def unscoped_api_refusal(detail: str) -> bool:
    """True when a refusal is the REST gate rather than a role or an ACL."""
    return _UNSCOPED_API_MARKER in detail.lower()


def _unscoped_api_suffix(detail: str, *, method: str = "POST", path: str = "this API") -> str:
    """Explain a "User Not Authorized / Access to unscoped api" refusal.

    Names the exact method and path that was refused: an auth scope binds per
    API *and per HTTP method*, so "writes are unauthorized" is not something an
    admin can act on, while "POST /api/now/identifyreconcile is unauthorized"
    is. Confirmed live on 2026-08-28 against both `/api/now/identifyreconcile`
    and `/api/now/cmdb/instance/{class}`, on a credential whose Table API reads
    were working in the same run.
    """
    if not unscoped_api_refusal(detail):
        return ""
    return (
        f" This is the OAuth client being refused {method} {path} at the REST gate, before "
        "any role or ACL is consulted, so it is not a missing role: the Application Registry "
        "entry is Securely Scoped and has no REST API Auth Scope linked for "
        f"{method} on {path}. Adding 'itil' will not change it. Whether "
        "SNOW_WRITE_MODE=cmdb_instance is refused too depends on which auth scopes exist — "
        "it is a separate API and has been observed allowed on an instance that refuses "
        "identifyreconcile. Run `intune-cmdb-sync --check-api` for a per-endpoint, "
        "per-method breakdown of what this credential may call. See "
        "docs/servicenow-setup.md."
    )


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
