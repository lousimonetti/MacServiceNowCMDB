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
Use 'managed_identity' only when Intune is in the same tenant as this
subscription; a managed identity cannot be granted app roles in another tenant.
''')
@allowed([
  'client_secret'
  'managed_identity'
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

var useManagedIdentityForGraph = graphAuthMode == 'managed_identity'

var suffix = uniqueString(resourceGroup().id)
var identityName = '${namePrefix}-id'
var keyVaultName = take('${namePrefix}kv${suffix}', 24)
var storageName = take('${namePrefix}st${suffix}', 24)
var workspaceName = '${namePrefix}-logs'
var environmentName = '${namePrefix}-env'
var jobName = '${namePrefix}-job'
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
  if (!useManagedIdentityForGraph) {
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

var graphEnv = useManagedIdentityForGraph ? graphEnvManagedIdentity : graphEnvClientSecret

var baseEnv = [
  { name: 'SNOW_INSTANCE', value: serviceNowInstance }
  { name: 'SNOW_AUTH_MODE', value: 'oauth_client_credentials' }
  { name: 'SNOW_CLIENT_ID', value: serviceNowClientId }
  { name: 'SNOW_CLIENT_SECRET', secretRef: 'servicenow-client-secret' }
  { name: 'SNOW_WRITE_MODE', value: 'identify_reconcile' }
  { name: 'SNOW_DISCOVERY_SOURCE', value: discoverySource }
  { name: 'SNOW_RETIRE_MISSING', value: string(retireMissingDevices) }
  { name: 'DRY_RUN', value: string(dryRun) }
  { name: 'LOG_FORMAT', value: 'json' }
  { name: 'LOG_LEVEL', value: 'INFO' }
]

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
        useManagedIdentityForGraph
          ? []
          : [
              {
                name: 'graph-client-secret'
                keyVaultUrl: graphSecret!.properties.secretUri
                identity: identity.id
              }
            ]
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
          env: concat(graphEnvCommon, graphEnv, baseEnv, stateEnv)
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
