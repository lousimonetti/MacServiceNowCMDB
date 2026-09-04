"""Per-endpoint authorization probe for the ServiceNow write path.

`verify_write_access` in writers.py answers one question -- can IRE simulate a
write -- and answers it only for `/api/now/identifyreconcile/query`. When that
comes back 403 it does not say *which* of the several things in front of the
CMDB refused, and the three candidates need three different people to fix:

* the OAuth client is not authorised for the API at the REST gate (Application
  Registry: Scope Restriction = Securely Scoped with no REST API Auth Scope
  linked for that API **and that HTTP method**),
* the integration *user* lacks `itil`/`asset` or an ACL denies the table,
* the endpoint does not exist on this release.

This module walks each endpoint the connector could ever use, one HTTP method
at a time, and classifies the answer. The whole matrix runs in a couple of
seconds and produces something an operator can hand to a ServiceNow admin: not
"writes are unauthorized" but "POST /api/now/identifyreconcile is refused at
the gate while GET /api/now/cmdb/instance/cmdb_ci_computer is allowed, so the
restriction is per-method".

**Nothing here can create, update, or delete a CI.** Two techniques keep it
that way, and both must be preserved by anything added to `PROBES`:

* the identifyreconcile probes post an empty `items` array -- IRE has no
  payload to identify, so there is nothing it could insert;
* the CMDB Instance probes post to a class name that does not exist, so even a
  fully authorised request has no table to write into.

Both get past the REST gate before they fail, which is the entire point: a 400
or 404 from the API itself is a *pass*, because it proves the request was
allowed through to the API's own validation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..config import ServiceNowConfig
from ..errors import SyncError
from ..http import describe_error
from .client import CMDB_INSTANCE_API, TABLE_API, ServiceNowClient
from .writers import (
    IDENTIFY_RECONCILE_API,
    IDENTIFY_RECONCILE_ENHANCED_API,
    IDENTIFY_RECONCILE_QUERY_API,
    unscoped_api_refusal,
)

log = logging.getLogger(__name__)

# A class name that cannot exist, so a POST to the CMDB Instance API has
# nowhere to write even if every permission in front of it is open. The API
# rejects it *after* the REST gate has already made its decision.
PROBE_CLASS = "cmdb_ci_intune_cmdb_sync_probe_no_such_class"

# Empty payload for the identifyreconcile probes: zero items to identify means
# zero records IRE could create.
EMPTY_IRE_BODY: dict[str, Any] = {"items": [], "relations": []}

# Verdicts, ordered from "working" to "broken".
AUTHORIZED = "authorized"
NOT_FOUND = "not_found"
DENIED_BY_ROLE = "denied_by_role"
BLOCKED_AT_GATE = "blocked_at_gate"
UNAUTHENTICATED = "unauthenticated"
UNKNOWN = "unknown"

_VERDICT_LABEL = {
    AUTHORIZED: "ALLOWED",
    NOT_FOUND: "NOT ON THIS INSTANCE",
    DENIED_BY_ROLE: "DENIED (role/ACL)",
    BLOCKED_AT_GATE: "REFUSED AT OAUTH GATE",
    UNAUTHENTICATED: "NOT AUTHENTICATED",
    UNKNOWN: "UNCLEAR",
}

# ServiceNow sets this on a response it authenticated. `true` alongside a 403
# is the tell that the credential is fine and the *API* was refused.
LOGGED_IN_HEADER = "X-Is-Logged-In"


@dataclass(frozen=True)
class EndpointProbe:
    """One (method, path) pair to test, and what a pass would prove."""

    name: str
    method: str
    path: str
    proves: str
    params: Mapping[str, Any] | None = None
    json_body: Any = None
    # The API as an Application Registry auth scope names it, when that differs
    # from the URL actually called. The CMDB Instance probes put a throwaway
    # class in the path; telling an admin to scope *that* would be wrong.
    api: str | None = None
    # False for probes that only add context (versioned aliases), so a failure
    # on them does not read as a blocker.
    required: bool = True

    @property
    def label(self) -> str:
        return f"{self.method} {self.api or self.path}"


@dataclass
class ProbeResult:
    probe: EndpointProbe
    verdict: str
    status_code: int | None
    detail: str
    logged_in: str | None = None

    @property
    def ok(self) -> bool:
        return self.verdict == AUTHORIZED

    @property
    def label(self) -> str:
        return _VERDICT_LABEL.get(self.verdict, self.verdict)


@dataclass
class ProbeReport:
    results: list[ProbeResult] = field(default_factory=list)
    write_mode: str = "identify_reconcile"

    def by_name(self, name: str) -> ProbeResult | None:
        return next((r for r in self.results if r.probe.name == name), None)

    @property
    def write_path_ok(self) -> bool:
        """True when the endpoint the configured write mode actually posts to is allowed."""
        name = "ire_write" if self.write_mode == "identify_reconcile" else "cmdb_instance_write"
        result = self.by_name(name)
        return bool(result and result.ok)

    def as_dict(self) -> dict[str, Any]:
        return {
            "write_mode": self.write_mode,
            "write_path_ok": self.write_path_ok,
            "endpoints": [
                {
                    "name": r.probe.name,
                    "method": r.probe.method,
                    "path": r.probe.path,
                    "verdict": r.verdict,
                    "status": r.status_code,
                    "logged_in": r.logged_in,
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }


def build_probes(cfg: ServiceNowConfig) -> list[EndpointProbe]:
    """The endpoint matrix, in the order an operator should read it."""
    ci_class = cfg.default_class or "cmdb_ci_computer"
    read_params = {"sysparm_limit": 1, "sysparm_fields": "sys_id"}
    ire_params = {"sysparm_data_source": cfg.discovery_source}

    probes = [
        EndpointProbe(
            name="table_read",
            method="GET",
            path=f"{TABLE_API}/sys_properties",
            params={"sysparm_query": "name=instance_name", **read_params},
            proves="the credential authenticates and the Table API is reachable",
        ),
        EndpointProbe(
            name="table_read_ci",
            method="GET",
            path=f"{TABLE_API}/{ci_class}",
            params=read_params,
            proves=f"the integration user can read {ci_class}",
        ),
        EndpointProbe(
            name="ire_query",
            method="POST",
            path=IDENTIFY_RECONCILE_QUERY_API,
            params=ire_params,
            json_body=EMPTY_IRE_BODY,
            proves="--dry-run can simulate a write",
        ),
        EndpointProbe(
            name="ire_write",
            method="POST",
            path=IDENTIFY_RECONCILE_API,
            params=ire_params,
            json_body=EMPTY_IRE_BODY,
            proves="the default write mode can run (this is the endpoint real runs use)",
        ),
        EndpointProbe(
            name="ire_write_versioned",
            method="POST",
            path=_versioned(IDENTIFY_RECONCILE_API),
            params=ire_params,
            json_body=EMPTY_IRE_BODY,
            proves="whether an auth scope bound to the versioned path behaves differently",
            required=False,
        ),
        EndpointProbe(
            name="ire_enhanced",
            method="POST",
            path=IDENTIFY_RECONCILE_ENHANCED_API,
            params=ire_params,
            json_body=EMPTY_IRE_BODY,
            proves="SNOW_USE_ENHANCED_IRE=true can run",
            required=cfg.use_enhanced_ire,
        ),
        EndpointProbe(
            name="cmdb_instance_read",
            method="GET",
            path=f"{CMDB_INSTANCE_API}/{ci_class}",
            params=read_params,
            api=f"{CMDB_INSTANCE_API}/{{className}}",
            proves="the CMDB Instance API is reachable for GET",
            required=False,
        ),
        EndpointProbe(
            name="cmdb_instance_write",
            method="POST",
            path=f"{CMDB_INSTANCE_API}/{PROBE_CLASS}",
            json_body={},
            api=f"{CMDB_INSTANCE_API}/{{className}}",
            proves="SNOW_WRITE_MODE=cmdb_instance can run",
            required=cfg.write_mode == "cmdb_instance",
        ),
        EndpointProbe(
            name="cmdb_instance_write_versioned",
            method="POST",
            path=_versioned(f"{CMDB_INSTANCE_API}/{PROBE_CLASS}"),
            json_body={},
            api=_versioned(f"{CMDB_INSTANCE_API}/{{className}}"),
            proves="whether the versioned CMDB Instance path is scoped differently",
            required=False,
        ),
    ]
    return probes


def _versioned(path: str, version: str = "v1") -> str:
    """Rewrite `/api/now/x` as `/api/now/v1/x`.

    ServiceNow serves most base-platform APIs at both, and an Application
    Registry REST API Auth Scope is recorded against a specific API *version*.
    A scope linked to one and not the other produces exactly the symptom this
    module exists to explain, so both get probed.
    """
    prefix = "/api/now/"
    return f"{prefix}{version}/{path[len(prefix):]}" if path.startswith(prefix) else path


def _body(response: Any) -> str:
    """The response body alone.

    `describe_error` now prefixes the URL, and the CMDB Instance probes put
    `PROBE_CLASS` *in* the URL, so matching on the formatted detail would call
    every 404 on that path a pass. Match on what the server said instead.
    """
    try:
        return response.text or ""
    except Exception:  # pragma: no cover - streamed/consumed body
        return ""


def classify(response: Any) -> tuple[str, str]:
    """Turn one HTTP response into (verdict, detail).

    A 4xx from the API's own validation is a **pass**: the probes are built to
    be rejected on their payload, so reaching the payload means the request got
    through authentication, the REST gate, and the ACLs.
    """
    status = response.status_code
    detail = describe_error(response)

    if response.is_success:
        return AUTHORIZED, f"HTTP {status}"
    if status == 401:
        return UNAUTHENTICATED, detail
    if status == 403:
        if unscoped_api_refusal(detail):
            return BLOCKED_AT_GATE, detail
        return DENIED_BY_ROLE, detail
    if status == 404:
        # The CMDB Instance probes aim at a class that does not exist, so a 404
        # whose *body* names the class is the API answering -- a pass. A 404 for
        # the API itself is not.
        if PROBE_CLASS in _body(response):
            return AUTHORIZED, f"HTTP {status} (reached the API; probe class rejected as expected)"
        return NOT_FOUND, detail
    if status in (400, 405, 422, 500):
        # Reached the API and it complained about the request, which is what a
        # deliberately-invalid probe is supposed to produce.
        return AUTHORIZED, f"HTTP {status} (reached the API; probe payload rejected as expected)"
    return UNKNOWN, detail


def probe_endpoints(client: ServiceNowClient, cfg: ServiceNowConfig) -> ProbeReport:
    """Run the whole matrix. Never raises for a refused endpoint -- that is data."""
    report = ProbeReport(write_mode=cfg.write_mode)
    for probe in build_probes(cfg):
        try:
            response = client.request(
                probe.method, probe.path, params=probe.params, json_body=probe.json_body
            )
        except SyncError as exc:
            # Transport failure or exhausted retries. Not an authorization
            # answer, and must not be reported as one.
            report.results.append(
                ProbeResult(probe=probe, verdict=UNKNOWN, status_code=None, detail=str(exc))
            )
            continue

        verdict, detail = classify(response)
        result = ProbeResult(
            probe=probe,
            verdict=verdict,
            status_code=response.status_code,
            detail=detail,
            logged_in=response.headers.get(LOGGED_IN_HEADER),
        )
        report.results.append(result)
        log.info(
            "endpoint probe",
            extra={
                "endpoint": probe.label,
                "verdict": verdict,
                "status": response.status_code,
                "logged_in": result.logged_in,
            },
        )
    return report


def diagnose(report: ProbeReport) -> list[str]:
    """Turn the matrix into the specific sentences an admin can act on."""
    lines: list[str] = []
    reads = [r for r in report.results if r.probe.method == "GET"]
    writes = [r for r in report.results if r.probe.method == "POST"]
    gated = [r for r in writes if r.verdict == BLOCKED_AT_GATE]
    unauth = [r for r in report.results if r.verdict == UNAUTHENTICATED]

    if unauth:
        lines.append(
            "The credential did not authenticate at all (HTTP 401). Fix SNOW_AUTH_MODE / "
            "client id / secret before reading anything else here."
        )
        return lines

    reads_ok = [r for r in reads if r.ok]
    if gated and reads_ok:
        allowed_gets = ", ".join(sorted(r.probe.label for r in reads_ok))
        lines.append(
            "Diagnosis: the OAuth client is refused these APIs at the REST gate, before any "
            "role or ACL is consulted. "
            f"{allowed_gets} succeeded on the same credential, so authentication and the "
            "integration user's roles are not the problem."
        )
        lines.append(
            "Fix: in Application Registry, on this client's entry, either set Scope "
            "Restriction = Broadly Scoped, or add a REST API Auth Scope covering "
            + ", ".join(sorted({r.probe.label for r in gated}))
            + ". Auth scopes bind per API *and per HTTP method* -- a scope that only "
            "covers GET will not admit these."
        )
        logged_in = {r.logged_in for r in gated if r.logged_in}
        if logged_in:
            lines.append(
                f"Corroborating: the refusals carry {LOGGED_IN_HEADER}: "
                f"{', '.join(sorted(logged_in))}, i.e. ServiceNow authenticated the request "
                "and then declined the API."
            )

    denied = [r for r in writes if r.verdict == DENIED_BY_ROLE]
    if denied:
        lines.append(
            "Refused without the unscoped-api marker on "
            + ", ".join(sorted(r.probe.label for r in denied))
            + ": that shape of 403 is a role or ACL problem, so check the integration user "
            "has 'itil' or 'asset'."
        )

    # Per-method and per-version asymmetries are the two findings that are
    # impossible to see from a single failing call, and both name the exact
    # thing to change in Application Registry.
    inst_read, inst_write = report.by_name("cmdb_instance_read"), report.by_name(
        "cmdb_instance_write"
    )
    if inst_read and inst_write and inst_read.ok and inst_write.verdict == BLOCKED_AT_GATE:
        lines.append(
            f"Note: {inst_read.probe.label} is allowed while POST to the same API is "
            "refused. That proves the restriction is per-method, not per-API."
        )

    for base, versioned in (
        ("ire_write", "ire_write_versioned"),
        ("cmdb_instance_write", "cmdb_instance_write_versioned"),
    ):
        a, b = report.by_name(base), report.by_name(versioned)
        if a and b and a.verdict != b.verdict and UNKNOWN not in (a.verdict, b.verdict):
            lines.append(
                f"Note: {a.probe.label} ({a.label}) and {b.probe.label} ({b.label}) differ. "
                "The auth scope is bound to one API version and not the other; scope both, "
                "or call the one that is scoped."
            )

    if report.write_path_ok:
        lines.append(
            f"The endpoint SNOW_WRITE_MODE={report.write_mode} posts to is allowed through. "
            "Any remaining failure is payload or data (discovery source, identification "
            "rules), not authorization -- run --check next."
        )
    elif not gated and not denied:
        lines.append(
            f"The write endpoint for SNOW_WRITE_MODE={report.write_mode} did not come back "
            "with a usable answer; read the rows above before drawing a conclusion."
        )
    return lines


def format_report(report: ProbeReport) -> str:
    """Render the matrix as a table plus the diagnosis."""
    rows = [(r.probe.label, r.label, str(r.status_code or "-"), r.probe.proves)
            for r in report.results]
    width_ep = max((len(r[0]) for r in rows), default=0)
    width_vd = max((len(r[1]) for r in rows), default=0)

    lines = ["", "ServiceNow endpoint authorization probe (nothing was written):", ""]
    for endpoint, verdict, status, proves in rows:
        lines.append(f"  {endpoint:<{width_ep}}  {verdict:<{width_vd}}  {status:>3}  {proves}")

    failures = [r for r in report.results if not r.ok]
    if failures:
        lines += ["", "Responses:"]
        # `detail` already leads with the method and the real path called.
        lines += [f"  {r.detail}" for r in failures]

    diagnosis = diagnose(report)
    if diagnosis:
        lines += [""] + [f"  {line}" for line in diagnosis]
    return "\n".join(lines) + "\n"
