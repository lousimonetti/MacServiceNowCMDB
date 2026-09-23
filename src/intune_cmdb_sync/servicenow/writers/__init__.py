"""The two CMDB write paths.

`identify_reconcile` (default, recommended)
    `POST /api/now/identifyreconcile` — the base-platform Identification and
    Reconciliation API. It accepts a bulk `items` array in a single request and
    carries `sys_object_source_info`, which lets IRE key each CI on the Intune
    `managedDevice.id` (`source_native_key`). That is what makes the sync stable
    across motherboard swaps, serial-number corrections, and device renames.
    Requires the `itil` or `asset` role. No plugin purchase, no Service Graph
    Connector subscription. See `ire.py`.

`cmdb_instance`
    `POST /api/now/cmdb/instance/{className}` — one HTTP call per CI. Still runs
    through IRE, but the documented request body has no slot for
    `sys_object_source_info`, so identification falls back to the class's
    identifier rules (serial number, then name). Use it only where the
    identifyreconcile endpoint is blocked. See `cmdb_instance.py`.

Write-access verification (`--check`) lives in `access.py`, discovery-source
registration (`--register-discovery-source`) in `discovery_source.py`, and
error-detail parsing shared by both writers in `errors.py`.
"""

from __future__ import annotations

from ...config import ServiceNowConfig
from ..client import ServiceNowClient
from ._constants import (
    IDENTIFY_RECONCILE_API,
    IDENTIFY_RECONCILE_ENHANCED_API,
    IDENTIFY_RECONCILE_QUERY_API,
    NO_SUCH_API_MARKER,
    PROBE_CLASS,
    PROBE_SOURCE_KEY,
)
from .access import WriteAccessCheck, verify_write_access
from .cmdb_instance import CmdbInstanceWriter, stringify_attributes
from .discovery_source import (
    DISCOVERY_SOURCE_CHOICE,
    register_discovery_source,
    similar_discovery_sources,
)
from .errors import unscoped_api_refusal
from .ire import CiPayload, IdentifyReconcileWriter, Writer, WriteResult

__all__ = [
    "DISCOVERY_SOURCE_CHOICE",
    "IDENTIFY_RECONCILE_API",
    "IDENTIFY_RECONCILE_ENHANCED_API",
    "IDENTIFY_RECONCILE_QUERY_API",
    "NO_SUCH_API_MARKER",
    "PROBE_CLASS",
    "PROBE_SOURCE_KEY",
    "CiPayload",
    "CmdbInstanceWriter",
    "IdentifyReconcileWriter",
    "WriteAccessCheck",
    "WriteResult",
    "Writer",
    "build_writer",
    "register_discovery_source",
    "similar_discovery_sources",
    "stringify_attributes",
    "unscoped_api_refusal",
    "verify_write_access",
]


def build_writer(
    client: ServiceNowClient, cfg: ServiceNowConfig, *, dry_run: bool = False
) -> Writer:
    if cfg.write_mode == "cmdb_instance":
        return CmdbInstanceWriter(client, cfg, dry_run=dry_run)
    return IdentifyReconcileWriter(client, cfg, dry_run=dry_run)
