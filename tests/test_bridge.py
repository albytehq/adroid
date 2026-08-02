"""Tests for the bridge layer — Mock bridge + LocalBlobStore + ADB parsing."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from adroid.bridge.adb import AdbBridge, LocalBlobStore
from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.contract.errors import DeviceNotAvailableError
from adroid.contract.types import AppInfo, DeviceId, DeviceInfo, DeviceState


# ---------------------------------------------------------------------------
# InMemoryBlobStore
# ---------------------------------------------------------------------------


def test_in_memory_blob_store_put_get_roundtrip():
    store = InMemoryBlobStore()
    data = b"hello adroid"
    ref = store.put(data)
    assert ref == hashlib.sha256(data).hexdigest()
    assert store.get(ref) == data
    assert store.exists(ref) is True
    assert store.exists("0" * 64) is False


def test_in_memory_blob_store_dedupes_identical_data():
    store = InMemoryBlobStore()
    ref1 = store.put(b"same")
    ref2 = store.put(b"same")
    assert ref1 == ref2


# ---------------------------------------------------------------------------
# LocalBlobStore
# ---------------------------------------------------------------------------


def test_local_blob_store_persists_to_disk(tmp_path):
    store = LocalBlobStore(tmp_path / "blobs")
    data = b"persistent bytes"
    ref = store.put(data)

    # New store instance over the same dir should see the blob
    store2 = LocalBlobStore(tmp_path / "blobs")
    assert store2.get(ref) == data
    assert store2.exists(ref) is True


def test_local_blob_store_shards_by_hash_prefix(tmp_path):
    store = LocalBlobStore(tmp_path / "blobs")
    ref = store.put(b"shard test")
    # File should live under <root>/<aa>/<bb>/<full-hash>
    path = tmp_path / "blobs" / ref[:2] / ref[2:4] / ref
    assert path.exists()


# ---------------------------------------------------------------------------
# MockBridge
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bridge():
    blob_store = InMemoryBlobStore()
    bridge = MockBridge(blob_store=blob_store)
    bridge.add_device(
        DeviceInfo(
            id=DeviceId(kind="adb", value="mock-1"),
            state=DeviceState.ONLINE,
            model="MockPhone",
            manufacturer="Adroid",
        ),
        apps=[
            AppInfo(package_name="com.example.app", version_name="1.0"),
            AppInfo(package_name="com.android.settings", system_app=True),
        ],
    )
    return bridge


def test_mock_bridge_lists_added_devices(mock_bridge):
    devices = mock_bridge.list_devices()
    assert len(devices) == 1
    assert devices[0].id.value == "mock-1"
    assert devices[0].state == DeviceState.ONLINE


def test_mock_bridge_get_device_info(mock_bridge):
    did = DeviceId(kind="adb", value="mock-1")
    info = mock_bridge.get_device_info(did)
    assert info.model == "MockPhone"


def test_mock_bridge_raises_on_missing_device(mock_bridge):
    did = DeviceId(kind="adb", value="nope")
    with pytest.raises(DeviceNotAvailableError) as exc:
        mock_bridge.get_device_info(did)
    assert exc.value.code == "adroid.bridge.mock_device_missing"


def test_mock_bridge_lists_apps(mock_bridge):
    did = DeviceId(kind="adb", value="mock-1")
    apps = mock_bridge.list_installed_apps(did)
    assert len(apps) == 2
    assert any(a.package_name == "com.example.app" for a in apps)


def test_mock_bridge_captures_screenshot(mock_bridge):
    did = DeviceId(kind="adb", value="mock-1")
    shot = mock_bridge.capture_screenshot(did)
    assert shot.device_id == did
    assert len(shot.blob_ref) == 64  # sha256 hex
    assert shot.width == 1
    assert shot.height == 1


def test_mock_bridge_remove_device(mock_bridge):
    did = DeviceId(kind="adb", value="mock-1")
    mock_bridge.remove_device(did)
    assert mock_bridge.list_devices() == []


# ---------------------------------------------------------------------------
# AdbBridge parsing logic — unit-test the pure parsers without shelling out
# ---------------------------------------------------------------------------


def test_adb_bridge_parses_devices_output():
    raw = (
        "List of devices attached\n"
        "emulator-5554    device product:sdk_gphone64 model:sdk_gphone64 device:emu64x transport_id:1\n"
        "R5CR30XXXXX      unauthorized transport_id:2\n"
        "\n"
    )
    devices = AdbBridge._parse_devices_output(raw)  # type: ignore[attr-defined]
    assert len(devices) == 2
    assert devices[0].id.value == "emulator-5554"
    assert devices[0].state == DeviceState.ONLINE
    assert devices[0].model == "sdk_gphone64"
    assert devices[1].id.value == "R5CR30XXXXX"
    assert devices[1].state == DeviceState.UNAUTHORIZED


def test_adb_bridge_parses_packages_output():
    raw = (
        "package:com.example.app\n"
        "package:com.android.settings\n"
        "package:io.adroid.runtime\n"
        "\n"
    )
    apps = AdbBridge._parse_packages_output(raw, system_flag=False)  # type: ignore[attr-defined]
    assert len(apps) == 3
    assert apps[0].package_name == "com.example.app"
    assert apps[2].package_name == "io.adroid.runtime"


def test_adb_bridge_parses_png_dimensions():
    # Build a minimal PNG with width=2, height=3
    png = b"\x89PNG\r\n\x1a\n"
    png += b"\x00\x00\x00\rIHDR"
    png += (2).to_bytes(4, "big")  # width
    png += (3).to_bytes(4, "big")  # height
    png += b"\x08\x02\x00\x00\x00"
    png += b"\x00" * 4  # CRC placeholder

    width, height = AdbBridge._parse_png_dimensions(png)  # type: ignore[attr-defined]
    assert width == 2
    assert height == 3


def test_adb_bridge_rejects_kind_mismatch():
    bridge = AdbBridge(blob_store=InMemoryBlobStore())
    did = DeviceId(kind="emulator", value="x")
    with pytest.raises(DeviceNotAvailableError) as exc:
        bridge.get_device_info(did)
    assert exc.value.code == "adroid.bridge.kind_mismatch"
