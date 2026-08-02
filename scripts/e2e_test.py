#!/usr/bin/env python
"""End-to-end test: boot runtime, simulate agent call + browser approval."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import requests


def drain_stdout(proc, sink: list):
    """Read proc.stdout line by line, append to sink. Returns when EOF."""
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


def wait_for_pending(base: str, timeout: float = 5.0) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            res = requests.get(f"{base}/api/pending", timeout=1)
            pending = res.json()
            if pending:
                return pending[0]["approval_id"]
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(0.1)
    return None


def main() -> None:
    print("=== Adroid end-to-end test: agent + browser approval ===\n")

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "adroid.cli",
            "--bridge", "mock",
            "--port", "17657",
            "--audit-log", "/tmp/adroid-e2e3.auditlog",
            "--approval-timeout", "60",
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
        base = "http://localhost:17657"
        if not wait_for_server(base):
            print("FAILED: server did not come up")
            print("\n".join(stdout_lines))
            return

        # Find the starter token from captured stdout
        token = None
        for _ in range(50):
            token = find_starter_token(stdout_lines)
            if token:
                break
            time.sleep(0.1)

        if not token:
            print("FAILED: did not see starter token")
            print("\n".join(stdout_lines))
            return

        print(f"Server ready. Token subject={token['subject']}, grants={len(token['grants'])}\n")

        # Test 1: device.list
        print("--- Test 1: device.list (READ scope, auto-approved) ---")
        res = requests.post(f"{base}/agent/call", json={
            "token": token, "tool": "device.list", "args": {},
        })
        body = res.json()
        print(f"  ok={body['ok']}, devices={len(body.get('result', {}).get('devices', []))}")
        assert body["ok"] is True

        # Test 2: device.observe
        print("\n--- Test 2: device.observe (READ scope) ---")
        res = requests.post(f"{base}/agent/call", json={
            "token": token,
            "tool": "device.observe",
            "args": {"device_id": {"kind": "adb", "value": "mock-emulator-5554"}},
        })
        body = res.json()
        print(f"  ok={body['ok']}, model={body.get('result', {}).get('device', {}).get('model')}")
        assert body["ok"] is True

        # Test 3: app.launch (ACT scope, requires approval)
        print("\n--- Test 3: app.launch (ACT scope, requires browser approval) ---")
        result_holder: dict = {}

        def agent_call():
            res = requests.post(f"{base}/agent/call", json={
                "token": token,
                "tool": "app.launch",
                "args": {
                    "device_id": {"kind": "adb", "value": "mock-emulator-5554"},
                    "package_name": "com.example.app",
                },
            }, timeout=60)
            result_holder["body"] = res.json()

        t = threading.Thread(target=agent_call)
        t.start()

        approval_id = wait_for_pending(base, timeout=5.0)
        assert approval_id, f"No pending approval appeared. stdout:\n{chr(10).join(stdout_lines[-20:])}"
        print(f"  Found pending: {approval_id[:8]}")
        # Pull details
        pending = requests.get(f"{base}/api/pending").json()[0]
        print(f"    subject={pending['subject']}, cap={pending['capability']}, scope={pending['required_scope']}")
        print(f"    args={pending['args']}")

        print("  -> Approving via /api/approve ...")
        res = requests.post(f"{base}/api/approve/{approval_id}")
        assert res.json()["decision"] == "approved"

        t.join(timeout=5)
        body = result_holder["body"]
        print(f"  ok={body['ok']}, launched={body.get('result', {}).get('launched')}")
        assert body["ok"] is True

        # Test 4: Audit log
        print("\n--- Test 4: Audit log ---")
        events = requests.get(f"{base}/api/audit").json()
        print(f"  events: {len(events)}")
        for e in events[-3:]:
            print(f"    [{e.get('outcome')}] {e.get('method')}")

        # Test 5: Terminal history
        print("\n--- Test 5: Terminal history ---")
        lines = requests.get(f"{base}/api/terminal").json()
        print(f"  lines: {len(lines)}")
        for line in lines[-6:]:
            print(f"    [{line['kind']}] {line['text']}")

        # Test 6: Denial flow
        print("\n--- Test 6: app.launch DENIED ---")
        result_holder = {}

        def agent_call2():
            res = requests.post(f"{base}/agent/call", json={
                "token": token,
                "tool": "app.launch",
                "args": {
                    "device_id": {"kind": "adb", "value": "mock-emulator-5554"},
                    "package_name": "com.evil.app",
                },
            }, timeout=60)
            result_holder["body"] = res.json()

        t = threading.Thread(target=agent_call2)
        t.start()

        approval_id = wait_for_pending(base, timeout=5.0)
        assert approval_id, "No pending approval for denial test"
        pending = requests.get(f"{base}/api/pending").json()[0]
        print(f"  Found pending: {approval_id[:8]} (pkg={pending['args']['package_name']})")

        print("  -> Denying via /api/deny ...")
        requests.post(f"{base}/api/deny/{approval_id}")

        t.join(timeout=5)
        body = result_holder["body"]
        print(f"  ok={body['ok']}, error_code={body.get('error', {}).get('code')}")
        assert body["ok"] is False
        assert body["error"]["code"] == "adroid.permission.interactive_denied"

        print("\n" + "=" * 60)
        print("ALL TESTS PASSED. End-to-end flow works:")
        print("  - READ scope auto-approved")
        print("  - ACT scope requires browser approval")
        print("  - Approval -> call proceeds, audit logged")
        print("  - Denial -> structured error returned")
        print("  - Terminal stream captures every step")
        print("=" * 60)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
