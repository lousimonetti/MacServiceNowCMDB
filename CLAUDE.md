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
  instance whose write path is still blocked by the unscoped-api gate. It reads
  with `sysparm_display_value=all` because `manufacturer` / `model_id` /
  `assigned_to` are references whose raw value is a sys_id; `query_table` keeps
  asking for raw values because the resolvers depend on that, so the reader
  issues its own request rather than adding a mode to a shared method. Keep it
  write-free — that property is what makes it safe to point at production.

- **Graph data calls are plain REST, deliberately** — `azure-identity` handles
  tokens, but `msgraph-sdk` is not used. Do not add it.

## Constraints

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
  file, which AKS and GitHub Actions provide and Container Apps does not, which
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
  403 carries `X-Is-Logged-In: true`. **`SNOW_WRITE_MODE=cmdb_instance` is not a
  workaround**: `/api/now/cmdb/instance/…` is a global-scope `/api/now/` API
  behind the same gate, confirmed refused identically. The fix is a REST API
  Auth Scope for `POST` on both `/api/now/identifyreconcile` and
  `/api/now/identifyreconcile/query`, or Scope Restriction = Broadly Scoped.

## State of the work

The Microsoft Graph half is verified against a live tenant. On the ServiceNow
half, **reads are now verified against a live instance and writes are not**: as
of 2026-08-28 a `--dry-run --limit 5` reached the instance, resolved real
`sys_user` sys_ids, and was then refused on every write by the unscoped-api gate
described under Constraints. Nothing has ever been written to a live CMDB, and
the 278 tests mock at the HTTP boundary with `respx` from vendor documentation,
not observed responses. Green tests are weaker evidence here than they look.

## Next steps

1. **Blocked: get the OAuth client authorized for the IRE API.** The instance
   exists and reads work; every write is refused by the unscoped-api gate (see
   Constraints). This is with the ServiceNow admin. Until it clears, nothing
   below can run — `SNOW_AUTH_MODE=basic` sidesteps it for local testing only,
   since the restriction is on the OAuth entity rather than the user.
2. **`intune-cmdb-sync --check`.** Proves both connections *and* simulates a
   write through `/api/now/identifyreconcile/query`, which commits nothing — so
   a missing `itil` role or an unregistered discovery source fails here rather
   than on the first real run. Exit 4 means the write path could not be
   simulated (older release, or `cmdb_instance` mode), which is not the same as
   a pass. Note `itil` does not always carry `sys_properties` read, so a 403 on
   the connectivity probe can be a red herring — as is the message this check
   prints on 403, which blames `itil` for what is usually the OAuth scope gate.
3. **`--dry-run --limit 5 --report-devices --report ./run.json`.** Confirm the `operation`
   values IRE actually returns against `_OPERATION_TO_ACTION` in `writers.py`.
   The dry run uses `/identifyreconcile/query`, a *different* endpoint whose
   response vocabulary is unconfirmed; an unrecognised operation is a hard
   error by design, so this is where a surprise will surface.
4. **First real write with `--limit` and `SNOW_RETIRE_MISSING=false`.** Then
   verify that `install_status=7` actually means retired *in that instance*
   before enabling retirement — the README calls this a convention, not a
   guarantee.
5. **Drop the limit** once the written CIs look right, and only then consider
   enabling retirement.
6. **Optional, later: move off the client secret.** The multi-tenant app for
   `federated_managed_identity` already exists and is consented into the Intune
   tenant; only the federated credential is outstanding, and it needs a deployed
   managed identity first (see Constraints). This saves a secret rotation and
   nothing else — it is not on the critical path.
