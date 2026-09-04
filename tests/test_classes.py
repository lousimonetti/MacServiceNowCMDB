"""CMDB class discovery and SNOW_CLASS_MAP validation.

Both 2026-09-04 runs skipped every macOS device with "no CMDB class mapped for
operatingSystem='macOS'" while the built-in default maps macos to
cmdb_ci_computer -- because setting SNOW_CLASS_MAP replaces that default rather
than extending it. Nothing in the run said so; the report line is the same one
you get for deliberately skipping iOS.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.servicenow.classes import (
    class_exists,
    format_classes,
    list_ci_classes,
    unmapped_os_note,
    verify_class_map,
)
from intune_cmdb_sync.servicenow.client import ServiceNowClient

SNOW = "https://acme.service-now.com"
TABLES = f"{SNOW}/api/now/table/sys_db_object"


@pytest.fixture
def snow_client(config: Config) -> ServiceNowClient:
    client = ServiceNowClient(config.servicenow)
    client.auth._token = "snow-token"
    client.auth._expires_at = float("inf")
    return client


class TestListing:
    @respx.mock
    def test_lists_cmdb_classes_only(self, snow_client, config: Config):
        route = respx.get(TABLES).mock(
            return_value=httpx.Response(
                200,
                json={"result": [
                    {"name": "cmdb_ci_computer", "label": "Computer"},
                    {"name": "cmdb_ci_appl", "label": "Application"},
                ]},
            )
        )
        classes = list_ci_classes(snow_client)
        assert [c["name"] for c in classes] == ["cmdb_ci_appl", "cmdb_ci_computer"]
        assert "nameSTARTSWITHcmdb_ci" in route.calls[0].request.url.params["sysparm_query"]

    @respx.mock
    def test_a_pattern_matches_the_name_or_the_label(self, snow_client, config: Config):
        """Someone hunting for a Mac class may know the label or the table
        name, rarely both."""
        route = respx.get(TABLES).mock(return_value=httpx.Response(200, json={"result": []}))
        list_ci_classes(snow_client, "mac")
        query = route.calls[0].request.url.params["sysparm_query"]
        assert "nameLIKEmac" in query
        assert "labelLIKEmac" in query

    @respx.mock
    def test_the_rendering_marks_what_is_already_mapped(self, snow_client, config: Config):
        respx.get(TABLES).mock(
            return_value=httpx.Response(
                200,
                json={"result": [
                    {"name": "cmdb_ci_computer", "label": "Computer"},
                    {"name": "cmdb_ci_msd", "label": "Mobile Device"},
                ]},
            )
        )
        text = format_classes(list_ci_classes(snow_client), config.servicenow)
        assert "cmdb_ci_computer" in text and "Computer" in text
        # The default map sends windows and macos here; say so rather than
        # making the reader cross-reference their own environment.
        assert "SNOW_CLASS_MAP" in text
        assert "cmdb_ci_msd" in text

    def test_an_empty_result_says_so(self, config: Config):
        assert "No CMDB classes matched" in format_classes([], config.servicenow)


class TestClassExists:
    @respx.mock
    def test_a_case_difference_is_not_a_match(self, snow_client):
        """Encoded-query `=` is not reliably case-sensitive, and a table name
        has to match exactly."""
        respx.get(TABLES).mock(
            return_value=httpx.Response(200, json={"result": [{"name": "cmdb_ci_computer"}]})
        )
        assert class_exists(snow_client, "cmdb_ci_computer")
        assert not class_exists(snow_client, "CMDB_CI_Computer")


class TestVerifyClassMap:
    @respx.mock
    def test_a_missing_class_is_a_problem_not_a_note(self, set_env):
        """It looks configured and fails every device of that OS at write
        time, which is the worst combination available."""
        set_env(SNOW_CLASS_MAP="windows=cmdb_ci_computer;macos=cmdb_ci_mac_typo")
        cfg = Config.from_env()
        client = ServiceNowClient(cfg.servicenow)
        client.auth._token = "t"
        client.auth._expires_at = float("inf")

        def respond(request):
            name = request.url.params["sysparm_query"].removeprefix("name=")
            rows = [{"name": name}] if name == "cmdb_ci_computer" else []
            return httpx.Response(200, json={"result": rows})

        respx.get(TABLES).mock(side_effect=respond)

        problems = verify_class_map(client, cfg.servicenow)
        assert len(problems) == 1
        assert "cmdb_ci_mac_typo" in problems[0]
        assert "'macos'" in problems[0]

    @respx.mock
    def test_one_request_per_class_not_per_os(self, set_env):
        set_env(SNOW_CLASS_MAP="windows=cmdb_ci_computer;macos=cmdb_ci_computer")
        cfg = Config.from_env()
        client = ServiceNowClient(cfg.servicenow)
        client.auth._token = "t"
        client.auth._expires_at = float("inf")
        route = respx.get(TABLES).mock(
            return_value=httpx.Response(200, json={"result": [{"name": "cmdb_ci_computer"}]})
        )
        assert verify_class_map(client, cfg.servicenow) == []
        assert route.call_count == 1


class TestUnmappedOsNote:
    def test_names_the_replacement_behaviour_that_causes_it(self, set_env):
        """The live failure: SNOW_CLASS_MAP set without macos, and the built-in
        default that includes it silently gone."""
        set_env(SNOW_CLASS_MAP="windows=cmdb_ci_computer")
        note = unmapped_os_note(Config.from_env().servicenow)
        assert note is not None
        assert "macos" in note
        assert "replaces the built-in default" in note

    def test_a_default_class_means_nothing_is_dropped(self, set_env):
        set_env(SNOW_CLASS_MAP="windows=cmdb_ci_computer", SNOW_DEFAULT_CLASS="cmdb_ci_computer")
        assert unmapped_os_note(Config.from_env().servicenow) is None

    def test_a_full_map_produces_no_note(self, set_env):
        set_env(
            SNOW_CLASS_MAP=(
                "windows=cmdb_ci_computer;macos=cmdb_ci_computer;ios=cmdb_ci_msd;"
                "android=cmdb_ci_msd;linux=cmdb_ci_computer"
            )
        )
        assert unmapped_os_note(Config.from_env().servicenow) is None
