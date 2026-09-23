"""Proving a write path is callable, without writing to it. Backs `--check`."""

from __future__ import annotations

from dataclasses import dataclass, field

from ...config import ServiceNowConfig
from ...errors import ServiceNowError
from ...http import describe_error
from ..client import CMDB_INSTANCE_API, ServiceNowClient
from ._constants import (
    IDENTIFY_RECONCILE_QUERY_API,
    NO_SUCH_API_MARKER,
    PROBE_CLASS,
    PROBE_SOURCE_KEY,
)
from .discovery_source import _discovery_source_caveats
from .errors import (
    _collect_item_errors,
    _log_context_id,
    _log_context_suffix,
    _unscoped_api_suffix,
    unscoped_api_refusal,
)


@dataclass
class WriteAccessCheck:
    """Outcome of proving the integration user can write, without writing."""

    verified: bool
    detail: str
    # Things this check could not establish. `verified` means "the write path
    # is callable and its prerequisites exist", which is not the same as "the
    # first run will succeed"; anything in that gap belongs here rather than
    # being folded into a pass or a fail.
    caveats: list[str] = field(default_factory=list)


def verify_write_access(client: ServiceNowClient, cfg: ServiceNowConfig) -> WriteAccessCheck:
    """Prove the IRE write path works, without creating anything.

    `--check` previously proved only that ServiceNow was reachable and readable,
    which leaves the two most common misconfigurations undetected until the
    first real run: an integration user without `itil`/`asset`, and a discovery
    source that was never registered as a choice value.

    This posts a synthetic item to `/api/now/identifyreconcile/query`, which runs
    identification and reports what *would* happen without committing anything.
    It is safe against production: the endpoint has no write path.

    Raises `ServiceNowError` when the write path is definitively broken. Returns
    an unverified result when the answer is genuinely unknown, which is not the
    same thing and must not be reported as success.
    """
    if cfg.write_mode == "cmdb_instance":
        return _verify_cmdb_instance_access(client, cfg)
    if cfg.write_mode != "identify_reconcile":
        return WriteAccessCheck(
            verified=False,
            detail=(
                f"SNOW_WRITE_MODE={cfg.write_mode} has no simulation endpoint, so write "
                "access cannot be checked without creating a CI. Verify it manually "
                "before the first run."
            ),
        )

    payload = {
        "items": [
            {
                "className": cfg.default_class or "cmdb_ci_computer",
                "values": {
                    "name": "intune-cmdb-sync-write-access-probe",
                    "serial_number": "INTUNE-CMDB-SYNC-PROBE",
                },
                "sys_object_source_info": {
                    "source_native_key": PROBE_SOURCE_KEY,
                    "source_name": cfg.discovery_source,
                },
            }
        ],
        "relations": [],
    }

    response = client.request(
        "POST",
        IDENTIFY_RECONCILE_QUERY_API,
        params={"sysparm_data_source": cfg.discovery_source},
        json_body=payload,
    )

    if response.status_code == 404:
        # Older releases predate the API. That is a real constraint, not a
        # permissions problem, and the fallback write mode still works.
        return WriteAccessCheck(
            verified=False,
            detail=(
                "this instance has no /api/now/identifyreconcile/query endpoint, so write "
                "access could not be simulated. If the release predates the IRE API, use "
                "SNOW_WRITE_MODE=cmdb_instance."
            ),
        )

    if response.status_code in (401, 403):
        detail = describe_error(response)
        if _unscoped_api_suffix(detail):
            raise ServiceNowError(
                f"the Identification and Reconciliation API is not available to these "
                f"credentials: {detail}."
                f"{_unscoped_api_suffix(detail, path=IDENTIFY_RECONCILE_QUERY_API)}"
            )
        raise ServiceNowError(
            f"the integration user cannot use the Identification and Reconciliation API: "
            f"{detail}. It needs the 'itil' or 'asset' role."
        )

    if not response.is_success:
        detail = describe_error(response)
        hint = ""
        if "data source" in detail.lower():
            # By far the most common first-run failure, and the message alone
            # does not say where the value has to be registered.
            hint = (
                f" SNOW_DISCOVERY_SOURCE={cfg.discovery_source!r} must exist as a choice "
                "value on cmdb_ci.discovery_source, matching exactly including case "
                "(docs/servicenow-setup.md section 5)."
            )
        raise ServiceNowError(
            f"IRE rejected a write-access probe: {detail}"
            f"{_log_context_suffix(_log_context_id(response))}{hint}"
        )

    result = (response.json() or {}).get("result") or {}
    items = result.get("items") or []
    errors = _collect_item_errors(items[0]) if items else []
    if errors:
        raise ServiceNowError(
            "IRE accepted the request but rejected the probe item: "
            + "; ".join(errors)
            + _log_context_suffix(result.get("logContextId"))
        )

    return WriteAccessCheck(
        verified=True,
        detail=(
            f"IRE simulated a write as {cfg.discovery_source!r} into "
            f"{cfg.default_class or 'cmdb_ci_computer'}; nothing was committed"
        ),
    )


def _verify_cmdb_instance_access(
    client: ServiceNowClient, cfg: ServiceNowConfig
) -> WriteAccessCheck:
    """Verify the CMDB Instance write path without creating a CI.

    This mode has no simulation endpoint, so it used to be reported as
    uncheckable. That was the right answer while it was a fallback nobody used.
    It is the wrong answer on an instance where the identifyreconcile API is
    refused at the OAuth gate and this one is not, because then it is the write
    path -- and "uncheckable" leaves the operator with a first run as their
    first test.

    Two things can be established without writing, and they are the two that
    fail first:

    * whether `POST /api/now/cmdb/instance/{class}` is callable at all, proven
      by posting to a class that cannot exist -- the request reaches the API,
      which rejects it on the class name, having already cleared the gate;
    * whether `SNOW_DISCOVERY_SOURCE` is a registered choice value, read from
      `sys_choice`.

    What cannot be established is whether the class's identification rules will
    accept the connector's attributes. That is a caveat, not a pass.
    """
    response = client.request("POST", f"{CMDB_INSTANCE_API}/{PROBE_CLASS}", json_body={})

    if response.status_code in (401, 403):
        detail = describe_error(response)
        if unscoped_api_refusal(detail):
            raise ServiceNowError(
                f"the CMDB Instance API is not available to these credentials: {detail}."
                f"{_unscoped_api_suffix(detail, path=f'{CMDB_INSTANCE_API}/{{className}}')}"
            )
        raise ServiceNowError(
            f"the integration user cannot write through the CMDB Instance API: {detail}. "
            "It needs the 'itil' or 'asset' role."
        )

    if response.status_code == 404 and NO_SUCH_API_MARKER in (response.text or "").lower():
        return WriteAccessCheck(
            verified=False,
            detail=(
                f"this instance has no {CMDB_INSTANCE_API} endpoint, so "
                "SNOW_WRITE_MODE=cmdb_instance cannot run here."
            ),
        )

    # Raises when the discovery source is positively absent: that is not a
    # caveat but a determined failure, and `--check` reporting "passed" while
    # knowing every write will be rejected is the exact thing this check exists
    # to prevent.
    caveats = _discovery_source_caveats(client, cfg)
    caveats.append(
        "the CMDB Instance API has no simulation endpoint, so whether the identification "
        f"rules for {cfg.default_class or 'cmdb_ci_computer'} accept these attributes is "
        "only knowable from a real write. Run with --limit 1 first."
    )
    if cfg.retire_missing:
        # Retirement is a Table API PATCH, a different API behind the same
        # gate. A run can therefore write CIs happily and then fail only at
        # retirement, which is the worst place to discover it.
        caveats.append(
            "SNOW_RETIRE_MISSING is on: retirement PATCHes the Table API, which is a "
            "separate API from this one and separately scoped. Confirm it with "
            "`intune-cmdb-sync --check-api` (the table_update row)."
        )
    if not (cfg.set_correlation and cfg.correlation_field):
        # Without sys_object_source_info there is nothing on the CI tying it to
        # the Intune device, so the correlation field is the only such link.
        caveats.append(
            "SNOW_SET_CORRELATION is off. This write mode cannot send "
            "sys_object_source_info, so with no correlation field there is nothing on "
            "the CI recording which Intune device it came from."
        )

    return WriteAccessCheck(
        verified=True,
        detail=(
            f"POST {CMDB_INSTANCE_API}/{{className}} is callable by these credentials "
            f"(probed with a class that does not exist: HTTP {response.status_code}); "
            "nothing was created"
        ),
        caveats=caveats,
    )
