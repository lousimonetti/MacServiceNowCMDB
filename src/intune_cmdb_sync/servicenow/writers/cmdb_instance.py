"""`cmdb_instance`

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
from typing import Any

from ...config import ServiceNowConfig
from ...errors import ServiceNowError
from ...http import describe_error
from ..client import CMDB_INSTANCE_API, ServiceNowClient
from .errors import _data_source_hint, _ire_item_error, _query_safe, _unscoped_api_suffix
from .ire import CiPayload, WriteResult

log = logging.getLogger(__name__)


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
