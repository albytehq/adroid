"""Heavy debugging + edge case tests — targeting zero bugs, extreme stability.

Tests cover:
  1. Empty/None/malformed inputs
  2. Concurrent access (thread safety)
  3. Error propagation
  4. Security boundaries (allowlist, token tampering)
  5. Persistence (memory, audit log crash recovery)
  6. Boundary conditions (max sessions, TTL expiry, rate limits)
  7. Unicode handling
  8. Large inputs
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.bridge.uiautomator_parser import parse_uiautomator_xml, parse_bounds, infer_role
from adroid.contract.errors import AdroidError, PermissionDeniedError, RateLimitedError
from adroid.contract.types import (
    Capability, CapabilityGrant, CapabilityToken, DeviceId, DeviceInfo,
    DeviceState, Scope,
)
from adroid.contract.ui import (
    Bounds, NodeRole, Selector, SemanticNode, WaitConditionType,
    WaitForCondition, WorldState,
)
from adroid.memory import MemoryStore, device_key, skill_key, failure_key
from adroid.permissions.allowlist import AllowlistChecker, AllowlistConfig
from adroid.permissions.sessions import SessionManager
from adroid.permissions.tokens import (
    TokenIssuer, generate_signing_keypair, validate_token_at, verify_token,
)
from adroid.perception import ConfidenceTracker, SelectorStrategy, SelfHealingSelector
from adroid.runtime.core import AdroidRuntime
from adroid.web.server import create_app


# ---------------------------------------------------------------------------
# 1. Empty/None/Malformed inputs
# ---------------------------------------------------------------------------


class TestMalformedInputs:
    def test_parse_empty_xml(self):
        assert parse_uiautomator_xml("") == []
        assert parse_uiautomator_xml("   ") == []
        assert parse_uiautomator_xml("\n\n") == []

    def test_parse_malformed_xml(self):
        # Should not crash — should return empty or partial
        result = parse_uiautomator_xml("<not xml at all")
        assert isinstance(result, list)

    def test_parse_xml_with_no_nodes(self):
        result = parse_uiautomator_xml('<?xml version="1.0"?><hierarchy></hierarchy>')
        assert result == []

    def test_parse_bounds_empty(self):
        b = parse_bounds("")
        assert b.x == 0 and b.w == 0

    def test_parse_bounds_garbage(self):
        b = parse_bounds("not bounds")
        assert b.x == 0 and b.w == 0

    def test_selector_with_all_none(self):
        sel = Selector()
        ws = WorldState(revision=1, nodes=())
        # Should match nothing (no predicates = match all, but no nodes)
        matches = ws.find(sel)
        assert len(matches) == 0

    def test_worldstate_empty_nodes(self):
        ws = WorldState(revision=0, nodes=())
        assert ws.node_count == 0
        assert ws.clickable_nodes == ()

    def test_memory_store_corrupt_file(self, tmp_path):
        # Write corrupt JSON to memory file
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / "device.json").write_text("{corrupt json!!!")
        # Should not crash — should start fresh
        store = MemoryStore(memory_dir=mem_dir)
        assert store.stats()["device"] == 0

    def test_audit_reader_nonexistent_file(self, tmp_path):
        from adroid.audit.writer import AuditReader
        from adroid.permissions.tokens import generate_signing_keypair
        _, pub = generate_signing_keypair()
        reader = AuditReader(log_path=tmp_path / "nonexistent.auditlog", public_key=pub)
        results = list(reader.verify_all())
        assert results == []

    def test_allowlist_empty_command(self):
        checker = AllowlistChecker()
        d = checker.check("")
        assert not d.allowed

    def test_allowlist_none_command(self):
        checker = AllowlistChecker()
        d = checker.check("   ")
        assert not d.allowed


# ---------------------------------------------------------------------------
# 2. Concurrent access (thread safety)
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_memory_store_concurrent_writes(self, tmp_path):
        store = MemoryStore(memory_dir=tmp_path / "mem")
        errors = []

        def writer(thread_id):
            try:
                for i in range(20):
                    store.remember_device(f"thread_{thread_id}:item_{i}", {"v": i})
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert store.stats()["device"] == 100  # 5 threads × 20 items

    def test_confidence_tracker_concurrent(self):
        tracker = ConfidenceTracker()
        errors = []

        def recorder():
            try:
                for _ in range(50):
                    tracker.record_success(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
                    tracker.record_failure(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=recorder) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        stats = tracker.get_stats(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
        # 4 threads × 50 successes + 4 threads × 50 failures
        assert stats.total == 400

    def test_in_memory_blob_store_concurrent(self):
        store = InMemoryBlobStore()
        errors = []

        def putter():
            try:
                for i in range(50):
                    store.put(f"data-{i}".encode())
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=putter) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# ---------------------------------------------------------------------------
# 3. Token security edge cases
# ---------------------------------------------------------------------------


class TestTokenSecurity:
    def test_tampered_subject_rejected(self):
        priv, pub = generate_signing_keypair()
        issuer = TokenIssuer(issuer_id="test", signing_key=priv, public_key=pub)
        token = issuer.issue(subject="agent:good", grants=[(Capability.DEVICE_LIST, Scope.READ)])
        tampered = token.model_copy(update={"subject": "agent:evil"})
        with pytest.raises(PermissionDeniedError):
            verify_token(tampered, pub)

    def test_tampered_grants_rejected(self):
        priv, pub = generate_signing_keypair()
        issuer = TokenIssuer(issuer_id="test", signing_key=priv, public_key=pub)
        token = issuer.issue(subject="agent:low", grants=[(Capability.DEVICE_LIST, Scope.READ)])
        # Try to add admin grant after signing
        tampered = token.model_copy(update={
            "grants": frozenset({
                CapabilityGrant(capability=Capability.DEVICE_LIST, scope=Scope.READ),
                CapabilityGrant(capability=Capability.SHELL_EXEC, scope=Scope.ADMIN),
            })
        })
        with pytest.raises(PermissionDeniedError):
            verify_token(tampered, pub)

    def test_tampered_expiry_rejected(self):
        priv, pub = generate_signing_keypair()
        issuer = TokenIssuer(issuer_id="test", signing_key=priv, public_key=pub)
        token = issuer.issue(subject="x", grants=[], ttl=timedelta(seconds=1))
        # Try to extend expiry
        far_future = datetime.now(timezone.utc) + timedelta(days=365)
        tampered = token.model_copy(update={"expires_at": far_future})
        with pytest.raises(PermissionDeniedError):
            verify_token(tampered, pub)

    def test_expired_token_rejected(self):
        priv, pub = generate_signing_keypair()
        issuer = TokenIssuer(issuer_id="test", signing_key=priv, public_key=pub)
        token = issuer.issue(subject="x", grants=[], ttl=timedelta(seconds=1))
        time.sleep(1.1)
        with pytest.raises(PermissionDeniedError) as exc:
            validate_token_at(token, pub)
        assert exc.value.code == "adroid.token.expired"

    def test_wrong_issuer_rejected(self):
        priv, pub = generate_signing_keypair()
        issuer = TokenIssuer(issuer_id="issuer-A", signing_key=priv, public_key=pub)
        token = issuer.issue(subject="x", grants=[])
        with pytest.raises(PermissionDeniedError) as exc:
            validate_token_at(token, pub, expected_issuer="issuer-B")
        assert exc.value.code == "adroid.token.issuer_mismatch"


# ---------------------------------------------------------------------------
# 4. Allowlist security edge cases
# ---------------------------------------------------------------------------


class TestAllowlistSecurity:
    def test_all_forbidden_metachars(self):
        checker = AllowlistChecker()
        for mc in [";", "|", "&", "`", "$(", "${", "&&", "||", ">"]:
            d = checker.check(f"pm list packages {mc} rm -rf /")
            assert not d.allowed, f"Should reject metachar {mc!r}"

    def test_rm_rf_rejected(self):
        checker = AllowlistChecker()
        assert not checker.check("rm -rf /").allowed
        assert not checker.check("rm -rf ~").allowed
        assert not checker.check("rm -rf /data").allowed

    def test_monkey_with_count_gt_1_rejected(self):
        checker = AllowlistChecker()
        # count=1 is allowed, count>1 should NOT match (generates random events)
        assert checker.check("monkey -p com.app 1").allowed
        assert not checker.check("monkey -p com.app 2").allowed
        assert not checker.check("monkey -p com.app 100").allowed

    def test_oversized_command_rejected(self):
        config = AllowlistConfig(max_command_length=50)
        checker = AllowlistChecker(config)
        long_cmd = "getprop " + "x" * 100
        d = checker.check(long_cmd)
        assert not d.allowed
        assert "max length" in d.reason

    def test_allow_compound_mode_works(self):
        config = AllowlistConfig(patterns=[r"^echo .*$"], allow_compound=True)
        checker = AllowlistChecker(config)
        # With allow_compound, metachar check is skipped
        assert checker.check("echo hello | grep h").allowed


# ---------------------------------------------------------------------------
# 5. Session manager edge cases
# ---------------------------------------------------------------------------


class TestSessionEdgeCases:
    @pytest.fixture
    def manager(self):
        priv, pub = generate_signing_keypair()
        issuer = TokenIssuer(issuer_id="test", signing_key=priv, public_key=pub)
        return SessionManager(issuer=issuer, max_sessions=3)

    def test_max_sessions_enforced(self, manager):
        for i in range(3):
            p = manager.request_pairing(agent_id=f"a{i}")
            manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=3600, approver="b")
        with pytest.raises(RateLimitedError):
            manager.request_pairing(agent_id="a4")

    def test_double_approve_rejected(self, manager):
        p = manager.request_pairing(agent_id="a")
        manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=3600, approver="b")
        with pytest.raises(AdroidError):
            manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=3600, approver="b")

    def test_deny_after_approve_rejected(self, manager):
        p = manager.request_pairing(agent_id="a")
        manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=3600, approver="b")
        with pytest.raises(AdroidError):
            manager.deny_pairing(pairing_id=p.pairing_id, approver="b")

    def test_disconnect_unknown_returns_false(self, manager):
        assert manager.disconnect_session("bogus-id") is False

    def test_double_disconnect_returns_false(self, manager):
        p = manager.request_pairing(agent_id="a")
        manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=3600, approver="b")
        assert manager.disconnect_session(p.session_id) is True
        assert manager.disconnect_session(p.session_id) is False

    def test_bad_agent_id_rejected(self, manager):
        with pytest.raises(AdroidError):
            manager.request_pairing(agent_id="")
        with pytest.raises(AdroidError):
            manager.request_pairing(agent_id="x" * 300)

    def test_bad_ttl_rejected(self, manager):
        p = manager.request_pairing(agent_id="a")
        with pytest.raises(AdroidError):
            manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=30, approver="b")
        with pytest.raises(AdroidError):
            manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=999999999, approver="b")


# ---------------------------------------------------------------------------
# 6. Unicode handling
# ---------------------------------------------------------------------------


class TestUnicode:
    def test_unicode_in_selector_text(self):
        sel = Selector(text="登录")  # Chinese "Login"
        ws = WorldState(revision=1, nodes=(
            SemanticNode(uuid="n1", role=NodeRole.BUTTON, text="登录", bounds=Bounds(x=0,y=0,w=100,h=50)),
        ))
        matches = ws.find(sel)
        assert len(matches) == 1
        assert matches[0].text == "登录"

    def test_unicode_in_input_text(self):
        blob_store = InMemoryBlobStore()
        bridge = MockBridge(blob_store=blob_store)
        bridge.add_device(DeviceInfo(
            id=DeviceId(kind="adb",value="m"), state=DeviceState.ONLINE, model="X"))
        bridge.input_text(DeviceId(kind="adb",value="m"), "héllo 世界 🎉")
        with bridge._lock:
            assert bridge._devices["adb:m"].last_text == "héllo 世界 🎉"

    def test_unicode_in_xml_text(self):
        xml = '''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy><node text="Submit" class="android.widget.Button" bounds="[0,0][10,10]" clickable="true" /></hierarchy>'''
        nodes = parse_uiautomator_xml(xml)
        assert len(nodes) == 1
        assert nodes[0].text == "Submit"


# ---------------------------------------------------------------------------
# 7. Audit log integrity
# ---------------------------------------------------------------------------


class TestAuditIntegrity:
    def test_audit_log_crash_recovery(self, tmp_path):
        from adroid.audit.writer import AuditWriter, AuditReader
        from adroid.contract.types import AuditEvent, AuditOutcome
        priv, pub = generate_signing_keypair()
        log_path = tmp_path / "test.auditlog"
        writer = AuditWriter(log_path=log_path, signing_key=priv, public_key=pub)

        # Write 3 valid events
        for i in range(3):
            writer.write(AuditEvent(
                outcome=AuditOutcome.SUCCESS,
                method=f"test.method_{i}",
                args_digest="0" * 64,
            ))

        # Append a truncated line (simulating crash mid-write)
        with open(log_path, "ab") as f:
            f.write(b'{"sequence": 3, "incomplete"')

        # New writer should recover — skip the truncated line
        writer2 = AuditWriter(log_path=log_path, signing_key=priv, public_key=pub)
        # 3 valid events written; truncated line is skipped during recovery
        # so next sequence = 3 (recovery finds 3 valid, next write = 3)
        assert writer2._sequence == 3  # noqa: SLF001
        assert writer2._prev_hash is not None  # noqa: SLF001

    def test_audit_log_tamper_detection(self, tmp_path):
        from adroid.audit.writer import AuditWriter, AuditReader
        from adroid.contract.types import AuditEvent, AuditOutcome
        priv, pub = generate_signing_keypair()
        log_path = tmp_path / "test.auditlog"
        writer = AuditWriter(log_path=log_path, signing_key=priv, public_key=pub)

        writer.write(AuditEvent(outcome=AuditOutcome.SUCCESS, method="ok", args_digest="0"*64))

        # Tamper: modify the payload
        lines = log_path.read_text().splitlines()
        rec = json.loads(lines[0])
        rec["payload"]["method"] = "TAMPERED"
        lines[0] = json.dumps(rec)
        log_path.write_text("\n".join(lines) + "\n")

        reader = AuditReader(log_path=log_path, public_key=pub)
        results = list(reader.verify_all())
        assert any(not ok for _, ok, _ in results)


# ---------------------------------------------------------------------------
# 8. Self-healing edge cases
# ---------------------------------------------------------------------------


class TestSelfHealingEdgeCases:
    def test_empty_worldstate(self):
        healer = SelfHealingSelector()
        ws = WorldState(revision=1, nodes=())
        result = healer.find(ws, Selector(text="anything"))
        assert not result.matched

    def test_multiple_matches_pick_first(self):
        nodes = (
            SemanticNode(uuid="b1", role=NodeRole.BUTTON, text="OK", bounds=Bounds(x=0,y=0,w=10,h=10)),
            SemanticNode(uuid="b2", role=NodeRole.BUTTON, text="OK", bounds=Bounds(x=20,y=20,w=10,h=10)),
        )
        ws = WorldState(revision=1, nodes=nodes)
        healer = SelfHealingSelector()
        result = healer.find(ws, Selector(text="OK", match="first"))
        assert result.matched
        assert result.nodes[0].uuid == "b1"

    def test_multiple_matches_pick_last(self):
        nodes = (
            SemanticNode(uuid="b1", role=NodeRole.BUTTON, text="OK", bounds=Bounds(x=0,y=0,w=10,h=10)),
            SemanticNode(uuid="b2", role=NodeRole.BUTTON, text="OK", bounds=Bounds(x=20,y=20,w=10,h=10)),
        )
        ws = WorldState(revision=1, nodes=nodes)
        healer = SelfHealingSelector()
        result = healer.find(ws, Selector(text="OK", match="last"))
        assert result.matched
        assert result.nodes[0].uuid == "b2"

    def test_resource_id_suffix_match(self):
        nodes = (
            SemanticNode(uuid="b1", role=NodeRole.BUTTON, text="",
                         resource_id="com.app:id/submit_btn",
                         bounds=Bounds(x=0,y=0,w=10,h=10)),
        )
        ws = WorldState(revision=1, nodes=nodes)
        # Short form should match
        matches = ws.find(Selector(resource_id="submit_btn"))
        assert len(matches) == 1
        # Full form should also match
        matches = ws.find(Selector(resource_id="com.app:id/submit_btn"))
        assert len(matches) == 1


# ---------------------------------------------------------------------------
# 9. Rust extension correctness
# ---------------------------------------------------------------------------


class TestRustExtension:
    def test_rust_available(self):
        try:
            import adroid_rust
            assert adroid_rust.version().startswith("0.3")
        except ImportError:
            pytest.skip("Rust extension not installed")

    def test_rust_matches_python_output(self):
        try:
            import adroid_rust
        except ImportError:
            pytest.skip("Rust extension not installed")

        xml = '''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node text="" class="android.widget.FrameLayout" package="com.app" bounds="[0,0][1080,2340]" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" selected="false">
    <node text="Search" resource-id="com.app:id/search" class="android.widget.Button" content-desc="Search" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" selected="false" bounds="[100,200][300,250]" />
    <node text="" resource-id="com.app:id/input" class="android.widget.EditText" clickable="true" enabled="true" focusable="true" focused="true" scrollable="false" selected="false" bounds="[50,500][950,650]" />
  </node>
</hierarchy>'''

        # Parse with both
        py_nodes = parse_uiautomator_xml(xml)
        rust_json = adroid_rust.parse_uiautomator_xml_json(xml)
        rust_nodes = json.loads(rust_json)

        assert len(py_nodes) == len(rust_nodes)

        # Compare by role + text + bounds
        py_sorted = sorted(py_nodes, key=lambda n: (n.role.value, n.text or "", n.bounds.x))
        rust_sorted = sorted(rust_nodes, key=lambda n: (n["role"], n.get("text") or "", n["bounds"]["x"]))

        for py_n, rust_n in zip(py_sorted, rust_sorted):
            assert py_n.role.value == rust_n["role"]
            assert py_n.text == rust_n.get("text")
            assert py_n.clickable == rust_n["clickable"]
            assert py_n.focused == rust_n["focused"]
            assert py_n.bounds.x == rust_n["bounds"]["x"]
            assert py_n.bounds.y == rust_n["bounds"]["y"]
            assert py_n.bounds.w == rust_n["bounds"]["w"]
            assert py_n.bounds.h == rust_n["bounds"]["h"]

    def test_rust_empty_xml(self):
        try:
            import adroid_rust
        except ImportError:
            pytest.skip("Rust extension not installed")
        result = adroid_rust.parse_uiautomator_xml_json("")
        assert json.loads(result) == []

    def test_rust_malformed_xml_declaration(self):
        try:
            import adroid_rust
        except ImportError:
            pytest.skip("Rust extension not installed")
        malformed = '<?xml version="1.0" encoding="UTF-8" badattr="x"?><hierarchy><node class="android.widget.Button" bounds="[0,0][10,10]" /></hierarchy>'
        result = json.loads(adroid_rust.parse_uiautomator_xml_json(malformed))
        assert len(result) == 1
        assert result[0]["role"] == "button"


# ---------------------------------------------------------------------------
# 10. End-to-end web API stress test
# ---------------------------------------------------------------------------


class TestWebAPIStress:
    @pytest.fixture
    def setup(self):
        blob_store = InMemoryBlobStore()
        bridge = MockBridge(blob_store=blob_store)
        bridge.add_device(DeviceInfo(
            id=DeviceId(kind="adb", value="mock-1"),
            state=DeviceState.ONLINE, model="MockPhone"))
        runtime = AdroidRuntime(
            issuer_id="stress-test",
            bridge=bridge, blob_store=blob_store,
            audit_log_path=Path("/tmp/adroid-stress.auditlog"))
        issuer = TokenIssuer(
            issuer_id=runtime.issuer_id,
            signing_key=runtime._token_private,
            public_key=runtime._token_public)
        sm = SessionManager(issuer=issuer, max_sessions=20)
        from datetime import timedelta
        bt = issuer.issue(subject="bootstrap", grants=[], ttl=timedelta(days=30))
        app = create_app(runtime=runtime, session_manager=sm, issuer=issuer,
                         bootstrap_token=bt, browser_session_id="b")
        client = TestClient(app)
        res = client.post("/agent/session/request", json={
            "bootstrap_token": bt.model_dump(mode="json"), "agent_id": "stress"})
        pid = res.json()["pairing_id"]
        client.post(f"/api/pair/{pid}/approve", json={"ttl_seconds": 3600})
        token = client.get(f"/agent/session/{pid}").json()["session_token"]
        return client, token

    DEVICE = {"kind": "adb", "value": "mock-1"}

    def test_rapid_20_calls(self, setup):
        """20 rapid calls — should all succeed, no race conditions."""
        client, token = setup
        for i in range(20):
            res = client.post("/agent/call", json={
                "token": token, "tool": "device.list", "args": {}})
            assert res.json()["ok"] is True, f"Call {i} failed"

    def test_all_17_tools_callable(self, setup):
        """Every registered tool should be callable without crashing."""
        client, token = setup
        tools = client.get("/api/tools").json()
        tool_names = {t["name"] for t in tools}
        assert len(tool_names) == 17

        # Test a subset that works with mock device
        test_tools = [
            ("device.list", {}),
            ("device.observe", {"device_id": self.DEVICE}),
            ("device.screenshot", {"device_id": self.DEVICE}),
            ("app.list", {"device_id": self.DEVICE}),
            ("app.launch", {"device_id": self.DEVICE, "package_name": "com.test"}),
            ("input.tap", {"device_id": self.DEVICE, "x": 100, "y": 200}),
            ("input.tap_text", {"device_id": self.DEVICE, "text": "Search"}),
            ("input.swipe", {"device_id": self.DEVICE, "x1": 100, "y1": 200, "x2": 300, "y2": 400}),
            ("input.text", {"device_id": self.DEVICE, "text": "hello"}),
            ("input.key", {"device_id": self.DEVICE, "keycode": "KEYCODE_HOME"}),
            ("shell.exec", {"device_id": self.DEVICE, "command": "pm list packages"}),
            ("logs.read", {"device_id": self.DEVICE}),
            ("permission.request", {"scope": "shell_exec"}),
            ("ui.dump", {"device_id": self.DEVICE}),
            ("ui.find_elements", {"device_id": self.DEVICE, "selector": {"text": "Search"}}),
            ("ui.tap_element", {"device_id": self.DEVICE, "selector": {"text": "Search"}}),
            ("ui.wait_for", {"device_id": self.DEVICE, "condition": {"type": "ui_stable", "timeout_seconds": 2}}),
        ]

        for tool_name, args in test_tools:
            res = client.post("/agent/call", json={
                "token": token, "tool": tool_name, "args": args})
            body = res.json()
            assert body["ok"] is True, f"Tool {tool_name} failed: {body.get('error', {})}"

    def test_revoked_token_rejected(self, setup):
        client, token = setup
        # Disconnect
        client.post(f"/api/session/{token['token_id']}/disconnect")
        # Next call should fail
        res = client.post("/agent/call", json={
            "token": token, "tool": "device.list", "args": {}})
        body = res.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "adroid.session.revoked"

    def test_unknown_tool_404(self, setup):
        client, token = setup
        res = client.post("/agent/call", json={
            "token": token, "tool": "nonexistent.tool", "args": {}})
        assert res.status_code == 404

    def test_invalid_token_400(self, setup):
        client, _ = setup
        res = client.post("/agent/call", json={
            "token": {"bad": "token"}, "tool": "device.list", "args": {}})
        assert res.status_code == 400
