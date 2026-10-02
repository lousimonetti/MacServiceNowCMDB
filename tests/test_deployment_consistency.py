"""Guards against drift between the places that define Graph auth modes.

Three files independently decide which modes exist: `config.py` validates them,
`main.bicep` offers a subset for Azure Functions, and `deploy.sh` gates on
that same subset. Nothing makes them agree automatically, and the failure when
they disagree is a deployment that validates fine and then cannot authenticate
at runtime.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from intune_cmdb_sync.config import VALID_GRAPH_AUTH_MODES

REPO = Path(__file__).resolve().parent.parent
BICEP = REPO / "deploy" / "azure" / "main.bicep"
DEPLOY_SH = REPO / "deploy" / "azure" / "deploy.sh"

# Modes that are real but deliberately absent from the Azure deployment:
#   workload_identity  needs a projected federated token file, which AKS and
#                      GitHub Actions provide and Azure Functions does not.
#   default            DefaultAzureCredential, a local-development convenience.
#   access_token       a hand-pasted, non-refreshable token. Deploying this on a
#                      schedule would produce a job that works until the token
#                      expires and then fails silently every night.
NOT_DEPLOYABLE_ON_AZURE = {"workload_identity", "default", "access_token"}


def _bicep_allowed_modes() -> set[str]:
    return _bicep_allowed_values("graphAuthMode")


def _bicep_allowed_values(param: str) -> set[str]:
    """The @allowed list directly above `param <name>`. Searching backwards from
    the param matters: a forward non-greedy match starts at the file's *first*
    @allowed and swallows every list in between."""
    text = BICEP.read_text()
    at = text.find(f"param {param} ")
    assert at != -1, f"main.bicep no longer declares {param}"
    return set(re.findall(r"'([a-z0-9_.]+)'", text[text.rfind("@allowed([", 0, at):at]))


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
    impossible = offered & NOT_DEPLOYABLE_ON_AZURE
    assert not impossible, (
        f"main.bicep offers {sorted(impossible)}, which cannot authenticate on "
        "Azure Functions. For secretless cross-tenant use federated_managed_identity."
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


def _deploy_sh_bicep_parameters() -> set[str]:
    text = DEPLOY_SH.read_text()
    block = re.search(r"--parameters \\\n(.*?)\n  --query", text, re.DOTALL)
    assert block, "could not locate the --parameters block in deploy.sh"
    return set(re.findall(r"^\s+([A-Za-z]+)=", block.group(1), re.MULTILINE))


def _bicep_params() -> set[str]:
    return set(re.findall(r"^param (\w+) ", BICEP.read_text(), re.MULTILINE))


def test_deploy_script_only_passes_parameters_the_template_declares():
    unknown = _deploy_sh_bicep_parameters() - _bicep_params()
    assert not unknown, f"deploy.sh passes {sorted(unknown)}, which main.bicep does not declare"


def test_discovery_source_is_settable_per_environment():
    """Each ServiceNow instance has its own cmdb_ci.discovery_source choice list,
    so a DEV and a PROD deploy may need different values. Unpassed, every deploy
    silently gets the template default and writes are rejected on any instance
    where that exact value is not registered."""
    assert "discoverySource" in _deploy_sh_bicep_parameters()
    assert 'discoverySource="${SNOW_DISCOVERY_SOURCE:-Intune}"' in DEPLOY_SH.read_text()


def test_storage_account_name_survives_a_hyphenated_prefix():
    """Multiple environments in one resource group use prefixes like
    intunecmdb-dev. Storage account names allow only lowercase letters and
    digits, so the prefix must be sanitised for that one resource or the deploy
    fails after everything else has been created."""
    text = BICEP.read_text()
    assert "var storageName = take('${storageSafePrefix}st${suffix}', 24)" in text
    safe = re.search(r"var storageSafePrefix = (.+)", text)
    assert safe, "storageSafePrefix is gone from main.bicep"
    assert "replace(" in safe.group(1) and "'-'" in safe.group(1)
    assert "toLower(" in safe.group(1)


def test_bicep_write_modes_are_ones_the_connector_accepts():
    """The write mode is per instance -- decided by that instance's OAuth auth
    scopes -- so a DEV and a PROD deploy may need different ones."""
    from intune_cmdb_sync.config import VALID_WRITE_MODES

    text = BICEP.read_text()
    param = text.find("param writeMode")
    assert param != -1, "main.bicep no longer offers a writeMode parameter"
    allowed = text[text.rfind("@allowed([", 0, param):param]
    offered = set(re.findall(r"'([a-z_]+)'", allowed))
    assert offered == set(VALID_WRITE_MODES)
    assert "SNOW_WRITE_MODE: writeMode" in text
    assert "writeMode" in _deploy_sh_bicep_parameters()


def test_azure_error_alert_also_fires_on_degraded_runs():
    """A degraded run (retirement guard tripped, state not saved) exits 4 but
    can report errors == 0. Alerting only on errors would miss exactly the runs
    that leave the next one unable to reason about the fleet."""
    text = BICEP.read_text()
    rule = text[text.index("-device-errors"):]
    assert "array_length(p.degraded)" in rule
    assert "degraded > 0" in rule


def test_no_interpolation_inside_bicep_multiline_strings():
    """Bicep does not interpolate inside ''' strings: `${jobName}` there reaches
    Azure as literal text. Both alert queries shipped that way, so the absence
    rule matched no job and fired every hour while the error rule never fired.
    Use format() instead."""
    blocks = re.findall(r"'''(.*?)'''", BICEP.read_text(), re.DOTALL)
    offenders = [b.strip().splitlines()[0] for b in blocks if "${" in b]
    assert not offenders, f"interpolation inside ''' strings is sent literally: {offenders}"


# ---------------------------------------------------------------------------
# Safety of redeploys, and parity with the locally tested configuration
# ---------------------------------------------------------------------------


def test_deploy_script_has_no_dry_run_default():
    """A default of false turned every redeploy that forgot DRY_RUN=true live;
    a default of true would leave production silently committing nothing. The
    script must demand the value instead."""
    text = DEPLOY_SH.read_text()
    assert "require_bool DRY_RUN" in text
    assert 'dryRun="$DRY_RUN"' in text
    assert "${DRY_RUN:-" not in text


def test_deploy_script_accepts_only_literal_booleans():
    """az passes any bool parameter value other than 'true' as false, so
    DRY_RUN=yes would otherwise deploy a live job without complaint."""
    text = DEPLOY_SH.read_text()
    helper = text[text.index("require_bool() {"):]
    helper = helper[: helper.index("\n}\n")]
    assert "true|false) ;;" in helper
    assert "require_bool RETIRE_MISSING" in text


def test_class_map_and_mapping_overrides_reach_the_job():
    """The app gets only what main.bicep sets. Without these, the class map and
    the last_discovered drop tested locally silently do not apply in Azure."""
    text = BICEP.read_text()
    assert "param classMap string = ''" in text
    assert "param mappingOverrides object = {}" in text
    assert "{ SNOW_CLASS_MAP: classMap }" in text
    assert "{ MAPPING_OVERRIDES_JSON: string(mappingOverrides) }" in text
    assert "mappingEnv" in text[text.index("properties: union("):]
    assert {"classMap", "mappingOverrides"} <= _deploy_sh_bicep_parameters()
    assert 'classMap="${SNOW_CLASS_MAP:-}"' in DEPLOY_SH.read_text()


def test_empty_class_map_is_omitted_not_blanked():
    """SNOW_CLASS_MAP replaces the built-in map. Setting it to '' on the app
    would be at best a no-op; omitting it keeps the default unambiguous."""
    assert "empty(classMap) ? {} :" in BICEP.read_text()


# ---------------------------------------------------------------------------
# Azure Functions host
#
# Each of these is a property whose failure is silent: the deploy succeeds and
# then nothing runs, the alerts go blind, or two runs race on state.json.
# ---------------------------------------------------------------------------

FUNCTIONS = REPO / "deploy" / "azure" / "functions"
FUNCTION_APP = FUNCTIONS / "function_app.py"
HOST_JSON = FUNCTIONS / "host.json"


def test_no_container_registry_or_container_apps_anywhere_in_the_azure_stack():
    """The landing-zone policy denies both. The stack deploys code as a zip
    package into its own storage account; nothing may reintroduce an image."""
    for path in (BICEP, DEPLOY_SH):
        text = path.read_text()
        for denied in ("Microsoft.ContainerRegistry", "Microsoft.App/", "az acr", "containerapp"):
            assert denied not in text, f"{path.name} references {denied}"
    assert "'FlexConsumption'" in BICEP.read_text()
    assert "Microsoft.Web/sites@" in BICEP.read_text()


def test_timer_schedule_comes_from_the_app_setting_bicep_sets():
    """function_app.py reads %SYNC_SCHEDULE%. If the setting is missing or
    renamed, the host fails to index the function and nothing ever runs."""
    assert 'schedule="%SYNC_SCHEDULE%"' in FUNCTION_APP.read_text()
    assert "SYNC_SCHEDULE: schedule" in BICEP.read_text()
    assert 'schedule="$SCHEDULE"' in DEPLOY_SH.read_text()


def test_default_schedules_agree_and_have_six_fields():
    """NCRONTAB has a seconds field; a five-field cron is rejected by the
    timer at startup, after the deploy has reported success."""
    bicep = re.search(r"param schedule string = '([^']+)'", BICEP.read_text())
    script = re.search(r'SCHEDULE="\$\{SCHEDULE:-([^}]+)\}"', DEPLOY_SH.read_text())
    assert bicep and script
    assert bicep.group(1) == script.group(1)
    assert len(bicep.group(1).split()) == 6


def test_timer_does_not_run_on_startup():
    """run_on_startup=True fires on every cold start and every deploy: an
    unscheduled live sync each time the platform moves the app."""
    text = FUNCTION_APP.read_text()
    assert "run_on_startup=False" in text
    assert "run_on_startup=True" not in text


def test_deploy_script_checks_the_function_it_actually_deploys():
    """deploy.sh fails the deploy if the function did not register; the name it
    looks for must be the one function_app.py defines."""
    name = re.search(r'FUNCTION_NAME="([^"]+)"', DEPLOY_SH.read_text())
    assert name, "FUNCTION_NAME is gone from deploy.sh"
    assert f"def {name.group(1)}(" in FUNCTION_APP.read_text()
    assert "az functionapp function list" in DEPLOY_SH.read_text()


def test_at_most_one_instance_runs():
    """Two concurrent runs would race on state.json, the Functions twin of the
    Lambda's reserved concurrency of 1."""
    assert "maximumInstanceCount: 1" in BICEP.read_text()


def test_trace_sampling_is_off():
    """Application Insights sampling can drop the one `run complete` trace a
    day, and the absence alert then reports a run that happened as missing."""
    host = json.loads(HOST_JSON.read_text())
    assert host["logging"]["applicationInsights"]["samplingSettings"]["isEnabled"] is False


def test_function_timeout_covers_a_large_tenant():
    """Same 30-minute ceiling the Container Apps job had. Flex Consumption's
    default is also 30, but an explicit value survives a platform change."""
    host = json.loads(HOST_JSON.read_text())
    assert host["functionTimeout"] == "00:30:00"


def test_python_versions_agree_between_template_and_package_build():
    """deploy.sh builds wheels for one Python ABI; the runtime must be the same
    one, or compiled dependencies (cryptography) fail to import."""
    offered = _bicep_allowed_values("pythonVersion")
    case = re.search(r'case "\$PYTHON_VERSION" in\n\s+([0-9.|]+)\)', DEPLOY_SH.read_text())
    assert case, "deploy.sh no longer validates PYTHON_VERSION"
    assert set(case.group(1).split("|")) == offered
    assert 'pythonVersion="$PYTHON_VERSION"' in DEPLOY_SH.read_text()
    assert '--python-version "$PYTHON_VERSION"' in DEPLOY_SH.read_text()


def test_package_is_built_for_linux_whatever_the_build_machine():
    """Without --platform, a Mac packages macOS wheels and the function fails
    to import on its first run."""
    text = DEPLOY_SH.read_text()
    assert "--platform manylinux" in text
    assert "--only-binary=:all:" in text
    assert '"${WHEEL}[azure]"' in text, "the azure extra carries azure-functions"
    assert "--build-remote false" in text


def test_secrets_reach_the_app_as_key_vault_references_only():
    """A secret parameter written into app settings as a literal is readable by
    anyone with read access to the app's configuration."""
    text = BICEP.read_text()
    settings = text[text.index("var graphEnvCommon"):text.index("resource appSettings")]
    assert "serviceNowClientSecret" not in settings
    assert "graphClientSecret" not in settings
    assert "SNOW_CLIENT_SECRET: snowSecretRef" in settings
    assert "@Microsoft.KeyVault(SecretUri=${serviceNowSecret.properties.secretUri})" in text
    assert "keyVaultReferenceIdentity: identity.id" in text


def test_state_lives_on_the_mounted_share():
    """state.json must outlive the instance, or retirement silently stops."""
    text = BICEP.read_text()
    mount = re.search(r"var stateMountPath = '([^']+)'", text)
    assert mount
    assert "STATE_PATH: '${stateMountPath}/state.json'" in text
    assert "mountPath: stateMountPath" in text
    assert "type: 'AzureFiles'" in text


def test_deploy_script_uses_an_existing_resource_group_and_never_creates_one():
    """Landing-zone resource groups are pre-provisioned (DEV is
    azc-obm-development). Creating one may be refused by policy, and a defaulted
    name would deploy a stack into a group nobody looks at."""
    text = DEPLOY_SH.read_text()
    assert 'RESOURCE_GROUP="${RESOURCE_GROUP:-}"' in text, "no default group, ever"
    assert "az group create" not in text
    assert "az group delete" not in text
    assert 'az group show --name "$RESOURCE_GROUP"' in text


def test_resource_group_is_picked_from_a_list_only_when_interactive():
    """Unset RESOURCE_GROUP lists the subscription's groups and asks. A
    non-interactive run must fail rather than hang on the prompt or guess."""
    text = DEPLOY_SH.read_text()
    picker = text[text.index("pick_resource_group() {"):]
    picker = picker[: picker.index("\n}\n")]
    assert "[[ -t 0 ]] || die" in picker
    assert "az group list" in picker
    assert '[[ -n "$RESOURCE_GROUP" ]] || pick_resource_group' in text
    # The picked group goes through the same existence check as a named one.
    exists_check = text.index('az group show --name "$RESOURCE_GROUP"')
    assert text.index("|| pick_resource_group") < exists_check


def test_resources_deploy_to_east_us_by_default():
    """East US by requirement, whatever region the chosen group is in."""
    text = DEPLOY_SH.read_text()
    assert 'LOCATION="${LOCATION:-eastus}"' in text
    assert 'location="$LOCATION"' in text
    assert "list-flexconsumption-locations" in text


def test_docs_never_suggest_deleting_the_shared_resource_group():
    readme = (REPO / "deploy" / "azure" / "README.md").read_text()
    assert "az group delete --name" not in readme


def test_deploy_script_survives_being_run_with_sh():
    """`sh deploy.sh` is a natural way to run it, and a POSIX shell rejects
    bash syntax with a bare "syntax error near unexpected token" (it happened
    with `done < <(...)`). The script re-runs itself under bash first, and that
    guard must come before any bash-only line."""
    text = DEPLOY_SH.read_text()
    guard = text.index('[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"')
    assert '*:posix:*) exec bash "$0" "$@" ;;' in text, "macOS sh is bash in POSIX mode"
    assert guard < text.index("set -euo pipefail")
    assert guard < text.index("[[")
    # Process substitution fails to parse even in bash's own POSIX mode.
    assert "< <(" not in text


# ---------------------------------------------------------------------------
# TLS-inspecting proxy (Zscaler)
# ---------------------------------------------------------------------------


def _before_first_az_call(text: str, needle: str) -> bool:
    first_az = text.index("$(az ")
    return text.index(needle) < first_az


def test_bicep_version_check_is_skipped_before_any_az_call():
    """The check is the first request to fail behind TLS inspection, and it is
    only a notice. The environment form keeps it to this run."""
    text = DEPLOY_SH.read_text()
    assert "export AZURE_BICEP_CHECK_VERSION=false" in text
    assert _before_first_az_call(text, "export AZURE_BICEP_CHECK_VERSION=false")


def test_ca_bundle_reaches_az_pip_and_python_before_any_az_call():
    text = DEPLOY_SH.read_text()
    for var in ("REQUESTS_CA_BUNDLE", "PIP_CERT", "SSL_CERT_FILE"):
        line = f'export {var}="${{BUILD_DIR}}/ca-bundle.pem"'
        assert line in text, f"{var} is not pointed at the combined bundle"
        assert _before_first_az_call(text, line)
    # The temp dir the bundle lives in must exist by then too.
    assert _before_first_az_call(text, 'BUILD_DIR="$(mktemp -d)"')


def test_combined_bundle_keeps_the_public_roots():
    """These variables REPLACE a tool's default bundle. The proxy root alone
    would break every site the proxy does not inspect."""
    text = DEPLOY_SH.read_text()
    assert "certifi.where()" in text
    combine = '{ cat "$PUBLIC_ROOTS"; echo; cat "$EXTRA_ROOTS"; }'
    assert f'{combine} > "${{BUILD_DIR}}/ca-bundle.pem"' in text
    assert "security find-certificate -a -p /Library/Keychains/System.keychain" in text


def test_tls_preflight_runs_before_any_az_call():
    text = DEPLOY_SH.read_text()
    assert _before_first_az_call(text, "TLS_PROBE=$(")
    assert "ssl.SSLCertVerificationError" in text


def test_certificate_verification_is_never_disabled():
    for path in (DEPLOY_SH, REPO / ".github" / "workflows" / "ci.yml"):
        text = path.read_text()
        assert "AZURE_CLI_DISABLE_CONNECTION_VERIFICATION=1" not in text
        assert "--trusted-host" not in text
        assert "CERT_NONE" not in text
