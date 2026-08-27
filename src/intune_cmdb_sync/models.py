"""Normalized data structures passed between the Graph and ServiceNow halves."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .logging_setup import current_run_id


@dataclass(frozen=True)
class EntraUser:
    """The subset of an Entra ID user we use to find the matching sys_user."""

    object_id: str
    user_principal_name: str | None = None
    mail: str | None = None
    display_name: str | None = None
    employee_id: str | None = None
    department: str | None = None
    company_name: str | None = None
    office_location: str | None = None
    city: str | None = None
    country: str | None = None
    account_enabled: bool | None = None

    @staticmethod
    def from_graph(payload: dict[str, Any]) -> EntraUser:
        return EntraUser(
            object_id=str(payload.get("id") or ""),
            user_principal_name=payload.get("userPrincipalName"),
            mail=payload.get("mail"),
            display_name=payload.get("displayName"),
            employee_id=payload.get("employeeId"),
            department=payload.get("department"),
            company_name=payload.get("companyName"),
            office_location=payload.get("officeLocation"),
            city=payload.get("city"),
            country=payload.get("country"),
            account_enabled=payload.get("accountEnabled"),
        )

    @property
    def primary_email(self) -> str | None:
        return self.mail or self.user_principal_name


@dataclass(frozen=True)
class SysUserRef:
    """A resolved ServiceNow sys_user record."""

    sys_id: str
    user_name: str | None
    email: str | None
    employee_number: str | None
    matched_on: str


@dataclass
class DeviceOutcome:
    """What happened to one device during the run."""

    intune_id: str
    device_name: str
    serial_number: str | None
    class_name: str | None = None
    action: str = "pending"  # inserted | updated | unchanged | skipped | error | dry_run
    ci_sys_id: str | None = None
    user_matched_on: str | None = None
    assigned_to: str | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intune_id": self.intune_id,
            "device_name": self.device_name,
            "serial_number": self.serial_number,
            "class_name": self.class_name,
            "action": self.action,
            "ci_sys_id": self.ci_sys_id,
            "user_matched_on": self.user_matched_on,
            "assigned_to": self.assigned_to,
            "message": self.message,
        }


@dataclass
class RunReport:
    """Machine-readable summary written at the end of every run."""

    # Same id stamped on every log line for this run, so a report and the logs
    # that produced it can be joined.
    run_id: str = field(default_factory=current_run_id)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    dry_run: bool = False
    write_mode: str = ""
    devices_fetched: int = 0
    devices_after_ownership_filter: int = 0
    devices_skipped_no_class: int = 0
    devices_skipped_no_identifier: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    retired: int = 0
    users_resolved: int = 0
    users_unresolved: int = 0
    outcomes: list[DeviceOutcome] = field(default_factory=list)
    error_samples: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Warnings that mean the run did not do its whole job. A tripped safety
    # guard or a lost state file leaves the CMDB in a state the next run cannot
    # reason about, so these have to reach the scheduler as a non-zero exit
    # rather than scrolling past in the logs.
    degraded: list[str] = field(default_factory=list)
    unresolved_references: dict[str, list[str]] = field(default_factory=dict)

    def degrade(self, message: str) -> None:
        """Record a warning that must also change the process exit code."""
        self.warnings.append(message)
        self.degraded.append(message)

    def record(self, outcome: DeviceOutcome) -> None:
        self.outcomes.append(outcome)
        if outcome.action == "inserted":
            self.inserted += 1
        elif outcome.action == "updated":
            self.updated += 1
        elif outcome.action == "unchanged":
            self.unchanged += 1
        elif outcome.action == "error":
            self.errors += 1
            if outcome.message and len(self.error_samples) < 20:
                self.error_samples.append(f"{outcome.device_name}: {outcome.message}")

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "dry_run": self.dry_run,
            "write_mode": self.write_mode,
            "devices_fetched": self.devices_fetched,
            "devices_after_ownership_filter": self.devices_after_ownership_filter,
            "devices_skipped_no_class": self.devices_skipped_no_class,
            "devices_skipped_no_identifier": self.devices_skipped_no_identifier,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "retired": self.retired,
            "users_resolved": self.users_resolved,
            "users_unresolved": self.users_unresolved,
            "error_samples": self.error_samples,
            "warnings": self.warnings,
            "degraded": self.degraded,
            "unresolved_references": self.unresolved_references,
        }

    def as_dict(self, include_outcomes: bool = True) -> dict[str, Any]:
        payload = self.summary()
        if include_outcomes:
            payload["devices"] = [o.as_dict() for o in self.outcomes]
        return payload
