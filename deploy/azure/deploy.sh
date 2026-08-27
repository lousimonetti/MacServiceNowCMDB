#!/usr/bin/env bash
# Deploy intune-cmdb-sync to Azure Container Apps Jobs.
#
# Three topologies, selected by GRAPH_AUTH_MODE:
#
#   client_secret     (default) Intune lives in a different tenant from this
#                     subscription. The job authenticates as an app registration
#                     from the Intune tenant; its secret goes into Key Vault.
#
#   managed_identity  Intune and this subscription share a tenant. The job's
#                     managed identity is granted Graph application permissions
#                     directly and no Graph credential exists. This script
#                     performs that grant, which ARM cannot do because app-role
#                     assignments live in Entra rather than in ARM.
#
#   federated_managed_identity
#                     Cross-tenant AND secretless. The job's managed identity is
#                     a federated credential on a multi-tenant app registration
#                     that has been admin-consented into the Intune tenant. That
#                     setup spans two tenants and cannot be automated from a
#                     single login, so this script verifies rather than creates
#                     it -- see docs/entra-setup.md for the four steps.
#
# Requires: az CLI, logged in to the SUBSCRIPTION's tenant. managed_identity mode
# additionally needs Privileged Role Administrator, Cloud Application
# Administrator, or Global Administrator in that same tenant.

set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-intune-cmdb-sync}"
LOCATION="${LOCATION:-eastus}"
NAME_PREFIX="${NAME_PREFIX:-intunecmdb}"
CONTAINER_IMAGE="${CONTAINER_IMAGE:-ghcr.io/your-org/intune-cmdb-sync:latest}"
CRON="${CRON:-15 3 * * *}"
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

echo "==> Resource group ${RESOURCE_GROUP} (${LOCATION})"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "==> Deploying infrastructure (graph auth: ${GRAPH_AUTH_MODE})"
DEPLOYMENT_OUTPUT=$(az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$(dirname "$0")/main.bicep" \
  --parameters \
      namePrefix="$NAME_PREFIX" \
      containerImage="$CONTAINER_IMAGE" \
      cronExpression="$CRON" \
      graphAuthMode="$GRAPH_AUTH_MODE" \
      graphTenantId="$GRAPH_TENANT_ID" \
      graphClientId="${GRAPH_CLIENT_ID:-}" \
      graphClientSecret="${GRAPH_CLIENT_SECRET:-}" \
      serviceNowInstance="$SNOW_INSTANCE" \
      serviceNowClientId="$SNOW_CLIENT_ID" \
      serviceNowClientSecret="$SNOW_CLIENT_SECRET" \
      retireMissingDevices="${RETIRE_MISSING:-false}" \
      dryRun="${DRY_RUN:-false}" \
  --query properties.outputs \
  --output json)

PRINCIPAL_ID=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.managedIdentityPrincipalId.value')
CLIENT_ID=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.managedIdentityClientId.value')
JOB_NAME=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.jobName.value')

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
  echo "    other tenant. Run the job once before trusting the schedule."
else
  echo "==> Graph permissions are carried by app registration ${GRAPH_CLIENT_ID}"
  echo "    in tenant ${GRAPH_TENANT_ID}. Confirm it has admin consent for:"
  printf '      %s\n' "${REQUIRED_ROLES[@]}"
  echo "    The managed identity ${CLIENT_ID} is used only to read Key Vault."
fi

cat <<SUMMARY

Deployed.

  Job              ${JOB_NAME}
  Resource group   ${RESOURCE_GROUP}
  Schedule         ${CRON} (UTC)
  Graph auth       ${GRAPH_AUTH_MODE}
  Intune tenant    ${GRAPH_TENANT_ID}

Verify the whole path end to end before trusting the schedule:

  az containerapp job start --name ${JOB_NAME} --resource-group ${RESOURCE_GROUP}
  az containerapp job execution list --name ${JOB_NAME} \\
      --resource-group ${RESOURCE_GROUP} --output table

Logs:

  az monitor log-analytics query \\
    --workspace "\$(az monitor log-analytics workspace show \\
        -g ${RESOURCE_GROUP} -n ${NAME_PREFIX}-logs --query customerId -o tsv)" \\
    --analytics-query "ContainerAppConsoleLogs_CL | where ContainerJobName_s == '${JOB_NAME}' | order by TimeGenerated desc | take 100"

SUMMARY
