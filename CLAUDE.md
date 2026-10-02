# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A connector that syncs corporate-owned Microsoft Intune devices into the
ServiceNow CMDB through the base-platform Identification and Reconciliation
Engine (IRE). No Service Graph Connector subscription, no paid plugin.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/pytest -q          # no network, no credentials needed
.venv/bin/ruff check src/ tests/
.venv/bin/mypy
```

All three must pass before any change is considered done. Add a test for any
behaviour change, particularly anything altering what gets written to a CI.

## Architecture notes that are not obvious from the code

- **The device sync never PATCHes.** It POSTs the full attribute set to
  `/api/now/identifyreconcile` and lets IRE decide INSERT / UPDATE / NO_CHANGE.
  There is exactly one PATCH in the codebase — retirement, in `sync.py`.
- **There is no client-side change detection.** Every field in
  `DeviceMapper.build_values` is sent every run, so any field whose value moves
  between runs makes every device an UPDATE. `last_discovered`
  (`lastSyncDateTime`) is the classic offender; it is dropped via
  `MAPPING_OVERRIDES_FILE` in local setups. Check that before adding a field.
- **Unresolved reference fields are omitted, not blanked.** A failed
  `manufacturer` / `model_id` / `assigned_to` lookup leaves the existing CI
  value alone rather than clearing it. Preserve that property.
- **Observability is `run_id` + the report.** Every log line is stamped with a
  `run_id` and the report carries the same value; IRE errors additionally carry
  ServiceNow's `logContextId`. Do not put a run id on the CI itself — a value
  that changes every run makes every device an UPDATE every run, the exact churn
  that dropping `last_discovered` removed.
- **Exit code 4 means degraded**, not merely "device errors". A tripped
  mass-retirement guard or a failed state write returns 4 even without
  `--fail-on-error`, because both leave the *next* run unable to reason about
  the fleet. See `RunReport.degraded`.
- **`intune-cmdb-query` is the read-only half.** `cmdb_report.py` + `query_cli.py`
  issue `GET /api/now/table/...` and nothing else, so they run against an
  instance whose write path is blocked by the unscoped-api gate. It reads
  with `sysparm_display_value=all` because `manufacturer` / `model_id` /
  `assigned_to` are references whose raw value is a sys_id; `query_table` keeps
  asking for raw values because the resolvers depend on that, so the reader
  issues its own request rather than adding a mode to a shared method. Keep it
  write-free — that property is what makes it safe to point at production.

- **Multiple ServiceNow environments = one Azure stack per `NAME_PREFIX`.**
  DEV and PROD run from one resource group by running `deploy.sh` once per
  instance with a distinct prefix. Every resource name derives from the prefix,
  so each environment gets its own identity, App Service plan and app. The
  separate app is load-bearing: `state.json` lives on that app's `/home` and
  holds instance-specific `sys_id`s, and a shared one would drive PROD
  retirement from DEV IDs. Do not point two environments at one app or one state
  path. See `deploy/azure/README.md`.
- **Resource groups are pre-provisioned; `deploy.sh` never creates or deletes
  one.** DEV deploys into the existing `azc-obm-development`. `RESOURCE_GROUP`
  has no default: when unset, `deploy.sh` lists the subscription's groups and
  asks the user to pick one, and a non-interactive run fails instead of
  prompting. The script fails if the group is missing. Resources go to
  **East US** (`LOCATION` defaults to `eastus`) by requirement, regardless of
  the group's region. Teardown is by prefix, never `az group delete`:
  the group may hold other teams' resources.

- **Azure runs on App Service as a scheduled WebJob — because of the landing
  zone, not by preference.** ACR and Container Apps are denied, and Key Vault
  and Storage must deny public access (see Constraints). **Every Azure Functions
  app needs a storage account**, so Functions forced VNet integration plus
  private endpoints, and the landing zone's VNet (`vpcx-vnet-eastus`,
  `10.52.46.0/26`, in `VPCXRG`) had no room for the /27 Flex needs. That
  version is kept on branch `azure-functions-vnet`. A Linux App Service app
  needs no storage account of ours: code, `state.json` and the run report live
  on its persistent `/home` (`/home/data/intune-cmdb-sync/`). So there is no
  Key Vault, no storage account, and no VNet. Do not add either resource back
  without accepting the networking that comes with it.
  - `deploy.sh` builds the zip locally (wheel + Linux x86_64 wheels via
    `pip --platform`, into `packages/`) with the job at
    `App_Data/jobs/triggered/intune-cmdb-sync/{run.py,settings.job}`, then
    uploads it through the private endpoint (below). No registry, no remote build
    (`SCM_DO_BUILD_DURING_DEPLOYMENT=false`).
  - `settings.job` holds the six-field NCRONTAB schedule (written from
    `SCHEDULE`) and `is_singleton: true`.
  - `run.py` finds `packages/` by absolute path: `WEBROOT_PATH`, then
    `/home/site/wwwroot`, then `$HOME/site/wwwroot`. Kudu copies a triggered job
    to a temp dir before running it, and Kudu, not the app container, runs it,
    so `HOME` alone was not reliable (`ModuleNotFoundError` on 2026-10-02).
    When nothing is found it prints where it looked and exits 3.
  - Basic B1 because scheduled WebJobs need Always On (~$13/month of the ~$20).
  - `WEBJOBS_IDLE_TIMEOUT=1800`: a triggered job is killed after 2 quiet
    minutes otherwise.
  - `deploy.sh` fails if Kudu's `/api/triggeredwebjobs` does not list the
    job.
- **Inbound to the app is private only; outbound is untouched.**
  `vpcx-lzn-app-service-deny-public-network` (2026-10-02) requires
  `publicNetworkAccess: 'Disabled'` on every App Service, Function and Logic
  App. The app therefore has one private endpoint (`<prefix>-app-pe`, group
  `sites`, covering site and scm) in `PRIVATE_ENDPOINT_SUBNET_ID` (DEV:
  `hybridsubnet-1` of `vpcx-vnet-eastus`, in `VPCXRG`). There is no VNet
  integration, because the job only calls out. The deploying Mac reaches
  10.52.46.x through Zscaler Private Access, but its DNS (`100.64.0.1`)
  resolves privatelink names publicly. So `kudu.sh` looks up the endpoint's IP
  through ARM and pins the scm hostname with `curl --resolve`, keeping TLS
  verification on the real name, with an App Service token
  (`--resource https://appservice.azure.com`). No `--cacert` on these calls: the ZPA path
  is not TLS-inspected, and the system curl's trust store verifies Kudu's real
  certificate. The reachability check uses `/api/deployments`: on Linux,
  `/api/environment` returns the Kudu dashboard with HTTP 500. That stopped the
  first real deploy (2026-10-02) on a healthy app. `deploy.sh` uploads through
  `POST /api/publish?...&async=true` that way, then polls
  `/api/deployments/latest` (status 4 = success) against the previous
  deployment's id. The synchronous publish got a gateway 502 mid-extract on
  2026-10-02. `webjob.sh` runs and reads the job.
  `az webapp deploy`, `az webapp webjob` and the portal's Kudu pages do not
  work from outside the network; ARM operations (app settings, restart, stop)
  still do. Cost is ~$20/month (B1 + one endpoint).
- **Secrets are app settings, deliberately.** With no Key Vault, the ServiceNow
  secret (and the Graph secret in `client_secret` mode) are plain app settings,
  readable by anyone with config read on the app. Prefer
  `GRAPH_AUTH_MODE=managed_identity` where the tenant allows it.
- **On App Service, an OpenTelemetry handler is the only route to Application
  Insights.** WebJob stdout reaches only the job's history.
  `appservice_job.run()` calls `configure_azure_monitor` (as the identity, via
  `AZURE_CLIENT_ID`; App Insights has local auth off), adds a stdout handler,
  then `adopt_host_handlers()` so `configure_logging` keeps both and gives them
  the JSON formatter. The handler sends `handler.format(record)` as the trace
  body, which is what the alert queries `parse_json(Message)`. This is pinned by
  `tests/test_appservice_job.py` with the in-memory exporter.
  - The handler also copies `extra=` fields into attributes without the
    formatter, so `_RedactFilter` masks secret-looking fields on the record
    itself.
  - `run()` flushes and shuts down the logger provider before returning, or the
    process exit drops `run complete` and the absence alert fires.
  - `OTEL_SERVICE_NAME` = the app name becomes `AppRoleName`, which both alerts
    filter on.
  - Per-request instrumentation (httpx, requests, azure_sdk, ...) is disabled:
    logs only.
  - `run_id` is reset per run here and in `aws_lambda.handler`.

- **`deploy.sh` has no `DRY_RUN` default and accepts only `true` / `false`.**
  A default of `false` turned any redeploy that forgot `DRY_RUN=true` live.
  `az` also sends any bool parameter other than the literal string `true` as
  `false`, so `DRY_RUN=yes` deployed a live job. `RETIRE_MISSING` gets the same
  strict check. Do not reintroduce a default.
- **The app gets only the settings `main.bicep` sets.** A local `.env` does not
  carry over; the `appsettings` resource replaces the whole list each deploy.
  `SNOW_CLASS_MAP` passes through as-is. `MAPPING_OVERRIDES_FILE` is read
  **locally** by `deploy.sh`, which strips `_comment` and ships the
  contents as `MAPPING_OVERRIDES_JSON`, because the package contains no such
  file. `config.py` accepts either variable but refuses both at once. Any
  future host (a VM, App Service) needs the same pass-through, or the
  `last_discovered` churn comes back. The AWS stack has it: `deploy/aws` takes
  `mapping_overrides_file` / `class_map` and a required `dry_run` for both of
  its hosts (`host = "lambda" | "ecs"`), checked offline by `terraform test`
  against a mocked provider. The ECS image needs `--build-arg EXTRAS=aws`,
  because S3 state imports boto3.
- **AWS secrets are referenced by SSM parameter name, never passed to
  Terraform.** As variables they landed in plaintext in `terraform.tfvars` and
  the (backend-less, local) state. The parameter ARNs are built from the names
  rather than read with the `aws_ssm_parameter` data source, which would copy
  the decrypted value into state anyway. `removed` blocks forget the
  parameters older versions created without deleting them.
- **A degraded run needs its own alarm.** It still logs `run complete` and can
  have zero errors, so neither the absence alarm nor the error alarm sees it.
  Azure's alert (on `AppTraces`) matches `degraded > 0`; AWS matches the
  `run completed in a degraded state` / `sync failed` messages. Any new host needs the same.
- **Lambda: no async retries, one run at a time.** A timeout counts as a
  failure, so Lambda's default two async retries would run a 15-minute sync
  three times. Reserved concurrency 1 stops overlapping runs racing on
  `state.json`. ECS has no equivalent, which the README documents.

- **Graph data calls are plain REST, deliberately** — `azure-identity` handles
  tokens, but `msgraph-sdk` is not used. Do not add it.

## Constraints

- **The target subscription's policy allows only listed resource types.**
  The first deploy (2026-09-23, `az acr create` into resource group
  `azc-obm-development`) was denied with `RequestDisallowedByPolicy` by
  `vpcx-lzn-cmmn-allowed-services`, in the "VPCx Landing Zone Common Baseline"
  set, assigned at the `Production` management group, effect Deny.
  - **Allowed:** Key Vault, storage accounts and file shares, Log Analytics
    workspaces, user-assigned identities, `insights` action groups and
    scheduled query rules, `Compute/virtualMachines`, and since 2026-10-02
    **Azure Functions and App Service** (`Microsoft.Web`).
  - **Still denied:** `ContainerRegistry/registries`, `App/managedEnvironments`,
    `App/jobs`, `Microsoft.Logic`, `Microsoft.Automation`,
    `Insights/dataCollectionRules`, `Network/natGateways`.
  - **Probably allowed, still load-bearing: `Microsoft.Insights/components`**
    (Application Insights). The 2026-10-02 preflight, which reports every
    refusal at once, refused only the vault and storage account. Without it the
    job's logs reach nothing and both alerts are blind; if it is ever refused,
    request it, and do not drop telemetry to get a deploy through.
  - **App Service must have public network access disabled**
    (`vpcx-lzn-app-service-deny-public-network`, found 2026-10-02; that
    preflight refused nothing else). The template also sets HTTPS-only, TLS 1.2,
    FTP off, remote debugging off and basic-auth publishing off.
  - **Not yet confirmed: `Microsoft.Network/privateEndpoints` from our
    resource group into `VPCXRG`'s subnet** (allowlist, and the deploying
    login's `subnets/join/action`).

- **The deploying machine is behind Zscaler TLS inspection.** `az` and `pip`
  trust only certifi's public roots, so they fail with
  `CERTIFICATE_VERIFY_FAILED`, first on the Bicep version check (`aka.ms`).
  `deploy.sh` always exports `AZURE_BICEP_CHECK_VERSION=false`; with
  `CA_BUNDLE=macos-keychain` (or a PEM path) it builds certifi + System-keychain
  roots into one bundle and exports `REQUESTS_CA_BUNDLE`, `PIP_CERT` and
  `SSL_CERT_FILE` for that run only. The bundle must include the public roots,
  because those variables replace a tool's defaults. A TLS preflight with the
  same trust stops the run before the first `az` call. The connector itself
  needs no code change behind the proxy: `httpx` 0.28 reads `SSL_CERT_FILE` and
  msal/`requests` read `REQUESTS_CA_BUNDLE`. Never disable verification
  (`AZURE_CLI_DISABLE_CONNECTION_VERIFICATION`, `--trusted-host`).

- **The landing zone also denies public access to Key Vault and Storage.** The
  first real Functions deploy (2026-10-02) was refused in preflight by
  `vpcx-lzn-kv-restrict-network-access`, `vpcx-lzn-strg-restrict-network-access`
  (both test `networkAcls.defaultAction != Deny`) and
  `vpcx-lzn-kv-enable-soft-delete-purge-retention-days-90` (purge protection,
  90 days). Nothing else was refused. Meeting them needs VNet integration and
  private endpoints, which is why Azure moved to App Service with neither
  resource (see Architecture notes). If a Key Vault or storage account is ever
  added back, all three policies apply to it.

- **`SNOW_CLASS_MAP` replaces the built-in default, it does not extend it.**
  `_env_kv_map` returns the parsed value or the default, never a merge, so a map
  set as `windows=cmdb_ci_computer` silently drops the built-in
  `macos=cmdb_ci_computer` — which is why every 2026-09-04 run skipped both
  macOS devices with the same line it prints for a deliberately unmapped iOS.
  `--check` now warns on common OSes with no entry and **fails** on a mapped
  class the instance does not have; `--list-classes [PATTERN]` reads
  `sys_db_object` so the right class is discoverable rather than guessed.

- **`SNOW_DISCOVERY_SOURCE` must be a registered choice value before any write
  succeeds.** `cmdb_ci.discovery_source` is a choice list, and an unregistered
  value is rejected per device with
  `INVALID_INPUT_DATA - In payload invalid data source [X] exist`, matched
  exactly including case. It failed all 17 devices of the second 2026-09-04 run.
  The CMDB Instance API returns that inside an IRE result envelope, so the
  message is past the point where a raw body snippet truncates — `_ire_item_error`
  in `writers/errors.py` parses it out. `--check` queries `valueLIKE<configured>`
  rather than listing the choice list — a stock instance has 200+ sources and
  `sys_choice` holds one row per language, so a listing is duplicated noise that
  cannot prove absence past its row limit either. A determined absence **fails**
  the check (exit 3), including a case-only near-miss; only an unreadable
  `sys_choice` is a caveat. On dpsnowdev nothing resembling "Intune" was
  registered on 2026-09-04. By 2026-09-25 `Intune` was registered and `--check`
  passes with it. `--register-discovery-source` creates the row
  via `POST /api/now/table/sys_choice` — which `--check-api` says this
  credential may call — and prints the record for an admin when ACLs refuse.
  Keep it on its own flag: a connector that edited choice lists as a side
  effect of syncing devices would be far worse to operate.

- **The CMDB Instance API takes strings only.** `POST
  /api/now/cmdb/instance/{class}` deserialises `attributes` as String->String
  and throws `HTTP 500 - class java.lang.Double cannot be cast to class
  java.lang.String` on a JSON number or boolean, before any validation worth
  reading. It failed all 17 devices of the 2026-09-04 run; the culprit was
  `disk_space` (`bytes_to_gb` returns a rounded float), with `ram` (int) and
  `virtual` (bool) behind it. `stringify_attributes` in `writers/cmdb_instance.py`
  coerces the whole payload at that writer. **Do not apply it to IRE** —
  `/api/now/identifyreconcile` accepts typed values, and that path is unchanged.

- **Never assume managed identity for Graph.** Deployments where Intune and the
  hosting subscription live in different tenants cannot use it — a managed
  identity is single-tenant and there is no cross-tenant consent path.
  `deploy.sh` hard-fails on that combination. `client_secret` is the default.
- **`federated_managed_identity` is the secretless cross-tenant mode**: a
  managed identity signs a client assertion for a multi-tenant app consented
  into the Intune tenant. `GRAPH_CLIENT_ID` is the *app*;
  `GRAPH_ASSERTION_IDENTITY_CLIENT_ID` is the *identity*. Do not conflate them —
  the resulting AADSTS error names neither.
- **`workload_identity` is not that mode.** It needs a projected federated token
  file, which AKS and GitHub Actions provide and App Service does not, which
  is why `main.bicep` deliberately does not offer it.
- **The federated credential can only be created after the first deploy.**
  `main.bicep` creates the user-assigned managed identity at deploy time, so
  there is nothing for the app registration to trust until that has run once.
  Order is: deploy with `client_secret` → create the FIC against the identity
  the deploy produced → redeploy with `graphAuthMode=federated_managed_identity`.
  No code changes at any step. The FIC's `subject` is the identity's
  **principal (object) ID**, not its client ID.
- **A multi-tenant app is only queryable from its home tenant.** Its service
  principal is projected into the other tenant, and that SP's app-role
  assignments *are* the cross-tenant admin consent — there is no separate
  consent call. `az ad app show` against the non-home tenant fails with
  "resource does not exist", which is correct behaviour, not a problem.
- **`GRAPH_AUTH_MODE=access_token` is local-development only.** It serves a
  pasted bearer token with no refresh, and is deliberately excluded from the
  Azure deployment — a scheduled job using it would work until the token expired
  and then fail every night. `StaticTokenProvider` checks audience, expiry, and
  Intune permission up front, because all three otherwise surface as an opaque
  401/403. Note `az account get-access-token` produces a token that passes the
  first two checks and fails the third: it is the Azure CLI's own app, which has
  no Intune permissions.
- **CLI flags must not outlive the call.** `_apply_overrides` is a context
  manager that restores the environment afterwards, because `aws_lambda.handler`
  calls `main()` repeatedly in a warm container and a permanent mutation left
  one invocation's `dry_run` applying to every later one. Flags stay one-way:
  absence never clears a value the environment set.
- **`--limit` / `INTUNE_DEVICE_LIMIT` disables retirement.** A truncated device
  list makes the rest of the fleet look like it vanished, and a small limit makes
  the missing fraction large enough that the percentage guard is not a reliable
  backstop. Never remove that short-circuit.
- **`403 Access to unscoped api is not allowed` is not a role problem.** It is
  the *OAuth client* being refused the API at the gate, before any ACL, role, or
  payload check: a Zurich Application Registry entry with **Scope Restriction =
  Securely Scoped** may only call REST APIs that have a REST API Auth Scope
  linked to it, bound per API *and per HTTP method*. Adding `itil` changes
  nothing. The tell is in the run report — `users_resolved > 0` with real
  sys_ids means the same credential already reads the Table API fine, and the
  403 carries `X-Is-Logged-In: true`. The fix is a REST API Auth Scope for
  `POST` on both `/api/now/identifyreconcile` and
  `/api/now/identifyreconcile/query`, or Scope Restriction = Broadly Scoped.
  **Resolved on dpsnowdev by 2026-09-25**: `--check-api` (run_id `5fe75e06fcfc`)
  shows all four `identifyreconcile` variants ALLOWED with 200. The notes stay
  because PROD's OAuth client will need the same scope.

- **Which APIs are gated is per-instance — probe, do not assume.** This file
  previously stated that `/api/now/cmdb/instance/…` was "behind the same gate,
  confirmed refused identically". A live `--check-api` on 2026-09-04 refuted
  that: every `identifyreconcile` variant returned 403 at the gate while `POST
  /api/now/cmdb/instance/{class}` returned 400, i.e. reached the API. Auth
  scopes bind per API *and per HTTP method*, so the only reliable statement is
  the one the probe makes. `SNOW_WRITE_MODE=cmdb_instance` is therefore a real
  fallback on that instance, with the trade-offs in `writers/cmdb_instance.py`: no
  `sys_object_source_info`, so identification falls back to serial number then
  name, and `correlation_id` becomes the only link back to the Intune device.

## State of the work

The Microsoft Graph half is verified against a live tenant. On the ServiceNow
half, reads are verified, and so are writes **through `SNOW_WRITE_MODE=cmdb_instance`
only**: on 2026-09-04 that mode wrote 19 CIs to dpsnowdev, and a re-run matched by
serial rather than duplicating. **The IRE path has never written anything.** Its
endpoints cleared the unscoped-api gate on 2026-09-25 (see Constraints), and a
dry run went through `/identifyreconcile/query` the same day (run_id
`4f027adeed81`, `--limit 5`): every operation it returned was recognised, and all
three mapped devices came back `updated` against the `ci_sys_id`s the 2026-09-04
`cmdb_instance` run created, so IRE matched them rather than planning duplicates.
A real `--limit 5` IRE write followed on 2026-09-25, and the same dry run repeated
straight afterwards reported every device `unchanged`, so no mapped field churns
between runs. Retirement through IRE runs is still untested. The tests mock at the
HTTP boundary with `respx` from vendor documentation, not observed responses.
Green tests are weaker evidence here than they look.

## Next steps

0. **First App Service deploy (written 2026-10-02, never deployed).** The
   stack in `deploy/azure/` is an App Service app plus a scheduled WebJob,
   chosen after Functions turned out to need private networking (see
   Architecture notes). Nothing about it has run in Azure yet. Green tests, a
   clean `az bicep build`, a stub-`az` run of `deploy.sh` and a local `run.py`
   exit-2 check are all that back it. The first deploy must confirm:
   - no further policy refuses the template, including the private endpoint
   - Kudu is reachable at the endpoint's private IP over Zscaler Private Access,
     and accepts the App Service token
   - the WebJob registers and fires on schedule with Always On
   - `AppTraces` carries the JSON line under `AppRoleName` = the app name
   - `state.json` persists on `/home`

   Follow `deploy/azure/README.md` stages 3-5 with `DRY_RUN=true`.
1. **Done on dpsnowdev (2026-09-25): the OAuth client is authorized for the IRE
   API.** `--check-api` shows every endpoint allowed. PROD's OAuth client will
   need the same REST API Auth Scope, so re-run the probe there before its first
   run. **`intune-cmdb-sync --check-api` is the diagnostic for this**: it probes
   every endpoint × method the connector can use (`servicenow/probe.py`),
   including the `/api/now/v1/...` aliases, and prints which are allowed plus
   the scope change to request. It writes nothing and must stay that way — the
   identifyreconcile probes submit an empty `items` array, the CMDB Instance
   probes post to a class that does not exist. A 400/404 from those probes is a
   **pass**: reaching the API's own validation proves the request cleared the
   gate. Exit 3 means the endpoint the configured write mode uses is refused.
1a. **Fallback only, now that IRE is open: `SNOW_WRITE_MODE=cmdb_instance`.**
   The 2026-09-04 probe showed that endpoint allowed on this instance, along with
   `PATCH /api/now/table/…` (retirement) and `POST /api/now/table/…`. `--check`
   verifies this mode now (`_verify_cmdb_instance_access`) and `--dry-run`
   predicts insert/update by reading serial number then name rather than
   reporting `pending`. Both are weaker than IRE and say so: the prediction is
   not a simulation, and without `sys_object_source_info` a serial-number
   correction duplicates a CI. Prefer IRE if the auth scope lands; do not treat
   this as equivalent.
2. **`intune-cmdb-sync --check`.** Proves both connections *and* simulates a
   write through `/api/now/identifyreconcile/query`, which commits nothing — so
   a missing `itil` role or an unregistered discovery source fails here rather
   than on the first real run. Exit 4 means the write path could not be
   simulated (older release, or `cmdb_instance` mode), which is not the same as
   a pass. Note `itil` does not always carry `sys_properties` read, so a 403 on
   the connectivity probe can be a red herring — as is the message this check
   prints on 403, which blames `itil` for what is usually the OAuth scope gate.
3. **`--dry-run --limit 5 --report-devices --report ./run.json`.** Confirm the `operation`
   values IRE actually returns against `_OPERATION_TO_ACTION` in `writers/ire.py`.
   The dry run uses `/identifyreconcile/query`, a *different* endpoint whose
   response vocabulary is unconfirmed; an unrecognised operation is a hard
   error by design, so this is where a surprise will surface.
   **Done 2026-09-25 (run_id `4f027adeed81`)**: 3 `updated`, 0 errors, no
   unrecognised operation. `updated` is expected on the first IRE pass over CIs
   the `cmdb_instance` mode wrote; whether it persists is step 4's churn check.
4. **First real write with `--limit` and `SNOW_RETIRE_MISSING=false`.** Re-run
   the same dry run straight afterwards: it must report `unchanged`. If it still
   says `updated`, some field moves between runs (see the `last_discovered` note
   in Architecture). **Done 2026-09-25**: the post-write dry run reported all
   `unchanged`. Still outstanding: verify that `install_status=7` actually means retired *in that instance*
   before enabling retirement — the README calls this a convention, not a
   guarantee.
5. **Drop the limit** once the written CIs look right, and only then consider
   enabling retirement.
6. **Optional, later: move off the client secret.** The multi-tenant app for
   `federated_managed_identity` already exists and is consented into the Intune
   tenant; only the federated credential is outstanding, and it needs a deployed
   managed identity first (see Constraints). This saves a secret rotation and
   nothing else — it is not on the critical path.
