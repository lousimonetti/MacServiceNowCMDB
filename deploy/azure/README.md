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

## Deploy

```bash
export SNOW_INSTANCE=acme
export SNOW_CLIENT_ID=<from the Application Registry entry>
export SNOW_CLIENT_SECRET=<from the Application Registry entry>

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
export CONTAINER_IMAGE=ghcr.io/your-org/intune-cmdb-sync:latest
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

## What gets created

| Resource | Purpose |
| --- | --- |
| User-assigned managed identity | Key Vault access, and Graph authentication in `managed_identity` mode |
| Key Vault | The ServiceNow client secret, plus the Graph secret in `client_secret` mode |
| Log Analytics workspace | Job logs, 30-day retention |
| Container Apps environment | Runtime for the job |
| Container Apps Job | The scheduled sync itself |
| Storage account + file share | State file, for retirement (skippable) |

User-assigned rather than system-assigned identity, deliberately: the Graph
permission grant survives the job being deleted and recreated, which
system-assigned would not.

## Cost

List prices, East US, one 5-minute run per day at 0.5 vCPU / 1 GiB.

| | Usage/month | Cost |
| --- | --- | --- |
| Container Apps Jobs | ~4,500 vCPU-s, ~9,000 GiB-s | **$0.00** — free grant is 180,000 vCPU-s and 360,000 GiB-s |
| Log Analytics | a few MB | **$0.00** — first 5 GB/month free |
| Key Vault (standard) | ~30–60 secret reads | **~$0.00** — no monthly fee, ~$0.03/10,000 operations |
| Storage (Standard LRS file share) | 1 GiB quota, a few hundred KB used | **~$0.06** |
| **Total** | | **under $0.10/month** |

Two choices that keep it there:

- **No Azure Container Registry.** The image comes from a public registry. ACR
  Basic is ~$5/month, which would be fifty times the cost of everything else
  combined. Point `containerImage` at your own ACR if policy requires a private
  registry, and accept that line item knowingly.
- **Consumption workload profile.** Nothing is provisioned between runs, so there
  is no idle cost.

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
| `<prefix>-device-errors` | a run finished with `errors > 0` | 2 |

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
