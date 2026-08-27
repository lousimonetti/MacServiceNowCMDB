# Microsoft Entra ID and Intune setup

The connector reads Microsoft Graph with **application** permissions — there is
no signed-in user, so delegated permissions do not apply.

Pick one of two paths:

- **[Managed identity](#option-a-managed-identity-azure-hosting)** — no secret
  exists at all. Use this **only** when your Azure subscription is in the *same
  tenant as Intune*. See [Cross-tenant](#cross-tenant-intune-and-azure-in-different-tenants).
- **[App registration + secret](#option-b-app-registration--client-secret)** —
  portable. Use this for AWS, on-prem, local development, and for any deployment
  where Intune and the Azure subscription are in different tenants.

Check before choosing:

```bash
az account show --query tenantId -o tsv    # subscription tenant
# compare against the tenant where Intune lives
```

---

## Permissions required

| Permission | Type | Needed for |
| --- | --- | --- |
| `DeviceManagementManagedDevices.Read.All` | Application | Reading `/deviceManagement/managedDevices`. Always required. |
| `User.Read.All` | Application | Reading `/users/{id}` to resolve device owners to Entra users. Required only when `GRAPH_ENRICH_USERS=true`, which is the default. |

Both are read-only. The connector never writes to Graph, and never calls a
device action such as wipe or retire — those methods exist on the
`managedDevice` resource but are not used here.

### Dropping `User.Read.All`

If your tenant will not approve `User.Read.All`, set `GRAPH_ENRICH_USERS=false`.
The connector then matches owners using only the `userPrincipalName` and
`emailAddress` already present on the Intune device record.

The cost is real: `employeeId` comes from the Entra user object, so
`employee_number` — the most stable match key most organisations have — stops
working. Adjust the order accordingly:

```bash
GRAPH_ENRICH_USERS=false
SNOW_USER_MATCH_ORDER=email,user_name
```

### Intune licensing

Graph's Intune APIs require the tenant to hold an active Intune licence. That is
a prerequisite of using Intune at all, not something this connector adds.

---

## Option A: managed identity (Azure hosting)

The best option when it is available, because there is no credential to store,
rotate, or leak. `deploy/azure/deploy.sh` does all of this for you; the manual
equivalent is below.

> **Same-tenant only.** A managed identity is single-tenant: it exists in the
> directory that owns its subscription, and there is no mechanism to consent it
> into another tenant's Graph. If Intune lives elsewhere, this whole section does
> not apply — use Option B and read
> [Cross-tenant](#cross-tenant-intune-and-azure-in-different-tenants).

1. Create a user-assigned managed identity and note its **object (principal) ID**.
   User-assigned rather than system-assigned, so the permission grant survives
   the job being deleted and recreated.

2. Grant the Graph application permissions. App-role assignments live in Entra,
   not ARM, so they go through Graph rather than through your IaC:

   ```bash
   PRINCIPAL_ID="<managed identity object id>"
   GRAPH_APP_ID="00000003-0000-0000-c000-000000000000"   # same in every tenant

   GRAPH_SP_ID=$(az ad sp show --id "$GRAPH_APP_ID" --query id -o tsv)

   for ROLE in DeviceManagementManagedDevices.Read.All User.Read.All; do
     ROLE_ID=$(az ad sp show --id "$GRAPH_APP_ID" \
       --query "appRoles[?value=='$ROLE'].id | [0]" -o tsv)
     az rest --method POST \
       --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$PRINCIPAL_ID/appRoleAssignments" \
       --headers "Content-Type=application/json" \
       --body "{\"principalId\":\"$PRINCIPAL_ID\",\"resourceId\":\"$GRAPH_SP_ID\",\"appRoleId\":\"$ROLE_ID\"}"
   done
   ```

   Making this assignment requires Privileged Role Administrator, Cloud
   Application Administrator, or Global Administrator. The assignment *is* the
   admin consent — there is no separate consent step.

3. Configure the connector:

   ```bash
   GRAPH_AUTH_MODE=managed_identity
   GRAPH_TENANT_ID=<tenant id>
   GRAPH_CLIENT_ID=<managed identity CLIENT id>   # not the object id
   # no GRAPH_CLIENT_SECRET
   ```

   The two IDs are different and the error you get from mixing them up is not
   obvious. `GRAPH_CLIENT_ID` selects *which* identity the token comes from, so
   it takes the client ID; the app-role grant in step 2 takes the object ID.
   Omit `GRAPH_CLIENT_ID` entirely to use a system-assigned identity.

Assignments take a minute or two to propagate. A `403` immediately after
granting usually means "wait", not "wrong".

---

## Option B: app registration + client secret

1. **Entra admin centre > App registrations > New registration**

   | Field | Value |
   | --- | --- |
   | Name | `intune-cmdb-sync` |
   | Supported account types | Single tenant |
   | Redirect URI | leave empty |

   Note the **Application (client) ID** and **Directory (tenant) ID**.

2. **API permissions > Add a permission > Microsoft Graph > Application permissions**

   Add `DeviceManagementManagedDevices.Read.All` and `User.Read.All`, then
   **Grant admin consent**. Application permissions are inert until consented;
   without that click every Graph call returns `403`.

3. **Certificates & secrets > New client secret**

   Copy the **Value** immediately — it is never shown again. Set an expiry your
   rotation process can actually meet; 24 months with no calendar reminder is
   how integrations die at 2am.

4. Configure the connector:

   ```bash
   GRAPH_AUTH_MODE=client_secret
   GRAPH_TENANT_ID=<directory (tenant) id>
   GRAPH_CLIENT_ID=<application (client) id>
   GRAPH_CLIENT_SECRET=<the secret Value>
   ```

   The secret can also come from a file (`GRAPH_CLIENT_SECRET_FILE`) or AWS SSM
   Parameter Store (`GRAPH_CLIENT_SECRET_PARAMETER`) so it never has to sit in
   an environment variable.

## Cross-tenant: Intune and Azure in different tenants

A common shape: Intune and the Entra licences are in one tenant, while the Azure
subscription paying for the compute (an MSDN/Visual Studio benefit, say) is in
another.

The connector handles this fine — it is only ever an OAuth client, and it does
not care which directory issued its credential. What breaks is the *managed
identity* shortcut.

**Do this:** create the app registration using
[Option B](#option-b-app-registration--client-secret) in the **Intune tenant**,
and set `GRAPH_TENANT_ID` to that tenant. The compute can then live anywhere —
the other Azure tenant, AWS, or a laptop. `deploy/azure/main.bicep` puts the
secret in Key Vault and defaults to exactly this mode.

**Do not** try to grant Graph app roles to a managed identity from the
subscription's tenant. There is no consent path, and `deploy.sh` will refuse.

### The secretless alternative

If rotating that secret is unacceptable, the supported pattern is:

1. A **multi-tenant** app registration and a user-assigned managed identity,
   **both in the subscription's tenant** — the identity and the app must share a
   tenant, that is the rule.
2. The managed identity added to the app as a
   [federated identity credential][fic] with audience `api://AzureADTokenExchange`.
3. The app admin-consented into the **Intune** tenant via
   `https://login.microsoftonline.com/<intune-tenant>/adminconsent?client_id=<app-id>`.
4. At runtime, the identity gets a token for `api://AzureADTokenExchange` and
   presents it as a `client_assertion` to the Intune tenant's token endpoint.

This is GA, genuinely secretless, and **implemented** as
`GRAPH_AUTH_MODE=federated_managed_identity`:

```bash
GRAPH_AUTH_MODE=federated_managed_identity
GRAPH_TENANT_ID=<the INTUNE tenant, where the app is consented>
GRAPH_CLIENT_ID=<the multi-tenant APP registration's client ID>
GRAPH_ASSERTION_IDENTITY_CLIENT_ID=<the managed IDENTITY's client ID>
# no GRAPH_CLIENT_SECRET
```

The last two are the easy mistake: `GRAPH_CLIENT_ID` is the *app*, and
`GRAPH_ASSERTION_IDENTITY_CLIENT_ID` is the *identity that signs for it*. Swap
them and the failure is an opaque `AADSTS700213`, which names neither.

`deploy/azure/main.bicep` supports this mode and wires the identity's client ID
automatically. It cannot create the federated credential or the cross-tenant
consent for you — those live in two different directories and cannot be done
from one login.

Still weigh it honestly against rotating one Key Vault secret on a calendar. It
removes a rotation task and adds a four-step setup spanning two tenants.

[fic]: https://learn.microsoft.com/entra/workload-id/workload-identity-federation-config-app-trust-managed-identity

## Local development without an app registration

`GRAPH_AUTH_MODE=access_token` takes a Graph bearer token directly, for trying
the connector against a tenant where you do not have — or cannot yet get — an
app registration.

```bash
GRAPH_AUTH_MODE=access_token
GRAPH_ACCESS_TOKEN=<paste>          # or GRAPH_ACCESS_TOKEN_FILE=/path
# no GRAPH_CLIENT_ID, no GRAPH_CLIENT_SECRET, no GRAPH_TENANT_ID needed:
# the token already encodes all three
```

Get the token from [Graph Explorer][ge]: sign in, consent
`DeviceManagementManagedDevices.Read.All`, and copy the access token from the
**Access token** tab.

**`az account get-access-token` does not work for this.** It mints a token for
the Azure CLI's own app registration, which has no Intune permissions, so
`/deviceManagement/managedDevices` returns 403. The connector reads the token's
claims at startup and tells you this before making the request.

Three ways this mode fails, all reported up front rather than as an opaque 401:

| Problem | What you see |
| --- | --- |
| Token for the wrong resource | Names the audience it actually has |
| Token already expired | Says how long ago |
| No Intune permission | Names the permission needed, and why an `az` token lacks it |

The token cannot be refreshed, so a run outliving it fails partway through.
That is the accepted trade — this mode exists to answer "does this work against
my data at all", not to run anything on a schedule. It is deliberately rejected
by `deploy/azure/main.bicep`, and `tests/test_deployment_consistency.py` keeps
it that way.

[ge]: https://developer.microsoft.com/graph/graph-explorer

### Certificate credentials

If policy forbids client secrets, upload a certificate and use
`GRAPH_AUTH_MODE=default` with `AZURE_CLIENT_CERTIFICATE_PATH`, which
`DefaultAzureCredential` picks up. Certificates do not expire silently the way
secrets do.

---

## Sovereign clouds

| Cloud | `GRAPH_BASE_URL` | `AZURE_AUTHORITY_HOST` |
| --- | --- | --- |
| Commercial | `https://graph.microsoft.com/v1.0` | `https://login.microsoftonline.com` |
| US Gov (L4/L5) | `https://graph.microsoft.us/v1.0` | `https://login.microsoftonline.us` |
| China (21Vianet) | `https://microsoftgraph.chinacloudapi.cn/v1.0` | `https://login.chinacloudapi.cn` |

`GET /deviceManagement/managedDevices` is available in all of them.

---

## Verify

```bash
intune-cmdb-sync --check
```

This acquires a Graph token, reads one page of devices, authenticates to
ServiceNow, and exits without writing anything.

To check the raw permission grant instead:

```bash
TOKEN=$(az account get-access-token --resource https://graph.microsoft.com --query accessToken -o tsv)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices?\$top=1&\$select=id,deviceName,managedDeviceOwnerType"
```

| Response | Cause |
| --- | --- |
| `401` | Token was not acquired, or is for the wrong resource. |
| `403 Forbidden` | Permission not granted, admin consent not given, or the grant has not propagated yet. |
| `400` on the `$filter` | Expected on some tenants. The connector detects this and refetches unfiltered, then filters client-side. |
