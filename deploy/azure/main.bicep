// Azure Container Apps Job that runs intune-cmdb-sync on a daily schedule.
//
// Cost shape (as of writing, East US, list prices):
//   Container Apps Jobs   first 180,000 vCPU-s + 360,000 GiB-s per month are free.
//                         A 5-minute daily run at 0.5 vCPU / 1 GiB uses roughly
//                         4,500 vCPU-s and 9,000 GiB-s per month, so it lands
//                         inside the free grant.                            $0.00
//   Log Analytics         first 5 GB ingested per month is free; this job
//                         produces a few MB.                                $0.00
//   Key Vault (standard)  no monthly fee; ~$0.03 per 10,000 operations and
//                         this reads two secrets a day.                     ~$0.00
//   Storage (Azure Files) a few hundred KB of state on a Standard LRS share. ~$0.06
//                                                                    -------------
//                                                              well under $1/month
//
// The container image is pulled from a public registry (GHCR by default), which
// avoids the ~$5/month an Azure Container Registry Basic tier would add. Point
// `containerImage` at your own ACR if your policy requires a private registry.
//
// TWO TOPOLOGIES, set by `graphAuthMode`:
//
//   'managed_identity'  Intune and this subscription live in the SAME tenant.
//                       The job's managed identity is granted the Graph
//                       application permissions directly and no Graph credential
//                       exists anywhere. Prefer this whenever it is available.
//
//   'client_secret'     Intune lives in a DIFFERENT tenant from this
//                       subscription (default). A managed identity is
//                       single-tenant and cannot be granted app roles in another
//                       directory, so the job authenticates as an app
//                       registration from the Intune tenant, with its secret held
//                       in Key Vault. The managed identity is still used - to
//                       read Key Vault, not to reach Graph.

targetScope = 'resourceGroup'

@description('Base name used to derive every resource name.')
@minLength(3)
@maxLength(18)
param namePrefix string = 'intunecmdb'

@description('Azure region. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Container image to run.')
param containerImage string = 'ghcr.io/your-org/intune-cmdb-sync:latest'

@description('Cron schedule in UTC. Default: 03:15 every day.')
param cronExpression string = '15 3 * * *'

@description('''
Email address for alerts. Leave empty to skip creating alert rules entirely.

Two rules are created when set:
  - no successful run in the last 24 hours
  - a run finished with device-level errors

The first matters more. A job that stops running is otherwise invisible: there
is no failure to notice, just a CMDB that quietly goes stale.
''')
param alertEmail string = ''

@description('ServiceNow instance: short name, host, or full https URL.')
param serviceNowInstance string

@description('ServiceNow OAuth client ID (client_credentials grant).')
param serviceNowClientId string

@description('ServiceNow OAuth client secret. Stored in Key Vault, never on the job.')
@secure()
param serviceNowClientSecret string

@description('Tenant of this subscription. Used for Key Vault, not for Graph.')
param tenantId string = subscription().tenantId

@description('''
How the job authenticates to Microsoft Graph.

'client_secret'              Works everywhere, including cross-tenant. A secret
                             in Key Vault that someone has to rotate.
'managed_identity'           No secret at all, but only when Intune is in the
                             SAME tenant as this subscription -- a managed
                             identity cannot be granted app roles in another
                             directory.
'federated_managed_identity' Secretless AND cross-tenant. This job's managed
                             identity is registered as a federated credential
                             on a multi-tenant app registration that has been
                             admin-consented into the Intune tenant. Requires
                             that setup to exist first; see docs/entra-setup.md.

'workload_identity' is deliberately absent: it needs a projected federated
token file, which AKS and GitHub Actions provide and Container Apps does not.
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

@description('App registration client secret. Stored in Key Vault, never on the job.')
@secure()
param graphClientSecret string = ''

@description('Discovery source name. Must match the sys_choice value on cmdb_ci.discovery_source.')
param discoverySource string = 'Intune'

@description('Set false to skip the Azure Files share used for retirement state.')
param enableStatePersistence bool = true

@description('Retire CIs for devices that have disappeared from Intune.')
param retireMissingDevices bool = false

@description('Run without committing anything to the CMDB.')
param dryRun bool = false

@description('vCPU per run. 0.5 is ample; the workload is IO-bound on two REST APIs.')
param cpu string = '0.5'

@description('Memory per run. Must pair with cpu per the Container Apps allowed combinations.')
param memory string = '1Gi'

@description('Seconds a single run may take before it is killed. 30 minutes covers very large tenants.')
param replicaTimeoutSeconds int = 1800

// Both of these authenticate without a Key Vault secret, so neither creates one.
var useManagedIdentityForGraph = graphAuthMode == 'managed_identity'
var useFederatedIdentityForGraph = graphAuthMode == 'federated_managed_identity'
var graphNeedsSecret = graphAuthMode == 'client_secret'

var suffix = uniqueString(resourceGroup().id)
var identityName = '${namePrefix}-id'
var keyVaultName = take('${namePrefix}kv${suffix}', 24)
var storageName = take('${namePrefix}st${suffix}', 24)
var workspaceName = '${namePrefix}-logs'
var environmentName = '${namePrefix}-env'
var jobName = '${namePrefix}-job'
var actionGroupName = '${namePrefix}-alerts'
var enableAlerts = !empty(alertEmail)
var shareName = 'state'
var storageMountName = 'statemount'
var stateMountPath = '/var/lib/intune-cmdb-sync'

// A user-assigned identity, rather than system-assigned, so the Graph app-role
// grant survives the job being deleted and recreated.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    // Logs are for troubleshooting a nightly job; 30 days is plenty and the
    // first 31 days of retention are free anyway.
    retentionInDays: 30
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

// Key Vault Secrets User — the minimum needed to read a secret value.
var secretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource vaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, identity.id, secretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      secretsUserRoleId
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = if (enableStatePersistence) {
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

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
  }
}

resource environmentStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' =
  if (enableStatePersistence) {
    parent: environment
    name: storageMountName
    properties: {
      azureFile: {
        accountName: storage!.name
        accountKey: storage!.listKeys().keys[0].value
        shareName: shareName
        accessMode: 'ReadWrite'
      }
    }
  }

// GRAPH_TENANT_ID is the Intune tenant, which is not necessarily this
// subscription's tenant.
var graphEnvCommon = [
  { name: 'GRAPH_TENANT_ID', value: graphTenantId }
  { name: 'INTUNE_OWNERSHIP', value: 'company' }
]

// In managed-identity mode GRAPH_CLIENT_ID selects *which* identity to use.
var graphEnvManagedIdentity = [
  { name: 'GRAPH_AUTH_MODE', value: 'managed_identity' }
  { name: 'GRAPH_CLIENT_ID', value: identity.properties.clientId }
]

var graphEnvClientSecret = [
  { name: 'GRAPH_AUTH_MODE', value: 'client_secret' }
  { name: 'GRAPH_CLIENT_ID', value: graphClientId }
  { name: 'GRAPH_CLIENT_SECRET', secretRef: 'graph-client-secret' }
]

// GRAPH_CLIENT_ID is the multi-tenant APP's client ID; the identity that signs
// the assertion is named separately. Conflating the two is the easiest mistake
// to make here, and the resulting error does not say which one is wrong.
var graphEnvFederatedIdentity = [
  { name: 'GRAPH_AUTH_MODE', value: 'federated_managed_identity' }
  { name: 'GRAPH_CLIENT_ID', value: graphClientId }
  { name: 'GRAPH_ASSERTION_IDENTITY_CLIENT_ID', value: identity.properties.clientId }
]

var graphEnv = useManagedIdentityForGraph
  ? graphEnvManagedIdentity
  : (useFederatedIdentityForGraph ? graphEnvFederatedIdentity : graphEnvClientSecret)

var baseEnv = [
  { name: 'SNOW_INSTANCE', value: serviceNowInstance }
  { name: 'SNOW_AUTH_MODE', value: 'oauth_client_credentials' }
  { name: 'SNOW_CLIENT_ID', value: serviceNowClientId }
  { name: 'SNOW_CLIENT_SECRET', secretRef: 'servicenow-client-secret' }
  { name: 'SNOW_WRITE_MODE', value: 'identify_reconcile' }
  { name: 'SNOW_DISCOVERY_SOURCE', value: discoverySource }
  { name: 'SNOW_RETIRE_MISSING', value: string(retireMissingDevices) }
  { name: 'DRY_RUN', value: string(dryRun) }
  // Without this a run where every device failed still exits 0.
  { name: 'FAIL_ON_ERROR', value: 'true' }
  { name: 'LOG_FORMAT', value: 'json' }
  { name: 'LOG_LEVEL', value: 'INFO' }
]

// The run report is the only per-device record of what happened. It lands on
// the same persistent share as the state file so it outlives the job replica.
var reportEnv = enableStatePersistence
  ? [
      { name: 'RUN_REPORT_PATH', value: '${stateMountPath}/run-report.json' }
      { name: 'RUN_REPORT_DEVICES', value: 'true' }
    ]
  : []

var stateEnv = enableStatePersistence
  ? [ { name: 'STATE_PATH', value: '${stateMountPath}/state.json' } ]
  : []

resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: replicaTimeoutSeconds
      // A failed run is retried once; the next scheduled run picks up anyway,
      // and IRE writes are idempotent so a retry cannot double-create CIs.
      replicaRetryLimit: 1
      scheduleTriggerConfig: {
        cronExpression: cronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: concat(
        [
          {
            name: 'servicenow-client-secret'
            keyVaultUrl: serviceNowSecret.properties.secretUri
            identity: identity.id
          }
        ],
        graphNeedsSecret
          ? [
              {
                name: 'graph-client-secret'
                keyVaultUrl: graphSecret!.properties.secretUri
                identity: identity.id
              }
            ]
          : []
      )
    }
    template: {
      containers: [
        {
          name: 'sync'
          image: containerImage
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: concat(graphEnvCommon, graphEnv, baseEnv, stateEnv, reportEnv)
          volumeMounts: enableStatePersistence
            ? [ { volumeName: 'state', mountPath: stateMountPath } ]
            : []
        }
      ]
      volumes: enableStatePersistence
        ? [
            {
              name: 'state'
              storageType: 'AzureFile'
              storageName: storageMountName
            }
          ]
        : []
    }
  }
  dependsOn: [
    vaultAccess
    environmentStorage
  ]
}

@description('''
Client ID of the managed identity. In managed_identity mode this is the identity
that must hold the Graph application permissions. In client_secret mode it is
only used to read Key Vault.
''')
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
      displayName: '${jobName}: no successful run in 24 hours'
      description: '''
The job has not logged a completed run in the last 24 hours. Either the schedule
stopped firing, or every attempt failed before finishing. The CMDB is going
stale and nothing else will tell you.
'''
      severity: 1
      enabled: true
      scopes: [ workspace.id ]
      // Checked hourly over a 24h window. A daily 03:15 schedule always has a
      // completed run inside a 24h window when healthy, so a miss is real.
      evaluationFrequency: 'PT1H'
      windowSize: 'P1D'
      criteria: {
        allOf: [
          {
            // summarize with no `by` yields a row of 0 when nothing matched,
            // which is what makes absence detectable at all.
            query: '''
ContainerAppConsoleLogs_CL
| where ContainerJobName_s == '${jobName}'
| extend p = parse_json(Log_s)
| where tostring(p.msg) == 'run complete'
| summarize completed = count()
'''
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
      displayName: '${jobName}: run completed with device errors'
      description: '''
A run finished but individual devices failed to write. Read the per-device
outcomes in run-report.json on the state share; error_samples in the summary
line carries the first twenty.
'''
      severity: 2
      enabled: true
      scopes: [ workspace.id ]
      evaluationFrequency: 'PT1H'
      windowSize: 'PT6H'
      criteria: {
        allOf: [
          {
            query: '''
ContainerAppConsoleLogs_CL
| where ContainerJobName_s == '${jobName}'
| extend p = parse_json(Log_s)
| where tostring(p.msg) == 'run complete'
| extend failed = toint(p.errors)
| where failed > 0
| summarize failed = sum(failed)
'''
            timeAggregation: 'Total'
            metricMeasureColumn: 'failed'
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

output managedIdentityClientId string = identity.properties.clientId

@description('Object ID of the managed identity service principal. Used for the app-role grant.')
output managedIdentityPrincipalId string = identity.properties.principalId

@description('How the job authenticates to Graph. deploy.sh grants app roles only in managed_identity mode.')
output graphAuthMode string = graphAuthMode

@description('True when Intune and this subscription are in different tenants.')
output crossTenant bool = graphTenantId != tenantId

output jobName string = job.name
output resourceGroupName string = resourceGroup().name
output keyVaultName string = vault.name
