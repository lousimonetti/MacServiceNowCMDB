"""Low-level ServiceNow REST client (Table API plus arbitrary endpoints)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from ..config import ServiceNowConfig
from ..errors import ServiceNowError
from ..http import RetryingClient, describe_error
from .auth import ServiceNowAuth

log = logging.getLogger(__name__)

TABLE_API = "/api/now/table"
CMDB_INSTANCE_API = "/api/now/cmdb/instance"

# ServiceNow caps a single Table API response; we page rather than assume.
DEFAULT_TABLE_PAGE_SIZE = 1000


class ServiceNowClient:
    def __init__(self, cfg: ServiceNowConfig, *, auth: ServiceNowAuth | None = None) -> None:
        self.cfg = cfg
        self.auth = auth or ServiceNowAuth(cfg)
        self._http = RetryingClient(
            base_url=cfg.base_url,
            token_provider=self.auth.token if self.auth.uses_bearer_token else None,
            auth=self.auth.basic_auth(),
            timeout=cfg.request_timeout,
            max_retries=cfg.max_retries,
            default_headers={"Content-Type": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ServiceNowClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- generic ---------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        return self._http.request(method, path, params=params, json_body=json_body)

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        context: str = "request",
    ) -> Any:
        response = self.request(method, path, params=params, json_body=json_body)
        if not response.is_success:
            raise ServiceNowError(f"{context} failed: {describe_error(response)}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ServiceNowError(
                f"{context} returned a non-JSON body (HTTP {response.status_code})"
            ) from exc

    # ---- Table API -------------------------------------------------------

    def query_table(
        self,
        table: str,
        *,
        query: str,
        fields: list[str] | tuple[str, ...],
        limit: int = DEFAULT_TABLE_PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        """Run one encoded query against a table and return raw (non-display) values."""
        params = {
            "sysparm_query": query,
            "sysparm_fields": ",".join(fields),
            "sysparm_limit": limit,
            "sysparm_display_value": "false",
            "sysparm_exclude_reference_link": "true",
        }
        payload = self.request_json(
            "GET", f"{TABLE_API}/{table}", params=params, context=f"query {table}"
        )
        result = (payload or {}).get("result") or []
        if not isinstance(result, list):
            raise ServiceNowError(f"query {table} returned an unexpected result shape")
        return result

    def update_record(self, table: str, sys_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        payload = self.request_json(
            "PATCH",
            f"{TABLE_API}/{table}/{sys_id}",
            json_body=dict(values),
            params={"sysparm_display_value": "false", "sysparm_exclude_reference_link": "true"},
            context=f"update {table}/{sys_id}",
        )
        return (payload or {}).get("result") or {}

    def get_record(
        self, table: str, sys_id: str, fields: list[str] | tuple[str, ...] | None = None
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "sysparm_display_value": "false",
            "sysparm_exclude_reference_link": "true",
        }
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        response = self.request("GET", f"{TABLE_API}/{table}/{sys_id}", params=params)
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise ServiceNowError(f"get {table}/{sys_id} failed: {describe_error(response)}")
        return (response.json() or {}).get("result")

    # ---- connectivity ----------------------------------------------------

    def verify_connectivity(self) -> dict[str, Any]:
        """Cheap round-trip that proves auth works and returns the instance identity."""
        payload = self.request_json(
            "GET",
            f"{TABLE_API}/sys_properties",
            params={
                "sysparm_query": "name=instance_name",
                "sysparm_fields": "name,value",
                "sysparm_limit": 1,
            },
            context="connectivity check",
        )
        result = (payload or {}).get("result") or []
        return {"instance": result[0].get("value") if result else None}
