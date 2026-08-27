"""Map the Entra ID user on an Intune device to a ServiceNow `sys_user` record.

Intune tells us who a device belongs to as an Entra object ID plus a UPN, email,
and display name. The CMDB needs a `sys_user` sys_id for `assigned_to`. There is
no universal join key between the two directories, so this module tries a
configurable ordered list of candidate keys and stops at the first unambiguous
hit:

  employee_number  sys_user.employee_number == Entra `employeeId`
                   The strongest key when HR data feeds both systems: it survives
                   name changes, domain migrations, and mailbox moves.
  email            sys_user.email == Entra `mail` (falling back to the UPN)
  user_name        sys_user.user_name == UPN, then the UPN local part
  entra_id         a customer-defined sys_user field holding the Entra object ID

A key that matches more than one `sys_user` is treated as no match: silently
assigning a device to the wrong person is worse than leaving `assigned_to` empty.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from .config import ServiceNowConfig
from .models import EntraUser, SysUserRef
from .servicenow.client import ServiceNowClient
from .snow_query import build_or_query, chunked, is_query_safe

log = logging.getLogger(__name__)

SYS_USER_TABLE = "sys_user"

# Encoded queries travel in the URL, so keep each `IN` list short enough that the
# request stays comfortably under typical proxy/instance URL limits.
LOOKUP_CHUNK_SIZE = 50

BASE_USER_FIELDS = ("sys_id", "user_name", "email", "employee_number", "active")


class UserResolver:
    """Resolves Entra users to sys_user records, with an in-process cache."""

    def __init__(self, client: ServiceNowClient, cfg: ServiceNowConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._cache: dict[str, SysUserRef | None] = {}

    @property
    def _fields(self) -> tuple[str, ...]:
        extra = (self._cfg.user_entra_id_field,) if self._cfg.user_entra_id_field else ()
        return tuple(dict.fromkeys(BASE_USER_FIELDS + extra))

    def resolve_many(self, users: Iterable[EntraUser]) -> dict[str, SysUserRef | None]:
        """Resolve a batch of Entra users, keyed by Entra object ID."""
        pending = [u for u in users if u.object_id and u.object_id not in self._cache]
        unresolved = {u.object_id: u for u in pending}

        for key in self._cfg.user_match_order:
            if not unresolved:
                break
            matched = self._resolve_by_key(key, list(unresolved.values()))
            for object_id, ref in matched.items():
                self._cache[object_id] = ref
                unresolved.pop(object_id, None)

        for object_id in unresolved:
            self._cache[object_id] = None

        return {u.object_id: self._cache.get(u.object_id) for u in users if u.object_id}

    def _resolve_by_key(
        self, key: str, users: Sequence[EntraUser]
    ) -> dict[str, SysUserRef]:
        candidates = self._candidate_values(key, users)
        if not candidates:
            return {}

        field = self._snow_field_for(key)
        if field is None:
            return {}

        # Query with the value as it appears in Entra. Most ServiceNow instances
        # collate case-insensitively, but Oracle-backed ones do not, so sending a
        # lowercased value would silently stop matching there.
        rows = self._query_users(field, [original for original, _ in candidates.values()])

        # Group by lowercased field value so we can detect ambiguity.
        by_value: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            value = str(row.get(field) or "").strip().lower()
            if value:
                by_value.setdefault(value, []).append(row)

        matched: dict[str, SysUserRef] = {}
        for value, (_original, object_ids) in candidates.items():
            rows_for_value = by_value.get(value, [])
            if not rows_for_value:
                continue
            if len(rows_for_value) > 1:
                log.warning(
                    "ambiguous sys_user match; leaving assigned_to empty",
                    extra={"match_key": key, "field": field, "candidates": len(rows_for_value)},
                )
                continue
            row = rows_for_value[0]
            ref = SysUserRef(
                sys_id=str(row.get("sys_id") or ""),
                user_name=row.get("user_name"),
                email=row.get("email"),
                employee_number=row.get("employee_number"),
                matched_on=key,
            )
            if not ref.sys_id:
                continue
            for object_id in object_ids:
                matched.setdefault(object_id, ref)
        return matched

    def _snow_field_for(self, key: str) -> str | None:
        if key == "employee_number":
            return "employee_number"
        if key == "email":
            return "email"
        if key == "user_name":
            return "user_name"
        if key == "entra_id":
            return self._cfg.user_entra_id_field
        log.warning("ignoring unknown user match key", extra={"match_key": key})
        return None

    def _candidate_values(
        self, key: str, users: Sequence[EntraUser]
    ) -> dict[str, tuple[str, list[str]]]:
        """Build {lowercased value -> (original value, [entra object ids])}.

        The lowercased key groups Entra users and ServiceNow rows that differ only
        in case; the original value is what actually goes into the query.
        """
        candidates: dict[str, tuple[str, list[str]]] = {}
        for user in users:
            for raw in self._values_for(key, user):
                value = (raw or "").strip()
                if not is_query_safe(value):
                    if value:
                        log.debug(
                            "skipping lookup value that cannot be encoded safely",
                            extra={"match_key": key},
                        )
                    continue
                _original, object_ids = candidates.setdefault(value.lower(), (value, []))
                object_ids.append(user.object_id)
        return candidates

    @staticmethod
    def _values_for(key: str, user: EntraUser) -> list[str | None]:
        if key == "employee_number":
            return [user.employee_id]
        if key == "email":
            return [user.primary_email]
        if key == "user_name":
            upn = user.user_principal_name
            local_part = upn.split("@", 1)[0] if upn and "@" in upn else None
            return [upn, local_part]
        if key == "entra_id":
            return [user.object_id]
        return []

    def _query_users(self, field: str, values: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        and_clause = "active=true" if self._cfg.user_active_only else None
        for chunk in chunked(values, LOOKUP_CHUNK_SIZE):
            rows.extend(
                self._client.query_table(
                    SYS_USER_TABLE,
                    query=build_or_query(field, chunk, and_clause=and_clause),
                    fields=self._fields,
                    limit=len(chunk) * 4,
                )
            )
        return rows
