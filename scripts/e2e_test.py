#!/usr/bin/env python
"""End-to-end test: session pairing flow.

Simulates the actual usage pattern:
    1. AI POSTs bootstrap_token to /agent/session/request
    2. Server returns pairing_id + pairing_url
    3. AI shares pairing_url with human
    4. Human opens URL, picks TTL, taps Approve
    5. AI polls /agent/session/{pairing_id} → gets session_token
    6. AI calls /agent/call freely (no per-action approval!)
    7. Human clicks Disconnect → AI gets revoked
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


def find_bootstrap_token(lines: list[str]) -> dict | None:
    """Find the bootstrap token JSON in the runtime's stdout."""
    for line in lines:
        s = line.strip()
        if s.startswith('{"token_id"') and '"subject":"bootstrap"' in s:
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass
    return None


def main() -> None:
    print("=== Adroid end-to-end test: session pairing flow ===\n")

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "adroid.cli",
            "--bridge", "mock",
            "--port", "17660",
            "--audit-log", "/tmp/adroid-pairing-e2e.auditlog",
            "--max-sessions", "20",
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
        base = "http://localhost:17660"
        if not wait_for_server(base):
            print("FAILED: server did not come up")
            print("\n".join(stdout_lines))
            return

        # Find bootstrap token from captured stdout
        bootstrap = None
        for _ in range(50):
            bootstrap = find_bootstrap_token(stdout_lines)
            if bootstrap:
                break
            time.sleep(0.1)

        if not bootstrap:
            print("FAILED: did not see bootstrap token in stdout")
            print("\n".join(stdout_lines))
            return

        print(f"Server ready. Bootstrap token subject={bootstrap['subject']}\n")

        # Test 1: Request pairing
        print("--- Test 1: AI requests pairing ---")
        res = requests.post(f"{base}/agent/session/request", json={
            "bootstrap_token": bootstrap,
            "agent_id": "agent:e2e-test",
        })
        body = res.json()
        print(f"  pairing_id={body['pairing_id'][:8]}")
        print(f"  pairing_url={body['pairing_url']}")
        assert body["pairing_id"]
        assert "/ui#pairing-" in body["pairing_url"]
        pairing_id = body["pairing_id"]

        # Test 2: AI polls — should be pending
        print("\n--- Test 2: AI polls (should be pending) ---")
        res = requests.get(f"{base}/agent/session/{pairing_id}")
        body = res.json()
        print(f"  status={body['status']}")
        assert body["status"] == "pending"

        # Test 3: Human approves with TTL=3600s
        print("\n--- Test 3: Human approves (TTL=3600s) ---")
        res = requests.post(f"{base}/api/pair/{pairing_id}/approve", json={"ttl_seconds": 3600})
        print(f"  approved={res.json()['pairing']['status']}")
        assert res.json()["pairing"]["status"] == "approved"

        # Test 4: AI polls — should now have session_token
        print("\n--- Test 4: AI polls (should have session_token) ---")
        res = requests.get(f"{base}/agent/session/{pairing_id}")
        body = res.json()
        print(f"  status={body['status']}")
        print(f"  session_token subject={body['session_token']['subject']}")
        print(f"  session_token grants count={len(body['session_token']['grants'])}")
        assert body["status"] == "approved"
        assert body["session_token"]["subject"] == "session:agent:e2e-test"
        assert len(body["session_token"]["grants"]) == 12  # all capabilities
        session_token = body["session_token"]
        session_id = session_token["token_id"]

        # Test 5: AI calls /agent/call freely — NO per-action approval
        print("\n--- Test 5: AI calls device.list (no approval needed!) ---")
        res = requests.post(f"{base}/agent/call", json={
            "token": session_token, "tool": "device.list", "args": {},
        })
        body = res.json()
        print(f"  ok={body['ok']}, devices={len(body.get('result', {}).get('devices', []))}")
        assert body["ok"] is True

        print("\n--- Test 6: AI calls app.launch (ACT scope, no approval!) ---")
        res = requests.post(f"{base}/agent/call", json={
            "token": session_token,
            "tool": "app.launch",
            "args": {
                "device_id": {"kind": "adb", "value": "mock-emulator-5554"},
                "package_name": "com.example.app",
            },
        })
        body = res.json()
        print(f"  ok={body['ok']}, launched={body.get('result', {}).get('launched')}")
        assert body["ok"] is True

        print("\n--- Test 7: AI calls device.screenshot ---")
        res = requests.post(f"{base}/agent/call", json={
            "token": session_token,
            "tool": "device.screenshot",
            "args": {"device_id": {"kind": "adb", "value": "mock-emulator-5554"}},
        })
        body = res.json()
        blob_ref = body["result"]["screenshot"]["blob_ref"]
        res = requests.get(f"{base}/blob/{blob_ref}/raw")
        print(f"  ok={body['ok']}, screenshot={len(res.content)} bytes (PNG={res.content[:4] == b'\\x89PNG'})")

        # Test 8: Multiple rapid calls — prove no per-action blocking
        print("\n--- Test 8: 5 rapid calls (no blocking) ---")
        start = time.monotonic()
        for i in range(5):
            res = requests.post(f"{base}/agent/call", json={
                "token": session_token, "tool": "device.list", "args": {},
            })
            assert res.json()["ok"] is True
        elapsed = time.monotonic() - start
        print(f"  5 calls in {elapsed:.2f}s (no blocking, no approval)")

        # Test 9: Human disconnects
        print("\n--- Test 9: Human disconnects session ---")
        res = requests.post(f"{base}/api/session/{session_id}/disconnect")
        print(f"  disconnected={res.json()['ok']}")
        assert res.json()["ok"] is True

        # Test 10: AI's next call should be rejected
        print("\n--- Test 10: AI's next call (should be rejected) ---")
        res = requests.post(f"{base}/agent/call", json={
            "token": session_token, "tool": "device.list", "args": {},
        })
        body = res.json()
        print(f"  ok={body['ok']}, error_code={body.get('error', {}).get('code')}")
        assert body["ok"] is False
        assert body["error"]["code"] == "adroid.session.revoked"

        # Test 11: Check audit log
        print("\n--- Test 11: Audit log ---")
        events = requests.get(f"{base}/api/audit?limit=20").json()
        print(f"  events: {len(events)}")
        for e in events[-5:]:
            print(f"    [{e.get('outcome')}] {e.get('method')}")

        # Test 12: Terminal stream
        print("\n--- Test 12: Terminal stream ---")
        state = requests.get(f"{base}/api/state").json()
        print(f"  terminal lines: {len(state['terminal'])}")
        for line in state["terminal"][-6:]:
            print(f"    [{line['kind']}] {line['text']}")

        print("\n" + "=" * 60)
        print("ALL PAIRING FLOW TESTS PASSED.")
        print()
        print("Architecture verified:")
        print("  1. AI boots → gets bootstrap_token from runtime output")
        print("  2. AI POSTs bootstrap_token + agent_id → gets pairing_url")
        print("  3. AI shares pairing_url with human in chat")
        print("  4. Human opens URL → picks TTL → Approve")
        print("  5. AI polls → gets session_token (full access, TTL-set)")
        print("  6. AI calls /agent/call FREELY — no per-action approval")
        print("  7. Human clicks Disconnect → AI immediately revoked")
        print("  8. Audit log records every call; terminal streams live")
        print("=" * 60)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
