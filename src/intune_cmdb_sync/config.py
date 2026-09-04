"""Environment-driven configuration.

Every knob is an environment variable so the same image runs unchanged on Azure
Container Apps, AWS Lambda, a laptop, or a cron box. Nothing is read from disk
except the optional mapping-override file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .secrets import resolve_secret

# Serial numbers firmware vendors ship as placeholders. Treating these as real
# identifiers is the single most common cause of mass CI collisions in the CMDB,
# because every affected machine "identifies" as the same CI.
DEFAULT_SERIAL_BLOCKLIST = (
    "0",
    "00000000",
    "123456789",
    "default string",
    "empty",
    "invalid",
    "n/a",
    "na",
    "none",
    "not applicable",
    "not available",
    "not specified",
    "o.e.m.",
    "system serial number",
    "to be filled by o.e.m.",
    "filled by o.e.m.",
    "unknown",
    "chassis serial number",
    "base board serial number",
    "not present",
    "0123456789",
    "1234567890",
)

# Intune `operatingSystem` value -> ServiceNow CMDB class. Anything not listed
# here is skipped unless SNOW_DEFAULT_CLASS is set.
DEFAULT_CLASS_MAP = {
    "windows": "cmdb_ci_computer",
    "macos": "cmdb_ci_computer",
}

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (got {raw!r})")


def _env_int(name: str, default: int, *, minimum: int | None = None,
             maximum: int | None = None) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer (got {raw!r})") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum} (got {value})")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum} (got {value})")
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None,
               maximum: float | None = None) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number (got {raw!r})") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum} (got {value})")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum} (got {value})")
    return value


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env(name)
    if raw is None:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _env_kv_map(name: str, default: dict[str, str]) -> dict[str, str]:
    """Parse `a=b;c=d` (or comma-separated) into a lowercased-key dict."""
    raw = _env(name)
    if raw is None:
        return dict(default)
    separator = ";" if ";" in raw else ","
    parsed: dict[str, str] = {}
    for pair in raw.split(separator):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ConfigError(f"{name} entries must look like key=value (got {pair!r})")
        key, _, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if not key or not value:
            raise ConfigError(f"{name} entries must look like key=value (got {pair!r})")
        parsed[key.lower()] = value
    return parsed


def _env_json_object(name: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = _env(name)
    if raw is None:
        return dict(default or {})
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{name} must be valid JSON ({exc})") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"{name} must be a JSON object")
    return parsed


@dataclass(frozen=True)
class GraphConfig:
    """Microsoft Graph connection and query settings."""

    tenant_id: str
    client_id: str | None
    client_secret: str | None
    auth_mode: str
    base_url: str
    scope: str
    page_size: int
    ownership: str
    server_side_filter: bool
    enrich_users: bool
    user_select_fields: tuple[str, ...]
    request_timeout: float
    max_retries: int
    # federated_managed_identity only: the managed identity that signs the
    # client assertion. None selects the system-assigned identity.
    assertion_identity_client_id: str | None = None
    # access_token only: a pre-obtained Graph bearer token. Local development
    # against a tenant where you cannot create an app registration.
    access_token: str | None = None

    @property
    def authority_host(self) -> str:
        return _env("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com") or ""


@dataclass(frozen=True)
class ServiceNowConfig:
    """ServiceNow connection, write-path, and CMDB semantics."""

    base_url: str
    auth_mode: str
    client_id: str | None
    client_secret: str | None
    username: str | None
    password: str | None
    write_mode: str
    use_enhanced_ire: bool
    enhanced_ire_options: str
    discovery_source: str
    source_feed: str
    class_map: dict[str, str]
    default_class: str | None
    batch_size: int
    concurrency: int
    # Stop a per-CI write run once this many writes have failed with none
    # having succeeded. 0 disables it.
    abort_after_errors: int
    request_timeout: float
    max_retries: int
    extra_attributes: dict[str, Any]
    assign_user: bool
    user_match_order: tuple[str, ...]
    user_entra_id_field: str | None
    user_active_only: bool
    retire_missing: bool
    retire_install_status: str
    retire_max_fraction: float
    install_status_active: str | None
    correlation_field: str | None
    set_correlation: bool
    fetch_hardware_detail: bool
    create_missing_manufacturers: bool
    create_missing_models: bool


@dataclass(frozen=True)
class RuntimeConfig:
    """Process-level behaviour."""

    dry_run: bool
    log_level: str
    log_format: str
    # A local path or an s3:// URL, resolved through storage.py the same way
    # STATE_PATH is -- a Lambda has no writable filesystem worth keeping.
    run_report_path: str | None
    # Include the per-device outcome list in the report. Env-settable because a
    # container deployment passes no CLI arguments.
    report_devices: bool
    # Exit non-zero when individual devices failed to write.
    fail_on_error: bool
    state_path: str | None
    serial_blocklist: frozenset[str]
    mapping_overrides: dict[str, Any]
    # Cap on devices processed in one run. A testing aid: it makes the first
    # write against a real instance small enough to inspect by hand.
    device_limit: int | None


@dataclass(frozen=True)
class Config:
    graph: GraphConfig
    servicenow: ServiceNowConfig
    runtime: RuntimeConfig

    @staticmethod
    def from_env() -> Config:
        return _build_config()


VALID_GRAPH_AUTH_MODES = {
    "access_token",
    "client_secret",
    "managed_identity",
    "federated_managed_identity",
    "workload_identity",
    "default",
}
VALID_SNOW_AUTH_MODES = {"oauth_client_credentials", "oauth_password", "basic"}
VALID_WRITE_MODES = {"identify_reconcile", "cmdb_instance"}
VALID_OWNERSHIP = {"company", "personal", "any"}
VALID_USER_MATCH_KEYS = {"employee_number", "email", "user_name", "entra_id"}

DEFAULT_USER_SELECT_FIELDS = (
    "id",
    "userPrincipalName",
    "mail",
    "displayName",
    "employeeId",
    "department",
    "companyName",
    "officeLocation",
    "city",
    "country",
    "accountEnabled",
)


def _normalize_instance_url(raw: str) -> str:
    """Accept `acme`, `acme.service-now.com`, or a full https URL."""
    value = raw.strip().rstrip("/")
    if value.startswith(("http://", "https://")):
        return value
    if "." in value:
        return f"https://{value}"
    return f"https://{value}.service-now.com"


def _build_config() -> Config:
    problems: list[str] = []

    # ---- Microsoft Graph -------------------------------------------------
    graph_auth_mode = (_env("GRAPH_AUTH_MODE", "client_secret") or "").lower()
    if graph_auth_mode not in VALID_GRAPH_AUTH_MODES:
        problems.append(
            f"GRAPH_AUTH_MODE must be one of {sorted(VALID_GRAPH_AUTH_MODES)} "
            f"(got {graph_auth_mode!r})"
        )

    graph_access_token = resolve_secret("GRAPH_ACCESS_TOKEN")
    tenant_id = _env("GRAPH_TENANT_ID") or _env("AZURE_TENANT_ID")
    client_id = _env("GRAPH_CLIENT_ID") or _env("AZURE_CLIENT_ID")
    client_secret = resolve_secret("GRAPH_CLIENT_SECRET") or resolve_secret("AZURE_CLIENT_SECRET")

    if graph_auth_mode == "access_token":
        # No tenant or client id needed: the token already encodes both, and
        # requiring them would only invite a mismatch between the two.
        if not graph_access_token:
            problems.append(
                "GRAPH_ACCESS_TOKEN is required when GRAPH_AUTH_MODE=access_token "
                "(or GRAPH_ACCESS_TOKEN_FILE). Get one with: az account get-access-token "
                "--resource https://graph.microsoft.com --query accessToken -o tsv"
            )
    elif graph_auth_mode == "client_secret":
        if not tenant_id:
            problems.append("GRAPH_TENANT_ID is required when GRAPH_AUTH_MODE=client_secret")
        if not client_id:
            problems.append("GRAPH_CLIENT_ID is required when GRAPH_AUTH_MODE=client_secret")
        if not client_secret:
            problems.append("GRAPH_CLIENT_SECRET is required when GRAPH_AUTH_MODE=client_secret")
    elif graph_auth_mode == "workload_identity" and not tenant_id:
        problems.append("GRAPH_TENANT_ID is required when GRAPH_AUTH_MODE=workload_identity")
    elif graph_auth_mode == "federated_managed_identity":
        # The tenant here is the one the app is consented into (where Intune
        # lives), which in the cross-tenant case is not the subscription's.
        if not tenant_id:
            problems.append(
                "GRAPH_TENANT_ID is required when GRAPH_AUTH_MODE=federated_managed_identity "
                "and must be the tenant the app registration is consented into"
            )
        if not client_id:
            problems.append(
                "GRAPH_CLIENT_ID is required when GRAPH_AUTH_MODE=federated_managed_identity "
                "and must be the multi-tenant app registration's client ID, not the "
                "managed identity's"
            )

    ownership = (_env("INTUNE_OWNERSHIP", "company") or "").lower()
    if ownership not in VALID_OWNERSHIP:
        problems.append(
            f"INTUNE_OWNERSHIP must be one of {sorted(VALID_OWNERSHIP)} (got {ownership!r})"
        )

    graph = GraphConfig(
        tenant_id=tenant_id or "",
        client_id=client_id,
        client_secret=client_secret,
        auth_mode=graph_auth_mode,
        base_url=(_env("GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0") or "").rstrip("/"),
        scope=_env("GRAPH_SCOPE", "https://graph.microsoft.com/.default") or "",
        page_size=_env_int("GRAPH_PAGE_SIZE", 200, minimum=1, maximum=1000),
        ownership=ownership,
        server_side_filter=_env_bool("INTUNE_SERVER_SIDE_FILTER", True),
        enrich_users=_env_bool("GRAPH_ENRICH_USERS", True),
        user_select_fields=_env_csv("GRAPH_USER_SELECT_FIELDS", DEFAULT_USER_SELECT_FIELDS),
        request_timeout=_env_float("GRAPH_TIMEOUT_SECONDS", 60.0, minimum=1.0),
        max_retries=_env_int("GRAPH_MAX_RETRIES", 5, minimum=0, maximum=10),
        assertion_identity_client_id=_env("GRAPH_ASSERTION_IDENTITY_CLIENT_ID"),
        access_token=graph_access_token,
    )

    # ---- ServiceNow ------------------------------------------------------
    instance = _env("SNOW_INSTANCE")
    if not instance:
        problems.append("SNOW_INSTANCE is required (e.g. 'acme' or 'https://acme.service-now.com')")

    snow_auth_mode = (_env("SNOW_AUTH_MODE", "oauth_client_credentials") or "").lower()
    if snow_auth_mode not in VALID_SNOW_AUTH_MODES:
        problems.append(
            f"SNOW_AUTH_MODE must be one of {sorted(VALID_SNOW_AUTH_MODES)} "
            f"(got {snow_auth_mode!r})"
        )

    snow_client_id = _env("SNOW_CLIENT_ID")
    snow_client_secret = resolve_secret("SNOW_CLIENT_SECRET")
    snow_username = _env("SNOW_USERNAME")
    snow_password = resolve_secret("SNOW_PASSWORD")

    if snow_auth_mode == "oauth_client_credentials":
        if not snow_client_id:
            problems.append(
                "SNOW_CLIENT_ID is required for SNOW_AUTH_MODE=oauth_client_credentials"
            )
        if not snow_client_secret:
            problems.append(
                "SNOW_CLIENT_SECRET is required for SNOW_AUTH_MODE=oauth_client_credentials"
            )
    elif snow_auth_mode == "oauth_password":
        missing = [
            n
            for n, v in (
                ("SNOW_CLIENT_ID", snow_client_id),
                ("SNOW_CLIENT_SECRET", snow_client_secret),
                ("SNOW_USERNAME", snow_username),
                ("SNOW_PASSWORD", snow_password),
            )
            if not v
        ]
        if missing:
            problems.append(
                f"SNOW_AUTH_MODE=oauth_password requires {', '.join(missing)}"
            )
    elif snow_auth_mode == "basic":
        if not snow_username or not snow_password:
            problems.append("SNOW_AUTH_MODE=basic requires SNOW_USERNAME and SNOW_PASSWORD")

    write_mode = (_env("SNOW_WRITE_MODE", "identify_reconcile") or "").lower()
    if write_mode not in VALID_WRITE_MODES:
        problems.append(
            f"SNOW_WRITE_MODE must be one of {sorted(VALID_WRITE_MODES)} (got {write_mode!r})"
        )

    user_match_order = _env_csv("SNOW_USER_MATCH_ORDER", ("employee_number", "email", "user_name"))
    unknown_keys = [k for k in user_match_order if k not in VALID_USER_MATCH_KEYS]
    if unknown_keys:
        problems.append(
            f"SNOW_USER_MATCH_ORDER contains unknown keys {unknown_keys}; "
            f"valid keys are {sorted(VALID_USER_MATCH_KEYS)}"
        )

    user_entra_id_field = _env("SNOW_USER_ENTRA_ID_FIELD")
    if "entra_id" in user_match_order and not user_entra_id_field:
        problems.append(
            "SNOW_USER_MATCH_ORDER includes 'entra_id' but SNOW_USER_ENTRA_ID_FIELD is not set"
        )

    class_map = _env_kv_map("SNOW_CLASS_MAP", DEFAULT_CLASS_MAP)
    default_class = _env("SNOW_DEFAULT_CLASS")
    if not class_map and not default_class:
        problems.append("SNOW_CLASS_MAP is empty and SNOW_DEFAULT_CLASS is unset; nothing to write")

    servicenow = ServiceNowConfig(
        base_url=_normalize_instance_url(instance) if instance else "",
        auth_mode=snow_auth_mode,
        client_id=snow_client_id,
        client_secret=snow_client_secret,
        username=snow_username,
        password=snow_password,
        write_mode=write_mode,
        use_enhanced_ire=_env_bool("SNOW_USE_ENHANCED_IRE", False),
        enhanced_ire_options=_env(
            "SNOW_ENHANCED_IRE_OPTIONS", "partial_payloads:true,generate_summary:true"
        ) or "",
        discovery_source=_env("SNOW_DISCOVERY_SOURCE", "Intune") or "Intune",
        source_feed=_env("SNOW_SOURCE_FEED", "Intune Managed Devices") or "",
        class_map=class_map,
        default_class=default_class,
        batch_size=_env_int("SNOW_BATCH_SIZE", 100, minimum=1, maximum=1000),
        concurrency=_env_int("SNOW_CONCURRENCY", 4, minimum=1, maximum=32),
        abort_after_errors=_env_int("SNOW_ABORT_AFTER_ERRORS", 10, minimum=0, maximum=10000),
        request_timeout=_env_float("SNOW_TIMEOUT_SECONDS", 60.0, minimum=1.0),
        max_retries=_env_int("SNOW_MAX_RETRIES", 5, minimum=0, maximum=10),
        extra_attributes=_env_json_object("SNOW_EXTRA_ATTRIBUTES"),
        assign_user=_env_bool("SNOW_ASSIGN_USER", True),
        user_match_order=user_match_order,
        user_entra_id_field=user_entra_id_field,
        user_active_only=_env_bool("SNOW_USER_ACTIVE_ONLY", True),
        retire_missing=_env_bool("SNOW_RETIRE_MISSING", False),
        retire_install_status=_env("SNOW_RETIRE_INSTALL_STATUS", "7") or "7",
        retire_max_fraction=_env_float("SNOW_RETIRE_MAX_FRACTION", 0.10, minimum=0.0, maximum=1.0),
        install_status_active=_env("SNOW_INSTALL_STATUS_ACTIVE"),
        correlation_field=_env("SNOW_CORRELATION_FIELD", "correlation_id"),
        set_correlation=_env_bool("SNOW_SET_CORRELATION", True),
        fetch_hardware_detail=_env_bool("INTUNE_FETCH_HARDWARE_DETAIL", False),
        create_missing_manufacturers=_env_bool("SNOW_CREATE_MISSING_MANUFACTURERS", False),
        create_missing_models=_env_bool("SNOW_CREATE_MISSING_MODELS", False),
    )

    # ---- Runtime ---------------------------------------------------------
    log_format = (_env("LOG_FORMAT", "json") or "").lower()
    if log_format not in {"json", "text"}:
        problems.append(f"LOG_FORMAT must be 'json' or 'text' (got {log_format!r})")

    serial_extra = _env_csv("SERIAL_BLOCKLIST_EXTRA", ())
    serial_blocklist = frozenset(
        s.lower() for s in (*DEFAULT_SERIAL_BLOCKLIST, *serial_extra) if s
    )

    overrides_path = _env("MAPPING_OVERRIDES_FILE")
    mapping_overrides: dict[str, Any] = {}
    if overrides_path:
        path = Path(overrides_path)
        if not path.is_file():
            problems.append(f"MAPPING_OVERRIDES_FILE does not exist: {overrides_path}")
        else:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                problems.append(f"MAPPING_OVERRIDES_FILE could not be read: {exc}")
            else:
                if isinstance(loaded, dict):
                    mapping_overrides = loaded
                else:
                    problems.append("MAPPING_OVERRIDES_FILE must contain a JSON object")

    device_limit = _env_int("INTUNE_DEVICE_LIMIT", 0, minimum=0) or None

    run_report = _env("RUN_REPORT_PATH")
    state_path = _env("STATE_PATH")

    if servicenow.retire_missing and not state_path:
        problems.append(
            "SNOW_RETIRE_MISSING=true requires STATE_PATH (a local path or an "
            "s3://bucket/key URL) so previously-synced devices can be compared "
            "against the current Intune inventory"
        )

    runtime = RuntimeConfig(
        dry_run=_env_bool("DRY_RUN", False),
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        log_format=log_format,
        run_report_path=run_report or None,
        report_devices=_env_bool("RUN_REPORT_DEVICES", False),
        fail_on_error=_env_bool("FAIL_ON_ERROR", False),
        state_path=state_path,
        device_limit=device_limit,
        serial_blocklist=serial_blocklist,
        mapping_overrides=mapping_overrides,
    )

    if problems:
        raise ConfigError(
            "Invalid configuration:\n  - " + "\n  - ".join(problems)
        )

    return Config(graph=graph, servicenow=servicenow, runtime=runtime)
