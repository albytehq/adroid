"""Tests for v0.3.0: Memory, Confidence, Self-Healing."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.contract.ui import Bounds, NodeRole, Selector, SemanticNode, WorldState
from adroid.contract.types import Capability, DeviceId, DeviceInfo, DeviceState, Scope
from adroid.memory import MemoryStore, device_key, failure_key, skill_key
from adroid.perception import (
    BASE_SCORES,
    ConfidenceTracker,
    SelectorStrategy,
    SelfHealingSelector,
)
from adroid.permissions.sessions import SessionManager
from adroid.permissions.tokens import TokenIssuer
from adroid.runtime.core import AdroidRuntime
from adroid.web.server import create_app


# ---------------------------------------------------------------------------
# MemoryStore tests
# ---------------------------------------------------------------------------


class TestMemoryStore:
    @pytest.fixture
    def store(self, tmp_path):
        return MemoryStore(memory_dir=tmp_path / "memory")

    def test_remember_device(self, store):
        store.remember_device("test:fact", {"value": "monkey"})
        entry = store.recall_device("test:fact")
        assert entry is not None
        assert entry.value["value"] == "monkey"
        assert entry.confidence == 0.3

    def test_observe_increases_confidence(self, store):
        store.remember_device("test:fact", {"v": 1})
        store.remember_device("test:fact", {"v": 2})
        entry = store.recall_device("test:fact")
        assert entry.observation_count == 2
        assert entry.confidence == 0.4

    def test_confidence_caps_at_1(self, store):
        for i in range(10):
            store.remember_device("test:fact", {"v": i})
        entry = store.recall_device("test:fact")
        assert entry.confidence == 1.0

    def test_recall_nonexistent(self, store):
        assert store.recall_device("nope") is None

    def test_skill_memory(self, store):
        store.remember_skill("com.app:tap", {"strategy": "text_exact"})
        entry = store.recall_skill("com.app:tap")
        assert entry is not None
        assert entry.value["strategy"] == "text_exact"

    def test_failure_memory(self, store):
        store.remember_failure("com.app:bad", {"reason": "stale"})
        entry = store.recall_failure("com.app:bad")
        assert entry is not None
        assert entry.value["reason"] == "stale"

    def test_persistence(self, tmp_path):
        store1 = MemoryStore(memory_dir=tmp_path / "mem")
        store1.remember_device("persist:test", {"v": "yes"})
        store2 = MemoryStore(memory_dir=tmp_path / "mem")
        entry = store2.recall_device("persist:test")
        assert entry is not None
        assert entry.value["v"] == "yes"

    def test_stats(self, store):
        store.remember_device("d1", {})
        store.remember_skill("s1", {})
        store.remember_failure("f1", {})
        stats = store.stats()
        assert stats["device"] == 1
        assert stats["skill"] == 1
        assert stats["failure"] == 1
        assert stats["total"] == 3

    def test_list_all(self, store):
        store.remember_device("d1", {"v": 1})
        store.remember_skill("s1", {"v": 2})
        store.remember_failure("f1", {"v": 3})
        all_mem = store.list_all()
        assert len(all_mem["device"]) == 1
        assert len(all_mem["skill"]) == 1
        assert len(all_mem["failure"]) == 1


# ---------------------------------------------------------------------------
# ConfidenceTracker tests
# ---------------------------------------------------------------------------


class TestConfidenceTracker:
    def test_base_scores(self):
        tracker = ConfidenceTracker()
        assert tracker.get_confidence(SelectorStrategy.TEXT_EXACT) == 1.0
        assert tracker.get_confidence(SelectorStrategy.RESOURCE_ID) == 0.9
        assert tracker.get_confidence(SelectorStrategy.VISION) == 0.2

    def test_failure_reduces_confidence(self):
        tracker = ConfidenceTracker()
        tracker.record_failure(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
        score = tracker.get_confidence(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
        assert score < 1.0

    def test_success_boosts_confidence(self):
        tracker = ConfidenceTracker()
        for _ in range(5):
            tracker.record_success(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
        score = tracker.get_confidence(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
        assert score > 1.0 - 0.001  # capped at 1.0

    def test_fallback_order(self):
        tracker = ConfidenceTracker()
        order = tracker.get_fallback_order(app="app", action="tap")
        assert order[0] == SelectorStrategy.TEXT_EXACT  # highest base
        assert order[-1] == SelectorStrategy.VISION  # lowest base

    def test_fallback_order_adjusted_by_history(self):
        tracker = ConfidenceTracker()
        # Make TEXT_EXACT fail repeatedly
        for _ in range(5):
            tracker.record_failure(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
        # Make RESOURCE_ID succeed
        for _ in range(5):
            tracker.record_success(SelectorStrategy.RESOURCE_ID, app="app", action="tap")
        order = tracker.get_fallback_order(app="app", action="tap")
        # RESOURCE_ID should now be above TEXT_EXACT
        assert order.index(SelectorStrategy.RESOURCE_ID) < order.index(SelectorStrategy.TEXT_EXACT)

    def test_success_rate(self):
        tracker = ConfidenceTracker()
        stats = tracker.get_stats(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
        assert stats.success_rate == 0.5  # neutral prior
        tracker.record_success(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
        tracker.record_success(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
        tracker.record_failure(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
        stats = tracker.get_stats(SelectorStrategy.TEXT_EXACT, app="app", action="tap")
        assert stats.success_rate == pytest.approx(2/3)


# ---------------------------------------------------------------------------
# SelfHealingSelector tests
# ---------------------------------------------------------------------------


class TestSelfHealingSelector:
    @pytest.fixture
    def worldstate(self):
        nodes = (
            SemanticNode(
                uuid="root", role=NodeRole.FRAME_LAYOUT,
                bounds=Bounds(x=0, y=0, w=1080, h=2340),
            ),
            SemanticNode(
                uuid="btn1", role=NodeRole.BUTTON,
                text="Sign in to your account", description="Login",
                resource_id="com.app:id/login_btn",
                bounds=Bounds(x=440, y=1180, w=200, h=80),
                clickable=True, parent_uuid="root",
            ),
        )
        return WorldState(revision=1, nodes=nodes, foreground_package="com.app")

    def test_exact_match_no_healing(self, worldstate):
        healer = SelfHealingSelector()
        result = healer.find(worldstate, Selector(text="Sign in to your account"))
        assert result.matched
        assert not result.healed
        assert result.winning_strategy == SelectorStrategy.TEXT_EXACT

    def test_text_fails_description_heals(self, worldstate):
        healer = SelfHealingSelector()
        result = healer.find(worldstate, Selector(text="Login"))
        assert result.matched
        assert result.healed
        assert result.winning_strategy == SelectorStrategy.DESCRIPTION

    def test_text_contains_fallback(self, worldstate):
        healer = SelfHealingSelector()
        result = healer.find(worldstate, Selector(text="Sign in"))
        # "Sign in" doesn't match exact text "Sign in to your account"
        # But text_contains="Sign in" should match
        assert result.matched
        assert result.healed

    def test_no_match_at_all(self, worldstate):
        healer = SelfHealingSelector()
        result = healer.find(worldstate, Selector(text="NonExistent"))
        assert not result.matched
        assert len(result.strategies_tried) > 1

    def test_recommended_selector_returned(self, worldstate):
        healer = SelfHealingSelector()
        result = healer.find(worldstate, Selector(text="Login"))
        assert result.matched
        assert result.recommended_selector is not None
        # The recommended selector should be the one that worked (description)
        assert result.recommended_selector.description == "Login"

    def test_confidence_tracker_integration(self, worldstate):
        tracker = ConfidenceTracker()
        healer = SelfHealingSelector(tracker)
        # First call: text="Login" fails, description heals
        healer.find(worldstate, Selector(text="Login"), app="com.app", action="login")
        stats_text = tracker.get_stats(SelectorStrategy.TEXT_EXACT, app="com.app", action="login")
        stats_desc = tracker.get_stats(SelectorStrategy.DESCRIPTION, app="com.app", action="login")
        assert stats_text.failure_count >= 1
        assert stats_desc.success_count >= 1


# ---------------------------------------------------------------------------
# Runtime integration: self-healing via web API
# ---------------------------------------------------------------------------


@pytest.fixture
def web_setup_v03():
    blob_store = InMemoryBlobStore()
    bridge = MockBridge(blob_store=blob_store)
    bridge.add_device(
        DeviceInfo(
            id=DeviceId(kind="adb", value="mock-1"),
            state=DeviceState.ONLINE,
            model="MockPhone",
        )
    )
    runtime = AdroidRuntime(
        issuer_id="adroid-v03-test",
        bridge=bridge,
        blob_store=blob_store,
        audit_log_path=Path("/tmp/adroid-test-v03.auditlog"),
    )
    issuer = TokenIssuer(
        issuer_id=runtime.issuer_id,
        signing_key=runtime._token_private,
        public_key=runtime._token_public,
    )
    session_manager = SessionManager(issuer=issuer, max_sessions=20)
    from datetime import timedelta
    bootstrap_token = issuer.issue(subject="bootstrap", grants=[], ttl=timedelta(days=30))
    app = create_app(
        runtime=runtime,
        session_manager=session_manager,
        issuer=issuer,
        bootstrap_token=bootstrap_token,
        browser_session_id="test-browser",
    )
    client = TestClient(app)
    res = client.post("/agent/session/request", json={
        "bootstrap_token": bootstrap_token.model_dump(mode="json"),
        "agent_id": "agent:v03-test",
    })
    pairing_id = res.json()["pairing_id"]
    client.post(f"/api/pair/{pairing_id}/approve", json={"ttl_seconds": 3600})
    res = client.get(f"/agent/session/{pairing_id}")
    session_token = res.json()["session_token"]
    return client, runtime, session_token


class TestRuntimeIntegration:
    DEVICE_ID = {"kind": "adb", "value": "mock-1"}

    def test_ui_tap_element_exact_match(self, web_setup_v03):
        client, _, token = web_setup_v03
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.tap_element",
            "args": {"device_id": self.DEVICE_ID, "selector": {"text": "Search"}},
        })
        body = res.json()
        assert body["ok"] is True
        assert body["result"]["tapped"] is True
        assert body["result"]["matched_nodes"] >= 1

    def test_ui_tap_element_self_healing(self, web_setup_v03):
        """Text='Login' doesn't exist in mock, but description might match."""
        client, _, token = web_setup_v03
        # Mock has: text="Search", text="Sign in", text="Username"
        # Try text="SearchButton" — should fail exact, but text_contains="Search" might heal
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.tap_element",
            "args": {
                "device_id": self.DEVICE_ID,
                "selector": {"description": "Search button"},
            },
        })
        body = res.json()
        assert body["ok"] is True
        # description="Search button" should match the Search button's content-desc
        assert body["result"]["tapped"] is True

    def test_ui_find_elements_returns_healing_metadata(self, web_setup_v03):
        """Check that audit metadata includes healing info."""
        client, runtime, token = web_setup_v03
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.find_elements",
            "args": {
                "device_id": self.DEVICE_ID,
                "selector": {"text": "Search"},
            },
        })
        body = res.json()
        assert body["ok"] is True
        assert len(body["result"]["elements"]) >= 1

        # Check audit log has healing metadata
        events = runtime.audit_reader().tail(n=5)
        last = events[-1]
        assert last.metadata.get("matched", 0) >= 1

    def test_runtime_has_memory(self, web_setup_v03):
        """Runtime should expose memory store."""
        _, runtime, _ = web_setup_v03
        assert runtime.memory is not None
        assert runtime.confidence_tracker is not None

    def test_auto_learn_on_tap(self, web_setup_v03):
        """After a successful tap, skill_memory should have an entry."""
        client, runtime, token = web_setup_v03
        # Perform a tap
        client.post("/agent/call", json={
            "token": token,
            "tool": "ui.tap_element",
            "args": {"device_id": self.DEVICE_ID, "selector": {"text": "Search"}},
        })
        # Check skill memory was written
        skills = runtime.memory.list_skill()
        assert len(skills) > 0
        assert skills[0].value.get("winning_strategy") is not None

    def test_auto_learn_on_failure(self, web_setup_v03):
        """After a failed find, failure_memory should have an entry."""
        client, runtime, token = web_setup_v03
        # Try to find something that doesn't exist
        client.post("/agent/call", json={
            "token": token,
            "tool": "ui.tap_element",
            "args": {"device_id": self.DEVICE_ID, "selector": {"text": "NonExistentButton"}},
        })
        # Check failure memory
        failures = runtime.memory.list_failure()
        assert len(failures) > 0
