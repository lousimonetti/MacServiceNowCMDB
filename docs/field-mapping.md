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
| `ram` | `physicalMemoryInBytes`; for Macs, the `INTUNE_MAC_RAM_ATTRIBUTE` custom attribute | bytes → MB (Graph's value requires `INTUNE_FETCH_HARDWARE_DETAIL=true`; see [Mac RAM](#mac-ram-from-a-custom-attribute)) |
| `cpu_name` | the `INTUNE_MAC_CPU_ATTRIBUTE` custom attribute (Macs only) | first line, up to 100 characters; see [Mac CPU name](#mac-cpu-name-from-a-custom-attribute) |
| `cpu_type` | `processorArchitecture` (beta) | `x64`, `x86`, `ARM64`, `ARM`; `unknown` is left out (requires `INTUNE_FETCH_HARDWARE_DETAIL=true`) |
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

### When a field is sent but does not change on the CI

Sending a field is not the same as IRE writing it. If another discovery source
(SCCM, Jamf, ServiceNow Discovery) also writes these CIs, IRE reconciliation
rules for `cmdb_ci_computer` (or a parent class) can give that source authority
over attributes such as `ram`, `disk_space` and `mac_address`. IRE then drops
those attributes from our payload and still applies the rest, with no error.
Check the class's reconciliation rules in CI Class Manager before you assume a
mapping bug. The fix there is a ServiceNow configuration decision, not a code
change.

### CPU and Mac memory

`cpu_type` carries the processor architecture, which Graph exposes only on the
beta endpoint. For that reason the hardware-detail lookup is batched against
beta. Nothing is mapped to `cpu_count`, `cpu_speed` or `cpu_core_count`, and
`cpu_name` comes only from a Mac custom attribute. Intune's Graph data has no
processor name, speed or core count, in v1.0 or in beta's
`hardwareInformation`. Graph reports
`physicalMemoryInBytes` as 0 for Macs on both endpoints (seen in two tenants,
2026-10-14), so Macs get no `ram`. Leave these fields to another discovery
source, or collect them with an Intune custom attribute script.

`cpu_type` is a weak value. If another source writes a full processor name into
it, add `"drop": ["cpu_type"]` to the overrides file, or let a reconciliation
rule give that source priority.

### Mac RAM from a custom attribute

Intune can run a script on each Mac every 8 hours and keep its output. The
connector reads that output when `INTUNE_MAC_RAM_ATTRIBUTE` names the attribute.
It fills `ram` only where Graph reported none, so Windows values are untouched.

1. In Intune: **Devices > macOS > Custom attributes > Add**. Name it (for
   example `RAM MB`), set the data type to **Integer**, and upload
   [`deploy/intune/mac-ram-mb.sh`](../deploy/intune/mac-ram-mb.sh). It
   prints `sysctl -n hw.memsize` in MB, and exits non-zero rather than print
   anything that is not a whole number. Assign it to the Macs.

   The output must be MB, not bytes. Intune marks an Integer attribute's run
   **Failed** when the value does not fit 32 bits, and 16 GB in bytes
   (17179869184) does not; the device report still shows the number, which
   makes this easy to miss. The connector skips failed runs.
2. Grant the Graph app `DeviceManagementScripts.Read.All` (Application), with
   admin consent. In `managed_identity` mode, `deploy.sh` grants it when the
   setting is set.
3. Set `INTUNE_MAC_RAM_ATTRIBUTE=RAM MB`. The value can be the attribute
   name, the profile's display name, or its id.

The connector reads only runs that succeeded, and ignores output that is not a
whole number of MB. A Mac has no value until its first run reports back. If
the attribute cannot be read (wrong name, missing permission), the run continues
and the run report carries a warning. The summary field
`ram_from_custom_attribute` counts the devices it filled. The read uses Graph's
beta endpoint, the only one that exposes custom attributes.

### Mac CPU name from a custom attribute

The same mechanism fills `cpu_name`, which Graph has for no platform.

1. In Intune: **Devices > macOS > Custom attributes > Add**, data type
   **String**, with [`deploy/intune/mac-cpu-name.sh`](../deploy/intune/mac-cpu-name.sh)
   (`sysctl -n machdep.cpu.brand_string`, which prints `Apple M5` on Apple
   silicon and the full brand string on Intel). Assign it to the Macs.
2. The same `DeviceManagementScripts.Read.All` permission as for RAM.
3. Set `INTUNE_MAC_CPU_ATTRIBUTE` to the attribute's name.

Only the first line of a successful run is used, trimmed to the field's 100
characters. The summary field `cpu_name_from_custom_attribute` counts the
devices it filled; an unreadable attribute is a run report warning. If another
discovery source also writes `cpu_name`, whichever IRE lets write last wins;
add `"drop": ["cpu_name"]` or a reconciliation rule to settle it.

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

The same check has a cost. IRE applies a payload only when its timestamp is
*later* than the one stored for this source, and otherwise reports the whole
payload as `NO_CHANGE` ("newer entry in sys_object_source from same feed"). So a
newly mapped field, a newly created model or a new owner match reaches a CI only
after that device checks in to Intune again. For a one-off backfill, run once with
`--source-recency run_time` (or `SNOW_SOURCE_RECENCY=run_time`), which sends the
run's start time instead. Afterwards, go back to the default (`last_sync`). Each
device then waits for its next check-in after the backfill before IRE accepts
its updates again, because the stored timestamp is now the backfill time.

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

A model is only ever created under a manufacturer that resolved to a real
`core_company` record, and it is linked to that record. If the device's
manufacturer matches nothing, the model is not created either: `model_id` is
left unset, and the run report carries a warning naming the model. Create the
manufacturer, or add a `manufacturer_aliases` entry, and the next run creates
the model. An existing model still resolves whatever the manufacturer; the gate
applies to creation only.

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
  "drop":     ["disk_space"],
  "manufacturer_aliases": { "LENOVO": "Lenovo", "Apple": "Apple Inc." },
  "model_aliases":        { "20W1S2BG00": "ThinkPad T14 Gen 2" }
}
```

- `fields` — CMDB field → Graph property. A `user.` prefix reads from the
  resolved Entra user. Values that look like ISO-8601 timestamps are converted to
  ServiceNow datetime format automatically.
- `os_names` — override the `operatingSystem` translation table.
- `static` — merged after everything else, so it wins.
- `drop` — remove a default field you do not want written.
- `manufacturer_aliases` / `model_aliases` — Intune name → the `name` of the
  existing `core_company` / `cmdb_model` record, matched case-insensitively.
  Use them when Intune spells a manufacturer differently (`LENOVO`), or reports
  a machine-type code instead of a model name. A name that still has no record
  appears under `unresolved_references` in the run report, as the alias.

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
