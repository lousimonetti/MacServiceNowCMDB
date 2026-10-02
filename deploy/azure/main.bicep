// Azure App Service app whose scheduled (triggered) WebJob runs
// intune-cmdb-sync once a day.
//
// Inbound is private: vpcx-lzn-app-service-deny-public-network denies any app
// with public network access, so the app's only way in is one private endpoint
// in an existing landing-zone subnet. That affects deploying and managing the
// WebJob (deploy.sh and webjob.sh reach Kudu at the endpoint's private IP), not
// the job: it only makes outbound calls, which public-access-disabled leaves
// alone, so no VNet integration is needed.
//
// Why App Service: the landing-zone policy denies Azure Container Registry and
// Container Apps, and denies public network access to Key Vault and Storage.
// Every Azure Functions app needs a storage account, so Functions would need
// VNet integration and private endpoints that the landing zone's network has no
// room for. A Linux App Service app needs no storage account of ours: code and
// files live on its built-in persistent /home. Nothing here is a Key Vault or a
// storage account, so neither policy applies, and the app needs no VNet.
//
// The trade-off is secrets: the ServiceNow client secret (and the Graph secret
// in client_secret mode) are app settings, not Key Vault references. App
// settings are encrypted at rest, but anyone who can read the app's
// configuration can read them. Prefer graphAuthMode=managed_identity where the
// tenant allows it, so the ServiceNow secret is the only one.
//
// Cost shape (list prices, East US):
//   App Service plan B1   Linux, 1 instance. Basic is the lowest tier with
//                         "Always On", which scheduled WebJobs need.   ~$13.14
//   Application Insights  workspace-based; a few MB a month, inside the 5 GB
//   + Log Analytics       free allowance.                               $0.00
//   Private endpoint      one, for the app's inbound.                    ~$7.30
//                                                                    ---------
//                                                                 ~$20/month
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
//                       registration from the Intune tenant, with its secret in
//                       app settings.

targetScope = 'resourceGroup'

// One prefix per ServiceNow environment (e.g. intunecmdb-dev, intunecmdb-prod)
// gives each a fully separate stack in the same resource group. See README.md.
@description('Base name used to derive every resource name.')
@minLength(3)
@maxLength(18)
param namePrefix string = 'intunecmdb'

@description('Azure region. deploy.sh passes eastus.')
param location string = resourceGroup().location

@description('''
Email address for alerts. Leave empty to skip creating alert rules entirely.

Two rules are created when set:
  - no successful run in the last 24 hours
  - a run finished with device-level errors, or degraded

The first matters more. A job that stops running is otherwise invisible: there
is no failure to notice, just a CMDB that quietly goes stale.
''')
param alertEmail string = ''

@description('''
Resource ID of an existing subnet for the app's inbound private endpoint (in DEV,
hybridsubnet-1 of vpcx-vnet-eastus). vpcx-lzn-app-service-deny-public-network
requires public network access disabled, so this endpoint is the only way in --
for deploys and for managing the WebJob, not for the job itself, which only
makes outbound calls.
''')
param privateEndpointSubnetId string

@description('ServiceNow instance: short name, host, or full https URL.')
param serviceNowInstance string

@description('ServiceNow OAuth client ID (client_credentials grant).')
param serviceNowClientId string

@description('ServiceNow OAuth client secret. Becomes an app setting; see the header for why not Key Vault.')
@secure()
param serviceNowClientSecret string

@description('Tenant of this subscription. Compared with graphTenantId to report a cross-tenant setup.')
param tenantId string = subscription().tenantId

@description('''
How the app authenticates to Microsoft Graph.

'client_secret'              Works everywhere, including cross-tenant. A secret
                             in app settings that someone has to rotate.
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

@description('App registration client secret. Becomes an app setting in client_secret mode only.')
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

@description('Retire CIs for devices that have disappeared from Intune.')
param retireMissingDevices bool = false

@description('Run without committing anything to the CMDB.')
param dryRun bool = false

var useManagedIdentityForGraph = graphAuthMode == 'managed_identity'
var useFederatedIdentityForGraph = graphAuthMode == 'federated_managed_identity'

var suffix = uniqueString(resourceGroup().id)
var identityName = '${namePrefix}-id'
var workspaceName = '${namePrefix}-logs'
var appInsightsName = '${namePrefix}-ai'
var planName = '${namePrefix}-plan'
// Web app names are global (<name>.azurewebsites.net), so the prefix alone
// would collide with any other tenant's intunecmdb.
var webAppName = '${namePrefix}-app-${take(suffix, 8)}'
var actionGroupName = '${namePrefix}-alerts'
var enableAlerts = !empty(alertEmail)
// /home is the app's built-in persistent storage: it survives restarts,
// redeploys and instance moves, and is not a storage account of ours.
var dataDir = '/home/data/intune-cmdb-sync'

// A user-assigned identity, rather than system-assigned, so the Graph app-role
// grant survives the app being deleted and recreated.
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
// what the alert rules query. Local (key) auth is off: the job publishes as the
// managed identity (appservice_job.py).
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

var metricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb' // Monitoring Metrics Publisher

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
  kind: 'linux'
  sku: {
    name: 'B1'
    tier: 'Basic'
  }
  properties: {
    reserved: true // Linux
  }
}

resource webApp 'Microsoft.Web/sites@2024-04-01' = {
  name: webAppName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    // vpcx-lzn-app-service-deny-public-network. Covers the site and its Kudu
    // (scm) endpoint; reach both through the private endpoint below.
    publicNetworkAccess: 'Disabled'
    siteConfig: {
      // The WebJob runs with the app's own Python; deploy.sh builds the
      // package's wheels for this same version.
      linuxFxVersion: 'PYTHON|3.12'
      // Scheduled WebJobs stop firing when the app idles out without this.
      alwaysOn: true
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      remoteDebuggingEnabled: false
      http20Enabled: true
    }
  }
}

// No FTP and no basic-auth deploys. deploy.sh deploys with the caller's Entra
// token through `az webapp deploy`.
resource ftpPublishing 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: webApp
  name: 'ftp'
  properties: { allow: false }
}

// The app's only inbound path. The 'sites' group covers both the site and its
// scm endpoint. DNS records are left to the landing zone's central policy;
// deploy.sh does not rely on them, because the deploying laptop's DNS (Zscaler)
// does not resolve privatelink zones -- it connects to the private IP directly.
resource appPrivateEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: '${namePrefix}-app-pe'
  location: location
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'sites'
        properties: {
          privateLinkServiceId: webApp.id
          groupIds: [ 'sites' ]
        }
      }
    ]
  }
}

resource scmPublishing 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: webApp
  name: 'scm'
  properties: { allow: false }
}

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
  GRAPH_CLIENT_SECRET: graphClientSecret
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

// The host itself: where code and data live, how the WebJob runs, and where
// its telemetry goes.
var hostEnv = {
  // The package ships its own Linux wheels; no build on deploy.
  SCM_DO_BUILD_DURING_DEPLOYMENT: 'false'
  // Keep /home persistent (the default for code apps; explicit because state
  // and the run report depend on it).
  WEBSITES_ENABLE_APP_SERVICE_STORAGE: 'true'
  // Linux WebJobs run through the Kudu agent.
  WEBSITE_SKIP_RUNNING_KUDUAGENT: 'false'
  // A triggered WebJob is killed after this many seconds without output or CPU
  // (default 120). A sync spends long stretches waiting on two REST APIs.
  WEBJOBS_IDLE_TIMEOUT: '1800'
  APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.properties.ConnectionString
  // The identity that publishes telemetry (local auth is off).
  AZURE_CLIENT_ID: identity.properties.clientId
  // Becomes AppRoleName on every trace, which the alert rules filter on.
  OTEL_SERVICE_NAME: webAppName
}

var baseEnv = {
  SNOW_INSTANCE: serviceNowInstance
  SNOW_AUTH_MODE: 'oauth_client_credentials'
  SNOW_CLIENT_ID: serviceNowClientId
  SNOW_CLIENT_SECRET: serviceNowClientSecret
  SNOW_WRITE_MODE: writeMode
  SNOW_DISCOVERY_SOURCE: discoverySource
  SNOW_RETIRE_MISSING: string(retireMissingDevices)
  DRY_RUN: string(dryRun)
  // Without this a run where every device failed still exits 0.
  FAIL_ON_ERROR: 'true'
  LOG_FORMAT: 'json'
  LOG_LEVEL: 'INFO'
  // Retirement state and the per-device run report, both on /home.
  STATE_PATH: '${dataDir}/state.json'
  RUN_REPORT_PATH: '${dataDir}/run-report.json'
  RUN_REPORT_DEVICES: 'true'
}

// Omitted rather than set empty, so the connector's own defaults apply.
var mappingEnv = union(
  empty(classMap) ? {} : { SNOW_CLASS_MAP: classMap },
  empty(mappingOverrides) ? {} : { MAPPING_OVERRIDES_JSON: string(mappingOverrides) }
)

// The complete settings list. Deploying 'appsettings' replaces every setting, so
// a change made in the portal or with `az webapp config appsettings set` lasts
// only until the next deploy.sh.
resource appSettings 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: webApp
  name: 'appsettings'
  properties: union(hostEnv, graphEnvCommon, graphEnv, baseEnv, mappingEnv)
  dependsOn: [
    // Telemetry is refused until the identity holds its role.
    appInsightsPublisher
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
      displayName: '${webAppName}: no successful run in 24 hours'
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
''', webAppName)
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
      displayName: '${webAppName}: run completed with device errors or degraded'
      description: '''
A run finished but individual devices failed to write, or it finished degraded:
the mass-retirement guard tripped or the state file could not be saved, either
of which leaves the next run unable to reason about the fleet. The summary
line carries error_samples and the degraded conditions; run-report.json in
/home/data/intune-cmdb-sync has the per-device detail.
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
''', webAppName)
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
that must hold the Graph application permissions. It always publishes the
job's telemetry.
''')
output managedIdentityClientId string = identity.properties.clientId

@description('Object ID of the managed identity service principal. Used for the app-role grant.')
output managedIdentityPrincipalId string = identity.properties.principalId

@description('How the app authenticates to Graph. deploy.sh grants app roles only in managed_identity mode.')
output graphAuthMode string = graphAuthMode

@description('True when Intune and this subscription are in different tenants.')
output crossTenant bool = graphTenantId != tenantId

output webAppName string = webApp.name
output privateEndpointName string = appPrivateEndpoint.name
output resourceGroupName string = resourceGroup().name
