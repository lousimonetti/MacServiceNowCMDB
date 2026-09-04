"""Run orchestration: Graph in, CMDB out."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from .config import Config
from .errors import ServiceNowError
from .graph import GraphClient, is_corporate
from .mapping import DeviceMapper, resolve_class_name, to_snow_datetime
from .models import DeviceOutcome, EntraUser, RunReport, SysUserRef
from .reference_resolver import ReferenceResolver
from .servicenow.client import ServiceNowClient
from .servicenow.writers import CiPayload, Writer, build_writer
from .state import SyncState
from .storage import build_state_store
from .user_resolver import UserResolver

log = logging.getLogger(__name__)


class SyncRunner:
    def __init__(
        self,
        cfg: Config,
        *,
        graph: GraphClient,
        snow: ServiceNowClient,
        writer: Writer | None = None,
    ) -> None:
        self.cfg = cfg
        self.graph = graph
        self.snow = snow
        self.mapper = DeviceMapper(cfg)
        self.writer = writer or build_writer(snow, cfg.servicenow, dry_run=cfg.runtime.dry_run)
        self.user_resolver = UserResolver(snow, cfg.servicenow)
        # Exposed so that a run which raises partway still leaves a report the
        # caller can persist; see __main__._run.
        self.report = RunReport(
            dry_run=cfg.runtime.dry_run, write_mode=cfg.servicenow.write_mode
        )
        self.references = ReferenceResolver(
            snow,
            create_missing_manufacturers=cfg.servicenow.create_missing_manufacturers,
            create_missing_models=cfg.servicenow.create_missing_models,
            dry_run=cfg.runtime.dry_run,
        )

    def run(self) -> RunReport:
        report = self.report
        store = build_state_store(self.cfg.runtime.state_path)
        state = SyncState.load(store)

        if self.cfg.runtime.dry_run and self.writer.mode == "cmdb_instance":
            # The CMDB Instance API has no simulate endpoint, so the insert /
            # update split here is predicted from the class's identifier fields
            # by reading the CMDB, not reported by IRE. Close enough to be worth
            # having before a first write, not close enough to be called a
            # simulation — and the difference has to be in the report, because
            # nothing else in it distinguishes the two.
            report.warnings.append(
                "dry run under SNOW_WRITE_MODE=cmdb_instance predicts insert/update by "
                "looking up each device's serial number, then name, in the CMDB: the "
                "identifier fields the API would use. It is a prediction, not a "
                "simulation — customised identification rules or IRE reclassification "
                "can still make the real write differ, and NO_CHANGE cannot be "
                "distinguished from an update. Only SNOW_WRITE_MODE=identify_reconcile "
                "reports what IRE itself would do."
            )

        identity = self.snow.verify_connectivity()
        log.info(
            "connected to ServiceNow",
            extra={"instance": identity.get("instance"), "write_mode": self.writer.mode},
        )

        devices = self._collect_devices(report)
        if not devices:
            log.warning("no devices to process")
            report.finished_at = datetime.now(UTC)
            return report

        if self.cfg.servicenow.fetch_hardware_detail:
            self._enrich_hardware(devices)

        sys_users, entra_users = self._resolve_users(devices, report)
        self._prime_references(devices)

        payloads, outcome_index = self._build_payloads(
            devices, sys_users, entra_users, report
        )

        for batch in _chunks(payloads, self.cfg.servicenow.batch_size):
            self._write_batch(batch, outcome_index, report, state)

        report.unresolved_references = self.references.unresolved
        if any(report.unresolved_references.values()):
            log.info(
                "some manufacturer/model references had no matching record",
                extra=report.unresolved_references,
            )

        current_ids = {str(d.get("id")) for d in devices if d.get("id")}
        self._retire_missing(current_ids, state, report)

        if not self.cfg.runtime.dry_run:
            try:
                state.save(store)
            except Exception as exc:
                # The CIs were written; only the bookkeeping failed. That is not
                # a reason to fail the whole run, but the next run will start
                # from empty state and silently stop retiring, so it has to show.
                report.degrade(
                    f"could not persist sync state to {getattr(store, 'location', '?')}: "
                    f"{exc}. The CMDB writes succeeded, but the next run will not be "
                    "able to retire devices that disappear from Intune."
                )
                log.error("state save failed", extra={"error": str(exc)})

        report.finished_at = datetime.now(UTC)
        return report

    # ---- stages ----------------------------------------------------------

    def _collect_devices(self, report: RunReport) -> list[dict[str, Any]]:
        ownership = self.cfg.graph.ownership
        limit = self.cfg.runtime.device_limit
        kept: list[dict[str, Any]] = []
        for device in self.graph.iter_managed_devices():
            report.devices_fetched += 1
            if is_corporate(device, ownership):
                kept.append(device)
                # Stop pulling pages once the cap is met; the generator is lazy,
                # so this also saves the Graph calls rather than fetching the
                # whole tenant and discarding most of it.
                if limit is not None and len(kept) >= limit:
                    break
        report.devices_after_ownership_filter = len(kept)
        if limit is not None:
            report.warnings.append(
                f"INTUNE_DEVICE_LIMIT={limit} was in effect: this run processed at most "
                f"{limit} device(s) and is not a full inventory pass"
            )
        log.info(
            "fetched Intune devices",
            extra={
                "fetched": report.devices_fetched,
                "kept": len(kept),
                "ownership": ownership,
                "limit": limit,
            },
        )
        return kept

    def _enrich_hardware(self, devices: list[dict[str, Any]]) -> None:
        """Fill in the Graph properties that the collection endpoint returns as null."""
        log.info("fetching per-device hardware detail", extra={"devices": len(devices)})
        workers = min(self.cfg.servicenow.concurrency, max(len(devices), 1))

        def fetch(device: dict[str, Any]) -> None:
            device_id = str(device.get("id") or "")
            if not device_id:
                return
            detail = self.graph.fetch_device_hardware_detail(device_id)
            for key, value in detail.items():
                if key != "id" and value not in (None, ""):
                    device[key] = value

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(fetch, devices))

    def _resolve_users(
        self, devices: list[dict[str, Any]], report: RunReport
    ) -> tuple[dict[str, SysUserRef | None], dict[str, EntraUser]]:
        if not self.cfg.servicenow.assign_user:
            return {}, {}

        object_ids = [str(d.get("userId")) for d in devices if d.get("userId")]
        if not object_ids:
            return {}, {}

        if self.cfg.graph.enrich_users:
            entra_users = self.graph.get_users(object_ids)
        else:
            # Fall back to the identity fields already on the device record.
            entra_users = {
                str(d["userId"]): EntraUser(
                    object_id=str(d["userId"]),
                    user_principal_name=d.get("userPrincipalName"),
                    mail=d.get("emailAddress"),
                    display_name=d.get("userDisplayName"),
                )
                for d in devices
                if d.get("userId")
            }

        sys_users = self.user_resolver.resolve_many(entra_users.values())
        report.users_resolved = sum(1 for ref in sys_users.values() if ref is not None)
        report.users_unresolved = sum(1 for ref in sys_users.values() if ref is None)
        log.info(
            "resolved device owners",
            extra={
                "entra_users": len(entra_users),
                "matched_sys_users": report.users_resolved,
                "unmatched": report.users_unresolved,
            },
        )
        return sys_users, entra_users

    def _prime_references(self, devices: list[dict[str, Any]]) -> None:
        manufacturers = {str(d.get("manufacturer") or "").strip() for d in devices}
        models = {str(d.get("model") or "").strip() for d in devices}
        self.references.prime(
            {m for m in manufacturers if m}, {m for m in models if m}
        )

    def _build_payloads(
        self,
        devices: list[dict[str, Any]],
        sys_users: dict[str, SysUserRef | None],
        entra_users: dict[str, EntraUser],
        report: RunReport,
    ) -> tuple[list[CiPayload], dict[str, DeviceOutcome]]:
        payloads: list[CiPayload] = []
        outcomes: dict[str, DeviceOutcome] = {}

        for device in devices:
            intune_id = str(device.get("id") or "")
            device_name = str(device.get("deviceName") or device.get("managedDeviceName") or "")
            serial = self.mapper.serial_for(device)
            outcome = DeviceOutcome(
                intune_id=intune_id, device_name=device_name, serial_number=serial
            )

            if not intune_id:
                outcome.action = "skipped"
                outcome.message = "device has no Intune id"
                report.devices_skipped_no_identifier += 1
                report.record(outcome)
                continue

            class_name = resolve_class_name(device, self.cfg)
            if not class_name:
                outcome.action = "skipped"
                outcome.message = (
                    f"no CMDB class mapped for operatingSystem="
                    f"{device.get('operatingSystem')!r}"
                )
                report.devices_skipped_no_class += 1
                report.record(outcome)
                continue
            outcome.class_name = class_name

            user_id = str(device.get("userId") or "")
            sys_user = sys_users.get(user_id) if user_id else None
            entra_user = entra_users.get(user_id) if user_id else None
            if sys_user is not None:
                outcome.user_matched_on = sys_user.matched_on
                outcome.assigned_to = sys_user.sys_id

            values = self.mapper.build_values(
                device,
                sys_user=sys_user,
                entra_user=entra_user,
                references=self.references.references_for(
                    device.get("manufacturer"), device.get("model")
                ),
            )

            # IRE identifies a computer on serial number, then name. With neither,
            # every run would create a fresh duplicate CI.
            if not values.get("serial_number") and not values.get("name"):
                outcome.action = "skipped"
                outcome.message = "no usable serial number or device name to identify the CI"
                report.devices_skipped_no_identifier += 1
                report.record(outcome)
                continue

            payloads.append(
                CiPayload(
                    intune_id=intune_id,
                    class_name=class_name,
                    values=values,
                    device_name=device_name,
                    serial_number=serial,
                    source_recency=to_snow_datetime(device.get("lastSyncDateTime")),
                )
            )
            outcomes[intune_id] = outcome

        return payloads, outcomes

    def _write_batch(
        self,
        batch: list[CiPayload],
        outcome_index: dict[str, DeviceOutcome],
        report: RunReport,
        state: SyncState,
    ) -> None:
        try:
            results = self.writer.write(batch)
        except ServiceNowError as exc:
            # A whole batch failing is a batch-level problem (auth, endpoint,
            # instance outage), not a per-device one. Mark every device in the
            # batch so the report reflects reality.
            log.error("batch write failed", extra={"size": len(batch), "error": str(exc)})
            for item in batch:
                failed = outcome_index[item.intune_id]
                failed.action = "error"
                failed.message = str(exc)
                report.record(failed)
            return

        for result in results:
            outcome = outcome_index.get(result.intune_id)
            if outcome is None:
                continue
            action = result.action
            if action.startswith("dry_run:"):
                simulated = action.split(":", 1)[1]
                if simulated == "pending":
                    outcome.action = "skipped"
                    outcome.message = "dry run - outcome not predictable in this write mode"
                else:
                    outcome.action = simulated
                    outcome.message = "dry run - nothing written"
            else:
                outcome.action = action
            outcome.ci_sys_id = result.sys_id
            if result.message:
                outcome.message = result.message
            report.record(outcome)

            if result.sys_id and outcome.action != "error":
                state.observe(
                    result.intune_id,
                    sys_id=result.sys_id,
                    name=outcome.device_name,
                    class_name=outcome.class_name,
                )

        log.info(
            "wrote batch",
            extra={
                "size": len(batch),
                "inserted": report.inserted,
                "updated": report.updated,
                "errors": report.errors,
            },
        )

    def _retire_missing(
        self, current_ids: set[str], state: SyncState, report: RunReport
    ) -> None:
        cfg = self.cfg.servicenow
        if not cfg.retire_missing:
            return

        if self.cfg.runtime.device_limit is not None:
            # The device list was truncated on purpose, so every device beyond
            # the cap looks like it vanished from Intune. Retiring against that
            # would be catastrophic and the fraction guard would not necessarily
            # catch it, since a small limit makes the missing fraction huge.
            message = (
                "retirement skipped: INTUNE_DEVICE_LIMIT was in effect, so the device "
                "list is a deliberate subset rather than the current fleet"
            )
            log.warning("skipping retirement under a device limit")
            report.warnings.append(message)
            return

        missing = state.missing_since_last_run(current_ids)
        if not missing:
            return

        known_total = len(state.devices) or 1
        fraction = len(missing) / known_total
        if fraction > cfg.retire_max_fraction:
            message = (
                f"refusing to retire {len(missing)} of {known_total} known devices "
                f"({fraction:.0%} > SNOW_RETIRE_MAX_FRACTION={cfg.retire_max_fraction:.0%}); "
                "this usually means the Graph fetch was incomplete, not that the fleet vanished"
            )
            log.error("mass retirement guard tripped", extra={"missing": len(missing)})
            report.degrade(message)
            return

        if self.cfg.runtime.dry_run:
            report.warnings.append(f"dry run: would retire {len(missing)} CIs")
            log.info("dry run: skipping retirement", extra={"count": len(missing)})
            return

        retired: set[str] = set()
        for intune_id, entry in missing.items():
            sys_id = str(entry.get("sys_id") or "")
            class_name = str(entry.get("class_name") or "cmdb_ci_computer")
            try:
                self.snow.update_record(
                    class_name, sys_id, {"install_status": cfg.retire_install_status}
                )
            except ServiceNowError as exc:
                log.warning(
                    "could not retire CI",
                    extra={"sys_id": sys_id, "intune_id": intune_id, "error": str(exc)},
                )
                continue
            retired.add(intune_id)

        state.forget(retired)
        report.retired = len(retired)
        log.info("retired CIs absent from Intune", extra={"count": len(retired)})


def _chunks(items: Sequence[CiPayload], size: int) -> Iterator[list[CiPayload]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])
