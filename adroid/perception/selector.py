"""Self-healing selector — multi-strategy fallback for UI element matching.

When a selector fails to match any element, the runtime tries alternative
strategies in descending confidence order. This is inspired by Selenium's
self-healing locators and Appium's fallback patterns (research-verified
from self-healing test automation literature, 2024-2026).

Fallback chain (default order, adjusted by ConfidenceTracker):
  1. text (exact)           — highest confidence
  2. resource_id (exact)    — stable across UI changes
  3. description            — content-desc, usually stable
  4. text_contains          — substring match
  5. resource_id (suffix)   — short-form ID match
  6. class_name             — low specificity
  7. bounds_only            — last resort (v0.4.0 will add vision)

If the original selector has text="Login" and it fails, the runtime
automatically tries:
  - description="Login"     (maybe the dev set content-desc)
  - text_contains="Login"   (maybe it's "Login to your account")
  - class_name matching Button + any text containing "Login"

Each attempt is logged to the ConfidenceTracker + failure_memory.
The first successful strategy becomes the "recommended" strategy for
future calls to the same app+action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adroid.contract.ui import SemanticNode, Selector, WorldState
from adroid.perception.confidence import (
    ConfidenceTracker,
    SelectorStrategy,
)


@dataclass
class HealingResult:
    """Result of a self-healing selector attempt.

    If healed=True: the original selector failed but a fallback found
    a match. The recommended_selector is what the agent should use next
    time (for efficiency).

    If healed=False: no strategy matched. Agent should consider:
      - scroll and retry
      - take a screenshot for VLM analysis
      - ask the user
    """

    matched: bool
    nodes: tuple[SemanticNode, ...]
    original_strategy: SelectorStrategy
    winning_strategy: SelectorStrategy | None
    confidence: float
    healed: bool  # True if fallback was needed
    strategies_tried: list[SelectorStrategy]
    recommended_selector: Selector | None  # what to use next time


class SelfHealingSelector:
    """Multi-strategy UI element finder with fallback.

    Wraps WorldState.find() with automatic strategy degradation.
    When the primary selector fails, tries alternative selectors
    derived from the same target description.

    Usage:
        healer = SelfHealingSelector(confidence_tracker)
        result = healer.find(world_state, selector, app="com.youtube")
        if result.matched:
            # result.nodes[0] is the element
            # result.winning_strategy tells you which strategy worked
            # result.recommended_selector is optimized for next time
    """

    def __init__(self, tracker: ConfidenceTracker | None = None) -> None:
        self._tracker = tracker or ConfidenceTracker()

    @property
    def tracker(self) -> ConfidenceTracker:
        return self._tracker

    def find(
        self,
        world_state: WorldState,
        selector: Selector,
        *,
        app: str = "",
        action: str = "",
    ) -> HealingResult:
        """Find elements with self-healing fallback.

        Tries the original selector first. If no match, derives
        alternative selectors from the same target and tries them
        in confidence order.
        """
        strategies_tried: list[SelectorStrategy] = []

        # Step 1: Try original selector
        original_strategy = self._classify_selector(selector)
        strategies_tried.append(original_strategy)

        matches = world_state.find(selector)
        if matches:
            self._tracker.record_success(original_strategy, app=app, action=action)
            confidence = self._tracker.get_confidence(
                original_strategy, app=app, action=action
            )
            return HealingResult(
                matched=True,
                nodes=matches,
                original_strategy=original_strategy,
                winning_strategy=original_strategy,
                confidence=confidence,
                healed=False,
                strategies_tried=strategies_tried,
                recommended_selector=selector,
            )

        # Step 2: Original failed — record failure + try fallbacks
        self._tracker.record_failure(original_strategy, app=app, action=action)

        # Generate fallback selectors
        fallbacks = self._generate_fallbacks(selector)
        fallback_order = self._tracker.get_fallback_order(app=app, action=action)

        for strategy in fallback_order:
            if strategy == original_strategy:
                continue
            if strategy not in fallbacks:
                continue

            strategies_tried.append(strategy)
            fallback_selector = fallbacks[strategy]
            matches = world_state.find(fallback_selector)

            if matches:
                self._tracker.record_success(strategy, app=app, action=action)
                confidence = self._tracker.get_confidence(
                    strategy, app=app, action=action
                )
                return HealingResult(
                    matched=True,
                    nodes=matches,
                    original_strategy=original_strategy,
                    winning_strategy=strategy,
                    confidence=confidence,
                    healed=True,
                    strategies_tried=strategies_tried,
                    recommended_selector=fallback_selector,
                )
            else:
                self._tracker.record_failure(strategy, app=app, action=action)

        # All strategies failed
        return HealingResult(
            matched=False,
            nodes=(),
            original_strategy=original_strategy,
            winning_strategy=None,
            confidence=0.0,
            healed=False,
            strategies_tried=strategies_tried,
            recommended_selector=None,
        )

    # ------------------------------------------------------------------
    # Selector classification + fallback generation
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_selector(selector: Selector) -> SelectorStrategy:
        """Classify which strategy the original selector uses."""
        if selector.text is not None:
            return SelectorStrategy.TEXT_EXACT
        if selector.resource_id is not None:
            # Could be exact or suffix — we treat as RESOURCE_ID
            return SelectorStrategy.RESOURCE_ID
        if selector.description is not None:
            return SelectorStrategy.DESCRIPTION
        if selector.text_contains is not None:
            return SelectorStrategy.TEXT_CONTAINS
        if selector.class_name is not None:
            return SelectorStrategy.CLASS_NAME
        return SelectorStrategy.BOUNDS_ONLY

    @staticmethod
    def _generate_fallbacks(selector: Selector) -> dict[SelectorStrategy, Selector]:
        """Generate fallback selectors from the original selector's intent.

        The idea: if the agent asked for text="Login", we derive:
          - description="Login"       (maybe content-desc is set)
          - text_contains="Login"     (maybe it's "Login to account")
          - class_name="android.widget.Button" + text_contains="Login"
            (maybe it's a custom button class)

        If the agent asked for resource_id="login_btn", we derive:
          - text_contains derived from the ID (heuristic: split by _ → "login")

        If the agent asked for description="Submit", we derive:
          - text="Submit"              (maybe text is set instead)
          - text_contains="Submit"
        """
        fallbacks: dict[SelectorStrategy, Selector] = {}

        # Derive from text
        text_value = selector.text or selector.description
        if text_value:
            # Try as description
            if selector.description is None:
                fallbacks[SelectorStrategy.DESCRIPTION] = Selector(
                    description=text_value,
                    match=selector.match,
                )
            # Try as text_contains (substring match)
            if selector.text_contains is None:
                fallbacks[SelectorStrategy.TEXT_CONTAINS] = Selector(
                    text_contains=text_value,
                    match=selector.match,
                )
            # Try class_name + text_contains combo
            fallbacks[SelectorStrategy.CLASS_NAME] = Selector(
                text_contains=text_value,
                class_name="android.widget.Button",
                match=selector.match,
            )

        # Derive from resource_id (heuristic: extract meaningful part)
        if selector.resource_id:
            # Split "login_btn" → "login" → try as text_contains
            parts = selector.resource_id.replace("/", " ").replace("_", " ").split()
            meaningful = " ".join(parts) if parts else selector.resource_id
            if meaningful:
                fallbacks[SelectorStrategy.TEXT_CONTAINS] = Selector(
                    text_contains=meaningful,
                    match=selector.match,
                )

        # Derive from text_contains → try as exact text
        if selector.text_contains and selector.text is None:
            fallbacks[SelectorStrategy.TEXT_EXACT] = Selector(
                text=selector.text_contains,
                match=selector.match,
            )

        return fallbacks


__all__ = [
    "HealingResult",
    "SelfHealingSelector",
]
