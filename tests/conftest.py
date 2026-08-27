from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from intune_cmdb_sync.config import Config

BASE_ENV = {
    "GRAPH_TENANT_ID": "11111111-1111-1111-1111-111111111111",
    "GRAPH_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
    "GRAPH_CLIENT_SECRET": "shhh",
    "SNOW_INSTANCE": "acme",
    "SNOW_CLIENT_ID": "snow-client",
    "SNOW_CLIENT_SECRET": "snow-secret",
}

# Every environment variable the connector reads, so a stray value in the
# developer's shell can never change a test result.
_MANAGED_PREFIXES = ("GRAPH_", "SNOW_", "INTUNE_", "AZURE_", "LOG_", "SERIAL_")
_MANAGED_EXACT = ("DRY_RUN", "RUN_REPORT_PATH", "STATE_PATH", "MAPPING_OVERRIDES_FILE")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ):
        if key.startswith(_MANAGED_PREFIXES) or key in _MANAGED_EXACT:
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def set_env(monkeypatch: pytest.MonkeyPatch):
    def _set(**overrides: str) -> None:
        for key, value in {**BASE_ENV, **overrides}.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)

    return _set


@pytest.fixture
def config(set_env) -> Config:
    set_env()
    return Config.from_env()


def make_device(**overrides: Any) -> dict[str, Any]:
    """A representative corporate-owned macOS device from Graph."""
    device = {
        "id": "705c034c-034c-705c-4c03-5c704c035c70",
        "deviceName": "LOU-MBP16",
        "managedDeviceName": "lou_MacMDM_1/1/2026",
        "managedDeviceOwnerType": "company",
        "operatingSystem": "macOS",
        "osVersion": "15.3.1",
        "manufacturer": "Apple",
        "model": "MacBook Pro (16-inch, 2023)",
        "serialNumber": "C02XY1Z2ABCD",
        "wiFiMacAddress": "A4B1C2D3E4F5",
        "complianceState": "compliant",
        "managementAgent": "mdm",
        "enrolledDateTime": "2025-04-02T10:15:30.1234567Z",
        "lastSyncDateTime": "2026-08-25T06:11:02.7654321Z",
        "isEncrypted": True,
        "azureADDeviceId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "totalStorageSpaceInBytes": 994662584320,
        "freeStorageSpaceInBytes": 412316860416,
        "userId": "99999999-9999-9999-9999-999999999999",
        "userPrincipalName": "lou@example.com",
        "userDisplayName": "Lou Simonetti",
        "emailAddress": "lou@example.com",
    }
    device.update(overrides)
    return device
