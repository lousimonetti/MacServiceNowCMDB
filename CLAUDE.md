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
- **Exit code 4 means degraded**, not merely "device errors". A tripped
  mass-retirement guard or a failed state write returns 4 even without
  `--fail-on-error`, because both leave the *next* run unable to reason about
  the fleet. See `RunReport.degraded`.
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
- **`--limit` / `INTUNE_DEVICE_LIMIT` disables retirement.** A truncated device
  list makes the rest of the fleet look like it vanished, and a small limit makes
  the missing fraction large enough that the percentage guard is not a reliable
  backstop. Never remove that short-circuit.

## State of the work

The Microsoft Graph half is verified against a live tenant. **The ServiceNow
half has never touched a live instance** — the 278 tests mock at the HTTP
boundary with `respx`, and those fixtures were written from vendor
documentation, not observed responses. Green tests are weaker evidence here
than they look.

## Next steps

1. **Get a ServiceNow instance.** A free Personal Developer Instance is enough;
   IRE is base platform. Then follow `docs/servicenow-setup.md` §2 (grant
   `itil`) and §5 (create the discovery source choice value — skipping it
   produces `Invalid data source`).
2. **`intune-cmdb-sync --check`.** Connectivity only. Note that `itil` does not
   always carry `sys_properties` read, so a 403 here can be a red herring.
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
