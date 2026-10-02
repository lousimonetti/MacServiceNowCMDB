#!/usr/bin/env bash
# Deploy intune-cmdb-sync to Azure App Service, as a scheduled WebJob.
#
# There is no container image, no registry, no storage account and no Key Vault:
# the landing-zone policy denies the first two outright and denies public access
# to the other two (see main.bicep). This script builds a zip package on this
# machine -- the connector, its Linux wheels, and the WebJob -- and deploys it to
# the app's own /home/site/wwwroot. Re-running it redeploys both the
# infrastructure and the code from the current checkout.
#
# The app has public network access disabled (landing-zone policy), so the code
# goes in through the app's private endpoint: kudu.sh reaches Kudu at the
# endpoint's private IP. This machine needs a route to that IP (Zscaler Private
# Access or VPN); it does not need DNS for it.
#
# Three topologies, selected by GRAPH_AUTH_MODE:
#
#   client_secret     (default) Intune lives in a different tenant from this
#                     subscription. The app authenticates as an app registration
#                     from the Intune tenant; its secret becomes an app setting.
#
#   managed_identity  Intune and this subscription share a tenant. The app's
#                     managed identity is granted Graph application permissions
#                     directly and no Graph credential exists. This script
#                     performs that grant, which ARM cannot do because app-role
#                     assignments live in Entra rather than in ARM.
#
#   federated_managed_identity
#                     Cross-tenant AND secretless. The app's managed identity is
#                     a federated credential on a multi-tenant app registration
#                     that has been admin-consented into the Intune tenant. That
#                     setup spans two tenants and cannot be automated from a
#                     single login, so this script verifies rather than creates
#                     it -- see docs/entra-setup.md for the four steps.
#
# Requires: az CLI logged in to the SUBSCRIPTION's tenant, jq, zip, and a python3
# with pip (any OS: the wheels are fetched for Linux explicitly). managed_identity mode
# additionally needs Privileged Role Administrator, Cloud Application
# Administrator, or Global Administrator in that same tenant.

# Bash only. `sh deploy.sh` runs it under a POSIX shell (dash on Linux, bash in
# POSIX mode on macOS), which rejects the bash syntax below with a bare syntax
# error, so re-run under bash instead. Written in POSIX sh so that any shell can
# parse it before it gets that far.
case ":${SHELLOPTS:-}:" in
  *:posix:*) exec bash "$0" "$@" ;;
esac
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

# The resource group must already exist: in the landing zone, groups are
# provisioned by the platform team (DEV is azc-obm-development), and this script
# never creates one. Set RESOURCE_GROUP to name it, or leave it unset and the
# script lists the groups in the current subscription and asks you to pick one.
# There is no default, for the same reason as DRY_RUN: a deploy into the wrong
# group is hard to see and harder to undo.
RESOURCE_GROUP="${RESOURCE_GROUP:-}"
# Region for every resource, independent of the resource group's own region.
# East US by requirement.
LOCATION="${LOCATION:-eastus}"
# Every resource name derives from this. Run once per ServiceNow environment
# with a distinct prefix (intunecmdb-dev, intunecmdb-prod) to get fully separate
# stacks in one resource group -- see README.md, "Multiple ServiceNow environments".
NAME_PREFIX="${NAME_PREFIX:-intunecmdb}"
# Six-field NCRONTAB (seconds first), UTC. It goes into the WebJob's settings.job.
SCHEDULE="${SCHEDULE:-0 15 3 * * *}"
# The app runs PYTHON|3.12 (main.bicep); the package's wheels must match it.
PYTHON_VERSION="3.12"
PYTHON="${PYTHON:-python3}"
# Folder name under App_Data/jobs/triggered/, and the job name Kudu reports.
WEBJOB_NAME="intune-cmdb-sync"
GRAPH_AUTH_MODE="${GRAPH_AUTH_MODE:-client_secret}"
# Extra root certificates, for a network that intercepts TLS (Zscaler): a PEM
# file, or `macos-keychain` to use the macOS System keychain, where Zscaler
# Client Connector installs its root. Unset = the tools' own trust stores.
CA_BUNDLE="${CA_BUNDLE:-}"

# Skip az's "is there a newer Bicep?" lookup. It is only a notice, it is the
# first request to fail behind TLS inspection, and the deploy does not need it.
# The environment form of `az config set bicep.check_version=false`, so it
# applies to this run only.
export AZURE_BICEP_CHECK_VERSION=false

# Graph's own service principal. This app ID is the same in every tenant.
GRAPH_APP_ID="00000003-0000-0000-c000-000000000000"

# Application permissions the connector needs.
#   DeviceManagementManagedDevices.Read.All  read Intune managed devices
#   User.Read.All                            resolve device owners to Entra users
#                                            (drop it if GRAPH_ENRICH_USERS=false)
REQUIRED_ROLES=(
  "DeviceManagementManagedDevices.Read.All"
  "User.Read.All"
)

die() { echo "error: $*" >&2; exit 1; }

require() {
  [[ -n "${!1:-}" ]] || die "$1 must be set"
}

# An existing subnet for the app's inbound private endpoint (DEV: hybridsubnet-1
# of vpcx-vnet-eastus in VPCXRG). Never created here.
require PRIVATE_ENDPOINT_SUBNET_ID
require SNOW_INSTANCE
require SNOW_CLIENT_ID
require SNOW_CLIENT_SECRET

# CRON was the Container Apps setting: five fields. NCRONTAB has six, and the
# WebJob scheduler rejects a five-field value only after deployment.
[[ -z "${CRON:-}" ]] || die "CRON is no longer read. Set SCHEDULE to a six-field NCRONTAB
       expression instead, seconds first: CRON='15 3 * * *' becomes SCHEDULE='0 15 3 * * *'"
read -ra SCHEDULE_FIELDS <<< "$SCHEDULE"
[[ ${#SCHEDULE_FIELDS[@]} -eq 6 ]] \
  || die "SCHEDULE must have six fields, seconds first (got '${SCHEDULE}')"

for tool in jq zip curl "$PYTHON"; do
  command -v "$tool" >/dev/null || die "$tool is required"
done

SNOW_WRITE_MODE="${SNOW_WRITE_MODE:-identify_reconcile}"
case "$SNOW_WRITE_MODE" in
  identify_reconcile|cmdb_instance) ;;
  *) die "SNOW_WRITE_MODE must be 'identify_reconcile' or 'cmdb_instance' (got '${SNOW_WRITE_MODE}')" ;;
esac

# No default, deliberately. Defaulting to false made every redeploy that forgot
# DRY_RUN=true turn a dry-run stack live; defaulting to true would leave a
# production stack silently committing nothing. Both values are also checked
# exactly: az passes any bool parameter other than 'true' as false, so
# DRY_RUN=yes would deploy a live app.
require_bool() {
  case "${!1:-}" in
    true|false) ;;
    "") die "$1 must be set to true or false$2" ;;
    *)  die "$1 must be exactly 'true' or 'false' (got '${!1}')" ;;
  esac
}
require_bool DRY_RUN " -- true for a first deploy; false commits to the CMDB"
RETIRE_MISSING="${RETIRE_MISSING:-false}"
require_bool RETIRE_MISSING ""

# The app receives only what main.bicep sets, so local tuning has to be passed
# through explicitly or the Azure run differs from the one tested locally.
#   SNOW_CLASS_MAP          passed as-is; replaces the built-in map
#   MAPPING_OVERRIDES_FILE  a LOCAL path; its contents reach the app inline as
#                           MAPPING_OVERRIDES_JSON, since the package holds no such file
MAPPING_OVERRIDES='{}'
if [[ -n "${MAPPING_OVERRIDES_FILE:-}" ]]; then
  [[ -f "$MAPPING_OVERRIDES_FILE" ]] || die "MAPPING_OVERRIDES_FILE not found: ${MAPPING_OVERRIDES_FILE}"
  # _comment is documentation for people; drop it rather than ship it in an env var.
  MAPPING_OVERRIDES=$(jq -ce 'if type == "object" then del(._comment) else error("not an object") end' \
      "$MAPPING_OVERRIDES_FILE" 2>/dev/null) \
    || die "MAPPING_OVERRIDES_FILE must contain a JSON object: ${MAPPING_OVERRIDES_FILE}"
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

# Behind TLS inspection, az and pip trust only their bundled public roots
# (certifi), not the proxy's, and fail with CERTIFICATE_VERIFY_FAILED. With
# CA_BUNDLE set, build one bundle of the public roots PLUS the extra ones --
# these variables replace a tool's default bundle, so the extra roots alone
# would break every site the proxy does not inspect -- and point all three tools
# at it, for this run only. Never disable verification instead.
if [[ -n "$CA_BUNDLE" ]]; then
  EXTRA_ROOTS="${BUILD_DIR}/extra-roots.pem"
  if [[ "$CA_BUNDLE" == macos-keychain ]]; then
    [[ "$(uname -s)" == Darwin ]] \
      || die "CA_BUNDLE=macos-keychain works only on macOS; set CA_BUNDLE to a PEM file"
    security find-certificate -a -p /Library/Keychains/System.keychain > "$EXTRA_ROOTS" 2>/dev/null || true
    grep -q "BEGIN CERTIFICATE" "$EXTRA_ROOTS" \
      || die "the macOS System keychain holds no certificates to add. Find the proxy's root
       as described in README.md, \"Behind a TLS-inspecting proxy\", and set CA_BUNDLE
       to that PEM file instead."
  else
    [[ -f "$CA_BUNDLE" ]] || die "CA_BUNDLE file not found: ${CA_BUNDLE}"
    grep -q "BEGIN CERTIFICATE" "$CA_BUNDLE" \
      || die "CA_BUNDLE must be PEM (text with BEGIN CERTIFICATE): ${CA_BUNDLE}. A DER .cer
       file converts with: openssl x509 -inform der -in root.cer -out root.pem"
    cp "$CA_BUNDLE" "$EXTRA_ROOTS"
  fi
  PUBLIC_ROOTS=$("$PYTHON" -c 'import certifi; print(certifi.where())' 2>/dev/null \
    || echo /etc/ssl/cert.pem)
  [[ -f "$PUBLIC_ROOTS" ]] || die "no public root bundle found (install certifi for ${PYTHON})"
  # The echo keeps the last certificate of one file off the first line of the next.
  { cat "$PUBLIC_ROOTS"; echo; cat "$EXTRA_ROOTS"; } > "${BUILD_DIR}/ca-bundle.pem"
  export REQUESTS_CA_BUNDLE="${BUILD_DIR}/ca-bundle.pem"   # az (requests)
  export PIP_CERT="${BUILD_DIR}/ca-bundle.pem"             # pip
  export SSL_CERT_FILE="${BUILD_DIR}/ca-bundle.pem"        # python ssl, httpx
  echo "==> Trusting extra root certificates from ${CA_BUNDLE}"
fi

# One TLS handshake per service, with the trust az and pip will use, so an
# intercepted connection fails here with one actionable line instead of a stack
# trace halfway through the deploy. Only a certificate failure stops the run;
# anything else (no route, an explicit proxy) is left for az and pip to report.
# X509_STRICT is cleared because az and pip do not set it, and some proxy roots
# that they accept fail it.
TLS_PROBE=$("$PYTHON" - "${REQUESTS_CA_BUNDLE:-}" <<'PY' 2>/dev/null
import socket, ssl, sys
bundle = sys.argv[1] or None
if bundle is None:
    try:
        import certifi
        bundle = certifi.where()
    except ImportError:
        pass
ctx = ssl.create_default_context(cafile=bundle)
if hasattr(ssl, "VERIFY_X509_STRICT"):
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
for host in ("management.azure.com", "pypi.org"):
    try:
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                pass
    except ssl.SSLCertVerificationError as exc:
        print(f"{host}: {exc.verify_message}")
        sys.exit(2)
    except OSError:
        pass
PY
) || {
  if [[ -n "$CA_BUNDLE" ]]; then
    die "TLS to ${TLS_PROBE%%:*} still fails certificate verification (${TLS_PROBE#*: })
       with CA_BUNDLE=${CA_BUNDLE}. That bundle does not contain the proxy's root;
       see README.md, \"Behind a TLS-inspecting proxy\"."
  fi
  die "TLS to ${TLS_PROBE%%:*} fails certificate verification (${TLS_PROBE#*: }).
       Something is intercepting HTTPS (Zscaler?). Re-run with CA_BUNDLE=macos-keychain,
       or CA_BUNDLE=/path/to/proxy-root.pem; see README.md, \"Behind a TLS-inspecting proxy\"."
}

SUBSCRIPTION_TENANT=$(az account show --query tenantId --output tsv)
SUBSCRIPTION_NAME=$(az account show --query name --output tsv)

# Interactive only: a pipeline or CI run with no RESOURCE_GROUP must fail, not
# block on a prompt or guess.
pick_resource_group() {
  [[ -t 0 ]] || die "RESOURCE_GROUP must be set when not running interactively"

  local names=() locations=() name location listing
  listing=$(az group list --query "sort_by(@, &name)[].[name, location]" --output tsv) \
    || die "could not list resource groups in subscription ${SUBSCRIPTION_NAME}"
  while IFS=$'\t' read -r name location; do
    [[ -n "$name" ]] || continue
    names+=("$name")
    locations+=("$location")
  done <<< "$listing"
  [[ ${#names[@]} -gt 0 ]] || die "no resource groups visible in subscription ${SUBSCRIPTION_NAME}.
       Check the subscription (az account set --subscription <name>)."

  echo "==> Resource groups in subscription ${SUBSCRIPTION_NAME}"
  local i
  for i in "${!names[@]}"; do
    printf '    %3d) %-40s %s\n' "$((i + 1))" "${names[$i]}" "${locations[$i]}"
  done
  echo "    Resources are created in ${LOCATION}, whatever the group's own region."

  local choice
  while true; do
    read -r -p "    Deploy into which resource group? [1-${#names[@]}, q to quit] " choice \
      || die "no resource group chosen"
    case "$choice" in
      q|Q) die "cancelled; nothing was deployed" ;;
      ''|*[!0-9]*) echo "    enter a number from the list" ;;
      *)
        choice=$((10#$choice))
        if (( choice >= 1 && choice <= ${#names[@]} )); then
          RESOURCE_GROUP="${names[$((choice - 1))]}"
          return
        fi
        echo "    enter a number from the list" ;;
    esac
  done
}

[[ -n "$RESOURCE_GROUP" ]] || pick_resource_group

echo "==> Resource group ${RESOURCE_GROUP} (subscription ${SUBSCRIPTION_NAME})"
az group show --name "$RESOURCE_GROUP" --output none 2>/dev/null \
  || die "resource group ${RESOURCE_GROUP} not found in subscription ${SUBSCRIPTION_NAME}.
       This script deploys into an existing group and never creates one. Check
       the subscription (az account set --subscription <name>) and the name."
echo "    deploying to ${LOCATION}"

# Before anything is built: the endpoint goes in this subnet, and the deploy
# fails late and confusingly if it does not exist or this login cannot use it.
ENDPOINT_SUBNET_NAME=$(az network vnet subnet show --ids "$PRIVATE_ENDPOINT_SUBNET_ID" \
    --query name --output tsv 2>/dev/null) \
  || die "private endpoint subnet not found, or not visible to this login:
       ${PRIVATE_ENDPOINT_SUBNET_ID}
       In DEV it is hybridsubnet-1 of vpcx-vnet-eastus:
         az network vnet subnet show -g VPCXRG --vnet-name vpcx-vnet-eastus -n hybridsubnet-1 --query id -o tsv"
echo "    app private endpoint in ${ENDPOINT_SUBNET_NAME}"

GRAPH_TENANT_ID="${GRAPH_TENANT_ID:-$SUBSCRIPTION_TENANT}"

case "$GRAPH_AUTH_MODE" in
  client_secret)
    require GRAPH_CLIENT_ID
    require GRAPH_CLIENT_SECRET
    ;;
  federated_managed_identity)
    require GRAPH_CLIENT_ID
    if [[ "$GRAPH_TENANT_ID" == "$SUBSCRIPTION_TENANT" ]]; then
      echo "NOTE: GRAPH_AUTH_MODE=federated_managed_identity works here, but with"
      echo "      Intune in this same tenant, managed_identity is simpler and needs"
      echo "      no app registration at all."
    fi
    ;;
  managed_identity)
    if [[ "$GRAPH_TENANT_ID" != "$SUBSCRIPTION_TENANT" ]]; then
      die "GRAPH_AUTH_MODE=managed_identity requires Intune and this subscription to
       share a tenant. Intune tenant is ${GRAPH_TENANT_ID}, subscription tenant is
       ${SUBSCRIPTION_TENANT}. A managed identity is single-tenant and cannot be
       granted app roles in another directory. Use GRAPH_AUTH_MODE=client_secret
       with an app registration from the Intune tenant, or
       GRAPH_AUTH_MODE=federated_managed_identity to stay secretless."
    fi
    ;;
  *)
    die "GRAPH_AUTH_MODE must be 'client_secret', 'managed_identity', or
       'federated_managed_identity' (got '${GRAPH_AUTH_MODE}')"
    ;;
esac

if [[ "$GRAPH_TENANT_ID" != "$SUBSCRIPTION_TENANT" ]]; then
  echo "==> Cross-tenant deployment"
  echo "      Intune tenant       ${GRAPH_TENANT_ID}"
  echo "      Subscription tenant ${SUBSCRIPTION_TENANT}"
fi

# Built before anything in Azure changes, so a package that cannot be built
# fails the deploy with nothing half-done.
PACKAGE_DIR="${BUILD_DIR}/package"
PACKAGE_ZIP="${BUILD_DIR}/app.zip"
SOURCE_REVISION=$(git -C "$REPO_ROOT" describe --always --dirty 2>/dev/null || echo unknown)

echo "==> Building WebJob package (Python ${PYTHON_VERSION}, source ${SOURCE_REVISION})"
JOB_DIR="${PACKAGE_DIR}/App_Data/jobs/triggered/${WEBJOB_NAME}"
mkdir -p "$JOB_DIR"
cp "$(dirname "$0")/webjob/run.py" "$JOB_DIR/"
# is_singleton: never two runs at once, which would race on state.json.
jq -n --arg schedule "$SCHEDULE" '{schedule: $schedule, is_singleton: true}' > "${JOB_DIR}/settings.job"
"$PYTHON" -m pip wheel --quiet --no-deps --wheel-dir "${BUILD_DIR}/wheel" "$REPO_ROOT" \
  || die "could not build the connector wheel"
WHEEL=$(ls "${BUILD_DIR}"/wheel/intune_cmdb_sync-*.whl)
# Linux x86_64 wheels for the runtime's Python, whatever this machine is.
# Without --platform, a Mac would package macOS builds of cryptography (pulled in
# by azure-identity) and the WebJob would fail to import at its first run.
"$PYTHON" -m pip install --quiet \
    --target "${PACKAGE_DIR}/packages" \
    --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 \
    --implementation cp --python-version "$PYTHON_VERSION" --only-binary=:all: \
    "${WHEEL}[appservice]" \
  || die "could not install the connector's Linux dependencies"
(cd "$PACKAGE_DIR" && zip --quiet --recurse-paths "$PACKAGE_ZIP" .)
echo "    $(du -h "$PACKAGE_ZIP" | cut -f1) package"

echo "==> Deploying infrastructure (graph auth: ${GRAPH_AUTH_MODE})"
DEPLOYMENT_OUTPUT=$(az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$(dirname "$0")/main.bicep" \
  --parameters \
      namePrefix="$NAME_PREFIX" \
      privateEndpointSubnetId="$PRIVATE_ENDPOINT_SUBNET_ID" \
      location="$LOCATION" \
      graphAuthMode="$GRAPH_AUTH_MODE" \
      graphTenantId="$GRAPH_TENANT_ID" \
      graphClientId="${GRAPH_CLIENT_ID:-}" \
      graphClientSecret="${GRAPH_CLIENT_SECRET:-}" \
      serviceNowInstance="$SNOW_INSTANCE" \
      serviceNowClientId="$SNOW_CLIENT_ID" \
      serviceNowClientSecret="$SNOW_CLIENT_SECRET" \
      discoverySource="${SNOW_DISCOVERY_SOURCE:-Intune}" \
      writeMode="$SNOW_WRITE_MODE" \
      classMap="${SNOW_CLASS_MAP:-}" \
      mappingOverrides="$MAPPING_OVERRIDES" \
      retireMissingDevices="$RETIRE_MISSING" \
      dryRun="$DRY_RUN" \
      alertEmail="${ALERT_EMAIL:-}" \
  --query properties.outputs \
  --output json)

PRINCIPAL_ID=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.managedIdentityPrincipalId.value')
CLIENT_ID=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.managedIdentityClientId.value')
APP_NAME=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.webAppName.value')
ENDPOINT_NAME=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.privateEndpointName.value')

# shellcheck source=kudu.sh
. "$(dirname "$0")/kudu.sh"

echo "==> Reaching Kudu for ${APP_NAME} through its private endpoint"
kudu_init "$RESOURCE_GROUP" "$APP_NAME" "$ENDPOINT_NAME" || die "could not resolve the app's private endpoint"
echo "    ${KUDU_HOST} at ${KUDU_IP}"
# A new endpoint and a new app can take a minute or two to answer.
for attempt in 1 2 3 4 5 6; do
  kudu_reachable && break
  if [[ $attempt -eq 6 ]]; then
    kudu_unreachable_help
    die "the infrastructure is deployed; re-run this script once Kudu is reachable"
  fi
  echo "    not answering yet; retrying in 30s"
  sleep 30
done

echo "==> Deploying code to ${APP_NAME}"
kudu POST "/api/publish?type=zip&clean=true&restart=true" \
    --data-binary "@${PACKAGE_ZIP}" --header "Content-Type: application/zip" \
    --max-time 900 --output /dev/null \
  || die "code deployment failed; the infrastructure is deployed, so re-running
       this script is safe"

# A package that deploys fine but puts the job in the wrong folder registers no
# WebJob at all, and then nothing ever runs and nothing errors. Make that a
# deploy-time failure instead.
echo "==> Checking the WebJob is registered"
REGISTERED=""
for attempt in 1 2 3 4 5 6; do
  REGISTERED=$(kudu GET /api/triggeredwebjobs --max-time 30 2>/dev/null \
      | jq --arg name "$WEBJOB_NAME" '[.[]? | select(.name == $name)] | length' 2>/dev/null || echo 0)
  [[ "$REGISTERED" == "1" ]] && break
  sleep 20
done
[[ "$REGISTERED" == "1" ]] || die "WebJob ${WEBJOB_NAME} is not registered on ${APP_NAME}. Check the
       package layout (App_Data/jobs/triggered/${WEBJOB_NAME}/run.py) and Kudu's
       deployment log: $(dirname "$0")/webjob.sh deployments"
echo "    ${WEBJOB_NAME} registered (schedule ${SCHEDULE} UTC)"

if [[ "$GRAPH_AUTH_MODE" == "managed_identity" ]]; then
  echo "==> Granting Graph permissions to managed identity ${CLIENT_ID}"
  GRAPH_SP_ID=$(az ad sp show --id "$GRAPH_APP_ID" --query id --output tsv)

  for ROLE in "${REQUIRED_ROLES[@]}"; do
    ROLE_ID=$(az ad sp show --id "$GRAPH_APP_ID" \
      --query "appRoles[?value=='${ROLE}'].id | [0]" --output tsv)
    [[ -n "$ROLE_ID" && "$ROLE_ID" != "null" ]] || die "could not resolve Graph app role ${ROLE}"

    EXISTING=$(az rest --method GET \
      --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${PRINCIPAL_ID}/appRoleAssignments" \
      --query "value[?appRoleId=='${ROLE_ID}'] | length(@)" --output tsv 2>/dev/null || echo 0)

    if [[ "$EXISTING" != "0" ]]; then
      echo "    ${ROLE} already granted"
      continue
    fi

    echo "    granting ${ROLE}"
    az rest --method POST \
      --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${PRINCIPAL_ID}/appRoleAssignments" \
      --headers "Content-Type=application/json" \
      --body "{\"principalId\":\"${PRINCIPAL_ID}\",\"resourceId\":\"${GRAPH_SP_ID}\",\"appRoleId\":\"${ROLE_ID}\"}" \
      --output none
  done
elif [[ "$GRAPH_AUTH_MODE" == "federated_managed_identity" ]]; then
  echo "==> Secretless cross-tenant auth"
  echo "    App registration ${GRAPH_CLIENT_ID} in tenant ${GRAPH_TENANT_ID} must:"
  echo "      - be multi-tenant"
  echo "      - have admin consent for:"
  printf '          %s\n' "${REQUIRED_ROLES[@]}"
  echo "      - list managed identity ${CLIENT_ID} as a federated credential"
  echo "        with audience api://AzureADTokenExchange"
  echo
  echo "    None of that is verifiable from this login, because it lives in the"
  echo "    other tenant. Run the WebJob once before trusting the schedule."
else
  echo "==> Graph permissions are carried by app registration ${GRAPH_CLIENT_ID}"
  echo "    in tenant ${GRAPH_TENANT_ID}. Confirm it has admin consent for:"
  printf '      %s\n' "${REQUIRED_ROLES[@]}"
  echo "    The managed identity ${CLIENT_ID} only publishes telemetry."
fi

cat <<SUMMARY

Deployed.

  Web app          ${APP_NAME} (WebJob ${WEBJOB_NAME})
  Source           ${SOURCE_REVISION}
  ServiceNow       ${SNOW_INSTANCE} (${SNOW_WRITE_MODE})
  Dry run          ${DRY_RUN}$([[ "$DRY_RUN" == false ]] && echo "  -- LIVE: the next run commits to the CMDB")
  Retire missing   ${RETIRE_MISSING}
  Class map        ${SNOW_CLASS_MAP:-built-in (windows, macos)}
  Mapping override ${MAPPING_OVERRIDES_FILE:-none}
  Resource group   ${RESOURCE_GROUP} (${LOCATION})
  Inbound          private only: ${ENDPOINT_NAME} in ${ENDPOINT_SUBNET_NAME} (${KUDU_IP})
  Schedule         ${SCHEDULE} (NCRONTAB, UTC)
  Graph auth       ${GRAPH_AUTH_MODE}
  Intune tenant    ${GRAPH_TENANT_ID}
  Alerts           ${ALERT_EMAIL:-NONE - set ALERT_EMAIL to be told when runs stop}

Verify the whole path end to end before trusting the schedule. The app is
reachable only through its private endpoint, so use webjob.sh (it finds the
app from RESOURCE_GROUP and NAME_PREFIX):

  RESOURCE_GROUP=${RESOURCE_GROUP} NAME_PREFIX=${NAME_PREFIX} $(dirname "$0")/webjob.sh run
  RESOURCE_GROUP=${RESOURCE_GROUP} NAME_PREFIX=${NAME_PREFIX} $(dirname "$0")/webjob.sh history

Logs (allow a few minutes for ingestion):

  az monitor log-analytics query \\
    --workspace "\$(az monitor log-analytics workspace show \\
        -g ${RESOURCE_GROUP} -n ${NAME_PREFIX}-logs --query customerId -o tsv)" \\
    --analytics-query "AppTraces | where AppRoleName == '${APP_NAME}' | order by TimeGenerated desc | take 100 | project TimeGenerated, Message"

SUMMARY
