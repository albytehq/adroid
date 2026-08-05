"""Tests for the contract layer — typed primitives that define the surface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from adroid.contract.types import (
    AppInfo,
    AuditEvent,
    AuditOutcome,
    Capability,
    CapabilityGrant,
    CapabilityToken,
    DeviceId,
    DeviceInfo,
    DeviceState,
    Screenshot,
    Scope,
    ToolSpec,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def test_capability_values_are_stable_strings():
    """Capability string values are part of the public contract — they
    cannot change without a major bump."""
    assert Capability.DEVICE_LIST.value == "device.list"
    assert Capability.DEVICE_OBSERVE.value == "device.observe"
    assert Capability.APP_LAUNCH.value == "app.launch"
    assert Capability.INPUT_TAP.value == "input.tap"
    assert Capability.FS_WRITE.value == "fs.write"


def test_scope_ranking_read_lt_act_lt_admin():
    from adroid.contract.types import _scope_rank
    assert _scope_rank(Scope.READ) < _scope_rank(Scope.ACT) < _scope_rank(Scope.ADMIN)


# ---------------------------------------------------------------------------
# DeviceId
# ---------------------------------------------------------------------------


def test_device_id_construction():
    d = DeviceId(kind="adb", value="emulator-5554")
    assert d.kind == "adb"
    assert d.value == "emulator-5554"
    assert str(d) == "adb:emulator-5554"


def test_device_id_rejects_empty_value():
    with pytest.raises(ValidationError):
        DeviceId(kind="adb", value="")


def test_device_id_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        DeviceId(kind="foobar", value="x")  # type: ignore[arg-type]


def test_device_id_is_frozen():
    d = DeviceId(kind="adb", value="x")
    with pytest.raises(ValidationError):
        d.value = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DeviceInfo
# ---------------------------------------------------------------------------


def test_device_info_defaults_observed_at_to_utc_now():
    d = DeviceInfo(id=DeviceId(kind="adb", value="x"), state=DeviceState.ONLINE)
    assert d.observed_at.tzinfo is not None
    # Within 5 seconds of now
    delta = abs((datetime.now(timezone.utc) - d.observed_at).total_seconds())
    assert delta < 5


def test_device_info_naive_observed_at_rejected():
    with pytest.raises(ValidationError):
        DeviceInfo(
            id=DeviceId(kind="adb", value="x"),
            state=DeviceState.ONLINE,
            observed_at=datetime(2026, 1, 1),  # naive
        )


def test_device_info_battery_range():
    with pytest.raises(ValidationError):
        DeviceInfo(
            id=DeviceId(kind="adb", value="x"),
            state=DeviceState.ONLINE,
            battery_level=150.0,
        )


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


def test_screenshot_blob_ref_must_be_sha256():
    with pytest.raises(ValidationError):
        Screenshot(
            device_id=DeviceId(kind="adb", value="x"),
            blob_ref="abc",  # not 64 hex chars
            width=100,
            height=100,
        )


def test_screenshot_dimensions_must_be_positive():
    with pytest.raises(ValidationError):
        Screenshot(
            device_id=DeviceId(kind="adb", value="x"),
            blob_ref="0" * 64,
            width=0,
            height=100,
        )


# ---------------------------------------------------------------------------
# CapabilityToken
# ---------------------------------------------------------------------------


def _valid_token_kwargs(**overrides):
    base = dict(
        issuer="adroid-test",
        subject="agent:foo",
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        grants=frozenset({CapabilityGrant(capability=Capability.DEVICE_LIST, scope=Scope.READ)}),
        signature="0" * 128,
    )
    base.update(overrides)
    return base


def test_token_expires_at_must_be_after_issued_at():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        CapabilityToken(
            **_valid_token_kwargs(issued_at=now, expires_at=now - timedelta(seconds=1))
        )


def test_token_has_capability_with_exact_scope():
    t = CapabilityToken(**_valid_token_kwargs())
    assert t.has(Capability.DEVICE_LIST, Scope.READ) is True


def test_token_has_capability_with_higher_scope_denies():
    t = CapabilityToken(**_valid_token_kwargs())
    assert t.has(Capability.DEVICE_LIST, Scope.ACT) is False


def test_token_has_capability_no_scope_check():
    t = CapabilityToken(**_valid_token_kwargs())
    assert t.has(Capability.DEVICE_LIST) is True
    assert t.has(Capability.APP_LAUNCH) is False


# ---------------------------------------------------------------------------
# AuditEvent
# ---------------------------------------------------------------------------


def test_audit_event_minimal():
    e = AuditEvent(
        outcome=AuditOutcome.SUCCESS,
        method="runtime.boot",
        args_digest="0" * 64,
    )
    assert e.sequence == 0
    assert e.event_id is not None
    assert e.timestamp.tzinfo is not None


def test_audit_event_outcome_required():
    with pytest.raises(ValidationError):
        AuditEvent(method="runtime.boot", args_digest="0" * 64)  # type: ignore[call-arg]


def test_audit_event_args_digest_must_be_sha256():
    with pytest.raises(ValidationError):
        AuditEvent(
            outcome=AuditOutcome.SUCCESS,
            method="x",
            args_digest="not-a-hash",
        )


# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------


def test_tool_spec_stability_default_is_experimental():
    spec = ToolSpec(
        name="x.y",
        capability=Capability.DEVICE_LIST,
        required_scope=Scope.READ,
        description="",
        args_schema={},
        result_schema={},
    )
    assert spec.stability == "experimental"
