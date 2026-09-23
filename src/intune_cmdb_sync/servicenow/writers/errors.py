"""Error-detail parsing shared by both writers.

Both `/api/now/identifyreconcile` and `/api/now/cmdb/instance/{class}` run
through the same identification engine, so a rejected write from either one
can carry the same IRE result envelope, and the same "unscoped api" gate
refusal is possible on either.
"""

from __future__ import annotations

from typing import Any

from ...config import ServiceNowConfig

# ServiceNow refuses an OAuth client that is not authorised for a global-scope
# API *before* it looks at roles or ACLs, and the response says only "User Not
# Authorized". Left unexplained that reads as a missing `itil` role, which is
# the one thing that cannot fix it.
_UNSCOPED_API_MARKER = "unscoped api"


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
