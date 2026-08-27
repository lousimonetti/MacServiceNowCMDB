# Field mapping

What lands on a `cmdb_ci_computer` record, where it comes from, and why.

All of it is implemented in [`src/intune_cmdb_sync/mapping.py`](../src/intune_cmdb_sync/mapping.py),
which is pure — no network, no I/O — so the tests in `tests/test_mapping.py`
pin every rule below.

---

## Default mapping

| CMDB field | Intune `managedDevice` property | Transform |
| --- | --- | --- |
| `name` | `deviceName`, else `managedDeviceName` | whitespace collapsed |
| `serial_number` | `serialNumber` | normalised; placeholders rejected — see below |
| `os` | `operatingSystem` | mapped to ServiceNow naming (`macOS` → `Mac OS X`) |
| `os_version` | `osVersion` | verbatim |
| `manufacturer` | `manufacturer` | resolved to a `core_company` sys_id |
| `model_id` | `model` | resolved to a `cmdb_model` sys_id |
| `mac_address` | `ethernetMacAddress`, else `wiFiMacAddress` | normalised to `AA:BB:CC:DD:EE:FF` |
| `ram` | `physicalMemoryInBytes` | bytes → MB (requires `INTUNE_FETCH_HARDWARE_DETAIL=true`) |
| `disk_space` | `totalStorageSpaceInBytes` | bytes → GB, 2 dp |
| `first_discovered` | `enrolledDateTime` | ISO-8601 → `YYYY-MM-DD HH:MM:SS` UTC |
| `last_discovered` | `lastSyncDateTime` | ISO-8601 → `YYYY-MM-DD HH:MM:SS` UTC |
| `assigned_to` | `userId` → Entra user → `sys_user` | see [Owner resolution](#owner-resolution) |
| `correlation_id` | `id` | the Intune device GUID |
| `virtual` | — | always `false` |
| `install_status` | — | only when `SNOW_INSTALL_STATUS_ACTIVE` is set |

Fields that resolve to `None` or an empty string are omitted from the payload
entirely rather than sent as blanks. That matters: an empty value in an IRE
payload is a value, and it will overwrite good data that another discovery
source contributed.

## Identity

Two independent mechanisms, and the distinction matters more than any single
field mapping.

**`sys_object_source_info`** — sent on every item in `identify_reconcile` mode:

```json
{
  "source_native_key": "<Intune managedDevice.id>",
  "source_name": "Intune",
  "source_feed": "Intune Managed Devices",
  "source_recency_timestamp": "2026-08-25 06:11:02"
}
```

IRE checks `source_native_key` *before* it evaluates any identification rule. So
once a device has been synced, it is matched by its Intune GUID — not by serial
number. A motherboard swap, a corrected serial, a rename: none of them create a
duplicate CI. `source_recency_timestamp` carries the device's last Intune
check-in, which is what lets IRE decide that a stale payload should not overwrite
fresher data from another source.

**Identification rules** — used the first time a device is seen, and the only
mechanism available in `cmdb_instance` mode. For `cmdb_ci_computer` that is
normally serial number, then name.

This is the main functional difference between the two write modes.
`cmdb_instance` has no documented slot for `sys_object_source_info`, so it
re-identifies on serial number every run and will duplicate a CI whose serial
changes.

## Serial number normalisation

Firmware vendors ship placeholder serials, and they are the single most common
cause of mass CI collisions: every affected machine identifies as the same CI and
the fleet collapses into one record.

Rejected (case-insensitive), leaving `serial_number` unset:

`0` · `00000000` · `0123456789` · `123456789` · `1234567890` ·
`base board serial number` · `chassis serial number` · `default string` ·
`empty` · `filled by o.e.m.` · `invalid` · `n/a` · `na` · `none` ·
`not applicable` · `not available` · `not present` · `not specified` ·
`o.e.m.` · `system serial number` · `to be filled by o.e.m.` · `unknown`

Also rejected: anything under 3 characters, and anything that is a single
repeated character once punctuation is stripped (`0000-0000`).

Add your own with `SERIAL_BLOCKLIST_EXTRA`.

A device with neither a usable serial nor a name is **skipped** and counted in
`devices_skipped_no_identifier`. Writing it would create a fresh duplicate CI on
every single run.

## Class routing

`SNOW_CLASS_MAP` maps the Intune `operatingSystem` string to a CMDB class:

```bash
SNOW_CLASS_MAP=windows=cmdb_ci_computer;macos=cmdb_ci_computer
```

The default covers Windows and macOS only. An OS in neither the map nor
`SNOW_DEFAULT_CLASS` is skipped and reported — deliberately, because guessing a
class name that does not exist in your instance fails at write time with a much
less obvious error.

To include mobile hardware, confirm the target class exists in **CI Class
Manager** first, then add it. ServiceNow's own Intune connector routes phones and
tablets to a handheld class rather than to `cmdb_ci_computer`; which class that
is varies by instance and release, so this connector will not assume one.

## Reference fields

`manufacturer` → `core_company` and `model_id` → `cmdb_model` are reference
fields. They need a sys_id, and **IRE will not resolve a display name into a
reference for you** — an unresolved reference is silently dropped.

So the connector looks both up once per run and caches them. A ten-thousand
machine fleet typically has fewer than a dozen manufacturers and a few hundred
models, so this is two or three Table API calls, not twenty thousand.

Lookups use OR-chained equality (`name=a^ORname=b`) rather than the `IN`
operator, because `IN` takes a comma-separated list and real inventory data is
full of commas — `MacBook Pro (16-inch, 2023)`, `Mac16,1`, `Acme, Inc.`. An `IN`
list containing those does not error; it silently matches nothing.

Names that match no record are left unset and listed under
`unresolved_references` in the run report. Set
`SNOW_CREATE_MISSING_MANUFACTURERS` / `SNOW_CREATE_MISSING_MODELS` to `true` to
have the connector create them instead — off by default, because auto-populating
the model catalogue affects Asset Management, and that is the asset team's call.

## Owner resolution

Intune reports the owner as an Entra object ID plus a UPN, email, and display
name. The CMDB needs a `sys_user` sys_id. There is no universal join key, so
`SNOW_USER_MATCH_ORDER` names the candidate keys to try, in order:

| Key | Match | Stability |
| --- | --- | --- |
| `entra_id` | a custom `sys_user` field == Entra `id` | Highest. Never changes. Requires you to populate the field. |
| `employee_number` | `sys_user.employee_number` == Entra `employeeId` | High, when HR data feeds both systems. |
| `email` | `sys_user.email` == Entra `mail`, falling back to the UPN | Medium. Breaks on mailbox migrations. |
| `user_name` | `sys_user.user_name` == UPN, then the UPN local part | Medium. Breaks on domain renames. |

Default: `employee_number,email,user_name`.

Two rules that exist to prevent bad data:

- **Ambiguity is not a match.** A key matching more than one `sys_user` is
  treated as no match at all. Assigning a laptop to the wrong person is worse
  than leaving `assigned_to` empty, and far harder to notice.
- **Values are queried as Entra reports them**, not lowercased. Most ServiceNow
  instances collate case-insensitively; Oracle-backed ones do not, and a
  lowercased value would silently stop matching there.

Unmatched owners are counted as `users_unresolved` in the run report. The CI is
still written — just without `assigned_to`.

Set `SNOW_ASSIGN_USER=false` to skip owner resolution entirely, which also skips
the `/users` Graph calls and the `sys_user` reads.

## Extending the mapping

Two mechanisms.

**Static values on every CI** — `SNOW_EXTRA_ATTRIBUTES`, as JSON. Reference
fields need a sys_id:

```bash
SNOW_EXTRA_ATTRIBUTES='{"company":"a1b2c3...","u_managed_by_team":"EUC"}'
```

**Per-field mappings** — `MAPPING_OVERRIDES_FILE`, pointing at JSON:

```json
{
  "fields": {
    "u_intune_compliance_state": "complianceState",
    "u_intune_last_sync":        "lastSyncDateTime",
    "u_intune_enrolled":         "enrolledDateTime",
    "u_intune_encrypted":        "isEncrypted",
    "u_intune_category":         "deviceCategoryDisplayName",
    "u_entra_device_id":         "azureADDeviceId",
    "u_owner_department":        "user.department",
    "u_owner_office":            "user.office_location"
  },
  "os_names": { "macos": "macOS" },
  "static":   { "u_data_source": "intune-cmdb-sync" },
  "drop":     ["disk_space"]
}
```

- `fields` — CMDB field → Graph property. A `user.` prefix reads from the
  resolved Entra user. Values that look like ISO-8601 timestamps are converted to
  ServiceNow datetime format automatically.
- `os_names` — override the `operatingSystem` translation table.
- `static` — merged after everything else, so it wins.
- `drop` — remove a default field you do not want written.

There is a working example in [`examples/mapping-overrides.json`](examples/mapping-overrides.json).

## Intune properties not mapped by default

Available on `managedDevice` and deliberately left unmapped, because no
out-of-box `cmdb_ci_computer` field is a good home for them. Map them to custom
columns via `MAPPING_OVERRIDES_FILE` if you want them:

`complianceState` · `managementAgent` · `managementState` ·
`deviceEnrollmentType` · `deviceRegistrationState` · `isEncrypted` ·
`isSupervised` · `jailBroken` · `azureADDeviceId` · `azureADRegistered` ·
`deviceCategoryDisplayName` · `enrollmentProfileName` ·
`freeStorageSpaceInBytes` · `imei` · `meid` · `udid` · `phoneNumber` ·
`androidSecurityPatchLevel` · `partnerReportedThreatState` ·
`managementCertificateExpirationDate`

`complianceState` is the one most teams want first — it is what makes
"non-compliant corporate laptops" a CMDB report rather than an Intune export.
