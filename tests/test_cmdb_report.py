from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from intune_cmdb_sync.cmdb_report import (
    Cell,
    CmdbReader,
    CmdbReport,
    analyze,
    by_discovery_source,
    by_field,
    combine,
)
from intune_cmdb_sync.config import Config
from intune_cmdb_sync.errors import ServiceNowError, SyncError
from intune_cmdb_sync.query_cli import main, render
from intune_cmdb_sync.servicenow.client import ServiceNowClient

SNOW = "https://acme.service-now.com"
COMPUTER = f"{SNOW}/api/now/table/cmdb_ci_computer"


@pytest.fixture
def snow_client(config: Config) -> ServiceNowClient:
    client = ServiceNowClient(config.servicenow)
    client.auth._token = "snow-token"
    client.auth._expires_at = float("inf")
    return client


def raw_row(**overrides: Any) -> dict[str, Any]:
    """A row as ServiceNow returns it under sysparm_display_value=all."""
    base: dict[str, Any] = {
        "sys_id": {"value": "ci-1", "display_value": "ci-1"},
        "sys_class_name": {"value": "cmdb_ci_computer", "display_value": "Computer"},
        "name": {"value": "LOU-MBP", "display_value": "LOU-MBP"},
        "serial_number": {"value": "C02XY1234", "display_value": "C02XY1234"},
        "manufacturer": {"value": "mfr-sys-id", "display_value": "Apple Inc."},
        "model_id": {"value": "model-sys-id", "display_value": "MacBook Pro (16-inch, 2023)"},
        "install_status": {"value": "1", "display_value": "Installed"},
        "assigned_to": {"value": "user-sys-id", "display_value": "Lou Simonetti"},
    }
    base.update(overrides)
    return base


def cells(**overrides: str) -> dict[str, Cell]:
    """A already-normalised row, for the pure analyze() tests."""
    base = {
        "sys_id": "ci-1",
        "name": "LOU-MBP",
        "serial_number": "C02XY1234",
        "manufacturer": "Apple Inc.",
        "model_id": "MacBook Pro",
        "assigned_to": "Lou Simonetti",
        "install_status": "1",
    }
    base.update(overrides)
    return {name: Cell(value=value, display=value) for name, value in base.items()}


# ---- reader --------------------------------------------------------------


@respx.mock
def test_fetch_normalises_display_values(snow_client: ServiceNowClient) -> None:
    respx.get(COMPUTER).mock(return_value=httpx.Response(200, json={"result": [raw_row()]}))

    reader = CmdbReader(snow_client, table="cmdb_ci_computer", fields=("sys_id", "model_id"))
    rows, truncated = reader.fetch("discovery_source=Intune")

    assert truncated is False
    # A reference field keeps both halves: the label for a human, the sys_id to
    # find the record with.
    assert rows[0]["model_id"] == Cell(value="model-sys-id", display="MacBook Pro (16-inch, 2023)")
    assert rows[0]["model_id"].best == "MacBook Pro (16-inch, 2023)"


@respx.mock
def test_fetch_asks_for_display_values(snow_client: ServiceNowClient) -> None:
    route = respx.get(COMPUTER).mock(return_value=httpx.Response(200, json={"result": []}))

    CmdbReader(snow_client, table="cmdb_ci_computer", fields=("sys_id",)).fetch("name=x")

    params = route.calls[0].request.url.params
    assert params["sysparm_display_value"] == "all"
    assert params["sysparm_query"] == "name=x"


@respx.mock
def test_fetch_is_read_only(snow_client: ServiceNowClient) -> None:
    """Nothing in the read path may issue a non-GET request."""
    route = respx.get(COMPUTER).mock(return_value=httpx.Response(200, json={"result": [raw_row()]}))

    CmdbReader(snow_client, table="cmdb_ci_computer").fetch("discovery_source=Intune")

    assert [call.request.method for call in route.calls] == ["GET"]
    assert all(call.request.method == "GET" for call in respx.calls)


@respx.mock
def test_fetch_pages_until_a_short_page(snow_client: ServiceNowClient) -> None:
    full = [raw_row(sys_id={"value": f"ci-{n}", "display_value": f"ci-{n}"}) for n in range(500)]
    route = respx.get(COMPUTER).mock(
        side_effect=[
            httpx.Response(200, json={"result": full}),
            httpx.Response(
                200,
                json={"result": [raw_row(sys_id={"value": "ci-500", "display_value": "ci-500"})]},
            ),
        ]
    )

    rows, truncated = CmdbReader(snow_client, table="cmdb_ci_computer").fetch(
        "discovery_source=Intune"
    )

    assert len(rows) == 501
    assert truncated is False
    assert route.calls[1].request.url.params["sysparm_offset"] == "500"


@respx.mock
def test_fetch_reports_truncation_at_the_row_cap(snow_client: ServiceNowClient) -> None:
    respx.get(COMPUTER).mock(
        return_value=httpx.Response(200, json={"result": [raw_row(), raw_row()]})
    )

    rows, truncated = CmdbReader(snow_client, table="cmdb_ci_computer").fetch(
        "discovery_source=Intune", max_rows=2
    )

    # Silently returning 2 rows would understate the fleet; the caller has to know.
    assert len(rows) == 2
    assert truncated is True


@respx.mock
def test_fetch_rejects_an_unexpected_shape(snow_client: ServiceNowClient) -> None:
    respx.get(COMPUTER).mock(return_value=httpx.Response(200, json={"result": {"sys_id": "x"}}))

    with pytest.raises(ServiceNowError):
        CmdbReader(snow_client, table="cmdb_ci_computer").fetch("name=x")


@respx.mock
def test_fetch_surfaces_the_403_body(snow_client: ServiceNowClient) -> None:
    respx.get(COMPUTER).mock(
        return_value=httpx.Response(403, json={"error": {"message": "Insufficient rights"}})
    )

    with pytest.raises(ServiceNowError, match="403"):
        CmdbReader(snow_client, table="cmdb_ci_computer").fetch("name=x")


def test_to_cell_handles_a_bare_string(snow_client: ServiceNowClient) -> None:
    """Fields with no separate display form come back as plain strings."""
    reader = CmdbReader(snow_client, table="cmdb_ci_computer", fields=("name",))
    assert reader._to_row({"name": "LOU-MBP"})["name"] == Cell(value="LOU-MBP", display="")


# ---- selectors -----------------------------------------------------------


def test_by_field_or_chains_rather_than_using_IN() -> None:
    # A comma inside a model name is why this cannot be `model_idIN...`.
    query = by_field("model_id", ["MacBook Pro (16-inch, 2023)", "Mac16,1"])
    assert query == "model_id=MacBook Pro (16-inch, 2023)^ORmodel_id=Mac16,1"


def test_by_field_drops_unsafe_and_blank_values() -> None:
    assert by_field("name", ["  a  ", "", "a"]) == "name=a"
    with pytest.raises(ValueError):
        by_field("name", ["^ORsys_id!=x"])


def test_combine_skips_empty_clauses() -> None:
    assert combine(by_discovery_source("Intune"), None, "install_status=1") == (
        "discovery_source=Intune^install_status=1"
    )


# ---- validation ----------------------------------------------------------


def test_analyze_is_quiet_on_a_healthy_row(config: Config) -> None:
    assert analyze([cells()], config.servicenow) == []


def test_analyze_flags_a_missing_serial(config: Config) -> None:
    findings = analyze([cells(serial_number="")], config.servicenow)
    assert [f.kind for f in findings] == ["missing_serial"]
    assert findings[0].sys_ids == ("ci-1",)


def test_analyze_flags_an_unresolved_reference(config: Config) -> None:
    findings = analyze([cells(manufacturer="", model_id="")], config.servicenow)
    kinds = {f.kind for f in findings}
    assert kinds == {"unresolved_manufacturer", "unresolved_model_id"}


def test_analyze_flags_duplicate_serials(config: Config) -> None:
    rows = [cells(), {**cells(), "sys_id": Cell("ci-2", "ci-2")}]
    findings = [f for f in analyze(rows, config.servicenow) if f.kind == "duplicate_serial_number"]
    # Two CIs on one serial is IRE one payload away from collapsing them.
    assert findings[0].sys_ids == ("ci-1", "ci-2")


def test_analyze_ignores_duplicate_blanks(config: Config) -> None:
    rows = [cells(serial_number=""), {**cells(serial_number=""), "sys_id": Cell("ci-2", "ci-2")}]
    kinds = [f.kind for f in analyze(rows, config.servicenow)]
    assert "duplicate_serial_number" not in kinds


def test_analyze_flags_an_unexpected_install_status(set_env) -> None:
    set_env(SNOW_INSTALL_STATUS_ACTIVE="1")
    cfg = Config.from_env()
    findings = analyze([cells(install_status="7")], cfg.servicenow)
    assert any(f.kind == "unexpected_install_status" for f in findings)


def test_analyze_only_checks_fields_that_were_read(config: Config) -> None:
    """A --fields subset must not report every omitted field as unresolved."""
    row = {"sys_id": Cell("ci-1", "ci-1"), "name": Cell("LOU-MBP", "LOU-MBP")}
    assert [f.kind for f in analyze([row], config.servicenow)] == ["missing_serial"]


# ---- rendering -----------------------------------------------------------


def report_for(rows: list[dict[str, Cell]], **overrides: Any) -> CmdbReport:
    base: dict[str, Any] = {
        "table": "cmdb_ci_computer",
        "query": "discovery_source=Intune",
        "fields": ("sys_id", "name", "serial_number", "model_id", "install_status"),
        "rows": rows,
    }
    base.update(overrides)
    return CmdbReport(**base)


def test_render_json_keeps_both_halves_of_a_reference() -> None:
    payload = json.loads(render(report_for([cells()]), "json"))
    assert payload["rows"][0]["name"] == "LOU-MBP"
    assert payload["count"] == 1


def test_render_table_lists_one_line_per_ci() -> None:
    text = render(report_for([cells()]), "table")
    assert "1 CI(s) in cmdb_ci_computer" in text
    assert "LOU-MBP" in text
    assert "No findings." in text


def test_render_warns_when_truncated() -> None:
    text = render(report_for([cells()], truncated=True), "table")
    assert "partial view" in text


def test_render_detail_shows_the_sys_id_behind_a_label() -> None:
    row = {**cells(), "model_id": Cell(value="model-sys-id", display="MacBook Pro")}
    text = render(report_for([row], fields=("model_id",)), "detail")
    assert "MacBook Pro  (model-sys-id)" in text


# ---- CLI -----------------------------------------------------------------


@respx.mock
def test_cli_defaults_to_the_configured_discovery_source(set_env, capsys) -> None:
    set_env(SNOW_DISCOVERY_SOURCE="Intune")
    route = respx.get(COMPUTER).mock(return_value=httpx.Response(200, json={"result": [raw_row()]}))
    respx.post(f"{SNOW}/oauth_token.do").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    )

    assert main(["--format", "table"]) == 0
    assert route.calls[0].request.url.params["sysparm_query"] == "discovery_source=Intune"
    assert "LOU-MBP" in capsys.readouterr().out


@respx.mock
def test_cli_refuses_an_unscoped_whole_table_read(set_env, capsys) -> None:
    set_env()
    # --all-sources with no other selector would page the entire class table.
    assert main(["--all-sources"]) == 3


@respx.mock
def test_cli_ands_selectors_together(set_env) -> None:
    set_env(SNOW_DISCOVERY_SOURCE="Intune")
    route = respx.get(COMPUTER).mock(return_value=httpx.Response(200, json={"result": []}))
    respx.post(f"{SNOW}/oauth_token.do").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    )

    assert main(["--serial", "C02XY1234", "--serial", "C02XY9999"]) == 0
    assert route.calls[0].request.url.params["sysparm_query"] == (
        "discovery_source=Intune^serial_number=C02XY1234^ORserial_number=C02XY9999"
    )


@respx.mock
def test_cli_intune_id_queries_the_correlation_field(set_env) -> None:
    set_env(SNOW_DISCOVERY_SOURCE="Intune", SNOW_CORRELATION_FIELD="u_intune_id")
    route = respx.get(COMPUTER).mock(return_value=httpx.Response(200, json={"result": []}))
    respx.post(f"{SNOW}/oauth_token.do").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    )

    assert main(["--intune-id", "device-guid"]) == 0
    params = route.calls[0].request.url.params
    assert params["sysparm_query"] == "discovery_source=Intune^u_intune_id=device-guid"
    # The correlation field is instance-specific, so it is not in CI_FIELDS and
    # has to be appended, or the query would filter on a column it never read.
    assert "u_intune_id" in params["sysparm_fields"]


def test_intune_id_without_a_correlation_field_is_an_error(config: Config) -> None:
    """Defensive: the dataclass allows None even though the env default never is."""
    import argparse
    import dataclasses

    from intune_cmdb_sync.query_cli import _build_query

    cfg = dataclasses.replace(
        config, servicenow=dataclasses.replace(config.servicenow, correlation_field=None)
    )
    args = argparse.Namespace(
        source=None, all_sources=False, serial=None, name=None, sys_id=None,
        intune_id=["abc"], query=None,
    )
    with pytest.raises(SyncError, match="SNOW_CORRELATION_FIELD"):
        _build_query(args, cfg)


@respx.mock
def test_cli_exits_4_only_when_asked(set_env) -> None:
    set_env(SNOW_DISCOVERY_SOURCE="Intune")
    respx.get(COMPUTER).mock(
        return_value=httpx.Response(
            200, json={"result": [raw_row(serial_number={"value": "", "display_value": ""})]}
        )
    )
    respx.post(f"{SNOW}/oauth_token.do").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    )

    assert main([]) == 0
    assert main(["--fail-on-findings"]) == 4


@respx.mock
def test_cli_writes_to_a_file(set_env, tmp_path) -> None:
    set_env(SNOW_DISCOVERY_SOURCE="Intune")
    respx.get(COMPUTER).mock(return_value=httpx.Response(200, json={"result": [raw_row()]}))
    respx.post(f"{SNOW}/oauth_token.do").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    )
    out = tmp_path / "report.json"

    assert main(["--format", "json", "--output", str(out)]) == 0
    assert json.loads(out.read_text())["count"] == 1


def test_cli_reports_a_config_error(monkeypatch, capsys) -> None:
    monkeypatch.delenv("SNOW_INSTANCE", raising=False)
    assert main([]) == 2
