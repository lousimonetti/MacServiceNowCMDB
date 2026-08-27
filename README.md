# intune-cmdb-sync

Sync corporate-owned Microsoft Intune devices into the ServiceNow CMDB, daily,
without a Service Graph Connector licence.

Reads `managedDevice` records from Microsoft Graph, filters to company-owned
hardware, resolves each device's owner to a `sys_user`, and writes
`cmdb_ci_computer` records through ServiceNow's base-platform Identification and
Reconciliation Engine.

Runs as a scheduled container on Azure or AWS for **well under $1/month**.

```
Microsoft Graph                 intune-cmdb-sync                    ServiceNow
──────────────────              ─────────────────                   ──────────────────
GET /deviceManagement           filter: company-owned only
    /managedDevices     ──────► map → cmdb_ci_computer fields
POST /$batch → /users   ──────► resolve owner → sys_user     ──────► POST /api/now
                                resolve manufacturer, model            /identifyreconcile
                                                                       └─► IRE ─► CMDB
```

---

## Why this exists

ServiceNow's [Service Graph Connector for Microsoft Intune][sgc] does this job
well and is built on IntegrationHub ETL, which is separately subscribed. If you
already own IntegrationHub, use it — it is supported, maintained, and has a UI.

If you do not, the underlying capability is still available to you. The
Identification and Reconciliation Engine — the part that actually protects CMDB
data quality by deduplicating and reconciling CIs — is base platform, and it is
exposed to any caller holding `itil` or `asset` through the
[Identification and Reconciliation API][ire-api]. This project does the extract
and transform outside the instance and posts the same shape of payload to that
endpoint.

**What you get:** IRE deduplication, reconciliation, source precedence, and
identity keyed on the Intune device GUID.

**What you give up:** vendor-maintained field mappings, the ETL UI, and
ServiceNow support for the integration. The mappings are documented in
[docs/field-mapping.md](docs/field-mapping.md), and from then on you own them.

[sgc]: https://www.servicenow.com/docs/r/servicenow-platform/service-graph-connectors/cmdb-integration-intune.html
[ire-api]: https://www.servicenow.com/docs/r/api-reference/rest-apis/identification-and-reconciliation-api.html

---

## Quick start

```bash
pip install -e .

cp .env.example .env
$EDITOR .env                     # tenant, ServiceNow instance, credentials

set -a && . ./.env && set +a

intune-cmdb-sync --check         # prove both systems are reachable
intune-cmdb-sync --dry-run       # run IRE identification, write nothing
intune-cmdb-sync                 # commit
```

`--dry-run` is not a simulation. It posts to `/api/now/identifyreconcile/query`,
which runs real identification against your real CMDB and reports the `INSERT` /
`UPDATE` / `NO_CHANGE` each device *would* produce — without committing. Use it
before every configuration change.

Before any of that, both sides need about 30 minutes of setup:

- **[docs/entra-setup.md](docs/entra-setup.md)** — app registration or managed
  identity, and the two Graph permissions.
- **[docs/servicenow-setup.md](docs/servicenow-setup.md)** — integration user,
  OAuth client, and the `Intune` discovery source. Ends with a copy-pasteable
  curl that proves the write path before you schedule anything.

### Documentation

| Document | Audience |
| --- | --- |
| [architecture.md](docs/architecture.md) | How the system is built, and the decisions behind it |
| [metamodel-mapping.md](docs/metamodel-mapping.md) | CMDB owners: classes, identity, reconciliation |
| [field-mapping.md](docs/field-mapping.md) | Exactly what lands on a CI, field by field |
| [engineering-guide.md](docs/engineering-guide.md) | Working on the code |
| [leadership-brief.md](docs/leadership-brief.md) | Cost, risk, status, decisions |

---

## What it does

**Corporate devices only.** `INTUNE_OWNERSHIP=company` is applied as a Graph
`$filter` *and* re-checked client-side on every record. Graph does not formally
document `managedDeviceOwnerType` as filterable, so if the filter is rejected the
connector refetches unfiltered — and the client-side check means personally-owned
hardware still cannot reach the CMDB.

**Stable CI identity.** Every item carries
`sys_object_source_info.source_native_key` = the Intune `managedDevice.id`. IRE
checks that before any identification rule, so a device keeps its CI through
motherboard swaps, corrected serial numbers, and renames.

**Junk serials rejected.** `To be filled by O.E.M.`, `System Serial Number`,
`Default string` and [two dozen others](docs/field-mapping.md#serial-number-normalisation)
are discarded. Left in, they make every affected machine identify as the *same*
CI, which collapses a fleet into one record.

**Owners resolved, carefully.** Entra object ID → Entra user → `sys_user`, trying
`employee_number`, then `email`, then `user_name` (configurable). A key that
matches more than one user is treated as no match: an empty `assigned_to` is
better than a wrong one.

**Reference fields resolved properly.** `manufacturer` and `model_id` are
looked up to sys_ids, because IRE will not resolve a display name and silently
drops what it cannot resolve.

**Throttling handled.** Both APIs return `429` with `Retry-After`; the connector
honours it, then falls back to exponential backoff with jitter.

**Retirement is guarded.** Optional, off by default, and refuses to act when more
than `SNOW_RETIRE_MAX_FRACTION` of known devices vanish at once — because a
partial Graph outage looks exactly like a fleet that disappeared overnight.

---

## Configuration

Every setting is an environment variable. [`.env.example`](.env.example)
documents all of them; these are the ones that matter most.

| Variable | Default | |
| --- | --- | --- |
| `GRAPH_TENANT_ID` | — | **Required.** Entra tenant GUID. |
| `GRAPH_AUTH_MODE` | `client_secret` | `client_secret`, `managed_identity`, `workload_identity`, `default`. |
| `GRAPH_CLIENT_ID` | — | App registration client ID, or the managed identity's client ID. |
| `GRAPH_CLIENT_SECRET` | — | Required for `client_secret`. Also `_FILE` / `_PARAMETER`. |
| `SNOW_INSTANCE` | — | **Required.** `acme`, `acme.service-now.com`, or a full URL. |
| `SNOW_AUTH_MODE` | `oauth_client_credentials` | Or `oauth_password`, `basic`. |
| `SNOW_CLIENT_ID` / `SNOW_CLIENT_SECRET` | — | From the Application Registry entry. |
| `SNOW_WRITE_MODE` | `identify_reconcile` | Or `cmdb_instance`. See below. |
| `SNOW_DISCOVERY_SOURCE` | `Intune` | Must match the `cmdb_ci.discovery_source` choice exactly. |
| `SNOW_CLASS_MAP` | `windows=cmdb_ci_computer;macos=cmdb_ci_computer` | Unmapped OSes are skipped, not guessed. |
| `SNOW_USER_MATCH_ORDER` | `employee_number,email,user_name` | Owner match keys, in order. |
| `SNOW_RETIRE_MISSING` | `false` | Needs `STATE_PATH`. |
| `DRY_RUN` | `false` | |

### Write modes

| | `identify_reconcile` *(default)* | `cmdb_instance` |
| --- | --- | --- |
| Endpoint | `POST /api/now/identifyreconcile` | `POST /api/now/cmdb/instance/{class}` |
| Requests | one per batch (100 CIs) | one per CI |
| Identity | Intune device GUID, then identification rules | identification rules only |
| Survives a serial-number change | yes | no — creates a duplicate |
| Per-CI result detail | `INSERT` / `UPDATE` / `NO_CHANGE` + errors | success or failure only |
| Role | `itil` or `asset` | `itil` |

Use `identify_reconcile`. `cmdb_instance` exists for instances that predate the
IRE API or where it is blocked by policy; it is meaningfully weaker.

---

## Deploying

### Azure Container Apps Jobs — recommended

```bash
export SNOW_INSTANCE=acme SNOW_CLIENT_ID=... SNOW_CLIENT_SECRET=...
./deploy/azure/deploy.sh
```

Provisions a scheduled Container Apps Job, a user-assigned managed identity, Key
Vault for the ServiceNow secret, an Azure Files share for state, and Log
Analytics — then grants the identity its Graph permissions.

A job that only runs for a few minutes a day lands inside the Container Apps
free grant, so the compute is genuinely free rather than merely cheap.

**Check which tenant topology you have first**, because it decides the
credential model:

- **Intune and the Azure subscription share a tenant** — set
  `GRAPH_AUTH_MODE=managed_identity`. The job's managed identity is granted the
  Graph permissions directly and **no Graph credential exists anywhere**.
  Nothing to rotate, nothing to leak. Prefer this whenever you can.
- **They are in different tenants** — the default. A managed identity is
  single-tenant and *cannot* be granted app roles in another directory, so the
  job authenticates as an app registration from the Intune tenant with its
  secret in Key Vault. The managed identity is still used, to read Key Vault
  rather than to reach Graph.

`deploy.sh` refuses to continue if you ask for `managed_identity` across
tenants, rather than deploying something that will only fail at 3am.

Details and cost breakdown: [deploy/azure/README.md](deploy/azure/README.md).

### AWS Lambda

```bash
cd deploy/aws && terraform init && terraform apply
```

Lambda on a container image, triggered by EventBridge Scheduler, secrets in SSM
Parameter Store, state in S3.

Two deliberate choices keep the bill near zero: the function stays **out of a
VPC** (a VPC-attached Lambda needs a NAT gateway to reach Graph, at ~$32/month),
and state goes in S3 rather than EFS, since EFS would force that VPC.

Details: [deploy/aws/README.md](deploy/aws/README.md).

### Anywhere else

The image is a plain container that runs to completion — Kubernetes `CronJob`,
ECS scheduled task, or cron on a VM all work:

```bash
docker build -t intune-cmdb-sync .
docker run --rm --env-file .env intune-cmdb-sync
```

### Cost

| | Azure | AWS |
| --- | --- | --- |
| Compute | $0.00 — inside the Container Apps free grant | ~$0.15 |
| Registry | $0.00 — public image | ~$0.04 (ECR) |
| Scheduler | included | $0.00 — free tier |
| Secrets | ~$0.00 — Key Vault standard | $0.00 — SSM Standard |
| State | ~$0.06 — Azure Files | ~$0.00 — S3 |
| Logs | $0.00 — under the 5 GB free tier | $0.00 — under the 5 GB free tier |
| **Total** | **< $0.10/month** | **~$0.20/month** |

List prices, single daily run, ~5 minutes. Both are rounding errors; pick on
credential model, not cost. Full workings are in the deployment READMEs.

---

## Operating it

### Run report

`--report` writes a machine-readable summary suitable for alerting:

```jsonc
{
  "duration_seconds": 74.2,
  "devices_fetched": 4210,
  "devices_after_ownership_filter": 3987,
  "devices_skipped_no_class": 219,          // mobile, no class mapped
  "devices_skipped_no_identifier": 4,       // no usable serial or name
  "inserted": 12, "updated": 3901, "unchanged": 70, "errors": 0,
  "users_resolved": 3702, "users_unresolved": 285,
  "unresolved_references": { "manufacturers": [], "models": ["Latitude 7455"] },
  "warnings": []
}
```

Worth alerting on: `errors > 0`, `devices_after_ownership_filter` dropping
sharply, a non-empty `warnings`, and a rising `users_unresolved`.

Add `--report-devices` for per-device outcomes when investigating.

### Exit codes

| | |
| --- | --- |
| `0` | Completed. Individual device failures may still have occurred — check the report. |
| `2` | Configuration invalid. Every problem is listed at once, not one per run. |
| `3` | The run failed (auth, connectivity, a whole batch rejected). A partial report is still written to `--report`. |
| `4` | Completed, but degraded: a safety guard tripped or the state file could not be written. Also returned for device-level errors when `--fail-on-error` is set. |

A degraded run (`4`) means the connector ran but did not do its whole job — the
mass-retirement guard refused to act, or the CMDB writes landed while the state
file did not. Both leave the *next* run unable to reason about the fleet, so
they are reported to the scheduler rather than only to the log. The `degraded`
array in the JSON report lists the specific conditions.

### Logging

JSON by default, so Log Analytics and CloudWatch Insights can query fields
directly. `LOG_FORMAT=text` for readable local output. Anything whose key looks
like a credential is redacted before it reaches a handler.

---

## Repository layout

```
src/intune_cmdb_sync/
  config.py            every setting, validated up front — all errors at once
  graph.py             Graph client: paging, $batch, ownership filter + fallback
  mapping.py           managedDevice → cmdb_ci_computer. Pure, fully tested.
  user_resolver.py     Entra user → sys_user, ordered keys, ambiguity-safe
  reference_resolver.py manufacturer/model display names → sys_ids
  servicenow/
    auth.py            client_credentials / password / basic
    writers.py         the two CMDB write paths
  sync.py              orchestration
  state.py             Intune id → CI sys_id, for retirement
  storage.py           state on local disk or S3
  secrets.py           literal / _FILE / _PARAMETER indirection

servicenow-app/        ServiceNow SDK (Fluent) app: discovery source, role, properties
deploy/azure/          Bicep + deploy script
deploy/aws/            Terraform + Lambda Dockerfile
docs/                  setup guides, design documents, field mapping reference
tests/                 269 tests, both APIs mocked at the HTTP boundary
```

The `servicenow-app/` directory ships no runtime logic. It captures the
platform-side configuration as code — the discovery source choice, the
integration role, the optional `sys_user` column — so it is versioned and
reproducible across dev/test/prod instead of clicked in by hand. Everything it
does can also be done manually; [docs/servicenow-setup.md](docs/servicenow-setup.md)
covers both.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/pytest -q                 # 269 tests, no network
.venv/bin/ruff check src/ tests/
.venv/bin/mypy
```

Tests mock at the HTTP boundary with `respx`, so request shapes — the exact IRE
payload, the `$batch` chunking, the encoded queries — are asserted rather than
assumed.

### A note on SDKs

Token acquisition uses `azure-identity`, Microsoft's first-party auth SDK, so
client secrets, workload-identity federation, and managed identity all work
through one code path with SDK-managed caching and refresh.

The Graph *data* calls are plain REST rather than `msgraph-sdk`. The surface used
here is four endpoints, and the Kiota dependency tree adds substantial weight to
an image whose entire job is one HTTP loop a day. The endpoints are documented
and stable; the tradeoff is deliberate.

The ServiceNow SDK (Fluent) is used for what it is actually for — authoring the
scoped application in `servicenow-app/`.

---

## Limitations

- **Computers only by default.** Windows and macOS map to `cmdb_ci_computer`.
  Mobile devices are skipped unless you add a class mapping, because the correct
  handheld class name varies by instance and release and this connector will not
  guess one.
- **No software inventory.** The paid connector also imports installed
  applications into `cmdb_sam_sw_install`. This does not.
- **No relationships.** No `cmdb_rel_ci` edges are created.
- **Full sync each run.** There is no delta query for `managedDevices` in Graph
  v1.0. A daily full pass over even a large tenant is a few minutes, so this is
  a cost worth paying for correctness.
- **`install_status` values are instance-specific.** `7` for retired is the
  common convention, not a guarantee. Verify yours before enabling retirement.

---

## Contributing

Issues and pull requests welcome. Please keep `pytest`, `ruff`, and `mypy`
passing, and add a test for any behaviour change — particularly anything that
alters what gets written to a CI.

## License

[Apache 2.0](LICENSE).

Not affiliated with, endorsed by, or supported by ServiceNow or Microsoft.
ServiceNow is a trademark of ServiceNow, Inc. Microsoft, Intune, and Entra are
trademarks of the Microsoft group of companies.
