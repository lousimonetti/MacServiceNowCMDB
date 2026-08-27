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
#   access_token       a hand-pasted, non-refreshable token. Deploying this on a
#                      schedule would produce a job that works until the token
#                      expires and then fails silently every night.
NOT_DEPLOYABLE_ON_CONTAINER_APPS = {"workload_identity", "default", "access_token"}


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


# ---------------------------------------------------------------------------
# Alerting
#
# A job that stops running is invisible: there is no failure to notice, just a
# CMDB that quietly goes stale. These assert the alarms stay deployed, and that
# the two settings which decide whether they can fire at all survive edits.
# ---------------------------------------------------------------------------

TERRAFORM = REPO / "deploy" / "aws" / "main.tf"


def test_azure_deploys_both_alert_rules():
    text = BICEP.read_text()
    assert "Microsoft.Insights/actionGroups" in text
    assert text.count("Microsoft.Insights/scheduledQueryRules") == 2, (
        "expected a no-successful-run rule and a device-errors rule"
    )
    assert "no-successful-run" in text and "device-errors" in text


def test_azure_absence_rule_can_actually_detect_absence():
    """`summarize` with no `by` returns a row of 0 when nothing matched. Add a
    `by` clause, or drop the summarize, and the query returns no rows at all --
    so the rule silently never fires."""
    text = BICEP.read_text()
    rule = text[text.index("no-successful-run"):text.index("device-errors")]
    summarize = [ln.strip() for ln in rule.splitlines() if ln.strip().startswith("| summarize")]
    assert summarize == ["| summarize completed = count()"], (
        f"the absence query's summarize must have no `by` clause, got {summarize}"
    )
    assert "'LessThan'" in rule and "threshold: 1" in rule


def test_aws_deploys_both_alarms():
    text = TERRAFORM.read_text()
    assert 'resource "aws_cloudwatch_metric_alarm" "no_successful_run"' in text
    assert 'resource "aws_cloudwatch_metric_alarm" "device_errors"' in text
    assert 'resource "aws_sns_topic" "alerts"' in text


def test_aws_absence_alarm_treats_missing_data_as_breaching():
    """The whole point of this alarm is the case where nothing was logged, which
    produces no datapoints. With the default handling it sits in
    INSUFFICIENT_DATA forever and never fires -- exactly when it is needed."""
    text = TERRAFORM.read_text()
    alarm = text[text.index('"aws_cloudwatch_metric_alarm" "no_successful_run"'):]
    alarm = alarm[: alarm.index("resource ", 10)]
    assert 'treat_missing_data = "breaching"' in alarm


def test_alerting_is_optional_but_not_accidentally_disabled():
    """Both templates gate alerting on an address being supplied, so a deploy
    without one is not silently unmonitored-looking-monitored."""
    assert "param alertEmail string = ''" in BICEP.read_text()
    assert 'variable "alert_email"' in TERRAFORM.read_text()
