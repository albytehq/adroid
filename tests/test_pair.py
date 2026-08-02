"""Tests for the adroid-pair CLI helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from adroid.pair import IP_PORT_RE, list_devices, print_devices


# ---------------------------------------------------------------------------
# IP_PORT_RE — wireless address validation
# ---------------------------------------------------------------------------


def test_ip_port_re_matches_valid_wireless_addr():
    assert IP_PORT_RE.match("192.168.1.42:5555")
    assert IP_PORT_RE.match("10.0.0.1:4321")
    assert IP_PORT_RE.match("172.16.0.1:65535")


def test_ip_port_re_rejects_missing_port():
    assert not IP_PORT_RE.match("192.168.1.42")


def test_ip_port_re_rejects_non_numeric_port():
    assert not IP_PORT_RE.match("192.168.1.42:abc")


def test_ip_port_re_rejects_non_ip():
    assert not IP_PORT_RE.match("not-an-ip:5555")


def test_ip_port_re_rejects_localhost_name():
    assert not IP_PORT_RE.match("localhost:5555")


# ---------------------------------------------------------------------------
# list_devices — parses adb devices -l output
# ---------------------------------------------------------------------------


def _mock_adb_output(stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_list_devices_parses_usb_device():
    out = b"List of devices attached\nR5CR30XXXXX       device product:foo model:Pixel8 device:bar transport_id:1\n"
    with patch("adroid.pair.subprocess.run", return_value=_mock_adb_output(out)):
        devices = list_devices("fake-adb")
    assert len(devices) == 1
    assert devices[0]["serial"] == "R5CR30XXXXX"
    assert devices[0]["type"] == "usb"
    assert devices[0]["state"] == "device"
    assert devices[0]["model"] == "Pixel8"


def test_list_devices_parses_wireless_device():
    out = b"List of devices attached\n192.168.1.42:5555 device product:foo model:Pixel8 device:bar transport_id:2\n"
    with patch("adroid.pair.subprocess.run", return_value=_mock_adb_output(out)):
        devices = list_devices("fake-adb")
    assert len(devices) == 1
    assert devices[0]["serial"] == "192.168.1.42:5555"
    assert devices[0]["type"] == "wireless"


def test_list_devices_parses_multiple_mixed():
    out = b"""List of devices attached
R5CR30XXXXX       device product:foo model:Pixel8 device:bar transport_id:1
192.168.1.42:5555 device product:foo model:Galaxy device:baz transport_id:2
emulator-5554     device product:sdk model:sdk_gphone64 transport_id:3
"""
    with patch("adroid.pair.subprocess.run", return_value=_mock_adb_output(out)):
        devices = list_devices("fake-adb")
    assert len(devices) == 3
    assert devices[0]["type"] == "usb"
    assert devices[1]["type"] == "wireless"
    assert devices[2]["type"] == "usb"  # emulator-5554 is not IP:PORT format


def test_list_devices_parses_unauthorized_state():
    out = b"List of devices attached\nR5CR30XXXXX       unauthorized transport_id:1\n"
    with patch("adroid.pair.subprocess.run", return_value=_mock_adb_output(out)):
        devices = list_devices("fake-adb")
    assert len(devices) == 1
    assert devices[0]["state"] == "unauthorized"


def test_list_devices_parses_offline_state():
    out = b"List of devices attached\nR5CR30XXXXX       offline transport_id:1\n"
    with patch("adroid.pair.subprocess.run", return_value=_mock_adb_output(out)):
        devices = list_devices("fake-adb")
    assert len(devices) == 1
    assert devices[0]["state"] == "offline"


def test_list_devices_empty_output():
    out = b"List of devices attached\n\n"
    with patch("adroid.pair.subprocess.run", return_value=_mock_adb_output(out)):
        devices = list_devices("fake-adb")
    assert devices == []


def test_list_devices_handles_blank_lines():
    out = b"List of devices attached\n\nR5CR30XXXXX       device transport_id:1\n\n"
    with patch("adroid.pair.subprocess.run", return_value=_mock_adb_output(out)):
        devices = list_devices("fake-adb")
    assert len(devices) == 1


# ---------------------------------------------------------------------------
# print_devices — formatting (just smoke test, no assertions on output)
# ---------------------------------------------------------------------------


def test_print_devices_empty(capsys):
    print_devices([])
    captured = capsys.readouterr()
    assert "no devices" in captured.out


def test_print_devices_with_items(capsys):
    devices = [
        {"serial": "R5CR30X", "state": "device", "type": "usb", "model": "Pixel8", "transport_id": "1"},
        {"serial": "192.168.1.42:5555", "state": "device", "type": "wireless", "model": "Galaxy", "transport_id": "2"},
    ]
    print_devices(devices)
    captured = capsys.readouterr()
    assert "R5CR30X" in captured.out
    assert "192.168.1.42:5555" in captured.out
    assert "usb" in captured.out
    assert "wireless" in captured.out
