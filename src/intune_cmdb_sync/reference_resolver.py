"""Resolve display names to sys_ids for CMDB reference fields.

`cmdb_ci_hardware.manufacturer` points at `core_company` and `model_id` points at
`cmdb_model`. Intune only gives us strings ("Apple", "MacBookPro18,3"), and IRE
will not resolve a display name into a reference on your behalf — an unresolved
reference is silently dropped or written as an invalid sys_id. So we look the
records up once per run and cache them; a fleet of ten thousand machines
typically has fewer than a dozen manufacturers and a few hundred models.

Creating missing records is opt-in. Auto-creating `cmdb_model` rows is convenient
for CMDB accuracy but pollutes Asset Management's model catalogue if the naming
does not match what asset managers expect, so the default is to leave the field
empty and report it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from .errors import ServiceNowError
from .servicenow.client import ServiceNowClient
from .snow_query import build_or_query, chunked, usable_values

log = logging.getLogger(__name__)

CORE_COMPANY_TABLE = "core_company"
CMDB_MODEL_TABLE = "cmdb_model"

LOOKUP_CHUNK_SIZE = 40


class ReferenceResolver:
    def __init__(
        self,
        client: ServiceNowClient,
        *,
        create_missing_manufacturers: bool = False,
        create_missing_models: bool = False,
        dry_run: bool = False,
    ) -> None:
        self._client = client
        self._create_manufacturers = create_missing_manufacturers
        self._create_models = create_missing_models
        self._dry_run = dry_run
        self._manufacturers: dict[str, str] = {}
        self._models: dict[str, str] = {}
        self._missing_manufacturers: set[str] = set()
        self._missing_models: set[str] = set()

    @property
    def unresolved(self) -> dict[str, list[str]]:
        return {
            "manufacturers": sorted(self._missing_manufacturers),
            "models": sorted(self._missing_models),
        }

    def prime(self, manufacturers: Iterable[str], models: Iterable[str]) -> None:
        """Bulk-load every manufacturer and model the run will need."""
        self._manufacturers.update(
            self._lookup(CORE_COMPANY_TABLE, usable_values(manufacturers))
        )
        self._models.update(self._lookup(CMDB_MODEL_TABLE, usable_values(models)))
        log.info(
            "primed CMDB reference caches",
            extra={
                "manufacturers_found": len(self._manufacturers),
                "models_found": len(self._models),
            },
        )

    def references_for(self, manufacturer: str | None, model: str | None) -> dict[str, str]:
        """Return {'manufacturer': sys_id, 'model_id': sys_id} for what resolved."""
        out: dict[str, str] = {}

        manufacturer_sys_id = self._resolve(
            manufacturer,
            cache=self._manufacturers,
            missing=self._missing_manufacturers,
            table=CORE_COMPANY_TABLE,
            create=self._create_manufacturers,
        )
        if manufacturer_sys_id:
            out["manufacturer"] = manufacturer_sys_id

        model_sys_id = self._resolve(
            model,
            cache=self._models,
            missing=self._missing_models,
            table=CMDB_MODEL_TABLE,
            create=self._create_models,
            extra_values={"manufacturer": manufacturer_sys_id} if manufacturer_sys_id else None,
        )
        if model_sys_id:
            out["model_id"] = model_sys_id

        return out

    def _resolve(
        self,
        name: str | None,
        *,
        cache: dict[str, str],
        missing: set[str],
        table: str,
        create: bool,
        extra_values: dict[str, str] | None = None,
    ) -> str | None:
        key = (name or "").strip()
        if not key:
            return None
        lowered = key.lower()
        if lowered in cache:
            return cache[lowered]
        if lowered in {m.lower() for m in missing}:
            return None

        if not create or self._dry_run:
            missing.add(key)
            return None

        sys_id = self._create_record(table, key, extra_values)
        if sys_id:
            cache[lowered] = sys_id
            return sys_id
        missing.add(key)
        return None

    def _lookup(self, table: str, names: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for chunk in chunked(names, LOOKUP_CHUNK_SIZE):
            rows = self._client.query_table(
                table,
                query=build_or_query("name", chunk),
                fields=("sys_id", "name"),
                limit=len(chunk) * 4,
            )
            for row in rows:
                row_name = str(row.get("name") or "").strip().lower()
                sys_id = str(row.get("sys_id") or "")
                if row_name and sys_id:
                    found.setdefault(row_name, sys_id)
        return found

    def _create_record(
        self, table: str, name: str, extra_values: dict[str, str] | None
    ) -> str | None:
        body = {"name": name, **(extra_values or {})}
        try:
            payload = self._client.request_json(
                "POST",
                f"/api/now/table/{table}",
                json_body=body,
                params={"sysparm_fields": "sys_id"},
                context=f"create {table}",
            )
        except ServiceNowError as exc:
            log.warning(
                "could not create reference record",
                extra={"table": table, "record_name": name, "error": str(exc)},
            )
            return None
        sys_id = ((payload or {}).get("result") or {}).get("sys_id")
        if sys_id:
            log.info("created reference record", extra={"table": table, "record_name": name})
        return str(sys_id) if sys_id else None
