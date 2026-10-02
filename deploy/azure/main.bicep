// Azure Functions app (Flex Consumption) that runs intune-cmdb-sync on a daily
// timer.
//
// Why Functions and not Container Apps: the target subscription's landing-zone
// policy allows Microsoft.Web (Functions, App Service) but not Container Apps or
// Azure Container Registry. A Flex Consumption app is zip-deployed into a blob
// container in this stack's own storage account, so there is no image and no
// registry anywhere in the path.
//
// Cost shape (as of writing, list prices):
//   Flex Consumption      on-demand executions get a monthly free grant of
//                         100,000 GB-s and 250,000 executions per subscription.
//                         A 5-minute daily run at 2 GB uses roughly 18,000 GB-s
//                         per month, inside the grant.                    $0.00
//   Application Insights  ingested into the Log Analytics workspace below;
//   + Log Analytics       first 5 GB per month is free and this produces a few
//                         MB.                                              $0.00
//   Key Vault (standard)  ~$0.03 per 10,000 operations.                  ~$0.00
//   Storage               deployment package, host leases, and a few hundred
//                         KB of state on a Standard LRS file share.      ~$0.10
//                                                                    -------------
//                                                                 ~$0.10/month
//
// TWO TOPOLOGIES, set by `graphAuthMode`:
//
//   'managed_identity'  Intune and this subscription live in the SAME tenant.
//                       The app's managed identity is granted the Graph
//                       application permissions directly and no Graph credential
//                       exists anywhere. Prefer this whenever it is available.
//
//   'client_secret'     Intune lives in a DIFFERENT tenant from this
//                       subscription (default). A managed identity is
//                       single-tenant and cannot be granted app roles in another
//                       directory, so the app authenticates as an app
//                       registration from the Intune tenant, with its secret held
//                       in Key Vault. The managed identity is still used - to
//                       read Key Vault, not to reach Graph.

targetScope = 'resourceGroup'

// One prefix per ServiceNow environment (e.g. intunecmdb-dev, intunecmdb-prod)
// gives each a fully separate stack in the same resource group. See README.md.
@description('Base name used to derive every resource name.')
@minLength(3)
@maxLength(18)
param namePrefix string = 'intunecmdb'

@description('Azure region. Defaults to the resource group location. Must be one Flex Consumption supports.')
param location string = resourceGroup().location

@description('''
Six-field NCRONTAB schedule (seconds first), in UTC. Default: 03:15:00 every day.
Flex Consumption does not support WEBSITE_TIME_ZONE, so there is no local time.
''')
param schedule string = '0 15 3 * * *'

@description('Python version of the Functions runtime. deploy.sh builds the package for this same version.')
@allowed([
  '3.11'
  '3.12'
])
param pythonVersion string = '3.12'

@description('Instance memory. The workload is IO-bound on two REST APIs; 2048 is ample.')
@allowed([
  512
  2048
  4096
])
param instanceMemoryMB int = 2048

@description('''
Email address for alerts. Leave empty to skip creating alert rules entirely.

Two rules are created when set:
  - no successful run in the last 24 hours
  - a run finished with device-level errors, or degraded

The first matters more. A function that stops running is otherwise invisible:
there is no failure to notice, just a CMDB that quietly goes stale.
''')
param alertEmail string = ''

@description('ServiceNow instance: short name, host, or full https URL.')
param serviceNowInstance string

@description('ServiceNow OAuth client ID (client_credentials grant).')
param serviceNowClientId string

@description('ServiceNow OAuth client secret. Stored in Key Vault, never in app settings.')
@secure()
param serviceNowClientSecret string

@description('Tenant of this subscription. Used for Key Vault, not for Graph.')
param tenantId string = subscription().tenantId

@description('''
How the app authenticates to Microsoft Graph.

'client_secret'              Works everywhere, including cross-tenant. A secret
                             in Key Vault that someone has to rotate.
'managed_identity'           No secret at all, but only when Intune is in the
                             SAME tenant as this subscription -- a managed
                             identity cannot be granted app roles in another
                             directory.
'federated_managed_identity' Secretless AND cross-tenant. This app's managed
                             identity is registered as a federated credential
                             on a multi-tenant app registration that has been
                             admin-consented into the Intune tenant. Requires
                             that setup to exist first; see docs/entra-setup.md.

'workload_identity' is deliberately absent: it needs a projected federated
token file, which AKS and GitHub Actions provide and Azure Functions does not.
''')
@allowed([
  'client_secret'
  'managed_identity'
  'federated_managed_identity'
])
param graphAuthMode string = 'client_secret'

@description('Tenant where Intune lives. Differs from tenantId in a cross-tenant deployment.')
param graphTenantId string = subscription().tenantId

@description('App registration client ID from the Intune tenant. Required for client_secret mode.')
param graphClientId string = ''

@description('App registration client secret. Stored in Key Vault, never in app settings.')
@secure()
param graphClientSecret string = ''

@description('Discovery source name. Must match the sys_choice value on cmdb_ci.discovery_source.')
param discoverySource string = 'Intune'

@description('''
How CIs are written. identify_reconcile is preferred; cmdb_instance is for an
instance whose OAuth client is refused the IRE API at the REST gate but allowed
the CMDB Instance API. Which one works is per instance: run
`intune-cmdb-sync --check-api` against each before choosing.
''')
@allowed([
  'identify_reconcile'
  'cmdb_instance'
])
param writeMode string = 'identify_reconcile'

@description('''
SNOW_CLASS_MAP, e.g. windows=cmdb_ci_computer;macos=cmdb_ci_computer. Empty
keeps the connector's built-in map. A value REPLACES the built-in map rather
than extending it, so list every OS you want written.
''')
param classMap string = ''

@description('''
Mapping overrides as a JSON object, the same content as a local
MAPPING_OVERRIDES_FILE (e.g. {"drop": ["last_discovered"]}). Reaches the app as
MAPPING_OVERRIDES_JSON. Empty object = no overrides.
''')
param mappingOverrides object = {}

@description('Set false to skip the Azure Files share used for retirement state.')
param enableStatePersistence bool = true

@description('Retire CIs for devices that have disappeared from Intune.')
param retireMissingDevices bool = false

@description('Run without committing anything to the CMDB.')
param dryRun bool = false

// Both of these authenticate without a Key Vault secret, so neither creates one.
var useManagedIdentityForGraph = graphAuthMode == 'managed_identity'
var useFederatedIdentityForGraph = graphAuthMode == 'federated_managed_identity'
var graphNeedsSecret = graphAuthMode == 'client_secret'

var suffix = uniqueString(resourceGroup().id)
var identityName = '${namePrefix}-id'
var keyVaultName = take('${namePrefix}kv${suffix}', 24)
// Storage account names allow only lowercase letters and digits, unlike every
// other resource here, so a hyphenated prefix would otherwise fail on this one.
var storageSafePrefix = toLower(replace(replace(namePrefix, '-', ''), '_', ''))
var storageName = take('${storageSafePrefix}st${suffix}', 24)
var workspaceName = '${namePrefix}-logs'
var appInsightsName = '${namePrefix}-ai'
var planName = '${namePrefix}-plan'
// Function app names are global (<name>.azurewebsites.net), so the prefix alone
// would collide with any other tenant's intunecmdb. At most 32 characters.
var functionAppName = '${namePrefix}-fn-${take(suffix, 8)}'
var actionGroupName = '${namePrefix}-alerts'
var enableAlerts = !empty(alertEmail)
var packageContainerName = 'app-package'
var shareName = 'state'
var stateMountPath = '/mounts/state'

// A user-assigned identity, rather than system-assigned, so the Graph app-role
// grant survives the function app being deleted and recreated.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    // Logs are for troubleshooting a nightly run; 30 days is plenty and the
    // first 31 days of retention are free anyway.
    retentionInDays: 30
  }
}

// Workspace-based, so traces land in the workspace's AppTraces table, which is
// what the alert rules query. Local (key) auth is off: the app publishes with its
// managed identity.
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

resource serviceNowSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'servicenow-client-secret'
  properties: {
    value: serviceNowClientSecret
  }
}

resource graphSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' =
  if (graphNeedsSecret) {
    parent: vault
    name: 'graph-client-secret'
    properties: {
      value: graphClientSecret
    }
  }

// Built-in role IDs.
var secretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6' // Key Vault Secrets User
var blobDataOwnerRoleId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b' // Storage Blob Data Owner
var queueDataContributorRoleId = '974c5e8b-45b9-4653-ba55-5f855dd0fb88' // Storage Queue Data Contributor
var tableDataContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3' // Storage Table Data Contributor
var metricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb' // Monitoring Metrics Publisher

resource vaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, identity.id, secretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// One account per stack holds the deployment package, the Functions host's own
// storage (timer schedule status and the singleton lease), and the state share.
// It is always created: the host cannot run without it. Shared key access stays
// enabled because an Azure Files mount on Functions authenticates only with the
// account key; everything else here uses the managed identity.
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: packageContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' =
  if (enableStatePersistence) {
    parent: storage
    name: 'default'
  }

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' =
  if (enableStatePersistence) {
    parent: fileService
    name: shareName
    properties: {
      // The state file is a few hundred KB; this is the smallest quota allowed.
      shareQuota: 1
      enabledProtocols: 'SMB'
    }
  }

// AzureWebJobsStorage over the managed identity needs blob (package, leases),
// queue and table access; these are the roles Microsoft's Flex template grants.
resource storageRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in [blobDataOwnerRoleId, queueDataContributorRoleId, tableDataContributorRoleId]: {
    scope: storage
    name: guid(storage.id, identity.id, roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: identity.properties.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource appInsightsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, identity.id, metricsPublisherRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', metricsPublisherRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  kind: 'functionapp'
  sku: {
    tier: 'FlexConsumption'
    name: 'FC1'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    // Key Vault references in app settings resolve as this identity.
    keyVaultReferenceIdentity: identity.id
    siteConfig: {
      minTlsVersion: '1.2'
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${packageContainerName}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identity.id
          }
        }
      }
      scaleAndConcurrency: {
        // One instance, like the Lambda's reserved concurrency of 1: two runs
        // at once would race on state.json. A timer trigger is also a singleton
        // by default; this makes it structural.
        maximumInstanceCount: 1
        instanceMemoryMB: instanceMemoryMB
      }
      runtime: {
        name: 'python'
        version: pythonVersion
      }
    }
  }
}

// Key Vault reference syntax. The versionless URI follows rotations; the
// platform re-reads it within 24 hours or on restart.
var snowSecretRef = '@Microsoft.KeyVault(SecretUri=${serviceNowSecret.properties.secretUri})'

// GRAPH_TENANT_ID is the Intune tenant, which is not necessarily this
// subscription's tenant.
var graphEnvCommon = {
  GRAPH_TENANT_ID: graphTenantId
  INTUNE_OWNERSHIP: 'company'
}

// In managed-identity mode GRAPH_CLIENT_ID selects *which* identity to use.
var graphEnvManagedIdentity = {
  GRAPH_AUTH_MODE: 'managed_identity'
  GRAPH_CLIENT_ID: identity.properties.clientId
}

var graphEnvClientSecret = {
  GRAPH_AUTH_MODE: 'client_secret'
  GRAPH_CLIENT_ID: graphClientId
  GRAPH_CLIENT_SECRET: graphNeedsSecret
    ? '@Microsoft.KeyVault(SecretUri=${graphSecret!.properties.secretUri})'
    : ''
}

// GRAPH_CLIENT_ID is the multi-tenant APP's client ID; the identity that signs
// the assertion is named separately. Conflating the two is the easiest mistake
// to make here, and the resulting error does not say which one is wrong.
var graphEnvFederatedIdentity = {
  GRAPH_AUTH_MODE: 'federated_managed_identity'
  GRAPH_CLIENT_ID: graphClientId
  GRAPH_ASSERTION_IDENTITY_CLIENT_ID: identity.properties.clientId
}

var graphEnv = useManagedIdentityForGraph
  ? graphEnvManagedIdentity
  : (useFederatedIdentityForGraph ? graphEnvFederatedIdentity : graphEnvClientSecret)

// The Functions host itself: its storage and its telemetry, both over the
// managed identity.
var hostEnv = {
  AzureWebJobsStorage__accountName: storage.name
  AzureWebJobsStorage__credential: 'managedidentity'
  AzureWebJobsStorage__clientId: identity.properties.clientId
  APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.properties.ConnectionString
  APPLICATIONINSIGHTS_AUTHENTICATION_STRING: 'ClientId=${identity.properties.clientId};Authorization=AAD'
  // Read by function_app.py's timer trigger as %SYNC_SCHEDULE%.
  SYNC_SCHEDULE: schedule
}

var baseEnv = {
  SNOW_INSTANCE: serviceNowInstance
  SNOW_AUTH_MODE: 'oauth_client_credentials'
  SNOW_CLIENT_ID: serviceNowClientId
  SNOW_CLIENT_SECRET: snowSecretRef
  SNOW_WRITE_MODE: writeMode
  SNOW_DISCOVERY_SOURCE: discoverySource
  SNOW_RETIRE_MISSING: string(retireMissingDevices)
  DRY_RUN: string(dryRun)
  // Without this a run where every device failed still exits 0.
  FAIL_ON_ERROR: 'true'
  LOG_FORMAT: 'json'
  LOG_LEVEL: 'INFO'
}

// Omitted rather than set empty, so the connector's own defaults apply.
var mappingEnv = union(
  empty(classMap) ? {} : { SNOW_CLASS_MAP: classMap },
  empty(mappingOverrides) ? {} : { MAPPING_OVERRIDES_JSON: string(mappingOverrides) }
)

// The run report is the only per-device record of what happened. It lands on
// the same persistent share as the state file so it outlives the instance.
var stateEnv = enableStatePersistence
  ? {
      STATE_PATH: '${stateMountPath}/state.json'
      RUN_REPORT_PATH: '${stateMountPath}/run-report.json'
      RUN_REPORT_DEVICES: 'true'
    }
  : {}

// The complete settings list. Deploying 'appsettings' replaces every setting, so
// a change made in the portal or with `az functionapp config appsettings set`
// lasts only until the next deploy.sh.
resource appSettings 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: functionApp
  name: 'appsettings'
  properties: union(hostEnv, graphEnvCommon, graphEnv, baseEnv, mappingEnv, stateEnv)
  dependsOn: [
    // Key Vault references and identity-based host storage both fail to
    // resolve until the identity holds its roles.
    vaultAccess
    storageRoles
    appInsightsPublisher
  ]
}

resource stateMount 'Microsoft.Web/sites/config@2024-04-01' =
  if (enableStatePersistence) {
    parent: functionApp
    name: 'azurestorageaccounts'
    properties: {
      state: {
        type: 'AzureFiles'
        accountName: storage.name
        shareName: shareName
        mountPath: stateMountPath
        accessKey: storage.listKeys().keys[0].value
      }
    }
    dependsOn: [
      share
    ]
  }

// ---------------------------------------------------------------------------
// Alerting
//
// Log-based rather than metric-based: the run summary is a structured JSON line,
// and the things worth alerting on (did it run at all, did devices fail) are
// fields in it rather than platform metrics.
// ---------------------------------------------------------------------------

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (enableAlerts) {
  name: actionGroupName
  location: 'global'
  properties: {
    groupShortName: take(namePrefix, 12)
    enabled: true
    emailReceivers: [
      {
        name: 'primary'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

resource noRunAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' =
  if (enableAlerts) {
    name: '${namePrefix}-no-successful-run'
    location: location
    properties: {
      displayName: '${functionAppName}: no successful run in 24 hours'
      description: '''
The function has not logged a completed run in the last 24 hours. Either the schedule
stopped firing, or every attempt failed before finishing. The CMDB is going
stale and nothing else will tell you.
'''
      severity: 1
      enabled: true
      scopes: [ workspace.id ]
      // Checked hourly over a 24h window. A daily schedule always has a
      // completed run inside a 24h window when healthy, so a miss is real.
      evaluationFrequency: 'PT1H'
      windowSize: 'P1D'
      criteria: {
        allOf: [
          {
            // summarize with no `by` yields a row of 0 when nothing matched,
            // which is what makes absence detectable at all.
            //
            // format(), not interpolation: Bicep multi-line strings are verbatim,
            // and a literal app-name placeholder matches no app -- which made this rule
            // fire every hour and the one below never fire.
            query: format('''
AppTraces
| where AppRoleName == '{0}'
| extend p = parse_json(Message)
| where tostring(p.msg) == 'run complete'
| summarize completed = count()
''', functionAppName)
            timeAggregation: 'Total'
            metricMeasureColumn: 'completed'
            operator: 'LessThan'
            threshold: 1
            failingPeriods: {
              numberOfEvaluationPeriods: 1
              minFailingPeriodsToAlert: 1
            }
          }
        ]
      }
      autoMitigate: true
      actions: {
        actionGroups: [ actionGroup!.id ]
      }
    }
  }

resource deviceErrorAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' =
  if (enableAlerts) {
    name: '${namePrefix}-device-errors'
    location: location
    properties: {
      displayName: '${functionAppName}: run completed with device errors or degraded'
      description: '''
A run finished but individual devices failed to write, or it finished degraded:
the mass-retirement guard tripped or the state file could not be saved, either
of which leaves the next run unable to reason about the fleet. Read
run-report.json on the state share; the summary line carries error_samples and
the degraded conditions.
'''
      severity: 2
      enabled: true
      scopes: [ workspace.id ]
      evaluationFrequency: 'PT1H'
      windowSize: 'PT6H'
      criteria: {
        allOf: [
          {
            query: format('''
AppTraces
| where AppRoleName == '{0}'
| extend p = parse_json(Message)
| where tostring(p.msg) == 'run complete'
| extend failed = toint(p.errors), degraded = array_length(p.degraded)
| where failed > 0 or degraded > 0
| summarize problems = count()
''', functionAppName)
            timeAggregation: 'Total'
            metricMeasureColumn: 'problems'
            operator: 'GreaterThan'
            threshold: 0
            failingPeriods: {
              numberOfEvaluationPeriods: 1
              minFailingPeriodsToAlert: 1
            }
          }
        ]
      }
      autoMitigate: true
      actions: {
        actionGroups: [ actionGroup!.id ]
      }
    }
  }

output alertsEnabled bool = enableAlerts

@description('''
Client ID of the managed identity. In managed_identity mode this is the identity
that must hold the Graph application permissions. In client_secret mode it reads
Key Vault and host storage, and publishes telemetry.
''')
output managedIdentityClientId string = identity.properties.clientId

@description('Object ID of the managed identity service principal. Used for the app-role grant.')
output managedIdentityPrincipalId string = identity.properties.principalId

@description('How the app authenticates to Graph. deploy.sh grants app roles only in managed_identity mode.')
output graphAuthMode string = graphAuthMode

@description('True when Intune and this subscription are in different tenants.')
output crossTenant bool = graphTenantId != tenantId

output functionAppName string = functionApp.name
output resourceGroupName string = resourceGroup().name
output keyVaultName string = vault.name
output storageAccountName string = storage.name
