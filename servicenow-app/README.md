# Intune CMDB Sync — ServiceNow application

Ships **no runtime logic**. The sync runs outside the instance and writes through
the base-platform Identification and Reconciliation API.

What this application provides is the platform-side configuration the connector
depends on, captured as code so it is versioned, reviewable, and reproducible
across dev/test/prod — rather than clicked in by hand three times and drifting.

| File | What it creates | |
| --- | --- | --- |
| `discovery-source.now.ts` | `Intune` choice on `cmdb_ci.discovery_source` | **Required.** IRE rejects an unregistered `sysparm_data_source`, so every write fails without it. |
| `roles.now.ts` | `x_icsy_intune_cmdb.intune_sync` role | Makes the integration identifiable in audit trails and gives ACLs one named grant to attach to. Carries no privileges itself. |
| `properties.now.ts` | Two system properties | Documents the expected source name and run interval inside the instance, so reports can alert on stale `last_discovered`. |
| `sys-user-entra-id.now.ts` | `u_entra_object_id` on `sys_user`, plus an index | **Optional.** Enables the most stable owner match available. Delete the file if you would rather a scoped app did not touch the global `sys_user` dictionary. |

The role granted here does **not** replace `itil` or `asset`. The IRE API
requires one of those, and a scoped role cannot confer them.

## Deploy

```bash
npm install
npx now-sdk auth --add https://<instance>.service-now.com --type basic
npm run build
npm run deploy
```

Then grant `x_icsy_intune_cmdb.intune_sync` to the integration user alongside
`itil`.

To produce an update set XML instead of installing directly:

```bash
npm run pack
```

## Changing the scope

`x_icsy_intune_cmdb` is a placeholder. Vendor prefixes are assigned per instance,
so replace it in `now.config.json` and in the role name in `roles.now.ts` before
deploying to a real instance.

## Not using the SDK

Everything here can be done manually in about five minutes — see
[docs/servicenow-setup.md](../docs/servicenow-setup.md), steps 5 and 6. The
connector does not care how the configuration got there.
