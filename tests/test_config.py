from __future__ import annotations

import json

import pytest

from intune_cmdb_sync.config import Config, _normalize_instance_url
from intune_cmdb_sync.errors import ConfigError


class TestInstanceUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("acme", "https://acme.service-now.com"),
            ("acme.service-now.com", "https://acme.service-now.com"),
            ("https://acme.service-now.com", "https://acme.service-now.com"),
            ("https://acme.service-now.com/", "https://acme.service-now.com"),
            ("acmedev.servicenowservices.com", "https://acmedev.servicenowservices.com"),
        ],
    )
    def test_accepts_short_and_full_forms(self, raw, expected):
        assert _normalize_instance_url(raw) == expected


class TestRequiredValues:
    def test_defaults_are_sane(self, config: Config):
        assert config.servicenow.write_mode == "identify_reconcile"
        assert config.graph.ownership == "company"
        assert config.servicenow.discovery_source == "Intune"
        assert config.runtime.dry_run is False
        assert config.servicenow.retire_missing is False

    def test_missing_instance_is_reported(self, set_env):
        set_env(SNOW_INSTANCE=None)
        with pytest.raises(ConfigError, match="SNOW_INSTANCE is required"):
            Config.from_env()

    def test_client_secret_mode_requires_all_three(self, set_env):
        set_env(GRAPH_CLIENT_SECRET=None)
        with pytest.raises(ConfigError, match="GRAPH_CLIENT_SECRET is required"):
            Config.from_env()

    def test_managed_identity_needs_no_secret(self, set_env):
        set_env(GRAPH_AUTH_MODE="managed_identity", GRAPH_CLIENT_SECRET=None)
        cfg = Config.from_env()
        assert cfg.graph.auth_mode == "managed_identity"

    def test_basic_auth_requires_credentials(self, set_env):
        set_env(SNOW_AUTH_MODE="basic")
        with pytest.raises(ConfigError, match="SNOW_USERNAME and SNOW_PASSWORD"):
            Config.from_env()

    def test_all_problems_are_reported_together(self, set_env):
        set_env(SNOW_INSTANCE=None, GRAPH_CLIENT_SECRET=None)
        with pytest.raises(ConfigError) as excinfo:
            Config.from_env()
        message = str(excinfo.value)
        assert "SNOW_INSTANCE" in message
        assert "GRAPH_CLIENT_SECRET" in message


class TestParsing:
    @pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
    def test_truthy_booleans(self, set_env, raw):
        set_env(DRY_RUN=raw)
        assert Config.from_env().runtime.dry_run is True

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off"])
    def test_falsy_booleans(self, set_env, raw):
        set_env(DRY_RUN=raw)
        assert Config.from_env().runtime.dry_run is False

    def test_invalid_boolean_is_rejected(self, set_env):
        set_env(DRY_RUN="maybe")
        with pytest.raises(ConfigError, match="DRY_RUN must be a boolean"):
            Config.from_env()

    def test_integer_bounds_are_enforced(self, set_env):
        set_env(SNOW_CONCURRENCY="99")
        with pytest.raises(ConfigError, match="SNOW_CONCURRENCY must be <= 32"):
            Config.from_env()

    def test_class_map_semicolon_and_comma(self, set_env):
        set_env(SNOW_CLASS_MAP="windows=cmdb_ci_computer;macos=cmdb_ci_computer")
        assert Config.from_env().servicenow.class_map == {
            "windows": "cmdb_ci_computer",
            "macos": "cmdb_ci_computer",
        }
        set_env(SNOW_CLASS_MAP="windows=cmdb_ci_computer,macos=cmdb_ci_computer")
        assert len(Config.from_env().servicenow.class_map) == 2

    def test_malformed_class_map_is_rejected(self, set_env):
        set_env(SNOW_CLASS_MAP="windows")
        with pytest.raises(ConfigError, match="key=value"):
            Config.from_env()

    def test_extra_attributes_must_be_a_json_object(self, set_env):
        set_env(SNOW_EXTRA_ATTRIBUTES="[1,2,3]")
        with pytest.raises(ConfigError, match="must be a JSON object"):
            Config.from_env()

    def test_unknown_write_mode_is_rejected(self, set_env):
        set_env(SNOW_WRITE_MODE="yolo")
        with pytest.raises(ConfigError, match="SNOW_WRITE_MODE must be one of"):
            Config.from_env()


class TestCrossFieldValidation:
    def test_retire_requires_state_path(self, set_env):
        set_env(SNOW_RETIRE_MISSING="true")
        with pytest.raises(ConfigError, match="requires STATE_PATH"):
            Config.from_env()

    def test_retire_with_state_path_is_accepted(self, set_env, tmp_path):
        set_env(SNOW_RETIRE_MISSING="true", STATE_PATH=str(tmp_path / "state.json"))
        assert Config.from_env().servicenow.retire_missing is True

    def test_entra_id_match_requires_a_field_name(self, set_env):
        set_env(SNOW_USER_MATCH_ORDER="entra_id,email")
        with pytest.raises(ConfigError, match="SNOW_USER_ENTRA_ID_FIELD is not set"):
            Config.from_env()

    def test_unknown_user_match_key_is_rejected(self, set_env):
        set_env(SNOW_USER_MATCH_ORDER="email,astrology")
        with pytest.raises(ConfigError, match="unknown keys"):
            Config.from_env()

    def test_empty_class_map_without_default_is_rejected(self, set_env):
        set_env(SNOW_CLASS_MAP="  ")
        # A blank value falls back to the default map, so force it empty via a
        # value that parses to nothing.
        set_env(SNOW_CLASS_MAP=";")
        with pytest.raises(ConfigError, match="nothing to write"):
            Config.from_env()


class TestMappingOverridesFile:
    def test_loads_json_file(self, set_env, tmp_path):
        path = tmp_path / "map.json"
        path.write_text(json.dumps({"fields": {"u_compliance": "complianceState"}}))
        set_env(MAPPING_OVERRIDES_FILE=str(path))
        cfg = Config.from_env()
        assert cfg.runtime.mapping_overrides["fields"]["u_compliance"] == "complianceState"

    def test_missing_file_is_reported(self, set_env, tmp_path):
        set_env(MAPPING_OVERRIDES_FILE=str(tmp_path / "nope.json"))
        with pytest.raises(ConfigError, match="does not exist"):
            Config.from_env()

    def test_invalid_json_is_reported(self, set_env, tmp_path):
        path = tmp_path / "map.json"
        path.write_text("{not json")
        set_env(MAPPING_OVERRIDES_FILE=str(path))
        with pytest.raises(ConfigError, match="could not be read"):
            Config.from_env()
