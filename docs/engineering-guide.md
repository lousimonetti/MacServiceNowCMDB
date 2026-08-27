# Engineering guide

For engineers changing, reviewing, or debugging this codebase.

Read [architecture.md](architecture.md) first for the shape of the system. This
document covers how the code is organised, how to work on it safely, and the
things that will bite you.

---

## 1. Getting set up

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/pytest -q                        # no network, no credentials
.venv/bin/ruff check src/ tests/
.venv/bin/mypy
```

All three gates must pass. CI runs them across Python 3.11, 3.12, and 3.13,
plus a container build, a Bicep/Terraform validation pass, and a `gitleaks`
secret scan.

To run against real systems, copy `.env.example` to `.env` and fill it in.
`.env` is gitignored; keep it that way.

---

## 2. Module map

```
src/intune_cmdb_sync/
├── __main__.py          CLI, exit codes, report writing
├── config.py            Environment → typed config. All validation lives here.
├── models.py            EntraUser, SysUserRef, DeviceOutcome, RunReport
├── errors.py            Exception hierarchy, all rooted at SyncError
├── http.py              Shared retrying client: throttling, backoff, redaction
├── graph.py             Microsoft Graph: devices, users, credentials
├── mapping.py           managedDevice → CMDB values. Pure, no I/O.
├── user_resolver.py     Entra user → sys_user
├── reference_resolver.py  Display name → core_company / cmdb_model sys_id
├── snow_query.py        Encoded-query construction
├── state.py             Run-to-run device → sys_id map
├── storage.py           State backends: local filesystem, S3
├── sync.py              Pipeline orchestration
├── secrets.py           File- and SSM-sourced secret resolution
├── logging_setup.py     JSON/text logging with credential redaction
├── aws_lambda.py        Lambda entry point
└── servicenow/
    ├── auth.py          OAuth client credentials / basic
    ├── client.py        Table API + arbitrary endpoints
    └── writers.py       The two write paths
```

**`mapping.py` is pure by design.** No network, no I/O, no clock. That is what
makes the mapping rules exhaustively testable and what lets a CMDB owner review
exactly what will land on their CI records by reading one file. Keep it that
way — if a mapping rule needs data from ServiceNow, resolve it in a resolver and
pass it in, as `references` already does.

---

## 3. Testing strategy

Tests mock at the **HTTP boundary** with `respx`, not at the client-class
boundary. Request shapes — the exact IRE payload, the `$batch` chunking, the
encoded queries — are asserted rather than assumed.

```python
@respx.mock
def test_something(self, set_env, runner_factory):
    respx.get(DEVICES).mock(return_value=httpx.Response(200, json={"value": [...]}))
    mock_snow_plumbing()
    respx.post(IRE).mock(return_value=ire_response("INSERT"))
    report = runner_factory(Config.from_env()).run()
```

Shared fixtures are in `tests/conftest.py`. `make_device()` returns a
representative corporate macOS device; `mock_snow_plumbing()` stubs the
supporting Table API reads every run performs.

`conftest.py` also clears **every** environment variable the connector reads
before each test, so a stray value in your shell cannot change a result.

### The limitation you must keep in mind

Those fixtures were written from **vendor documentation, not observed
responses**. The Microsoft Graph half has since been verified against a live
tenant; the ServiceNow half has not. A green suite proves the client code does
what the fixtures expect — it does not prove the fixtures match reality.

Treat any ServiceNow response-shape assumption as unverified until it has run
against a real instance.

### Verifying a test actually catches the bug

When fixing a defect, confirm the new test fails without the fix. Re-introduce
the defect, run the test, watch it fail, then revert. A test that passes against
both the broken and fixed code is worse than no test, because it advertises
coverage that does not exist.

---

## 4. Adding a mapped field

1. Ask **how often the value changes.** Anything that moves on every device
   check-in causes fleet-wide daily churn — see
   [metamodel-mapping.md §7](metamodel-mapping.md#7-attribute-churn). If it
   moves, it belongs in an opt-in custom column, not the defaults.
2. Add it to `DEVICE_SELECT_FIELDS` in `graph.py` if it is not already
   requested. Non-default Graph properties additionally require
   `INTUNE_FETCH_HARDWARE_DETAIL`, which costs one call per device.
3. Add it to `build_values` in `mapping.py`. **Preserve the omit-when-empty
   property** — the final dict comprehension strips `None` and `""`, so do not
   bypass it. Sending an empty value overwrites good data from another source.
4. Add a test in `tests/test_mapping.py` covering present, absent, and malformed
   input.
5. Document it in [field-mapping.md](field-mapping.md).

---

## 5. Things that will bite you

**Reference fields need sys_ids.** IRE will not resolve `"Apple"` into a
`core_company` reference. An unresolved reference is silently dropped or written
as an invalid sys_id — no error either way.

**`IN` queries silently match nothing.** ServiceNow's `IN` operator takes a
comma-separated list, and inventory data is full of commas (`Mac16,1`, `Acme,
Inc.`). Use `build_or_query` from `snow_query.py`, which OR-chains equality
terms. This does not error when it breaks; it just returns zero rows.

**Graph emits 7-digit fractional seconds.** Python's `fromisoformat` accepts at
most 6. `to_snow_datetime` truncates. It also rejects `0001-01-01T00:00:00Z`,
which is what Graph returns for a `DateTimeOffset` that was never set.

**The dry run uses a different endpoint.** `--dry-run` posts to
`/identifyreconcile/query`, not `/identifyreconcile`. Its response vocabulary is
not the same thing as the write endpoint's, and it is unverified against a live
instance. An unrecognised `operation` is a hard error by design in both modes —
that is where a surprise is meant to surface.

**`--limit` disables retirement.** A truncated device list makes the rest of the
fleet look like it vanished, and a small limit makes the missing fraction large
enough that the percentage guard is not a reliable backstop. The limit path
short-circuits retirement outright rather than relying on it.

**`workload_identity` is not the cross-tenant answer.** It needs a projected
federated token file, which AKS and GitHub Actions provide and Container Apps
does not. For secretless cross-tenant use `federated_managed_identity`, which
signs a client assertion with a managed identity.

**Exit code 4 means degraded, not just device errors.** A tripped retirement
guard or a failed state write returns 4 even without `--fail-on-error`, because
both leave the next run unable to reason about the fleet.

**`nextLink` carries its own query parameters.** When paging, do not re-send
`params` — the absolute URL already has them.

---

## 6. Debugging a run

```bash
intune-cmdb-sync --check                   # both connections + a simulated write
intune-cmdb-sync --dry-run --log-level DEBUG --log-format text
intune-cmdb-sync --dry-run --report-devices --report ./run.json
```

`--report-devices` writes a per-device outcome list: action, CI sys_id, which
key the owner matched on, and any error message. That file is the first thing to
look at when the summary counters look wrong. Both deployments enable it via
`RUN_REPORT_DEVICES=true`, writing to the state volume (Azure) or the state
bucket (AWS).

**Every log line carries a `run_id`**, and the report carries the same value.
That is how you isolate one run in a log store holding weeks of them, and how
you tie a report back to the run that produced it:

```kusto
ContainerAppConsoleLogs_CL
| extend p = parse_json(Log_s)
| where p.run_id == "<id from the report>"
| order by TimeGenerated asc
```

**IRE errors carry ServiceNow's own trace id**, appended as
`[IRE logContextId=...]`. That is the handle to quote in a ServiceNow support
case or to look up in the instance's own logs — without it, an investigation on
their side starts from a timestamp.

Logging is JSON by default so Log Analytics and CloudWatch Insights can query
fields directly. Anything whose key looks like a credential is redacted before
it reaches a handler — verified, not assumed.

### Reading the run report

| Counter | Means |
|---|---|
| `devices_skipped_no_class` | OS matched neither `SNOW_CLASS_MAP` nor a default |
| `devices_skipped_no_identifier` | No usable serial **and** no name — writing it would duplicate |
| `users_unresolved` | Owner found in Entra but no unambiguous `sys_user`; CI still written |
| `unresolved_references` | Manufacturer/model names with no matching record |
| `degraded` | The run did not do its whole job — read these first |

---

## 7. Verification backlog

The ServiceNow half has never run against a live instance. In priority order:

1. **IRE response shape.** The writer keys off `operation` per result item and
   hard-fails on an unrecognised value and on an item-count mismatch. Both paths
   have only ever fired against fixtures. Confirm the actual `operation` values
   against `_OPERATION_TO_ACTION` in `writers.py`.
2. **`install_status=7` means retired** in the target instance. The README calls
   this a convention, not a guarantee. Verify before enabling retirement.
3. **`sys_properties` read access.** `--check` reads one row as a connectivity
   probe, and `itil` does not always carry that grant. A 403 there can be a red
   herring rather than a real auth problem — the write probe that follows is the
   one that matters.
4. **Graph paging at scale.** `@odata.nextLink` handling has never seen a real
   multi-page tenant.
5. **Vendor prefix.** `x_icsy_intune_cmdb` in `servicenow-app/` is a
   placeholder; real prefixes are assigned per instance.

---

## 8. Open work

- **Retirement PATCHes are unbatched.** One call per device. The Table API has
  no bulk PATCH, and the mass-retirement guard bounds the volume, so this is
  acceptable rather than good.

---

## 9. Conventions

- Comments explain **why**, not what. The codebase is dense with rationale for
  non-obvious decisions; match that density rather than narrating the code.
- Every behaviour change gets a test, particularly anything altering what is
  written to a CI.
- New configuration goes through `config.py` validation with a bounded range and
  a clear error message. Every problem is reported at once, not one per run.
- Errors that should abort the run derive from `SyncError`. Anything else is a
  bug that will produce an unhandled traceback instead of an exit code.
