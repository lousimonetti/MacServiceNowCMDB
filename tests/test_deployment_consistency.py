"""Guards against drift between the places that define Graph auth modes.

Three files independently decide which modes exist: `config.py` validates them,
`main.bicep` offers a subset for Azure Container Apps, and `deploy.sh` gates on
that same subset. Nothing makes them agree automatically, and the failure when
they disagree is a deployment that validates fine and then cannot authenticate
at runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

from intune_cmdb_sync.config import VALID_GRAPH_AUTH_MODES

REPO = Path(__file__).resolve().parent.parent
BICEP = REPO / "deploy" / "azure" / "main.bicep"
DEPLOY_SH = REPO / "deploy" / "azure" / "deploy.sh"

# Modes that are real but deliberately absent from the Azure deployment:
#   workload_identity  needs a projected federated token file, which AKS and
#                      GitHub Actions provide and Container Apps does not.
#   default            DefaultAzureCredential, a local-development convenience.
NOT_DEPLOYABLE_ON_CONTAINER_APPS = {"workload_identity", "default"}


def _bicep_allowed_modes() -> set[str]:
    text = BICEP.read_text()
    block = re.search(
        r"@allowed\(\[(.*?)\]\)\s*param graphAuthMode", text, re.DOTALL
    )
    assert block, "could not locate the graphAuthMode @allowed block in main.bicep"
    return set(re.findall(r"'([a-z_]+)'", block.group(1)))


def _deploy_sh_modes() -> set[str]:
    text = DEPLOY_SH.read_text()
    block = re.search(r'case "\$GRAPH_AUTH_MODE" in(.*?)\nesac', text, re.DOTALL)
    assert block, "could not locate the GRAPH_AUTH_MODE case statement in deploy.sh"
    # Case labels sit at the start of a line, two spaces in, ending in ')'.
    return set(re.findall(r"^  ([a-z_]+)\)", block.group(1), re.MULTILINE))


def test_bicep_offers_only_modes_the_connector_understands():
    unknown = _bicep_allowed_modes() - VALID_GRAPH_AUTH_MODES
    assert not unknown, (
        f"main.bicep offers Graph auth mode(s) {sorted(unknown)} that config.py "
        "would reject at startup"
    )


def test_deploy_script_accepts_exactly_what_bicep_offers():
    assert _deploy_sh_modes() == _bicep_allowed_modes(), (
        "deploy.sh and main.bicep disagree about which Graph auth modes are "
        "deployable. A mode in one but not the other is either an unreachable "
        "template branch or a script that fails after resources are created."
    )


def test_every_deployable_mode_is_reachable_from_azure():
    """Nothing offered by the Azure deployment may be a mode that cannot work
    there. This is the check that would have caught `workload_identity` being
    added to the template because it looked like the cross-tenant answer."""
    offered = _bicep_allowed_modes()
    impossible = offered & NOT_DEPLOYABLE_ON_CONTAINER_APPS
    assert not impossible, (
        f"main.bicep offers {sorted(impossible)}, which cannot authenticate on "
        "Container Apps. For secretless cross-tenant use federated_managed_identity."
    )


def test_single_tenant_production_path_is_offered():
    """The simplest production topology -- Intune and the subscription in one
    tenant -- must stay deployable with no Graph credential at all."""
    assert "managed_identity" in _bicep_allowed_modes()
    assert "managed_identity" in _deploy_sh_modes()
    assert "managed_identity" in VALID_GRAPH_AUTH_MODES


def test_deploy_script_refuses_managed_identity_across_tenants():
    """The guard that stops a same-tenant-only credential being used
    cross-tenant, where it would deploy cleanly and then 403 at runtime."""
    text = DEPLOY_SH.read_text()
    guard = re.search(
        r'managed_identity\)\s*\n\s*if \[\[ "\$GRAPH_TENANT_ID" != "\$SUBSCRIPTION_TENANT" \]\]',
        text,
    )
    assert guard, "deploy.sh no longer refuses managed_identity when tenants differ"
