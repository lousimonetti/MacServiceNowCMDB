# Azure deployment

An Azure Functions app on the Flex Consumption plan, with one timer-triggered
function that runs the sync. `deploy.sh` builds one self-contained stack per
ServiceNow environment. Re-running it is how you change anything.

**Why Functions.** The target subscription's landing-zone policy allows
`Microsoft.Web` (Functions and App Service) but denies Azure Container Registry
and Container Apps. So there is no image: `deploy.sh` builds a zip package on
your machine (the connector, its Linux wheels, and a small `function_app.py`)
and deploys it into a blob container in the stack's own storage account. The
root `Dockerfile` is still there for the AWS ECS host and for anything else
that runs containers, but the Azure path does not use it.

This page is laid out in the order you do things. Read **Before you run
anything** first, even if you have deployed before.

## Safe rollout at a glance

Every stage has a gate. Do not start a stage until the one before it has passed.

| # | Stage | Where | Writes to the CMDB? | Gate to move on |
| --- | --- | --- | --- | --- |
| 1 | [Prerequisites](#1-prerequisites) | Azure, Entra, ServiceNow | No | Everything in the checklist exists |
| 2 | [Prove ServiceNow from your workstation](#2-prove-servicenow-from-your-workstation) | Local | One choice-list row, if you register the source | `--check-api` and `--check` exit 0 |
| 3 | [Check region and policy](#3-check-region-and-policy) | Azure | No | Region supports Flex Consumption, every resource type is allowed |
| 4 | [Deploy in dry-run](#4-deploy-in-dry-run) | Azure | No | `deploy.sh` finishes, function registered |
| 5 | [Trigger and verify a dry run](#5-trigger-and-verify-a-dry-run) | Azure | No | Invocation succeeded, `run complete` with `errors: 0` |
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
   change made in the portal or with `az functionapp config appsettings set`.
   Treat those as temporary overrides only (see [Emergency stop](#emergency-stop)).
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
downloads those wheels from PyPI on **your machine**. Nothing at runtime
reaches outside Azure for code; the package is served from the stack's own
storage account.

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
- [ ] **Two subnets in the landing zone's network**, because the policy denies
  public access to Key Vault and Storage: a `/27` (or larger) delegated to
  `Microsoft.App/environments` for the function's VNet integration, and one for
  private endpoints. Usually the platform team provides them. See
  [Networking](#networking) for what to ask for and how to find out what exists.
- [ ] **A Graph credential that matches your tenant topology.** See
  [Choosing Graph authentication](#choosing-graph-authentication) below and
  [docs/entra-setup.md](../../docs/entra-setup.md).
- [ ] **A ServiceNow OAuth client** for each instance, with the roles in
  [docs/servicenow-setup.md](../../docs/servicenow-setup.md) sections 1-4.
- [ ] **The discovery source registered** on each instance
  (`servicenow-setup.md` section 5, or `--register-discovery-source` in stage 2).
  Until it is, every write is rejected.

**Roles the person deploying needs:**

| Action | Needs |
| --- | --- |
| Deploy, and assign the stack's identity its roles | Owner, or Contributor plus User Access Administrator (or Role Based Access Control Administrator), on the resource group |
| `GRAPH_AUTH_MODE=managed_identity` (the script grants Graph app roles) | Privileged Role Administrator, Cloud Application Administrator, or Global Administrator in the subscription's tenant |

The template assigns roles to the stack's own managed identity (Key Vault
secrets, storage, Application Insights publishing), so plain Contributor is not
enough.

### Choosing Graph authentication

This choice decides the credential model. A wrong choice produces a deployment
that fails only when the function runs at 3am.

```bash
az account show --query tenantId -o tsv   # the subscription's tenant
```

Compare that value with the tenant where **Intune** lives.

| Topology | `GRAPH_AUTH_MODE` | Graph secret? | Notes |
| --- | --- | --- | --- |
| Same tenant | `managed_identity` | None | Best case. `deploy.sh` grants the identity the Graph app roles itself. |
| Different tenants | `client_secret` (default) | App registration secret from the **Intune** tenant, stored in Key Vault | The managed identity only reads Key Vault (and host storage). |
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
parts across two directories, and it saves you from rotating one Key Vault
secret.

`workload_identity` is not offered: it needs a projected federated token file,
which AKS and GitHub Actions provide and Azure Functions does not.

[fic]: https://learn.microsoft.com/entra/workload-id/workload-identity-federation-config-app-trust-managed-identity

## 2. Prove ServiceNow from your workstation

Which write API an OAuth client may call is set per instance and per HTTP
method. The only reliable way to know is to probe it. Do that from your
workstation, where a failure is one command and not a scheduled run's log.
Configure a local `.env` as in the [top-level README](../../README.md#quick-start),
with the **same instance and OAuth client** the function will use.

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

## 3. Check region and policy

**Region.** Every resource is created in **East US** (`eastus`), whatever
region the chosen resource group is in; a resource's region need not match
its group's. East US offers Flex Consumption, and `deploy.sh` re-checks that
against `az functionapp list-flexconsumption-locations` before creating
anything. `LOCATION` overrides the region, but the requirement is East US, so
leave it unset.

**Policy.** The landing-zone policy is an allowlist of resource types. This
stack creates these, and every one must be on it:

| Resource type | Status as last recorded |
| --- | --- |
| `Microsoft.Web/serverfarms`, `Microsoft.Web/sites` (and `sites/config`) | Approved for Functions and App Service |
| `Microsoft.Storage/storageAccounts` (blob container, file share) | Allowed **with public network access denied** (`vpcx-lzn-strg-restrict-network-access`); the template sets `networkAcls.defaultAction: Deny` |
| `Microsoft.KeyVault/vaults` | Allowed **with public network access denied, purge protection on and 90-day retention** (`vpcx-lzn-kv-restrict-network-access`, `vpcx-lzn-kv-enable-soft-delete-purge-retention-days-90`); the template sets all three |
| `Microsoft.Network/privateEndpoints` (and `privateDnsZones` in `local` DNS mode) | **Not confirmed.** Needed because of the two rows above |
| `Microsoft.ManagedIdentity/userAssignedIdentities` | Allowed |
| `Microsoft.OperationalInsights/workspaces` | Allowed |
| `Microsoft.Insights/actionGroups`, `Microsoft.Insights/scheduledQueryRules` | Allowed |
| `Microsoft.Insights/components` (Application Insights) | Probably allowed: the 2026-10-02 preflight refused only the vault and storage account |

Preflight evaluates every resource in the template and reports every refusal at
once, so the two rows it refused on 2026-10-02 are good evidence that the
others passed. Application Insights matters most of those: it is how a Python
function's logs leave the host, and without it the alerts have nothing to read.

A denied type costs nothing to discover: ARM evaluates deny policies during
preflight, before it creates any resource, so `deploy.sh` stops with
`RequestDisallowedByPolicy` and the name of the refused type. Only the resource
group (allowed) exists at that point.

The storage account keeps **shared-key access enabled**, because an Azure Files
mount on Functions authenticates only with the account key. A policy that
requires `allowSharedKeyAccess=false` would break the state mount. Everything
else in the stack uses the managed identity.

Gate: the region is listed, and nothing on the table above is known to be
refused.

## 4. Deploy in dry-run

Use a small per-environment file for the non-secret settings, so a redeploy
reuses exactly the values you tested. Keep it out of git.

```bash
# dev.env: no secrets in this file
NAME_PREFIX=intunecmdb-dev
RESOURCE_GROUP=azc-obm-development     # pre-existing, never created; omit to pick from a list
# LOCATION is not set: resources go to East US (eastus) by default
CA_BUNDLE=macos-keychain               # behind Zscaler; see "Behind a TLS-inspecting proxy"

# Networking: both required; see "Networking"
INTEGRATION_SUBNET_ID=/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<delegated /27>
PRIVATE_ENDPOINT_SUBNET_ID=/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<endpoints>
PRIVATE_DNS_MODE=central               # or local; see "Networking"
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
   or `false`, and rejects a five-field `SCHEDULE`, or the old `CRON` variable,
   because the timer would reject it only after deployment.
2. Builds the zip package locally, before anything in Azure changes.
3. Deploys the infrastructure (`main.bicep`).
4. Deploys the package. The upload runs as the stack's identity, whose role
   assignments were created seconds earlier, so it retries for a couple of
   minutes while they propagate.
5. **Checks that the timer function registered.** A package that deploys but
   cannot import (a missing or wrong-platform wheel) registers no function, and
   then nothing ever runs and nothing errors. `deploy.sh` fails instead.
6. Grants Graph app roles (`managed_identity` mode only).

There is no `DRY_RUN` default, so a redeploy can't switch a stack from dry-run
to live because you forgot the variable. Keeping `DRY_RUN` in the environment
file means every redeploy repeats the value you last chose on purpose.

Before you go further, read the summary the script prints. Check the
subscription, the instance, `Source`, `Dry run`, `Class map`, and the `Alerts`
line. If `Alerts` says `NONE`, you will not be told when runs stop.

## 5. Trigger and verify a dry run

Don't wait for the schedule. A timer function is started by hand through the
host's admin endpoint, with the master key. It returns `202` at once and the
run continues in the background:

```bash
APP=<Function app name from the deploy summary>
HOST=$(az functionapp show -g "$RESOURCE_GROUP" -n "$APP" --query defaultHostName -o tsv)
KEY=$(az functionapp keys list -g "$RESOURCE_GROUP" -n "$APP" --query masterKey -o tsv)

curl -sS -X POST "https://${HOST}/admin/functions/intune_cmdb_sync" \
  -H "x-functions-key: ${KEY}" -H "Content-Type: application/json" -d '{}'
```

The master key can do anything to the app. Use it from a shell, not a script
you keep.

After a few minutes, read the invocation result and the run summary:

```bash
WORKSPACE=$(az monitor log-analytics workspace show \
  -g "$RESOURCE_GROUP" -n "${NAME_PREFIX}-logs" --query customerId -o tsv)

# Did the invocation succeed?
az monitor log-analytics query --workspace "$WORKSPACE" --analytics-query "
  AppRequests
  | where AppRoleName == '$APP'
  | project TimeGenerated, Name, Success, DurationMs
  | order by TimeGenerated desc | take 5" -o table

# What did it do?
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

The app sets `FAIL_ON_ERROR=true`, and the function raises on any non-zero
exit, so an invocation with `Success = false` means at least one device failed.
That flag is not the only cause, though: exit 4 (degraded) also fails the
invocation. Look at the log lines just before `run complete` to find out which.
A failed invocation is **not retried**; the next scheduled run picks up.

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

## Networking

The landing zone denies public network access to Key Vault and Storage. The
function therefore reaches its own vault and storage privately:

- **Outbound:** VNet integration through `INTEGRATION_SUBNET_ID`. Flex
  Consumption sends **all** of the app's outbound traffic through that subnet,
  not only traffic to the vault and storage. So the landing zone's firewall
  must allow it out to Entra, Graph, ServiceNow and Application Insights, or
  every run fails at its first HTTP call.
- **To the vault and storage:** five private endpoints in
  `PRIVATE_ENDPOINT_SUBNET_ID`: `vault`, plus storage `blob` (the code package
  and the timer's lease), `queue` and `table` (the Functions host), and `file`
  (the state share).
- **Name resolution:** the `privatelink.*` DNS zones. With
  `PRIVATE_DNS_MODE=central` (the default), the landing zone's hub owns them
  and a policy registers each endpoint. With `local`, this stack creates the
  zones in its resource group and links them to the endpoint subnet's VNet.

`deploy.sh` checks the integration subnet before anything is built: it must be
delegated to `Microsoft.App/environments`, be `/27` or larger, have no
underscore in its name, and hold no other endpoints. It also checks that the
endpoint subnet exists and that the `Microsoft.App` provider is registered.

**Find out what exists** (read-only):

```bash
az network vnet list --query "[].{name:name, rg:resourceGroup, prefixes:addressSpace.addressPrefixes}" -o table
az network vnet subnet list -g <vnet resource group> --vnet-name <vnet> \
  --query "[].{name:name, prefix:addressPrefix, delegations:delegations[].serviceName, id:id}" -o table
az network private-dns zone list --query "[?starts_with(name,'privatelink')].{name:name, rg:resourceGroup}" -o table
az provider show -n Microsoft.App --query registrationState -o tsv
```

If `privatelink.vaultcore.azure.net` and the `privatelink.*.core.windows.net`
zones show up in a resource group you don't own, DNS is central: keep the
default. If none exist anywhere, ask before choosing `local`. A local zone
shadows a central one for the linked VNet.

**What to ask the platform team for:**

1. Two subnets in the spoke VNet: a `/27` delegated to
   `Microsoft.App/environments`, named without underscores, for Flex
   Consumption VNet integration; and one for private endpoints. Their resource
   IDs go in `INTEGRATION_SUBNET_ID` and `PRIVATE_ENDPOINT_SUBNET_ID`.
2. How private DNS works: central `privatelink` zones with a policy that
   registers endpoints, or zones we create.
3. Egress from the integration subnet, HTTPS (443) to:
   `login.microsoftonline.com`, `graph.microsoft.com`,
   `<instance>.service-now.com`, `*.in.applicationinsights.azure.com`,
   `*.livediagnostics.monitor.azure.com`.
4. `Microsoft.Network/privateEndpoints` on the allowed-resource list, and the
   `Microsoft.App` resource provider registered in the subscription.

**What no longer works from your laptop.** The vault and the storage data plane
now accept only private traffic:
- Setting a secret with `az keyvault secret set`: rotate by redeploying instead.
  The template writes secrets through ARM, which the vault firewall does not
  cover.
- Reading `run-report.json` or `state.json` from the share, including through
  Storage Explorer and the portal's file browser.

The run summary in Application Insights is unaffected and stays the place to
look.

## Emergency stop

To stop writes **immediately**:

```bash
az functionapp config appsettings set -g "$RESOURCE_GROUP" -n "$APP" \
  --settings DRY_RUN=true SNOW_RETIRE_MISSING=false --output none
```

Changing app settings restarts the app, which also ends a run in progress. To
stop runs entirely rather than make them dry, use `az functionapp stop`
instead; the schedule does not fire while the app is stopped, and the *no
successful run* alert will then fire, as it should.

The next `deploy.sh` overwrites either change. To make it stick, set
`DRY_RUN=true` in the environment file and redeploy. A dry-run app keeps
running and keeps logging, so you still see what it would do and the
*no successful run* alert keeps working.

To roll back a bad build, check out the previous revision and redeploy:
`git checkout <revision> && ./deploy.sh`. The `Source` line of each deploy's
summary is the revision to go back to.

## What the app is configured with

These are the complete settings the function receives. Nothing else from a
local `.env` reaches it.

| Variable | Set from |
| --- | --- |
| `SNOW_INSTANCE`, `SNOW_CLIENT_ID` | `deploy.sh` input |
| `SNOW_CLIENT_SECRET` | a Key Vault reference; the value never appears in app settings |
| `SNOW_AUTH_MODE` | always `oauth_client_credentials` |
| `SNOW_WRITE_MODE` | `SNOW_WRITE_MODE` (default `identify_reconcile`) |
| `SNOW_DISCOVERY_SOURCE` | `SNOW_DISCOVERY_SOURCE` (default `Intune`) |
| `SNOW_RETIRE_MISSING` | `RETIRE_MISSING` (default `false`) |
| `DRY_RUN` | `DRY_RUN` (**required**, no default) |
| `SNOW_CLASS_MAP` | `SNOW_CLASS_MAP`; omitted when unset, so the built-in map applies |
| `MAPPING_OVERRIDES_JSON` | the contents of the local `MAPPING_OVERRIDES_FILE`, minus `_comment`; omitted when unset |
| `GRAPH_AUTH_MODE`, `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` | depend on the Graph mode; the secret is a Key Vault reference |
| `INTUNE_OWNERSHIP` | always `company` |
| `FAIL_ON_ERROR` | always `true` |
| `LOG_FORMAT`, `LOG_LEVEL` | always `json`, `INFO` |
| `STATE_PATH`, `RUN_REPORT_PATH`, `RUN_REPORT_DEVICES` | the mounted file share (`/mounts/state`), when state persistence is on |
| `SYNC_SCHEDULE` | `SCHEDULE`; read by the timer trigger |
| `AzureWebJobsStorage__*`, `APPLICATIONINSIGHTS_*` | the host's own storage and telemetry, both over the managed identity |

Every other setting uses the connector's built-in default.

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

**Resources each environment gets for itself:** Key Vault, managed identity,
Flex Consumption plan and function app, Application Insights, alert rules, and
storage account. The separate storage account is a correctness requirement.
`state.json` maps Intune device IDs to ServiceNow `sys_id`s, and a `sys_id`
means something only on the instance that issued it. If DEV and PROD shared a
state file, DEV's IDs would drive PROD's retirement decisions. **Do not
consolidate environments onto shared storage.**

**Promote independently.** DEV runs through every stage before PROD starts. A
new build follows the same path: redeploy DEV from the new revision, and
redeploy PROD from that same revision only after DEV has run cleanly. Re-running
`deploy.sh` for one prefix does not touch the other.

In `managed_identity` mode, each prefix creates its own identity and grants
Graph permissions to it, so each deploy needs the admin role from stage 1.

**Removing one environment.** Never use `az group delete`: the resource group
is pre-provisioned and may hold other teams' resources. Delete by prefix
instead. Run the list on
its own first and read what it matches:

```bash
az resource list -g "$RESOURCE_GROUP" \
  --query "[?starts_with(name, 'intunecmdb-dev') || starts_with(name, 'intunecmdbdev')].id" -o tsv

# then, once you are sure:
az resource list -g "$RESOURCE_GROUP" \
  --query "[?starts_with(name, 'intunecmdb-dev') || starts_with(name, 'intunecmdbdev')].id" \
  -o tsv | xargs -r az resource delete --ids
```

The second pattern matches the storage account, whose name has the hyphen
removed. If a delete fails because the function app still depends on its plan,
run the command again. The deleted vault's name stays reserved for 90 days
afterwards (see [Teardown](#teardown)).

## What gets created

For each `NAME_PREFIX`:

| Resource | Name | Purpose |
| --- | --- | --- |
| User-assigned managed identity | `<prefix>-id` | Key Vault references, host storage, telemetry, and Graph authentication in `managed_identity` mode |
| Key Vault | `<prefix>kv<hash>` | The ServiceNow secret, and the Graph secret in `client_secret` mode. Public access denied, purge protection on, 90-day retention |
| Private endpoints | `<prefix>-pe-{vault,blob,queue,table,file}` | Private access to the vault and storage, in `PRIVATE_ENDPOINT_SUBNET_ID` |
| Private DNS zones | `privatelink.*` | `local` DNS mode only; shared by every stack in the resource group |
| Log Analytics workspace | `<prefix>-logs` | Where Application Insights stores traces, 30-day retention |
| Application Insights | `<prefix>-ai` | Collects the function's logs and invocations; local auth disabled |
| Flex Consumption plan | `<prefix>-plan` | One app per plan, scales to zero |
| Function app | `<prefix>-fn-<hash>` | The timer-triggered sync. At most one instance, 30-minute timeout, no retry. |
| Storage account | `<prefix>st<hash>` | The deployment package (blob), the host's timer state, and the `state` file share for `state.json` and `run-report.json`. Public access denied |
| Alert rules + action group | `<prefix>-alerts` and others | Only when `ALERT_EMAIL` is set |

The function app name carries a hash because function app names are global
(`<name>.azurewebsites.net`).

The identity is user-assigned on purpose, not system-assigned. That way the
Graph permission grant survives the function app being deleted and recreated.

`maximumInstanceCount` is 1 so two runs can never race on `state.json`, the
same reason the AWS Lambda has reserved concurrency of 1. A timer trigger is a
singleton anyway; this makes it structural.

## Cost

List prices, one 5-minute run per day on a 2 GB instance.

| | Usage/month | Cost |
| --- | --- | --- |
| Flex Consumption (on demand) | ~18,000 GB-s, ~30 executions | **$0.00**: the free grant is 100,000 GB-s and 250,000 executions |
| Application Insights + Log Analytics | a few MB | **$0.00**: the first 5 GB/month is free |
| Key Vault (standard) | ~30–60 secret reads | **~$0.00**: no monthly fee, ~$0.03/10,000 operations |
| Storage (Standard LRS) | ~11 MB package, 1 GiB share quota, a few hundred KB used | **~$0.10** |
| Private endpoints | 5 × ~730 hours, a few MB processed | **~$36.50**: ~$0.01/hour each |
| Private DNS zones | 5 zones, `local` mode only | **~$2.50**, or $0 with central DNS |
| **Per environment** | | **about $37/month** |

The private endpoints are nearly the whole bill. The landing zone requires them
(see [Networking](#networking)); without that policy this stack costs about
$0.10/month. The free grant is per subscription, so environments share it.
There is no registry to pay for.

To drop the file share, deploy with `enableStatePersistence=false`. You lose
retirement and the persisted run report. Everything else works. The storage
account itself stays, because the Functions host needs it.

## Alerting

Set `ALERT_EMAIL` before running `deploy.sh`, and two alert rules are deployed
with an action group:

| Rule | Fires when | Severity |
| --- | --- | --- |
| `<prefix>-no-successful-run` | no `run complete` trace in 24 hours | 1 |
| `<prefix>-device-errors` | a run finished with `errors > 0`, or degraded (retirement guard tripped, state not saved) | 2 |

If `ALERT_EMAIL` is unset, no alert resources are created at all. That is
deliberate, so a deployment is never *almost* monitored.

The first rule matters most. A function that stops firing produces no error
for anyone to notice, and the CMDB goes stale without warning. An expired Graph
secret shows up here. The rule's query ends in `summarize completed = count()`
with **no `by` clause**, which is what makes absence detectable. That form
returns a row of `0` when nothing matched. A grouped form would return no rows,
and the rule would never fire.

Two Functions-specific details keep both rules honest:

- **Sampling is off** in `functions/host.json`. Application Insights samples
  traces by default, and a sampled-out `run complete` line would make the
  absence rule report a run that happened as missing.
- **The connector keeps the worker's log handler.** On the command line,
  `configure_logging` replaces the root logger's handlers with its own stdout
  handler. Inside Functions, stdout does not reach Application Insights; the
  Python worker's root handler does. So the function entry point
  (`intune_cmdb_sync.azure_function`) tells the connector to keep that handler
  and give it the JSON formatter. The worker sends the formatted line as the
  trace `Message`, which is what the alert queries parse.

## Operating

```bash
RG=azc-obm-development
PREFIX=intunecmdb-dev
APP=<function app name>

WORKSPACE=$(az monitor log-analytics workspace show \
  -g $RG -n $PREFIX-logs --query customerId -o tsv)
```

To run now, use the admin endpoint call in [stage 5](#5-trigger-and-verify-a-dry-run).

Logs are structured JSON, so the run summary can be queried directly:

```kusto
AppTraces
| where AppRoleName == "<function app name>"
| extend p = parse_json(Message)
| where p.msg == "run complete"
| project TimeGenerated,
          inserted = toint(p.inserted),
          updated  = toint(p.updated),
          errors   = toint(p.errors),
          unresolved_users = toint(p.users_unresolved)
```

To isolate one run, filter by `run_id`. It is on every log line and in
`run-report.json`, and each invocation gets a fresh one even when the platform
reuses a warm instance:

```kusto
AppTraces
| extend p = parse_json(Message)
| where p.run_id == "<id from run-report.json>"
| order by TimeGenerated asc
```

`run-report.json` and `state.json` are on the `state` file share of the stack's
storage account. That share now accepts only private traffic, so they can't be
read from a laptop (see [Networking](#networking)). The `run complete` trace
carries the counts and `error_samples`, which covers most questions.

### Changing configuration

Edit the environment file and re-run `deploy.sh`. The script is idempotent.
Changes made with `az functionapp config appsettings set` last only until the
next deploy.

### Secret rotation

- **ServiceNow or Graph secret:** redeploy with the new value. The vault denies
  public access, so `az keyvault secret set` from a laptop no longer works. The
  template writes the secret through ARM, which the vault firewall does not
  cover. The app settings reference the secret without a version, so the app
  picks up the new one within 24 hours, or at once on `az functionapp restart`.
- **An expired Graph secret stops runs.** The *no successful run* alert is what
  reports it. Put the expiry dates in a calendar.
- **Rotating the storage account key breaks the state mount** until the next
  `deploy.sh`, which re-reads the key. Redeploy straight after a rotation.

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

**A deleted vault cannot be purged.** The policy requires purge protection,
which keeps a deleted vault, and its name, for the full 90-day retention, and
`az keyvault purge` is refused. Redeploying the same `NAME_PREFIX` into the
same resource group within those 90 days needs the old vault recovered
(`az keyvault recover --name <vault>`), not recreated.
