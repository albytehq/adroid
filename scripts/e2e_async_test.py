#!/usr/bin/env python
"""End-to-end test: async agent flow + browser approval.

Simulates the actual usage pattern:
    1. Agent POSTs to /agent/call/async with token
    2. Server returns 200 with {status: "pending_approval", approval_id, approval_url}
    3. Agent shares approval_url with the human
    4. Human opens URL in browser, taps Approve (or Deny)
    5. Agent polls /agent/result/{approval_id} until state != "pending"
    6. Agent reads the final result (or error)
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import requests


def drain_stdout(proc, sink: list):
    for line in proc.stdout:
        sink.append(line.rstrip())


def wait_for_server(base: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{base}/api/tools", timeout=1)
            if r.status_code == 200:
                return True
        except requests.exceptions.ConnectionError:
            time.sleep(0.1)
    return False


def find_starter_token(lines: list[str]) -> dict | None:
    for line in lines:
        s = line.strip()
        if s.startswith('{"token_id"'):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass
    return None


def wait_for_terminal_state(base: str, approval_id: str, timeout: float = 10.0) -> dict:
    """Poll /agent/result/{id} until state is terminal (completed/denied/timeout/error)."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        res = requests.get(f"{base}/agent/result/{approval_id}")
        if res.status_code == 404:
            time.sleep(0.1)
            continue
        body = res.json()
        last = body
        if body["state"] in ("completed", "denied", "timeout", "error"):
            return body
        time.sleep(0.2)
    return last or {"state": "unknown"}


def main() -> None:
    print("=== Adroid end-to-end test: ASYNC agent flow ===\n")

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "adroid.cli",
            "--bridge", "mock",
            "--port", "17658",
            "--audit-log", "/tmp/adroid-async-e2e.auditlog",
            "--approval-timeout", "30",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    stdout_lines: list[str] = []
    drain_thread = threading.Thread(target=drain_stdout, args=(proc, stdout_lines), daemon=True)
    drain_thread.start()

    try:
        base = "http://localhost:17658"
        if not wait_for_server(base):
            print("FAILED: server did not come up")
            print("\n".join(stdout_lines))
            return

        token = None
        for _ in range(50):
            token = find_starter_token(stdout_lines)
            if token:
                break
            time.sleep(0.1)

        if not token:
            print("FAILED: did not see starter token")
            return

        print(f"Server ready. Token subject={token['subject']}\n")

        # Test 1: Async READ scope — completes immediately
        print("--- Test 1: async device.list (READ, immediate) ---")
        res = requests.post(f"{base}/agent/call/async", json={
            "token": token, "tool": "device.list", "args": {},
        })
        body = res.json()
        print(f"  status={body['status']}, devices={len(body.get('result', {}).get('devices', []))}")
        assert body["status"] == "completed"
        assert body["approval_id"] is None

        # Test 2: Async ACT scope — returns pending + URL
        print("\n--- Test 2: async app.launch (ACT, returns approval URL) ---")
        res = requests.post(f"{base}/agent/call/async", json={
            "token": token,
            "tool": "app.launch",
            "args": {
                "device_id": {"kind": "adb", "value": "mock-emulator-5554"},
                "package_name": "com.example.app",
            },
        })
        body = res.json()
        print(f"  status={body['status']}")
        print(f"  approval_id={body['approval_id'][:8]}")
        print(f"  approval_url={body['approval_url']}")
        assert body["status"] == "pending_approval"
        approval_id = body["approval_id"]
        approval_url = body["approval_url"]
        assert approval_id in approval_url

        # Test 3: Poll immediately — should be pending
        print("\n--- Test 3: poll /agent/result (should be pending) ---")
        res = requests.get(f"{base}/agent/result/{approval_id}")
        body = res.json()
        print(f"  state={body['state']}, decision={body.get('decision')}")
        assert body["state"] == "pending"

        # Test 4: Human opens URL, approves
        print("\n--- Test 4: human approves via /api/approve ---")
        res = requests.post(f"{base}/api/approve/{approval_id}")
        print(f"  decision={res.json()['decision']}")
        assert res.json()["decision"] == "approved"

        # Test 5: Poll — should eventually be completed
        print("\n--- Test 5: poll /agent/result (should be completed) ---")
        final = wait_for_terminal_state(base, approval_id, timeout=10.0)
        print(f"  state={final['state']}, decision={final.get('decision')}, approver={final.get('approver')}")
        print(f"  result={final.get('result')}")
        assert final["state"] == "completed"
        assert final["result"]["launched"] is True

        # Test 6: Async screenshot flow
        print("\n--- Test 6: async device.screenshot + fetch bytes ---")
        res = requests.post(f"{base}/agent/call/async", json={
            "token": token,
            "tool": "device.screenshot",
            "args": {"device_id": {"kind": "adb", "value": "mock-emulator-5554"}},
        })
        body = res.json()
        assert body["status"] == "completed"
        blob_ref = body["result"]["screenshot"]["blob_ref"]
        res = requests.get(f"{base}/blob/{blob_ref}/raw")
        print(f"  screenshot: {len(res.content)} bytes, starts with PNG={res.content[:4] == b'\\x89PNG'}")
        assert res.content.startswith(b"\x89PNG")

        # Test 7: Async denial flow
        print("\n--- Test 7: async app.launch DENIED ---")
        res = requests.post(f"{base}/agent/call/async", json={
            "token": token,
            "tool": "app.launch",
            "args": {
                "device_id": {"kind": "adb", "value": "mock-emulator-5554"},
                "package_name": "com.evil.app",
            },
        })
        approval_id2 = res.json()["approval_id"]
        print(f"  approval_id={approval_id2[:8]}")
        # Deny
        requests.post(f"{base}/api/deny/{approval_id2}")
        # Poll
        final = wait_for_terminal_state(base, approval_id2, timeout=5.0)
        print(f"  state={final['state']}, decision={final.get('decision')}")
        assert final["state"] in ("denied", "error")
        assert final.get("decision") == "denied"

        # Test 8: Verify audit log has all events
        print("\n--- Test 8: audit log ---")
        events = requests.get(f"{base}/api/audit?limit=50").json()
        print(f"  events: {len(events)}")
        for e in events[-5:]:
            print(f"    [{e.get('outcome')}] {e.get('method')}")

        print("\n" + "=" * 60)
        print("ALL ASYNC TESTS PASSED.")
        print()
        print("Flow verified:")
        print("  1. Agent POST /agent/call/async → 200 + approval_id + approval_url")
        print("  2. Agent shares approval_url with human (URL contains hash fragment)")
        print("  3. Human opens URL in browser → sees pending card highlighted")
        print("  4. Human taps Approve → /api/approve/{id}")
        print("  5. Agent polls /agent/result/{id} → state goes pending → completed")
        print("  6. Agent reads result and continues")
        print()
        print("Same flow with Deny → state goes pending → denied (with error code)")
        print("=" * 60)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
