from __future__ import annotations

import json
from pathlib import Path

import pytest

from intune_cmdb_sync.state import SyncState
from intune_cmdb_sync.storage import LocalStateStore, build_state_store


def store(path: Path) -> LocalStateStore:
    return LocalStateStore(str(path))


class TestRoundTrip:
    def test_save_then_load(self, tmp_path):
        path = tmp_path / "nested" / "state.json"
        state = SyncState()
        state.observe("intune-1", sys_id="sys-1", name="LOU-MBP16", class_name="cmdb_ci_computer")
        state.save(store(path))

        loaded = SyncState.load(store(path))
        assert loaded.devices["intune-1"]["sys_id"] == "sys-1"
        assert loaded.devices["intune-1"]["class_name"] == "cmdb_ci_computer"
        assert loaded.last_run_at is not None

    def test_missing_file_gives_empty_state(self, tmp_path):
        assert SyncState.load(store(tmp_path / "absent.json")).devices == {}

    def test_no_path_is_a_no_op(self):
        state = SyncState()
        state.observe("a", sys_id="s", name="n")
        state.save(None)  # must not raise
        assert SyncState.load(None).devices == {}

    def test_corrupt_file_degrades_to_empty_state(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{ this is not json")
        assert SyncState.load(store(path)).devices == {}

    def test_save_is_atomic_leaving_no_temp_files(self, tmp_path):
        path = tmp_path / "state.json"
        state = SyncState()
        state.observe("a", sys_id="s", name="n")
        state.save(store(path))
        state.save(store(path))
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_observe_updates_without_losing_sys_id(self, tmp_path):
        state = SyncState()
        state.observe("a", sys_id="sys-1", name="old-name", class_name="cmdb_ci_computer")
        state.observe("a", sys_id=None, name="new-name")
        assert state.devices["a"]["sys_id"] == "sys-1"
        assert state.devices["a"]["name"] == "new-name"


class TestMissingDetection:
    def _state(self) -> SyncState:
        state = SyncState()
        state.observe("keep", sys_id="sys-keep", name="Keep")
        state.observe("gone", sys_id="sys-gone", name="Gone")
        state.observe("never-written", sys_id=None, name="NoCI")
        return state

    def test_reports_only_devices_with_a_known_ci(self):
        missing = self._state().missing_since_last_run({"keep"})
        assert set(missing) == {"gone"}

    def test_nothing_missing_when_all_present(self):
        assert self._state().missing_since_last_run({"keep", "gone", "never-written"}) == {}

    def test_forget_removes_entries(self):
        state = self._state()
        state.forget({"gone"})
        assert "gone" not in state.devices
        assert "keep" in state.devices


def test_written_file_is_readable_json(tmp_path):
    path = tmp_path / "state.json"
    state = SyncState()
    state.observe("a", sys_id="s", name="n")
    state.save(store(path))
    payload = json.loads(path.read_text())
    assert payload["version"] == 1
    assert "devices" in payload


class TestStoreSelection:
    def test_local_path_gives_a_filesystem_store(self):
        selected = build_state_store("/mnt/state/state.json")
        assert isinstance(selected, LocalStateStore)

    def test_s3_url_gives_an_s3_store(self):
        from intune_cmdb_sync.storage import S3StateStore

        selected = build_state_store("s3://my-bucket/intune/state.json")
        assert isinstance(selected, S3StateStore)
        assert selected._bucket == "my-bucket"
        assert selected._key == "intune/state.json"

    def test_s3_url_without_a_key_is_rejected(self):
        import pytest

        with pytest.raises(ValueError, match=r"s3://bucket/key\.json"):
            build_state_store("s3://my-bucket")

    def test_no_location_gives_no_store(self):
        assert build_state_store(None) is None
        assert build_state_store("") is None

    def test_s3_backend_requires_boto3(self, monkeypatch):
        import builtins

        import pytest

        from intune_cmdb_sync.storage import S3StateStore

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "boto3":
                raise ImportError("no boto3 here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        with pytest.raises(RuntimeError, match=r"intune-cmdb-sync\[aws\]"):
            S3StateStore("s3://b/k.json")._client()


class TestWriteFailureIsLoud:
    """A state file that cannot be written must raise rather than log-and-continue:
    a silent failure leaves the run looking clean while the next one starts from
    empty state and quietly stops retiring."""

    def test_unwritable_path_raises(self, tmp_path):
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("i am a file")
        state = SyncState()
        state.observe("intune-1", sys_id="sys-1", name="LOU-MBP16")
        with pytest.raises(OSError):
            state.save(store(blocker / "nested" / "state.json"))

    def test_no_temp_file_is_left_behind(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.mkdir()
        (blocker / "state.json").mkdir()  # a directory where the file should go
        state = SyncState()
        state.observe("intune-1", sys_id="sys-1", name="LOU-MBP16")
        with pytest.raises(OSError):
            state.save(store(blocker / "state.json"))
        assert [p.name for p in blocker.iterdir()] == ["state.json"]
