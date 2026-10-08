# Azure deployment

An Azure App Service web app with one **scheduled WebJob** that runs the sync
once a day. `deploy.sh` builds one self-contained stack per ServiceNow
environment. Re-running it is how you change anything.

**Why App Service.** The landing zone's policies decide the shape:

- Azure Container Registry and Container Apps are denied, so there is no
  container image.
- Key Vault and Storage are allowed only with public network access denied.
  Every Azure Functions app needs a storage account, so Functions would have to
  join the landing zone's network through VNet integration and private
  endpoints. That network had no room for it. (That version is kept on the
  `azure-functions-vnet` branch.)
- An App Service app needs **no storage account of ours**. Its code and files
  live on the app's built-in persistent `/home`. Secrets go in app settings
  rather than Key Vault (see [Secrets](#secrets)). With neither a storage
  account nor a Key Vault, neither restricted policy applies. The job calls
  Graph and ServiceNow over App Service's normal outbound internet access.
- App Service itself must have **public network access disabled**
  (`vpcx-lzn-app-service-deny-public-network`). That blocks inbound only, so the
  job runs unaffected. But deploying code and managing the WebJob go *into* the
  app, through its management endpoint (Kudu). So the app gets **one private
  endpoint** in an existing landing-zone subnet (DEV: `hybridsubnet-1`), and
  `deploy.sh` and `webjob.sh` reach Kudu through it. See
  [Private access](#private-access).

`deploy.sh` builds a zip on your machine: the connector, its Linux wheels, and
the WebJob. It uploads the zip to the app through the private endpoint. The root `Dockerfile` is
still there for the AWS ECS host, but the Azure path does not use it.

This page is laid out in the order you do things. Read **Before you run
anything** first, even if you have deployed before.

## Safe rollout at a glance

Every stage has a gate. Do not start a stage until the one before it has passed.

| # | Stage | Where | Writes to the CMDB? | Gate to move on |
| --- | --- | --- | --- | --- |
| 1 | [Prerequisites](#1-prerequisites) | Azure, Entra, ServiceNow | No | Everything in the checklist exists |
| 2 | [Prove ServiceNow from your workstation](#2-prove-servicenow-from-your-workstation) | Local | One choice-list row, if you register the source | `--check-api` and `--check` exit 0 |
| 3 | [Check policy](#3-check-policy) | Azure | No | Nothing in the template is known to be refused |
| 4 | [Deploy in dry-run](#4-deploy-in-dry-run) | Azure | No | `deploy.sh` finishes, WebJob registered |
| 5 | [Trigger and verify a dry run](#5-trigger-and-verify-a-dry-run) | Azure | No | Run succeeded, `run complete` with `errors: 0` |
| 6 | [First real write, limited](#6-first-real-write-limited) | Local | Yes, about 5 CIs | The CIs look right in ServiceNow |
| 7 | [Go live](#7-go-live) | Azure | Yes, whole fleet | First scheduled run is clean |
| 8 | [Enable retirement](#8-enable-retirement-optional-later) (optional, later) | Azure | Yes, retires CIs | Several clean live runs |

With more than one ServiceNow instance, walk DEV through the whole table before
PROD starts it. See [Multiple ServiceNow environments](#multiple-servicenow-environments).

## Before you run anything

Five behaviours, each able to cause a bad write or a silent failure:

1. **The schedule is armed from the moment the deploy finishes.** Nothing waits
   for you to trigger a first run. A deploy at 02:00 UTC with the default
   schedule runs at 03:15.
2. **`deploy.sh` is the source of truth for the app's settings.** The Bicep
   template writes the complete app-settings list, so each redeploy discards any
   change made in the portal or with `az webapp config appsettings set`. Treat
   those as temporary overrides only (see [Emergency stop](#emergency-stop)).
3. **`deploy.sh` deploys the code in your checkout.** Every run rebuilds the
   package from the working tree, so check out the revision you mean to ship
   first. The summary prints it as `Source` (`git describe`, with `-dirty` when
   there are uncommitted changes).
4. **Only some local settings carry over.** The app receives only the
   variables listed in
   [What the app is configured with](#what-the-app-is-configured-with).
   `SNOW_CLASS_MAP` and `MAPPING_OVERRIDES_FILE` reach it **if you set them
   when running `deploy.sh`**. Everything else in your local `.env` does not.
   Without the overrides file, `last_discovered` goes out on every run and
   every device reports UPDATE from the second run on. That is noise, not
   damage. Put the settings you tested locally into the environment file in
   stage 4.
5. **Secrets passed on the command line go into shell history.** Read them
   with `read -rs` as the examples do, not `export X=secret`.

## 1. Prerequisites

**Tools on the machine that deploys:** `az` (logged in), `jq`, `zip`, a
`python3` with `pip`, and this repo. Any OS works: the package's wheels are
fetched for Linux x86_64 explicitly, whatever the build machine is. The build
downloads those wheels from PyPI on **your machine**; nothing is built in Azure.

**Behind Zscaler or another TLS-inspecting proxy?** `az` and `pip` fail with
`SSL: CERTIFICATE_VERIFY_FAILED` until they trust the proxy's root. Set
`CA_BUNDLE=macos-keychain` when running `deploy.sh`. See
[Behind a TLS-inspecting proxy](#behind-a-tls-inspecting-proxy-zscaler).

**Check the subscription before anything else.** `deploy.sh` deploys into
whichever subscription `az` currently points at:

```bash
az account show --query "{subscription:name, id:id, tenant:tenantId}" -o table
az account set --subscription <name or id>    # if it is the wrong one
```

**These must already exist.** `deploy.sh` creates none of them:

- [ ] **The resource group.** In the landing zone, groups are provisioned for
  you: DEV deploys into **`azc-obm-development`**. If `RESOURCE_GROUP` is
  unset, `deploy.sh` lists the groups in the current subscription and asks you
  to pick one by number. It checks the group exists, and never creates one.
  There is no default group.
- [ ] **A Graph credential that matches your tenant topology.** See
  [Choosing Graph authentication](#choosing-graph-authentication) below and
  [docs/entra-setup.md](../../docs/entra-setup.md).
- [ ] **A ServiceNow OAuth client** for each instance, with the roles in
  [docs/servicenow-setup.md](../../docs/servicenow-setup.md) sections 1-4.
- [ ] **The discovery source registered** on each instance
  (`servicenow-setup.md` section 5, or `--register-discovery-source` in stage 2).
  Until it is, every write is rejected.

- [ ] **A subnet for the app's private endpoint**, and a route to it from
  your machine. In DEV that is `hybridsubnet-1` of `vpcx-vnet-eastus` (in
  `VPCXRG`), which already holds the vault and storage endpoints of the
  landing zone. Your Mac reaches its addresses through Zscaler Private
  Access, tested on 2026-10-02. See [Private access](#private-access).

No VNet integration, new subnet or DNS change is needed.

**Roles the person deploying needs:**

| Action | Needs |
| --- | --- |
| Deploy, and assign the stack's identity its role | Owner, or Contributor plus User Access Administrator (or Role Based Access Control Administrator), on the resource group |
| Put the private endpoint in the subnet | `Microsoft.Network/virtualNetworks/subnets/join/action` on that subnet (in Network Contributor), which lives in `VPCXRG`, not your resource group |
| `GRAPH_AUTH_MODE=managed_identity` (the script grants Graph app roles) | Privileged Role Administrator, Cloud Application Administrator, or Global Administrator in the subscription's tenant |

The template gives the stack's managed identity permission to publish to
Application Insights, so plain Contributor is not enough.

### Finding the private endpoint subnet

`PRIVATE_ENDPOINT_SUBNET_ID` is the Azure resource ID of a subnet in a
landing-zone VNet. It is usually **not** in your own resource group, and it can
differ per environment, so look it up against the subscription you are deploying
into.

```bash
# 1. Be in the right subscription.
az account show --query "{name:name, id:id, tenant:tenantId}" -o table

# 2. Find the VNet and its subnets. DEV used vpcx-vnet-eastus in VPCXRG.
az network vnet list \
  --query "[].{name:name, rg:resourceGroup, location:location, space:addressSpace.addressPrefixes[0]}" -o table
az network vnet subnet list -g <vnet-rg> --vnet-name <vnet-name> \
  --query "[].{name:name, prefix:addressPrefix, privateEndpointPolicies:privateEndpointNetworkPolicies}" -o table

# 3. Print the ID of the one you chose, and put it in the environment file.
az network vnet subnet show -g <vnet-rg> --vnet-name <vnet-name> -n <subnet-name> --query id -o tsv
```

The ID looks like
`/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<subnet>`.

Choosing the subnet:

- It must be in East US and have a free address: the endpoint takes one IP.
- Prefer the subnet that already holds the landing zone's other private
  endpoints (DEV: `hybridsubnet-1`). If you do not know which, ask the network
  team.
- Your machine must be able to reach that subnet's addresses, through Zscaler
  Private Access or the VPN, because `kudu.sh` deploys through the endpoint.
- You need `Microsoft.Network/virtualNetworks/subnets/join/action` on it (part of
  Network Contributor), which is often granted on the VNet's resource group and
  not yours. Check it with:

  ```bash
  az role assignment list --assignee "$(az ad signed-in-user show --query id -o tsv)" \
    --scope <subnet-id> --include-inherited -o table
  ```

If `az network vnet list` returns nothing, your login cannot see the network
subscription or resource group. Ask the network team for the subnet ID and the
join permission.

### Choosing Graph authentication

This choice decides the credential model. A wrong choice produces a deployment
that fails only when the job runs at 3am.

```bash
az account show --query tenantId -o tsv   # the subscription's tenant
```

Compare that value with the tenant where **Intune** lives.

| Topology | `GRAPH_AUTH_MODE` | Graph secret? | Notes |
| --- | --- | --- | --- |
| Same tenant | `managed_identity` | None | Best case. `deploy.sh` grants the identity the Graph app roles itself. |
| Different tenants | `client_secret` (default) | App registration secret from the **Intune** tenant, stored as an app setting | The managed identity only publishes telemetry. |
| Different tenants, secretless | `federated_managed_identity` | None | Must be set up in two passes, described below |

`deploy.sh` compares the two tenants and refuses `managed_identity` when they
differ. A managed identity is single-tenant and there is no consent path that
grants it app roles in another directory.

**`federated_managed_identity`** needs a multi-tenant app registration in the
subscription's tenant. The app's managed identity is added to that app
registration as a [federated identity credential][fic], and the app
registration is admin-consented into the Intune tenant. The federated
credential must name the managed identity, and this template is what creates
the identity, so the setup takes two passes:

1. Deploy in `client_secret` mode.
2. Create the federated credential against the identity that deploy produced.
   Its `subject` is the identity's **principal (object) ID**, not its client ID.
3. Redeploy with `GRAPH_AUTH_MODE=federated_managed_identity`.

`deploy.sh` cannot check the other tenant's side of this, so trigger a run
manually after step 3. Be realistic about the cost: this is several moving
parts across two directories, and it saves you from rotating one secret.

`workload_identity` is not offered: it needs a projected federated token file,
which AKS and GitHub Actions provide and App Service does not.

[fic]: https://learn.microsoft.com/entra/workload-id/workload-identity-federation-config-app-trust-managed-identity

## 2. Prove ServiceNow from your workstation

Which write API an OAuth client may call is set per instance and per HTTP
method. The only reliable way to know is to probe it. Do that from your
workstation, where a failure is one command and not a scheduled run's log.
Configure a local `.env` as in the [top-level README](../../README.md#quick-start),
with the **same instance and OAuth client** the job will use.

```bash
set -a && . ./.env && set +a

# Which endpoints does this client clear the REST gate for? Writes nothing.
# Exit 3 = the configured SNOW_WRITE_MODE is refused. Pick the mode it reports
# as allowed and use that for SNOW_WRITE_MODE when deploying.
intune-cmdb-sync --check-api

# Only if --check reports the discovery source missing. Writes one sys_choice row.
intune-cmdb-sync --register-discovery-source

# Both connections, plus a simulated write that commits nothing.
intune-cmdb-sync --check

# Full read, no commit. Read what it would do to five devices.
intune-cmdb-sync --dry-run --limit 5 --report-devices --report ./run.json
```

Gate: `--check-api` and `--check` both exit 0, and `run.json` shows the
insert/update split you expect. Record the `SNOW_WRITE_MODE` that worked, plus
any `SNOW_CLASS_MAP` and `MAPPING_OVERRIDES_FILE` you used. All three go into
the environment file in stage 4.

## 3. Check policy

The landing zone's policies are deny rules checked when the template is
submitted. This stack creates only these resource types:

| Resource type | Status as last recorded |
| --- | --- |
| `Microsoft.Web/serverfarms`, `Microsoft.Web/sites` (plus `sites/config`, `sites/basicPublishingCredentialsPolicies`) | Approved, **with public network access disabled** (`vpcx-lzn-app-service-deny-public-network`); the template sets it |
| `Microsoft.Network/privateEndpoints` | The app's one inbound path. Not yet confirmed by a preflight; the landing zone's own vault and storage use them in the same subnet |
| `Microsoft.ManagedIdentity/userAssignedIdentities` | Allowed |
| `Microsoft.OperationalInsights/workspaces` | Allowed |
| `Microsoft.Insights/components` (Application Insights) | Probably allowed: the 2026-10-02 preflight refused only the vault and storage account, and it reports every refusal at once |
| `Microsoft.Insights/actionGroups`, `Microsoft.Insights/scheduledQueryRules` | Allowed |

There is no Key Vault and no storage account, the two types whose
public-access policies blocked the Functions stack.

The 2026-10-02 App Service preflight refused only public network access, and it
reports every refusal at once. The template also sets the other usual
landing-zone requirements: HTTPS only, TLS 1.2, FTP off, remote debugging off
and basic-auth publishing off. If something else refuses later, the cost is
low: ARM evaluates deny policies during preflight, before it creates any
resource, so `deploy.sh` stops with `RequestDisallowedByPolicy` and names the
refused property.

Gate: nothing in the table is known to be refused.

## 4. Deploy in dry-run

Use a small per-environment file for the non-secret settings, so a redeploy
reuses exactly the values you tested. Keep it out of git.

```bash
# dev.env: no secrets in this file
NAME_PREFIX=intunecmdb-dev
RESOURCE_GROUP=azc-obm-development     # pre-existing, never created; omit to pick from a list
# The app's private endpoint goes here. Get the ID with:
#   az network vnet subnet show -g VPCXRG --vnet-name vpcx-vnet-eastus -n hybridsubnet-1 --query id -o tsv
PRIVATE_ENDPOINT_SUBNET_ID=/subscriptions/<sub>/resourceGroups/VPCXRG/providers/Microsoft.Network/virtualNetworks/vpcx-vnet-eastus/subnets/hybridsubnet-1
# LOCATION is not set: resources go to East US (eastus) by default
CA_BUNDLE=macos-keychain               # behind Zscaler; see "Behind a TLS-inspecting proxy"
SNOW_INSTANCE=acmedev
SNOW_CLIENT_ID=<from the Application Registry entry>
SNOW_WRITE_MODE=identify_reconcile     # the mode --check-api allowed
SNOW_DISCOVERY_SOURCE=Intune           # exactly as registered, including case
SCHEDULE="0 15 3 * * *"                # six-field NCRONTAB, seconds first, UTC
ALERT_EMAIL=ops@example.com            # strongly recommended; see Alerting
DRY_RUN=true                           # required, no default: true or false
RETIRE_MISSING=false

# Carry over what you tested locally. Both optional.
SNOW_CLASS_MAP="windows=cmdb_ci_computer;macos=cmdb_ci_computer"  # replaces the built-in map
MAPPING_OVERRIDES_FILE=./mapping-overrides.json                   # a local path; deploy.sh ships its contents

# Graph: same tenant as Intune
GRAPH_AUTH_MODE=managed_identity
# ...or a different tenant: an app registration from the INTUNE tenant
# GRAPH_AUTH_MODE=client_secret
# GRAPH_TENANT_ID=<intune tenant id>
# GRAPH_CLIENT_ID=<app registration client id>
```

```bash
set -a && . ./dev.env && set +a

# Secrets are prompted for, so they stay out of files and shell history.
read -rs -p "ServiceNow client secret: " SNOW_CLIENT_SECRET; echo; export SNOW_CLIENT_SECRET
# client_secret mode only:
# read -rs -p "Graph client secret: " GRAPH_CLIENT_SECRET; echo; export GRAPH_CLIENT_SECRET

./deploy.sh
```

What it does, in order:

1. Validates every input. It refuses to run unless `DRY_RUN` is exactly `true`
   or `false`. It also rejects a five-field `SCHEDULE`, or the old `CRON`
   variable, because the WebJob scheduler would reject it only after
   deployment.
2. Checks TLS to Azure and PyPI, so a Zscaler interception fails here with one
   line rather than halfway through.
3. Builds the zip locally, before anything in Azure changes:
   `App_Data/jobs/triggered/intune-cmdb-sync/` holds `run.py` and a
   `settings.job` with your schedule, and `packages/` holds the connector and
   its Linux wheels.
4. Deploys the infrastructure (`main.bicep`).
5. Reaches the app's Kudu through its private endpoint, retrying for a few
   minutes while a brand-new endpoint and app come up, then uploads the zip
   with your Entra login (basic-auth publishing is off). See
   [Private access](#private-access).
6. **Checks that the WebJob registered.** A job in the wrong folder never runs
   and never errors, so `deploy.sh` fails instead.
7. Grants Graph app roles (`managed_identity` mode only).

There is no `DRY_RUN` default, so a redeploy can't switch a stack from dry-run
to live because you forgot the variable. Keeping `DRY_RUN` in the environment
file means every redeploy repeats the value you last chose on purpose.

Before you go further, read the summary the script prints. Check the
subscription, the instance, `Source`, `Dry run`, `Class map`, and the `Alerts`
line. If `Alerts` says `NONE`, you will not be told when runs stop.

## 5. Trigger and verify a dry run

Don't wait for the schedule. Start the WebJob now, then read its run history.
The app accepts traffic only through its private endpoint, so use `webjob.sh`,
not `az webapp webjob` or the portal's Kudu tools:

```bash
export RESOURCE_GROUP=azc-obm-development NAME_PREFIX=intunecmdb-dev
./webjob.sh run        # starts a run and returns
./webjob.sh history    # status, start time and duration of recent runs
./webjob.sh output     # the latest run's output
```

`history` shows each run's status (`Success` or `Failed`) and duration, and
`output` shows the latest run's log lines, the same ones the run sends to
Application Insights. Then read the run summary from Log Analytics:

```bash
WORKSPACE=$(az monitor log-analytics workspace show \
  -g "$RESOURCE_GROUP" -n "${NAME_PREFIX}-logs" --query customerId -o tsv)

az monitor log-analytics query --workspace "$WORKSPACE" --analytics-query "
  AppTraces
  | where AppRoleName == '$APP'
  | extend p = parse_json(Message)
  | where p.msg in ('starting intune-cmdb-sync', 'run complete')
  | project TimeGenerated, msg = tostring(p.msg), dry_run = p.dry_run,
            inserted = p.inserted, updated = p.updated, errors = p.errors" -o table
```

Telemetry can take a few minutes to reach Log Analytics.

Gate:

- The `starting` line shows `dry_run: true`. If it shows `false`, the stack is
  live. Go to [Emergency stop](#emergency-stop).
- `run complete` shows `errors: 0`.
- The counts are plausible for your fleet.

The app sets `FAIL_ON_ERROR=true`, and the job exits with the sync's exit code,
so a run marked `Failed` means at least one device failed. That flag is not
the only cause, though: exit 4 (degraded) also fails the run. Look at the log
lines just before `run complete` to find out which. A failed run is **not
retried**; the next scheduled run picks up.

This is the first time the **Graph** half runs with the app's own credential.
In `managed_identity` and `federated_managed_identity` modes, nothing earlier
could test it.

## 6. First real write, limited

The app has no device limit setting, so do the first real write from your
workstation. Use the configuration that passed stage 2, with retirement off:

```bash
SNOW_RETIRE_MISSING=false intune-cmdb-sync --limit 5 --report-devices --report ./first-write.json
```

`--limit` turns retirement off automatically, and this command sets it off
explicitly as well. Now look at those five CIs in ServiceNow: the class,
serial number, manufacturer, model, and assigned user. The read-only
`intune-cmdb-query` tool reads them back without writing anything.

Run the same command again. Every device should come back as UPDATE or
NO_CHANGE against the **same** `sys_id`. A second INSERT means identification
is not matching, and a full-fleet run would duplicate CIs.

Gate: the CIs are correct, and a re-run does not duplicate them.

## 7. Go live

Change one line in the environment file, `DRY_RUN=false`, and redeploy the same
way as in stage 4. The summary now reads `Dry run  false  -- LIVE`. Keep
`RETIRE_MISSING=false`. Then either trigger a run as in stage 5 or wait for the
schedule, and read the `run complete` line.

Gate: `errors: 0`, and the inserted count is roughly your fleet size minus the
CIs that already exist. On a second run, expect NO_CHANGE and UPDATE, not
INSERT. With the overrides file dropping `last_discovered`, UPDATE means
something actually changed. Without it, every device reports UPDATE.

## 8. Enable retirement (optional, later)

Retirement PATCHes `install_status` on CIs whose devices have left Intune. It
is the only way this connector ever marks a CI as gone. Before you enable it:

- [ ] Several live runs have been clean, so `state.json` on the file share maps
  the whole fleet.
- [ ] `install_status=7` really means *retired* **on this instance**. That is a
  convention, not a guarantee, so check the choice list.
- [ ] State persistence is on (the default). Without it the connector cannot
  know what disappeared.

Then redeploy with `RETIRE_MISSING=true`. A guard skips retirement and marks
the run degraded when more than 10% of known devices vanish at once. That
catches a partial Graph response or a wrong tenant before they turn into a
mass retirement.

## Emergency stop

To stop writes **immediately**:

```bash
az webapp config appsettings set -g "$RESOURCE_GROUP" -n "$APP" \
  --settings DRY_RUN=true SNOW_RETIRE_MISSING=false --output none
```

Changing app settings restarts the app, which also ends a run in progress. To
stop runs entirely rather than make them dry, use `az webapp stop` instead. The
schedule does not fire while the app is stopped, and the *no successful run*
alert will then fire, as it should.

The next `deploy.sh` overwrites either change. To make it stick, set
`DRY_RUN=true` in the environment file and redeploy. A dry-run app keeps
running and keeps logging, so you still see what it would do and the
*no successful run* alert keeps working.

To roll back a bad build, check out the previous revision and redeploy:
`git checkout <revision> && ./deploy.sh`. The `Source` line of each deploy's
summary is the revision to go back to.

## Secrets

The ServiceNow client secret, and the Graph client secret in `client_secret`
mode, are **app settings**, not Key Vault references. A Key Vault here would
have to deny public access, which would bring back the private network this
host exists to avoid. App settings are encrypted at rest and never shown in
logs or the deploy output. But anyone who can read the app's configuration can
read them: the Contributor and Website Contributor roles on the app or its
resource group can. Keep those roles to the people who deploy.

To keep this to one secret, use `GRAPH_AUTH_MODE=managed_identity` wherever
Intune is in this subscription's tenant. Graph then has no secret at all.

## What the app is configured with

These are the complete settings the WebJob receives. Nothing else from a local
`.env` reaches it.

| Variable | Set from |
| --- | --- |
| `SNOW_INSTANCE`, `SNOW_CLIENT_ID`, `SNOW_CLIENT_SECRET` | `deploy.sh` input; see [Secrets](#secrets) |
| `SNOW_AUTH_MODE` | always `oauth_client_credentials` |
| `SNOW_WRITE_MODE` | `SNOW_WRITE_MODE` (default `identify_reconcile`) |
| `SNOW_DISCOVERY_SOURCE` | `SNOW_DISCOVERY_SOURCE` (default `Intune`) |
| `SNOW_RETIRE_MISSING` | `RETIRE_MISSING` (default `false`) |
| `DRY_RUN` | `DRY_RUN` (**required**, no default) |
| `SNOW_CLASS_MAP` | `SNOW_CLASS_MAP`; omitted when unset, so the built-in map applies |
| `MAPPING_OVERRIDES_JSON` | the contents of the local `MAPPING_OVERRIDES_FILE`, minus `_comment`; omitted when unset |
| `GRAPH_AUTH_MODE`, `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` | depend on the Graph mode |
| `INTUNE_OWNERSHIP` | always `company` |
| `FAIL_ON_ERROR` | always `true` |
| `LOG_FORMAT`, `LOG_LEVEL` | always `json`, `INFO` |
| `STATE_PATH`, `RUN_REPORT_PATH`, `RUN_REPORT_DEVICES` | `/home/data/intune-cmdb-sync/`, the app's persistent storage |
| `APPLICATIONINSIGHTS_CONNECTION_STRING`, `AZURE_CLIENT_ID`, `OTEL_SERVICE_NAME` | telemetry, published as the managed identity; `OTEL_SERVICE_NAME` is the app name the alerts filter on |
| `WEBJOBS_IDLE_TIMEOUT` | `1800`: a triggered WebJob is otherwise killed after 2 minutes without output |
| `SCM_DO_BUILD_DURING_DEPLOYMENT`, `WEBSITES_ENABLE_APP_SERVICE_STORAGE`, `WEBSITE_SKIP_RUNNING_KUDUAGENT` | host behaviour: no build on deploy, persistent `/home`, WebJobs enabled |

The schedule is not an app setting: it is in the WebJob's `settings.job`,
written by `deploy.sh` from `SCHEDULE`.

## Multiple ServiceNow environments

To feed more than one ServiceNow instance from the same Intune tenant, such as
DEV and PROD, run `deploy.sh` once per instance, each time with a different
`NAME_PREFIX`. Every resource name is derived from the prefix, so each run
creates a separate stack, whether the environments share a resource group or,
as is likely here, each has its own pre-provisioned one (DEV is
`azc-obm-development`; set PROD's in `prod.env`). In practice that means one
environment file per instance, `dev.env` and `prod.env`, following the stage 4
pattern.

**Settings that differ per environment:** the `SNOW_*` credentials, the
`SNOW_DISCOVERY_SOURCE` value (registered separately on each instance), usually
`SNOW_WRITE_MODE`, and `SCHEDULE`. Auth scopes are configured per instance, so
DEV may allow the IRE API while PROD refuses it. Run `--check-api` against each
instance. Offset the schedules so the two runs are easy to tell apart in the
logs.

**Settings shared by both:** the Graph settings (one Intune tenant).

**Resources each environment gets for itself:** managed identity, App Service
plan and app, its private endpoint, Application Insights and its workspace, and
alert rules. Both environments can put their endpoints in the same subnet if it
has room. Each app
has its own `/home`, so each has its own `state.json`. That separation is a
correctness requirement: `state.json` maps Intune device IDs to ServiceNow
`sys_id`s, and a `sys_id` means something only on the instance that issued it.
If DEV and PROD shared a state file, DEV's IDs would drive PROD's retirement
decisions. **Do not point two environments at one app or one state path.**

### Deploying each environment, step by step

Repeat these steps once per environment, with that environment's own file
(`dev.env`, `prod.env`). Nothing is shared between runs except the code revision.

**What differs per environment** (everything else in the stage 4 file can match):

| Setting | DEV | PROD |
| --- | --- | --- |
| `NAME_PREFIX` | `intunecmdb-dev` | `intunecmdb-prod` |
| `RESOURCE_GROUP` | `azc-obm-development` | PROD's pre-provisioned group |
| `PRIVATE_ENDPOINT_SUBNET_ID` | `hybridsubnet-1` of `vpcx-vnet-eastus` | look it up, see [Finding the private endpoint subnet](#finding-the-private-endpoint-subnet) |
| `SNOW_INSTANCE`, `SNOW_CLIENT_ID`, `SNOW_CLIENT_SECRET` | the DEV instance and its OAuth client | the PROD instance and its own OAuth client |
| `SNOW_DISCOVERY_SOURCE` | registered on DEV | registered separately on PROD |
| `SCHEDULE` | e.g. `0 15 3 * * *` | offset, e.g. `0 45 3 * * *` |
| `GRAPH_AUTH_MODE` | `client_secret` (Intune is in another tenant) | `managed_identity` if the subscription and Intune share a tenant |
| `ALERT_EMAIL` | optional | set it |

**Steps:**

1. **Select the subscription.** `az account show`, then `az account set
   --subscription <name or id>`. The script deploys into whatever `az` points at.
2. **Check out the revision to ship.** PROD gets a revision only after DEV has
   run it cleanly. The summary prints it as `Source`.
3. **Probe the instance from your workstation** (stage 2), with that
   environment's `.env`: `--check-api`, `--check`, and a `--dry-run --limit 5`.
   Auth scopes are per instance, so a pass on DEV says nothing about PROD. Fix
   anything it reports (REST API Auth Scope, discovery source, class map) first.
4. **Write the environment file** (stage 4) with `DRY_RUN=true` and
   `RETIRE_MISSING=false`, and the same `SNOW_CLASS_MAP` and
   `MAPPING_OVERRIDES_FILE` that passed step 3.
5. **Deploy in dry-run:**

   ```bash
   set -a && . ./prod.env && set +a
   read -rs -p "ServiceNow client secret: " SNOW_CLIENT_SECRET; echo; export SNOW_CLIENT_SECRET
   # client_secret mode only:
   # read -rs -p "Graph client secret: " GRAPH_CLIENT_SECRET; echo; export GRAPH_CLIENT_SECRET
   ./deploy.sh
   ```

   Read the summary: subscription, instance, `Source`, `Dry run true`, and an
   `Alerts` line that is not `NONE`.
6. **Trigger and verify** (stage 5):
   `RESOURCE_GROUP=<group> NAME_PREFIX=<prefix> ./webjob.sh run`, then `history`
   and `output`, and query `AppTraces`. Gate: `dry_run: true`, `errors: 0`.
7. **First limited real write** (stage 6) from your workstation against that
   instance, with `--limit 5` and retirement off. Re-run it: no duplicates.
8. **Go live** (stage 7): set `DRY_RUN=false` in the environment file and rerun
   step 5. The summary must read `Dry run  false  -- LIVE`. Trigger one run and
   check `errors: 0`.
9. **Daily operation.** The schedule runs the job every day with no further
   action. The two alerts report a missed or failing run. Put the secret expiry
   dates in a calendar.
10. **Retirement** (stage 8) stays off until several clean live runs, and
    `install_status=7` is confirmed as retired on **that** instance.

To add a third environment, give it a new prefix and environment file and start at
step 1. Day-to-day commands for any environment take `RESOURCE_GROUP` and
`NAME_PREFIX` from the shell (`export RESOURCE_GROUP=... NAME_PREFIX=...`), so
check them before running `webjob.sh` or any `az webapp` command, because the
wrong pair acts on the wrong environment.

**Promote independently.** DEV runs through every stage before PROD starts. A
new build follows the same path: redeploy DEV from the new revision, and
redeploy PROD from that same revision only after DEV has run cleanly. Re-running
`deploy.sh` for one prefix does not touch the other.

In `managed_identity` mode, each prefix creates its own identity and grants
Graph permissions to it, so each deploy needs the admin role from stage 1.

**Removing one environment.** Never use `az group delete`: the resource group
is pre-provisioned and may hold other teams' resources. Delete by prefix
instead. Run the list on its own first and read what it matches:

```bash
az resource list -g "$RESOURCE_GROUP" \
  --query "[?starts_with(name, 'intunecmdb-dev')].id" -o tsv

# then, once you are sure:
az resource list -g "$RESOURCE_GROUP" \
  --query "[?starts_with(name, 'intunecmdb-dev')].id" \
  -o tsv | xargs -r az resource delete --ids
```

If a delete fails because the app still depends on its plan, run the command
again.

## What gets created

For each `NAME_PREFIX`:

| Resource | Name | Purpose |
| --- | --- | --- |
| User-assigned managed identity | `<prefix>-id` | Publishes telemetry; Graph authentication in `managed_identity` mode |
| App Service plan | `<prefix>-plan` | Linux, Basic B1, one instance |
| Web app | `<prefix>-app-<hash>` | Hosts the scheduled WebJob `intune-cmdb-sync`. Python 3.12, Always On, HTTPS only, TLS 1.2, FTP and basic-auth publishing off, public network access disabled |
| Private endpoint | `<prefix>-app-pe` | The app's only inbound path, for deploys and WebJob operations; in `PRIVATE_ENDPOINT_SUBNET_ID` |
| Log Analytics workspace | `<prefix>-logs` | Where Application Insights stores traces, 30-day retention |
| Application Insights | `<prefix>-ai` | Receives the job's logs; local auth disabled |
| Alert rules + action group | `<prefix>-alerts` and others | Only when `ALERT_EMAIL` is set |

The app name carries a hash because web app names are global
(`<name>.azurewebsites.net`). The site itself serves only App Service's
placeholder page; the WebJob is the whole application.

The identity is user-assigned on purpose, not system-assigned. That way the
Graph permission grant survives the app being deleted and recreated.

The WebJob's `settings.job` sets `is_singleton: true` and the plan runs one
instance, so two runs can never race on `state.json`. That's the same reason the
AWS Lambda has reserved concurrency of 1.

## Cost

List prices, East US, one ~5-minute run per day.

| | Usage/month | Cost |
| --- | --- | --- |
| App Service plan, Linux B1 | always on | **~$13.14** |
| Private endpoint | one, ~730 hours | **~$7.30** |
| Application Insights + Log Analytics | a few MB | **$0.00**: the first 5 GB/month is free |
| **Per environment** | | **about $20/month** |

Basic B1 is the cheapest tier with "Always On", which a scheduled WebJob needs
to keep firing. The private endpoint is the policy's price: the app's only way
in.

## Alerting

Set `ALERT_EMAIL` before running `deploy.sh`, and two alert rules are deployed
with an action group:

| Rule | Fires when | Severity |
| --- | --- | --- |
| `<prefix>-no-successful-run` | no `run complete` trace in 24 hours | 1 |
| `<prefix>-device-errors` | a run finished with `errors > 0`, or degraded (retirement guard tripped, state not saved) | 2 |

If `ALERT_EMAIL` is unset, no alert resources are created at all. That is
deliberate, so a deployment is never *almost* monitored.

The first rule matters most. A job that stops firing produces no error for
anyone to notice, and the CMDB goes stale without warning. An expired Graph or
ServiceNow secret shows up here. The rule's query ends in
`summarize completed = count()` with **no `by` clause**, which is what makes
absence detectable. That form returns a row of `0` when nothing matched. A
grouped form would return no rows, and the rule would never fire.

**How the logs get there.** A WebJob's output goes only to its own run history,
which no alert can query. So the job (`intune_cmdb_sync.appservice_job`) also
sends its log records to Application Insights through OpenTelemetry, as the
managed identity. Three details keep the alerts honest:

- The OpenTelemetry handler gets the connector's JSON formatter, so each trace's
  `Message` is the same one-line JSON the alert queries `parse_json`.
- The job flushes telemetry before it exits. Otherwise the last lines, including
  `run complete`, could be lost with the process, and the absence alert would
  report a run that happened as missing.
- `OTEL_SERVICE_NAME` is set to the app name, which becomes each trace's
  `AppRoleName`, the field both queries filter on.

The handler also copies each log line's fields into the trace's custom
dimensions, bypassing the formatter. Fields whose names look like secrets are
masked on the record itself, so they are masked there too.

## Operating

```bash
RG=azc-obm-development
PREFIX=intunecmdb-dev
APP=<web app name>

export RESOURCE_GROUP=$RG NAME_PREFIX=$PREFIX
./webjob.sh run           # run now
./webjob.sh history       # recent runs
./webjob.sh output        # latest run's output
./webjob.sh report        # latest run-report.json
./webjob.sh state         # state.json
./webjob.sh deployments   # recent code deployments
./webjob.sh schedule      # show the live schedule (settings.job)
./webjob.sh schedule "0 30 6 * * *"   # change it in place, UTC, no redeploy

WORKSPACE=$(az monitor log-analytics workspace show \
  -g $RG -n $PREFIX-logs --query customerId -o tsv)
```

Logs are structured JSON, so the run summary can be queried directly:

```kusto
AppTraces
| where AppRoleName == "<web app name>"
| extend p = parse_json(Message)
| where p.msg == "run complete"
| project TimeGenerated,
          inserted = toint(p.inserted),
          updated  = toint(p.updated),
          errors   = toint(p.errors),
          unresolved_users = toint(p.users_unresolved)
```

To isolate one run, filter by `run_id`. It is on every log line and in
`run-report.json`, and each run gets a fresh one:

```kusto
AppTraces
| extend p = parse_json(Message)
| where p.run_id == "<id from run-report.json>"
| order by TimeGenerated asc
```

`run-report.json` and `state.json` are in `/home/data/intune-cmdb-sync/` on the
app; `webjob.sh report` and `webjob.sh state` print them.

## Private access

The app has public network access disabled, as the landing zone requires, and
one private endpoint (`<prefix>-app-pe`) in `PRIVATE_ENDPOINT_SUBNET_ID`. That
endpoint serves both the site and its Kudu management endpoint, which is what
deploys and WebJob operations use. The job itself only makes outbound calls,
which are unaffected, so there is no VNet integration.

**How your machine gets in.** On 2026-10-02 a Mac on Zscaler reached a private
endpoint in `hybridsubnet-1` at its private IP (Zscaler Private Access routes
it), but its DNS (`100.64.0.1`) resolved the name to the public address. So
`kudu.sh` does not use DNS: it asks Azure for the endpoint's private IP and
pins the Kudu hostname to it with `curl --resolve`. TLS is still verified
against the real hostname, and nothing is written to `/etc/hosts`. The
cleaner long-term fix is for IT to forward `privatelink.azurewebsites.net` (and
the other `privatelink` zones) from Zscaler's DNS to the hub. Nothing here
depends on that.

Calls authenticate with your Entra login: an App Service token from `az`, the
same one `az webapp deploy` would use. You need Contributor or Website
Contributor on the app.

**If Kudu is unreachable**, both scripts stop and print this check:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 \
  --resolve <app>.scm.azurewebsites.net:443:<private IP> https://<app>.scm.azurewebsites.net/
```

`000` or a timeout means no route to the private IP: connect Zscaler Private
Access or the VPN. 401 or 403 means the route works but your login lacks a
role on the app.

**What no longer works:** anything that reaches the app publicly. That includes
`az webapp deploy`, `az webapp webjob ...`, `az webapp log tail`, and the
portal's **Advanced Tools** (Kudu) and **WebJobs** pages, unless your browser
resolves the private address. App settings, restarts, stop and start still
work: they go through Azure Resource Manager, not the app.

### Changing the schedule

Permanent: change `SCHEDULE` in the environment file and redeploy.

Without a redeploy: `./webjob.sh schedule "0 30 6 * * *"` rewrites only the
`schedule` key of the WebJob's `settings.job` through Kudu (six fields, UTC) and
prints the result. It is temporary, because the next `deploy.sh` writes
`settings.job` from `SCHEDULE`, so set the same value in the environment file.
Whether Kudu reloads the schedule without a restart is not verified yet: after the
new time passes, check `./webjob.sh history`, and run `az webapp restart` if no run
appears.

### Changing configuration

Edit the environment file and re-run `deploy.sh`. The script is idempotent.
Changes made with `az webapp config appsettings set` last only until the next
deploy.

### Secret rotation

- **ServiceNow or Graph secret:** redeploy with the new value. The app restarts
  with it; a run in progress at that moment ends, and the next scheduled run
  uses the new secret.
- **An expired secret stops runs.** The *no successful run* alert is what
  reports it. Put the expiry dates in a calendar.

## Behind a TLS-inspecting proxy (Zscaler)

Zscaler decrypts and re-signs HTTPS with its own root certificate. macOS trusts
that root because Zscaler Client Connector installs it in the System keychain,
but `az` and `pip` do not read the keychain. They ship their own list of public
roots (certifi), so every request fails with
`SSL: CERTIFICATE_VERIFY_FAILED`. The first one to fail is usually
`az`'s Bicep version check:
`Error while attempting to retrieve the latest Bicep version ... aka.ms`.

**For `deploy.sh`:**

```bash
CA_BUNDLE=macos-keychain ./deploy.sh         # roots from the System keychain
CA_BUNDLE=/path/to/zscaler-root.pem ./deploy.sh   # or a PEM file from IT
```

Put `CA_BUNDLE=macos-keychain` in your environment file to make it permanent.
For that run only, the script builds one bundle of the public roots **plus**
the extra ones and points `az`, `pip` and Python at it through
`REQUESTS_CA_BUNDLE`, `PIP_CERT` and `SSL_CERT_FILE`. Nothing on the machine
changes. `macos-keychain` takes every certificate in the System keychain: that
is the set macOS already trusts, and it does not depend on the exact name
Zscaler gives its root.

`deploy.sh` also always skips the Bicep version check
(`AZURE_BICEP_CHECK_VERSION=false`, for that run only). It is only a notice that
a newer Bicep exists, and the deploy does not need it.

Before its first `az` call, the script checks TLS to `management.azure.com` and
`pypi.org` with the same trust `az` and `pip` will use. If something is
intercepting HTTPS and `CA_BUNDLE` is unset or lacks the proxy's root, it stops
there with one line saying so, before anything changes.

**Finding the root,** if `macos-keychain` reports none or the check still
fails:

```bash
# Is it in the System keychain, and under what name?
security find-certificate -a -c Zscaler -p /Library/Keychains/System.keychain > zscaler-root.pem
grep -c "BEGIN CERTIFICATE" zscaler-root.pem     # 1 or more = found

# Which root does the proxy actually present?
openssl s_client -connect management.azure.com:443 -showcerts </dev/null 2>/dev/null \
  | grep -E "^ *[si]:"
```

If neither finds it, ask IT for the Zscaler root CA. A DER `.cer` file converts
with `openssl x509 -inform der -in root.cer -out root.pem`.

**For running the connector locally** (stage 2: `--check-api`, `--check`, the
limited first write), the same applies to its HTTPS calls. Build the bundle
once and export it in that shell. No code change is needed: `httpx` reads
`SSL_CERT_FILE`, and the token libraries (`requests`) read `REQUESTS_CA_BUNDLE`.

```bash
{ cat "$(.venv/bin/python -c 'import certifi; print(certifi.where())')"; echo
  security find-certificate -a -p /Library/Keychains/System.keychain; } > ~/.ca-bundle.pem
export SSL_CERT_FILE=~/.ca-bundle.pem REQUESTS_CA_BUNDLE=~/.ca-bundle.pem
```

**Never switch verification off instead.** `AZURE_CLI_DISABLE_CONNECTION_VERIFICATION=1`
and `pip --trusted-host` make the error go away by accepting any certificate at
all, including the credentials this deploy sends to ServiceNow and Azure.

## Teardown

**Do not delete the resource group.** It was provisioned for you, `deploy.sh`
did not create it, and it may hold resources that are not this connector's.
Remove a stack by its prefix, as in
[Removing one environment](#multiple-servicenow-environments). Role assignments
made to the deleted identity are left behind as "Identity not found" entries;
remove them from the group's Access control blade if they bother you.

Nothing in this stack is soft-deleted, so a prefix can be redeployed straight
away.
