# Azure deployment

A Container Apps Job that runs the sync on a cron schedule. `deploy.sh` builds
one self-contained stack per ServiceNow environment. Re-running it is how you
change anything.

This page is laid out in the order you do things. Read **Before you run
anything** first, even if you have deployed before.

## Safe rollout at a glance

Every stage has a gate. Do not start a stage until the one before it has passed.

| # | Stage | Where | Writes to the CMDB? | Gate to move on |
| --- | --- | --- | --- | --- |
| 1 | [Prerequisites](#1-prerequisites) | Azure, Entra, ServiceNow | No | Everything in the checklist exists |
| 2 | [Prove ServiceNow from your workstation](#2-prove-servicenow-from-your-workstation) | Local | One choice-list row, if you register the source | `--check-api` and `--check` exit 0 |
| 3 | [Build the image](#3-build-the-image) | ACR | No | Tag exists in the registry |
| 4 | [Deploy in dry-run](#4-deploy-in-dry-run) | Azure | No | `deploy.sh` finishes |
| 5 | [Trigger and verify a dry run](#5-trigger-and-verify-a-dry-run) | Azure | No | Execution `Succeeded`, `run complete` with `errors: 0` |
| 6 | [First real write, limited](#6-first-real-write-limited) | Local | Yes, about 5 CIs | The CIs look right in ServiceNow |
| 7 | [Go live](#7-go-live) | Azure | Yes, whole fleet | First scheduled run is clean |
| 8 | [Enable retirement](#8-enable-retirement-optional-later) (optional, later) | Azure | Yes, retires CIs | Several clean live runs |

With more than one ServiceNow instance, walk DEV through the whole table before
PROD starts it. See [Multiple ServiceNow environments](#multiple-servicenow-environments).

## Before you run anything

Four behaviours, each able to cause a bad write or a silent failure:

1. **The schedule is armed from the moment the deploy finishes.** Nothing waits
   for you to trigger a first run. A deploy at 02:00 UTC with the default cron
   runs at 03:15.
2. **`deploy.sh` is the source of truth for the job's settings.** The Bicep
   template sets the job's full environment list, so each redeploy discards any
   change made with `az containerapp job update --set-env-vars`. Treat that
   command as a temporary override only (see [Emergency stop](#emergency-stop)).
3. **Only some local settings carry over.** The job receives only the
   variables listed in
   [What the job is configured with](#what-the-job-is-configured-with).
   `SNOW_CLASS_MAP` and `MAPPING_OVERRIDES_FILE` reach it **if you set them
   when running `deploy.sh`**. Everything else in your local `.env` does not.
   Without the overrides file, `last_discovered` goes out on every run and
   every device reports UPDATE from the second run on. That is noise, not
   damage. Put the settings you tested locally into the environment file in
   stage 4.
4. **Secrets passed on the command line go into shell history.** Read them
   with `read -rs` as the examples do, not `export X=secret`.

## 1. Prerequisites

**Tools on the machine that deploys:** `az` (logged in), `jq`, and this repo.

**Check the subscription before anything else.** `deploy.sh` deploys into
whichever subscription `az` currently points at:

```bash
az account show --query "{subscription:name, id:id, tenant:tenantId}" -o table
az account set --subscription <name or id>    # if it is the wrong one
```

**These must already exist.** `deploy.sh` creates none of them:

- [ ] **Azure Container Registry**, shared by every environment
  ([3. Build the image](#3-build-the-image) shows how to create it).
- [ ] **An Entra service principal with `AcrPull`** on that registry, plus its
  secret. This is the default image puller. See
  [Pulling with a managed identity instead](#pulling-with-a-managed-identity-instead)
  for the alternative.
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
| Create the resource group and deploy | Contributor on the subscription or resource group |
| `GRAPH_AUTH_MODE=managed_identity` (the script grants Graph app roles) | Privileged Role Administrator, Cloud Application Administrator, or Global Administrator in the subscription's tenant |
| `ACR_AUTH_MODE=managed_identity` (the script grants AcrPull) | Owner, User Access Administrator, or Role Based Access Control Administrator on the registry |

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
| Different tenants | `client_secret` (default) | App registration secret from the **Intune** tenant, stored in Key Vault | The managed identity only reads Key Vault. |
| Different tenants, secretless | `federated_managed_identity` | None | Must be set up in two passes, described below |

`deploy.sh` compares the two tenants and refuses `managed_identity` when they
differ. A managed identity is single-tenant and there is no consent path that
grants it app roles in another directory.

**`federated_managed_identity`** needs a multi-tenant app registration in the
subscription's tenant. The job's managed identity is added to that app as a
[federated identity credential][fic], and the app is admin-consented into the
Intune tenant. The federated credential must name the managed identity, and
this template is what creates the identity, so the setup takes two passes:

1. Deploy in `client_secret` mode.
2. Create the federated credential against the identity that deploy produced.
   Its `subject` is the identity's **principal (object) ID**, not its client ID.
3. Redeploy with `GRAPH_AUTH_MODE=federated_managed_identity`.

`deploy.sh` cannot check the other tenant's side of this, so trigger a run
manually after step 3. Be realistic about the cost: this is several moving
parts across two directories, and it saves you from rotating one Key Vault
secret.

[fic]: https://learn.microsoft.com/entra/workload-id/workload-identity-federation-config-app-trust-managed-identity

## 2. Prove ServiceNow from your workstation

Which write API an OAuth client may call is set per instance and per HTTP
method. The only reliable way to know is to probe it. Do that from your
workstation, where a failure is one command and not a scheduled job's log.
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

## 3. Build the image

The image comes from an **Azure Container Registry**, so nothing in the
production path leaves Azure. You create the registry once, and every
environment shares it.

```bash
# Once. The name must be globally unique, 5-50 letters and digits.
az acr create --resource-group rg-intune-cmdb-sync --name <registry> --sku Basic

# Let the service principal pull. The job authenticates as this principal,
# not as its managed identity.
az role assignment create --assignee <service principal client id> --role AcrPull \
  --scope "$(az acr show --name <registry> --query id -o tsv)"

# Build and push, from the repo root. Azure does the build, so you do not need
# Docker locally.
az acr build --registry <registry> --image intune-cmdb-sync:1.0.0 .
```

**Tag every build with a version, never `latest`.** Each deploy pins one tag.
That lets DEV run a new build while PROD stays on the previous one, and a
rollback is a redeploy of the old tag. `deploy.sh` refuses a tag that does not
exist. It also warns when the service principal has no pull role, so both
mistakes show up at deploy time and not at the first scheduled run.

### Pulling with a managed identity instead

The service principal is the default, as required. To remove the registry
secret and its rotation for one environment, redeploy with
`ACR_AUTH_MODE=managed_identity` and leave `ACR_CLIENT_ID` and
`ACR_CLIENT_SECRET` unset. `deploy.sh` then grants that stack's identity
`AcrPull` on the registry. If you lack the rights to do that, it stops and
prints the exact `az role assignment create` command for someone who has them.
Switch one environment at a time.

## 4. Deploy in dry-run

Use a small per-environment file for the non-secret settings, so a redeploy
reuses exactly the values you tested. Keep it out of git.

```bash
# dev.env: no secrets in this file
NAME_PREFIX=intunecmdb-dev
RESOURCE_GROUP=rg-intune-cmdb-sync
LOCATION=eastus
SNOW_INSTANCE=acmedev
SNOW_CLIENT_ID=<from the Application Registry entry>
SNOW_WRITE_MODE=identify_reconcile     # the mode --check-api allowed
SNOW_DISCOVERY_SOURCE=Intune           # exactly as registered, including case
ACR_NAME=<registry>
IMAGE_TAG=1.0.0
ACR_CLIENT_ID=<service principal client id>
CRON="15 3 * * *"                      # UTC
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
read -rs -p "ACR service principal secret: " ACR_CLIENT_SECRET; echo; export ACR_CLIENT_SECRET
# client_secret mode only:
# read -rs -p "Graph client secret: " GRAPH_CLIENT_SECRET; echo; export GRAPH_CLIENT_SECRET

./deploy.sh
```

`deploy.sh` refuses to run unless `DRY_RUN` is exactly `true` or `false`.
There is no default, so a redeploy can't switch a stack from dry-run to live
because you forgot the variable. Keeping `DRY_RUN` in the environment file
means every redeploy repeats the value you last chose on purpose.

Before you go further, read the summary the script prints. Check the
subscription, the instance, the image tag, `Dry run`, `Class map`, and the
`Alerts` line. If `Alerts` says `NONE`, you will not be told when runs stop.

## 5. Trigger and verify a dry run

Don't wait for the schedule. Trigger a run now:

```bash
JOB="${NAME_PREFIX}-job"
az containerapp job start --name "$JOB" --resource-group "$RESOURCE_GROUP"
az containerapp job execution list --name "$JOB" --resource-group "$RESOURCE_GROUP" -o table
```

When the execution shows `Succeeded`, read the run summary:

```bash
WORKSPACE=$(az monitor log-analytics workspace show \
  -g "$RESOURCE_GROUP" -n "${NAME_PREFIX}-logs" --query customerId -o tsv)

az monitor log-analytics query --workspace "$WORKSPACE" --analytics-query "
  ContainerAppConsoleLogs_CL
  | where ContainerJobName_s == '$JOB'
  | extend p = parse_json(Log_s)
  | where p.msg in ('starting intune-cmdb-sync', 'run complete')
  | project TimeGenerated, msg = tostring(p.msg), dry_run = p.dry_run,
            inserted = p.inserted, updated = p.updated, errors = p.errors" -o table
```

Logs can take a few minutes to reach Log Analytics.

Gate:

- The `starting` line shows `dry_run: true`. If it shows `false`, the stack is
  live. Go to [Emergency stop](#emergency-stop).
- `run complete` shows `errors: 0`.
- The counts are plausible for your fleet.

The job sets `FAIL_ON_ERROR=true`, so an execution that shows `Failed` means at
least one device failed. That flag is not the only cause, though: exit 4
(degraded) also fails the execution. Look at the log lines just before
`run complete` to find out which.

This is the first time the **Graph** half runs with the job's own credential.
In `managed_identity` and `federated_managed_identity` modes, nothing earlier
could test it.

## 6. First real write, limited

The Azure job has no device limit setting, so do the first real write from
your workstation. Use the configuration that passed stage 2, with retirement
off:

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
way as in stage 4. The summary now reads `Dry run  false  -- LIVE`. Keep `RETIRE_MISSING=false`. Then either trigger a run as in
stage 5 or wait for the schedule, and read the `run complete` line.

Gate: `errors: 0`, and the inserted count is roughly your fleet size minus the
CIs that already exist. On a second run, expect NO_CHANGE and UPDATE, not
INSERT. With the overrides file dropping `last_discovered`, UPDATE means
something actually changed. Without it, every device reports UPDATE.

## 8. Enable retirement (optional, later)

Retirement PATCHes `install_status` on CIs whose devices have left Intune. It
is the only way this job ever marks a CI as gone. Before you enable it:

- [ ] Several live runs have been clean, so `state.json` on the file share maps
  the whole fleet.
- [ ] `install_status=7` really means *retired* **on this instance**. That is a
  convention, not a guarantee, so check the choice list.
- [ ] State persistence is on (the default). Without it the job cannot know
  what disappeared.

Then redeploy with `RETIRE_MISSING=true`. A guard skips retirement and marks
the run degraded when more than 10% of known devices vanish at once. That
catches a partial Graph response or a wrong tenant before they turn into a
mass retirement.

## Emergency stop

To stop writes **immediately**:

```bash
az containerapp job update --name "${NAME_PREFIX}-job" --resource-group "$RESOURCE_GROUP" \
  --set-env-vars DRY_RUN=true SNOW_RETIRE_MISSING=false
az containerapp job stop --name "${NAME_PREFIX}-job" --resource-group "$RESOURCE_GROUP"  # a run in progress
```

The next `deploy.sh` overwrites that change. To make it stick, set
`DRY_RUN=true` in the environment file and redeploy. A dry-run job keeps
running and keeps logging, so you still see what it would do and the
*no successful run* alert keeps working.

To roll back a bad build, redeploy with the previous `IMAGE_TAG`.

## What the job is configured with

These are the complete settings the job receives. Nothing else from a local
`.env` reaches it.

| Variable | Set from |
| --- | --- |
| `SNOW_INSTANCE`, `SNOW_CLIENT_ID`, `SNOW_CLIENT_SECRET` | `deploy.sh` input; the secret goes through Key Vault |
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
| `STATE_PATH`, `RUN_REPORT_PATH`, `RUN_REPORT_DEVICES` | the file share, when state persistence is on |

Every other setting uses the connector's built-in default.

## Multiple ServiceNow environments

To feed more than one ServiceNow instance from the same Intune tenant, such as
DEV and PROD, run `deploy.sh` once per instance into the same resource group,
each time with a different `NAME_PREFIX`. Every resource name is derived from
the prefix, so each run creates a separate stack. In practice that means one
environment file per instance, `dev.env` and `prod.env`, following the stage 4
pattern.

**Settings that differ per environment:** the `SNOW_*` credentials, the
`SNOW_DISCOVERY_SOURCE` value (registered separately on each instance), usually
`SNOW_WRITE_MODE`, `IMAGE_TAG`, and `CRON`. Auth scopes are configured per
instance, so DEV may allow the IRE API while PROD refuses it. Run `--check-api`
against each instance. Offset the cron times so the two runs are easy to tell
apart in the logs.

**Settings shared by both:** the Graph settings (one Intune tenant) and the
registry with its pulling service principal.

**Resources each environment gets for itself:** Key Vault, managed identity,
Container Apps Job, alert rules, and storage account. The separate storage
account is a correctness requirement. `state.json` maps Intune device IDs to
ServiceNow `sys_id`s, and a `sys_id` means something only on the instance that
issued it. If DEV and PROD shared a state file, DEV's IDs would drive PROD's
retirement decisions. **Do not consolidate environments onto shared storage.**

**Promote independently.** DEV runs through every stage before PROD starts. A
new build follows the same path: `az acr build` a new tag, redeploy DEV onto
it, and change PROD's `IMAGE_TAG` only after DEV has run cleanly. Re-running
`deploy.sh` for one prefix does not touch the other.

In `managed_identity` mode, each prefix creates its own identity and grants
Graph permissions to it, so each deploy needs the admin role from stage 1.

**Removing one environment.** `az group delete` removes every environment in
the group. To remove only one, delete its resources by prefix. Run the list on
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
removed. If a delete fails because the job still depends on its environment,
run the command again. Key Vault soft-delete keeps the vault name reserved for
7 days afterwards (see [Teardown](#teardown)).

## What gets created

For each `NAME_PREFIX`:

| Resource | Name | Purpose |
| --- | --- | --- |
| User-assigned managed identity | `<prefix>-id` | Key Vault access, and Graph authentication in `managed_identity` mode |
| Key Vault | `<prefix>kv<hash>` | The ServiceNow secret, the registry service principal's secret (unless `ACR_AUTH_MODE=managed_identity`), and the Graph secret in `client_secret` mode |
| Log Analytics workspace | `<prefix>-logs` | Job logs, 30-day retention |
| Container Apps environment | `<prefix>-env` | Runtime for the job |
| Container Apps Job | `<prefix>-job` | The scheduled sync. A failed run is retried once. |
| Storage account + file share | `<prefix>st<hash>` | `state.json` and `run-report.json` |
| Alert rules + action group | `<prefix>-alerts` and others | Only when `ALERT_EMAIL` is set |

The identity is user-assigned on purpose, not system-assigned. That way the
Graph permission grant survives the job being deleted and recreated.

This template does not create the Container Registry or the service principal
that pulls from it. Every environment shares them.

## Cost

List prices, East US, one 5-minute run per day at 0.5 vCPU / 1 GiB.

| | Usage/month | Cost |
| --- | --- | --- |
| Container Apps Jobs | ~4,500 vCPU-s, ~9,000 GiB-s | **$0.00**: the free grant is 180,000 vCPU-s and 360,000 GiB-s |
| Log Analytics | a few MB | **$0.00**: the first 5 GB/month is free |
| Key Vault (standard) | ~30–60 secret reads | **~$0.00**: no monthly fee, ~$0.03/10,000 operations |
| Storage (Standard LRS file share) | 1 GiB quota, a few hundred KB used | **~$0.06** |
| **Per environment** | | **under $0.10/month** |
| Container Registry (Basic) | shared by every environment | **~$5.00** |
| **Total, any number of environments** | | **about $5/month** |

The free grant is per subscription, so environments share it. The registry
accounts for nearly the whole bill. It exists because production may only use
Azure resources, which rules out a free public registry.

To drop the storage account, deploy with `enableStatePersistence=false`. You
lose retirement and the persisted run report. Everything else works.

## Alerting

Set `ALERT_EMAIL` before running `deploy.sh`, and two alert rules are deployed
with an action group:

| Rule | Fires when | Severity |
| --- | --- | --- |
| `<prefix>-no-successful-run` | no `run complete` line in 24 hours | 1 |
| `<prefix>-device-errors` | a run finished with `errors > 0`, or degraded (retirement guard tripped, state not saved) | 2 |

If `ALERT_EMAIL` is unset, no alert resources are created at all. That is
deliberate, so a deployment is never *almost* monitored.

The first rule matters most. A job that stops firing produces no error for
anyone to notice, and the CMDB goes stale without warning. An expired registry
or Graph secret shows up here. The rule's query ends in
`summarize completed = count()` with **no `by` clause**, which is what makes
absence detectable. That form returns a row of `0` when nothing matched. A
grouped form would return no rows, and the rule would never fire.

## Operating

```bash
RG=rg-intune-cmdb-sync
PREFIX=intunecmdb-dev
JOB=$PREFIX-job

az containerapp job start --name $JOB --resource-group $RG             # run now
az containerapp job execution list --name $JOB --resource-group $RG -o table

WORKSPACE=$(az monitor log-analytics workspace show \
  -g $RG -n $PREFIX-logs --query customerId -o tsv)
```

Logs are structured JSON, so the run summary can be queried directly:

```kusto
ContainerAppConsoleLogs_CL
| where ContainerJobName_s == "intunecmdb-dev-job"
| extend p = parse_json(Log_s)
| where p.msg == "run complete"
| project TimeGenerated,
          inserted = toint(p.inserted),
          updated  = toint(p.updated),
          errors   = toint(p.errors),
          unresolved_users = toint(p.users_unresolved)
```

To isolate one run, filter by `run_id`. It is on every log line and in
`run-report.json`:

```kusto
ContainerAppConsoleLogs_CL
| extend p = parse_json(Log_s)
| where p.run_id == "<id from run-report.json>"
| order by TimeGenerated asc
```

### Changing configuration

Edit the environment file and re-run `deploy.sh`. The script is idempotent.
Changes made with `az containerapp job update` last only until the next
deploy.

### Secret rotation

- **ServiceNow, Graph, or registry secret:** redeploy with the new value.
  Nothing else changes.
- **An expired registry secret stops pulls**, and an expired Graph secret stops
  runs. Either way the *no successful run* alert is what reports it. Put the
  expiry dates in a calendar.

## Teardown

```bash
az group delete --name rg-intune-cmdb-sync --yes
```

This removes **every** environment in the group. To remove only one, see
[Multiple ServiceNow environments](#multiple-servicenow-environments). Key
Vault soft-delete keeps the vault name reserved for 7 days. Use
`az keyvault purge` if you need to reuse it sooner.
