"""Run-to-run state.

The only thing that genuinely needs to persist between runs is the map of
Intune device ID -> CMDB sys_id. It lets a later run retire CIs for devices that
have disappeared from Intune, without having to reverse-engineer which CIs this
connector owns.

The payload is small (a few hundred KB for a 20k fleet). Where it lives is
decided by `STATE_PATH` and handled by `storage.py`. If no state location is
configured the connector still works — it just cannot retire anything.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .storage import StateStore

log = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass
class SyncState:
    version: int = STATE_VERSION
    last_run_at: str | None = None
    # intune device id -> {"sys_id": ..., "name": ..., "class_name": ..., "last_seen": ...}
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def load(store: StateStore | None) -> SyncState:
        if store is None:
            return SyncState()
        payload = store.read()
        if not payload:
            return SyncState()
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            log.warning(
                "state is not valid JSON; starting from empty state",
                extra={"path": store.location, "error": str(exc)},
            )
            return SyncState()
        if not isinstance(raw, dict):
            return SyncState()
        devices = raw.get("devices")
        return SyncState(
            version=int(raw.get("version") or STATE_VERSION),
            last_run_at=raw.get("last_run_at"),
            devices=devices if isinstance(devices, dict) else {},
        )

    def save(self, store: StateStore | None) -> None:
        if store is None:
            return
        self.last_run_at = datetime.now(UTC).isoformat()
        store.write(
            json.dumps(
                {
                    "version": self.version,
                    "last_run_at": self.last_run_at,
                    "devices": self.devices,
                },
                indent=2,
            )
        )

    def observe(
        self, intune_id: str, *, sys_id: str | None, name: str, class_name: str | None = None
    ) -> None:
        entry = self.devices.setdefault(intune_id, {})
        entry["name"] = name
        entry["last_seen"] = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        if sys_id:
            entry["sys_id"] = sys_id
        if class_name:
            # Retirement PATCHes the CI through its own class table, so the class
            # has to survive alongside the sys_id.
            entry["class_name"] = class_name

    def missing_since_last_run(self, current_ids: set[str]) -> dict[str, dict[str, Any]]:
        """Devices we previously wrote that Intune no longer reports."""
        return {
            intune_id: entry
            for intune_id, entry in self.devices.items()
            if intune_id not in current_ids and entry.get("sys_id")
        }

    def forget(self, intune_ids: set[str]) -> None:
        for intune_id in intune_ids:
            self.devices.pop(intune_id, None)
