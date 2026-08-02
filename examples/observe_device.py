"""End-to-end example: spin up Adroid runtime with a mock device and run
every v0.1.0 tool call.

Run with::

    python examples/observe_device.py

No real Android device needed — the mock bridge fakes one in-process.
"""

from __future__ import annotations

import json
from pathlib import Path

from adroid.bridge.mock import InMemoryBlobStore, MockBridge
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


def main() -> None:
    print("=" * 70)
    print("Adroid v0.1.0 — End-to-End Example (mock device)")
    print("=" * 70)

    # 1. Boot the runtime
    blob_store = InMemoryBlobStore()
    bridge = MockBridge(blob_store=blob_store)
    bridge.add_device(
        DeviceInfo(
            id=DeviceId(kind="adb", value="mock-emulator-5554"),
            state=DeviceState.ONLINE,
            model="Pixel 8 Pro (Mock)",
            manufacturer="Adroid",
            android_version="14",
            sdk_level=34,
            battery_level=87.5,
        ),
        apps=[
            AppInfo(package_name="com.example.calculator", version_name="3.1.4", version_code=314),
            AppInfo(package_name="com.example.todo", version_name="1.0.0"),
            AppInfo(package_name="com.android.settings", system_app=True),
        ],
    )

    runtime = AdroidRuntime(
        issuer_id="example-runtime",
        bridge=bridge,
        blob_store=blob_store,
        audit_log_path=Path("/tmp/adroid-example.auditlog"),
    )
    print(f"\n[boot] runtime issuer_id = {runtime.issuer_id}")
    print(f"[boot] tools registered  = {[t.name for t in runtime.list_tools()]}")

    # 2. Issue a token for our agent
    token = runtime.issue_token(
        subject="agent:example",
        grants=[
            (Capability.DEVICE_LIST, Scope.READ),
            (Capability.DEVICE_OBSERVE, Scope.READ),
            (Capability.DEVICE_SCREENSHOT, Scope.READ),
            (Capability.APP_LIST, Scope.READ),
            (Capability.APP_LAUNCH, Scope.ACT),
        ],
        ttl_seconds=3600,
    )
    print(f"[token] subject = {token.subject}")
    print(f"[token] grants = {[(g.capability.value, g.scope.value) for g in token.grants]}")
    print(f"[token] expires_at = {token.expires_at.isoformat()}")

    # 3. List devices
    print("\n--- device.list ---")
    devices = runtime.list_devices(token=token)
    for d in devices:
        print(f"  {d.id} state={d.state.value} model={d.model}")

    # 4. Observe one device
    print("\n--- device.observe ---")
    target = DeviceId(kind="adb", value="mock-emulator-5554")
    info = runtime.observe_device(token=token, device_id=target)
    print(f"  {info.model} (Android {info.android_version}, SDK {info.sdk_level})")
    print(f"  battery = {info.battery_level}%")

    # 5. List installed apps
    print("\n--- app.list ---")
    apps = runtime.list_installed_apps(token=token, device_id=target)
    for a in apps:
        kind = "system" if a.system_app else "user"
        print(f"  [{kind}] {a.package_name} ({a.version_name or 'unknown'})")

    # 6. Capture a screenshot
    print("\n--- device.screenshot ---")
    shot = runtime.capture_screenshot(token=token, device_id=target)
    print(f"  blob_ref = {shot.blob_ref}")
    print(f"  size     = {shot.width}x{shot.height}")
    png_bytes = runtime.get_blob(shot.blob_ref)
    print(f"  bytes    = {len(png_bytes) if png_bytes else 0}")

    # 7. Launch an app
    print("\n--- app.launch ---")
    runtime.launch_app(token=token, device_id=target, package_name="com.example.calculator")
    print(f"  launched com.example.calculator")

    # 8. Verify the audit log
    print("\n--- audit verification ---")
    reader = runtime.audit_reader()
    results = list(reader.verify_all())
    ok_count = sum(1 for _, ok, _ in results if ok)
    fail_count = sum(1 for _, ok, _ in results if not ok)
    print(f"  verified: {ok_count} events OK, {fail_count} failed")
    print(f"  audit log file: /tmp/adroid-example.auditlog")

    # 9. Print the audit trail
    print("\n--- audit trail (last 5 events) ---")
    events = reader.tail(n=5)
    for e in events:
        cap = e.capability.value if e.capability else "-"
        scope = e.scope.value if e.scope else "-"
        outcome = e.outcome.value
        method = e.method
        err = f" err={e.error_code}" if e.error_code else ""
        print(f"  seq={e.sequence} {outcome:8s} {method:32s} cap={cap:24s} scope={scope}{err}")

    print("\n" + "=" * 70)
    print("Done. All 5 v0.1.0 tools exercised end-to-end.")
    print("=" * 70)


if __name__ == "__main__":
    main()
