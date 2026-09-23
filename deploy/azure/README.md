# Azure deployment

A Container Apps Job that runs the sync on a cron schedule.

## First: which tenant topology do you have?

This decides the credential model, and getting it wrong produces a deployment
that only fails at 3am.

```bash
az account show --query tenantId -o tsv   # your subscription's tenant
```

Compare that to the tenant where **Intune** lives.

### Same tenant — `GRAPH_AUTH_MODE=managed_identity`

The best case, and the reason to prefer Azure as a host: **no Graph credential
exists**. A user-assigned managed identity is granted the Graph application
permissions directly, so there is nothing to store, rotate, or leak. The only
secret in the deployment is the ServiceNow one, in Key Vault, never written into
the job definition.

### Different tenants — `GRAPH_AUTH_MODE=client_secret` (the default)

A managed identity is single-tenant. It **cannot** be granted app roles in
another directory — there is no consent path for that. So the job authenticates
as an app registration belonging to the *Intune* tenant, with its client secret
in Key Vault alongside the ServiceNow one. The managed identity is still there,
used to read Key Vault rather than to reach Graph.

`deploy.sh` compares the two tenants and refuses `managed_identity` when they
differ.

### Different tenants, no secret — `GRAPH_AUTH_MODE=federated_managed_identity`

A multi-tenant app registration plus a user-assigned managed identity **both in
the subscription's tenant**, the identity added to the app as a
[federated identity credential][fic], and the app admin-consented into the
Intune tenant. Genuinely secretless, GA, and supported here.

The catch is ordering: the federated credential has to name the managed
identity, and this template *creates* that identity. So deploy once in
`client_secret` mode, create the credential against the identity the deploy
produced, then redeploy in this mode. Its `subject` is the identity's
**principal (object) id**, not its client id.

Weigh it honestly — it is several moving parts across two directories to avoid
rotating one Key Vault secret.

[fic]: https://learn.microsoft.com/entra/workload-id/workload-identity-federation-config-app-trust-managed-identity

## Container image

The job runs a container image, and it comes from an **Azure Container
Registry** so that nothing in the production path leaves Azure. The registry is
created once and shared by every environment; `deploy.sh` does not create it.

```bash
# Once. The name must be globally unique, 5-50 letters and digits.
az acr create --resource-group rg-intune-cmdb-sync --name <registry> --sku Basic

# Let the existing Entra service principal pull from it. The job authenticates
# as this principal, not as its managed identity.
az role assignment create --assignee <service principal client id> --role AcrPull \
  --scope "$(az acr show --name <registry> --query id -o tsv)"

# Build and push, from the repo root. Azure builds it: no local Docker needed.
az acr build --registry <registry> --image intune-cmdb-sync:1.0.0 .
```

Tag with a version, not `latest`. Each deploy pins one tag, which is what lets
DEV run a new build while PROD stays on the previous one.

The service principal's secret is stored in each stack's Key Vault and read by
the job at pull time. When it expires, pulls fail and the job stops running, and
the *no successful run* alert is what tells you. Rotate it with a redeploy.

`deploy.sh` checks that the tag exists and warns if the service principal has no
pull role on the registry, so either mistake surfaces at deploy time rather than
at the first scheduled run.

### Pulling with a managed identity instead

The service principal is the default. To switch an environment to its own
managed identity, which removes the registry secret and its rotation, redeploy
with:

```bash
ACR_AUTH_MODE=managed_identity ./deploy.sh    # ACR_CLIENT_ID/SECRET not needed
```

Each environment's identity then needs `AcrPull` on the shared registry.
`deploy.sh` grants it after deploying, so the person running it needs rights to
create role assignments on the registry (Owner, User Access Administrator, or
Role Based Access Control Administrator). Without them it stops and prints the
exact `az role assignment create` for someone who has them. Switch one
environment at a time. DEV can pull with its identity while PROD stays on the
service principal.

## Deploy

```bash
export SNOW_INSTANCE=acme
export SNOW_CLIENT_ID=<from the Application Registry entry>
export SNOW_CLIENT_SECRET=<from the Application Registry entry>

# Required: the image, and the service principal that pulls it.
export ACR_NAME=<registry>
export IMAGE_TAG=1.0.0
export ACR_CLIENT_ID=<service principal client id>
export ACR_CLIENT_SECRET=<service principal secret>

# identify_reconcile (default) or cmdb_instance. Which one works is decided per
# instance by its OAuth auth scopes -- run `intune-cmdb-sync --check-api` first.
export SNOW_WRITE_MODE=identify_reconcile

# Same tenant as Intune (simplest -- no Graph credential exists at all):
export GRAPH_AUTH_MODE=managed_identity

# Different tenant from Intune instead? Drop the line above and set these,
# using an app registration from the INTUNE tenant:
# export GRAPH_TENANT_ID=<intune tenant id>
# export GRAPH_CLIENT_ID=<app registration client id>
# export GRAPH_CLIENT_SECRET=<app registration secret>

# optional
export RESOURCE_GROUP=rg-intune-cmdb-sync
export LOCATION=eastus
export CRON="15 3 * * *"          # 03:15 UTC daily
export DRY_RUN=true               # strongly recommended for the first deploy

./deploy.sh
```

In `managed_identity` mode the script also grants the identity
`DeviceManagementManagedDevices.Read.All` and `User.Read.All`, which needs
Privileged Role Administrator, Cloud Application Administrator, or Global
Administrator — app-role assignments live in Entra, not ARM, so they cannot come
from the Bicep template.

In `client_secret` mode those permissions belong to the app registration in the
Intune tenant and must already be consented there; the script prints a reminder
rather than attempting a grant it has no rights to make.

Deploy with `DRY_RUN=true` first, trigger a run, read the logs, then redeploy
with `DRY_RUN=false`.

## Multiple ServiceNow environments

To feed more than one ServiceNow instance (say a DEV and a PROD) from the
same Intune tenant, run `deploy.sh` once per instance into the same resource
group, with a different `NAME_PREFIX` each time. Every resource name derives
from the prefix, so each run creates a completely separate stack.

```bash
export RESOURCE_GROUP=rg-intune-cmdb-sync

# One registry and one pulling service principal for both.
export ACR_NAME=<registry>
export ACR_CLIENT_ID=<service principal client id>
export ACR_CLIENT_SECRET=<service principal secret>

# Graph settings are the same for both: one Intune tenant, one set of devices.
export GRAPH_TENANT_ID=<intune tenant id>
export GRAPH_CLIENT_ID=<app registration client id>
export GRAPH_CLIENT_SECRET=<app registration secret>

# DEV
NAME_PREFIX=intunecmdb-dev \
SNOW_INSTANCE=acmedev \
SNOW_CLIENT_ID=<dev client id> \
SNOW_CLIENT_SECRET=<dev client secret> \
SNOW_DISCOVERY_SOURCE=Intune \
IMAGE_TAG=1.1.0 \
CRON="15 3 * * *" \
DRY_RUN=true \
  ./deploy.sh

# PROD
NAME_PREFIX=intunecmdb-prod \
SNOW_INSTANCE=acme \
SNOW_CLIENT_ID=<prod client id> \
SNOW_CLIENT_SECRET=<prod client secret> \
SNOW_DISCOVERY_SOURCE=Intune \
IMAGE_TAG=1.0.0 \
CRON="45 3 * * *" \
DRY_RUN=true \
  ./deploy.sh
```

**What differs per environment:** the `SNOW_*` credentials, usually
`SNOW_DISCOVERY_SOURCE`, and possibly `SNOW_WRITE_MODE`. OAuth auth scopes are
configured per instance, so DEV may allow the IRE API while PROD refuses it (or
the reverse). Run `--check-api` against each and set the mode per deploy. Each
instance has its own `cmdb_ci.discovery_source` choice list, and the value must
be registered on *each* one
([servicenow-setup.md](../../docs/servicenow-setup.md) section 5). Offsetting
`CRON` keeps the two runs apart in the logs. `IMAGE_TAG` is per environment
too: the example has DEV on a newer build than PROD.

**What each environment gets on its own:** Key Vault, managed identity,
Container Apps Job, alert rules, and storage account. The separate storage
account is a correctness requirement, not tidiness. `state.json` maps Intune
device IDs to ServiceNow `sys_id`s, and a `sys_id` is only meaningful on the
instance that issued it. A state file shared between DEV and PROD would feed
DEV's IDs into PROD's retirement decisions. Separate storage accounts make that
impossible rather than something to be careful about.

**Promote independently.** Flip DEV to `DRY_RUN=false` first, check the CIs it
writes, and only then redeploy PROD with `DRY_RUN=false`. New builds follow the
same path: `az acr build` a new tag, redeploy DEV onto it, and move PROD's
`IMAGE_TAG` once DEV has run cleanly. Re-running `deploy.sh`
for one prefix does not touch the other.

**In `managed_identity` mode**, each prefix creates its own identity, and each
`deploy.sh` run grants Graph permissions to its own. Both need the admin role
described above.

**Teardown is the catch.** `az group delete` removes every environment in the
group. To remove one environment only, delete its resources by prefix:

```bash
az resource list -g "$RESOURCE_GROUP" \
  --query "[?starts_with(name, 'intunecmdb-dev') || starts_with(name, 'intunecmdbdev')].id" \
  -o tsv | xargs -r az resource delete --ids
```

The second pattern catches the storage account, whose name has the hyphen
stripped. Run the `az resource list` on its own first and read what it matches.
If a delete fails because the job still depends on its environment, run the
same command again. Key Vault soft-delete then keeps the vault name reserved
for 7 days (see Teardown below).

**Cost** barely moves per environment. The registry is shared, and the Container
Apps free grant (180,000 vCPU-s per month) is per subscription, not per job, so
the environments share it too. Each uses about 4,500 vCPU-s a month.

## What gets created

Per `NAME_PREFIX`:

| Resource | Purpose |
| --- | --- |
| User-assigned managed identity | Key Vault access, and Graph authentication in `managed_identity` mode |
| Key Vault | The ServiceNow client secret, the registry service principal's secret (unless `ACR_AUTH_MODE=managed_identity`), plus the Graph secret in `client_secret` mode |
| Log Analytics workspace | Job logs, 30-day retention |
| Container Apps environment | Runtime for the job |
| Container Apps Job | The scheduled sync itself |
| Storage account + file share | State file, for retirement (skippable) |

User-assigned rather than system-assigned identity, deliberately: the Graph
permission grant survives the job being deleted and recreated, which
system-assigned would not.

Not created here: the Azure Container Registry and the service principal that
pulls from it. Both are shared by every environment and exist before the first
deploy (see Container image).

## Cost

List prices, East US, one 5-minute run per day at 0.5 vCPU / 1 GiB.

| | Usage/month | Cost |
| --- | --- | --- |
| Container Apps Jobs | ~4,500 vCPU-s, ~9,000 GiB-s | **$0.00** — free grant is 180,000 vCPU-s and 360,000 GiB-s |
| Log Analytics | a few MB | **$0.00** — first 5 GB/month free |
| Key Vault (standard) | ~30–60 secret reads | **~$0.00** — no monthly fee, ~$0.03/10,000 operations |
| Storage (Standard LRS file share) | 1 GiB quota, a few hundred KB used | **~$0.06** |
| **Per environment** | | **under $0.10/month** |
| Container Registry (Basic) | shared by every environment | **~$5.00** |
| **Total, any number of environments** | | **about $5/month** |

The registry is nearly the whole bill. It is there because production may only
use Azure resources, which rules out a free public registry; Basic is the
smallest tier and far more than one small image needs.

Nothing is provisioned between runs (Consumption workload profile), so the jobs
themselves have no idle cost.

To drop the storage account entirely, set `enableStatePersistence=false`. You
lose the ability to retire CIs for devices that leave Intune; everything else
works.

## Operating

```bash
RG=rg-intune-cmdb-sync
JOB=intunecmdb-job

# run now
az containerapp job start --name $JOB --resource-group $RG

# execution history
az containerapp job execution list --name $JOB --resource-group $RG -o table

# logs
WORKSPACE=$(az monitor log-analytics workspace show \
  -g $RG -n intunecmdb-logs --query customerId -o tsv)

az monitor log-analytics query --workspace "$WORKSPACE" --analytics-query "
  ContainerAppConsoleLogs_CL
  | where ContainerJobName_s == '$JOB'
  | order by TimeGenerated desc
  | take 100
  | project TimeGenerated, Log_s"
```

### Alerting

Set `ALERT_EMAIL` before `./deploy.sh` and two alert rules are deployed with an
action group:

| Rule | Fires when | Severity |
| --- | --- | --- |
| `<prefix>-no-successful-run` | no `run complete` line in 24 hours | 1 |
| `<prefix>-device-errors` | a run finished with `errors > 0`, or degraded (retirement guard tripped, state not saved) | 2 |

```bash
export ALERT_EMAIL=ops@example.com
./deploy.sh
```

Leave it unset and no alert resources are created at all — deliberately, so a
deployment is never *almost* monitored.

The first rule is the one that matters. A job that stops firing produces no
error to notice; the CMDB just goes quietly stale. Its query ends in
`summarize completed = count()` with **no `by` clause**, which is what makes
absence detectable: that form returns a row of `0` when nothing matched, where a
grouped form would return no rows and the rule would never fire.

Because logs are structured JSON, the run summary is also directly queryable:

```kusto
ContainerAppConsoleLogs_CL
| where ContainerJobName_s == "intunecmdb-job"
| extend p = parse_json(Log_s)
| where p.msg == "run complete"
| project TimeGenerated,
          inserted = toint(p.inserted),
          updated  = toint(p.updated),
          errors   = toint(p.errors),
          unresolved_users = toint(p.users_unresolved)
```

Both deployed rules use exactly this shape. Filter by `run_id` to isolate a
single run:

```kusto
ContainerAppConsoleLogs_CL
| extend p = parse_json(Log_s)
| where p.run_id == "<id from run-report.json>"
| order by TimeGenerated asc
```

## Changing configuration

Re-running `deploy.sh` is safe and idempotent. For a single setting:

```bash
az containerapp job update --name $JOB --resource-group $RG \
  --set-env-vars SNOW_RETIRE_MISSING=true
```

## Teardown

```bash
az group delete --name rg-intune-cmdb-sync --yes
```

Key Vault has soft-delete enabled with a 7-day retention, so the vault name is
reserved for a week. Use `az keyvault purge` if you need to reuse it sooner.
