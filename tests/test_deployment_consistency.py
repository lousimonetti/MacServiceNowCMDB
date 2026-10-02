"""Guards against drift between the places that define Graph auth modes.

Three files independently decide which modes exist: `config.py` validates them,
`main.bicep` offers a subset for Azure App Service, and `deploy.sh` gates on
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
#                      GitHub Actions provide and App Service does not.
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
        "Azure App Service. For secretless cross-tenant use federated_managed_identity."
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


# ---------------------------------------------------------------------------
# App Service + scheduled WebJob host
#
# Each of these is a property whose failure is silent: the deploy succeeds and
# then nothing runs, the alerts go blind, or a policy refuses the next deploy.
# ---------------------------------------------------------------------------

WEBJOB_RUN = REPO / "deploy" / "azure" / "webjob" / "run.py"


def _resource_block(text: str, declaration: str) -> str:
    start = text.index(declaration)
    end = text.find("\nresource ", start + 1)
    return text[start:end if end != -1 else None]


def test_template_creates_nothing_the_landing_zone_refuses():
    """ACR and Container Apps are denied outright. Key Vault and Storage are
    allowed only with public access denied, which would force a VNet; avoiding
    both is the reason this host was chosen."""
    text = BICEP.read_text()
    for refused in (
        "Microsoft.ContainerRegistry",
        "Microsoft.App/",
        "Microsoft.KeyVault",
        "Microsoft.Storage",
    ):
        assert refused not in text, f"main.bicep creates {refused}"
    # The one network resource is the app's inbound private endpoint; no VNet
    # integration, subnets, DNS zones or NAT.
    network_types = set(re.findall(r"'(Microsoft\.Network/[A-Za-z/]+)@", text))
    assert network_types == {"Microsoft.Network/privateEndpoints"}, network_types
    for refused in ("az acr", "containerapp", "functionapp"):
        assert refused not in DEPLOY_SH.read_text()


def test_app_service_runs_the_webjob_reliably():
    """Without Always On a scheduled WebJob stops firing when the app idles,
    which needs Basic or above; without the idle timeout a triggered job is
    killed after two quiet minutes."""
    text = BICEP.read_text()
    site = _resource_block(text, "resource webApp 'Microsoft.Web/sites@")
    assert "alwaysOn: true" in site
    assert "linuxFxVersion: 'PYTHON|3.12'" in site
    assert "name: 'B1'" in _resource_block(text, "resource plan 'Microsoft.Web/serverfarms@")
    timeout = re.search(r"WEBJOBS_IDLE_TIMEOUT: '(\d+)'", text)
    assert timeout and int(timeout.group(1)) >= 1800
    assert "WEBSITE_SKIP_RUNNING_KUDUAGENT: 'false'" in text


def test_site_is_hardened():
    text = BICEP.read_text()
    site = _resource_block(text, "resource webApp 'Microsoft.Web/sites@")
    assert "httpsOnly: true" in site
    assert "minTlsVersion: '1.2'" in site
    assert "ftpsState: 'Disabled'" in site
    assert "remoteDebuggingEnabled: false" in site
    assert text.count("properties: { allow: false }") == 2, "basic-auth publishing must be off"


def test_python_version_agrees_between_app_and_package_build():
    """deploy.sh builds wheels for one Python ABI; the app must run the same
    one, or compiled dependencies (cryptography) fail to import."""
    version = re.search(r"linuxFxVersion: 'PYTHON\|([0-9.]+)'", BICEP.read_text())
    assert version
    assert f'PYTHON_VERSION="{version.group(1)}"' in DEPLOY_SH.read_text()


def test_package_is_built_for_linux_whatever_the_build_machine():
    """Without --platform, a Mac packages macOS wheels and the WebJob fails to
    import on its first run."""
    text = DEPLOY_SH.read_text()
    assert "--platform manylinux" in text
    assert "--only-binary=:all:" in text
    assert '"${WHEEL}[appservice]"' in text, "the appservice extra carries the telemetry"
    assert "SCM_DO_BUILD_DURING_DEPLOYMENT: 'false'" in BICEP.read_text()


def test_webjob_layout_matches_what_the_script_verifies():
    """A job outside App_Data/jobs/triggered/<name>/ is never registered, and
    then nothing runs and nothing errors."""
    text = DEPLOY_SH.read_text()
    assert re.search(r'WEBJOB_NAME="([^"]+)"', text), "WEBJOB_NAME is gone from deploy.sh"
    assert 'JOB_DIR="${PACKAGE_DIR}/App_Data/jobs/triggered/${WEBJOB_NAME}"' in text
    assert 'cp "$(dirname "$0")/webjob/run.py" "$JOB_DIR/"' in text
    assert "kudu GET /api/triggeredwebjobs" in text


def test_webjob_package_path_matches_the_install_target():
    """run.py adds wwwroot/packages to sys.path; the build must install there."""
    assert '"site", "wwwroot", "packages"' in WEBJOB_RUN.read_text()
    assert '--target "${PACKAGE_DIR}/packages"' in DEPLOY_SH.read_text()


def test_webjob_run_exits_with_the_sync_exit_code():
    """A failed sync must be a failed WebJob run in the job history."""
    assert "sys.exit(run())" in WEBJOB_RUN.read_text()


def test_schedule_is_six_fields_and_reaches_settings_job():
    """NCRONTAB has a seconds field; a five-field cron is rejected by the
    scheduler only after the deploy has reported success."""
    text = DEPLOY_SH.read_text()
    default = re.search(r'SCHEDULE="\$\{SCHEDULE:-([^}]+)\}"', text)
    assert default and len(default.group(1).split()) == 6
    assert "{schedule: $schedule, is_singleton: true}" in text
    assert '"${JOB_DIR}/settings.job"' in text


def test_state_and_report_live_on_persistent_home():
    """/home survives restarts and redeploys; anything else on App Service does
    not, and a lost state.json silently stops retirement."""
    text = BICEP.read_text()
    assert "var dataDir = '/home/" in text
    assert "STATE_PATH: '${dataDir}/state.json'" in text
    assert "RUN_REPORT_PATH: '${dataDir}/run-report.json'" in text
    assert "WEBSITES_ENABLE_APP_SERVICE_STORAGE: 'true'" in text


def test_alert_queries_match_the_name_telemetry_carries():
    """OTEL_SERVICE_NAME becomes AppRoleName on every trace. If it and the
    alert filter disagree, the absence alert fires every hour and the error
    alert never fires."""
    text = BICEP.read_text()
    assert "OTEL_SERVICE_NAME: webAppName" in text
    assert text.count("| where AppRoleName == '{0}'") == 2
    assert text.count("''', webAppName)") == 2
    assert text.count("| extend p = parse_json(Message)") == 2


def test_telemetry_publishes_with_the_identity():
    """Application Insights keeps local auth off; the job authenticates as the
    identity, which needs Monitoring Metrics Publisher before settings land."""
    text = BICEP.read_text()
    assert "DisableLocalAuth: true" in text
    assert "AZURE_CLIENT_ID: identity.properties.clientId" in text
    assert "3913510d-42f4-4e42-8a64-420c390055eb" in text
    assert "appInsightsPublisher" in _resource_block(text, "resource appSettings ")


# ---------------------------------------------------------------------------
# vpcx-lzn-app-service-deny-public-network: inbound only through a private
# endpoint, and deploys that use it without depending on DNS.
# ---------------------------------------------------------------------------

KUDU_SH = REPO / "deploy" / "azure" / "kudu.sh"
WEBJOB_SH = REPO / "deploy" / "azure" / "webjob.sh"


def test_app_has_public_network_access_disabled():
    site = _resource_block(BICEP.read_text(), "resource webApp 'Microsoft.Web/sites@")
    assert "publicNetworkAccess: 'Disabled'" in site


def test_app_private_endpoint_covers_site_and_scm():
    """The 'sites' group serves both the app and its Kudu endpoint; without it
    nothing can deploy or manage the WebJob."""
    text = BICEP.read_text()
    endpoint = _resource_block(text, "resource appPrivateEndpoint ")
    assert "privateLinkServiceId: webApp.id" in endpoint
    assert "groupIds: [ 'sites' ]" in endpoint
    assert "subnet: { id: privateEndpointSubnetId }" in endpoint
    assert "output privateEndpointName string = appPrivateEndpoint.name" in text


def test_no_vnet_integration_for_outbound():
    """Public access disabled is inbound only; the job's outbound calls need no
    VNet. Adding integration would route them through the landing zone's
    firewall for no reason."""
    assert "virtualNetworkSubnetId" not in BICEP.read_text()


def test_deploy_script_requires_and_checks_the_endpoint_subnet():
    text = DEPLOY_SH.read_text()
    assert "require PRIVATE_ENDPOINT_SUBNET_ID" in text
    assert 'privateEndpointSubnetId="$PRIVATE_ENDPOINT_SUBNET_ID"' in text
    assert 'az network vnet subnet show --ids "$PRIVATE_ENDPOINT_SUBNET_ID"' in text
    # Checked before the package is built.
    assert text.index('--ids "$PRIVATE_ENDPOINT_SUBNET_ID"') < text.index(
        'echo "==> Building WebJob package'
    )


def test_kudu_calls_pin_the_hostname_to_the_private_ip():
    """The laptop's DNS (Zscaler) resolves privatelink names to public
    addresses. Every Kudu call must connect to the endpoint's IP while still
    verifying TLS against the real hostname."""
    text = KUDU_SH.read_text()
    assert '--resolve "${KUDU_HOST}:443:${KUDU_IP}"' in text
    assert '"https://${KUDU_HOST}${path}"' in text
    assert "--insecure" not in text and " -k " not in text
    # The private path is not TLS-inspected; the system trust store suffices.
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "--cacert" not in code
    assert "--resource https://appservice.azure.com" in text, "same token az webapp deploy uses"


def test_deploys_and_operations_go_through_the_private_path():
    """`az webapp deploy` and `az webapp webjob` reach Kudu publicly, which the
    policy refuses."""
    for path in (DEPLOY_SH, WEBJOB_SH):
        text = path.read_text()
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        assert '. "$(dirname "$0")/kudu.sh"' in code
        assert "az webapp deploy" not in code
        assert "az webapp webjob" not in code
    deploy = DEPLOY_SH.read_text()
    assert 'kudu POST "/api/publish?type=zip' in deploy
    assert "kudu GET /api/triggeredwebjobs" in deploy
    assert deploy.index("kudu_reachable") < deploy.index('kudu POST "/api/publish')


def test_kudu_failures_report_their_cause():
    """A bare "cannot reach Kudu" hid a client-side TLS failure behind a message
    about routes; the status and curl's own error must reach the user."""
    text = KUDU_SH.read_text()
    assert "--write-out '%{http_code}'" in text
    assert "KUDU_ERROR=" in text
    for status in ("000)", "401)", "403)"):
        assert status in text


def test_kudu_reachability_uses_an_endpoint_linux_kudu_serves():
    """/api/environment returns Kudu's HTML dashboard with HTTP 500 on a Linux
    app, which made the reachability check fail a healthy app on the first real
    deploy (2026-10-02). /api/deployments returns 200 there."""
    text = KUDU_SH.read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "kudu GET /api/deployments --max-time 20" in code
    assert "/api/environment" not in code

