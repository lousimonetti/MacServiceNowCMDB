#!/usr/bin/env bash
# Deploy intune-cmdb-sync to Azure Functions (Flex Consumption).
#
# There is no container image and no registry: this script builds a zip package
# on this machine -- the connector, its Linux wheels, and the function entry
# point -- and deploys it into a blob container in the stack's own storage
# account. Re-running it redeploys both the infrastructure and the code from the
# current checkout.
#
# Three topologies, selected by GRAPH_AUTH_MODE:
#
#   client_secret     (default) Intune lives in a different tenant from this
#                     subscription. The app authenticates as an app registration
#                     from the Intune tenant; its secret goes into Key Vault.
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

set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-intune-cmdb-sync}"
LOCATION="${LOCATION:-eastus}"
# Every resource name derives from this. Run once per ServiceNow environment
# with a distinct prefix (intunecmdb-dev, intunecmdb-prod) to get fully separate
# stacks in one resource group -- see README.md, "Multiple ServiceNow environments".
NAME_PREFIX="${NAME_PREFIX:-intunecmdb}"
# Six-field NCRONTAB (seconds first), UTC. Flex Consumption has no time zones.
SCHEDULE="${SCHEDULE:-0 15 3 * * *}"
# The Functions runtime version; the package's wheels are built for the same one.
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
PYTHON="${PYTHON:-python3}"
FUNCTION_NAME="intune_cmdb_sync"
GRAPH_AUTH_MODE="${GRAPH_AUTH_MODE:-client_secret}"

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

require SNOW_INSTANCE
require SNOW_CLIENT_ID
require SNOW_CLIENT_SECRET

# CRON was the Container Apps setting: five fields. NCRONTAB has six, and a
# five-field value is rejected by the timer at startup, after deployment.
[[ -z "${CRON:-}" ]] || die "CRON is no longer read. Set SCHEDULE to a six-field NCRONTAB
       expression instead, seconds first: CRON='15 3 * * *' becomes SCHEDULE='0 15 3 * * *'"
read -ra SCHEDULE_FIELDS <<< "$SCHEDULE"
[[ ${#SCHEDULE_FIELDS[@]} -eq 6 ]] \
  || die "SCHEDULE must have six fields, seconds first (got '${SCHEDULE}')"

case "$PYTHON_VERSION" in
  3.11|3.12) ;;
  *) die "PYTHON_VERSION must be 3.11 or 3.12, the versions main.bicep offers (got '${PYTHON_VERSION}')" ;;
esac

for tool in jq zip "$PYTHON"; do
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

SUBSCRIPTION_TENANT=$(az account show --query tenantId --output tsv)

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
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
PACKAGE_DIR="${BUILD_DIR}/package"
PACKAGE_ZIP="${BUILD_DIR}/app.zip"
SOURCE_REVISION=$(git -C "$REPO_ROOT" describe --always --dirty 2>/dev/null || echo unknown)

echo "==> Building function package (Python ${PYTHON_VERSION}, source ${SOURCE_REVISION})"
mkdir -p "$PACKAGE_DIR"
cp "$(dirname "$0")/functions/function_app.py" "$(dirname "$0")/functions/host.json" "$PACKAGE_DIR/"
"$PYTHON" -m pip wheel --quiet --no-deps --wheel-dir "${BUILD_DIR}/wheel" "$REPO_ROOT" \
  || die "could not build the connector wheel"
WHEEL=$(ls "${BUILD_DIR}"/wheel/intune_cmdb_sync-*.whl)
# Linux x86_64 wheels for the runtime's Python, whatever this machine is.
# Without --platform, a Mac would package macOS builds of cryptography (pulled in
# by azure-identity) and the function would fail to import at its first run.
"$PYTHON" -m pip install --quiet \
    --target "${PACKAGE_DIR}/.python_packages/lib/site-packages" \
    --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 \
    --implementation cp --python-version "$PYTHON_VERSION" --only-binary=:all: \
    "${WHEEL}[azure]" \
  || die "could not install the connector's Linux dependencies"
(cd "$PACKAGE_DIR" && zip --quiet --recurse-paths "$PACKAGE_ZIP" .)
echo "    $(du -h "$PACKAGE_ZIP" | cut -f1) package"

echo "==> Resource group ${RESOURCE_GROUP} (${LOCATION})"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "==> Deploying infrastructure (graph auth: ${GRAPH_AUTH_MODE})"
DEPLOYMENT_OUTPUT=$(az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$(dirname "$0")/main.bicep" \
  --parameters \
      namePrefix="$NAME_PREFIX" \
      schedule="$SCHEDULE" \
      pythonVersion="$PYTHON_VERSION" \
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
APP_NAME=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.functionAppName.value')

# The upload writes to the package container as the app's identity, whose role
# assignments were created seconds ago and can take a minute or two to apply.
echo "==> Deploying code to ${APP_NAME}"
for attempt in 1 2 3 4 5; do
  if az functionapp deployment source config-zip \
      --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
      --src "$PACKAGE_ZIP" --build-remote false --output none; then
    break
  fi
  [[ $attempt -lt 5 ]] || die "code deployment failed; the infrastructure is deployed, so re-running
       this script is safe"
  echo "    attempt ${attempt} failed; retrying in 30s (role assignments may still be propagating)"
  sleep 30
done

# A package that deploys fine but fails to import (a missing or wrong-platform
# wheel) registers no function at all, and then nothing ever runs and nothing
# errors. Make that a deploy-time failure instead.
echo "==> Checking the timer function is registered"
REGISTERED=""
for attempt in 1 2 3 4 5 6; do
  REGISTERED=$(az functionapp function list --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
      --query "[?ends_with(name, '/${FUNCTION_NAME}')] | length(@)" --output tsv 2>/dev/null || echo 0)
  [[ "$REGISTERED" == "1" ]] && break
  sleep 20
done
[[ "$REGISTERED" == "1" ]] || die "${FUNCTION_NAME} is not registered on ${APP_NAME}. The package
       deployed but did not load; check the host's startup traces:
         AppTraces | where AppRoleName == '${APP_NAME}' | where SeverityLevel >= 3"
echo "    ${FUNCTION_NAME} registered"

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
  echo "    other tenant. Run the function once before trusting the schedule."
else
  echo "==> Graph permissions are carried by app registration ${GRAPH_CLIENT_ID}"
  echo "    in tenant ${GRAPH_TENANT_ID}. Confirm it has admin consent for:"
  printf '      %s\n' "${REQUIRED_ROLES[@]}"
  echo "    The managed identity ${CLIENT_ID} is used only to read Key Vault."
fi

APP_HOST=$(az functionapp show --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
    --query defaultHostName --output tsv)

cat <<SUMMARY

Deployed.

  Function app     ${APP_NAME} (${FUNCTION_NAME})
  Source           ${SOURCE_REVISION}
  ServiceNow       ${SNOW_INSTANCE} (${SNOW_WRITE_MODE})
  Dry run          ${DRY_RUN}$([[ "$DRY_RUN" == false ]] && echo "  -- LIVE: the next run commits to the CMDB")
  Retire missing   ${RETIRE_MISSING}
  Class map        ${SNOW_CLASS_MAP:-built-in (windows, macos)}
  Mapping override ${MAPPING_OVERRIDES_FILE:-none}
  Resource group   ${RESOURCE_GROUP}
  Schedule         ${SCHEDULE} (NCRONTAB, UTC)
  Graph auth       ${GRAPH_AUTH_MODE}
  Intune tenant    ${GRAPH_TENANT_ID}
  Alerts           ${ALERT_EMAIL:-NONE - set ALERT_EMAIL to be told when runs stop}

Verify the whole path end to end before trusting the schedule. A timer function
is started by hand through the host's admin endpoint (returns 202 at once):

  curl -sS -X POST "https://${APP_HOST}/admin/functions/${FUNCTION_NAME}" \\
    -H "x-functions-key: \$(az functionapp keys list -g ${RESOURCE_GROUP} -n ${APP_NAME} --query masterKey -o tsv)" \\
    -H "Content-Type: application/json" -d '{}'

Logs (allow a few minutes for ingestion):

  az monitor log-analytics query \\
    --workspace "\$(az monitor log-analytics workspace show \\
        -g ${RESOURCE_GROUP} -n ${NAME_PREFIX}-logs --query customerId -o tsv)" \\
    --analytics-query "AppTraces | where AppRoleName == '${APP_NAME}' | order by TimeGenerated desc | take 100 | project TimeGenerated, Message"

SUMMARY
