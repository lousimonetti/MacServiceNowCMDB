# ServiceNow setup

Everything here uses base-platform capability. There is no plugin to buy, no
Service Graph Connector entitlement, and no IntegrationHub subscription.

Roughly 20 minutes of admin work. Steps 1–4 are required; 5 and 6 are worth doing.

---

## Why this connector needs no licence

The paid [Service Graph Connector for Microsoft Intune][sgc] is a ServiceNow
Store application built on IntegrationHub ETL, and IntegrationHub is separately
subscribed. What it fundamentally does, though, is pull `managedDevice` records
from Microsoft Graph, transform them, and hand the result to the CMDB
Identification and Reconciliation Engine.

The IRE is not the licensed part. It is exposed to any authenticated caller
holding `itil` or `asset` through the base-platform
[Identification and Reconciliation API][ire-api] at
`POST /api/now/identifyreconcile`. This connector does the pull and the transform
outside the instance, then posts the same shape of payload to that endpoint. You
get IRE deduplication, reconciliation, and source-precedence handling — the parts
that actually protect CMDB data quality — without the connector licence.

What you give up is the vendor's maintained field mappings, the ETL UI, and
ServiceNow support for the integration itself. Those mappings are reproduced in
[field-mapping.md](field-mapping.md), and you own them from then on.

[sgc]: https://www.servicenow.com/docs/r/servicenow-platform/service-graph-connectors/cmdb-integration-intune.html
[ire-api]: https://www.servicenow.com/docs/r/api-reference/rest-apis/identification-and-reconciliation-api.html

---

## 1. Create the integration user

**User Administration > Users > New**

| Field | Value |
| --- | --- |
| User ID | `svc.intune.cmdb` |
| First name | `Intune` |
| Last name | `CMDB Integration` |
| Web service access only | ✅ checked |
| Internal Integration User | ✅ checked |
| Active | ✅ checked |

"Web service access only" blocks interactive login. "Internal Integration User"
keeps the account off your subscribed-user count — worth ticking, because an
integration account that consumes a fulfiller licence is a licence cost you did
not intend.

With the client-credentials grant you never set a password for this user.

## 2. Grant roles

**Related Links > Edit** on the user's Roles list:

| Role | Why |
| --- | --- |
| `itil` | Required by the Identification and Reconciliation API. Also carries read access to `sys_user`, `core_company`, and `cmdb_model`, which the connector needs to resolve `assigned_to`, `manufacturer`, and `model_id`. |

`asset` also satisfies the IRE API and is a reasonable alternative if your CMDB
governance prefers it, but confirm it grants the `sys_user` read the owner
lookup depends on.

Do **not** grant `admin`. Nothing here needs it.

> If you installed the bundled ServiceNow application (see
> [`servicenow-app/`](../servicenow-app)), also grant
> `x_icsy_intune_cmdb.intune_sync`. It carries no privileges of its own; it makes
> the integration identifiable in audit trails and gives instance-specific ACLs
> a single named grant to attach to.

## 3. Enable the client-credentials grant

Available from the Washington DC release onward. On older instances, skip to
[Older instances](#older-instances-oauth_password-or-basic) below.

**System Properties > All Properties** (`sys_properties.list`) — create or update:

| Field | Value |
| --- | --- |
| Name | `glide.oauth.inbound.client.credential.grant_type.enabled` |
| Type | `true \| false` |
| Value | `true` |

Confirm these plugins are active (all are base platform):

- OAuth 2.0 — `com.snc.platform.security.oauth`
- REST API Provider — `com.glide.rest`
- Authentication Scope — `com.glide.auth.scope`
- REST API Auth Scope Plugin — `com.glide.rest.auth.scope`

## 4. Register the OAuth client

**System OAuth > Application Registry > New >
"Create an OAuth API endpoint for external clients"**

| Field | Value |
| --- | --- |
| Name | `Intune CMDB Sync` |
| Client ID | auto-generated — this is `SNOW_CLIENT_ID` |
| Client Secret | auto-generated — this is `SNOW_CLIENT_SECRET` |
| Redirect URL | leave empty |
| Accessible from | `All application scopes` |

Then the step that is easy to miss: **Configure > Form Layout**, add the
**OAuth Application User** field, save, and set it to `svc.intune.cmdb`.

The client-credentials grant has no user in the exchange, so this field is how
ServiceNow decides whose roles the token carries. Leave it empty and every
request authenticates as nobody, and every write is denied.

Verify:

```bash
curl -s -X POST "https://<instance>.service-now.com/oauth_token.do" \
  -d "grant_type=client_credentials" \
  -d "client_id=<SNOW_CLIENT_ID>" \
  -d "client_secret=<SNOW_CLIENT_SECRET>"
```

A JSON body containing `access_token` means steps 1–4 are done.

### Older instances: `oauth_password` or `basic`

If the property in step 3 does not exist on your release:

- **`SNOW_AUTH_MODE=oauth_password`** — keep the Application Registry entry from
  step 4, give `svc.intune.cmdb` a password, and set `SNOW_USERNAME` /
  `SNOW_PASSWORD` as well as the client ID and secret.
- **`SNOW_AUTH_MODE=basic`** — no Application Registry entry at all, just
  `SNOW_USERNAME` and `SNOW_PASSWORD`. Simplest, and the least desirable: the
  password travels on every request.

## 5. Register `Intune` as a discovery source

**Required.** The IRE API validates `sysparm_data_source` against the choice list
on `cmdb_ci.discovery_source`. An unregistered value is rejected, so until this
exists every write fails.

Either install the bundled application:

```bash
cd servicenow-app
npm install
npx now-sdk auth --add https://<instance>.service-now.com --type basic
npm run build && npm run deploy
```

Or do it by hand — navigate to `sys_choice_list.do`, then **New**:

| Field | Value |
| --- | --- |
| Table | `cmdb_ci` |
| Element | `discovery_source` |
| Value | `Intune` |
| Label | `Intune` |
| Sequence | `900` |

The value must match `SNOW_DISCOVERY_SOURCE` exactly, including case.

## 6. Optional: `u_entra_object_id` on `sys_user`

The strongest possible owner match. Email and username both break on mailbox
migrations, domain renames, and duplicate accounts; an Entra object ID never
changes for the life of the account.

Add a String (40) column `u_entra_object_id` to `sys_user`, index it, populate it
from whatever already feeds Entra users into ServiceNow, then set:

```bash
SNOW_USER_MATCH_ORDER=entra_id,employee_number,email
SNOW_USER_ENTRA_ID_FIELD=u_entra_object_id
```

The bundled application ships this column in
`servicenow-app/src/fluent/sys-user-entra-id.now.ts`. Delete that file before
deploying if you would rather not have a scoped app touching the global
`sys_user` dictionary.

---

## Verify the write path

Before scheduling anything, prove the whole chain with a single CI. This
`identifyreconcile` call is exactly what the connector sends:

```bash
TOKEN=$(curl -s -X POST "https://<instance>.service-now.com/oauth_token.do" \
  -d "grant_type=client_credentials" \
  -d "client_id=<SNOW_CLIENT_ID>" \
  -d "client_secret=<SNOW_CLIENT_SECRET>" | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')

curl -s -X POST \
  "https://<instance>.service-now.com/api/now/identifyreconcile?sysparm_data_source=Intune" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "items": [{
          "className": "cmdb_ci_computer",
          "values": { "name": "IRE-SMOKE-TEST", "serial_number": "SMOKETEST0001" },
          "sys_object_source_info": {
            "source_native_key": "smoke-test-1",
            "source_name": "Intune",
            "source_feed": "Intune Managed Devices"
          }
        }]
      }' | python3 -m json.tool
```

Expected:

```json
{ "result": { "items": [ { "className": "cmdb_ci_computer",
                           "operation": "INSERT",
                           "sysId": "…" } ] } }
```

Run it a second time. `operation` becomes `UPDATE` or `NO_CHANGE` against the
same `sysId` — that is IRE recognising the device rather than duplicating it,
which is the behaviour the whole design rests on.

Then delete the test CI.

`intune-cmdb-sync --check` performs the same simulation automatically, through
`/api/now/identifyreconcile/query`, and turns each of the failures below into a
message that names the cause. Run it before scheduling anything.

### If it fails

When the failure is a 403, run `intune-cmdb-sync --check-api` before reading
the table below. `--check` stops at the first refusal, which cannot distinguish
the three layers that produce an identical "User Not Authorized" body.
`--check-api` walks every endpoint the connector can use, one HTTP method at a
time, and reports which are allowed:

```
  GET  /api/now/table/sys_properties        ALLOWED                200
  GET  /api/now/cmdb/instance/{className}   ALLOWED                200
  POST /api/now/identifyreconcile           REFUSED AT OAUTH GATE  403
  POST /api/now/cmdb/instance/{className}   REFUSED AT OAUTH GATE  403
```

That shape — reads allowed, every POST refused, `X-Is-Logged-In: true` on the
refusals — is the OAuth scope gate and nothing else. GET allowed alongside POST
refused *on the same API* is the proof that the restriction is per-method, which
is the specific thing to fix in the auth scope. It writes nothing: the
identifyreconcile probes submit an empty `items` array, and the CMDB Instance
probes post to a class name that does not exist, so neither has anything it
could create even against a fully authorised instance. It also probes the
versioned paths (`/api/now/v1/...`), because an auth scope is recorded against
a specific API version and one bound to only one of the two produces exactly
this symptom.

| Response | Cause |
| --- | --- |
| `401 User Not Authenticated` | The OAuth Application User field on the Application Registry entry is empty. |
| `403` / `insufficient rights` | The integration user lacks `itil` or `asset`. |
| `403 Access to unscoped api is not allowed` | Not a role. The OAuth client is refused the API at the gate: its Application Registry entry is **Securely Scoped** and has no **REST API Auth Scope** linked for `POST` on this API. Link one covering both `/api/now/identifyreconcile` and `/api/now/identifyreconcile/query`, or set **Scope Restriction = Broadly Scoped**. `SNOW_WRITE_MODE=cmdb_instance` is behind the same gate and is not a workaround; `SNOW_AUTH_MODE=basic` sidesteps it for local testing, because the restriction is on the OAuth entity rather than the user. Reads keep working throughout, so a run report with `users_resolved > 0` and every device in error is the signature. |
| `Invalid data source` | Step 5 was skipped, or `SNOW_DISCOVERY_SOURCE` does not match the choice value exactly. |
| `Required_Attribute_Empty` | The identification rule for the class requires an attribute the payload omits. Check **CI Class Manager > Identification/Reconciliation** for `cmdb_ci_computer`. |
| `404` on `/api/now/identifyreconcile` | The release predates the IRE API. Fall back to `SNOW_WRITE_MODE=cmdb_instance`. |

---

## What the connector touches

| Table | Access | Why |
| --- | --- | --- |
| `cmdb_ci_computer` (via IRE) | write | The CIs themselves. |
| `sys_object_source` | write (by IRE) | Records the Intune device ID against each CI. |
| `sys_user` | read | Resolving `assigned_to`. |
| `core_company` | read | Resolving `manufacturer`. |
| `cmdb_model` | read, optional write | Resolving `model_id`. Writes only when `SNOW_CREATE_MISSING_MODELS=true`. |
| `sys_properties` | read | One row (`instance_name`) as a connectivity check. |

Nothing else is read or written.
