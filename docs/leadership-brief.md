# Leadership brief

**Subject:** Intune → ServiceNow CMDB integration without an IntegrationHub subscription
**Status:** Built and tested; not yet run against a live ServiceNow instance
**Prepared:** August 2026

---

## Summary

Endpoint data that lives in Microsoft Intune does not reach the ServiceNow CMDB
without an integration. ServiceNow sells one — the Service Graph Connector for
Microsoft Intune — but it is built on IntegrationHub ETL, which is licensed
separately from ITSM. Organisations that own ServiceNow and Intune but not
IntegrationHub have the data and the destination, and no supported path between
them.

This project provides that path using capability already included in the
ServiceNow platform. It is complete, tested, and running against live Microsoft
data. It has not yet been pointed at a ServiceNow instance.

**If your organisation already owns IntegrationHub, use the vendor connector.**
It is supported, maintained, and has a management UI. This project exists for
the case where that subscription is not available.

---

## What problem this solves

A CMDB that does not know about endpoints cannot answer the questions it is
bought to answer: which laptops are assigned to which staff, which corporate
devices are unaccounted for, which machines are running an OS version with a
known vulnerability. Those questions are routinely answered today by exporting
spreadsheets from Intune, which is manual, immediately stale, and not auditable.

The result is a CMDB with a visible gap where a significant share of the asset
estate should be, and a class of ITSM and security questions that cannot be
answered from the system of record.

---

## Approach

The part of ServiceNow that protects CMDB data quality — the Identification and
Reconciliation Engine, which deduplicates records, reconciles competing data
sources, and enforces which source wins for which field — is **base platform**,
not a paid add-on. It is reachable through a documented API by any account with
standard ITSM permissions.

This project reads devices from Microsoft, transforms them outside ServiceNow,
and submits them to that engine in the same shape the vendor connector uses. The
data-quality protections are identical because they are the same engine.

**What is retained:** deduplication, reconciliation, source precedence, and
device identity keyed on an immutable Microsoft identifier — so a hardware
repair, a corrected serial number, or a device rename does not create a
duplicate record.

**What is given up:** vendor-maintained field mappings, the configuration UI,
and ServiceNow support for the integration itself. The mappings are documented
and version-controlled; from adoption onward, the organisation owns them.

---

## Cost

Infrastructure cost is negligible in either cloud, for a daily sync of a full
fleet:

| | Azure | AWS |
|---|---|---|
| **Total** | **under $0.10 / month** | **approximately $0.20 / month** |

These are list prices for a single daily run of roughly five minutes. Both are
rounding errors on any IT budget; the platform choice should be made on
credential and security model, not cost.

The meaningful cost is not infrastructure. It is the engineering ownership
described under *Risks* below.

---

## Current status

| Component | Status |
|---|---|
| Microsoft Graph integration | **Verified against a live tenant** |
| Data transformation and mapping | Built; comprehensively unit tested |
| ServiceNow write path | Built; **never run against a live instance** |
| Deployment automation (Azure, AWS) | Built; not yet deployed |
| ServiceNow-side configuration | Authored as code; not yet installed |

The gap is deliberate and worth stating plainly: the automated test suite is
substantial, but its expectations of ServiceNow's responses were written from
vendor documentation rather than from observed behaviour. **A passing test suite
is weaker evidence of readiness than it appears.** The next milestone is a
supervised run against a non-production instance.

---

## Risks and how they are handled

**Incorrect data reaching the production CMDB.** The highest-consequence risk.
Mitigated in three ways: a dry-run mode that reports what would be written
without writing it; refusal to write any device that cannot be safely identified,
rather than guessing; and a hard rule that empty values are never sent, so this
integration cannot blank out data another source contributed.

**Mass incorrect retirement.** If a data fetch failed partway, a naive
integration would conclude the missing devices had left the fleet and retire
them. This one refuses to retire more than a configurable share of known devices
in a single run — 10% by default — and reports the refusal as a failure to the
scheduler rather than a log entry. Retirement is also disabled by default.

**Credential exposure.** Secrets are held in Key Vault or Parameter Store, never
in the container image or source. Anything resembling a credential is redacted
from logs before it is written. The repository is scanned for committed secrets
on every push.

**Unsupported integration.** This is a genuine, permanent trade, not a mitigated
risk. The organisation owns the mappings and the code. That is the cost of the
approach and it should be accepted explicitly, not discovered later.

**Key-person dependency.** The system is documented at architecture, modelling,
and engineering level, with rationale recorded for non-obvious decisions. That
reduces the dependency; it does not remove it. Any production adoption should
name a second owner.

---

## Decisions requested

1. **Proceed to a supervised pilot?** The next step is a run against a
   non-production ServiceNow instance with writes enabled and retirement off.
   This requires an instance and an integration account with standard ITSM
   permissions. No production impact.

2. **Confirm ownership.** If this goes to production, who owns the mappings when
   ServiceNow changes a class definition or Microsoft changes a Graph property?
   This is the real recurring cost and it should have a named owner before
   adoption, not after.

3. **Confirm scope.** The current scope is corporate-owned computers. Mobile
   devices, installed software inventory, and CI relationships are deliberately
   excluded — each is a meaningful addition and none should be assumed.

---

## What this does not do

Stated plainly so expectations are not set by omission:

- **Computers only.** Phones and tablets are excluded by default, because the
  correct CMDB class for handheld hardware varies between ServiceNow instances
  and the integration will not guess.
- **No software inventory.** The vendor connector also imports installed
  applications. This does not.
- **No relationships.** No topology or dependency mapping between records.
- **Personally-owned devices are excluded by default**, and the exclusion is
  enforced twice independently so that a filter failing silently at Microsoft's
  end cannot let personal devices into the CMDB.

---

## Further reading

| Document | Audience |
|---|---|
| [architecture.md](architecture.md) | Architects, reviewers |
| [metamodel-mapping.md](metamodel-mapping.md) | CMDB owners, data governance |
| [engineering-guide.md](engineering-guide.md) | Engineers |
| [servicenow-setup.md](servicenow-setup.md) | ServiceNow administrators |
| [entra-setup.md](entra-setup.md) | Identity administrators |
