from __future__ import annotations

import pytest

from intune_cmdb_sync.config import DEFAULT_SERIAL_BLOCKLIST, Config
from intune_cmdb_sync.mapping import (
    DeviceMapper,
    bytes_to_gb,
    bytes_to_mb,
    normalize_serial,
    os_display_name,
    resolve_class_name,
    to_snow_datetime,
)
from intune_cmdb_sync.models import EntraUser, SysUserRef

from .conftest import make_device

BLOCKLIST = frozenset(s.lower() for s in DEFAULT_SERIAL_BLOCKLIST)


class TestNormalizeSerial:
    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "   ",
            "To be filled by O.E.M.",
            "TO BE FILLED BY O.E.M.",
            "System Serial Number",
            "Default string",
            "0",
            "00000000",
            "unknown",
            "ab",  # too short
            "0000-0000",  # all one character once punctuation is stripped
        ],
    )
    def test_rejects_placeholders(self, raw):
        assert normalize_serial(raw, BLOCKLIST) is None

    def test_keeps_real_serial(self):
        assert normalize_serial("C02XY1Z2ABCD", BLOCKLIST) == "C02XY1Z2ABCD"

    def test_collapses_whitespace(self):
        assert normalize_serial("  5CD 123   4XY  ", BLOCKLIST) == "5CD 123 4XY"

    def test_extra_blocklist_entries_are_honoured(self):
        extended = BLOCKLIST | {"chassis serial number"}
        assert normalize_serial("Chassis Serial Number", extended) is None


class TestTimestamps:
    def test_seven_digit_fraction_from_graph(self):
        assert to_snow_datetime("2026-08-25T06:11:02.7654321Z") == "2026-08-25 06:11:02"

    def test_offset_is_converted_to_utc(self):
        assert to_snow_datetime("2017-01-01T00:02:49.3205976-08:00") == "2017-01-01 08:02:49"

    @pytest.mark.parametrize(
        "raw", [None, "", "0001-01-01T00:00:00Z", "0001-01-01T00:00:00.0000000Z", "nonsense"]
    )
    def test_null_sentinels_and_junk_become_none(self, raw):
        assert to_snow_datetime(raw) is None


class TestUnits:
    def test_bytes_to_mb_rounds(self):
        assert bytes_to_mb(17179869184) == 16384

    def test_bytes_to_gb_rounds_to_two_places(self):
        assert bytes_to_gb(994662584320) == 926.35

    @pytest.mark.parametrize("raw", [None, 0, "", "abc", -5])
    def test_non_positive_becomes_none(self, raw):
        assert bytes_to_mb(raw) is None
        assert bytes_to_gb(raw) is None


class TestClassRouting:
    def test_maps_known_os(self, config: Config):
        assert resolve_class_name(make_device(operatingSystem="macOS"), config) == (
            "cmdb_ci_computer"
        )
        assert resolve_class_name(make_device(operatingSystem="Windows"), config) == (
            "cmdb_ci_computer"
        )

    def test_unmapped_os_without_default_is_skipped(self, config: Config):
        assert resolve_class_name(make_device(operatingSystem="iOS"), config) is None

    def test_default_class_catches_unmapped_os(self, set_env):
        set_env(SNOW_DEFAULT_CLASS="cmdb_ci_computer")
        cfg = Config.from_env()
        assert resolve_class_name(make_device(operatingSystem="iOS"), cfg) == "cmdb_ci_computer"

    def test_custom_class_map(self, set_env):
        set_env(SNOW_CLASS_MAP="windows=cmdb_ci_win_server;macos=cmdb_ci_computer")
        cfg = Config.from_env()
        assert resolve_class_name(make_device(operatingSystem="Windows"), cfg) == (
            "cmdb_ci_win_server"
        )


class TestOsDisplayName:
    def test_default_translation(self):
        assert os_display_name({"operatingSystem": "macOS"}, {}) == "Mac OS X"
        assert os_display_name({"operatingSystem": "Windows"}, {}) == "Windows"

    def test_override_wins(self):
        assert os_display_name({"operatingSystem": "macOS"}, {"macOS": "macOS"}) == "macOS"

    def test_unknown_os_passes_through(self):
        assert os_display_name({"operatingSystem": "PlanNine"}, {}) == "PlanNine"


class TestDeviceMapper:
    def test_core_fields(self, config: Config):
        values = DeviceMapper(config).build_values(make_device())
        assert values["name"] == "LOU-MBP16"
        assert values["serial_number"] == "C02XY1Z2ABCD"
        assert values["os"] == "Mac OS X"
        assert values["os_version"] == "15.3.1"
        assert values["mac_address"] == "A4:B1:C2:D3:E4:F5"
        assert values["disk_space"] == 926.35
        assert values["first_discovered"] == "2025-04-02 10:15:30"
        assert values["last_discovered"] == "2026-08-25 06:11:02"
        assert values["virtual"] is False

    def test_correlation_id_carries_the_intune_device_id(self, config: Config):
        values = DeviceMapper(config).build_values(make_device())
        assert values["correlation_id"] == "705c034c-034c-705c-4c03-5c704c035c70"

    def test_correlation_can_be_disabled(self, set_env):
        set_env(SNOW_SET_CORRELATION="false")
        values = DeviceMapper(Config.from_env()).build_values(make_device())
        assert "correlation_id" not in values

    def test_empty_and_null_values_are_dropped(self, config: Config):
        device = make_device(osVersion="", model=None, serialNumber="unknown")
        values = DeviceMapper(config).build_values(device)
        assert "os_version" not in values
        assert "serial_number" not in values

    def test_assigned_to_uses_resolved_sys_user(self, config: Config):
        ref = SysUserRef(
            sys_id="abc123", user_name="lou", email="lou@example.com",
            employee_number="E42", matched_on="email",
        )
        values = DeviceMapper(config).build_values(make_device(), sys_user=ref)
        assert values["assigned_to"] == "abc123"

    def test_assign_user_disabled(self, set_env):
        set_env(SNOW_ASSIGN_USER="false")
        ref = SysUserRef("abc123", "lou", "lou@example.com", "E42", "email")
        values = DeviceMapper(Config.from_env()).build_values(make_device(), sys_user=ref)
        assert "assigned_to" not in values

    def test_reference_sys_ids_are_applied(self, config: Config):
        values = DeviceMapper(config).build_values(
            make_device(), references={"manufacturer": "mfr-sys-id", "model_id": "model-sys-id"}
        )
        assert values["manufacturer"] == "mfr-sys-id"
        assert values["model_id"] == "model-sys-id"

    def test_unresolved_references_are_omitted_not_written_as_names(self, config: Config):
        values = DeviceMapper(config).build_values(make_device(), references={})
        assert "manufacturer" not in values
        assert "model_id" not in values

    def test_ram_only_appears_with_hardware_detail(self, config: Config):
        mapper = DeviceMapper(config)
        assert "ram" not in mapper.build_values(make_device())
        enriched = make_device(physicalMemoryInBytes=68719476736)
        assert mapper.build_values(enriched)["ram"] == 65536

    def test_ethernet_mac_wins_over_wifi(self, config: Config):
        device = make_device(ethernetMacAddress="001122334455")
        values = DeviceMapper(config).build_values(device)
        assert values["mac_address"] == "00:11:22:33:44:55"

    def test_install_status_set_when_configured(self, set_env):
        set_env(SNOW_INSTALL_STATUS_ACTIVE="1")
        values = DeviceMapper(Config.from_env()).build_values(make_device())
        assert values["install_status"] == "1"

    def test_extra_attributes_are_merged(self, set_env):
        set_env(SNOW_EXTRA_ATTRIBUTES='{"company": "co-sys-id", "u_source": "intune"}')
        values = DeviceMapper(Config.from_env()).build_values(make_device())
        assert values["company"] == "co-sys-id"
        assert values["u_source"] == "intune"


class TestMappingOverrides:
    def _mapper(self, config: Config, overrides: dict) -> DeviceMapper:
        object.__setattr__(config.runtime, "mapping_overrides", overrides)
        return DeviceMapper(config)

    def test_extra_device_field(self, config: Config):
        mapper = self._mapper(config, {"fields": {"u_compliance": "complianceState"}})
        assert mapper.build_values(make_device())["u_compliance"] == "compliant"

    def test_timestamp_fields_are_auto_converted(self, config: Config):
        mapper = self._mapper(config, {"fields": {"u_last_sync": "lastSyncDateTime"}})
        assert mapper.build_values(make_device())["u_last_sync"] == "2026-08-25 06:11:02"

    def test_user_scoped_field(self, config: Config):
        mapper = self._mapper(config, {"fields": {"u_dept": "user.department"}})
        user = EntraUser(object_id="x", department="Platform Engineering")
        values = mapper.build_values(make_device(), entra_user=user)
        assert values["u_dept"] == "Platform Engineering"

    def test_drop_removes_a_default_field(self, config: Config):
        mapper = self._mapper(config, {"drop": ["disk_space"]})
        assert "disk_space" not in mapper.build_values(make_device())

    def test_os_name_override(self, config: Config):
        mapper = self._mapper(config, {"os_names": {"macos": "Apple macOS"}})
        assert mapper.build_values(make_device())["os"] == "Apple macOS"
