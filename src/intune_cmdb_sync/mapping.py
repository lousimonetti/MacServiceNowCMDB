"""Translate a Graph `managedDevice` into a ServiceNow CMDB payload.

Everything here is pure: no I/O, no network. That keeps the field decisions
testable and makes it obvious to a CMDB owner reviewing the repo exactly what
lands on their CI records.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from .config import Config
from .models import EntraUser, SysUserRef

log = logging.getLogger(__name__)

# Graph emits this when a DateTimeOffset was never set.
_NULL_DATETIMES = frozenset({"0001-01-01T00:00:00Z", "0001-01-01T00:00:00.0000000Z"})

_WHITESPACE = re.compile(r"\s+")

BYTES_PER_MB = 1024 * 1024
BYTES_PER_GB = 1024 * 1024 * 1024

# Intune `operatingSystem` -> the value ServiceNow's `os` field expects. Instances
# customise this list, so it is overridable via the mapping-overrides file.
DEFAULT_OS_NAMES = {
    "windows": "Windows",
    "macos": "Mac OS X",
    "ios": "iOS",
    "ipados": "iPadOS",
    "android": "Android",
    "linux": "Linux",
    "chromeos": "ChromeOS",
}

# Serial numbers shorter than this are placeholders, not identifiers.
MIN_SERIAL_LENGTH = 3


def normalize_serial(raw: str | None, blocklist: frozenset[str]) -> str | None:
    """Return a trustworthy serial number, or None.

    A junk serial that slips through is worse than no serial at all: IRE will
    happily identify every machine sharing `To be filled by O.E.M.` as the same
    CI and collapse the fleet into one record.
    """
    if not raw:
        return None
    value = _WHITESPACE.sub(" ", raw.strip())
    if not value:
        return None
    if value.lower() in blocklist:
        return None
    if len(value) < MIN_SERIAL_LENGTH:
        return None
    # An all-zero or all-same-character serial is never real.
    stripped = value.replace("-", "").replace(" ", "")
    if stripped and len(set(stripped)) == 1:
        return None
    return value


def to_snow_datetime(raw: str | None) -> str | None:
    """Convert a Graph ISO-8601 timestamp to ServiceNow's `YYYY-MM-DD HH:MM:SS` UTC."""
    if not raw or raw in _NULL_DATETIMES:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python's parser accepts at most 6 fractional digits; Graph emits 7.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        log.debug("unparseable timestamp from Graph", extra={"value": raw})
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed.year <= 1:
        return None
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def bytes_to_mb(raw: Any) -> int | None:
    value = _as_int(raw)
    return round(value / BYTES_PER_MB) if value else None


def bytes_to_gb(raw: Any) -> float | None:
    value = _as_int(raw)
    if not value:
        return None
    return round(value / BYTES_PER_GB, 2)


def _as_int(raw: Any) -> int | None:
    if raw in (None, "", False):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        stripped = _WHITESPACE.sub(" ", value.strip())
        return stripped or None
    return value


def resolve_class_name(device: dict[str, Any], cfg: Config) -> str | None:
    """Pick the CMDB class for a device from its Intune `operatingSystem`."""
    os_name = str(device.get("operatingSystem") or "").strip().lower()
    mapped = cfg.servicenow.class_map.get(os_name)
    if mapped:
        return mapped
    return cfg.servicenow.default_class


def os_display_name(device: dict[str, Any], overrides: dict[str, str]) -> str | None:
    raw = str(device.get("operatingSystem") or "").strip()
    if not raw:
        return None
    lookup = {**DEFAULT_OS_NAMES, **{k.lower(): v for k, v in overrides.items()}}
    return lookup.get(raw.lower(), raw)


class DeviceMapper:
    """Builds the `values` dict written to the CMDB for one device."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        overrides = cfg.runtime.mapping_overrides
        self._os_names: dict[str, str] = dict(overrides.get("os_names") or {})
        self._extra_fields: dict[str, str] = dict(overrides.get("fields") or {})
        self._static: dict[str, Any] = {
            **cfg.servicenow.extra_attributes,
            **dict(overrides.get("static") or {}),
        }
        self._drop: set[str] = {str(f) for f in (overrides.get("drop") or [])}

    def serial_for(self, device: dict[str, Any]) -> str | None:
        return normalize_serial(
            device.get("serialNumber"), self.cfg.runtime.serial_blocklist
        )

    def build_values(
        self,
        device: dict[str, Any],
        *,
        sys_user: SysUserRef | None = None,
        entra_user: EntraUser | None = None,
        references: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Map one device to CMDB field values.

        `references` carries already-resolved sys_ids for reference fields
        (`manufacturer`, `model_id`), because those cannot be set from a display
        name through IRE.
        """
        snow = self.cfg.servicenow
        references = references or {}

        values: dict[str, Any] = {
            "name": _clean(device.get("deviceName")) or _clean(device.get("managedDeviceName")),
            "serial_number": self.serial_for(device),
            "os": os_display_name(device, self._os_names),
            "os_version": _clean(device.get("osVersion")),
            "mac_address": _normalize_mac(
                device.get("ethernetMacAddress") or device.get("wiFiMacAddress")
            ),
            "ram": bytes_to_mb(device.get("physicalMemoryInBytes")),
            "disk_space": bytes_to_gb(device.get("totalStorageSpaceInBytes")),
            "first_discovered": to_snow_datetime(device.get("enrolledDateTime")),
            "last_discovered": to_snow_datetime(device.get("lastSyncDateTime")),
            "virtual": False,
        }

        # Reference fields only go on the payload when we have a real sys_id.
        for field_name in ("manufacturer", "model_id"):
            sys_id = references.get(field_name)
            if sys_id:
                values[field_name] = sys_id

        if snow.set_correlation and snow.correlation_field:
            values[snow.correlation_field] = device.get("id")

        if snow.assign_user and sys_user is not None:
            values["assigned_to"] = sys_user.sys_id

        if snow.install_status_active:
            values["install_status"] = snow.install_status_active

        # Admin-declared extra mappings: {"u_compliance_state": "complianceState"}
        for cmdb_field, graph_field in self._extra_fields.items():
            values[cmdb_field] = _map_extra_field(device, graph_field, entra_user)

        values.update(self._static)

        for field_name in self._drop:
            values.pop(field_name, None)

        return {k: v for k, v in values.items() if v is not None and v != ""}


def _map_extra_field(
    device: dict[str, Any], graph_field: str, entra_user: EntraUser | None
) -> Any:
    """Resolve an override mapping target.

    `user.<attr>` reads from the resolved Entra user; anything else is a
    top-level `managedDevice` property. Timestamp-looking values are converted to
    ServiceNow's datetime format automatically.
    """
    if graph_field.startswith("user."):
        if entra_user is None:
            return None
        return _clean(getattr(entra_user, graph_field[5:], None))

    raw = device.get(graph_field)
    if isinstance(raw, str) and _looks_like_timestamp(raw):
        return to_snow_datetime(raw)
    return _clean(raw)


_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _looks_like_timestamp(value: str) -> bool:
    return bool(_TIMESTAMP_RE.match(value))


def _normalize_mac(raw: str | None) -> str | None:
    """Normalise a MAC address to colon-separated uppercase, or None."""
    if not raw:
        return None
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", raw)
    if len(hex_only) != 12 or set(hex_only.lower()) == {"0"}:
        return None
    upper = hex_only.upper()
    return ":".join(upper[i : i + 2] for i in range(0, 12, 2))
