"""End-to-end runs with both APIs mocked at the HTTP boundary."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from intune_cmdb_sync.config import Config
from intune_cmdb_sync.graph import GraphClient
from intune_cmdb_sync.servicenow.client import ServiceNowClient
from intune_cmdb_sync.state import SyncState
from intune_cmdb_sync.storage import LocalStateStore
from intune_cmdb_sync.sync import SyncRunner

from .conftest import make_device

GRAPH = "https://graph.microsoft.com/v1.0"
DEVICES = f"{GRAPH}/deviceManagement/managedDevices"
SNOW = "https://acme.service-now.com"
IRE = f"{SNOW}/api/now/identifyreconcile"
TABLE = f"{SNOW}/api/now/table"


def mock_snow_plumbing(*, sys_user_rows=None, company_rows=None, model_rows=None) -> None:
    """Stub the supporting Table API reads every run performs."""
    respx.get(f"{TABLE}/sys_properties").mock(
        return_value=httpx.Response(200, json={"result": [{"name": "instance_name",
                                                          "value": "acme"}]})
    )
    respx.get(f"{TABLE}/sys_user").mock(
        return_value=httpx.Response(200, json={"result": sys_user_rows or []})
    )
    respx.get(f"{TABLE}/core_company").mock(
        return_value=httpx.Response(200, json={"result": company_rows or []})
    )
    respx.get(f"{TABLE}/cmdb_model").mock(
        return_value=httpx.Response(200, json={"result": model_rows or []})
    )


def ire_response(*operations: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "result": {
                "items": [
                    {"className": "cmdb_ci_computer", "operation": op, "sysId": f"sys-{i}"}
                    for i, op in enumerate(operations)
                ]
            }
        },
    )


@pytest.fixture
def runner_factory(config: Config):
    def _make(cfg: Config | None = None) -> SyncRunner:
        cfg = cfg or config
        graph = GraphClient(cfg.graph, token_provider=lambda: "graph-token")
        snow = ServiceNowClient(cfg.servicenow)
        snow.auth._token = "snow-token"
        snow.auth._expires_at = float("inf")
        return SyncRunner(cfg, graph=graph, snow=snow)

    return _make


class TestHappyPath:
    @respx.mock
    def test_writes_corporate_devices_and_reports(self, runner_factory):
        respx.get(DEVICES).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        make_device(id="d1", deviceName="MAC-1"),
                        make_device(id="d2", deviceName="WIN-1", operatingSystem="Windows"),
                    ]
                },
            )
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(
                200,
                json={
                    "responses": [
                        {
                            "id": "0",
                            "status": 200,
                            "body": {
                                "id": "99999999-9999-9999-9999-999999999999",
                                "mail": "lou@example.com",
                                "employeeId": "E4242",
                            },
                        }
                    ]
                },
            )
        )
        mock_snow_plumbing(
            sys_user_rows=[
                {"sys_id": "snow-user-1", "user_name": "lou", "email": "lou@example.com",
                 "employee_number": "E4242", "active": "true"}
            ],
            company_rows=[{"sys_id": "mfr-apple", "name": "Apple"}],
        )
        ire = respx.post(IRE).mock(return_value=ire_response("INSERT", "UPDATE"))

        report = runner_factory().run()

        assert report.devices_fetched == 2
        assert report.devices_after_ownership_filter == 2
        assert report.inserted == 1
        assert report.updated == 1
        assert report.errors == 0
        assert report.users_resolved == 1

        body = json.loads(ire.calls[0].request.read())
        assert len(body["items"]) == 2
        first = body["items"][0]
        assert first["sys_object_source_info"]["source_native_key"] == "d1"
        assert first["values"]["assigned_to"] == "snow-user-1"
        assert first["values"]["manufacturer"] == "mfr-apple"

    @respx.mock
    def test_personal_devices_are_excluded(self, runner_factory):
        respx.get(DEVICES).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        make_device(id="d1"),
                        make_device(id="d2", managedDeviceOwnerType="personal"),
                        make_device(id="d3", managedDeviceOwnerType="unknown"),
                    ]
                },
            )
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        ire = respx.post(IRE).mock(return_value=ire_response("INSERT"))

        report = runner_factory().run()

        assert report.devices_fetched == 3
        assert report.devices_after_ownership_filter == 1
        assert len(json.loads(ire.calls[0].request.read())["items"]) == 1

    @respx.mock
    def test_batches_respect_snow_batch_size(self, set_env, runner_factory):
        set_env(SNOW_BATCH_SIZE="2")
        cfg = Config.from_env()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(
                200, json={"value": [make_device(id=f"d{i}") for i in range(5)]}
            )
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        ire = respx.post(IRE).mock(
            side_effect=[
                ire_response("INSERT", "INSERT"),
                ire_response("INSERT", "INSERT"),
                ire_response("INSERT"),
            ]
        )
        report = runner_factory(cfg).run()
        assert ire.call_count == 3
        assert report.inserted == 5


class TestSkips:
    @respx.mock
    def test_unmapped_os_is_skipped_not_written(self, runner_factory):
        respx.get(DEVICES).mock(
            return_value=httpx.Response(
                200,
                json={"value": [make_device(id="d1", operatingSystem="iOS")]},
            )
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        ire = respx.post(IRE)

        report = runner_factory().run()
        assert report.devices_skipped_no_class == 1
        assert ire.call_count == 0

    @respx.mock
    def test_device_with_no_serial_or_name_is_skipped(self, runner_factory):
        respx.get(DEVICES).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        make_device(
                            id="d1",
                            deviceName="",
                            managedDeviceName="",
                            serialNumber="To be filled by O.E.M.",
                        )
                    ]
                },
            )
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        ire = respx.post(IRE)

        report = runner_factory().run()
        assert report.devices_skipped_no_identifier == 1
        assert ire.call_count == 0

    @respx.mock
    def test_empty_tenant_finishes_cleanly(self, runner_factory):
        respx.get(DEVICES).mock(return_value=httpx.Response(200, json={"value": []}))
        mock_snow_plumbing()
        report = runner_factory().run()
        assert report.devices_fetched == 0
        assert report.finished_at is not None


class TestFailureHandling:
    @respx.mock
    def test_batch_failure_marks_every_device_in_it(self, runner_factory):
        respx.get(DEVICES).mock(
            return_value=httpx.Response(
                200, json={"value": [make_device(id="d1"), make_device(id="d2")]}
            )
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=httpx.Response(403, text="itil role required"))

        report = runner_factory().run()
        assert report.errors == 2
        assert report.inserted == 0
        assert any("itil role" in sample for sample in report.error_samples)

    @respx.mock
    def test_unmatched_user_still_writes_the_ci(self, runner_factory):
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(
                200,
                json={
                    "responses": [
                        {"id": "0", "status": 200,
                         "body": {"id": "99999999-9999-9999-9999-999999999999",
                                  "mail": "ghost@example.com"}}
                    ]
                },
            )
        )
        mock_snow_plumbing(sys_user_rows=[])
        ire = respx.post(IRE).mock(return_value=ire_response("INSERT"))

        report = runner_factory().run()
        assert report.inserted == 1
        assert report.users_unresolved == 1
        assert "assigned_to" not in json.loads(ire.calls[0].request.read())["items"][0]["values"]


class TestDryRun:
    @respx.mock
    def test_uses_query_endpoint_and_writes_no_state(self, set_env, runner_factory, tmp_path):
        state_path = tmp_path / "state.json"
        set_env(DRY_RUN="true", STATE_PATH=str(state_path))
        cfg = Config.from_env()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        commit = respx.post(IRE)
        query = respx.post(f"{IRE}/query").mock(return_value=ire_response("INSERT"))

        report = runner_factory(cfg).run()

        assert query.call_count == 1
        assert commit.call_count == 0
        assert report.dry_run is True
        assert report.inserted == 1  # what *would* have happened
        assert not state_path.exists()


class TestRetirement:
    def _seed_state(self, path, *, count: int) -> None:
        state = SyncState()
        for i in range(count):
            state.observe(
                f"old-{i}", sys_id=f"sys-old-{i}", name=f"OLD-{i}",
                class_name="cmdb_ci_computer",
            )
        state.observe("d1", sys_id="sys-0", name="LOU-MBP16", class_name="cmdb_ci_computer")
        state.save(LocalStateStore(str(path)))

    @respx.mock
    def test_retires_devices_absent_from_intune(self, set_env, runner_factory, tmp_path):
        state_path = tmp_path / "state.json"
        self._seed_state(state_path, count=1)  # 1 stale + 1 still present = 50%... too high
        set_env(
            SNOW_RETIRE_MISSING="true",
            STATE_PATH=str(state_path),
            SNOW_RETIRE_MAX_FRACTION="0.6",
        )
        cfg = Config.from_env()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=ire_response("UPDATE"))
        patch = respx.patch(f"{TABLE}/cmdb_ci_computer/sys-old-0").mock(
            return_value=httpx.Response(200, json={"result": {"sys_id": "sys-old-0"}})
        )

        report = runner_factory(cfg).run()

        assert report.retired == 1
        assert json.loads(patch.calls[0].request.read())["install_status"] == "7"
        assert "old-0" not in SyncState.load(LocalStateStore(str(state_path))).devices

    @respx.mock
    def test_mass_retirement_guard_blocks_a_bad_run(self, set_env, runner_factory, tmp_path):
        state_path = tmp_path / "state.json"
        self._seed_state(state_path, count=50)
        set_env(
            SNOW_RETIRE_MISSING="true",
            STATE_PATH=str(state_path),
            SNOW_RETIRE_MAX_FRACTION="0.10",
        )
        cfg = Config.from_env()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=ire_response("UPDATE"))
        patch = respx.patch(url__startswith=f"{TABLE}/cmdb_ci_computer/")

        report = runner_factory(cfg).run()

        assert report.retired == 0
        assert patch.call_count == 0
        assert any("refusing to retire" in w for w in report.warnings)
        # The guard tripping means the run did not do its whole job, so it has
        # to be visible to the scheduler and not merely logged.
        assert any("refusing to retire" in d for d in report.degraded)

    @respx.mock
    def test_retirement_off_by_default(self, set_env, runner_factory, tmp_path):
        state_path = tmp_path / "state.json"
        self._seed_state(state_path, count=1)
        set_env(STATE_PATH=str(state_path))
        cfg = Config.from_env()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=ire_response("UPDATE"))
        patch = respx.patch(url__startswith=f"{TABLE}/cmdb_ci_computer/")

        assert runner_factory(cfg).run().retired == 0
        assert patch.call_count == 0


class TestHardwareDetail:
    @respx.mock
    def test_disabled_by_default(self, runner_factory):
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=ire_response("INSERT"))
        detail = respx.get(f"{DEVICES}/d1")

        runner_factory().run()
        assert detail.call_count == 0

    @respx.mock
    def test_enriches_ram_when_enabled(self, set_env, runner_factory):
        set_env(INTUNE_FETCH_HARDWARE_DETAIL="true")
        cfg = Config.from_env()
        respx.get(f"{DEVICES}/d1").mock(
            return_value=httpx.Response(
                200, json={"id": "d1", "physicalMemoryInBytes": 68719476736}
            )
        )
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        ire = respx.post(IRE).mock(return_value=ire_response("INSERT"))

        runner_factory(cfg).run()
        values = json.loads(ire.calls[0].request.read())["items"][0]["values"]
        assert values["ram"] == 65536


class TestDryRunUnderCmdbInstance:
    @respx.mock
    def test_predicts_outcomes_but_labels_them_a_prediction(self, set_env, runner_factory):
        set_env(DRY_RUN="true", SNOW_WRITE_MODE="cmdb_instance")
        cfg = Config.from_env()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.get(f"{SNOW}/api/now/table/cmdb_ci_computer").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
        write = respx.post(f"{SNOW}/api/now/cmdb/instance/cmdb_ci_computer")

        report = runner_factory(cfg).run()

        assert write.call_count == 0
        # The outcome is now predicted rather than skipped, but the report has
        # to keep saying that a prediction is not a simulation.
        assert report.outcomes[0].action == "inserted"
        assert any("prediction, not a simulation" in w for w in report.warnings)


class TestTwoHundredDevicesThroughCmdbInstance:
    """The run this mode exists for on an instance that refuses the IRE API:
    a bounded first write, one POST per CI, no retirement."""

    def _devices(self, count: int) -> list[dict]:
        return [
            make_device(
                id=f"d{i}",
                deviceName=f"HOST-{i}",
                serialNumber=f"SN{i:05d}",
            )
            for i in range(count)
        ]

    @respx.mock
    def test_writes_two_hundred_cis_one_request_each(self, set_env, runner_factory):
        set_env(SNOW_WRITE_MODE="cmdb_instance", INTUNE_DEVICE_LIMIT="200")
        cfg = Config.from_env()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": self._devices(250)})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        write = respx.post(f"{SNOW}/api/now/cmdb/instance/cmdb_ci_computer").mock(
            return_value=httpx.Response(
                201,
                json={"result": {"attributes": {
                    "sys_id": "ci-1", "sys_created_on": "x", "sys_updated_on": "x"}}},
            )
        )

        report = runner_factory(cfg).run()

        # One request per CI -- the defining property of this write mode, and
        # what SNOW_BATCH_SIZE does *not* change here.
        assert write.call_count == 200
        assert report.inserted == 200
        assert report.errors == 0
        # The limit stopped Graph short rather than fetching all 250 and
        # discarding 50.
        assert report.devices_after_ownership_filter == 200
        assert not report.degraded

    @respx.mock
    def test_the_limit_keeps_retirement_off(self, set_env, runner_factory, tmp_path):
        """200 of a larger fleet makes every other device look like it vanished."""
        set_env(
            SNOW_WRITE_MODE="cmdb_instance",
            INTUNE_DEVICE_LIMIT="200",
            SNOW_RETIRE_MISSING="true",
            STATE_PATH=str(tmp_path / "state.json"),
        )
        cfg = Config.from_env()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": self._devices(200)})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(f"{SNOW}/api/now/cmdb/instance/cmdb_ci_computer").mock(
            return_value=httpx.Response(
                201,
                json={"result": {"attributes": {
                    "sys_id": "ci-1", "sys_created_on": "x", "sys_updated_on": "x"}}},
            )
        )
        retire = respx.patch(url__startswith=f"{TABLE}/")

        report = runner_factory(cfg).run()

        assert retire.call_count == 0
        assert report.retired == 0
        assert any("retirement skipped" in w for w in report.warnings)

    @respx.mock
    def test_a_systematic_failure_stops_early_and_degrades_the_run(
        self, set_env, runner_factory
    ):
        """Without the guard this is 200 identical failed POSTs at a production
        instance, and an exit code that says only "some devices failed"."""
        set_env(SNOW_WRITE_MODE="cmdb_instance", INTUNE_DEVICE_LIMIT="200")
        cfg = Config.from_env()
        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": self._devices(200)})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        write = respx.post(f"{SNOW}/api/now/cmdb/instance/cmdb_ci_computer").mock(
            return_value=httpx.Response(400, json={"error": {"message": "Invalid data source"}})
        )

        report = runner_factory(cfg).run()

        assert write.call_count < 30
        assert report.errors == 200
        # Degraded, not merely "some devices failed": the fleet was not covered.
        assert report.degraded


class TestStatePersistenceFailure:
    """CMDB writes succeeding while the state file is lost is a partial success,
    not a clean run: the next run cannot retire anything it can no longer see."""

    @respx.mock
    def test_unwritable_state_degrades_the_report(self, set_env, runner_factory, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("i am a file, not a directory")
        set_env(STATE_PATH=str(blocker / "nested" / "state.json"))
        cfg = Config.from_env()

        respx.get(DEVICES).mock(
            return_value=httpx.Response(200, json={"value": [make_device(id="d1")]})
        )
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=ire_response("INSERT"))

        report = runner_factory(cfg).run()

        # The device still got written.
        assert report.inserted == 1
        # But the run is flagged.
        assert any("could not persist sync state" in d for d in report.degraded)
        assert any("could not persist sync state" in w for w in report.warnings)


class TestDeviceLimit:
    """`--limit` / INTUNE_DEVICE_LIMIT caps a run so a first write against a real
    instance is small enough to inspect by hand."""

    @respx.mock
    def test_limit_caps_processed_devices(self, set_env, runner_factory):
        set_env(INTUNE_DEVICE_LIMIT="2")
        cfg = Config.from_env()
        respx.get(DEVICES).mock(return_value=httpx.Response(200, json={
            "value": [make_device(id=f"d{i}", serialNumber=f"SER{i}") for i in range(10)]
        }))
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        ire = respx.post(IRE).mock(return_value=ire_response("INSERT", "INSERT"))

        report = runner_factory(cfg).run()

        assert report.devices_after_ownership_filter == 2
        assert report.inserted == 2
        submitted = json.loads(ire.calls[0].request.read())["items"]
        assert len(submitted) == 2

    @respx.mock
    def test_limit_is_reported_so_the_run_is_not_mistaken_for_a_full_pass(
        self, set_env, runner_factory
    ):
        set_env(INTUNE_DEVICE_LIMIT="1")
        cfg = Config.from_env()
        respx.get(DEVICES).mock(return_value=httpx.Response(200, json={
            "value": [make_device(id=f"d{i}", serialNumber=f"SER{i}") for i in range(5)]
        }))
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=ire_response("INSERT"))

        report = runner_factory(cfg).run()
        assert any("not a full inventory pass" in w for w in report.warnings)

    @respx.mock
    def test_limit_disables_retirement(self, set_env, runner_factory, tmp_path):
        """The critical interaction: a truncated device list makes the rest of the
        fleet look like it vanished. Retiring against that would be catastrophic,
        and a small limit makes the missing fraction large enough that the
        percentage guard alone is not a reliable backstop."""
        state_path = tmp_path / "state.json"
        seeded = SyncState()
        for i in range(50):
            seeded.observe(f"old-{i}", sys_id=f"sys-old-{i}", name=f"OLD-{i}",
                           class_name="cmdb_ci_computer")
        seeded.save(LocalStateStore(str(state_path)))
        set_env(
            SNOW_RETIRE_MISSING="true",
            STATE_PATH=str(state_path),
            SNOW_RETIRE_MAX_FRACTION="1.0",   # guard wide open on purpose
            INTUNE_DEVICE_LIMIT="1",
        )
        cfg = Config.from_env()
        respx.get(DEVICES).mock(return_value=httpx.Response(200, json={
            "value": [make_device(id=f"d{i}") for i in range(5)]
        }))
        respx.post(f"{GRAPH}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": []})
        )
        mock_snow_plumbing()
        respx.post(IRE).mock(return_value=ire_response("UPDATE"))
        patch = respx.patch(url__startswith=f"{TABLE}/cmdb_ci_computer/")

        report = runner_factory(cfg).run()

        assert patch.call_count == 0
        assert report.retired == 0
        assert any("retirement skipped" in w for w in report.warnings)
