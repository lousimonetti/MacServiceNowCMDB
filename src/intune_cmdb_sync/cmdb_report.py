"""Read-only queries against the CMDB, for validating what is actually on the CIs.

This module never writes. It issues `GET /api/now/table/...` and nothing else --
no POST to `/identifyreconcile`, no PATCH, no record creation -- so it is safe to
point at production while the write path is still being sorted out.

That distinction matters right now. The OAuth client is refused on every write by
the unscoped-api gate (see CLAUDE.md), but Table API reads through the same
credential already work, which is how `user_resolver` resolves real sys_ids. So
this reader runs today, against an instance whose write path does not.

Two things it does that a hand-written `sysparm_query` does not:

- **Display values.** `manufacturer`, `model_id` and `assigned_to` are reference
  fields whose raw value is a sys_id, which tells a human nothing. Every row is
  fetched with `sysparm_display_value=all`, so each cell carries both the sys_id
  and the label. `ServiceNowClient.query_table` deliberately keeps asking for raw
  values -- the resolvers depend on that -- so this reader issues its own request
  rather than adding a mode to a method the write path shares.
- **Paging.** A Table API response is capped, and a truncated read is exactly the
  kind of thing that makes a fleet look smaller than it is.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .config import ServiceNowConfig
from .errors import ServiceNowError
from .servicenow.client import TABLE_API, ServiceNowClient
from .snow_query import build_or_query, usable_values

log = logging.getLogger(__name__)

# Rows per request. Below ServiceNow's own cap, so a page that comes back short
# genuinely means the end of the result set rather than a server-side trim.
PAGE_SIZE = 500

# Hard stop on a report that selected far more than anyone meant to read. Raised
# with --limit; the point is that an unbounded query does not silently page
# through a 200k-CI table.
DEFAULT_MAX_ROWS = 5000

# The fields the connector writes (mapping.DeviceMapper.build_values), plus the
# record identity needed to find the CI again. Reported in this order.
CI_FIELDS: tuple[str, ...] = (
    "sys_id",
    "sys_class_name",
    "name",
    "serial_number",
    "manufacturer",
    "model_id",
    "os",
    "os_version",
    "mac_address",
    "ram",
    "disk_space",
    "assigned_to",
    "install_status",
    "discovery_source",
    "first_discovered",
    "last_discovered",
    "sys_created_on",
    "sys_updated_on",
)

# Fields whose raw value is a sys_id: only the display value is meaningful to a
# reader, and an empty one is the signature of a lookup that failed.
REFERENCE_FIELDS = frozenset({"manufacturer", "model_id", "assigned_to"})


@dataclass(frozen=True)
class Cell:
    """One field of one CI, carrying both representations ServiceNow returns."""

    value: str
    display: str

    @property
    def best(self) -> str:
        """What to show a human: the label if there is one, else the raw value."""
        return self.display or self.value

    @property
    def empty(self) -> bool:
        return not self.value and not self.display


@dataclass(frozen=True)
class Finding:
    """One observation about the fetched rows. Advisory; nothing here is fatal."""

    kind: str
    detail: str
    sys_ids: tuple[str, ...] = ()


@dataclass
class CmdbReport:
    table: str
    query: str
    fields: tuple[str, ...]
    rows: list[dict[str, Cell]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "query": self.query,
            "count": len(self.rows),
            "truncated": self.truncated,
            "findings": [
                {"kind": f.kind, "detail": f.detail, "sys_ids": list(f.sys_ids)}
                for f in self.findings
            ],
            "rows": [
                {
                    name: (
                        {"value": cell.value, "display": cell.display}
                        if cell.display and cell.display != cell.value
                        else cell.best
                    )
                    for name, cell in row.items()
                }
                for row in self.rows
            ],
        }


class CmdbReader:
    """Paged, display-value reads of a CMDB class table."""

    def __init__(
        self,
        client: ServiceNowClient,
        *,
        table: str,
        fields: Iterable[str] = CI_FIELDS,
    ) -> None:
        self._client = client
        self.table = table
        self.fields = tuple(dict.fromkeys(fields))

    def fetch(
        self, query: str, *, max_rows: int = DEFAULT_MAX_ROWS
    ) -> tuple[list[dict[str, Cell]], bool]:
        """Return rows matching `query`, and whether the read hit `max_rows`.

        `sysparm_display_value=all` makes every field an object with `value` and
        `display_value`; `_to_cell` normalises that back to a flat mapping.
        """
        rows: list[dict[str, Cell]] = []
        offset = 0
        while len(rows) < max_rows:
            page_size = min(PAGE_SIZE, max_rows - len(rows))
            payload = self._client.request_json(
                "GET",
                f"{TABLE_API}/{self.table}",
                params={
                    "sysparm_query": query,
                    "sysparm_fields": ",".join(self.fields),
                    "sysparm_limit": page_size,
                    "sysparm_offset": offset,
                    "sysparm_display_value": "all",
                    "sysparm_exclude_reference_link": "true",
                },
                context=f"query {self.table}",
            )
            result = (payload or {}).get("result")
            if result is None:
                result = []
            if not isinstance(result, list):
                raise ServiceNowError(f"query {self.table} returned an unexpected result shape")
            rows.extend(self._to_row(raw) for raw in result)
            log.debug(
                "fetched CMDB page",
                extra={"table": self.table, "offset": offset, "returned": len(result)},
            )
            # A short page is the end of the result set. Asking for one more
            # would be a wasted round trip on every complete read.
            if len(result) < page_size:
                return rows, False
            offset += len(result)
        return rows, True

    def _to_row(self, raw: dict[str, Any]) -> dict[str, Cell]:
        return {name: _to_cell(raw.get(name)) for name in self.fields}


def _to_cell(raw: Any) -> Cell:
    """Normalise one field of a `sysparm_display_value=all` response.

    ServiceNow returns `{"value": ..., "display_value": ...}` per field in that
    mode, but falls back to a bare string for fields that have no separate
    display form, so both shapes have to be handled.
    """
    if isinstance(raw, dict):
        return Cell(value=_text(raw.get("value")), display=_text(raw.get("display_value")))
    return Cell(value=_text(raw), display="")


def _text(raw: Any) -> str:
    if raw is None or isinstance(raw, bool):
        return "" if raw is None else str(raw).lower()
    return str(raw).strip()


# ---- selectors -----------------------------------------------------------
#
# Each returns an encoded query. Values go through snow_query so a model name
# like "MacBook Pro (16-inch, 2023)" cannot silently match nothing.


def by_discovery_source(source: str) -> str:
    return f"discovery_source={source}"


def by_field(field_name: str, values: Iterable[str]) -> str:
    usable = usable_values(values)
    if not usable:
        raise ValueError(f"no usable {field_name} values were given")
    return build_or_query(field_name, usable)


def combine(*clauses: str | None) -> str:
    """AND the given clauses together, skipping any that are empty."""
    return "^".join(clause for clause in clauses if clause)


# ---- validation ----------------------------------------------------------


def analyze(
    rows: list[dict[str, Cell]], cfg: ServiceNowConfig, *, correlation_field: str | None = None
) -> list[Finding]:
    """Flag the CI conditions worth a second look. Read-only; changes nothing.

    Every check here corresponds to a way the connector's own mapping can land
    wrong -- an unresolved reference, a dropped serial, an identity collision --
    rather than to general CMDB hygiene.
    """
    findings: list[Finding] = []

    def flag(kind: str, detail: str, matched: list[dict[str, Cell]]) -> None:
        if matched:
            findings.append(
                Finding(kind, detail, tuple(_cell(row, "sys_id").value for row in matched))
            )

    flag(
        "missing_serial",
        "no serial_number: IRE cannot identify these by hardware, so a re-run may "
        "insert duplicates rather than update them",
        [row for row in rows if _cell(row, "serial_number").empty],
    )
    flag(
        "missing_name",
        "no name: the CI is effectively unreadable in the UI",
        [row for row in rows if _cell(row, "name").empty],
    )

    for field_name in ("manufacturer", "model_id"):
        if field_name not in _field_names(rows):
            continue
        flag(
            f"unresolved_{field_name}",
            f"empty {field_name}: the reference lookup found no match, so the connector "
            "omitted the field rather than blanking it",
            [row for row in rows if _cell(row, field_name).empty],
        )

    if cfg.assign_user and "assigned_to" in _field_names(rows):
        flag(
            "unassigned",
            "empty assigned_to with SNOW_ASSIGN_USER enabled: no sys_user matched the "
            "device's Entra user",
            [row for row in rows if _cell(row, "assigned_to").empty],
        )

    if cfg.install_status_active:
        flag(
            "unexpected_install_status",
            f"install_status is not SNOW_INSTALL_STATUS_ACTIVE={cfg.install_status_active!r}; "
            f"{cfg.retire_install_status!r} means the connector retired it",
            [
                row
                for row in rows
                if _cell(row, "install_status").value
                and _cell(row, "install_status").value != cfg.install_status_active
            ],
        )

    if correlation_field and correlation_field in _field_names(rows):
        flag(
            "missing_correlation",
            f"empty {correlation_field}: this CI carries no Intune device id, so it was "
            "matched on serial alone or predates the connector",
            [row for row in rows if _cell(row, correlation_field).empty],
        )

    findings.extend(_duplicate_findings(rows, "serial_number"))
    if correlation_field and correlation_field in _field_names(rows):
        findings.extend(_duplicate_findings(rows, correlation_field))

    return findings


def _duplicate_findings(rows: list[dict[str, Cell]], field_name: str) -> Iterator[Finding]:
    """Report values shared by more than one CI.

    A duplicated serial means IRE is one bad payload away from collapsing two
    machines into one record; a duplicated correlation id means it already
    happened in reverse and one Intune device owns two CIs.
    """
    if field_name not in _field_names(rows):
        return
    groups: dict[str, list[str]] = {}
    for row in rows:
        key = _cell(row, field_name).value
        if key:
            groups.setdefault(key.lower(), []).append(_cell(row, "sys_id").value)
    for key, sys_ids in sorted(groups.items()):
        if len(sys_ids) > 1:
            yield Finding(
                f"duplicate_{field_name}",
                f"{len(sys_ids)} CIs share {field_name}={key!r}",
                tuple(sys_ids),
            )


def _field_names(rows: list[dict[str, Cell]]) -> frozenset[str]:
    return frozenset(rows[0]) if rows else frozenset()


def _cell(row: dict[str, Cell], name: str) -> Cell:
    return row.get(name) or Cell("", "")
