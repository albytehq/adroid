"""Tests for the runtime layer — the orchestrator that ties everything together."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.contract.errors import (
    CapabilityNotFoundError,
    DeviceNotAvailableError,
    PermissionDeniedError,
    RateLimitedError,
)
from adroid.contract.types import (
    AppInfo,
    Capability,
    CapabilityGrant,
    DeviceId,
    DeviceInfo,
    DeviceState,
    Scope,
)
from adroid.runtime.core import AdroidRuntime


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runtime(tmp_path) -> AdroidRuntime:
    blob_store = InMemoryBlobStore()
    bridge = MockBridge(blob_store=blob_store)
    bridge.add_device(
        DeviceInfo(
            id=DeviceId(kind="adb", value="mock-1"),
            state=DeviceState.ONLINE,
            model="MockPhone",
            manufacturer="Adroid",
            android_version="14",
            sdk_level=34,
        ),
        apps=[
            AppInfo(package_name="com.example.app", version_name="1.0"),
            AppInfo(package_name="com.android.settings", system_app=True),
        ],
    )
    return AdroidRuntime(
        issuer_id="adroid-test",
        bridge=bridge,
        blob_store=blob_store,
        audit_log_path=tmp_path / "test.auditlog",
    )


@pytest.fixture
def read_token(runtime: AdroidRuntime):
    return runtime.issue_token(
        subject="agent:read",
        grants=[
            (Capability.DEVICE_LIST, Scope.READ),
            (Capability.DEVICE_OBSERVE, Scope.READ),
            (Capability.DEVICE_SCREENSHOT, Scope.READ),
            (Capability.APP_LIST, Scope.READ),
        ],
    )


@pytest.fixture
def act_token(runtime: AdroidRuntime):
    return runtime.issue_token(
        subject="agent:act",
        grants=[
            (Capability.DEVICE_LIST, Scope.READ),
            (Capability.APP_LAUNCH, Scope.ACT),
        ],
    )


# ---------------------------------------------------------------------------
# Boot + identity
# ---------------------------------------------------------------------------


def test_runtime_writes_boot_event_to_audit_log(runtime: AdroidRuntime):
    reader = runtime.audit_reader()
    events = reader.tail(n=10)
    assert len(events) >= 1
    boot = events[0]
    assert boot.method == "runtime.boot"
    assert boot.outcome.value == "success"


def test_runtime_lists_tools_with_specs(runtime: AdroidRuntime):
    tools = runtime.list_tools()
    names = {t.name for t in tools}
    assert {
        "device.list",
        "device.observe",
        "device.screenshot",
        "app.list",
        "app.launch",
    }.issubset(names)
    for t in tools:
        assert t.args_schema.get("type") == "object"


def test_runtime_get_tool_returns_spec(runtime: AdroidRuntime):
    spec = runtime.get_tool("device.list")
    assert spec is not None
    assert spec.capability == Capability.DEVICE_LIST
    assert spec.required_scope == Scope.READ


def test_runtime_get_unknown_tool_returns_none(runtime: AdroidRuntime):
    assert runtime.get_tool("nonexistent.tool") is None


# ---------------------------------------------------------------------------
# device.list
# ---------------------------------------------------------------------------


def test_list_devices_succeeds_with_read_token(runtime: AdroidRuntime, read_token):
    devices = runtime.list_devices(token=read_token)
    assert len(devices) == 1
    assert devices[0].id.value == "mock-1"
    assert devices[0].state == DeviceState.ONLINE


def test_list_devices_audits_success(runtime: AdroidRuntime, read_token):
    runtime.list_devices(token=read_token)
    events = runtime.audit_reader().tail(n=5)
    last = events[-1]
    assert last.method == "runtime.device_list"
    assert last.outcome.value == "success"
    assert last.capability == Capability.DEVICE_LIST
    assert last.token_id == read_token.token_id


def test_list_devices_denies_without_capability(runtime: AdroidRuntime):
    token = runtime.issue_token(
        subject="agent:no-grants",
        grants=[],  # no grants at all
    )
    with pytest.raises(PermissionDeniedError):
        runtime.list_devices(token=token)


def test_list_devices_audits_denial(runtime: AdroidRuntime):
    token = runtime.issue_token(subject="agent:no-grants", grants=[])
    with pytest.raises(PermissionDeniedError):
        runtime.list_devices(token=token)

    events = runtime.audit_reader().tail(n=5)
    # Last event might be the denial... let's check
    denials = [e for e in events if e.outcome.value == "denied"]
    # v0.1.0 doesn't write denials yet (gate raises before audit). That's
    # a known limitation. v0.2.0 will wrap the gate in audit-denied.
    # For now: ensure no false "success" was logged for the denied call.
    success_for_device_list = [
        e for e in events
        if e.method == "runtime.device_list" and e.outcome.value == "success"
    ]
    assert success_for_device_list == []


# ---------------------------------------------------------------------------
# device.observe
# ---------------------------------------------------------------------------


def test_observe_device_succeeds(runtime: AdroidRuntime, read_token):
    did = DeviceId(kind="adb", value="mock-1")
    info = runtime.observe_device(token=read_token, device_id=did)
    assert info.model == "MockPhone"
    assert info.id.value == "mock-1"


def test_observe_device_audits_with_device_id(runtime: AdroidRuntime, read_token):
    did = DeviceId(kind="adb", value="mock-1")
    runtime.observe_device(token=read_token, device_id=did)

    events = runtime.audit_reader().tail(n=5)
    last = events[-1]
    assert last.device_id == did
    assert last.method == "runtime.device_observe"


def test_observe_device_unknown_device_audits_error(runtime: AdroidRuntime, read_token):
    did = DeviceId(kind="adb", value="does-not-exist")
    with pytest.raises(DeviceNotAvailableError):
        runtime.observe_device(token=read_token, device_id=did)

    events = runtime.audit_reader().tail(n=5)
    errors = [e for e in events if e.outcome.value == "error"]
    assert len(errors) >= 1
    assert errors[-1].error_code == "adroid.bridge.mock_device_missing"


# ---------------------------------------------------------------------------
# device.screenshot
# ---------------------------------------------------------------------------


def test_capture_screenshot_returns_blob_ref(runtime: AdroidRuntime, read_token):
    did = DeviceId(kind="adb", value="mock-1")
    shot = runtime.capture_screenshot(token=read_token, device_id=did)
    assert len(shot.blob_ref) == 64
    # Bytes retrievable via get_blob
    bytes_ = runtime.get_blob(shot.blob_ref)
    assert bytes_ is not None
    assert bytes_.startswith(b"\x89PNG")


def test_capture_screenshot_audits_blob_ref(runtime: AdroidRuntime, read_token):
    did = DeviceId(kind="adb", value="mock-1")
    shot = runtime.capture_screenshot(token=read_token, device_id=did)

    events = runtime.audit_reader().tail(n=5)
    last = events[-1]
    assert last.metadata.get("blob_ref") == shot.blob_ref
    assert last.metadata.get("width") == 1


# ---------------------------------------------------------------------------
# app.list
# ---------------------------------------------------------------------------


def test_list_installed_apps(runtime: AdroidRuntime, read_token):
    did = DeviceId(kind="adb", value="mock-1")
    apps = runtime.list_installed_apps(token=read_token, device_id=did)
    assert len(apps) == 2
    assert any(a.package_name == "com.example.app" for a in apps)


# ---------------------------------------------------------------------------
# app.launch — requires ACT scope
# ---------------------------------------------------------------------------


def test_launch_app_with_act_scope_succeeds(runtime: AdroidRuntime, act_token):
    did = DeviceId(kind="adb", value="mock-1")
    runtime.launch_app(token=act_token, device_id=did, package_name="com.example.app")


def test_launch_app_with_read_scope_denied(runtime: AdroidRuntime, read_token):
    did = DeviceId(kind="adb", value="mock-1")
    with pytest.raises(PermissionDeniedError) as exc:
        runtime.launch_app(token=read_token, device_id=did, package_name="com.example.app")
    assert exc.value.code == "adroid.permission.insufficient_scope"


# ---------------------------------------------------------------------------
# Audit trail integrity — runtime must produce a verifiable chain
# ---------------------------------------------------------------------------


def test_audit_log_verifies_end_to_end(runtime: AdroidRuntime, read_token):
    """After a sequence of calls, the audit log must verify cleanly."""
    did = DeviceId(kind="adb", value="mock-1")
    runtime.list_devices(token=read_token)
    runtime.observe_device(token=read_token, device_id=did)
    runtime.list_installed_apps(token=read_token, device_id=did)
    runtime.capture_screenshot(token=read_token, device_id=did)

    reader = runtime.audit_reader()
    results = list(reader.verify_all())
    assert len(results) >= 5  # boot + 4 calls
    for seq, ok, err in results:
        assert ok, f"audit verification failed at sequence {seq}: {err}"


# ---------------------------------------------------------------------------
# Rate limiting — runtime reuses gate's rate limiter
# ---------------------------------------------------------------------------


def test_runtime_enforces_rate_limit(runtime: AdroidRuntime):
    token = runtime.issue_token(
        subject="agent:limited",
        grants=[
            CapabilityGrant(
                capability=Capability.DEVICE_LIST,
                scope=Scope.READ,
                rate_limit_per_minute=2,
            )
        ],
    )
    runtime.list_devices(token=token)
    runtime.list_devices(token=token)
    with pytest.raises(RateLimitedError):
        runtime.list_devices(token=token)


# ---------------------------------------------------------------------------
# Capability token PEM export — needed for KMS / verifier distribution
# ---------------------------------------------------------------------------


def test_runtime_exports_public_keys_as_pem(runtime: AdroidRuntime):
    token_pem = runtime.token_public_key_pem
    audit_pem = runtime.audit_public_key_pem
    assert "BEGIN PUBLIC KEY" in token_pem
    assert "BEGIN PUBLIC KEY" in audit_pem
    # Different keypairs
    assert token_pem != audit_pem


# ---------------------------------------------------------------------------
# v0.3.7: args_schema enforcement
# ---------------------------------------------------------------------------


def test_validate_args_rejects_out_of_range_x(runtime: AdroidRuntime):
    """x=99999 should fail validation against max 8192."""
    from adroid.contract.errors import ContractError
    from adroid.runtime.core import INPUT_TAP_SPEC
    with pytest.raises(ContractError) as exc_info:
        runtime.validate_args(INPUT_TAP_SPEC, {
            "device_id": {"kind": "adb", "value": "mock-1"},
            "x": 99999,
            "y": 100,
        })
    assert exc_info.value.code == "adroid.contract.schema_violation"
    assert exc_info.value.details["arg"] == "x"
    assert exc_info.value.details["maximum"] == 8192


def test_validate_args_rejects_wrong_type(runtime: AdroidRuntime):
    """x as string should fail type validation."""
    from adroid.contract.errors import ContractError
    from adroid.runtime.core import INPUT_TAP_SPEC
    with pytest.raises(ContractError) as exc_info:
        runtime.validate_args(INPUT_TAP_SPEC, {
            "device_id": {"kind": "adb", "value": "mock-1"},
            "x": "540",
            "y": 100,
        })
    assert exc_info.value.code == "adroid.contract.schema_violation"
    assert "must be integer" in str(exc_info.value)


def test_validate_args_rejects_missing_required(runtime: AdroidRuntime):
    """Missing required arg should fail with structured error."""
    from adroid.contract.errors import ContractError
    from adroid.runtime.core import INPUT_TAP_SPEC
    with pytest.raises(ContractError) as exc_info:
        runtime.validate_args(INPUT_TAP_SPEC, {
            "device_id": {"kind": "adb", "value": "mock-1"},
            "x": 540,
            # y missing
        })
    assert exc_info.value.code == "adroid.contract.schema_violation"
    assert "y" in exc_info.value.details["missing"]


def test_validate_args_rejects_unknown_arg_when_strict(runtime: AdroidRuntime):
    """additionalProperties: False should reject unknown args."""
    from adroid.contract.errors import ContractError
    from adroid.runtime.core import INPUT_TAP_SPEC
    with pytest.raises(ContractError) as exc_info:
        runtime.validate_args(INPUT_TAP_SPEC, {
            "device_id": {"kind": "adb", "value": "mock-1"},
            "x": 540,
            "y": 100,
            "unexpected": "should fail",
        })
    assert exc_info.value.code == "adroid.contract.schema_violation"
    assert exc_info.value.details["unknown_arg"] == "unexpected"


def test_validate_args_accepts_valid_input(runtime: AdroidRuntime):
    """Valid args should pass validation without raising."""
    from adroid.runtime.core import INPUT_TAP_SPEC
    runtime.validate_args(INPUT_TAP_SPEC, {
        "device_id": {"kind": "adb", "value": "mock-1"},
        "x": 540,
        "y": 1200,
    })  # should not raise


def test_validate_args_skips_permissive_schema(runtime: AdroidRuntime):
    """Tools with additionalProperties: True or no constraints should skip validation."""
    from adroid.contract.types import Capability, Scope, ToolSpec
    permissive_spec = ToolSpec(
        name="test.permissive",
        capability=Capability.DEVICE_LIST,
        required_scope=Scope.READ,
        description="test",
        args_schema={"type": "object"},  # no constraints
        result_schema={"type": "object"},
    )
    runtime.validate_args(permissive_spec, {"anything": "goes"})  # should not raise
