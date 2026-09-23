"""Registering and checking the `cmdb_ci.discovery_source` choice value.

Every write is rejected with `INVALID_INPUT_DATA` unless `SNOW_DISCOVERY_SOURCE`
exactly matches a registered choice value on `cmdb_ci.discovery_source`. See
docs/servicenow-setup.md section 5.
"""

from __future__ import annotations

from ...config import ServiceNowConfig
from ...errors import ServiceNowError
from ...http import describe_error
from ..client import TABLE_API, ServiceNowClient
from .errors import _unscoped_api_suffix

DISCOVERY_SOURCE_CHOICE = {"name": "cmdb_ci", "element": "discovery_source"}


def similar_discovery_sources(client: ServiceNowClient, cfg: ServiceNowConfig) -> list[str]:
    """Registered `cmdb_ci.discovery_source` values resembling the configured one.

    `valueLIKE` matches a substring case-insensitively, so "Intune" finds
    "Microsoft Intune" and vice versa. Deduplicated, because `sys_choice` holds
    one row per language, and filtered to active choices, because a disabled one
    is present without being usable.
    """
    rows = client.query_table(
        "sys_choice",
        query=(
            f"name={DISCOVERY_SOURCE_CHOICE['name']}"
            f"^element={DISCOVERY_SOURCE_CHOICE['element']}"
            f"^valueLIKE{cfg.discovery_source}"
        ),
        fields=("value", "inactive"),
        limit=100,
    )
    values: list[str] = []
    for row in rows:
        value = str(row.get("value") or "")
        if not value or str(row.get("inactive") or "").lower() in ("true", "1"):
            continue
        if value not in values:
            values.append(value)
    return values


def register_discovery_source(client: ServiceNowClient, cfg: ServiceNowConfig) -> str:
    """Create the `cmdb_ci.discovery_source` choice value this run needs.

    Adding a choice value is normally a ServiceNow admin action through System
    Definition > Choice Lists, and on an instance where the admin team is the
    bottleneck that is a wait for a single row. `POST /api/now/table/sys_choice`
    is the same operation, and `--check-api` reports whether this credential may
    call it -- the 2026-09-04 probe says it may.

    Deliberately behind its own CLI flag and never part of a sync: it writes to
    a configuration table, and a connector that quietly edited choice lists
    while syncing devices would be a much worse thing to own. Refusal on ACL
    grounds is a normal outcome, not a bug -- `sys_choice` writes usually want
    `admin` or `personalize_choices` -- so the failure path prints the exact
    record for someone who has it.
    """
    if cfg.discovery_source in similar_discovery_sources(client, cfg):
        return f"{cfg.discovery_source!r} is already a registered choice value; nothing to do"

    record = {
        **DISCOVERY_SOURCE_CHOICE,
        "value": cfg.discovery_source,
        "label": cfg.discovery_source,
        "inactive": "false",
        "language": "en",
    }
    response = client.request("POST", f"{TABLE_API}/sys_choice", json_body=record)

    if not response.is_success:
        detail = describe_error(response)
        raise ServiceNowError(
            f"could not create the choice value: {detail}"
            f"{_unscoped_api_suffix(detail, path=f'{TABLE_API}/sys_choice')}"
            " Writing sys_choice usually needs the 'admin' or 'personalize_choices' role, "
            "which an integration user does not normally carry. Ask a ServiceNow admin to "
            "create exactly this record (System Definition > Choice Lists > New): "
            + ", ".join(f"{k}={v!r}" for k, v in record.items())
        )

    # Read it back rather than trusting the 201: a business rule or data policy
    # can accept the insert and store something else, and the next thing that
    # happens is a device write that has to match this value exactly.
    confirmed = similar_discovery_sources(client, cfg)
    if cfg.discovery_source not in confirmed:
        raise ServiceNowError(
            f"the choice value was submitted but {cfg.discovery_source!r} is still not "
            f"readable on cmdb_ci.discovery_source (found: {confirmed or 'nothing similar'}). "
            "Check for a business rule or data policy on sys_choice before writing devices."
        )

    sys_id = ((response.json() or {}).get("result") or {}).get("sys_id")
    return (
        f"registered {cfg.discovery_source!r} as a choice value on "
        f"cmdb_ci.discovery_source (sys_choice sys_id={sys_id}); writes will now be accepted"
    )


def _discovery_source_caveats(client: ServiceNowClient, cfg: ServiceNowConfig) -> list[str]:
    """Check `SNOW_DISCOVERY_SOURCE` against the registered choice values.

    Queries for values *resembling* the configured one rather than listing the
    whole choice list. A stock instance carries 200+ discovery sources, so a
    dump of the first twenty is alphabetical noise ("ACC-Visibility",
    "AgentClientCollector", "Altiris"...) that answers nothing, and a bounded
    read cannot even prove absence -- a registered value past the row limit
    would be reported as missing.

    `valueLIKE` matches case-insensitively on a substring, so it finds
    "Microsoft Intune" for "Intune" and vice versa. That makes the three
    outcomes distinguishable: registered, registered under a different
    spelling, or genuinely absent.
    """
    try:
        candidates = similar_discovery_sources(client, cfg)
    except ServiceNowError as exc:
        return [
            f"could not read sys_choice to confirm SNOW_DISCOVERY_SOURCE="
            f"{cfg.discovery_source!r} is registered ({exc}); an unregistered value is "
            "rejected on every write"
        ]

    if cfg.discovery_source in candidates:
        return []

    # Every write will be rejected. That is known, not suspected, so it fails
    # the check rather than being filed as something to bear in mind.
    problem = (
        f"SNOW_DISCOVERY_SOURCE={cfg.discovery_source!r} is not a choice value on "
        "cmdb_ci.discovery_source, so every write will be rejected with "
        "INVALID_INPUT_DATA (docs/servicenow-setup.md section 5)."
    )
    case_only = [
        v for v in candidates if v.strip().lower() == cfg.discovery_source.strip().lower()
    ]
    if case_only:
        # The field matches exactly, so a case or spacing difference is a real
        # failure and an easy one to stare past -- and the only variant fixable
        # without a ServiceNow admin.
        raise ServiceNowError(
            f"{problem} {case_only[0]!r} IS registered and differs only by case or "
            "spacing — set SNOW_DISCOVERY_SOURCE to exactly that."
        )
    if candidates:
        raise ServiceNowError(
            f"{problem} These registered values resemble it: "
            + ", ".join(repr(v) for v in candidates[:10])
            + ". Use one of them, or have the configured value registered."
        )
    raise ServiceNowError(
        f"{problem} No registered value contains {cfg.discovery_source!r} either, so it "
        "needs adding under System Definition > Choice Lists (table cmdb_ci, element "
        "discovery_source) — a ServiceNow admin action."
    )
