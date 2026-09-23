"""Constants shared across the write-path submodules and `servicenow/probe.py`."""

from __future__ import annotations

IDENTIFY_RECONCILE_API = "/api/now/identifyreconcile"
IDENTIFY_RECONCILE_ENHANCED_API = "/api/now/identifyreconcile/enhanced"
IDENTIFY_RECONCILE_QUERY_API = "/api/now/identifyreconcile/query"

# Source key for the write-access probe. Deliberately unlike any Intune device
# GUID, so that even if a future change sent it to a committing endpoint it
# could not collide with a real CI.
PROBE_SOURCE_KEY = "intune-cmdb-sync:write-access-probe"

# A class name that cannot exist. POSTing to it exercises the CMDB Instance API
# with nowhere to write, which is how that endpoint's availability gets proven
# without creating a CI. Shared with probe.py.
PROBE_CLASS = "cmdb_ci_intune_cmdb_sync_probe_no_such_class"

# ServiceNow's body for a URI that routes to no REST API at all. It is the only
# thing separating "this API does not exist" from "this API answered and the
# class you named does not exist" -- both come back 404.
NO_SUCH_API_MARKER = "does not represent any resource"
